"""HUNT ONLY (branch hunt/cpu-render-path, never merged; copied from hunt/udp-chain-post at 5cb46f6): under the same load, does OUR Firefox use more CPU per browser than stock Firefox?

    python scripts/hunt_cpu_per_browser.py --arm ours        --binary "$FF" --out DIR/ours-1.json
    python scripts/hunt_cpu_per_browser.py --arm ours_swwr   --binary "$FF" --out DIR/ours_swwr-1.json
    python scripts/hunt_cpu_per_browser.py --arm stock                      --out DIR/stock-1.json
    python scripts/hunt_cpu_per_browser.py --arm stockheaded                --out DIR/stockheaded-1.json
    python scripts/hunt_cpu_per_browser.py --compare DIR

An arm runs 4 browsers at once in ONE client process, each headless with 1 page, and each page loops 10 times over the same scenario,
served by an http server inside the client process:

  1. load a page that builds an RTCPeerConnection with no ICE servers, createDataChannel, createOffer, setLocalDescription and waits for
     iceGatheringState "complete";
  2. draw 200 DOM elements and one 2D canvas frame;
  3. navigate to the next copy of that page (/p/<browser>/<n>).

  ours   invisible_playwright.async_api.InvisiblePlaywright (the in-process client, our binary), headless=True
  stock  stock Playwright's async client (its node driver) and the firefox-1572 it installed, headless=True, with
         firefox_user_prefs {"fission.autostart": true}. Stock's playwright.cfg forces webContentIsolationStrategy 0 by autoconfig, so the
         pref does not change the isolation the cfg picked; it is passed so that both arms are launched with Fission asked for.
  ours_swwr  ours with gfx.webrender.software=true passed ONLY here, through extra_prefs (a hunt-side switch, the product is untouched):
         would software WebRender on the VM cost as little as stock's fallback does?
  stockheaded  the same as stock with headless=False: a headed Firefox, which is what ours is on Windows (headless=True there creates a hidden
         desktop and runs headed Firefox on it), so that the comparison with ours is not headed against --headless.

THE CPU. A tiny separate process (SAMPLER_CODE) reads NtQuerySystemInformation(SystemProcessInformation) every 250 ms (psutil elsewhere): one
call that holds name, ppid and user+kernel time of every process. It keeps, per sample, the cumulative CPU of every firefox process and of
the client side (the client pid and its non-firefox descendants: the python client, stock's node driver).

THE ATTRIBUTION PER BROWSER. The command line is read ONCE per pid, and only for processes whose image name is firefox (the first time the
sampler sees them, whatever CPU they have used, so a short-lived process is not missed for lack of it). The type comes from it:
no `-contentproc` = "parent", otherwise the last token of the command line (tab = content, gpu, socket, rdd, utility, ...). The browser of a
process comes from the table of ppid: walk up through the firefox processes while the parent is a firefox process; the topmost one is the
browser (so a Windows launcher process, should one stay, is the root and the real parent is its child, both counted as "parent"). Four
roots are expected; any other number is printed as a warning.
CPU of a process in a window = its cumulative CPU at the end minus at the start (the last sample at or before each instant; 0 before the
process was first seen), so a process that dies inside the window still counts up to its last sample (at most 250 ms of its end is lost).
The loop window is from the moment all four pages exist to the moment the last page finished its tenth round; the whole-arm window is
from before the first launch to after the last close.
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import contextlib
import importlib
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BROWSERS = int(os.environ.get("HUNT_BROWSERS", "4"))   # only the hunt's GPU-process profile shortens the loop; every other step leaves these alone
ROUNDS = int(os.environ.get("HUNT_ROUNDS", "10"))
ARMS = ["ours", "ours_swwr", "stock", "stockheaded"]
#: prefs an arm passes through extra_prefs: the only place this pref is added, the hunt and never the product
ARM_PREFS = {"ours_swwr": {"gfx.webrender.software": True}}
TYPES = ["parent", "tab", "gpu", "socket", "rdd", "utility", "other"]
ICE_WAIT_MS = 15000
SCENARIO_TIMEOUT_S = 60

PAGE = """<!doctype html><meta charset=utf-8><title>scenario</title>
<body><div id=root></div><canvas id=c width=320 height=200></canvas>
<script>
window.__ready = (async () => {
  const pc = new RTCPeerConnection({iceServers: []});
  pc.createDataChannel("x");
  const done = new Promise(res => {
    const t = setTimeout(() => res("timeout:" + pc.iceGatheringState), %(ice_wait)d);
    pc.addEventListener("icegatheringstatechange", () => {
      if (pc.iceGatheringState === "complete") { clearTimeout(t); res("complete"); }
    });
  });
  await pc.setLocalDescription(await pc.createOffer());
  const state = pc.iceGatheringState === "complete" ? "complete" : await done;
  const root = document.getElementById("root");
  for (let i = 0; i < 200; i++) {
    const d = document.createElement("div");
    d.textContent = "element " + i;
    d.style.cssText = "display:inline-block;width:40px;height:16px;margin:1px;background:hsl(" + (i * 7 %% 360) + ",60%%,60%%)";
    root.appendChild(d);
  }
  const g = document.getElementById("c").getContext("2d");
  for (let i = 0; i < 40; i++) {
    g.fillStyle = "hsl(" + (i * 9) + ",70%%,50%%)";
    g.fillRect(i * 8, (i * 5) %% 160, 60, 30);
  }
  g.font = "16px sans-serif";
  g.fillText("a frame of the scenario", 10, 100);
  g.getImageData(0, 0, 1, 1);
  pc.close();
  return state;
})();
</script>
""" % {"ice_wait": ICE_WAIT_MS}


class Handler(BaseHTTPRequestHandler):
    timeout = 10

    def do_GET(self):   # noqa: N802
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):   # noqa: D102
        pass


# --------------------------------------------------------------------------------------------------------------- the sampler

SAMPLER_CODE = r"""
import json, os, sys, threading, time
halt = threading.Event()
threading.Thread(target=lambda: (sys.stdin.read(), halt.set()), daemon=True).start()
client = int(sys.argv[1])
info, samples = {}, []     # info: pid -> [kind, ppid, name, cmdline-or-None]; samples: [t, {pid: cumulative cpu seconds}]


