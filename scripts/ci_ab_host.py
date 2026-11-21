#!/usr/bin/env python3
"""Host-footprint A/B oracle: after the same short journey, what the engine leaves on the machine is the same.

scripts/ci_ab_oracle.py compares what a PAGE reads. This compares what the engine does to the HOST, which a page cannot
read but a release can change (a helper that is no longer spawned, a library the parent maps that it did not map before):

  * the process tree: the multiset of process KINDS (the parent, or the last argument of `-contentproc` with its
    `-sandboxingKind N` and `-isForBrowser` / `-notForBrowser`);
  * the modules: for each kind, the union of the files its processes have mapped (Windows `memory_maps()`, Linux
    /proc/<pid>/maps), normalized so that the install dir, the system dirs and the temporary paths of the run are the
    same strings on every engine and every run;
  * the children that live and die before the snapshot: Linux, `strace` (execve, clone, fork, prctl: every process
    the tree ever made, named by what it ran); Windows, the job object's TotalProcesses plus the names the polling and
    a WMI trace (Win32_ProcessStartTrace through PowerShell) saw, see `windows_short_lived`.

The journey is the first steps of the fake web's P4 walk (tests/fakeweb/profile_p4.py, no internet) driven by the stock
Playwright client: home, a teaser, the next article, the category (the ad frames of the other origin are out of
process). The snapshot is taken after the journey, once the tree is quiet, before the browser is closed.

    python ci_ab_host.py run <firefox> [--seal SEAL] --leg linux-x86_64 --out footprint.json [--seeds 11,12,13]
    python ci_ab_host.py compare A.json B.json --out diff.json [--a-a]
    python ci_ab_host.py ab [<candidate> --candidate-seal SEAL] --leg L --out DIR [--published-binary B [--published-seal S]]

`ab` without a candidate is the A/A control (the published engine twice). With a candidate: the A/A first, then the
published engine against the candidate. Exit 0 green, 1 red, 2 the control is broken (A/A differs: what is compared is not
stable, the A/B says nothing).

Declared differences (ci/ab-host-expected.json) are applied to the A/B only, never to the A/A, and one that does not show
is red as stale. Nothing here reads the clock for a verdict.

The leaves that differ between two launches of ONE engine are the noise (ci/ab-host-volatile.json), and they are born from
the A/A: each entry names the leaf, the system, the reason and the run that proved it. A volatile leaf is excused in the A/A
and in the A/B alike (it is noise of the engine, not of the candidate), only for the system it is declared for; unless the
entry says `absent_ok`, the leaf must still be present on both sides, so that a kind or a counter that vanishes is not noise.
An entry that matches no leaf of the run is red as stale.
"""
from __future__ import annotations

import argparse
import codecs
import collections
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tests"))
import ci_ab_oracle as oracle  # noqa: E402  (fetch_published, ENGINE_TAG)
import ci_host_gate as gate  # noqa: E402  (SEEDS)

FORMAT = "ab-host/1"
EXPECTED_FORMAT = "ab-host-expected/1"
EXPECTED_FILE = ROOT / "ci" / "ab-host-expected.json"
VOLATILE_FORMAT = "ab-host-volatile/1"
VOLATILE_FILE = ROOT / "ci" / "ab-host-volatile.json"
SEEDS_FILE = ROOT / "ci" / "persona-seeds.json"
#: The steps of the journey (tests/fakeweb/profile_p4.py STEPS, by method name).
JOURNEY = (("open home", "s1_open_home"),
           ("hover and click a teaser", "s2_hover_click_teaser"),
           ("scroll to the bottom, next article", "s3_scroll_next"),
           ("nav Category", "s7_nav_category"))
#: What a seed's footprint must hold at least, per category of leaf: a footprint that holds less did not see the tree.
#: Provisional: the first A/A on both systems gives the real numbers (they are in diff.json, "leaves").
FLOORS = {"proc": 3, "mod": 100}
#: The categories of leaf a footprint has.
CATEGORIES = ("proc", "mod", "spawn", "short")
#: Of what a Windows run says about short-lived children, what is COMPARED (the exact count of the job, the names of the WMI
#: trace). The polling is kept as evidence only: it sees a process only if it lives longer than its period.
MISSING = "<missing>"
#: Seconds the tree is polled while it settles, and how many equal polls in a row make it quiet; the wait ends at LIMIT (an
#: acquisition limit: the verdict never reads it, `quiet.reached` says whether it was reached).
QUIET_PERIOD_S, QUIET_POLLS, QUIET_LIMIT_S = 0.25, 8, 60.0
RUN_TIMEOUT_S = 30 * 60
CLIP = 400


# -
# Kinds of process.

def kind_of(cmdline) -> str:
    """parent = no `-contentproc`; else the last argument (tab, gpu, socket, rdd, utility, forkserver ...) plus
    `-sandboxingKind N` and `-isForBrowser` / `-notForBrowser` when the command line has them: `tab/isForBrowser`."""
    cmd = [str(a) for a in (cmdline or [])]
    if "-contentproc" not in cmd:
        return "parent"
    parts = [cmd[-1].strip('"').lower() if cmd else "?"]
    if "-sandboxingKind" in cmd:
        i = cmd.index("-sandboxingKind")
        parts.append("sandboxingKind=%s" % (cmd[i + 1] if i + 1 < len(cmd) else "?"))
    if "-isForBrowser" in cmd:
        parts.append("isForBrowser")
    elif "-notForBrowser" in cmd:
        parts.append("notForBrowser")
    return "/".join(parts)


# -
# Normalization of a mapped path: the same string for the same file on every engine, run and runner.

