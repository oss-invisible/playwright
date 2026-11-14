"""HUNT ONLY (branch hunt/cpu-render-path, never merged): the right baseline for the PARENT's CPU, one browser, 20 seconds, with a window.

    python scripts/hunt_cpu_baseline.py --arm retail       [--retail-binary firefox.exe] [--retail-version 159.0] --out DIR
    python scripts/hunt_cpu_baseline.py --arm ours         --binary "$FF"                                          --out DIR
    python scripts/hunt_cpu_baseline.py --arm ours_reused  --binary "$FF"                                          --out DIR
    python scripts/hunt_cpu_baseline.py --compare DIR

The earlier hunt compared ours with stock PLAYWRIGHT's Firefox (a build with automation: Juggler, playwright.cfg, its own prefs), whose parent
might do less work for that very reason. Here the reference is a Firefox nobody drives:

  retail       Firefox retail, launched by hand: subprocess.Popen([firefox.exe, -no-remote, -profile <fresh dir>, <url of the scene>]), no automation
               prefs, no Playwright, the environment untouched. The binary is Mozilla's own installer of the stable release
               (https://archive.mozilla.org/pub/firefox/releases/<version>/win64/en-US/Firefox Setup <version>.exe, checked against the release's
               SHA256SUMS, unpacked with 7-Zip: no install, nothing written outside the work dir). If that cannot be done the arm falls back to
               the firefox-1572 that `playwright install firefox` put on the machine and SAYS SO on its first line: that one is not retail.
  ours         our binary, invisible_playwright, headless=True (on Windows a headed Firefox on a hidden desktop), a fresh temporary profile.
  ours_reused  ours twice on the SAME persistent profile (profile_dir=): launch 1 is a fresh profile (measured too: it answers "does profile_dir
               itself cost anything?"), launch 2 is the same profile used again, and is the one this arm exists for.

The scene is the one of scripts/hunt_cpu_per_browser.py (an RTCPeerConnection gathering, 200 DOM elements, one 2D canvas frame) with one addition:
when it is done the page tells the server (/done/...) and, 300 ms later, navigates to the next copy (/p/<arm>/<n+1>), so a browser nobody drives
keeps the same load for the whole window. The server counts the scenes each arm completed: CPU per scene is the number to compare when the arms
turn the scene at different speeds.

THE WINDOW. t0 = just before the launch; the window is [t0, t0 + 20 s] for every arm (startup included: that is also what retail pays), and the
STEADY window is the last 10 s. The sampler is the one of hunt_cpu_per_browser.py (NtQuerySystemInformation every 250 ms, the command line of each
firefox pid read once at first sight). CPU per PROCESS TYPE comes from it. CPU per THREAD comes from a watcher thread of this script: every 0.5 s
psutil's Process.threads() of every firefox process that is not a tab (the parent, the GPU process, socket, rdd, utility) with the thread's NAME
(GetThreadDescription; Firefox names its threads) and cumulative user+kernel time; a thread's CPU in a window is read like a process's (the last
sample at or before each instant, 0 before it was first seen). Threads are grouped by name with a trailing number removed ("StyleThread#3" ->
"StyleThread"). A thread that lived and died between two polls is not seen: the report prints, per process type, the process CPU of the sampler, the
sum over its threads and the difference.
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import contextlib
import hashlib
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
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hunt_cpu_per_browser as hunt   # noqa: E402

ARMS = ["retail", "ours", "ours_reused"]
WINDOW_S = 20.0
TYPES = hunt.TYPES

LOOP = """<script>
(async () => {
  const m = location.pathname.match(/^\\/p\\/([^/]+)\\/(\\d+)$/);
  if (!m) return;
  let state = "error";
  try { state = await window.__ready; } catch (e) { state = "error:" + e; }
  try { await fetch("/done/" + m[1] + "/" + m[2] + "?" + encodeURIComponent(state), {cache: "no-store"}); } catch (e) {}
  await new Promise(r => setTimeout(r, 300));
  location.replace("/p/" + m[1] + "/" + (parseInt(m[2], 10) + 1));
})();
</script>
"""
PAGE = hunt.PAGE + LOOP


class Scene(BaseHTTPRequestHandler):
    timeout = 10
    done: Dict[str, List[str]] = {}
    lock = threading.Lock()

    def do_GET(self):   # noqa: N802
        if self.path.startswith("/done/"):
            parts = self.path.split("?")[0].split("/")
            state = self.path.split("?", 1)[1] if "?" in self.path else ""
            with Scene.lock:
                Scene.done.setdefault(parts[2] if len(parts) > 2 else "?", []).append(state)
            self.send_response(204)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if not self.path.startswith("/p/"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):   # noqa: D102
        pass


# ---------------------------------------------------------------------------------------------------------------- the threads

def thread_names_windows():
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenThread.restype = wintypes.HANDLE
    k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetThreadDescription.restype = ctypes.c_long
    k32.GetThreadDescription.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.LocalFree.argtypes = [ctypes.c_void_p]

    def name_of(pid: int, tid: int) -> str:
        h = k32.OpenThread(0x0800, False, tid)       # THREAD_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            ptr = ctypes.c_void_p()
            if k32.GetThreadDescription(h, ctypes.byref(ptr)) < 0 or not ptr.value:
                return ""
            try:
                return ctypes.wstring_at(ptr.value)
            finally:
                k32.LocalFree(ptr)
        finally:
            k32.CloseHandle(h)
    return name_of


def thread_names_linux():
    def name_of(pid: int, tid: int) -> str:
        try:
            return Path("/proc/%d/task/%d/comm" % (pid, tid)).read_text().strip()
        except OSError:
            return ""
    return name_of


def normalise(name: str) -> str:
    return re.sub(r"[\s#]*\d+$", "", name) or "(unnamed)"


class Watcher(threading.Thread):
    """Per-thread cumulative CPU of every firefox process that is not a tab, every 0.5 s."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.stop_flag = threading.Event()
        self.kinds: Dict[int, str] = {}
        self.series: Dict[Tuple[int, int], Dict[str, Any]] = {}
        self.name_of = thread_names_windows() if sys.platform == "win32" else thread_names_linux()
        self.error = ""

    def poll(self) -> None:
        import psutil
        now = time.time()
        for p in psutil.process_iter(["pid", "name"]):
            if not (p.info.get("name") or "").lower().startswith("firefox"):
                continue
            pid = p.info["pid"]
            if pid not in self.kinds:
                try:
                    self.kinds[pid] = hunt.classify(p.cmdline())
                except Exception:   # noqa: BLE001
                    continue
            if self.kinds[pid] == "tab":
                continue
            try:
                threads = p.threads()
            except Exception:   # noqa: BLE001
                continue
            for th in threads:
                key = (pid, th.id)
                row = self.series.get(key)
                if row is None:
                    row = self.series[key] = {"name": "", "kind": self.kinds[pid], "t": [], "c": []}
                if not row["name"]:
                    row["name"] = self.name_of(pid, th.id)
                row["t"].append(now)
                row["c"].append(th.user_time + th.system_time)

    def run(self) -> None:
        while not self.stop_flag.is_set():
            try:
                self.poll()
            except Exception as exc:   # noqa: BLE001
                self.error = "%s: %s" % (type(exc).__name__, exc)
            self.stop_flag.wait(0.5)

    def used(self, a: float, b: float) -> List[Dict[str, Any]]:
        out = []
        for (pid, tid), row in self.series.items():
            def at(t, row=row):
                i = bisect.bisect_right(row["t"], t)
                return row["c"][i - 1] if i else 0.0
            out.append({"pid": pid, "tid": tid, "kind": row["kind"], "name": row["name"] or "?", "cpu_s": max(0.0, at(b) - at(a))})
        return out


