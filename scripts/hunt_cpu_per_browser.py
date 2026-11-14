"""HUNT ONLY (branch hunt/udp-chain-post, never merged): why does OUR Firefox use more CPU than stock Firefox on Windows?

    python scripts/hunt_cpu_per_browser.py --arm ours --browsers 1 --mode scenes --binary "$FF" --out DIR/ours-n1-scenes-r1.json
    python scripts/hunt_cpu_per_browser.py --arm stockwin --browsers 4 --mode loop --out DIR/stockwin-n4-loop-r1.json
    python scripts/hunt_cpu_per_browser.py --compare DIR

ARMS (what is launched, how):
  ours      invisible_playwright, headless=True: a real window on a hidden Win32 desktop made by invisible_core (CreateDesktop)
  ourswin   invisible_playwright, headless=False: a real window on the runner's interactive desktop
  ourshl    invisible_playwright, headless=True with INVPW_TRUE_HEADLESS=1: our binary in real --headless
  stock     stock Playwright's firefox, headless=True (real --headless)
  stockwin  stock Playwright's firefox, headless=False: a real window on the interactive desktop
  stockhid  stock Playwright's firefox, headless=False, the WHOLE client (python + node driver + firefox) created on a hidden desktop made
            here the way invisible_core makes it (CreateDesktopW, process created with STARTUPINFO.lpDesktop; every child inherits it), with
            the same two sandbox prefs invisible_core adds for that desktop (security.sandbox.gpu.level 0, content.level 4)

MODES:
  loop    N browsers at once (--browsers), 1 page each, each page loops 10 times over a local scenario: an RTCPeerConnection with no ICE
          servers up to iceGatheringState complete, 200 DOM elements and a 2D canvas frame, then the next copy of the page.
  scenes  N browsers (use 1), in this order, each in its own window: IDLE (about:blank, 30 s), STATIC (text and 200 divs, no animation,
          15 s), RAF (a 2D canvas animated by requestAnimationFrame, 15 s; the page counts its frames); then about:support is read.

THE CPU. A tiny separate process (SAMPLER_CODE) reads NtQuerySystemInformation(SystemProcessInformation) every 250 ms: one call that holds,
for EVERY process of the machine, the name, ppid and the user+kernel time the kernel accounted (seconds, not percentages), and for the
firefox processes also every thread (tid, user+kernel time, context switches). Next to it GetSystemTimes (idle, kernel, user of the whole
machine). The client pid and its non-firefox descendants (the python client, stock's node driver, the sampler) are the "client side"; every
other process of the runner is listed under "others" with the CPU it used inside the window.
The thread names are read from outside with OpenThread + GetThreadDescription, at the begin and the end of every window ("snap"), and a snap
also takes a fresh sample, so the process CPU and the thread CPU of a window are the same instants.

THE ATTRIBUTION PER BROWSER. The command line of a firefox process is read once per pid (psutil), at first sight. The type: no
`-contentproc` = "parent", otherwise the last token of the command line (tab = content, gpu, socket, rdd, utility, ...). The browser of a
process comes from walking ppid through the firefox processes; the topmost is the browser (a Windows launcher process that stays would be
the root and its child the real parent, both "parent"). A root count other than N is printed as a warning.
CPU of a process in a window = its cumulative CPU at the end minus at the start, so a process that dies inside the window counts up to its
last sample.
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import collections
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

ROUNDS = 10
TYPES = ["parent", "tab", "gpu", "socket", "rdd", "utility", "other"]
ICE_WAIT_MS = 15000
SCENARIO_TIMEOUT_S = 60
OURS_ARMS = ("ours", "ourswin", "ourshl")
STOCK_ARMS = ("stock", "stockwin", "stockhid")
ARMS = OURS_ARMS + STOCK_ARMS
SETTLE_S = 8
IDLE_S = 30
STATIC_S = 15
RAF_S = 15
LATE_S = 30

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

# STATIC: text and 200 divs, nothing moves, no script after load. RAF: a 2D canvas redrawn on every requestAnimationFrame.
PAGE_STATIC = """<!doctype html><meta charset=utf-8><title>static</title>
<body><h1>A still page</h1><p>Some text that never changes. Nothing on this page is animated and no script runs after load.</p>
<div id=root></div>
<script>
const root = document.getElementById("root");
for (let i = 0; i < 200; i++) {
  const d = document.createElement("div");
  d.textContent = "element " + i;
  d.style.cssText = "display:inline-block;width:40px;height:16px;margin:1px;background:hsl(" + (i * 7 %% 360) + ",60%%,60%%)";
  root.appendChild(d);
}
</script>
""" % {}

PAGE_RAF = """<!doctype html><meta charset=utf-8><title>raf</title>
<body style="margin:0"><canvas id=c width=640 height=360></canvas>
<script>
const g = document.getElementById("c").getContext("2d");
window.__frames = 0;
function frame(t) {
  window.__frames++;
  g.fillStyle = "#222"; g.fillRect(0, 0, 640, 360);
  for (let i = 0; i < 20; i++) {
    g.fillStyle = "hsl(" + ((i * 18 + t / 10) %% 360) + ",70%%,55%%)";
    g.fillRect((i * 30 + t / 8) %% 600, (i * 17 + t / 12) %% 320, 40, 40);
  }
  g.fillStyle = "#fff"; g.font = "20px sans-serif"; g.fillText("frame " + window.__frames, 10, 30);
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
window.__stats = () => ({frames: window.__frames, now: performance.now(), dpr: window.devicePixelRatio});
</script>
""" % {}


class Handler(BaseHTTPRequestHandler):
    timeout = 10

    def do_GET(self):   # noqa: N802
        page = PAGE_STATIC if self.path.startswith("/static") else PAGE_RAF if self.path.startswith("/raf") else PAGE
        body = page.encode("utf-8")
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
lock = threading.Lock()
client = int(sys.argv[1])
info, samples, snaps = {}, [], []   # info: pid -> [kind, ppid, name, cmdline-or-None]; samples: [t, {pid: cpu s, only when changed}, [idle, kernel, user]]
last = {}                            # pid -> the last cumulative cpu written for it
names = {}                           # (pid, tid) -> thread name, once known
getdesc = None

if sys.platform == "win32":
    import ctypes, struct
    from ctypes import wintypes
    ntdll = ctypes.windll.ntdll
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenThread.restype = wintypes.HANDLE
    k32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.LocalFree.argtypes = (ctypes.c_void_p,)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    psapi.EnumProcessModulesEx.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.DWORD)
    psapi.GetModuleInformation.argtypes = (wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD)
    psapi.GetModuleBaseNameW.argtypes = (wintypes.HANDLE, ctypes.c_void_p, ctypes.c_wchar_p, wintypes.DWORD)
    ntdll.NtQueryInformationThread.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p)

    class MODULEINFO(ctypes.Structure):
        _fields_ = [("base", ctypes.c_void_p), ("size", wintypes.DWORD), ("entry", ctypes.c_void_p)]

    getdesc = getattr(k32, "GetThreadDescription", None)
    if getdesc is None:
        getdesc = getattr(ctypes.WinDLL("kernelbase"), "GetThreadDescription", None)
    if getdesc is not None:
        getdesc.argtypes = (wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p))
        getdesc.restype = ctypes.c_long
    k32.GetSystemTimes.argtypes = (ctypes.POINTER(ctypes.c_ulonglong),) * 3


def module_list(pid):
    # [(base, size, basename)] of the modules of a process, or [] when it cannot be opened
    out = []
    h = k32.OpenProcess(0x0410, False, pid)      # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
    if not h:
        return out
    try:
        arr = (ctypes.c_void_p * 4096)()
        need = wintypes.DWORD()
        if psapi.EnumProcessModulesEx(h, arr, ctypes.sizeof(arr), ctypes.byref(need), 3):
            for i in range(min(4096, need.value // ctypes.sizeof(ctypes.c_void_p))):
                info_ = MODULEINFO()
                name = ctypes.create_unicode_buffer(260)
                if psapi.GetModuleInformation(h, arr[i], ctypes.byref(info_), ctypes.sizeof(info_)) and psapi.GetModuleBaseNameW(h, arr[i], name, 260):
                    out.append((info_.base or 0, info_.size, name.value))
    finally:
        k32.CloseHandle(h)
    return out


def thread_name(pid, tid, mods):
    # the thread's description, or, when it has none, "?" + the module of its Win32 start address (so an unnamed thread says whose it is)
    key = (pid, tid)
    if names.get(key):
        return names[key]
    name, start = "", 0
    h = k32.OpenThread(0x0840, False, tid)      # THREAD_QUERY_INFORMATION | THREAD_QUERY_LIMITED_INFORMATION
    if not h:
        h = k32.OpenThread(0x0800, False, tid)
    if h:
        try:
            if getdesc is not None:
                buf = ctypes.c_void_p()
                if getdesc(h, ctypes.byref(buf)) >= 0 and buf.value:
                    name = ctypes.wstring_at(buf.value)
                    k32.LocalFree(buf)
            if not name:
                addr = ctypes.c_void_p()
                if ntdll.NtQueryInformationThread(h, 9, ctypes.byref(addr), 8, None) == 0 and addr.value:   # ThreadQuerySetWin32StartAddress
                    start = addr.value
        finally:
            k32.CloseHandle(h)
    if name:
        names[key] = name
        return name
    if start:
        if pid not in mods:
            mods[pid] = module_list(pid)
        for base, size, mname in mods[pid]:
            if base <= start < base + size:
                return "?" + mname
        return "?unmapped"
    return "?"


def machine_times():
    idle, kernel, user = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
    k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
    return [idle.value / 1e7, kernel.value / 1e7, user.value / 1e7]     # kernel includes idle


def table(threads=False):
    # {pid: (name, cpu seconds user+kernel, ppid, [threads])} of every process, in ONE call (psutil's cpu_times opens every process).
    # SYSTEM_PROCESS_INFORMATION (x64): UserTime@40 KernelTime@48 ImageName@56 pid@80 ppid@88, the threads follow at @256, 80 bytes each:
    # Kernel@0 User@8 ClientId{pid@40, tid@48} ContextSwitches@64 ThreadState@68.
    out = {}
    if sys.platform == "win32":
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
            nthreads = struct.unpack_from("<I", raw, off + 4)[0]
            user, kernel = struct.unpack_from("<qq", raw, off + 40)
            length = struct.unpack_from("<H", raw, off + 56)[0]
            pointer = struct.unpack_from("<Q", raw, off + 64)[0]
            pid, ppid = struct.unpack_from("<QQ", raw, off + 80)
            name = raw[pointer - base:pointer - base + length].decode("utf-16-le", "replace") if pointer and length else "?"
            th = []
            if threads and name.lower().startswith("firefox"):
                for i in range(nthreads):
                    o = off + 256 + i * 80
                    tk, tu = struct.unpack_from("<qq", raw, o)
                    tid = struct.unpack_from("<Q", raw, o + 48)[0]
                    ctx, state = struct.unpack_from("<II", raw, o + 64)
                    th.append((tid, (tk + tu) / 1e7, ctx, state))
            if pid != 0:
                out[pid] = (name, (user + kernel) / 1e7, ppid, th)
            if nxt == 0:
                break
            off += nxt
        return out
    import psutil
    for p in psutil.process_iter(["pid", "name", "cpu_times", "ppid"]):
        ct = p.info.get("cpu_times")
        if ct is not None:
            out[p.info["pid"]] = (p.info.get("name") or "?", ct.user + ct.system, p.info.get("ppid") or -1, [])
    return out


def cmdline_of(pid):
    try:
        import psutil
        return psutil.Process(pid).cmdline()
    except Exception as exc:
        return ["?", "%s: %s" % (type(exc).__name__, exc)]


def is_firefox(name):
    return name.lower().startswith("firefox")


def sample(threads=False):
    with lock:
        now = time.time()
        tab = table(threads)
        tracked = {client}
        grew = True
        while grew:                  # the client side: the client pid and its descendants that are not firefox (node driver, sampler)
            grew = False
            for pid, (name, _, ppid, _) in tab.items():
                if pid not in tracked and ppid in tracked and not is_firefox(name):
                    tracked.add(pid)
                    grew = True
        cpu = {}
        for pid, (name, c, ppid, _) in tab.items():
            if pid not in info:
                if is_firefox(name):   # the command line: once per pid, only for firefox, at first sight
                    info[pid] = ["firefox", ppid, name, cmdline_of(pid)]
                elif pid in tracked:
                    info[pid] = ["client" if pid == client else "client-child", ppid, name, None]
                else:
                    info[pid] = ["other", ppid, name, None]
            if last.get(pid) != c:
                cpu[pid] = c
                last[pid] = c
        samples.append([now, cpu, machine_times() if sys.platform == "win32" else [0, 0, 0]])
        return now, tab


def snap(label):
    now, tab = sample(True)
    procs, mods = {}, {}
    for pid, (name, _, _, th) in tab.items():
        if th:
            procs[pid] = [[tid, c, ctx, state, thread_name(pid, tid, mods)] for tid, c, ctx, state in th]
    snaps.append({"label": label, "t": now, "procs": procs})


def reader():
    for line in sys.stdin:
        if line.startswith("snap "):
            try:
                snap(line[5:].strip())
            except Exception as exc:
                info[-2] = ["error", -1, "snap: %s: %s" % (type(exc).__name__, exc), None]
            print("ok", flush=True)
    halt.set()


threading.Thread(target=reader, daemon=True).start()
print("ready", flush=True)
while not halt.is_set():
    try:
        sample()
    except Exception as exc:
        info[-1] = ["error", -1, "%s: %s" % (type(exc).__name__, exc), None]
    halt.wait(0.25)
print(json.dumps({"info": info, "samples": samples, "snaps": snaps}), flush=True)
"""


class Sampler:
    def __init__(self) -> None:
        self.info: dict = {}
        self.samples: list = []
        self.snaps: dict = {}
        self._proc = None

    def start(self) -> None:
        self._proc = subprocess.Popen([sys.executable, "-I", "-c", SAMPLER_CODE, str(os.getpid())], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, text=True)
        ready = self._proc.stdout.readline()
        if "ready" not in ready:
            raise RuntimeError("the sampler said %r instead of ready" % ready)

    def snap(self, label: str) -> None:
        """A fresh sample plus every thread of every firefox process, named. Blocks until the sampler has taken it."""
        self._proc.stdin.write("snap %s\n" % label)
        self._proc.stdin.flush()
        self._proc.stdout.readline()

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
            data = json.loads(proc.stdout.readline())
            self.info = {int(k): v for k, v in data["info"].items()}
            self.samples = data["samples"]
            self.snaps = {s["label"]: s for s in data["snaps"]}
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
    """cumulative CPU of every pid over time (written only when it changed): at(pid, t) = the value at the last sample at or before t,
    0 before the pid's first sample. The machine times (idle, kernel, user) are kept per sample."""

    def __init__(self, samples: list) -> None:
        self.t: dict = {}
        self.c: dict = {}
        self.mt = [s[0] for s in samples]
        self.m = [s[2] for s in samples]
        for ts, cpu, _ in samples:
            for pid, c in cpu.items():
                pid = int(pid)
                self.t.setdefault(pid, []).append(ts)
                self.c.setdefault(pid, []).append(c)

    def at(self, pid: int, t: float) -> float:
        i = bisect.bisect_right(self.t.get(pid, []), t)
        return self.c[pid][i - 1] if i else 0.0

    def used(self, pid: int, a: float, b: float) -> float:
        return max(0.0, self.at(pid, b) - self.at(pid, a))

    def machine(self, a: float, b: float) -> float:
        """CPU seconds the whole machine was busy in [a, b] (kernel + user - idle, summed over the vCPUs)."""
        def at(t):
            i = bisect.bisect_right(self.mt, t)
            return self.m[i - 1] if i else self.m[0] if self.m else [0, 0, 0]
        x, y = at(a), at(b)
        return max(0.0, (y[1] - x[1]) + (y[2] - x[2]) - (y[0] - x[0]))


def window_report(sampler: Sampler, a: float, b: float) -> dict:
    """Everything the CPU of the window [a, b] says: per browser and type, the client side, the machine and every other process."""
    series = Series(sampler.samples)
    where, roots = roots_and_kinds(sampler.info)
    rows = {r: {t: 0.0 for t in TYPES} for r in roots}
    nprocs = collections.Counter()
    for pid, (root, kind) in where.items():
        u = series.used(pid, a, b)
        rows[root][kind] += u
        if u > 0:
            nprocs[kind] += 1
    rows_l = [rows[r] for r in roots]
    med = {t: (statistics.median([r[t] for r in rows_l]) if rows_l else 0.0) for t in TYPES}
    sums = {t: sum(r[t] for r in rows_l) for t in TYPES}
    totals = [sum(r.values()) for r in rows_l]
    client, client_detail, others, others_n = 0.0, collections.Counter(), collections.Counter(), collections.Counter()
    for pid, v in sampler.info.items():
        if v[0] in ("client", "client-child"):
            u = series.used(pid, a, b)
            client += u
            client_detail[v[2]] += u
        elif v[0] == "other":
            u = series.used(pid, a, b)
            if u > 0:
                others[v[2]] += u
                others_n[v[2]] += 1
    firefox_total = sum(totals)
    others_total = sum(others.values())
    busy = series.machine(a, b)
    return {
        "wall_s": b - a, "browsers_seen": len(roots), "median_by_type": med, "sum_by_type": sums,
        "median_total": statistics.median(totals) if totals else 0.0, "total_all": firefox_total, "nprocs": dict(nprocs),
        "client_s": client, "client_detail": dict(client_detail),
        "machine_busy_s": busy, "machine_capacity_s": (b - a) * (os.cpu_count() or 1),
        "others_s": others_total, "others_top": [[n, others_n[n], c] for n, c in others.most_common(15)],
        "unattributed_s": busy - firefox_total - client - others_total,
    }


def thread_report(sampler: Sampler, begin: str, end: str) -> dict:
    """{type: [[thread name, cpu s per browser, context switches per browser, threads per browser]]} between two snaps. A thread is matched
    by its tid; a thread that died inside the window is lost, a thread born inside it counts in full."""
    sb, se = sampler.snaps.get(begin), sampler.snaps.get(end)
    if not sb or not se:
        return {}
    where, roots = roots_and_kinds(sampler.info)
    n = max(1, len(roots))
    cpu, ctx, cnt = collections.Counter(), collections.Counter(), collections.Counter()
    for pid, threads in se["procs"].items():
        kind = where.get(int(pid), (None, "other"))[1]
        before = {t[0]: t for t in sb["procs"].get(pid, [])}
        for tid, c, cs, _state, name in threads:
            b = before.get(tid)
            key = (kind, name or "?")
            cpu[key] += c - (b[1] if b else 0.0)
            ctx[key] += cs - (b[2] if b else 0)
            cnt[key] += 1
    out: dict = {}
    for (kind, name), c in cpu.items():
        out.setdefault(kind, []).append([name, c / n, ctx[(kind, name)] / n, cnt[(kind, name)] / n])
    for kind in out:
        out[kind].sort(key=lambda r: -r[1])
        out[kind] = out[kind][:40]
    return out


# ----------------------------------------------------------------------------------------------------------------------- the arms

ABOUT_SUPPORT_JS = r"""() => {
  const rows = sel => [...document.querySelectorAll(sel + " tr")].map(tr => [...tr.children].map(c => c.textContent.trim()).join(" | "));
  const keep = /gfx|layers|webrender|layout\.frame_rate|widget\.|security\.sandbox|dom\.ipc|media\.hardware|apz|layout\.display|vsync|d3d11|angle|force-/i;
  return {
    graphics: rows("#graphics-tbody"), features: rows("#graphics-features-tbody"), diagnostics: rows("#graphics-diagnostics-tbody"),
    decisions: rows("#graphics-decisions-tbody"), prefs: rows("#prefs-tbody").filter(r => keep.test(r)),
    version: (document.querySelector("#version-box") || {}).textContent || ""
  };
}"""


OPEN_BROWSERS: list = []   # the browsers of the arm, so that about:support can open a context of its own


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


@contextlib.asynccontextmanager
async def launched(arm: str, binary: str, n: int):
    """Open n browsers of the arm at once in this client process and yield one page each."""
    if arm in OURS_ARMS:
        from invisible_playwright.async_api import InvisiblePlaywright
        if arm == "ourshl":
            os.environ["INVPW_TRUE_HEADLESS"] = "1"
        async with contextlib.AsyncExitStack() as stack:
            browsers = await asyncio.gather(*(stack.enter_async_context(
                InvisiblePlaywright(seed=100 + i, binary_path=binary, headless=arm != "ourswin", locale="en-US", timezone="Europe/Rome"))
                for i in range(n)))
            OPEN_BROWSERS[:] = browsers
            yield await asyncio.gather(*(b.new_page() for b in browsers))
        return
    async_playwright = importlib.import_module("playwright.async_api").async_playwright   # by name: the fork's guard forbids a static import
    pw = await async_playwright().start()
    browsers = []
    prefs = {"fission.autostart": True}
    if arm == "stockhid":     # what invisible_core adds when it puts the browser on its hidden desktop
        prefs.update({"security.sandbox.gpu.level": 0, "security.sandbox.content.level": 4})
    try:
        browsers = await asyncio.gather(*(pw.firefox.launch(headless=arm == "stock", firefox_user_prefs=prefs) for _ in range(n)))
        OPEN_BROWSERS[:] = browsers
        yield await asyncio.gather(*(b.new_page() for b in browsers))
    finally:
        for b in browsers:
            with contextlib.suppress(Exception):
                await b.close()
        with contextlib.suppress(Exception):
            await pw.stop()


async def window(sampler: Sampler, name: str, seconds: float, extra=None) -> None:
    sampler.snap(name + "-b")
    if extra:
        await extra("b")
    await asyncio.sleep(seconds)
    if extra:
        await extra("e")
    sampler.snap(name + "-e")


async def run_loop(pages: list, base: str, sampler: Sampler, notes: dict) -> None:
    outcome = {"completed": 0, "incomplete": [], "failed": [], "ms": []}
    sampler.snap("loop-b")
    await asyncio.gather(*(loop_one(p, base, i, outcome) for i, p in enumerate(pages)))
    sampler.snap("loop-e")
    notes["outcome"] = outcome


async def run_scenes(pages: list, base: str, sampler: Sampler, notes: dict) -> None:
    await window(sampler, "settle", SETTLE_S)            # the first seconds after the page exists
    # idle in three slices of 10 s (idle-s1, idle-s2 are the cuts), to see whether the CPU decays or stays
    sampler.snap("idle-b")
    await asyncio.sleep(IDLE_S / 3)
    sampler.snap("idle-s1")
    await asyncio.sleep(IDLE_S / 3)
    sampler.snap("idle-s2")
    await asyncio.sleep(IDLE_S / 3)
    sampler.snap("idle-e")
    for p in pages:
        await p.goto(base + "/static", wait_until="load", timeout=SCENARIO_TIMEOUT_S * 1000)
    await asyncio.sleep(2)
    await window(sampler, "static", STATIC_S)
    for p in pages:
        await p.goto(base + "/raf", wait_until="load", timeout=SCENARIO_TIMEOUT_S * 1000)
    await asyncio.sleep(2)
    frames: dict = {}

    async def stats(which: str) -> None:
        frames[which] = [await p.evaluate("window.__stats()") for p in pages]

    await window(sampler, "raf", RAF_S, stats)
    notes["raf_frames"] = [{"frames": e["frames"] - b["frames"], "ms": e["now"] - b["now"],
                            "fps": (e["frames"] - b["frames"]) / ((e["now"] - b["now"]) / 1000.0) if e["now"] > b["now"] else None,
                            "dpr": e["dpr"]} for b, e in zip(frames.get("b", []), frames.get("e", []))]
    for p in pages:
        await p.goto("about:blank")
    await asyncio.sleep(2)
    await window(sampler, "late", LATE_S)                # idle again, after everything above: does the CPU end?
    # about:support, as evidence only: the compositor, WebRender software or not, the refresh rate, and the prefs that matter
    try:
        ctx = await OPEN_BROWSERS[0].new_context()
        page = await ctx.new_page()
        await page.goto("about:support", timeout=30000)
        await asyncio.sleep(4)
        notes["about_support"] = await page.evaluate(ABOUT_SUPPORT_JS)
        await ctx.close()
    except Exception as exc:   # noqa: BLE001
        notes["about_support"] = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:300])}