class Context:
    """What the normalizer needs to know about the machine and the run: `platform` ("win32" or "linux"), `app` (the engine's
    install folders: the link and the real one), `drops` (folders whose files are the run's own: profile, temp, home) and, on
    Windows, `sysroot` (%SystemRoot%)."""

    def __init__(self, platform: str, app=(), drops=(), sysroot: str = "C:\\Windows") -> None:
        self.platform = platform
        self.windows = platform == "win32"
        self.app = [self._key(p) for p in app if p]
        self.drops = [self._key(p) for p in drops if p]
        self.sysroot = self._key(sysroot)

    def _key(self, path: str) -> str:
        if self.windows:
            return path.replace("/", "\\").rstrip("\\").lower()
        return path.rstrip("/")

    def under(self, path: str, root: str) -> str | None:
        """The part of `path` below `root` (with `/`), or None."""
        sep = "\\" if self.windows else "/"
        if path == root:
            return ""
        if path.startswith(root + sep):
            return path[len(root) + 1:].replace("\\", "/")
        return None


_DIGITS = re.compile(r"\d+")
_NT_VOLUME = re.compile(r"^\\device\\harddiskvolume\d+", re.I)
_DELETED = " (deleted)"
#: Folders no run shares with another: where a temporary file or a profile is (any system).
_PRIVATE_WINDOWS = re.compile(r"\\(appdata|temp|pytest-of-[^\\]*|invisible-[^\\]*|playwright[^\\]*)\\")


def _no_numbers(text: str) -> str:
    return _DIGITS.sub("N", text)


def normalize_module(path: str, ctx: Context) -> str | None:
    """One mapped path as the footprint spells it, or None when it is not a file (an anonymous mapping)."""
    if not path:
        return None
    if ctx.windows:
        return _normalize_windows(path, ctx)
    return _normalize_posix(path, ctx)


def _normalize_windows(path: str, ctx: Context) -> str | None:
    p = path.replace("/", "\\")
    p = _NT_VOLUME.sub("c:", p) if p.lower().startswith("\\device\\") else p
    low = p.lower()
    for root in ctx.app:
        rel = ctx.under(low, root)
        if rel is not None:
            return "<app>/" + rel
    for root in ctx.drops:
        if ctx.under(low, root) is not None:
            return "<profile>"
    sysroot = ctx.sysroot
    rel = ctx.under(low, sysroot)
    if rel is not None:
        parts = rel.split("/")
        if parts[0] == "system32" and len(parts) == 2:
            return "<sys>/" + parts[1]
        if parts[0] == "syswow64" and len(parts) == 2:
            return "<syswow>/" + parts[1]
        if parts[0] == "winsxs" and len(parts) >= 3:
            return "<winsxs>/" + parts[-1]
        if parts[0] == "temp":
            return "<profile>"
        return "<win>/" + rel
    if re.match(r"^[a-z]:\\users\\", low) or _PRIVATE_WINDOWS.search(low + "\\"):
        return "<profile>"
    return low


def _normalize_posix(path: str, ctx: Context) -> str | None:
    if not path.startswith("/"):
        return None  # [heap], [stack], [vdso], [anon:...]: not a file
    p, deleted = path, ""
    if p.endswith(_DELETED):
        p, deleted = p[:-len(_DELETED)], " (deleted)"
    if p.startswith("/memfd:"):
        return "<memfd:%s>" % _no_numbers(p[len("/memfd:"):])
    if p.startswith("/dev/shm/"):
        return "<shm:%s>" % _no_numbers(p[len("/dev/shm/"):])
    for root in ctx.app:
        rel = ctx.under(p, root)
        if rel is not None:
            return "<app>/" + rel + deleted
    for root in ctx.drops:
        if ctx.under(p, root) is not None:
            return "<profile>"
    if p.startswith(("/tmp/", "/var/tmp/")):
        return "<profile>"
    p = re.sub(r"^/proc/\d+/", "/proc/N/", p)
    p = re.sub(r"^/run/user/\d+", "/run/user/N", p)
    return p + deleted


def normalize_modules(paths, ctx: Context) -> list:
    out = set()
    for path in paths:
        n = normalize_module(path, ctx)
        if n:
            out.add(n)
    return sorted(out)


# -
# Linux: the processes the tree ever made, from the strace log.

_LINE = re.compile(r"^(\d+)\s+(\w+)\((.*)$")
_RESUMED = re.compile(r"^(\d+)\s+<\.\.\. (\w+) resumed>(.*)$")
_RESULT = re.compile(r"=\s+(-?\d+)\b[^=]*$")
_STR = re.compile(r'"((?:[^"\\]|\\.)*)"')
_EXEC = re.compile(r'"((?:[^"\\]|\\.)*)",\s*\[(.*?)\],\s*(?:0x[0-9a-fA-F]+|NULL|\[)')
_FLAGS = re.compile(r"flags=([A-Za-z0-9_|]+)")
_SET_NAME = re.compile(r'PR_SET_NAME,\s*"((?:[^"\\]|\\.)*)"')
#: What strace traces for this: the processes the tree makes and what they run, nothing else.
STRACE_CALLS = "execve,clone,clone3,fork,vfork,prctl"


def _unescape(text: str) -> str:
    return codecs.escape_decode(text.encode("latin-1", errors="replace"))[0].decode("utf-8", errors="replace")


def label_of_exec(path: str, argv: list, real: str) -> str:
    """What a process that ran `path` with `argv` is called: the engine's kinds, `glxtest` (argv0 or `-glxtest`), else the
    program's name."""
    if "-contentproc" in argv:
        return kind_of(argv)  # whatever path it was started by (the binary, /proc/self/exe)
    if any(a.lstrip("-") == "glxtest" for a in argv[1:]) or os.path.basename(argv[0] if argv else "") == "glxtest":
        return "glxtest"
    if path == real or os.path.basename(path) == os.path.basename(real):
        return "parent"
    return os.path.basename(path) or path