def table():
    # {pid: (name, cpu seconds user+kernel, ppid)} of every process, in ONE call (psutil's cpu_times opens every process: far slower).
    out = {}
    if sys.platform == "win32":
        import ctypes, struct
        ntdll = ctypes.windll.ntdll
        size = 1 << 20
        while True:
            buf = ctypes.create_string_buffer(size)
            need = ctypes.c_ulong(0)
            status = ntdll.NtQuerySystemInformation(5, buf, size, ctypes.byref(need)) & 0xFFFFFFFF
            if status == 0xC0000004:
                size = max(size * 2, need.value + 65536)
                continue
            if status != 0:
                raise OSError("NtQuerySystemInformation %#x" % status)
            break
        base, raw, off = ctypes.addressof(buf), buf.raw, 0
        while True:
            nxt = struct.unpack_from("<I", raw, off)[0]
            user, kernel = struct.unpack_from("<qq", raw, off + 40)
            length = struct.unpack_from("<H", raw, off + 56)[0]
            pointer = struct.unpack_from("<Q", raw, off + 64)[0]
            pid, ppid = struct.unpack_from("<QQ", raw, off + 80)
            name = raw[pointer - base:pointer - base + length].decode("utf-16-le", "replace") if pointer and length else "?"
            if pid != 0:
                out[pid] = (name, (user + kernel) / 1e7, ppid)
            if nxt == 0:
                break
            off += nxt
        return out
    import psutil
    for p in psutil.process_iter(["pid", "name", "cpu_times", "ppid"]):
        ct = p.info.get("cpu_times")
        if ct is not None:
            out[p.info["pid"]] = (p.info.get("name") or "?", ct.user + ct.system, p.info.get("ppid") or -1)
    return out


def cmdline_of(pid):
    try:
        import psutil
        return psutil.Process(pid).cmdline()
    except Exception as exc:
        return ["?", "%s: %s" % (type(exc).__name__, exc)]


