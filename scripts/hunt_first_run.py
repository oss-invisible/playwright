"""HUNT ONLY (branch hunt/cpu-render-path, never merged): what happens in the first 80 seconds of a NEW profile, and what can a page read of it?

    python scripts/hunt_first_run.py fetch-retail --out DIR [--retail-version 159.0]       # once: Mozilla's installer, verified, unpacked
    python scripts/hunt_first_run.py census --arm retail --retail-from DIR --out DIR2        # 1. the first 80 s of a fresh profile, in 10 s slices
    python scripts/hunt_first_run.py census --arm ours   --binary "$FF"     --out DIR2
    python scripts/hunt_first_run.py census-table DIR2                                       # ours and retail in one table
    python scripts/hunt_first_run.py probe --arm retail_fresh|retail_used|ours_fresh|ours_used --mode seq|late ... --out DIR3   # 2. what a page reads
    python scripts/hunt_first_run.py probe-table DIR3

THE QUESTION OF PART 1. In ours with a fresh profile the parent shows a ~15 s CPU burst between +8 and +50 s after the launch, with these downloads:
remote settings 18.9 MB, CRLite 6 MB, safebrowsing 8.5 MB, Widevine 23 MB, the newtab xpi; after that it drops to zero. Does retail Firefox 159.0 (the
stable release, Mozilla's installer checked against its SHA256SUMS, launched by hand: subprocess, -no-remote, a fresh profile, an idle local page, no
automation prefs, no Playwright, the environment untouched) show the same burst, with the same duration and the same downloads?
  census     one browser for 80 s. The parent's CPU per 10 s slice (the sampler of hunt_cpu_per_browser.py: NtQuerySystemInformation every 250 ms) and per
             thread NAME per slice (hunt_cpu_baseline.Watcher: psutil threads + GetThreadDescription every 0.5 s); the files written in the PROFILE per slice
             (the profile directory is walked every 2 s: path, size, grouped by top-level directory, and every file in gmp-*, security_state, safebrowsing,
             settings, features, extensions listed with its size); the bytes received and sent by the machine's network interfaces per slice (psutil
             net_io_counters, loopback excluded: the runner is otherwise idle, but its own agent traffic is in the number). A process that lives less than
             the polling period is not seen; a file created and deleted between two walks is not seen.
  Retail is closed at the end the way a user closes it (taskkill without /F, WM_CLOSE), so that the profile is left as a used one.

THE QUESTION OF PART 2. A page reads state that first-run downloads change: requestMediaKeySystemAccess waits for the Widevine download (it is rejected
after 60 s if there is none), the WebRTC H.264 entries depend on the OpenH264 GMP (and in ours on a declaration), and so on; the code that shows it is
cited in the table headings of probe-table. `probe` serves a page (scripts/hunt_first_run_probe.js) that runs every probe at +5, +20, +40 and +90 s after the
launch and posts, per probe and moment: the outcome (resolved, rejected, timeout), the time the probe took, and the value (the configuration returned, the
codec lists, ...). Arms: ours_fresh (a new profile_dir), ours_used (the same profile_dir, launched a second time after an 80 s first launch), retail_fresh,
retail_used (retail launched by hand a second time on the profile of the census). Modes: `seq` (all four moments in one launch: an early probe can start a
download, so a later one answers after it) and `late` (ONE moment, +90 s, nothing before it: it shows whether asking early changed the answer).
`probe-table` prints a table per probe and moment and then, for each arm family, the probes whose answer differs between a fresh and a used profile.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hunt_cpu_baseline as hb   # noqa: E402
import hunt_cpu_per_browser as hunt   # noqa: E402

WINDOW_S = 80.0
SLICE_S = 10.0
MOMENT_GRACE_S = 95.0          # a probe may wait 80 s: the run lasts until the last moment + this
WATCHED = re.compile(r"(?i)^(gmp-|security_state|safebrowsing|settings|features|extensions|datareporting|startupCache|storage|saved-telemetry|"
                     r"crashes|minidumps|sessionstore|bookmarkbackups|cache2|shader-cache|offlineCache)")
BIG = 100 * 1024


class Idle(BaseHTTPRequestHandler):
    """The page every browser of part 1 opens: nothing on it."""
    t0_ms = 0
    results: List[Dict[str, Any]] = []
    lock = threading.Lock()

    def _send(self, body: bytes, ctype: str = "text/html; charset=utf-8", code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):   # noqa: N802
        path = self.path.split("?")[0]
        if path == "/t0":
            self._send(str(Idle.t0_ms).encode(), "text/plain")
        elif path == "/probe":
            self._send(b'<!doctype html><meta charset=utf-8><title>probe</title><body><script src="/probe.js"></script></body>')
        elif path == "/probe.js":
            self._send((HERE / "hunt_first_run_probe.js").read_bytes(), "text/javascript; charset=utf-8")
        else:
            self._send(b"<!doctype html><meta charset=utf-8><title>idle</title><body>idle</body>")

    def do_POST(self):   # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        try:
            row = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
            row["arrived_ms"] = int(time.time() * 1000) - Idle.t0_ms
            with Idle.lock:
                Idle.results.append(row)
        except Exception:   # noqa: BLE001
            pass
        self._send(b"ok", "text/plain")

    def log_message(self, *a):   # noqa: D102
        pass


# -------------------------------------------------------------------------------------------------------------- the watchers

class Net(threading.Thread):
    """Bytes received and sent by every non-loopback interface, once a second."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.stop_flag = threading.Event()
        self.rows: List[Tuple[float, int, int]] = []

    def run(self) -> None:
        import psutil
        while not self.stop_flag.is_set():
            try:
                c = psutil.net_io_counters(pernic=True)
                recv = sum(v.bytes_recv for k, v in c.items() if not re.search(r"(?i)loopback|^lo$|isatap|teredo", k))
                sent = sum(v.bytes_sent for k, v in c.items() if not re.search(r"(?i)loopback|^lo$|isatap|teredo", k))
                self.rows.append((time.time(), recv, sent))
            except Exception:   # noqa: BLE001
                pass
            self.stop_flag.wait(1.0)

    def between(self, a: float, b: float) -> Tuple[float, float]:
        def at(t: float, i: int) -> int:
            best = None
            for row in self.rows:
                if row[0] <= t:
                    best = row
            return (best[i] if best else (self.rows[0][i] if self.rows else 0))
        return (at(b, 1) - at(a, 1)) / 1e6, (at(b, 2) - at(a, 2)) / 1e6