async def run_arm_async(arm: str, binary: str, n: int, mode: str, base: str, sampler: Sampler, notes: dict) -> None:
    async with launched(arm, binary, n) as pages:
        await (run_loop if mode == "loop" else run_scenes)(pages, base, sampler, notes)


def run_arm(arm: str, binary: str, rnd: int, n: int, mode: str) -> dict:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    sampler = Sampler()
    sampler.start()
    whole_a = time.time()
    err = ""
    notes: dict = {}
    try:
        asyncio.run(run_arm_async(arm, binary, n, mode, base, sampler, notes))
    except BaseException as exc:   # noqa: BLE001
        err = "%s: %s" % (type(exc).__name__, exc)
    time.sleep(0.6)           # two more samples after the last close
    whole_b = time.time()
    sampler.stop()
    httpd.shutdown()
    windows = {}
    for w in (["loop"] if mode == "loop" else ["settle", "idle", "static", "raf", "late"]):
        sb, se = sampler.snaps.get(w + "-b"), sampler.snaps.get(w + "-e")
        if sb and se:
            windows[w] = {"report": window_report(sampler, sb["t"], se["t"]), "threads": thread_report(sampler, w + "-b", w + "-e")}
            if w == "idle":
                cuts = ["idle-b", "idle-s1", "idle-s2", "idle-e"]
                windows[w]["slices"] = [{"report": window_report(sampler, sampler.snaps[x]["t"], sampler.snaps[y]["t"]),
                                         "threads": thread_report(sampler, x, y)} for x, y in zip(cuts, cuts[1:])]
    outcome = notes.get("outcome") or {"completed": 0, "incomplete": [], "failed": [], "ms": []}
    return {
        "arm": arm, "round": rnd, "browsers": n, "mode": mode, "error": err, "windows": windows,
        "whole": window_report(sampler, whole_a, whole_b), "whole_wall_s": whole_b - whole_a, "ncpu": os.cpu_count(),
        "completed": outcome["completed"], "incomplete": outcome["incomplete"], "failed": outcome["failed"],
        "median_scenario_ms": statistics.median(outcome["ms"]) if outcome["ms"] else None,
        "raf_frames": notes.get("raf_frames"), "about_support": notes.get("about_support"),
        "sampler_errors": [v[2] for v in sampler.info.values() if v[0] == "error"],
    }