def spawned_processes(lines, real: str) -> dict:
    """{pid: {"parent", "exec", "name", "label"}} for every PROCESS (not thread) the traced tree made, from a strace log read as
    lines: a clone without CLONE_THREAD, a fork or a vfork is a process; the last execve says what it ran, else
    `prctl(PR_SET_NAME)` says what it called itself, else it is `fork<-<its parent's label>`."""
    procs: dict = {}
    names: dict = {}  # pid -> what it called itself (any pid: the child's first calls can come before its parent's clone returns)
    pending: dict = {}  # pid -> ("clone", is_thread) or ("execve", path, argv) of a call strace split in two

    def proc(pid):
        return procs.setdefault(pid, {"parent": None, "exec": None, "name": None})

    for line in lines:
        m = _LINE.match(line)
        if m:
            pid, call, rest = int(m.group(1)), m.group(2), m.group(3)
            unfinished = "<unfinished ...>" in rest
            result = None if unfinished else _RESULT.search(rest)
            result = int(result.group(1)) if result else None
            if call in ("clone", "clone3", "fork", "vfork"):
                flags = _FLAGS.search(rest)
                is_thread = bool(flags and "CLONE_THREAD" in flags.group(1))
                if unfinished:
                    pending[pid] = ("clone", is_thread)
                elif result is not None and result > 0 and not is_thread:
                    proc(result)["parent"] = pid
            elif call == "execve":
                e = _EXEC.match(rest)
                if e:
                    path, argv = _unescape(e.group(1)), [_unescape(s) for s in _STR.findall(e.group(2))]
                    if unfinished:
                        pending[pid] = ("execve", path, argv)
                    elif result is not None and result >= 0:
                        proc(pid)["exec"] = (path, argv)
                        names.pop(pid, None)
            elif call == "prctl":
                n = _SET_NAME.search(rest)
                if n:
                    names[pid] = _unescape(n.group(1))
            continue
        m = _RESUMED.match(line)
        if m:
            pid, call, rest = int(m.group(1)), m.group(2), m.group(3)
            held = pending.pop(pid, None)
            result = _RESULT.search(rest)
            result = int(result.group(1)) if result else None
            if held and held[0] == "clone" and call.startswith(("clone", "fork", "vfork")):
                if result is not None and result > 0 and not held[1]:
                    proc(result)["parent"] = pid
            elif held and held[0] == "execve" and call == "execve" and result is not None and result >= 0:
                proc(pid)["exec"] = (held[1], held[2])
                names.pop(pid, None)
    for pid, p in procs.items():
        p["name"] = names.get(pid)
    labels: dict = {}

    def label(pid, seen=()):
        if pid in labels:
            return labels[pid]
        p = procs.get(pid)
        if p is None or pid in seen:
            return "?"
        if p["exec"]:
            out = label_of_exec(p["exec"][0], p["exec"][1], real)
        elif p["name"]:
            out = p["name"]
        else:
            out = "fork<-%s" % label(p["parent"], seen + (pid,))
        labels[pid] = out
        return out

    for pid in procs:
        procs[pid]["label"] = label(pid)
    return procs


def spawned_counts(procs: dict) -> dict:
    """{label: how many processes} of `spawned_processes`."""
    return dict(sorted(collections.Counter(p["label"] for p in procs.values()).items()))


# -
# Windows: the children that are gone before the snapshot.