class Files(threading.Thread):
    """Walks the profile directory every 2 s and records every file that is new or changed (size, mtime)."""

    def __init__(self, root: Path) -> None:
        super().__init__(daemon=True)
        self.root = root
        self.stop_flag = threading.Event()
        self.state: Dict[str, Tuple[int, int]] = {}
        self.events: List[Tuple[float, str, int, str]] = []     # (time, relative path, size, new|changed)
        self.sizes_at: List[Tuple[float, Dict[str, int]]] = []   # (time, size by top-level dir)

    def scan(self) -> None:
        now = time.time()
        seen: Dict[str, Tuple[int, int]] = {}
        by_top: Dict[str, int] = {}
        if self.root.is_dir():
            for dirpath, _dirs, names in os.walk(self.root):
                for n in names:
                    p = os.path.join(dirpath, n)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    rel = os.path.relpath(p, self.root).replace("\\", "/")
                    seen[rel] = (st.st_size, st.st_mtime_ns)
                    top = rel.split("/")[0] if "/" in rel else "(root)"
                    by_top[top] = by_top.get(top, 0) + st.st_size
        for rel, v in seen.items():
            old = self.state.get(rel)
            if old is None:
                self.events.append((now, rel, v[0], "new"))
            elif old != v:
                self.events.append((now, rel, v[0], "changed"))
        self.state = seen
        self.sizes_at.append((now, by_top))

    def run(self) -> None:
        while not self.stop_flag.is_set():
            try:
                self.scan()
            except Exception:   # noqa: BLE001
                pass
            self.stop_flag.wait(2.0)

    def finish(self) -> None:
        with contextlib.suppress(Exception):
            self.scan()


# ----------------------------------------------------------------------------------------------------------------- launching