# ------------------------------------------------------------------------------------------------------------ the retail binary

def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str, dest: Path, timeout: int = 180) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "invisible-hunt"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f, 1 << 20)


def retail_from_mozilla(version: str, work: Path) -> Tuple[Optional[Path], str]:
    base = "https://archive.mozilla.org/pub/firefox/releases/%s" % version
    name = "Firefox Setup %s.exe" % version
    installer = work / "installer.exe"
    try:
        fetch("%s/win64/en-US/%s" % (base, name.replace(" ", "%20")), installer)
    except Exception as exc:   # noqa: BLE001
        return None, "download failed: %s: %s" % (type(exc).__name__, str(exc)[:200])
    digest = sha256_of(installer)
    verdict = "sha256 %s, NOT checked (no SHA256SUMS)" % digest
    try:
        sums = work / "SHA256SUMS"
        fetch(base + "/SHA256SUMS", sums, 60)
        want = [l.split()[0] for l in sums.read_text(encoding="utf-8", errors="replace").splitlines()
                if l.strip().endswith("win64/en-US/" + name)]
        if want:
            if want[0].lower() != digest:
                return None, "SHA256 MISMATCH: installer %s, SHA256SUMS %s" % (digest, want[0])
            verdict = "sha256 %s, equal to the release's SHA256SUMS" % digest
        else:
            verdict = "sha256 %s, its line not found in SHA256SUMS" % digest
    except Exception as exc:   # noqa: BLE001
        verdict += " (SHA256SUMS: %s)" % type(exc).__name__
    seven = shutil.which("7z") or next((p for p in (r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe") if os.path.isfile(p)), None)
    if not seven:
        return None, "7-Zip not found, %s" % verdict
    target = work / "unpacked"
    run = subprocess.run([seven, "x", "-y", "-o" + str(target), str(installer)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300)
    exe = next(iter(sorted(target.rglob("firefox.exe"))), None)
    if exe is None:
        return None, "7z exit %d and no firefox.exe in the installer; %s" % (run.returncode, verdict)
    return exe, "Mozilla's installer %s (%s)" % (name, verdict)


def retail_from_playwright() -> Tuple[Optional[Path], str]:
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "", str(Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"),
             str(Path.home() / ".cache" / "ms-playwright")]
    for r in roots:
        if r and Path(r).is_dir():
            for exe in sorted(Path(r).glob("firefox-*/firefox/firefox.exe")) + sorted(Path(r).glob("firefox-*/firefox/firefox")):
                return exe, "NOT RETAIL: Playwright's %s, launched by hand" % exe.parent.parent.name
    return None, "no Playwright firefox found"


def application_ini(exe: Path) -> str:
    ini = exe.parent / "application.ini"
    if not ini.is_file():
        return "no application.ini"
    keys = {}
    for line in ini.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line and line.split("=")[0] in ("Version", "BuildID", "Name", "SourceStamp", "Vendor"):
            keys[line.split("=")[0]] = line.split("=", 1)[1]
    return ", ".join("%s=%s" % kv for kv in keys.items())


def find_retail(args, work: Path) -> Tuple[Optional[Path], str]:
    if args.retail_binary:
        return Path(args.retail_binary), "given: %s" % args.retail_binary
    exe, why = retail_from_mozilla(args.retail_version, work)
    if exe:
        return exe, why
    print("retail from Mozilla impossible (%s); falling back to Playwright's firefox" % why)
    exe, why2 = retail_from_playwright()
    return exe, "%s (Mozilla: %s)" % (why2, why)


# --------------------------------------------------------------------------------------------------------------------- launches

def kill_tree(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        import psutil
        with contextlib.suppress(Exception):
            parent = psutil.Process(pid)
            for c in parent.children(recursive=True):
                c.kill()
            parent.kill()


def kill_firefox() -> None:
    import psutil
    for p in psutil.process_iter(["name"]):
        if (p.info.get("name") or "").lower().startswith("firefox"):
            with contextlib.suppress(Exception):
                p.kill()
    time.sleep(1)


def launch_retail(exe: Path, base: str, profile: Path, t0: float) -> None:
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([str(exe), "-no-remote", "-profile", str(profile), "%s/p/retail/0" % base],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(max(0.0, t0 + WINDOW_S - time.time()))
    finally:
        kill_tree(proc.pid)
        with contextlib.suppress(Exception):
            proc.wait(timeout=15)


async def ours_session(base: str, binary: str, label: str, profile_dir: Optional[str], t0: float) -> None:
    from invisible_playwright.async_api import InvisiblePlaywright
    kwargs: Dict[str, Any] = {}
    if profile_dir:
        kwargs["profile_dir"] = profile_dir
    async with InvisiblePlaywright(seed=100, binary_path=binary, headless=True, locale="en-US", timezone="Europe/Rome", **kwargs) as browser:
        page = await browser.new_page()
        await page.goto("%s/p/%s/0" % (base, label), wait_until="load", timeout=60000)
        await asyncio.sleep(max(0.0, t0 + WINDOW_S - time.time()))


# ------------------------------------------------------------------------------------------------------------------ measuring

def by_type(sampler: hunt.Sampler, a: float, b: float) -> Tuple[Dict[str, float], int]:
    series = hunt.Series(sampler.samples)
    where, roots = hunt.roots_and_kinds(sampler.info)
    out = {t: 0.0 for t in TYPES}
    for pid, (_root, kind) in where.items():
        out[kind] += series.used(pid, a, b)
    return out, len(roots)


def measure(label: str, arm: str, key: str, launch) -> Dict[str, Any]:
    """One window: sampler + watcher around launch(t0), which returns when the browser is closed. `key` is the scene path the server counts."""
    with Scene.lock:
        Scene.done.pop(key, None)
    sampler = hunt.Sampler()
    sampler.start()
    watcher = Watcher()
    watcher.start()
    t0 = time.time()
    err = ""
    try:
        launch(t0)
    except BaseException as exc:   # noqa: BLE001
        err = "%s: %s" % (type(exc).__name__, str(exc)[:400])
    t_end = time.time()
    time.sleep(0.8)
    watcher.stop_flag.set()
    watcher.join(5)
    sampler.stop()
    kill_firefox()
    mid = t0 + WINDOW_S / 2
    end = t0 + WINDOW_S
    full, roots = by_type(sampler, t0, end)
    steady, _ = by_type(sampler, mid, end)
    series = hunt.Series(sampler.samples)
    client_full = sum(series.used(pid, t0, end) for pid, v in sampler.info.items() if v[0] in ("client", "client-child"))
    threads_full, threads_steady = watcher.used(t0, end), watcher.used(mid, end)
    steady_by = {(r["pid"], r["tid"]): r["cpu_s"] for r in threads_steady}
    for r in threads_full:
        r["steady_s"] = steady_by.get((r["pid"], r["tid"]), 0.0)
    done = list(Scene.done.get(key, []))
    return {
        "label": label, "arm": arm, "error": err, "roots_seen": roots, "window_s": WINDOW_S, "lived_s": t_end - t0,
        "scenes_completed": sum(1 for s in done if s == "complete"), "scenes_other": [s for s in done if s != "complete"][:10],
        "full": full, "steady": steady, "client_cpu_s": client_full, "threads": threads_full, "watcher_error": watcher.error,
        "processes": hunt.process_listing(sampler, t0),
        "process_cpu": {k: {"full": full[k], "steady": steady[k]} for k in TYPES},
    }


def print_result(r: Dict[str, Any], note: str = "") -> None:
    print("=" * 100)
    print("%s: window %.0f s from just before the launch (steady = the last %.0f s), %d browser root(s) seen%s%s"
          % (r["label"], r["window_s"], r["window_s"] / 2, r["roots_seen"], "" if r["roots_seen"] == 1 else "   <-- WARNING (1 expected)",
             ("   [" + note + "]") if note else ""))
    if r["error"]:
        print("ERROR: " + r["error"])
    if r["watcher_error"]:
        print("thread watcher: " + r["watcher_error"])
    n = r["scenes_completed"]
    tot_full, tot_steady = sum(r["full"].values()), sum(r["steady"].values())
    print("scenes completed: %d%s; CPU per scene (whole window, all processes): %s s"
          % (n, (" (not completed: %s)" % r["scenes_other"]) if r["scenes_other"] else "", ("%.3f" % (tot_full / n)) if n else "-"))
    print("  CPU seconds by process type:   %9s %9s" % ("full", "steady"))
    for t in TYPES:
        print("    %-8s %20.2f %9.2f" % (t, r["full"][t], r["steady"][t]))
    print("    %-8s %20.2f %9.2f   (client side, python: %.2f s)" % ("TOTAL", tot_full, tot_steady, r["client_cpu_s"]))
    for kind in ("parent", "gpu"):
        rows = [t for t in r["threads"] if t["kind"] == kind]
        if not rows:
            print("  no %s-process threads were read" % kind)
            continue
        groups: Dict[str, List[float]] = {}
        for t in rows:
            g = groups.setdefault(normalise(t["name"]), [0, 0.0, 0.0])
            g[0] += 1
            g[1] += t["cpu_s"]
            g[2] += t["steady_s"]
        seen_sum = sum(t["cpu_s"] for t in rows)
        print("  threads of the %s process(es), grouped by name, by full-window CPU (the sum over the threads %.2f s, the sampler's process CPU %.2f s, "
              "difference %.2f s = threads born and dead between two polls):" % (kind, seen_sum, r["full"][kind], r["full"][kind] - seen_sum))
        print("    %-34s %7s %9s %9s" % ("thread", "threads", "full s", "steady s"))
        for name, (cnt, f, s) in sorted(groups.items(), key=lambda kv: -kv[1][1])[:18]:
            print("    %-34s %7d %9.2f %9.2f" % (name[:34], cnt, f, s))
    hunt.print_processes(r["processes"])
    sys.stdout.flush()


def run_arm(args) -> int:
    Scene.done = {}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Scene)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    work = Path(tempfile.mkdtemp(prefix="hunt-baseline-"))
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    rc = 0
    try:
        kill_firefox()
        if args.arm == "retail":
            exe, why = find_retail(args, work)
            if exe is None:
                print("retail: no binary (%s)" % why)
                return 1
            note = "%s; %s" % (why, application_ini(exe))
            print("RETAIL BINARY: %s" % note)
            r = measure("retail", "retail", "retail", lambda t0: launch_retail(exe, base, work / "profile-retail", t0))
            print_result(r, note)
            (out / "retail.json").write_text(json.dumps(r, indent=1), encoding="utf-8")
            rc = 1 if r["error"] else 0
        elif args.arm == "ours":
            r = measure("ours", "ours", "ours", lambda t0: asyncio.run(ours_session(base, args.binary, "ours", None, t0)))
            print_result(r, "fresh temporary profile")
            (out / "ours.json").write_text(json.dumps(r, indent=1), encoding="utf-8")
            rc = 1 if r["error"] else 0
        else:
            persistent = str(work / "profile-reused")
            for n in (1, 2):
                label = "ours_reused/%d" % n
                r = measure(label, "ours_reused", "ours_reused%d" % n,
                            lambda t0, n=n: asyncio.run(ours_session(base, args.binary, "ours_reused%d" % n, persistent, t0)))
                r["launch"] = n
                print_result(r, "profile_dir=%s, launch %d of 2 on it (%s)" % ("<persistent>", n, "fresh profile" if n == 1 else "SECOND launch: the measured one"))
                (out / ("ours_reused-%d.json" % n)).write_text(json.dumps(r, indent=1), encoding="utf-8")
                rc = rc or (1 if r["error"] else 0)
                kill_firefox()
    finally:
        httpd.shutdown()
        kill_firefox()
        shutil.rmtree(work, ignore_errors=True)
    return rc


# -------------------------------------------------------------------------------------------------------------------- compare

def compare(root: Path) -> int:
    files = [("retail", "retail.json"), ("ours (fresh)", "ours.json"), ("reused #1 (fresh dir)", "ours_reused-1.json"), ("reused #2 (REUSED)", "ours_reused-2.json")]
    runs = []
    for title, name in files:
        p = root / name
        if p.is_file():
            runs.append((title, json.loads(p.read_text(encoding="utf-8"))))
    if not runs:
        print("no results in %s" % root)
        return 1
    print("=" * 100)
    print("BASELINE FOR THE PARENT: one browser, %.0f s from the launch (full) and the last %.0f s (steady); %s"
          % (WINDOW_S, WINDOW_S / 2, ", ".join(t + (" (ERROR)" if r["error"] else "") for t, r in runs)))
    for window in ("full", "steady"):
        print("-" * 100)
        print("CPU seconds by process type, %s window" % window)
        print("  %-24s" % "" + "".join("%22s" % t for t, _ in runs))
        for kind in TYPES:
            print("  %-24s" % kind + "".join("%22.2f" % r[window][kind] for _, r in runs))
        print("  %-24s" % "TOTAL" + "".join("%22.2f" % sum(r[window].values()) for _, r in runs))
    print("  %-24s" % "scenes completed" + "".join("%22d" % r["scenes_completed"] for _, r in runs))
    print("  %-24s" % "CPU s per scene (full)" + "".join("%22s" % (("%.3f" % (sum(r["full"].values()) / r["scenes_completed"])) if r["scenes_completed"] else "-")
                                                        for _, r in runs))
    for kind in ("parent", "gpu"):
        names: Dict[str, Dict[str, float]] = {}
        for title, r in runs:
            for t in r["threads"]:
                if t["kind"] == kind:
                    names.setdefault(normalise(t["name"]), {}).setdefault(title, 0.0)
                    names[normalise(t["name"])][title] += t["steady_s"]
        order = sorted(names, key=lambda n: -max(names[n].values()))[:20]
        print("-" * 100)
        print("CPU seconds by thread name of the %s process, STEADY window (largest first)" % kind)
        print("  %-30s" % "" + "".join("%22s" % t for t, _ in runs))
        for n in order:
            print("  %-30s" % n[:30] + "".join("%22.2f" % names[n].get(t, 0.0) for t, _ in runs))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=ARMS)
    ap.add_argument("--binary", default=os.environ.get("INVPW_BINARY_PATH", ""))
    ap.add_argument("--retail-binary", default="")
    ap.add_argument("--retail-version", default="159.0")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--compare", type=Path)
    args = ap.parse_args()
    if args.compare:
        return compare(args.compare)
    if not args.arm or not args.out:
        ap.error("--arm and --out, or --compare")
    if args.arm.startswith("ours") and not args.binary:
        ap.error("the arms ours and ours_reused need --binary (or INVPW_BINARY_PATH)")
    return run_arm(args)


if __name__ == "__main__":
    sys.exit(main())
