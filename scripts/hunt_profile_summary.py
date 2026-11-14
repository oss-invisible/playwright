"""HUNT ONLY (branch hunt/cpu-render-path, never merged): read the Gecko profiles of scripts/hunt_cpu_render_path.py and print what a person
would otherwise click through in the Firefox Profiler.

    python scripts/hunt_profile_summary.py DIR [--symbols ZIP ...] [--detail 2] [--ids]

DIR holds profile-<pid>.json(.gz), one per process (written at each process's clean shutdown, MOZ_PROFILER_SHUTDOWN=...%p.json). A file may
also carry the profiles of child processes under "processes"; those are read too.

For every process: pid, type (the thread's processType: default = the parent, gpu, tab, socket, ...), threads, samples and the CPU the
profiler measured (the `cpu` feature: threadCPUDelta of every sample, microseconds; without it every sample counts 1).
For the DETAIL busiest files of each of the types default and gpu, and for the four busiest threads of each: the CPU by module of the leaf
frame (self time: xul.dll, d3d10warp.dll, a vendor driver, ntdll, ...) and the twelve hottest leaf functions.

Symbols. A native frame is a module and an RVA. Module names are always known (the profile's libs). Function names come from
  1. Breakpad .sym files inside the zips given with --symbols (our engine: the candidate's artifact symbols-win-x86_64), matched on the
     module's debug name and breakpad id, streamed (only the RVAs asked for are kept);
  2. Mozilla's symbol server (https://symbolication.services.mozilla.com/symbolicate/v5), for every module the zips do not have.
A module neither source knows stays `module+0xRVA`, and the report says which modules were and were not symbolicated. Nothing here fails
the job: every problem is printed as a line.

--ids prints `debugName breakpadId name` of the libs of the first parent profile and exits (used to pick the symbols artifact).

Formats read: tables as columns (`{"length": n, "func": [...]}`) or as `{"schema": {...}, "data": [[...]]}`; frames through funcTable and
resourceTable, or through a `location` string (absolute address `0x...`, mapped to a lib by the libs' start/end).
"""
from __future__ import annotations

import argparse
import bisect
import collections
import gzip
import io
import json
import re
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

SERVER = "https://symbolication.services.mozilla.com/symbolicate/v5"
HEX = re.compile(r"^0x[0-9a-fA-F]+$")
PROCESS_TYPES = {0: "default", 1: "plugin", 2: "tab", 3: "ipdlunittest", 4: "gmplugin", 5: "gpu", 6: "vr", 7: "rdd", 8: "socket",
                 9: "remotesandboxbroker", 10: "forkserver", 11: "utility"}


def load_json(path: Path) -> dict:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as f:
        return json.loads(f.read().decode("utf-8", "replace"))


def walk(profile: dict) -> Iterator[dict]:
    yield profile
    for sub in profile.get("processes") or []:
        yield from walk(sub)


def cols(t: Any) -> dict:
    """A table as {field: list}, whichever of the two shapes it has."""
    if not t:
        return {}
    if isinstance(t, dict) and "schema" in t and "data" in t:
        return {k: [row[i] if i < len(row) else None for row in t["data"]] for k, i in t["schema"].items()}
    return t


def strings(profile: dict, thread: dict) -> List[str]:
    return thread.get("stringArray") or thread.get("stringTable") or (profile.get("shared") or {}).get("stringArray") or []


def lib_of_address(libs: List[dict], addr: int) -> Tuple[Optional[int], int]:
    for i, lib in enumerate(libs):
        start, end = lib.get("start"), lib.get("end")
        if isinstance(start, int) and isinstance(end, int) and start <= addr < end:
            return i, addr - start + (lib.get("offset") or 0)
    return None, addr