def is_firefox(name):
    return name.lower().startswith("firefox")


def sample():
    now = time.time()
    tab = table()
    tracked = {client}
    grew = True
    while grew:                      # the client side: the client pid and its descendants that are not firefox (a node driver, helpers)
        grew = False
        for pid, (name, _, ppid) in tab.items():
            if pid not in tracked and ppid in tracked and not is_firefox(name):
                tracked.add(pid)
                grew = True
    cpu = {}
    for pid, (name, c, ppid) in tab.items():
        if is_firefox(name):
            if pid not in info:      # the command line: once per pid, only for firefox, at first sight
                info[pid] = ["firefox", ppid, name, cmdline_of(pid)]
            cpu[pid] = c
        elif pid in tracked:
            if pid not in info:
                info[pid] = ["client" if pid == client else "client-child", ppid, name, None]
            cpu[pid] = c
    samples.append([now, cpu])


print("ready", flush=True)
while not halt.is_set():
    try:
        sample()
    except Exception as exc:
        info[-1] = ["error", -1, "%s: %s" % (type(exc).__name__, exc), None]
    halt.wait(0.25)
print(json.dumps({"info": info, "samples": samples}), flush=True)
"""


class Sampler:
    def __init__(self) -> None:
        self.info: dict = {}
        self.samples: list = []
        self.note = ""
        self._proc = None

    def start(self) -> None:
        self._proc = subprocess.Popen([sys.executable, "-I", "-c", SAMPLER_CODE, str(os.getpid())], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, text=True)
        ready = self._proc.stdout.readline()
        if "ready" not in ready:
            raise RuntimeError("the sampler said %r instead of ready" % ready)

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
            data = json.loads(proc.stdout.readline())
            self.info = {int(k): v for k, v in data["info"].items()}
            self.samples = data["samples"]
        finally:
            try:
                proc.wait(timeout=10)
            except Exception:   # noqa: BLE001
                proc.kill()


# ------------------------------------------------------------------------------------------------------------------ the attribution

def classify(cmd: list) -> str:
    """parent = no -contentproc; otherwise the last token of the command line (tab, gpu, socket, rdd, utility, ...)."""
    if "-contentproc" not in cmd:
        return "parent"
    last = str(cmd[-1]).strip('"').lower()
    return last if last in TYPES else "other"


def roots_and_kinds(info: dict) -> tuple:
    """{pid: (root pid, type)} for every firefox process, and the list of roots."""
    firefox = {p: v for p, v in info.items() if v[0] == "firefox"}
    out, roots = {}, set()
    for pid, v in firefox.items():
        top, seen = pid, {pid}
        while firefox.get(top) is not None and firefox[top][1] in firefox and firefox[top][1] not in seen:
            top = firefox[top][1]
            seen.add(top)
        out[pid] = (top, classify(v[3]))
        roots.add(top)
    return out, sorted(roots)


class Series:
    """cumulative CPU of every tracked pid over time: cpu_at(pid, t) = the last sample at or before t, 0 before the pid's first sample."""

    def __init__(self, samples: list) -> None:
        self.t: dict = {}
        self.c: dict = {}
        for ts, cpu in samples:
            for pid, c in cpu.items():
                pid = int(pid)
                self.t.setdefault(pid, []).append(ts)
                self.c.setdefault(pid, []).append(c)

    def at(self, pid: int, t: float) -> float:
        i = bisect.bisect_right(self.t.get(pid, []), t)
        return self.c[pid][i - 1] if i else 0.0

    def used(self, pid: int, a: float, b: float) -> float:
        return max(0.0, self.at(pid, b) - self.at(pid, a))


def per_browser(sampler: Sampler, a: float, b: float) -> tuple:
    """([ {type: cpu s} per browser ], roots, client-side cpu s) over the window [a, b]."""
    series = Series(sampler.samples)
    where, roots = roots_and_kinds(sampler.info)
    rows = {r: {t: 0.0 for t in TYPES} for r in roots}
    for pid, (root, kind) in where.items():
        rows[root][kind] += series.used(pid, a, b)
    client = sum(series.used(pid, a, b) for pid, v in sampler.info.items() if v[0] in ("client", "client-child"))
    return [rows[r] for r in roots], roots, client