def print_arm(r: dict) -> None:
    print("=" * 110)
    print("arm %s, round %s, %d browser(s), mode %s: %s"
          % (r["arm"], r["round"], r["browsers"], r["mode"], "ERROR " + r["error"] if r["error"] else "ok"))
    if r["mode"] == "loop":
        print("  %d of %d scenarios completed, median scenario %.0f ms" % (r["completed"], r["browsers"] * ROUNDS, r["median_scenario_ms"] or 0))
    for line in (r["incomplete"] + r["failed"])[:10]:
        print("  not completed: " + line)
    for name, w in list(r["windows"].items()) + [("whole arm", {"report": r["whole"], "threads": {}})]:
        p = w["report"]
        warn = "" if p["browsers_seen"] == r["browsers"] else "   <-- WARNING: %d expected" % r["browsers"]
        print("  -- window %s: %.1f s, %d browser root(s)%s" % (name, p["wall_s"], p["browsers_seen"], warn))
        print("     CPU s per browser (median) " + "  ".join("%s %.2f" % (t, p["median_by_type"][t]) for t in TYPES if p["median_by_type"][t] > 0)
              + "   TOTAL %.2f" % p["median_total"])
        print("     client side %.2f s %s; machine busy %.2f s of %.1f; others %.2f s; unattributed %.2f s"
              % (p["client_s"], {k: round(v, 2) for k, v in p["client_detail"].items() if v > 0.05}, p["machine_busy_s"],
                 p["machine_capacity_s"], p["others_s"], p["unattributed_s"]))
        print("     others: " + ", ".join("%s x%d %.2f" % tuple(o) for o in p["others_top"][:6]))
        for kind in ("parent", "gpu", "tab"):
            rows = w["threads"].get(kind, [])[:6]
            if rows:
                print("     threads %s: " % kind + ", ".join("%s %.2f" % (x[0], x[1]) for x in rows))
    if r["raf_frames"]:
        print("  rAF frames: " + str(r["raf_frames"]))
    if r["about_support"]:
        print("  about:support: " + json.dumps(r["about_support"])[:3000])
    if r["sampler_errors"]:
        print("  SAMPLER ERRORS: " + str(r["sampler_errors"][:5]))
    sys.stdout.flush()