class Frames:
    """For one thread: frame index -> (key, label, lib index or None, rva or None, is_js)."""

    def __init__(self, profile: dict, thread: dict) -> None:
        libs = profile.get("libs") or thread.get("libs") or []
        strs = strings(profile, thread)

        def s(i):
            return strs[i] if isinstance(i, int) and 0 <= i < len(strs) else (i if isinstance(i, str) else "?")

        ft = cols(thread.get("frameTable"))
        self.items: List[tuple] = []
        n = ft.get("length") or max((len(v) for v in ft.values() if isinstance(v, list)), default=0)
        if thread.get("funcTable"):
            fu, res = cols(thread["funcTable"]), cols(thread.get("resourceTable"))
            for i in range(n):
                f = ft["func"][i]
                name = s(fu["name"][f])
                isjs = fu.get("isJS") or []
                is_js = bool(isjs[f]) if f < len(isjs) else False
                r = fu["resource"][f] if fu.get("resource") else None
                lib = res["lib"][r] if isinstance(r, int) and r >= 0 and res.get("lib") and r < len(res["lib"]) else None
                addr = ft["address"][i] if ft.get("address") else None
                rva = addr if isinstance(addr, int) and addr >= 0 else (int(name, 16) if HEX.match(name) else None)
                if lib is None and HEX.match(name) and rva is not None:
                    lib, rva = lib_of_address(libs, rva)
                self._add(libs, name, lib, rva, is_js)
        else:
            for i in range(n):
                loc = s(ft["location"][i]) if ft.get("location") else "?"
                if HEX.match(loc):
                    lib, rva = lib_of_address(libs, int(loc, 16))
                    self._add(libs, loc, lib, rva if lib is not None else None, False)
                else:
                    self._add(libs, loc, None, None, True)

    def _add(self, libs, name, lib, rva, is_js) -> None:
        if lib is not None and rva is not None and 0 <= lib < len(libs):
            mod = libs[lib].get("name") or "?"
            self.items.append((("n", lib, rva), "%s+0x%x" % (mod, rva), lib, rva, False))
        elif is_js:
            self.items.append((("j", name), "[JS] " + name, None, None, True))
        else:
            self.items.append((("o", name), name, None, None, False))


def thread_weights(profile: dict, thread: dict) -> Tuple[Dict[int, float], int, float]:
    """({leaf frame: weight}, samples, total weight). Weight = threadCPUDelta (us) when the profile has it, else 1 per sample."""
    sm = cols(thread.get("samples"))
    stacks = sm.get("stack") or []
    deltas = sm.get("threadCPUDelta")
    st = cols(thread.get("stackTable"))
    frame_of = st.get("frame") or []
    use_cpu = bool(deltas) and any(isinstance(d, (int, float)) and d for d in deltas)
    out: Dict[int, float] = collections.defaultdict(float)
    total = 0.0
    for i, stack in enumerate(stacks):
        if stack is None or not isinstance(stack, int) or stack >= len(frame_of):
            continue
        w = float(deltas[i] or 0) if use_cpu else 1.0
        out[frame_of[stack]] += w
        total += w
    return out, len(stacks), total


# ----------------------------------------------------------------------------------------------------------- symbolication

def local_symbols(zips: List[Path], wanted: Dict[Tuple[str, str], List[int]], found: Dict[Tuple[str, str, int], str], notes: List[str]) -> None:
    for z in zips:
        try:
            zf = zipfile.ZipFile(z)
        except Exception as exc:   # noqa: BLE001
            notes.append("symbols zip %s unreadable: %s" % (z, exc))
            continue
        names = zf.namelist()
        for (debug, bid), rvas in wanted.items():
            members = [n for n in names if n.endswith(".sym") and ("/%s/%s/" % (debug, bid)) in "/" + n]
            if not members:
                continue
            rvas = sorted(set(rvas))
            hits = 0
            with zf.open(members[0]) as raw:
                for line in io.TextIOWrapper(raw, encoding="utf-8", errors="replace"):
                    if not line.startswith("FUNC "):
                        continue
                    rest = line[5:]
                    if rest.startswith("m "):
                        rest = rest[2:]
                    try:
                        addr_s, size_s, _param, name = rest.split(" ", 3)
                        addr, size = int(addr_s, 16), int(size_s, 16)
                    except ValueError:
                        continue
                    j = bisect.bisect_left(rvas, addr)
                    while j < len(rvas) and rvas[j] < addr + max(size, 1):
                        if (debug, bid, rvas[j]) not in found:
                            found[(debug, bid, rvas[j])] = name.strip()
                            hits += 1
                        j += 1
            notes.append("local symbols: %s %s: %d of %d addresses named (from %s)" % (debug, bid, hits, len(rvas), z.name))