def process_listing(sampler: "Sampler", t0: float) -> list:
    """Every firefox process the sampler saw: pid, parent, browser root, type, first/last sample (s from t0), lifetime, CPU used in all, command line."""
    series = Series(sampler.samples)
    where, _ = roots_and_kinds(sampler.info)
    out = []
    for pid, (root, kind) in sorted(where.items()):
        ts, cs = series.t.get(pid, []), series.c.get(pid, [])
        if not ts:
            continue
        cmd = sampler.info[pid][3] or []
        out.append({"pid": pid, "ppid": sampler.info[pid][1], "root": root, "kind": kind, "first_s": round(ts[0] - t0, 2),
                    "last_s": round(ts[-1] - t0, 2), "life_s": round(ts[-1] - ts[0], 2), "cpu_s": round(cs[-1], 3),
                    "cmd": " ".join(str(x) for x in cmd)})
    return out


SHORT_S = 5.0


def print_processes(listing: list) -> None:
    """What the short-lived processes are. `default` = a firefox process with no recognised -contentproc type (parent or other here)."""
    import re
    print("  processes seen by the sampler (every 250 ms: one that lives less is not seen): %d in all" % len(listing))
    kinds: dict = {}
    for r in listing:
        kinds.setdefault(r["kind"], []).append(r)
    for k, rs in sorted(kinds.items()):
        shorts = [r for r in rs if r["life_s"] < SHORT_S]
        print("    %-8s %3d process(es), %3d shorter than %.0f s, lifetime median %.1f s, CPU of all of them %.2f s"
              % (k, len(rs), len(shorts), SHORT_S, statistics.median([r["life_s"] for r in rs]), sum(r["cpu_s"] for r in rs)))
    defaults = [r for r in listing if r["kind"] in ("parent", "other")]
    print("  every firefox process WITHOUT a recognised -contentproc type (`default` in a Gecko profile), %d:" % len(defaults))
    for r in defaults[:80]:
        print("    pid %-6s ppid %-6s root %-6s first %6.1f s life %6.1f s cpu %6.2f s  %s"
              % (r["pid"], r["ppid"], r["root"], r["first_s"], r["life_s"], r["cpu_s"], r["cmd"][:500]))
    groups: dict = {}
    for r in listing:
        if r["life_s"] < SHORT_S and r["kind"] not in ("parent", "other"):
            groups.setdefault((r["kind"], re.sub(r"\d+", "N", r["cmd"])), []).append(r)
    print("  the other short-lived processes (< %.0f s), grouped by type and command line with the numbers blanked:" % SHORT_S)
    for (kind, _norm), rs in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:20]:
        print("    %3d x %-8s median life %.1f s, CPU %.2f s: %s" % (len(rs), kind, statistics.median([r["life_s"] for r in rs]),
                                                                    sum(r["cpu_s"] for r in rs), rs[0]["cmd"][:400]))
    sys.stdout.flush()


def summarise(rows: list) -> dict:
    med = {t: (statistics.median([r[t] for r in rows]) if rows else 0.0) for t in TYPES}
    totals = [sum(r.values()) for r in rows]
    return {"median_by_type": med, "median_total": statistics.median(totals) if totals else 0.0, "total_all": sum(totals)}


# ----------------------------------------------------------------------------------------------------------------------- the arms

async def loop_one(page, base: str, index: int, outcome: dict) -> None:
    for n in range(ROUNDS):
        t = time.perf_counter()
        try:
            await page.goto("%s/p/%d/%d" % (base, index, n), wait_until="load", timeout=SCENARIO_TIMEOUT_S * 1000)
            state = await asyncio.wait_for(page.evaluate("window.__ready"), SCENARIO_TIMEOUT_S)
        except Exception as exc:   # noqa: BLE001
            outcome["failed"].append("browser %d round %d: %s: %s" % (index, n, type(exc).__name__, str(exc)[:200]))
            continue
        if state == "complete":
            outcome["completed"] += 1
        else:
            outcome["incomplete"].append("browser %d round %d: ice %s" % (index, n, state))
        outcome["ms"].append((time.perf_counter() - t) * 1000)