def parents_of_firefox() -> List[int]:
    import psutil
    out = []
    for p in psutil.process_iter(["pid", "name"]):
        if (p.info.get("name") or "").lower().startswith("firefox"):
            try:
                if "-contentproc" not in p.cmdline():
                    out.append(p.info["pid"])
            except Exception:   # noqa: BLE001
                pass
    return out


def close_like_a_user(timeout: float = 25.0) -> str:
    """taskkill without /F (WM_CLOSE) on every parent process, then wait; whatever is left is killed."""
    pids = parents_of_firefox()
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid)] if sys.platform == "win32" else ["kill", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    end = time.time() + timeout
    while time.time() < end and parents_of_firefox():
        time.sleep(0.5)
    left = parents_of_firefox()
    note = "closed gracefully" if not left else "still running after %.0f s: killed" % timeout
    hb.kill_firefox()
    return note


def launch_retail(exe: Path, url: str, profile: Path, t0: float, until: float) -> str:
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([str(exe), "-no-remote", "-profile", str(profile), url], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(max(0.0, until - time.time()))
    note = close_like_a_user()
    with contextlib.suppress(Exception):
        proc.wait(timeout=10)
    return note


async def ours_session(binary: str, url: str, profile: Path, until: float) -> str:
    from invisible_playwright.async_api import InvisiblePlaywright
    profile.mkdir(parents=True, exist_ok=True)
    async with InvisiblePlaywright(seed=100, binary_path=binary, headless=True, locale="en-US", timezone="Europe/Rome",
                                   profile_dir=str(profile)) as browser:
        page = await browser.new_page()
        await page.goto(url, wait_until="load", timeout=60000)
        await asyncio.sleep(max(0.0, until - time.time()))
    return "closed by the context manager"


def retail_exe(args) -> Tuple[Path, str]:
    if args.retail_from:
        info = json.loads((Path(args.retail_from) / "retail.json").read_text(encoding="utf-8"))
        return Path(info["exe"]), info["note"]
    work = Path(tempfile.mkdtemp(prefix="hunt-retail-"))
    exe, why = hb.find_retail(args, work)
    if exe is None:
        raise SystemExit("no retail binary: %s" % why)
    return exe, "%s; %s" % (why, hb.application_ini(exe))


def fetch_retail(args) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    exe, why = hb.find_retail(args, args.out)
    if exe is None:
        print("no retail binary: %s" % why)
        return 1
    note = "%s; %s" % (why, hb.application_ini(exe))
    (args.out / "retail.json").write_text(json.dumps({"exe": str(exe), "note": note}), encoding="utf-8")
    print("RETAIL BINARY: %s\n  %s" % (exe, note))
    return 0


# ------------------------------------------------------------------------------------------------------------------- part 1

def serve() -> Tuple[ThreadingHTTPServer, str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Idle)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:%d" % httpd.server_address[1]


def census(args) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    httpd, base = serve()
    work = Path(tempfile.mkdtemp(prefix="hunt-census-"))
    profile = Path(args.keep_profile) if args.keep_profile else work / "profile"
    if profile.exists():
        shutil.rmtree(profile, ignore_errors=True)
    hb.kill_firefox()
    note = ""
    if args.arm == "retail":
        exe, note = retail_exe(args)
        print("RETAIL BINARY: %s" % note)
    sampler = hunt.Sampler()
    sampler.start()
    watcher = hb.Watcher()
    watcher.start()
    net = Net()
    net.start()
    files = Files(profile)
    files.start()
    err = ""
    t0 = time.time()
    Idle.t0_ms = int(t0 * 1000)
    try:
        if args.arm == "retail":
            how = launch_retail(exe, base + "/idle", profile, t0, t0 + WINDOW_S)
        else:
            how = asyncio.run(ours_session(args.binary, base + "/idle", profile, t0 + WINDOW_S))
        print("closed: %s" % how)
    except BaseException as exc:   # noqa: BLE001
        err = "%s: %s" % (type(exc).__name__, str(exc)[:400])
    time.sleep(0.8)
    for t in (watcher, net, files):
        t.stop_flag.set()
    watcher.join(5)
    net.join(3)
    files.join(5)
    files.finish()
    sampler.stop()
    hb.kill_firefox()
    httpd.shutdown()

    series = hunt.Series(sampler.samples)
    where, _roots = hunt.roots_and_kinds(sampler.info)
    slices = []
    n_slices = int(WINDOW_S / SLICE_S)
    for i in range(n_slices):
        a, b = t0 + i * SLICE_S, t0 + (i + 1) * SLICE_S
        cpu = {k: 0.0 for k in hunt.TYPES}
        for pid, (_root, kind) in where.items():
            cpu[kind] += series.used(pid, a, b)
        threads: Dict[str, float] = {}
        for t in watcher.used(a, b):
            if t["kind"] == "parent":
                threads[hb.normalise(t["name"])] = threads.get(hb.normalise(t["name"]), 0.0) + t["cpu_s"]
        recv, sent = net.between(a, b)
        ev = [e for e in files.events if a <= e[0] < b]
        by_dir: Dict[str, List[int]] = {}
        latest: Dict[str, int] = {}
        for _t, rel, size, _kind in ev:
            latest[rel] = size
        for rel, size in latest.items():
            top = rel.split("/")[0] if "/" in rel else "(root)"
            d = by_dir.setdefault(top, [0, 0])
            d[0] += 1
            d[1] += size
        notable = sorted(((rel, size) for rel, size in latest.items() if WATCHED.match(rel) or size >= BIG), key=lambda x: -x[1])
        end_sizes = {}
        for ts, sizes in files.sizes_at:
            if ts <= b:
                end_sizes = sizes
        slices.append({"slice": i, "from_s": i * SLICE_S, "to_s": (i + 1) * SLICE_S, "cpu": cpu, "threads": threads, "net_recv_mb": recv, "net_sent_mb": sent,
                       "files_by_dir": by_dir, "notable_files": notable[:40], "dir_sizes_at_end": end_sizes})
    first_seen: Dict[str, Tuple[int, int]] = {}
    for ts, rel, size, _k in files.events:
        i = min(n_slices - 1, int((ts - t0) // SLICE_S)) if ts >= t0 else 0
        if rel not in first_seen:
            first_seen[rel] = (i, size)
        else:
            first_seen[rel] = (first_seen[rel][0], size)
    result = {"arm": args.arm, "note": note, "error": err, "window_s": WINDOW_S, "slice_s": SLICE_S, "slices": slices,
              "downloads": sorted(((rel, i, size) for rel, (i, size) in first_seen.items() if size >= BIG or WATCHED.match(rel)), key=lambda x: (x[1], -x[2]))[:300],
              "profile_final_bytes": sum(v[0] for v in files.state.values()), "processes": hunt.process_listing(sampler, t0),
              "watcher_error": watcher.error}
    (args.out / ("%s.json" % args.arm)).write_text(json.dumps(result, indent=1), encoding="utf-8")
    print_census(result)
    if args.keep_profile:
        print("profile kept in %s (%.1f MB)" % (profile, result["profile_final_bytes"] / 1e6))
    else:
        shutil.rmtree(work, ignore_errors=True)
    return 1 if err else 0


def print_census(r: Dict[str, Any]) -> None:
    print("=" * 110)
    print("FIRST-RUN CENSUS, arm %s%s, %.0f s from the launch, slices of %.0f s%s" % (r["arm"], (" (" + r["note"][:120] + ")") if r["note"] else "", r["window_s"],
                                                                                      r["slice_s"], ("   ERROR " + r["error"]) if r["error"] else ""))
    print("%-9s %8s %6s %6s %6s | %9s %9s | %s" % ("slice", "parent s", "gpu", "tab", "other", "recv MB", "sent MB", "files written in the profile (dir: files, MB)"))
    for s in r["slices"]:
        other = sum(v for k, v in s["cpu"].items() if k not in ("parent", "gpu", "tab"))
        dirs = ", ".join("%s: %d, %.2f" % (d, c, b / 1e6) for d, (c, b) in sorted(s["files_by_dir"].items(), key=lambda kv: -kv[1][1])[:5])
        print("+%2d..%2d s %8.2f %6.2f %6.2f %6.2f | %9.2f %9.2f | %s" % (s["from_s"], s["to_s"], s["cpu"]["parent"], s["cpu"]["gpu"], s["cpu"]["tab"], other,
                                                                         s["net_recv_mb"], s["net_sent_mb"], dirs or "-"))
    busy = [s for s in r["slices"] if s["cpu"]["parent"] >= 0.5]
    total_parent = sum(s["cpu"]["parent"] for s in r["slices"])
    print("parent CPU in the window: %.2f s; slices with at least 0.5 s: %s%s" % (
        total_parent, ", ".join("+%d..%d" % (s["from_s"], s["to_s"]) for s in busy) or "none",
        ("  -> burst from +%d s to +%d s (%d s)" % (busy[0]["from_s"], busy[-1]["to_s"], busy[-1]["to_s"] - busy[0]["from_s"])) if busy else ""))
    print("network received in the window: %.1f MB, sent %.1f MB; profile at the end: %.1f MB" % (
        sum(s["net_recv_mb"] for s in r["slices"]), sum(s["net_sent_mb"] for s in r["slices"]), r["profile_final_bytes"] / 1e6))
    print("-" * 110)
    print("PARENT CPU BY THREAD NAME, per slice (the 4 largest of each slice)")
    for s in r["slices"]:
        top = sorted(s["threads"].items(), key=lambda kv: -kv[1])[:4]
        print("  +%2d..%2d s  %s" % (s["from_s"], s["to_s"], ", ".join("%s %.2f" % (n[:28], v) for n, v in top if v >= 0.01) or "(nothing above 0.01 s)"))
    print("-" * 110)
    print("FILES per slice (path, size): everything in gmp-*, security_state, safebrowsing, settings, features, extensions, ... and any file of 100 KB or more")
    for s in r["slices"]:
        if s["notable_files"]:
            print("  +%2d..%2d s" % (s["from_s"], s["to_s"]))
            for rel, size in s["notable_files"][:14]:
                print("      %-90s %12d" % (rel[:90], size))
            if len(s["notable_files"]) > 14:
                print("      ... %d more" % (len(s["notable_files"]) - 14))
    print("-" * 110)
    print("size of each top-level directory of the profile at the end of each slice (MB), the ones above 0.5 MB:")
    for s in r["slices"]:
        print("  +%2d s  %s" % (s["to_s"], ", ".join("%s %.1f" % (d, b / 1e6) for d, b in sorted(s["dir_sizes_at_end"].items(), key=lambda kv: -kv[1]) if b >= 5e5) or "-"))
    hunt.print_processes(r["processes"])
    sys.stdout.flush()


def norm_path(rel: str) -> str:
    rel = re.sub(r"[0-9a-f]{8,}", "H", rel)
    rel = re.sub(r"\d+(\.\d+)+", "V", rel)
    return rel


def census_table(root: Path) -> int:
    arms = [(n, json.loads((root / (n + ".json")).read_text(encoding="utf-8"))) for n in ("retail", "ours") if (root / (n + ".json")).is_file()]
    if not arms:
        print("no census in %s" % root)
        return 1
    print("=" * 110)
    print("FIRST RUN, a fresh profile: %s" % ", ".join("%s%s" % (n, " (ERROR)" if r["error"] else "") for n, r in arms))
    for n, r in arms:
        if n == "retail":
            print("  retail binary: %s" % r["note"][:300])
    print("%-10s" % "slice" + "".join(" | %-9s %-9s %-9s" % (n + " CPU", n + " recv", n + " MB wr") for n, _ in arms))
    for i in range(int(WINDOW_S / SLICE_S)):
        cells = ""
        for _n, r in arms:
            s = r["slices"][i]
            written = sum(b for _c, b in s["files_by_dir"].values()) / 1e6
            cells += " | %-9.2f %-9.2f %-9.2f" % (s["cpu"]["parent"], s["net_recv_mb"], written)
        print("+%2d..%2d s  %s" % (i * SLICE_S, (i + 1) * SLICE_S, cells))
    for n, r in arms:
        busy = [s for s in r["slices"] if s["cpu"]["parent"] >= 0.5]
        print("%-8s parent CPU total %.2f s; busy slices (>= 0.5 s): %s; network received %.1f MB" % (
            n, sum(s["cpu"]["parent"] for s in r["slices"]), ", ".join("+%d" % s["from_s"] for s in busy) or "none", sum(s["net_recv_mb"] for s in r["slices"])))
    print("-" * 110)
    print("THE DOWNLOADS: files of 100 KB or more, and everything in gmp-*, security_state, safebrowsing, settings, features (slice of first appearance, final size)")
    names: Dict[str, Dict[str, Tuple[int, int]]] = {}
    for n, r in arms:
        for rel, i, size in r["downloads"]:
            names.setdefault(norm_path(rel), {})[n] = (i, size)
    print("  %-80s" % "path (version and hash blanked)" + "".join(" | %-18s" % n for n, _ in arms))
    for p in sorted(names, key=lambda p: min(v[0] for v in names[p].values())):
        if max(v[1] for v in names[p].values()) < BIG and not any(WATCHED.match(p) for _ in (0,)):
            continue
        print("  %-80s" % p[:80] + "".join(" | %-18s" % (("+%d s, %.2f MB" % (names[p][n][0] * SLICE_S, names[p][n][1] / 1e6)) if n in names[p] else "-") for n, _ in arms))
    if len(arms) == 2:
        only = {n: [p for p in names if n in names[p] and (arms[0][0] if n == arms[1][0] else arms[1][0]) not in names[p] and names[p][n][1] >= BIG] for n, _ in arms}
        for n, lst in only.items():
            print("  files of 100 KB or more only in %s: %s" % (n, ", ".join(lst[:15]) or "none"))
    print("-" * 110)
    print("PARENT CPU BY THREAD NAME over the whole window (the 12 largest of each arm)")
    for n, r in arms:
        tot: Dict[str, float] = {}
        for s in r["slices"]:
            for k, v in s["threads"].items():
                tot[k] = tot.get(k, 0.0) + v
        print("  %-8s %s" % (n, ", ".join("%s %.2f" % (k[:26], v) for k, v in sorted(tot.items(), key=lambda kv: -kv[1])[:12])))
    return 0


# ------------------------------------------------------------------------------------------------------------------- part 2

def probe(args) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    httpd, base = serve()
    moments = [90] if args.mode == "late" else [5, 20, 40, 90]
    work = Path(tempfile.mkdtemp(prefix="hunt-probe-"))
    family = "retail" if args.arm.startswith("retail") else "ours"
    used = args.arm.endswith("used")
    profile = work / "profile"
    hb.kill_firefox()
    note = ""
    exe = None
    if family == "retail":
        exe, note = retail_exe(args)
    if used:
        if family == "retail":
            if not args.profile or not Path(args.profile).is_dir():
                print("retail_used needs --profile (the profile left by `census --arm retail --keep-profile`)")
                return 1
            shutil.copytree(args.profile, profile, ignore=shutil.ignore_patterns("parent.lock", "lock", ".parentlock"))
        else:
            # ours_used: a first launch of 80 s on this profile_dir, then the measured one
            httpd0, base0 = serve()
            print("ours_used: first launch of %.0f s on the profile" % WINDOW_S)
            t = time.time()
            Idle.t0_ms = int(t * 1000)
            asyncio.run(ours_session(args.binary, base0 + "/idle", profile, t + WINDOW_S))
            hb.kill_firefox()
            httpd0.shutdown()
    Idle.results = []
    deadline_s = max(moments) + MOMENT_GRACE_S + 15
    t0 = time.time()
    Idle.t0_ms = int(t0 * 1000)
    url = "%s/probe?moments=%s" % (base, ",".join(str(m) for m in moments))
    err = ""
    how = ""

    def waiter():
        while time.time() < t0 + deadline_s:
            with Idle.lock:
                if any(r.get("probe") == "page.done" for r in Idle.results):
                    return
            time.sleep(1)

    try:
        if family == "retail":
            def run_retail():
                nonlocal how
                profile.mkdir(parents=True, exist_ok=True)
                proc = subprocess.Popen([str(exe), "-no-remote", "-profile", str(profile), url], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                waiter()
                how = close_like_a_user()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=10)
            run_retail()
        else:
            async def run_ours():
                from invisible_playwright.async_api import InvisiblePlaywright
                async with InvisiblePlaywright(seed=100, binary_path=args.binary, headless=True, locale="en-US", timezone="Europe/Rome",
                                               profile_dir=str(profile)) as browser:
                    page = await browser.new_page()
                    await page.goto(url, wait_until="load", timeout=60000)
                    await asyncio.get_running_loop().run_in_executor(None, waiter)
            asyncio.run(run_ours())
            how = "closed by the context manager"
    except BaseException as exc:   # noqa: BLE001
        err = "%s: %s" % (type(exc).__name__, str(exc)[:400])
    hb.kill_firefox()
    httpd.shutdown()
    with Idle.lock:
        rows = list(Idle.results)
    result = {"arm": args.arm, "mode": args.mode, "moments": moments, "note": note, "error": err, "closed": how, "rows": rows}
    (args.out / ("%s-%s.json" % (args.arm, args.mode))).write_text(json.dumps(result, indent=1), encoding="utf-8")
    print("probe arm %s, mode %s%s: %d result rows%s; browser %s" % (args.arm, args.mode, (" (" + note[:100] + ")") if note else "", len(rows),
                                                                    ("; ERROR " + err) if err else "", how))
    for r in sorted(rows, key=lambda r: (r.get("moment", 0), r.get("probe", ""))):
        if r.get("moment", 0) >= 0:
            print("  +%-3s %-26s started +%6.1f s, took %6d ms, %-9s %s" % (r["moment"], r["probe"], r["started_ms"] / 1000, r["ms"], r["status"],
                                                                         json.dumps(r["value"])[:160]))
    shutil.rmtree(work, ignore_errors=True)
    return 1 if err else 0


def probe_table(root: Path) -> int:
    runs: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for p in sorted(root.glob("*.json")):
        r = json.loads(p.read_text(encoding="utf-8"))
        runs[(r["arm"], r["mode"])] = r
    if not runs:
        print("no probe results in %s" % root)
        return 1
    arms = ["ours_fresh", "ours_used", "retail_fresh", "retail_used"]
    print("=" * 120)
    print("WHAT A PAGE READS OF THE STATE OF A PROFILE. runs: %s" % ", ".join("%s/%s%s" % (a, m, " (ERROR)" if r["error"] else "") for (a, m), r in sorted(runs.items())))
    for (a, m), r in sorted(runs.items()):
        if r["note"]:
            print("  %s binary: %s" % (a, r["note"][:200]))
    ua = {}
    for (a, m), r in runs.items():
        for row in r["rows"]:
            if row.get("probe") == "page.loaded":
                ua[a] = row["value"]
    for a, u in sorted(ua.items()):
        print("  %-13s user agent %s" % (a, u))

    def cell(row: Optional[Dict[str, Any]]) -> str:
        if row is None:
            return "-"
        v = row["value"]
        text = v if isinstance(v, str) else json.dumps(v, sort_keys=True)
        return "%s %dms %s" % (row["status"], row["ms"], text)

    index: Dict[Tuple[str, str, int, str], Dict[str, Any]] = {}
    probes_seen: List[str] = []
    for (a, m), r in runs.items():
        for row in r["rows"]:
            if row.get("moment", -1) >= 0:
                index[(a, m, row["moment"], row["probe"])] = row
                if row["probe"] not in probes_seen:
                    probes_seen.append(row["probe"])
    for name in probes_seen:
        print("=" * 120)
        print("PROBE %s" % name)
        for mode, moments in (("seq", [5, 20, 40, 90]), ("late", [90])):
            for mo in moments:
                cells = [(a, index.get((a, mode, mo, name))) for a in arms if (a, mode) in runs]
                if not cells:
                    continue
                print("  [%s] +%d s" % (mode, mo))
                for a, row in cells:
                    print("      %-13s %s" % (a, cell(row)[:230]))

    def key_of(row: Optional[Dict[str, Any]]) -> str:
        if row is None:
            return "missing"
        return "%s|%s" % (row["status"], json.dumps(row["value"], sort_keys=True))
    print("=" * 120)
    print("CAN A PAGE TELL A PROFILE JUST BORN FROM A USED ONE? Probes (outcome and value, not the time) that differ between a fresh and a used profile at the same moment:")
    for fam in ("ours", "retail"):
        fresh, usedn = fam + "_fresh", fam + "_used"
        for mode, moments in (("seq", [5, 20, 40, 90]), ("late", [90])):
            if (fresh, mode) not in runs or (usedn, mode) not in runs:
                continue
            diffs = []
            for mo in moments:
                for name in probes_seen:
                    a, b = index.get((fresh, mode, mo, name)), index.get((usedn, mode, mo, name))
                    if a is not None and b is not None and key_of(a) != key_of(b):
                        diffs.append("+%d %s [%s -> %s]" % (mo, name, a["status"], b["status"]))
            print("  %-7s %-5s %s" % (fam, mode, ("%d differ: %s" % (len(diffs), "; ".join(diffs))) if diffs else "NONE differ: fresh and used answer the same at every moment"))
    print("-" * 120)
    print("DOES THE ANSWER CHANGE WITH TIME IN ONE PROFILE? (the same arm, the same mode, +5 against +90 s, outcome or value)")
    for a in arms:
        if (a, "seq") in runs:
            ch = [n for n in probes_seen if key_of(index.get((a, "seq", 5, n))) != key_of(index.get((a, "seq", 90, n)))]
            print("  %-13s %s" % (a, ("changes: " + ", ".join(ch)) if ch else "nothing changes between +5 and +90 s"))
    print("-" * 120)
    print("DID ASKING EARLY CHANGE THE ANSWER AT +90 s? (mode seq against mode late, same arm)")
    for a in arms:
        if (a, "seq") in runs and (a, "late") in runs:
            ch = [n for n in probes_seen if key_of(index.get((a, "seq", 90, n))) != key_of(index.get((a, "late", 90, n)))]
            print("  %-13s %s" % (a, ("differs: " + ", ".join(ch)) if ch else "no difference"))
    print("-" * 120)
    print("PROBE TIMES: median ms per probe over the moments, per arm (seq runs)")
    print("  %-26s" % "" + "".join(" %-14s" % a for a in arms))
    for name in probes_seen:
        line = "  %-26s" % name
        for a in arms:
            ms = [index[(a, "seq", mo, name)]["ms"] for mo in (5, 20, 40, 90) if (a, "seq", mo, name) in index]
            line += " %-14s" % (("%d" % statistics.median(ms)) if ms else "-")
        print(line)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, arms):
        p.add_argument("--arm", choices=arms, required=True)
        p.add_argument("--binary", default=os.environ.get("INVPW_BINARY_PATH", ""))
        p.add_argument("--retail-from", default="", help="the --out of fetch-retail")
        p.add_argument("--retail-binary", default="")
        p.add_argument("--retail-version", default="159.0")
        p.add_argument("--out", type=Path, required=True)
    f = sub.add_parser("fetch-retail")
    f.add_argument("--out", type=Path, required=True)
    f.add_argument("--retail-binary", default="")
    f.add_argument("--retail-version", default="159.0")
    c = sub.add_parser("census")
    common(c, ("retail", "ours"))
    c.add_argument("--keep-profile", default="", help="the profile directory to use and keep (the used profile of part 2)")
    ct = sub.add_parser("census-table")
    ct.add_argument("root", type=Path)
    p = sub.add_parser("probe")
    common(p, ("ours_fresh", "ours_used", "retail_fresh", "retail_used"))
    p.add_argument("--mode", choices=("seq", "late"), required=True)
    p.add_argument("--profile", default="", help="retail_used: the profile left by census --keep-profile")
    pt = sub.add_parser("probe-table")
    pt.add_argument("root", type=Path)
    args = ap.parse_args()
    if args.cmd == "fetch-retail":
        return fetch_retail(args)
    if args.cmd == "census-table":
        return census_table(args.root)
    if args.cmd == "probe-table":
        return probe_table(args.root)
    if args.arm.startswith("ours") and not args.binary:
        ap.error("the ours arms need --binary (or INVPW_BINARY_PATH)")
    return census(args) if args.cmd == "census" else probe(args)


if __name__ == "__main__":
    sys.exit(main())