# ------------------------------------------------------------------------------------------------------------ the stock arm on a desktop

def run_on_hidden_desktop(log: Path) -> int:
    """Create a hidden desktop the way invisible_core does (CreateDesktopW, never switched to) and run this script again as a process
    CREATED on it (STARTUPINFO.lpDesktop), so the python client, stock's node driver and every firefox are born there. Returns its exit code."""
    import ctypes
    import secrets
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.CreateDesktopW.restype = wintypes.HANDLE
    user32.CreateDesktopW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)
    user32.CloseDesktop.argtypes = (wintypes.HANDLE,)
    name = "hunt_" + secrets.token_hex(6)
    desk = user32.CreateDesktopW(name, None, None, 0, 0x10000000, None)
    if not desk:
        raise RuntimeError("CreateDesktopW failed, WinError %d" % ctypes.get_last_error())

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                    ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD), ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                    ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
                    ("hStdError", wintypes.HANDLE)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE), ("dwProcessId", wintypes.DWORD),
                    ("dwThreadId", wintypes.DWORD)]

    k32.CreateProcessW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION))
    k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = name
    pi = PROCESS_INFORMATION()
    cmd = subprocess.list2cmdline([sys.executable, "-u", os.path.abspath(sys.argv[0])] + sys.argv[1:] + ["--hidden-child", "--log", str(log)])
    # CREATE_NEW_CONSOLE: the client has a console of its own, on the hidden desktop; its output goes to the log file
    if not k32.CreateProcessW(None, ctypes.create_unicode_buffer(cmd), None, None, False, 0x00000010, None, None,
                              ctypes.byref(si), ctypes.byref(pi)):
        raise RuntimeError("CreateProcessW failed, WinError %d" % ctypes.get_last_error())
    print("client created on the hidden desktop %s, pid %d" % (name, pi.dwProcessId), flush=True)
    k32.WaitForSingleObject(pi.hProcess, 0xFFFFFFFF)
    code = wintypes.DWORD()
    k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
    k32.CloseHandle(pi.hProcess)
    k32.CloseHandle(pi.hThread)
    user32.CloseDesktop(desk)
    with contextlib.suppress(Exception):
        sys.stdout.write(log.read_text(encoding="utf-8", errors="replace"))
    return int(code.value)