async def run_scenarios(pages: list, base: str) -> tuple:
    outcome = {"completed": 0, "incomplete": [], "failed": [], "ms": []}
    t0 = time.time()
    await asyncio.gather(*(loop_one(p, base, i, outcome) for i, p in enumerate(pages)))
    return t0, time.time(), outcome


async def arm_ours(base: str, binary: str, extra_prefs: dict | None = None):
    from invisible_playwright.async_api import InvisiblePlaywright
    async with contextlib.AsyncExitStack() as stack:
        browsers = await asyncio.gather(*(stack.enter_async_context(
            InvisiblePlaywright(seed=100 + i, binary_path=binary, headless=True, locale="en-US", timezone="Europe/Rome",
                                extra_prefs=extra_prefs))
            for i in range(BROWSERS)))
        pages = await asyncio.gather(*(b.new_page() for b in browsers))
        return await run_scenarios(pages, base)


async def arm_stock(base: str, binary: str, headless: bool = True):
    async_playwright = importlib.import_module("playwright.async_api").async_playwright   # by name: the fork's guard forbids a static import
    pw = await async_playwright().start()
    browsers = []
    try:
        browsers = await asyncio.gather(*(pw.firefox.launch(headless=headless, firefox_user_prefs={"fission.autostart": True})
                                          for _ in range(BROWSERS)))
        pages = await asyncio.gather(*(b.new_page() for b in browsers))
        return await run_scenarios(pages, base)
    finally:
        for b in browsers:
            with contextlib.suppress(Exception):
                await b.close()
        with contextlib.suppress(Exception):
            await pw.stop()


def run_arm(arm: str, binary: str, rnd: int) -> dict:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    sampler = Sampler()
    sampler.start()
    whole_a = time.time()
    err = ""
    loop_a = loop_b = whole_a
    outcome = {"completed": 0, "incomplete": [], "failed": [], "ms": []}
    try:
        if arm in ("ours", "ours_swwr"):
            coro = arm_ours(base, binary, ARM_PREFS.get(arm))
        else:
            coro = arm_stock(base, binary, headless=(arm == "stock"))
        loop_a, loop_b, outcome = asyncio.run(coro)
    except BaseException as exc:   # noqa: BLE001
        err = "%s: %s" % (type(exc).__name__, exc)
    time.sleep(0.6)           # two more samples after the last close
    whole_b = time.time()
    sampler.stop()
    httpd.shutdown()
    rows, roots, client = per_browser(sampler, loop_a, loop_b)
    whole_rows, _, whole_client = per_browser(sampler, whole_a, whole_b)
    loop = summarise(rows)
    result = {
        "arm": arm, "round": rnd, "error": err, "browsers_seen": len(roots), "wall_s": loop_b - loop_a,
        "completed": outcome["completed"], "incomplete": outcome["incomplete"], "failed": outcome["failed"],
        "median_scenario_ms": statistics.median(outcome["ms"]) if outcome["ms"] else None,
        "loop": loop, "client_cpu_s": client,
        "cpu_per_scenario_s": (loop["total_all"] / outcome["completed"]) if outcome["completed"] else None,
        "whole": summarise(whole_rows), "whole_client_cpu_s": whole_client, "whole_wall_s": whole_b - whole_a,
        "processes": process_listing(sampler, whole_a),
    }
    return result