def server_symbols(wanted: Dict[Tuple[str, str], List[int]], found: Dict[Tuple[str, str, int], str], notes: List[str]) -> None:
    for (debug, bid), rvas in wanted.items():
        todo = sorted({r for r in rvas if (debug, bid, r) not in found})
        if not todo:
            continue
        hits = 0
        try:
            for k in range(0, len(todo), 400):
                chunk = todo[k:k + 400]
                body = json.dumps({"jobs": [{"memoryMap": [[debug, bid]], "stacks": [[[0, r] for r in chunk]]}]}).encode()
                req = urllib.request.Request(SERVER, body, {"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read())
                frames = data["results"][0]["stacks"][0]
                for r, fr in zip(chunk, frames):
                    fn = fr.get("function")
                    if fn and not HEX.match(str(fn)):
                        found[(debug, bid, r)] = str(fn)
                        hits += 1
            notes.append("Mozilla symbol server: %s %s: %d of %d addresses named" % (debug, bid, hits, len(todo)))
        except Exception as exc:   # noqa: BLE001
            notes.append("Mozilla symbol server: %s %s: %s: %s" % (debug, bid, type(exc).__name__, str(exc)[:200]))


# ------------------------------------------------------------------------------------------------------------------ report

def process_type(profile: dict) -> str:
    for t in profile.get("threads") or []:
        if t.get("processType"):
            return str(t["processType"])
    pt = (profile.get("meta") or {}).get("processType")
    return PROCESS_TYPES.get(pt, str(pt))


def pid_of(profile: dict) -> Any:
    for t in profile.get("threads") or []:
        if t.get("pid") is not None:
            return t["pid"]
    return (profile.get("meta") or {}).get("pid", "?")


def analyse(profile: dict) -> dict:
    libs = profile.get("libs") or []
    threads = []
    for t in profile.get("threads") or []:
        try:
            frames = Frames(profile, t)
            weights, nsamples, total = thread_weights(profile, t)
            self_by_key: Dict[tuple, float] = collections.defaultdict(float)
            info: Dict[tuple, tuple] = {}
            for fi, w in weights.items():
                if 0 <= fi < len(frames.items):
                    key, label, lib, rva, js = frames.items[fi]
                    self_by_key[key] += w
                    info[key] = (label, lib, rva, js)
            threads.append({"name": t.get("name") or "?", "tid": t.get("tid"), "samples": nsamples, "total": total,
                            "self": self_by_key, "info": info})
        except Exception as exc:   # noqa: BLE001
            threads.append({"name": t.get("name") or "?", "tid": t.get("tid"), "samples": 0, "total": 0.0, "self": {}, "info": {},
                            "error": "%s: %s" % (type(exc).__name__, str(exc)[:160])})
    return {"profile": profile, "libs": libs, "threads": threads, "type": process_type(profile), "pid": pid_of(profile),
            "total": sum(t["total"] for t in threads), "samples": sum(t["samples"] for t in threads),
            "cpu": any(((cols(t.get("samples")).get("threadCPUDelta")) for t in profile.get("threads") or []))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", type=Path)
    ap.add_argument("--symbols", nargs="*", default=[], type=Path)
    ap.add_argument("--detail", type=int, default=2)
    ap.add_argument("--ids", action="store_true")
    args = ap.parse_args()
    files = sorted(list(args.dir.glob("profile-*.json")) + list(args.dir.glob("profile-*.json.gz")))
    if not files:
        print("no profile files in %s" % args.dir)
        return 0
    procs = []
    for f in files:
        try:
            for p in walk(load_json(f)):
                if p.get("threads"):
                    a = analyse(p)
                    a["file"] = f.name
                    procs.append(a)
        except Exception as exc:   # noqa: BLE001
            print("%s: unreadable: %s: %s" % (f.name, type(exc).__name__, str(exc)[:200]))
    if args.ids:
        for a in procs:
            if a["type"] == "default":
                for lib in a["libs"]:
                    if (lib.get("name") or "").lower() in ("xul.dll", "firefox.exe", "d3d10warp.dll"):
                        print(lib.get("debugName"), lib.get("breakpadId"), lib.get("name"))
                break
        return 0
    unit = "us of CPU" if any(a["cpu"] for a in procs) else "samples"
    print("%d process profile(s) from %d file(s); weights are %s" % (len(procs), len(files), unit))
    print("%-8s %-26s %7s %9s %14s   file" % ("pid", "type", "threads", "samples", "weight"))
    for a in sorted(procs, key=lambda a: (a["type"], -a["total"])):
        print("%-8s %-26s %7d %9d %14.0f   %s" % (a["pid"], a["type"], len(a["threads"]), a["samples"], a["total"], a["file"]))
    by_type: Dict[str, float] = collections.defaultdict(float)
    for a in procs:
        by_type[a["type"]] += a["total"]
    print("total weight by process type: " + ", ".join("%s %.0f" % kv for kv in sorted(by_type.items(), key=lambda kv: -kv[1])))

    chosen = []
    for kind in ("default", "gpu"):
        chosen += sorted([a for a in procs if a["type"] == kind], key=lambda a: -a["total"])[:args.detail]
    # which addresses to name
    wanted: Dict[Tuple[str, str], List[int]] = collections.defaultdict(list)
    for a in chosen:
        for t in sorted(a["threads"], key=lambda t: -t["total"])[:4]:
            for key, w in sorted(t["self"].items(), key=lambda kv: -kv[1])[:12]:
                label, lib, rva, js = t["info"][key]
                if lib is not None and rva is not None:
                    L = a["libs"][lib]
                    if L.get("debugName") and L.get("breakpadId"):
                        wanted[(L["debugName"], L["breakpadId"])].append(rva)
    found: Dict[Tuple[str, str, int], str] = {}
    notes: List[str] = []
    zips = [p for p in args.symbols if p.is_file()]
    if wanted:
        if zips:
            local_symbols(zips, wanted, found, notes)
        else:
            notes.append("no symbols zip given: our engine's functions stay module+offset")
        server_symbols(wanted, found, notes)
    print("symbolication:")
    for n in notes:
        print("  " + n)

    for a in chosen:
        print("=" * 100)
        print("process %s, type %s, file %s: %d thread(s), weight %.0f %s" % (a["pid"], a["type"], a["file"], len(a["threads"]), a["total"], unit))
        for t in sorted(a["threads"], key=lambda t: -t["total"])[:4]:
            print("-" * 100)
            print("thread %s (tid %s): %d samples, weight %.0f%s" % (t["name"], t["tid"], t["samples"], t["total"],
                                                                   "   ERROR " + t["error"] if t.get("error") else ""))
            by_mod: Dict[str, float] = collections.defaultdict(float)
            for key, w in t["self"].items():
                label, lib, rva, js = t["info"][key]
                by_mod["[JS]" if js else (a["libs"][lib].get("name") if lib is not None else "(no module: %s)" % label[:40])] += w
            tot = t["total"] or 1
            print("  self weight by module: " + ", ".join("%s %.0f (%.0f%%)" % (m, w, 100 * w / tot)
                                                          for m, w in sorted(by_mod.items(), key=lambda kv: -kv[1])[:8]))
            for key, w in sorted(t["self"].items(), key=lambda kv: -kv[1])[:12]:
                label, lib, rva, js = t["info"][key]
                if lib is not None and rva is not None:
                    L = a["libs"][lib]
                    name = found.get((L.get("debugName"), L.get("breakpadId"), rva))
                    if name:
                        label = "%s!%s" % (L.get("name"), name)
                print("  %9.0f %5.1f%%  %s" % (w, 100 * w / tot, label[:200]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