# ----------------------------------------------------------------------------------------------------------------------- compare

def mean(values: list) -> float:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else float("nan")


def compare(directory: Path) -> int:
    results = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))]
    results = [r for r in results if "windows" in r]
    good = [r for r in results if not r["error"]]
    print("runs: %d good of %d" % (len(good), len(results)))
    for r in results:
        print("  %s-n%d-%s-r%s %s" % (r["arm"], r["browsers"], r["mode"], r["round"], "ERROR " + r["error"][:300] if r["error"] else "ok"))
    keys = sorted({(r["mode"], r["browsers"], w) for r in good for w in r["windows"]},
                  key=lambda k: (k[0], k[1], ["loop", "settle", "idle", "static", "raf", "late"].index(k[2])))
    show = [a for a in ARMS if any(r["arm"] == a for r in good)]

    def runs(arm, key):
        return [r["windows"][key[2]] for r in good if r["arm"] == arm and r["mode"] == key[0] and r["browsers"] == key[1] and key[2] in r["windows"]]

    for key in keys:
        print()
        print("=" * 130)
        print("WINDOW %s, %d browser(s) (mode %s); CPU seconds, mean over the rounds; per browser = median over the browsers" % (key[2], key[1], key[0]))
        print("%-34s" % "" + "".join("%10s" % a for a in show) + "   ours/stockwin ourswin/stockwin ours/stockhid ours/stock")
        rows = [("%s per browser" % t, lambda p, t=t: p["median_by_type"][t]) for t in TYPES]
        rows += [("TOTAL per browser", lambda p: p["median_total"]), ("all firefox", lambda p: p["total_all"]), ("client side", lambda p: p["client_s"]),
                 ("machine busy (all vCPUs)", lambda p: p["machine_busy_s"]), ("others (all other processes)", lambda p: p["others_s"]),
                 ("unattributed (kernel, DPC...)", lambda p: p["unattributed_s"]), ("wall s", lambda p: p["wall_s"])]
        for label, f in rows:
            vals = {a: mean([f(w["report"]) for w in runs(a, key)]) for a in show}
            if all(v != v or abs(v) < 0.005 for v in vals.values()):
                continue

            def ratio(x, y):
                ok = x in vals and y in vals and vals[x] == vals[x] and vals[y] == vals[y] and abs(vals[y]) > 0.005
                return "%8.2f" % (vals[x] / vals[y]) if ok else "%8s" % "-"
            print("%-34s" % label + "".join("%10.2f" % vals[a] for a in show) + "   " + ratio("ours", "stockwin") + "       "
                  + ratio("ourswin", "stockwin") + "      " + ratio("ours", "stockhid") + "     " + ratio("ours", "stock"))
        for a in show:
            sl = [w["slices"] for w in runs(a, key) if w.get("slices")]
            if sl:
                print("   idle by 10 s slice, %s (parent CPU s; total per browser; GPU): " % a + "; ".join(
                    "%.2f / %.2f / %.2f" % (mean([x[i]["report"]["median_by_type"]["parent"] for x in sl]), mean([x[i]["report"]["median_total"] for x in sl]),
                                            mean([x[i]["report"]["median_by_type"]["gpu"] for x in sl])) for i in range(3)))
        for a in show:
            tops = [w["report"]["others_top"] for w in runs(a, key)]
            if tops:
                acc = collections.Counter()
                for t in tops:
                    for n, _, c in t:
                        acc[n] += c / len(tops)
                print("   others in %s: " % a + ", ".join("%s %.2f" % (n, c) for n, c in acc.most_common(6)))
        for kind in ("parent", "gpu", "tab"):
            names: dict = {}
            for a in show:
                for w in runs(a, key):
                    for name, c, _ctx, _n in w["threads"].get(kind, []):
                        names.setdefault(name, {}).setdefault(a, []).append(c)
            if not names:
                continue
            avg = {n: {a: mean(v) for a, v in d.items()} for n, d in names.items()}
            top = sorted(avg, key=lambda n: -max(avg[n].values()))[:14]
            print("   THREADS of %s processes (CPU s per browser; '-' = no such thread):" % kind)
            print("   %-30s" % "" + "".join("%10s" % a for a in show))
            for n in top:
                print("   %-30s" % n[:30] + "".join("%10s" % ("%.2f" % avg[n][a] if a in avg[n] else "-") for a in show))
    print()
    for r in good:
        if r["raf_frames"]:
            print("rAF %s r%s: " % (r["arm"], r["round"]) + ", ".join("%.1f fps (dpr %s)" % (f["fps"] or 0, f["dpr"]) for f in r["raf_frames"]))
    for r in good:
        if r["about_support"]:
            print("about:support %s r%s: %s" % (r["arm"], r["round"], json.dumps(r["about_support"])[:1500]))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=ARMS)
    ap.add_argument("--browsers", type=int, default=4)
    ap.add_argument("--mode", choices=["loop", "scenes"], default="loop")
    ap.add_argument("--binary", default=os.environ.get("INVPW_BINARY_PATH", ""))
    ap.add_argument("--round", type=int, default=1)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--compare", type=Path)
    ap.add_argument("--hidden-child", action="store_true")
    ap.add_argument("--log", type=Path)
    args = ap.parse_args()
    if args.compare:
        return compare(args.compare)
    if not args.arm:
        ap.error("--arm or --compare")
    if args.arm in OURS_ARMS and not args.binary:
        ap.error("the arms of ours need --binary (or INVPW_BINARY_PATH)")
    if args.arm == "stockhid" and not args.hidden_child:
        return run_on_hidden_desktop(args.log or Path(os.environ.get("TEMP", ".")) / "stockhid.log")
    if args.hidden_child and args.log:
        sys.stdout = sys.stderr = open(args.log, "w", buffering=1, encoding="utf-8")
    result = run_arm(args.arm, args.binary, args.round, args.browsers, args.mode)
    print_arm(result)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    return 1 if result["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