def print_arm(r: dict) -> None:
    print("=" * 100)
    print("arm %s, round %s: %s completed of %d scenarios, wall time of the loop %.1f s, %d browser(s) seen (4 expected)%s"
          % (r["arm"], r["round"], r["completed"], BROWSERS * ROUNDS, r["wall_s"], r["browsers_seen"],
             "" if r["browsers_seen"] == BROWSERS else "   <-- WARNING"))
    if r["error"]:
        print("ERROR: " + r["error"])
    for line in (r["incomplete"] + r["failed"])[:10]:
        print("  not completed: " + line)
    print("  CPU seconds per browser, median of the browsers, LOOP window:")
    for t in TYPES:
        print("    %-8s %8.2f" % (t, r["loop"]["median_by_type"][t]))
    print("    %-8s %8.2f   (median per browser; all browsers together %.2f)" % ("TOTAL", r["loop"]["median_total"], r["loop"]["total_all"]))
    print("  client side (python client, node driver): %.2f s" % r["client_cpu_s"])
    if r["cpu_per_scenario_s"] is not None:
        print("  CPU per completed scenario (all browsers): %.3f s; median scenario time %.0f ms"
              % (r["cpu_per_scenario_s"], r["median_scenario_ms"] or 0))
    print("  whole arm (launch to close, %.1f s): median total per browser %.2f s, all %.2f s, client side %.2f s"
          % (r["whole_wall_s"], r["whole"]["median_total"], r["whole"]["total_all"], r["whole_client_cpu_s"]))
    print_processes(r.get("processes") or [])
    sys.stdout.flush()


# ----------------------------------------------------------------------------------------------------------------------- compare

def compare(directory: Path) -> int:
    results = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))]
    arms = {a: [r for r in results if r["arm"] == a and not r["error"]] for a in ARMS}
    for a, rs in arms.items():
        print("%s: %d good run(s) (%s)" % (a, len(rs), ", ".join("round %s" % r["round"] for r in rs)))
    if not arms["ours"] or not (arms["stock"] or arms["stockheaded"]):
        print("no comparison: ours or every stock arm has no good run")
        return 1

    def mean(a, f):
        values = [f(r) for r in arms[a] if f(r) is not None]
        return sum(values) / len(values) if values else float("nan")

    rows = [("%s CPU s per browser (median of 4)" % t, lambda r, t=t: r["loop"]["median_by_type"][t]) for t in TYPES]
    rows += [
        ("TOTAL CPU s per browser (median of 4)", lambda r: r["loop"]["median_total"]),
        ("total CPU s, all browsers", lambda r: r["loop"]["total_all"]),
        ("client side CPU s", lambda r: r["client_cpu_s"]),
        ("wall time of the loop, s", lambda r: r["wall_s"]),
        ("completed scenarios (of %d)" % (BROWSERS * ROUNDS), lambda r: r["completed"]),
        ("CPU s per completed scenario", lambda r: r["cpu_per_scenario_s"]),
        ("whole arm, total CPU s per browser", lambda r: r["whole"]["median_total"]),
    ]
    def ratio(a, b):
        return ("%.2f" % (a / b)) if b and b == b else "-"

    print()
    print("%-52s %9s %9s %9s %11s %11s %13s %12s" % ("loop window, mean over the rounds", "ours", "ours_swwr", "stock", "stockheaded",
                                                      "ours/stock", "ours/stockhd", "swwr/stockhd"))
    for label, f in rows:
        o, w, s, h = mean("ours", f), mean("ours_swwr", f), mean("stock", f), mean("stockheaded", f)
        print("%-52s %9.3f %9.3f %9.3f %11.3f %11s %13s %12s" % (label, o, w, s, h, ratio(o, s), ratio(o, h), ratio(w, h)))
    print()
    for a in ARMS:
        print("%s per round: " % a + "; ".join("r%s total/browser %.2f s, wall %.1f s" % (r["round"], r["loop"]["median_total"], r["wall_s"])
                                               for r in arms[a]))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=ARMS)
    ap.add_argument("--binary", default=os.environ.get("INVPW_BINARY_PATH", ""))
    ap.add_argument("--round", type=int, default=1)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--compare", type=Path)
    args = ap.parse_args()
    if args.compare:
        return compare(args.compare)
    if not args.arm:
        ap.error("--arm or --compare")
    if args.arm in ("ours", "ours_swwr") and not args.binary:
        ap.error("the arms ours and ours_swwr need --binary (or INVPW_BINARY_PATH)")
    result = run_arm(args.arm, args.binary, args.round)
    print_arm(result)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    return 1 if result["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