def accounting_of(job) -> dict:
    """JOBOBJECT_BASIC_ACCOUNTING_INFORMATION of the launch's job: how many processes the job has EVER held (TotalProcesses, the
    exact count of every child of the tree, short-lived or not), how many are alive and how many were terminated.

    The way to the handle: `RemoteServer.process` (invisible_playwright/_juggler/remote.py `process`) is the core's
    `_WindowsProcess` (invisible_core/juggler/connection.py:1014), whose `.job` (:1044, set at :1313) is the launch's `EngineJob`
    (invisible_core/process.py:547) and `EngineJob.handle` (:574) is the Windows job handle, valid in this process. The core
    itself reads only ActiveProcesses (`EngineJob.active`, process.py:657-684: the struct has TotalProcesses at :668 and
    discards it), so this makes the same QueryInformationJobObject call, information class 1, and keeps the field."""
    if job is None:
        return {"reachable": False, "why": "the launch has no job (invisible_core/juggler/connection.py `_spawn_windows`: "
                                           "EngineJob.create() answered None, or the process is not a _WindowsProcess)"}
    if not getattr(job, "handle", None):
        return {"reachable": False, "why": "the job's handle is closed (invisible_core/process.py EngineJob.close; read it before the close)"}
    import ctypes
    from ctypes import wintypes

    class Accounting(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                    ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                    ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    info = Accounting()
    if not k.QueryInformationJobObject(job.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
        return {"reachable": False, "why": "QueryInformationJobObject failed: error %d" % ctypes.get_last_error()}
    return {"reachable": True, "total_processes": int(info.TotalProcesses), "active_processes": int(info.ActiveProcesses),
            "terminated_processes": int(info.TotalTerminatedProcesses)}


WMI_SCRIPT = r"""
param([string]$out, [string]$stop)
$ErrorActionPreference = 'Stop'
Register-WmiEvent -Class Win32_ProcessStartTrace -SourceIdentifier abhost | Out-Null
Set-Content -Path "$out.ready" -Value 'ready'
while (-not (Test-Path $stop)) {
  $e = Wait-Event -SourceIdentifier abhost -Timeout 1
  if ($e) {
    $n = $e.SourceEventArgs.NewEvent
    Add-Content -Path $out -Value ("{0}`t{1}`t{2}" -f $n.ProcessID, $n.ParentProcessID, $n.ProcessName)
    Remove-Event -EventIdentifier $e.EventIdentifier
  }
}
Unregister-Event -SourceIdentifier abhost
"""


def descendants_of(events, roots) -> list:
    """The (pid, ppid, name) of `events` that are a root or come from one, by the parent pid each start event says."""
    wanted = set(roots)
    out, changed = [], True
    chosen = set()
    while changed:
        changed = False
        for i, (pid, ppid, name) in enumerate(events):
            if i not in chosen and (pid in wanted or ppid in wanted):
                chosen.add(i)
                wanted.add(pid)
                changed = True
    for i in sorted(chosen):
        out.append(events[i])
    return out


def wmi_names(events, roots) -> dict:
    """{process name (lower case): how many started} among the descendants of `roots`, the roots themselves included."""
    return dict(sorted(collections.Counter(name.lower() for _p, _pp, name in descendants_of(events, roots)).items()))


class WmiTrace:
    """Method 2 for the short-lived children on Windows: a PowerShell subprocess subscribed to Win32_ProcessStartTrace (needs the
    rights to register a WMI event: an administrator, as the hosted runners are). Started BEFORE the launch, stopped after the
    snapshot. `result(roots)` is {"available": bool, "why": ..., "names": {...}, "events": n}."""

    def __init__(self, work: Path) -> None:
        self.out, self.stop = work / "wmi-events.tsv", work / "wmi.stop"
        self.script = work / "wmi.ps1"
        self.proc = None
        self.error = None

    def start(self, timeout_s: float = 60.0) -> None:
        if sys.platform != "win32":
            self.error = "WMI is Windows only"
            return
        self.script.write_text(WMI_SCRIPT, encoding="utf-8")
        exe = shutil.which("powershell") or shutil.which("pwsh")
        if not exe:
            self.error = "no powershell on this machine"
            return
        self.proc = subprocess.Popen([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.script),
                                      str(self.out), str(self.stop)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ready = Path(str(self.out) + ".ready")
        end = time.monotonic() + timeout_s
        while time.monotonic() < end and not ready.exists():
            if self.proc.poll() is not None:
                break
            time.sleep(0.2)
        if not ready.exists():
            _o, err = self._finish()
            self.error = "the WMI subscription did not start: %s" % ((err or "").strip()[:400] or "no message")

    def _finish(self):
        if self.proc is None:
            return "", ""
        self.stop.write_text("stop", encoding="utf-8")
        try:
            return self.proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.communicate()

    def result(self, roots) -> dict:
        if self.error:
            return {"available": False, "why": self.error}
        time.sleep(2.0)  # the events are delivered to the subscriber a moment after the start; read what has come, then stop
        _o, err = self._finish()
        events = []
        if self.out.exists():
            for line in self.out.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.split("\t")
                if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                    events.append((int(parts[0]), int(parts[1]), parts[2].strip()))
        if self.proc is not None and self.proc.returncode not in (0, None) and not events:
            return {"available": False, "why": "powershell ended with %s: %s" % (self.proc.returncode, (err or "").strip()[:400])}
        return {"available": True, "events_seen": len(events), "names": wmi_names(events, roots)}


class Poller:
    """Names, by polling, the engine processes that exist at some moment (evidence for the Windows run: a process that lives
    less than the period is not seen, and the job's count says how many were missed). `pids` gives the pids to look at."""

    def __init__(self, pids, period: float = 0.02) -> None:
        self.pids, self.period = pids, period
        self.seen: dict = {}
        self._halt = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ab-host-poller", daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        import psutil

        while not self._halt.is_set():
            try:
                for pid in self.pids():
                    try:
                        p = psutil.Process(pid)
                        key = (pid, p.create_time())
                        if key not in self.seen:
                            self.seen[key] = kind_of(p.cmdline()) if is_engine_name(p.name()) else p.name().lower()
                    except Exception:
                        continue
            except Exception:
                pass
            self._halt.wait(self.period)

    def stop(self) -> dict:
        self._halt.set()
        self._thread.join(timeout=10)
        return dict(sorted(collections.Counter(self.seen.values()).items()))


def is_engine_name(name: str) -> bool:
    return (name or "").lower().startswith("firefox")


# -
# The snapshot of the tree.

def engine_processes(root_pid: int, job=None) -> list:
    """The living engine processes of the tree: `[(psutil.Process, cmdline)]`. Windows: the job's own list; else the descendants of
    `root_pid` (and `root_pid` itself when it is the engine)."""
    import psutil
    from _cpu_by_type import is_engine

    found = []
    pids = []
    if job is not None and getattr(job, "handle", None):
        pids = list(job.pids())
    else:
        try:
            root = psutil.Process(root_pid)
            pids = [root.pid] + [c.pid for c in root.children(recursive=True)]
        except psutil.Error:
            pids = []
    for pid in pids:
        try:
            p = psutil.Process(pid)
            if is_engine(p):
                found.append((p, p.cmdline()))
        except (psutil.Error, OSError):
            continue
    return found


def wait_quiet(read, period=QUIET_PERIOD_S, polls=QUIET_POLLS, limit_s=QUIET_LIMIT_S, sleep=time.sleep, now=time.monotonic) -> tuple:
    """Reads the tree (`read()` returns the set of its pids) until it has not changed for `polls` reads in a row, or `limit_s`.
    Returns (last set, {"reached": bool, "reads": n})."""
    end = now() + limit_s
    last, same, reads = None, 0, 0
    while True:
        current = frozenset(read())
        reads += 1
        same = same + 1 if current == last else 1
        last = current
        if same >= polls:
            return set(current), {"reached": True, "reads": reads}
        if now() >= end:
            return set(current), {"reached": False, "reads": reads}
        sleep(period)


def modules_of(proc, windows: bool):
    """The files `proc` has mapped: Windows `memory_maps()` (path), Linux /proc/<pid>/maps. Raises OSError / psutil errors."""
    if windows:
        return [m.path for m in proc.memory_maps()]
    paths = set()
    with open("/proc/%d/maps" % proc.pid, encoding="utf-8", errors="replace") as f:
        for line in f:
            fields = line.rstrip("\n").split(None, 5)
            if len(fields) == 6:
                paths.add(fields[5])
    return sorted(paths)


def take_footprint(processes, ctx: Context) -> dict:
    """{"kinds", "modules", "unreadable", "processes"} of `processes` ([(psutil.Process, cmdline)])."""
    kinds = collections.Counter()
    mods: dict = {}
    unreadable: list = []
    listing = []
    for proc, cmd in processes:
        kind = kind_of(cmd)
        kinds[kind] += 1
        listing.append({"pid": proc.pid, "kind": kind})
        try:
            mods.setdefault(kind, set()).update(modules_of(proc, ctx.windows))
        except Exception as e:  # noqa: BLE001 - a process that cannot be read is said, never skipped
            unreadable.append("%s (pid %d): %s: %s" % (kind, proc.pid, type(e).__name__, str(e)[:120]))
    return {"kinds": dict(sorted(kinds.items())),
            "modules": {k: normalize_modules(v, ctx) for k, v in sorted(mods.items())},
            "unreadable": unreadable, "processes": listing}


# -
# The comparison.

def leaves_of(seed_footprint: dict) -> dict:
    """{leaf: value} of one seed: proc.<kind> = count; mod.<kind>:<path> = 1; spawn.<label> = count (Linux strace);
    short.job_total_processes and short.wmi.<name> = count (Windows)."""
    out = {}
    for kind, n in seed_footprint.get("kinds", {}).items():
        out["proc." + kind] = n
    for kind, paths in seed_footprint.get("modules", {}).items():
        for path in paths:
            out["mod.%s:%s" % (kind, path)] = 1
    for label, n in seed_footprint.get("spawned", {}).items():
        out["spawn." + label] = n
    short = seed_footprint.get("short_lived") or {}
    job = short.get("job") or {}
    if job.get("reachable"):
        out["short.job_total_processes"] = job["total_processes"]
    wmi = short.get("wmi") or {}
    if wmi.get("available"):
        for name, n in wmi.get("names", {}).items():
            out["short.wmi." + name] = n
    return out


def category_of(leaf: str) -> str:
    return leaf.split(".", 1)[0]


def _clip(value):
    text = json.dumps(value, sort_keys=True)
    return value if len(text) <= CLIP else text[:CLIP // 2] + "..."


def load_expected(path: Path = EXPECTED_FILE) -> list:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("format") != EXPECTED_FORMAT:
        raise SystemExit("%s is not an %s file" % (path, EXPECTED_FORMAT))
    return document["differences"]


def load_volatile(path: Path = VOLATILE_FILE) -> list:
    """The entries of the volatile list: each `{"os", "leaf", "reason", "aa_run"}` and, when the leaf may be absent on one side, `"absent_ok": true`. `leaf` is a
    pattern (`fnmatch`) over the leaf names of `leaves_of`. A malformed list is an error, not an empty one."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("format") != VOLATILE_FORMAT:
        raise SystemExit("%s: format %r, not %r" % (path, document.get("format"), VOLATILE_FORMAT))
    entries = document.get("entries")
    if not isinstance(entries, list):
        raise SystemExit("%s: `entries` is not a list" % path)
    seen = set()
    for entry in entries:
        if entry.get("os") not in ("linux", "windows"):
            raise SystemExit("%s: every entry needs an os, linux or windows (%r)" % (path, entry))
        for field in ("leaf", "reason", "aa_run"):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                raise SystemExit("%s: every entry needs a non-empty %s (%r)" % (path, field, entry))
        if not isinstance(entry.get("absent_ok", False), bool):
            raise SystemExit("%s: absent_ok is true or false (%r)" % (path, entry))
        if (entry["os"], entry["leaf"]) in seen:
            raise SystemExit("%s: the leaf %r is listed twice for %s" % (path, entry["leaf"], entry["os"]))
        seen.add((entry["os"], entry["leaf"]))
    return entries


def _declared_covers(entry: dict, diff: dict) -> bool:
    """A declared difference covers a difference of `spawn.<name>` that is the published engine having it (count >= 1) and the
    candidate not having it."""
    if entry.get("category") != "spawn" or diff["leaf"] != "spawn." + entry["name"]:
        return False
    published, candidate = diff["a"], diff["b"]
    return (published != MISSING and published >= 1) and candidate == MISSING


def judge(a: dict, b: dict, expected: list | None = None, a_a: bool = False, platform: str | None = None,
          floors: dict | None = None, volatile: list | None = None) -> dict:
    """The verdict on two footprints (the document `run` writes). `expected` is the declared differences, applied only when `a_a`
    is false; `volatile` is the noise of the engine (`load_volatile`), applied in both modes. Returns {"verdict", "problems",
    "differences", "declared", "excused", "volatile", "leaves": {seed: {category: n}}, "mode"}."""
    floors = FLOORS if floors is None else floors
    platform = platform or a.get("platform") or sys.platform
    os_name = "windows" if platform == "win32" else "linux"
    seeds_a = {s["seed"]: s for s in a.get("seeds", [])}
    seeds_b = {s["seed"]: s for s in b.get("seeds", [])}
    problems, differences, declared, counts = [], [], [], {}
    every_leaf: set = set()
    for side, fp, seeds in (("A", a, seeds_a), ("B", b, seeds_b)):
        for message in fp.get("problems", []):
            problems.append("side %s: %s" % (side, message))
    if sorted(seeds_a) != sorted(seeds_b):
        problems.append("the two footprints have different seeds: A %s, B %s" % (sorted(seeds_a), sorted(seeds_b)))
    for seed in sorted(set(seeds_a) & set(seeds_b)):
        la, lb = leaves_of(seeds_a[seed]), leaves_of(seeds_b[seed])
        every_leaf |= set(la) | set(lb)
        for side, fp in (("A", seeds_a[seed]), ("B", seeds_b[seed])):
            for u in fp.get("unreadable", []):
                problems.append("seed %s side %s: the modules of a process cannot be read: %s" % (seed, side, u))
        per = {c: 0 for c in CATEGORIES}
        for leaf in set(la) | set(lb):
            per[category_of(leaf)] = per.get(category_of(leaf), 0) + 1
        counts[str(seed)] = per
        for side, leaves in (("A", la), ("B", lb)):
            have = {c: sum(1 for k in leaves if category_of(k) == c) for c in floors}
            for c, floor in floors.items():
                if have[c] < floor:
                    problems.append("seed %s side %s has %d %s leaves, fewer than the floor of %d: the tree was not seen"
                                    % (seed, side, have[c], c, floor))
        for leaf in sorted(set(la) | set(lb)):
            va, vb = la.get(leaf, MISSING), lb.get(leaf, MISSING)
            if va != vb:
                differences.append({"seed": seed, "category": category_of(leaf), "leaf": leaf, "a": _clip(va), "b": _clip(vb)})
    stale = []
    if not a_a:
        for entry in expected or []:
            if entry.get("os") != os_name:
                continue
            hit = [d for d in differences if _declared_covers(entry, d)]
            if not hit:
                stale.append(entry)
                problems.append("the declared difference %r (%s) does not show in this run: it is stale, remove it"
                                % (entry.get("id", entry.get("name")), entry.get("reason", "")))
            for d in hit:
                differences.remove(d)
                declared.append(dict(d, id=entry.get("id")))
    excused, used = [], []
    for entry in (e for e in volatile or [] if e["os"] == os_name):
        matching = sorted(leaf for leaf in every_leaf if fnmatch.fnmatchcase(leaf, entry["leaf"]))
        if not matching:
            stale.append({"id": entry["leaf"]})
            problems.append("the volatile entry %r (%s) matches no leaf of either footprint: it is stale, remove it" % (entry["leaf"], entry["aa_run"]))
            continue
        covered = [d for d in differences if fnmatch.fnmatchcase(d["leaf"], entry["leaf"])
                   and (entry.get("absent_ok") or (d["a"] != MISSING and d["b"] != MISSING))]
        for d in covered:
            differences.remove(d)
            excused.append(dict(d, aa_run=entry["aa_run"]))
        used.append({"leaf": entry["leaf"], "leaves": len(matching), "differing": len(covered)})
    for d in differences:
        problems.append("seed %s %s: A %s | B %s" % (d["seed"], d["leaf"], json.dumps(d["a"], sort_keys=True)[:160],
                                                     json.dumps(d["b"], sort_keys=True)[:160]))
    return {"format": FORMAT, "mode": "a-a" if a_a else "a-b", "verdict": "red" if problems else "green", "problems": problems,
            "differences": differences, "declared": declared, "excused": excused, "volatile": used,
            "stale": [e.get("id") for e in stale], "leaves": counts}


def print_report(report: dict, a_name: str, b_name: str) -> None:
    for seed, per in sorted(report["leaves"].items()):
        print("seed %s leaves: %s" % (seed, ", ".join("%s %d" % kv for kv in sorted(per.items()))))
    for d in report["differences"][:200]:
        print("DIFF seed %s %s: %s %s | %s %s" % (d["seed"], d["leaf"], a_name, json.dumps(d["a"], sort_keys=True)[:160],
                                                  b_name, json.dumps(d["b"], sort_keys=True)[:160]))
    for d in report["declared"]:
        print("DECLARED seed %s %s (%s)" % (d["seed"], d["leaf"], d["id"]))
    for d in report.get("excused", []):
        print("VOLATILE seed %s %s: %s %s | %s %s (run %s)" % (d["seed"], d["leaf"], a_name, json.dumps(d["a"], sort_keys=True)[:160], b_name,
                                                              json.dumps(d["b"], sort_keys=True)[:160], d["aa_run"]))
    n = len(report["differences"])
    for p in report["problems"][:len(report["problems"]) - n]:
        print("FAIL " + p)
    print("AB HOST (%s) %s: %d differences, %d problems"
          % (report["mode"], "OK" if report["verdict"] == "green" else "FAILED", n, len(report["problems"])))


# -
# One engine: the journey, the snapshot, the footprint.

def default_seeds() -> list:
    """ci/persona-seeds.json key "candidate" when it is there, else the seeds of the A/B oracle (the host gate's)."""
    try:
        document = json.loads(SEEDS_FILE.read_text(encoding="utf-8"))
        seeds = document.get("candidate") if isinstance(document, dict) else None
        if seeds:
            return [int(s) for s in seeds]
    except (OSError, ValueError):
        pass
    return list(gate.SEEDS)


def _context(binary: str, windows: bool, profile_dirs=()) -> Context:
    real = os.path.realpath(binary)
    app = {os.path.dirname(real), os.path.dirname(os.path.abspath(binary))}
    drops = {tempfile.gettempdir(), os.environ.get("RUNNER_TEMP", ""), os.path.expanduser("~"), *profile_dirs}
    if windows:
        drops |= {os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", ""), os.environ.get("TEMP", "")}
        # the engine folder may sit under the temp dir or the home: the app wins (Context checks it first)
        return Context("win32", app=app, drops=drops, sysroot=os.environ.get("SystemRoot", "C:\\Windows"))
    return Context("linux", app=app, drops=drops)


def run_seed(binary: str, seed: int, use_strace: bool, work: Path) -> dict:
    """One browser, one journey, one snapshot. Returns the seed's footprint."""
    from playwright.sync_api import sync_playwright

    from fakeweb import journey_site, profile_p4
    from invisible_playwright._juggler.remote import launch_server

    windows = sys.platform == "win32"
    seed_work = work / ("seed%d" % seed)
    seed_work.mkdir(parents=True)
    exe, log, pidfile = binary, seed_work / "strace.log", seed_work / "stand-in.pid"
    if use_strace:
        from _strace_stand_in import _stand_in

        exe = _stand_in(binary, seed_work / "engine", log, pidfile, syscalls=STRACE_CALLS)
    origin_a, origin_o, stop_site = journey_site.start((0, 0))
    holder = {"job": None}
    poller = wmi = None
    if windows:
        wmi = WmiTrace(seed_work)
        wmi.start()
        poller = Poller(lambda: _poll_pids(holder))
        poller.start()
    server, steps, footprint, short = None, [], {}, {}
    try:
        server = launch_server(headless=True, binary_path=exe, seed=seed, locale="en-US", timezone="Europe/Rome",
                               launch_timeout_ms=180_000)
        root = server.process
        holder["job"] = getattr(root, "job", None)
        with sync_playwright() as p:
            browser = p.firefox.connect(server.ws_endpoint)
            context = browser.new_context()
            context.set_default_timeout(profile_p4.TIMEOUT_MS)
            context.set_default_navigation_timeout(profile_p4.TIMEOUT_MS)
            profile_p4.install_route(context)
            page = context.new_page()
            journey = profile_p4.Journey(context, page, origin_a, origin_o)
            journey_site.STATE.begin_walk()
            for n, (name, method) in enumerate(JOURNEY, 1):
                journey.extra = {}
                journey_site.STATE.set_step(n)
                try:
                    getattr(journey, method)()
                    steps.append({"n": n, "name": name, "ok": True})
                except Exception as exc:  # noqa: BLE001 - recorded, and a step that failed is a problem of the run
                    steps.append({"n": n, "name": name, "ok": False, "error": str(exc).strip().splitlines()[0][:300] if str(exc).strip() else repr(exc)})
            # The snapshot: after the journey, once the tree is quiet, before anything is closed.
            _pids, quiet = wait_quiet(lambda: {q.pid for q, _c in engine_processes(root.pid, holder["job"])})
            ctx = _context(binary, windows)
            footprint = take_footprint(engine_processes(root.pid, holder["job"]), ctx)
            footprint["quiet"] = quiet
            if windows:
                short["job"] = accounting_of(holder["job"])
                short["polled"] = poller.stop()
                short["wmi"] = wmi.result({root.pid})
            context.close()
            browser.close()
    finally:
        stop_site()
        if poller is not None:
            poller.stop()
        if server is not None:
            try:
                server.close()
            except Exception:  # noqa: BLE001
                pass
        if use_strace:
            from _strace_stand_in import _kill_group

            _kill_group(pidfile)
    result = {"seed": seed, "steps": steps}
    result.update(footprint)
    if windows:
        result["short_lived"] = short
    if use_strace and log.exists():
        procs = spawned_processes(log.read_text(encoding="utf-8", errors="replace").splitlines(), os.path.realpath(binary))
        result["spawned"] = spawned_counts(procs)
        result["spawned_detail"] = sorted(
            ({"label": p["label"], "exec": (" ".join([os.path.basename(p["exec"][0])] + p["exec"][1][1:6]) if p["exec"] else None),
              "name": p["name"]} for p in procs.values()), key=lambda d: (d["label"], d["exec"] or ""))
    return result


def _poll_pids(holder) -> list:
    job = holder["job"]
    if job is not None and getattr(job, "handle", None):
        return job.pids()
    import psutil

    return [c.pid for c in psutil.Process().children(recursive=True)]


def run_engine(binary: str, leg: str, seeds: list, label: str, use_strace: bool) -> dict:
    """The footprint document of one engine: every seed's journey, one browser at a time."""
    work = Path(tempfile.mkdtemp(prefix="ab-host-"))
    problems, out_seeds = [], []
    try:
        for seed in seeds:
            fp = run_seed(binary, seed, use_strace, work)
            out_seeds.append(fp)
            for step in fp["steps"]:
                if not step["ok"]:
                    problems.append("seed %d: step %d (%s) failed: %s" % (seed, step["n"], step["name"], step.get("error")))
            if not fp["quiet"]["reached"]:
                print("seed %d: the tree did not settle within %d s; the last reading is the footprint" % (seed, QUIET_LIMIT_S))
            print("seed %d: %s; %d module leaves" % (seed, json.dumps(fp["kinds"], sort_keys=True),
                                                      sum(len(v) for v in fp["modules"].values())))
            sys.stdout.flush()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"format": FORMAT, "engine": label, "leg": leg, "platform": sys.platform, "strace": use_strace,
            "binary": os.path.basename(os.path.dirname(os.path.realpath(binary))), "seeds": out_seeds, "problems": problems}


def write_json(path: Path, document) -> None:
    Path(path).write_text(json.dumps(document, sort_keys=True, indent=1), encoding="utf-8")


def read_json(path: Path) -> dict:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("format") != FORMAT:
        raise SystemExit("%s is not an %s footprint" % (path, FORMAT))
    return document


# -
# The run of an engine as a subprocess (the seal a process runs under is read at import), and the orchestration.

def run_subprocess(binary: str, seal: Path | None, leg: str, out: Path, label: str, seeds: list, strace: str) -> int:
    env = dict(os.environ, INVISIBLE_CORE_AUTOFIX="off", INVISIBLE_CORE_PIN="allow")
    if seal:
        env["INVISIBLE_SEAL_FILE"] = str(seal)
    for name in ("MOZ_LOG", "MOZ_LOG_FILE"):
        env.pop(name, None)
    cmd = [sys.executable, "-u", str(Path(__file__).resolve()), "run", binary, "--leg", leg, "--out", str(out), "--label", label,
           "--seeds", ",".join(str(s) for s in seeds), "--strace", strace]
    try:
        with tempfile.TemporaryDirectory(prefix="ab-host-cache-") as cache:
            return subprocess.run(cmd, env=oracle.own_cache_env(env, cache), timeout=RUN_TIMEOUT_S).returncode
    except subprocess.TimeoutExpired:
        return 124


def ab(candidate: str | None, candidate_seal: Path | None, leg: str, out: Path, engine_tag: str, seeds: list, strace: str,
       published: tuple | None = None, expected_file: Path = EXPECTED_FILE, volatile_file: Path = VOLATILE_FILE) -> int:
    """A/A first (the published engine twice), then, with a candidate, the published engine against it. 0 green, 1 the A/B is red,
    2 the control is broken."""
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="ab-host-fetch-"))
    try:
        binary, seal = published or oracle.fetch_published(engine_tag, work)
        runs = [(engine_tag, "footprint-%s.json" % engine_tag.rsplit("-", 1)[-1], binary, seal),
                (engine_tag + " again", "footprint-%s-again.json" % engine_tag.rsplit("-", 1)[-1], binary, seal)]
        if candidate:
            runs.append((oracle.CANDIDATE, "footprint-candidate.json", candidate, candidate_seal))
        failed = []
        for label, name, exe, seal_file in runs:
            print("=== footprint with %s: %s" % (label, exe))
            sys.stdout.flush()
            rc = run_subprocess(exe, Path(seal_file) if seal_file else None, leg, out / name, label, seeds, strace)
            if rc != 0 or not (out / name).exists():
                failed.append("the run with %s ended with %s" % (label, rc))
        if failed:
            write_json(out / "diff-a-a.json", {"format": FORMAT, "mode": "a-a", "verdict": "red", "problems": failed,
                                               "differences": [], "declared": [], "stale": [], "leaves": {}})
            print("\n".join("FAIL " + f for f in failed))
            return 2
        first, again = (read_json(out / runs[0][1]), read_json(out / runs[1][1]))
        volatile = load_volatile(volatile_file)
        control = judge(first, again, a_a=True, volatile=volatile)
        write_json(out / "diff-a-a.json", control)
        print("--- A/A: %s against itself" % engine_tag)
        print_report(control, engine_tag, engine_tag + " again")
        if control["verdict"] != "green":
            print("BROKEN CONTROL: the published engine differs from itself on this machine; the A/B says nothing")
            return 2
        if not candidate:
            return 0
        expected = load_expected(expected_file)
        report = judge(first, read_json(out / runs[2][1]), expected=expected, a_a=False, volatile=volatile)
        write_json(out / "diff.json", report)
        print("--- A/B: %s against the candidate" % engine_tag)
        print_report(report, engine_tag, oracle.CANDIDATE)
        return 0 if report["verdict"] == "green" else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="one engine: the journey and the footprint JSON")
    r.add_argument("binary")
    r.add_argument("--leg", required=True)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--label", default="engine")
    r.add_argument("--seeds", default=None, help="comma separated (default: ci/persona-seeds.json 'candidate', else the host gate's)")
    r.add_argument("--strace", choices=("auto", "on", "off"), default="auto", help="Linux: trace the children (auto = on)")
    c = sub.add_parser("compare", help="two footprints: the diff and the verdict")
    c.add_argument("a", type=Path)
    c.add_argument("b", type=Path)
    c.add_argument("--out", type=Path)
    c.add_argument("--a-a", action="store_true", help="the control: no declared difference is applied; a difference is a broken control (exit 2)")
    c.add_argument("--expected", type=Path, default=EXPECTED_FILE)
    c.add_argument("--volatile", type=Path, default=VOLATILE_FILE)
    x = sub.add_parser("ab", help="A/A, then with a candidate the published engine against it")
    x.add_argument("candidate", nargs="?")
    x.add_argument("--candidate-seal", type=Path, default=os.environ.get("INVISIBLE_SEAL_FILE"))
    x.add_argument("--leg", required=True)
    x.add_argument("--out", type=Path, required=True)
    x.add_argument("--engine-tag", default=oracle.ENGINE_TAG)
    x.add_argument("--seeds", default=None)
    x.add_argument("--strace", choices=("auto", "on", "off"), default="auto")
    x.add_argument("--published-binary", type=Path, help="use this binary for the published engine instead of fetching it")
    x.add_argument("--published-seal", type=Path, help="its seal (default: the environment's)")
    args = ap.parse_args(argv)
    if args.cmd == "compare":
        a, b = read_json(args.a), read_json(args.b)
        report = judge(a, b, expected=None if args.a_a else load_expected(args.expected), a_a=args.a_a, volatile=load_volatile(args.volatile))
        if args.out:
            write_json(args.out, report)
        print_report(report, a.get("engine", "A"), b.get("engine", "B"))
        if report["verdict"] == "green":
            return 0
        return 2 if args.a_a else 1
    seeds = [int(s) for s in args.seeds.split(",")] if args.seeds else default_seeds()
    if args.cmd == "run":
        use_strace = sys.platform.startswith("linux") and args.strace != "off"
        if use_strace and not shutil.which("strace"):
            print("needs strace (apt install strace): without it the short-lived children are not seen; --strace off to go without")
            return 3
        document = run_engine(args.binary, args.leg, seeds, args.label, use_strace)
        write_json(args.out, document)
        return 0
    if args.candidate and not args.candidate_seal:
        ap.error("--candidate-seal (or INVISIBLE_SEAL_FILE) is required with a candidate: it runs under its own seal")
    given = None
    if args.published_binary:
        given = (str(args.published_binary), args.published_seal)
    return ab(args.candidate, Path(args.candidate_seal) if args.candidate_seal else None, args.leg, args.out, args.engine_tag, seeds,
              args.strace, given)


if __name__ == "__main__":
    sys.exit(main())
