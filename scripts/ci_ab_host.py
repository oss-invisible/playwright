#!/usr/bin/env python3
"""Host-footprint A/B oracle: what the engine does to the HOST, over the whole of a closed run.

scripts/ci_ab_oracle.py compares what a PAGE reads. This compares what the engine does to the host, which a page cannot read but a release can
change (a helper that is no longer spawned, a path of the machine it did not touch before). It is not a photograph of one instant: it is the
TRACE of the run, from the launch to the exit of the browser (the quiet point is the exit of the process, not a wait), after the same short
journey (the first steps of the fake web's P4 walk, tests/fakeweb/profile_p4.py, no internet, driven by the stock Playwright client).

After the four steps of the journey comes a fifth: the geolocation permission is granted to a page of the oracle's own and `getCurrentPosition` is called and awaited
(the callback or the error, an event). On Windows the candidate must start NO utility process of the kind WINDOWS_UTILS (`-sandboxingKind 3`) and load none of the modules of
the platform's location provider (an absolute rule, `geolocation_problems`); the NEGATIVE CONTROL is Playwright's own Firefox on the same runner with the same step
(`run_stock`, command `stock`), which MUST start that process: if it does not the step does not reach the provider and the leg is a broken control (exit 2).
On Linux the same step runs and its footprint is compared like the rest.

Three things are compared, over the whole trace:

  * the processes, by KIND, from the command lines of the launch: the parent, `-contentproc` with its type, `-sandboxingKind N` and
    `-isForBrowser` / `-notForBrowser`, and the helpers (glxtest, pingsender, ...) by what they ran. Every kind and its COUNT must be identical,
    with one exception: for the content processes of type `tab` only the presence of the kind counts, because how many are alive follows the idle
    time (a spare process is prelaunched after a delay and closed after a grace period);
  * the files: every path the tree touched (opened, stat'ed, accessed, read as a link, executed), classified `ours` or `host`. The SET of host
    paths must be identical, no exception. `ours` leaves the comparison as a class (OURS below: reading its own package is not touching the host);
  * the network, per kind (Linux): every NETLINK socket the tree opens, by protocol (NETLINK_ROUTE, NETLINK_KOBJECT_UEVENT, ...), with the requests
    it sends on it (RTM_GETLINK, RTM_GETADDR, ...) and the multicast groups it binds to, and every connect or send towards a PUBLIC address, with
    the transport, the family, the address and the port. The SET per kind must be identical, no exception, like the host paths. A destination that
    is loopback, the lab's (the documentation ranges of LAB_NETWORKS), private or link-local, or a multicast group is not compared: it is counted
    by class (NET_NOT_COMPARED). On Windows it is not covered (NET_WINDOWS says why).

The trace is of the TREE of the launch only: what a process outside it does (on Windows a service, the antivirus) is not the engine's, so it is left out of
the comparison and the report says how many paths each outside process name touched (`OUTSIDE THE TREE`). A path or a kind that differs names the processes
to blame (`by A: ... | B: ...`: who touched the path, who spawned the kind).

Each engine run starts with a discarded WARM-UP launch (the first seed), an experiment control and not an excuse: its trace is kept in the footprint and the
report lists what only the warm-up did (`WARMUP-ONLY`, per kind, per path and per net entry, with the process that did it), so that a first-launch-only behaviour (a
helper spawned at the first shutdown of a cold machine) stays visible from run to run. The measured launches are compared as they are: a kind or a count that differs
after the warm-up is red.

The exit pings (`firefox --backgroundtask pingsender`, through the session's route, never the separate `pingsender[.exe]`) leave only when Mozilla's `canUpload()` is
true (TelemetrySend.sys.mjs:1083), which the core's declared terms of use make true from the start: in a measured launch their
number is 0 or 3 (the three pings of a first run, spawned by the parent), anything else is red. The paths and the destinations only they touch are compared when they are
there on both sides of a launch, and left out when they are there on one side only (a destination is `public, telemetry`: the telemetry host is the third party's, and
whether the connection succeeded is not in the verdict). The IDLE launch of each engine run (a page, no pref changed, up to 120 s) is evidence: which pings leave in a long launch is not a rule until it is proven (the core declares
the terms of use accepted, so `canUpload()` is true from the start and the notification of the data policy is never recorded; Linux started 1 pingsender in it, Windows 0).
Only a separate `pingsender[.exe]` there is red. Behind a session proxy a direct pingsender is the network oracle's.

On Windows the system modules (a path under the system folder) are compared as a SET over the union of the measured seeds of a side (`SYSTEM_REASON`): a module that is in a
seed of each side is the same module; one that only one side has in any seed is still red.

How the trace is taken:

  * Linux: `strace -f -y` of the whole tree (the stand-in of tests/_strace_stand_in.py), calls execve, clone, fork, prctl, the file calls
    (open, openat, stat, access, readlink, ...) and the network calls (socket, connect, bind, sendto, sendmsg, sendmmsg). The tree is named by
    what each process ran, or called itself when it forked without exec (the forkserver's children), and the log is read after the tree has exited.
    A socket is known by the inode `-y` prints for it (`5<socket:[1234]>`), so a send names the protocol of the socket() that made it.
  * Windows: the process starts WITH their command lines come from the Security log, event 4688 (the process-creation audit with the command
    line, switched on for the run and put back after it): the kernel writes one per process, so a short-lived child cannot be missed, which a
    poll of the job's pids can. The files come from an ETW session on Microsoft-Windows-Kernel-File (the Create events: every path a process
    opens, a library included) taken with `logman` and read with `tracerpt`, both part of Windows. It needs an administrator (the hosted runners
    are). Two canary files of the oracle's own, opened after the session starts and before it stops, must be in the trace: a session that did not
    see them is red, not empty.

EVERY launch is also judged by how its parent ended (scripts/ci_crash_evidence.py): the warm-up, each measured seed, the idle and the bare launch alike. A root exit code other
than 0, or one that was not seen, is a RED line that names the seed, the run side, the launch and the code; the verdict of a closed run is not "the trace is equal" alone. A launch
that did not end with 0 keeps what it left in `<footprint>.crash/<launch>/`, next to the footprint (the artifact): the last 100 lines of the strace log and the fatal signal lines of
the whole log (`--- SIGSEGV {si_addr=...} ---`: the trace records the fatal signals now, `tests/_strace_stand_in.py` RECORDED_SIGNALS, and the lines are dropped where the log is read, so they
are never data of the comparison), the core run through `gdb -batch` (`thread apply all bt`, the MOZ_CRASH reason, `info sharedlibrary`) with the build ids, the crash reporter's `.extra` and
the minidump names, and `said.txt` for every collector that found nothing. Cores are enabled in every launch (`ulimit -c unlimited` in the stand-in; the working directory is not touched, the
job's `kernel.core_pattern` decides where the kernel writes them: INVPW_CORE_DIR), and a crash watcher reads the crash folders in every launch, as it did in the bare one. On Windows the `.extra`
files and the minidump names are collected the same way. `crash-probe` is the recorder's own test (one launch, its parent sent SIGSEGV; red is the right answer, tests/test_ab_host_crash_linux_e2e.py).

    python ci_ab_host.py run <firefox> [--seal SEAL] --leg linux-x86_64 --out footprint.json [--seeds 11,12,13]
    python ci_ab_host.py crash-probe <firefox> --leg linux-x86_64 --out DIR
    python ci_ab_host.py compare A.json B.json --out diff.json [--a-a]
    python ci_ab_host.py ab [<candidate> --candidate-seal SEAL] --leg L --out DIR [--published-binary B [--published-seal S]]

`ab` without a candidate is the A/A control (the published engine twice). With a candidate: the A/A first, then the published engine against the
candidate. Exit 0 green, 1 red, 2 the control is broken (A/A differs: what is compared is not stable, the A/B says nothing). Nothing here reads
the clock for a verdict, and nothing excuses a difference: the A/A is green or the comparison is not stable.

Declared differences (ci/ab-host-expected.json) are applied to the A/B only, never to the A/A, by kind, and one that does not show is red as stale.
"""
from __future__ import annotations

import argparse
import codecs
import contextlib
import datetime
import collections
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tests"))
import ci_ab_oracle as oracle  # noqa: E402  (fetch_published, ENGINE_TAG)
import ci_host_gate as gate  # noqa: E402  (SEEDS)
import ci_crash_evidence as crash  # noqa: E402  (the exit code is red, and what a bad end left is saved)
from ci_crash_evidence import CrashWatcher, core_info, crash_report_dirs, scan_crash_files  # noqa: E402,F401  (the bare launch's collectors, shared by every launch)

FORMAT = "ab-host/2"
EXPECTED_FORMAT = "ab-host-expected/1"
EXPECTED_FILE = ROOT / "ci" / "ab-host-expected.json"
SEEDS_FILE = ROOT / "ci" / "persona-seeds.json"
#: The steps of the journey (tests/fakeweb/profile_p4.py STEPS, by method name).
JOURNEY = (("open home", "s1_open_home"),
           ("hover and click a teaser", "s2_hover_click_teaser"),
           ("scroll to the bottom, next article", "s3_scroll_next"),
           ("nav Category", "s7_nav_category"))
#: The step that follows the journey (not a method of the fake web's journey: it has a page of the oracle's own, `start_geo_lab`): the geolocation
#: permission is granted to that page and `navigator.geolocation.getCurrentPosition` is called. It ends with the callback or the error, whichever the
#: engine gives (an event, never a clock): what the page is answered is recorded (`geo`) and never judged, the host is what is judged.
GEO_STEP = "geolocation granted, getCurrentPosition"
GEO_PATH = "/geo"
#: The position the core declares to our engine, made up (the engine's own contract test, juggler/tests/geolocation_windows_provider_never_selected.test.mjs, uses
#: the same); the stock control is given none, so that it has to ask the platform.
GEO_DECLARED = {"latitude": 45.0, "longitude": 9.0, "accuracy": 1500}
#: The page asks once and ends with a position or an error. `timeout` is the acquisition limit of the request (a stock Firefox with no answer from its
#: provider would wait for ever): it is never read by a verdict, the answer is recorded and not judged.
GEO_READ = """() => new Promise(resolve => navigator.geolocation.getCurrentPosition(
  position => resolve({answer: 'position'}),
  error => resolve({answer: 'error', code: error.code}),
  {timeout: %d, maximumAge: 0}))""" % 20000
#: The Windows location provider of Firefox runs in the utility process of the kind WINDOWS_UTILS: `-sandboxingKind 3` (the enum of
#: ipc/glue/UtilityProcessSandboxing.h: GENERIC_UTILITY 0, UTILITY_AUDIO_DECODING_WMF 1, MF_MEDIA_ENGINE_CDM 2, WINDOWS_UTILS 3, WINDOWS_FILE_DIALOG 4).
#: Our engine has no caller of it left (acceptance contract, item 4, of the engine's geolocation_windows_provider_never_selected.test.mjs).
LOCATION_UTILITY_KIND = "3"
#: The modules the provider loads (a Windows trace sees a library as a file opened): the engine's contract names the first four as ABSENT (item 3 of the
#: same test); the WinRT geolocation modules are matched by their prefix.
LOCATION_MODULES = frozenset({"geolocation.dll", "locationframeworkps.dll", "locationapi.dll", "wlanapi.dll", "capabilityaccessmanagerclient.dll"})
LOCATION_MODULE_PREFIXES = ("windows.devices.geolocation",)

#: What a seed's footprint must hold at least, per category of leaf: a footprint that holds less did not see the run.
#: Provisional: the first A/A on both systems gives the real numbers (they are in diff.json, "leaves").
#: There is no floor for `net`: a run that opens no netlink socket and sends nothing to a public address is a run that can exist.
FLOORS = {"proc": 3, "file": 100}
#: The categories of leaf a footprint has.
CATEGORIES = ("proc", "file", "net")
MISSING = "<missing>"
#: Seconds a closed run is given to exit (an acquisition limit, never read by a verdict: `trace.exited` says whether the tree had gone).
EXIT_LIMIT_S, EXIT_PERIOD_S = 120.0, 0.25
RUN_TIMEOUT_S = 30 * 60
CLIP = 400

#: What is `ours` and why it leaves the comparison as a class. The reason is written here once; the report and the footprints name the class.
OURS = {
    "package": "reading its own package is not touching the host",
    "profile": "its own profile and the temp folders of the launch are made by the launch: the run's temp root belongs to the run, not to the host",
    "cache": "the cache folder the oracle gives every run is the run's own",
    "ipc": "the shared memory and pipes it opens between its own processes are named after its own pids",
}
#: The content processes of type `tab`: `-contentproc ... tab` on Windows and, where they fork from the forkserver and name themselves
#: (prctl PR_SET_NAME, 15 characters), the names of the remote types. Only the PRESENCE of one of these kinds counts, not how many.
TAB_NAMES = frozenset({"web content", "isolated web co", "privileged cont", "webextensions", "file content", "isolated servic"})
TAB_REASON = ("the number of content processes of type tab follows the idle time (a spare is prelaunched after a delay, a process with no frame "
              "left is closed after a grace period): only the presence of the kind is compared")


# -
# Kinds of process.

#: The exit pings of the engine leave as `firefox[.exe] --backgroundtask pingsender URL path` (toolkit/components/telemetry/app/StealthPingsender.sys.mjs, B266),
#: through the session's route, never as the separate `pingsender[.exe]`, which took its road from the host. Whether they leave at all is Mozilla's
#: `canUpload()` (TelemetrySend.sys.mjs:1083: `options.usePingSender && !this._isOSShutdown && TelemetryReportingPolicy.canUpload()`), true once the data-policy
#: notification was recorded (TelemetryReportingPolicy.sys.mjs:988 `_userNotified`, from `ensureUserIsNotified` at `sessionstore-windows-restored`, :1140-1175):
#: a launch that stays short may or may not have got there, so it has 0 of them or all 3 (the first run sends new-profile, first-shutdown and event; the shutdown
#: ping waits for the second session, TelemetrySession.sys.mjs:1011).
PINGSENDER_KIND = "backgroundtask/pingsender"
PINGSENDER_EXES = ("pingsender", "pingsender.exe")
PING_TYPES = ("event", "first-shutdown", "new-profile")
PINGSENDER_COUNTS = (0, 3)
CAN_UPLOAD = "TelemetrySend.sys.mjs:1083"
#: The pref the policy writes when the notification is recorded (TelemetryUtils.sys.mjs:80, TelemetryReportingPolicy.sys.mjs:1025): the profile's prefs.js says
#: it, a file of the launch's own, so the precondition of the pings is observed without any channel into the engine.
NOTIFIED_PREF = "datareporting.policy.dataSubmissionPolicyNotifiedTime"
#: The idle launch: how long it waits for the notification to be recorded (the first-run infobar is at 60 s, NOTIFICATION_DELAY_FIRST_RUN_MSEC, :51) and how
#: often it looks. A limit of acquisition: that the pref was seen is the verdict, never how long it took.
NOTIFY_LIMIT_S, NOTIFY_PERIOD_S = 120.0, 0.5


def is_pingsender(kind: str) -> bool:
    return kind == PINGSENDER_KIND or kind in PINGSENDER_EXES


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


def is_tab(label: str) -> bool:
    """A process of type `tab`: its kind says so (`tab/isForBrowser`), or, forked from the forkserver, it named itself after a remote type."""
    low = label.lower()
    return low == "tab" or low.startswith("tab/") or low in TAB_NAMES


def _base(path: str) -> str:
    """The file name of a path of either system."""
    return re.split(r"[\\/]", path)[-1]


def label_of_exec(path: str, argv: list, real: str) -> str:
    """What a process that ran `path` with `argv` is called: the engine's kinds, `glxtest` (argv0 or `-glxtest`), else the
    program's name (lower case); a background task of the engine (`firefox.exe --backgroundtask pingsender ...`) is `backgroundtask/<name>`."""
    if "-contentproc" in argv:
        return kind_of(argv)  # whatever path it was started by (the binary, /proc/self/exe)
    for flag in ("--backgroundtask", "-backgroundtask"):
        if flag in argv:
            i = argv.index(flag)
            return "backgroundtask/%s" % (argv[i + 1].lower() if i + 1 < len(argv) else "?")
    if any(a.lstrip("-") == "glxtest" for a in argv[1:]) or _base(argv[0] if argv else "").lower() == "glxtest":
        return "glxtest"
    if path == real or _base(path).lower() == _base(real).lower():
        return "parent"
    return _base(path).lower() or path


# -
# The path of a touched file: ours, or the host's.

class Context:
    """What the classifier needs to know about the machine and the run: `platform` ("win32" or "linux"), `app` (the engine's package
    folders: the link and the real one), `temp` (the run's temp root and the work folders under it: the profile and the temp dirs of the
    launch), `cache` (the folders the oracle made the cache of the run in) and, on Windows, `devices` ({`\\device\\harddiskvolume3`: `c:`}) and
    `aliases` ([(short, long)] prefixes: the 8.3 spelling of a folder and its long one, `c:\\lab\\labrun~1` and `c:\\lab\\labrunner`: the runner's
    TEMP is spelled the short way and the engine opens the same folder under both spellings, so every path and every root is put in the long one)."""

    def __init__(self, platform: str, app=(), temp=(), cache=(), devices: dict | None = None, aliases=()) -> None:
        self.platform = platform
        self.windows = platform == "win32"
        self.aliases = [(a.replace("/", "\\").rstrip("\\").lower(), b.replace("/", "\\").rstrip("\\").lower()) for a, b in aliases if a and b]
        self.app = [self._key(p) for p in app if p]
        self.temp = [self._key(p) for p in temp if p]
        self.cache = [self._key(p) for p in cache if p]
        self.devices = {k.lower(): v.lower() for k, v in (devices or {}).items()}

    def _key(self, path: str) -> str:
        if self.windows:
            return self.long_form(path.replace("/", "\\").rstrip("\\").lower())
        return path.rstrip("/")

    def long_form(self, low: str) -> str:
        """`low` (a lower case Windows path) with the 8.3 spelling of a folder in `aliases` replaced by the long one."""
        for short, long in self.aliases:
            if low == short or low.startswith(short + "\\"):
                return long + low[len(short):]
        return low

    def under(self, path: str, root: str) -> str | None:
        """The part of `path` below `root` (with `/`), or None."""
        sep = "\\" if self.windows else "/"
        if path == root:
            return ""
        if path.startswith(root + sep):
            return path[len(root) + 1:].replace("\\", "/")
        return None

    def own(self, path: str) -> tuple | None:
        """(class, name) when `path` is under one of the run's own folders, else None."""
        for cls, roots in (("package", self.app), ("profile", self.temp), ("cache", self.cache)):
            for root in roots:
                rel = self.under(path, root)
                if rel is not None:
                    return cls, rel
        return None


_NT_VOLUME = re.compile(r"^\\device\\harddiskvolume\d+", re.I)
_NT_PIPE = re.compile(r"^\\(device\\namedpipe|\\\.\\pipe|pipe)\\", re.I)
_OWN_PIPE = re.compile(r"(gecko|mozilla|firefox|chrome\.\d)", re.I)
_PROC_NUMBER = re.compile(r"(?<=/)\d+(?=/|$)")
#: The engine writes `C:\ProgramData\Mozilla-<install id>\uninstall_ping_<install hash>_<uuid>.json` (and `.json.tmp`) once per launch: a host path whose
#: name carries the uuid of the ping. The folder and the name are compared, the random part is not.
_UNINSTALL_PING = re.compile(r"(\\uninstall_ping_[0-9a-f]+_)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(\.json(?:\.tmp)?)$")


def classify(path: str, ctx: Context) -> tuple | None:
    """`(class, name)` of one touched path: class is a key of OURS or `host`; `name` is the path as the footprint spells it (host: the same
    string for the same file on every run: the numbers of /proc/<pid> and /run/user/<uid> are N, a Windows path is lower case with its volume
    named). None when it is not a path (empty)."""
    if not path:
        return None
    return _classify_windows(path, ctx) if ctx.windows else _classify_posix(path, ctx)


def _classify_posix(path: str, ctx: Context):
    if path.startswith("/memfd:"):
        return "ipc", path
    own = ctx.own(path)
    if own:
        return own
    if path.startswith("/dev/shm/org.mozilla."):
        return "ipc", path
    if path == "/proc" or path.startswith("/proc/"):
        path = _PROC_NUMBER.sub("N", path)
    path = re.sub(r"^/run/user/\d+", "/run/user/N", path)
    return "host", path


def _classify_windows(path: str, ctx: Context):
    p = path.replace("/", "\\")
    low = p.lower()
    for prefix in ("\\??\\", "\\\\?\\"):
        if low.startswith(prefix):
            low = low[len(prefix):]
    vol = _NT_VOLUME.match(low)
    if vol and vol.group(0) in ctx.devices:
        low = ctx.devices[vol.group(0)] + low[len(vol.group(0)):]
    low = ctx.long_form(low)
    pipe = _NT_PIPE.match(low)
    if pipe:
        return ("ipc", low) if _OWN_PIPE.search(low) else ("host", low)
    own = ctx.own(low)
    if own:
        return own
    return "host", _UNINSTALL_PING.sub(r"\1<uuid>\2", low.rstrip("\\") or low)


def classify_paths(paths, ctx: Context) -> tuple:
    """`(host paths, {class: how many})` of the touched paths: the host's as a sorted list, ours counted by class."""
    host, ours = set(), collections.Counter()
    for path in paths:
        got = classify(path, ctx)
        if got is None:
            continue
        if got[0] == "host":
            host.add(got[1])
        else:
            ours[got[0]] += 1
    return sorted(host), dict(sorted(ours.items()))


# -
# Linux: the processes and the files of the tree, from the strace log.

_LINE = re.compile(r"^(\d+)\s+(\w+)\((.*)$")
_RESUMED = re.compile(r"^(\d+)\s+<\.\.\. (\w+) resumed>(.*)$")
_RESULT = re.compile(r"=\s+(-?\d+)\b[^=]*$")
_STR = re.compile(r'"((?:[^"\\]|\\.)*)"')
_EXEC = re.compile(r'"((?:[^"\\]|\\.)*)",\s*\[(.*?)\],\s*(?:0x[0-9a-fA-F]+|NULL|\[)')
_FLAGS = re.compile(r"flags=([A-Za-z0-9_|]+)")
_SET_NAME = re.compile(r'PR_SET_NAME,\s*"((?:[^"\\]|\\.)*)"')
_FIRST_STR = re.compile(r'^"((?:[^"\\]|\\.)*)"')
_DIRFD_STR = re.compile(r'^(AT_FDCWD|\d+(?:<([^>]*)>)?),\s*"((?:[^"\\]|\\.)*)"')
#: The calls that name a path as their first argument, and those that name it after a directory descriptor (`-y` spells the descriptor
#: `3</usr/share>`, so a path relative to it is known).
PATH_CALLS = ("execve", "open", "creat", "stat", "lstat", "access", "readlink", "statfs")
PATH_AT_CALLS = ("openat", "openat2", "newfstatat", "statx", "faccessat", "faccessat2", "readlinkat")
#: What strace traces here: the processes the tree makes and what they run, and every path they touch.
STRACE_CALLS = "clone,clone3,fork,vfork,prctl,memfd_create," + ",".join(PATH_CALLS + PATH_AT_CALLS)
#: A path relative to the working directory, which the strace log does not carry: the same text on every run, so the same on both sides.
CWD = "<cwd>"


def _unescape(text: str) -> str:
    return codecs.escape_decode(text.encode("latin-1", errors="replace"))[0].decode("utf-8", errors="replace")


def _path_calls(lines):
    """(pid, base, raw path) of every call of a strace log that names a path: `base` is what a relative path is relative to."""
    for pid, base, raw, _call in _named_calls(lines):
        yield pid, base, raw


def _named_calls(lines):
    """(pid, base, raw path, call name) of every call of a strace log that names a path."""
    for line in lines:
        m = _LINE.match(line)
        if not m:
            continue
        call, rest = m.group(2), m.group(3)
        if call in PATH_CALLS:
            s = _FIRST_STR.match(rest)
            base, raw = CWD, s.group(1) if s else None
        elif call in PATH_AT_CALLS:
            s = _DIRFD_STR.match(rest)
            base = (s.group(2) or "<fd>") if s and s.group(1) != "AT_FDCWD" else CWD
            raw = s.group(3) if s else None
        else:
            continue
        if raw:
            yield int(m.group(1)), base, raw, call


def _joined(base: str, raw: str) -> str:
    path = _unescape(raw)
    return path if path.startswith("/") else _unescape(base).rstrip("/") + "/" + path


def touched_paths(lines) -> set:
    """Every path a strace log (read as lines) says a process of the tree touched, failed attempts included: opening, stat, access, readlink,
    execve. A path relative to a directory descriptor is joined to the path `-y` printed for it, one relative to the working directory is
    spelled `<cwd>/...`, an empty path (the descriptor itself) is not a path."""
    return {_joined(base, raw) for _pid, base, raw in _path_calls(lines)}


def touched_by_pid(lines) -> dict:
    """{path: {pids that touched it}} of a strace log: `touched_paths` with the process (or thread) that did it."""
    out: dict = {}
    for pid, base, raw in set(_path_calls(lines)):
        out.setdefault(_joined(base, raw), set()).add(pid)
    return out


_EXIT = re.compile(r"^(\d+)\s+\+\+\+ (?:exited with (\d+)|killed by (\w+))")
_OPEN_PROC_FD = re.compile(r'^(\d+)\s+openat?\(.*"/proc/self/fd/(\d+)"')
_MEMFD = re.compile(r'^(\d+)\s+memfd_create\("[^"]*".*=\s+(\d+)')


def process_lifecycle(lines, procs: dict) -> list:
    """The order in which the processes of the tree started and ended, from a strace log read as lines (with `exit_status`): `[{"line", "event":
    "start"|"end", "kind", "how"}]`, `how` being `exited N` or `killed by SIGKILL`. Evidence of WHEN in the shutdown a process came and of whether the
    browser was ended by itself or by a kill; the position is the line of the log, never a time."""
    first, ended = {}, {}
    for i, line in enumerate(lines):
        m = _EXIT.match(line)
        if m:
            ended[int(m.group(1))] = (i, "exited %s" % m.group(2) if m.group(2) is not None else "killed by %s" % m.group(3))
            continue
        m = _LINE.match(line) or _RESUMED.match(line)
        if m:
            first.setdefault(int(m.group(1)), i)
    events = []
    for pid, p in procs.items():
        if pid in first:
            events.append((first[pid], 0, p["label"], ""))
        if pid in ended:
            events.append((ended[pid][0], 1, p["label"], ended[pid][1]))
    return [{"line": i, "event": "end" if e else "start", "kind": k, "how": h} for i, e, k, h in sorted(events)]


def ping_type(argv) -> str | None:
    """The type of the ping a pingsender is given: the url is `.../submit/telemetry/<id>/<type>/Firefox/<version>/...`."""
    for a in argv:
        m = re.search(r"/submit/telemetry/[^/]+/([^/]+)/", str(a))
        if m:
            return m.group(1)
    return None


def ping_records(procs: dict, labels: dict, parents: dict, windows: bool) -> list:
    """`[{"type", "form", "parent"}]` of the pingsender processes of a tree: `form` is `backgroundtask` for `firefox --backgroundtask pingsender`, else the
    name of the program that ran; `parent` the kind of the process that spawned it."""
    out = []
    for pid, p in procs.items():
        if not is_pingsender(labels.get(pid, p.get("label", ""))):
            continue
        argv = split_command_line(p["cmd"]) if windows else (p["exec"][1] if p.get("exec") else [])
        out.append({"type": ping_type(argv), "form": "backgroundtask" if labels.get(pid) == PINGSENDER_KIND else labels.get(pid),
                    "parent": labels.get(parents.get(pid), "(oracle)")})
    return sorted(out, key=lambda r: (str(r["type"]), r["parent"]))


def pingsender_problems(fp: dict) -> list:
    """What is wrong with the exit pings of one launch (a short measured launch, or the idle one asking `strict`): the separate executable ran, a number of
    pingsenders that is not 0 or 3, three that are not the three pings of a first run or not spawned by the parent."""
    kinds, out = fp.get("kinds", {}), []
    for exe in PINGSENDER_EXES:
        if kinds.get(exe):
            out.append("the separate %s ran (B266: the pings leave as `firefox --backgroundtask pingsender`, through the session's route)" % exe)
    n = kinds.get(PINGSENDER_KIND, 0)
    if n not in PINGSENDER_COUNTS:
        out.append("%d pingsender processes, not 0 or 3 (they leave only when canUpload is true, %s)" % (n, CAN_UPLOAD))
    if n == 3:
        records = [r for r in fp.get("pingsenders", []) if r["form"] == "backgroundtask"]
        if sorted(str(r["type"]) for r in records) != list(PING_TYPES):
            out.append("the three pingsenders are not the pings of a first run %s: %s" % (list(PING_TYPES), sorted(str(r["type"]) for r in records)))
        if sorted({r["parent"] for r in records}) != ["parent"]:
            out.append("the pingsenders were not all spawned by the parent: %s" % sorted({r["parent"] for r in records}))
    return out


def privileged_problems(fp: dict) -> list:
    """More than one preloaded about:newtab process in a launch (PRIVILEGED_REASON)."""
    n = fp.get("kinds", {}).get(PRIVILEGED, 0)
    return ["%d %s processes: the preloaded about:newtab is absent or present, never more than one (%s)" % (n, PRIVILEGED, PRIVILEGED_REASON)] if n > 1 else []


def pingsender_surface(fp: dict) -> tuple:
    """`(paths only the pingsender kind touched, its destinations)` of a launch: a destination is `<kind>: connect public, telemetry` (or `send`), whatever the
    address and the port: the telemetry host is the third party's."""
    touched = fp.get("touched_by", {})
    files = sorted(p for p in fp.get("files", []) if touched.get(p) and all(is_pingsender_part(k) for k in touched[p]))
    nets = sorted({"%s: %s public, telemetry" % (PINGSENDER_KIND, e.split(": ", 1)[1].split(" ", 1)[0])
                   for e in fp.get("net") or [] if e.startswith(PINGSENDER_KIND + ": ")})
    return files, nets


def _bare_summary(bare: dict | None) -> dict | None:
    """What the bare launch (never compared) says, for the report: how its parent ended, the crash reporter processes, the dump files, the cores."""
    if not bare:
        return None
    crash = bare.get("crash") or {}
    core = crash.get("core") or {}
    return {"notified": (bare.get("notified") or {}).get("observed"), "parent_exits": crash.get("parent_exits"), "root_returncode": crash.get("root_returncode"),
            "crash_processes": [(p["kind"], p["argv"]) for p in crash.get("crash_processes", [])], "dump_files": [f["name"] for f in crash.get("files", [])],
            "cores": core.get("cores"), "core_pattern": core.get("core_pattern")}


#: THE SYSTEM MODULES OF WINDOWS: the paths under System32, SysWOW64 and WinSxS (the security catalogs are under System32 catroot) that are not in the package. They are
#: loaded by the Windows loader on behalf of system DLLs (a catalog lookup when a library is loaded, a library a system DLL loads lazily, the resource file of a module), and no
#: page reads them: which of them a launch opens follows the system and its timing, not the engine. They are compared as a set per PATH over the union of the launches of a
#: run (the measured seeds and the warm-up, and as a layer of its own the long idle and bare launches), never seed by seed:
#:   * in the A/B, red only a module the CANDIDATE opened that NO launch of the 68 opened, in either run of the 68 (the published engine twice, warm-ups included); a module
#:     the candidate lacks is not red (printed as `lacking`);
#:   * in the A/A, a module of this class that only one run has is `rare`: printed with its count, and the control is red only if the rare ones of a run are more than RARE_LIMIT
#:     paths in its measured launches, or RARE_LIMIT_LONG in its long ones (the class is growing and must be looked at).
#: Everything outside these folders stays strict, and the shortcut inventory is its own rule (absent or complete).
SYSTEM_FOLDERS = tuple("c:" + chr(92) + "windows" + chr(92) + name + chr(92) for name in ("system32", "syswow64", "winsxs"))
SYSTEM_PREFIX = tuple("file." + f for f in SYSTEM_FOLDERS)
RARE_LIMIT = 5
SYSTEM_REASON = ("loaded by the Windows loader on behalf of system DLLs, and no page reads them: compared as a set per path over the union of all the launches of a run; in the "
                 "A/B red only a module the candidate opened that no launch of the 68 opened; in the A/A the ones only one run has are rare, red above %d" % RARE_LIMIT)


LAYERS = {"measured": ("seeds", "warmup"), "long": ("idle", "bare")}
RARE_LIMIT_LONG = 30


def _launches(fp: dict, layer: str) -> list:
    out = []
    for key in LAYERS[layer]:
        got = fp.get(key)
        out += got if key == "seeds" else [got or {}]
    return out


def system_paths(fp: dict, layers=("measured", "long"), with_inventory: bool = True) -> set:
    """The system modules (SYSTEM_FOLDERS) the launches of the given layers of a footprint opened. Two layers: `measured` (the seeds and the warm-up) and `long` (the idle
    and the bare launch, 120 s, in which the parent runs Mozilla's idle tasks: the maintenance at 57 s, the shell code). Without `with_inventory` the ones a launch opened
    as part of the shortcut inventory (its own rule: absent or complete, `INVENTORY_MARKER`) are left out."""
    out = set()
    for layer in layers:
        for launch in _launches(fp, layer):
            skip = set() if with_inventory else set(launch.get("inventory") or [])
            out.update(p for p in launch.get("files", []) if p.startswith(SYSTEM_FOLDERS) and p not in skip)
    return out


BS = chr(92)
CATROOT = BS + "catroot" + BS


def is_catalog(path: str) -> bool:
    """A security catalog under `catroot`: Windows' signature verification reads it when a library is loaded, the engine does not choose it."""
    return CATROOT in path and path.endswith(".cat")


def mui_dll(path: str) -> str | None:
    """The library a resource file belongs to (`...\\system32\\en-us\\x.dll.mui` is `...\\system32\\x.dll`), None for a path that is not one."""
    if not path.endswith(".mui"):
        return None
    folder, _sep, name = path.rpartition(BS)
    parent, _sep2, language = folder.rpartition(BS)
    return parent + BS + name[:-4] if re.fullmatch(r"[a-z]{2,3}(-[a-z0-9]{2,8})*", language) and parent else None


def classify_rare(paths, seen: set) -> dict:
    """Splits the modules of a layer that only one run has: `counted` (what the cap counts: every module that is not excused, orphan resource files included), `mui` (a
    resource file whose library is among the modules `seen` in that run: the library carries the count), `cat` (a security catalog under catroot) and `orphans` (the
    resource files whose library was never seen: they are in `counted`)."""
    out = {"counted": [], "mui": [], "cat": [], "orphans": []}
    for path in sorted(paths):
        dll = mui_dll(path)
        if is_catalog(path):
            out["cat"].append(path)
        elif dll is not None and dll in seen:
            out["mui"].append(path)
        else:
            out["counted"].append(path)
            if dll is not None:
                out["orphans"].append(path)
    return out


def system_class(a: dict, b: dict, a_a: bool, also_68=()) -> tuple:
    """`(differences, problems, report)` of the system modules of two footprints: see SYSTEM_REASON. `also_68` are the other footprints of the 68 in an A/B.

    A/A: a module only one run has is rare, counted in two layers with their own caps: the measured launches (RARE_LIMIT) and the long ones (RARE_LIMIT_LONG, what only a
    long launch of the run opened). A module the other run has in ANY launch is not rare. The cap counts every rare module but two kinds that are not the engine's choice,
    both listed and counted in the report, never silent (`classify_rare`): a resource file (`.mui`) whose library is among the modules seen in that run, since the library
    carries the count, and a security catalog under `catroot`. An orphan `.mui` (its library never seen) counts. A/B: red only a module any launch of the candidate opened
    that no launch of either 68 run opened, with the same two kinds left out of the red (a new library shows through the library itself); the ones each side opened are
    listed per run."""
    differences, problems = [], []
    if a_a:
        rare, excused = {"measured": {}, "long": {}}, {"measured": {}, "long": {}}
        for side, x, y in (("A", a, b), ("B", b, a)):
            other = system_paths(y, with_inventory=False)
            seen = system_paths(x)
            measured = system_paths(x, ("measured",), False)
            for layer, paths in (("measured", measured - other), ("long", system_paths(x, ("long",), False) - measured - other)):
                split = classify_rare(paths, seen)
                rare[layer][side], excused[layer][side] = split["counted"], split
        for layer, limit in (("measured", RARE_LIMIT), ("long", RARE_LIMIT_LONG)):
            for side in ("A", "B"):
                if len(rare[layer][side]) > limit:
                    problems.append("the system modules only run %s has in its %s launches are %d, more than %d: the class is growing (%s)"
                                    % (side, layer, len(rare[layer][side]), limit, rare[layer][side][:3]))
        counts = {layer: {side: {"counted": len(excused[layer][side]["counted"]), "mui_excused": len(excused[layer][side]["mui"]),
                                 "cat_excused": len(excused[layer][side]["cat"]), "orphans": len(excused[layer][side]["orphans"])}
                          for side in ("A", "B")} for layer in rare}
        return differences, problems, {"mode": "a-a", "rare": rare, "excused": excused, "counts": counts, "limits": {"measured": RARE_LIMIT, "long": RARE_LIMIT_LONG},
                                       "reason": SYSTEM_REASON}
    known = set(system_paths(a, with_inventory=False)).union(*[system_paths(x, with_inventory=False) for x in also_68])
    mine = system_paths(b, with_inventory=False)
    split = classify_rare(mine - known, system_paths(b))
    for path in split["counted"]:
        differences.append({"seed": "all", "category": "file", "leaf": "file." + path, "a": MISSING, "b": 1, "by": {"A": [], "B": []}, "calls": {"A": [], "B": []}})
    listed = {"68": [p for p in sorted(known) if mui_dll(p) or is_catalog(p)], "candidate": [p for p in sorted(mine) if mui_dll(p) or is_catalog(p)]}
    return differences, problems, {"mode": "a-b", "candidate_only": split["counted"], "excused_candidate_only": {"mui": split["mui"], "cat": split["cat"]}, "orphans": split["orphans"],
                                   "listed_mui_and_cat": listed, "lacking": sorted(known - mine)[:50], "lacking_count": len(known - mine), "reason": SYSTEM_REASON}


PRIVILEGED = "Privileged Cont"
PRIVILEGED_REASON = ("the preloaded about:newtab (remote type privilegedabout, NewTabPagePreloading.sys.mjs:96 `maybeCreatePreloadedBrowser`, browser.newtab.preload true, "
                     "firefox.js:1952) is created once the window is idle: whether a launch has it depends on the idle time after startup, so per launch it is absent or present, "
                     "and more than one is red")


def _idle_summary(idle: dict | None) -> dict | None:
    """What the idle launch of one side shows, for the report: the notification, the pingsenders (count, types, parents), ."""
    if not idle:
        return None
    records = idle.get("pingsenders", [])
    return {"notified": (idle.get("idle") or {}).get("notified"), "pingsenders": idle.get("kinds", {}).get(PINGSENDER_KIND, 0),
            "types": sorted(str(r["type"]) for r in records), "parents": sorted({r["parent"] for r in records})}


def idle_problems(a: dict, b: dict) -> list:
    """What is held of the IDLE launch: that each side has one, and that no pingsender in it is the separate executable. NOTHING about how many pingsenders it
    started or whether the notification of the data policy was recorded: the core declares the terms of use accepted (invisible_core/prefs.py `termsofuse.accepted*`,
    `browser.preonboarding.enabled`), so `canUpload()` is true from the start (TelemetryReportingPolicy.sys.mjs:839, `_delayedSetup` :1156 returns before `_userNotified`
    :988) and the notification is never recorded; which pings leave in a long launch is evidence (run 44993377458: Linux started 1, new-profile; Windows 0), not a rule
    until it is proven."""
    ia, ib = a.get("idle"), b.get("idle")
    if not ia and not ib:
        return []
    out = []
    for side, idle in (("A", ia), ("B", ib)):
        if not idle:
            out.append("side %s has no idle launch" % side)
            continue
        out += ["idle launch of side %s: %s" % (side, p) for p in pingsender_problems(idle) if p.startswith("the separate ")]
    return out


def windows_lifecycle(tree, exits, procs: dict) -> list:
    """The order in which the processes of the tree started (the 4688 events) and ended (the Kernel-Process stops, with the exit code), by the time each event
    carries (ISO text, so the order is the text's): `[{"time", "event", "kind", "how"}]`. Evidence of when in the shutdown a process came and of whether it
    ended by itself (`exited 0`) or was ended (`exited 1`, the core's TerminateProcess)."""
    events = [(e.get("time", ""), 0, procs[e["pid"]]["label"], "") for e in tree if e["pid"] in procs]
    events += [(x["time"], 1, procs[x["pid"]]["label"], "exited %d" % x["code"]) for x in exits if x["pid"] in procs]
    return [{"time": t, "event": "end" if e else "start", "kind": k, "how": h} for t, e, k, h in sorted(events)]


def proc_fd_opens(lines, owners: dict, label_of) -> list:
    """Every open of `/proc/self/fd/<n>` of a strace log, with the process that made it, the line, and the `memfd_create` that made the descriptor when
    the same process did (Firefox opens it to make a read-only copy of a frozen shared memory: ipc/glue/SharedMemoryPlatform_posix.cpp `DupReadOnly`)."""
    made, out = {}, []
    for line in lines:
        m = _MEMFD.match(line)
        if m:
            made[(owner_of(int(m.group(1)), owners), int(m.group(2)))] = line[:200]
            continue
        m = _OPEN_PROC_FD.match(line)
        if m:
            pid, fd = int(m.group(1)), int(m.group(2))
            out.append({"pid": pid, "kind": label_of(pid), "line": line[:300], "made_by": made.get((owner_of(pid, owners), fd))})
    return out[:20]


def touched_calls(lines) -> dict:
    """{path: {names of the calls that touched it}} of a strace log (open, openat, stat, readlink, ...): which call, the evidence of a path that differs."""
    out: dict = {}
    for _pid, base, raw, call in set(_named_calls(lines)):
        out.setdefault(_joined(base, raw), set()).add(call)
    return out


def thread_owners(lines) -> dict:
    """{tid: pid of the process it belongs to} from the clones with CLONE_THREAD of a strace log (a thread's calls are logged under its tid)."""
    owners: dict = {}
    pending: dict = {}
    for line in lines:
        m = _LINE.match(line)
        if m:
            pid, call, rest = int(m.group(1)), m.group(2), m.group(3)
            if call in ("clone", "clone3"):
                flags = _FLAGS.search(rest)
                is_thread = bool(flags and "CLONE_THREAD" in flags.group(1)) or ("CLONE_THREAD" in rest)
                if "<unfinished ...>" in rest:
                    pending[pid] = is_thread
                else:
                    r = _RESULT.search(rest)
                    if is_thread and r and int(r.group(1)) > 0:
                        owners[int(r.group(1))] = pid
            continue
        m = _RESUMED.match(line)
        if m and m.group(2) in ("clone", "clone3"):
            pid = int(m.group(1))
            r = _RESULT.search(m.group(3))
            if pending.pop(pid, False) and r and int(r.group(1)) > 0:
                owners[int(r.group(1))] = pid
    return owners


def owner_of(pid: int, owners: dict) -> int:
    """The process a tid belongs to (itself when it is not a known thread)."""
    seen = set()
    while pid in owners and pid not in seen:
        seen.add(pid)
        pid = owners[pid]
    return pid


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


def derive_labels(labels: dict, parents: dict) -> dict:
    """`labels` with the processes a pingsender spawned named `<kind><-pingsender`: each background task is a Firefox of its own and starts its own socket process
    (run 44988820319: four socket processes, the parent's and one of each of the three pingsenders, all ended with exit 0), so they come and go with the pingsenders."""
    out: dict = {}

    def label(pid, seen=()):
        if pid in out:
            return out[pid]
        base, parent = labels[pid], parents.get(pid)
        if parent in labels and pid not in seen and parent not in seen:
            above = label(parent, seen + (pid,))
            if above == PINGSENDER_KIND or above.endswith("<-pingsender"):
                base = base + "<-pingsender"
        out[pid] = base
        return base

    for pid in labels:
        label(pid)
    return out


def is_pingsender_part(kind: str) -> bool:
    """The pingsender kind and what it spawned: what is there when the pingsenders are."""
    return kind == PINGSENDER_KIND or kind.endswith("<-pingsender")


#: The shortcut inventory of Windows (a launch that ran it touches these): StartupOSIntegration.sys.mjs `onStartupIdle` (:165) -> `ensurePrivateBrowsingShortcutExists`
#: (:238, `hasPinnableShortcut` :266 -> nsWindowsShellService.cpp `HasPinnableShortcut` :1672 -> `FindPinnableShortcut` :1625) and `CustomIconManager.ensureAppliedOrRevert` ->
#: `enumerateInstallShortcuts` (CustomIconManager.sys.mjs:189 -> nsWindowsShellService.cpp `EnumerateInstallShortcutsImpl` :2960): Mozilla code at startup idle, which a short
#: launch may or may not have reached before it closed. The marker is a path of the places it walks.
INVENTORY_MARKER = re.compile(r"(\\start menu\\|\\users\\public\\desktop|\\quick launch\\|_shortcuts\.ini$|\\implicitappshortcuts)")


def creates_of(dispositions: dict, label_of, ctx: Context) -> list:
    """`[{"path", "kinds", "how"}]` of the host paths a process of the tree asked the system to MAKE (create, open-or-create, overwrite, supersede: the Create
    event's disposition, `DISPOSITIONS`), not only to open. Evidence of a write to the host; an open-if of a file that was there is in it too."""
    found: dict = {}
    for (pid, raw), values in dispositions.items():
        got = classify(raw, ctx)
        if got and got[0] == "host" and any(v in MAKING for v in values):
            entry = found.setdefault(got[1], {"kinds": set(), "how": set()})
            entry["kinds"].add(label_of(pid))
            entry["how"].update(DISPOSITIONS.get(v, str(v)) for v in values if v in MAKING)
    return [{"path": p, "kinds": sorted(v["kinds"]), "how": sorted(v["how"])} for p, v in sorted(found.items())]


def inventory_of(order: dict, label_of, ctx: Context) -> list:
    """The host paths of the shortcut inventory of a launch: the paths the PARENT first touched from the first to the last open of a path of the places the
    inventory walks, by the order of the system's log (a shell library loaded for it is in it). `[]` when the launch did not run it."""
    mine = {}
    for (pid, raw), position in order.items():
        if label_of(pid) == "parent":
            got = classify(raw, ctx)
            if got and got[0] == "host":
                mine[got[1]] = min(position, mine.get(got[1], position))
    marks = [pos for path, pos in mine.items() if INVENTORY_MARKER.search(path)]
    if not marks:
        return []
    low, high = min(marks), max(marks)
    return sorted(path for path, pos in mine.items() if low <= pos <= high)


def by_kind(pairs, label_of, ctx: Context) -> dict:
    """{host path: sorted kinds of the processes that touched it} of `(pid, raw path)` pairs, `label_of(pid)` naming the process. Evidence: the cause
    of a path that differs, or that is only in the warm-up."""
    out: dict = {}
    for pid, raw in pairs:
        got = classify(raw, ctx)
        if got and got[0] == "host":
            out.setdefault(got[1], set()).add(label_of(pid))
    return {path: sorted(kinds) for path, kinds in sorted(out.items())}


def spawners(labels: dict, parents: dict) -> dict:
    """{kind: sorted kinds of the processes that spawned it} from {pid: kind} and {pid: parent pid}: the process that is to blame for a kind."""
    out: dict = {}
    for pid, label in labels.items():
        out.setdefault(label, set()).add(labels.get(parents.get(pid), "(oracle)"))
    return {kind: sorted(v) for kind, v in sorted(out.items())}


def spawned_counts(procs: dict) -> dict:
    """{label: how many processes} of `spawned_processes`."""
    return dict(sorted(collections.Counter(p["label"] for p in procs.values()).items()))


# -
# Linux: the network of the tree. A page cannot read it, a release can change it: a netlink socket it did not open before (the machine's interfaces and
# addresses asked of the kernel), a request it did not send, a public address it did not reach.

#: The calls of the network the trace adds (tests/_strace_stand_in.py traces them when it is asked `network=True`).
NET_CALLS = ("socket", "connect", "bind", "sendto", "sendmsg", "sendmmsg")
#: The documentation ranges (RFC 5737) the lab gives its fake servers and exits: 192.0.2.0/24 (the DoH bootstrap of tests/_doh_servers.py),
#: 198.51.100.0/24 (the page work of tests/_page_work.py, STUN, TURN and the exits of tests/_socks_servers.py and scripts/ci_ab_network.py, the
#: lab exit on `lo`) and 203.0.113.0/24 (the exits and the web address of tests/_socks_servers.py).
LAB_NETWORKS = tuple(ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))
#: The destinations that are not compared, by class, and why. They are counted per seed (`net_not_compared`), never excused one by one.
NET_NOT_COMPARED = {
    "loopback": "the engine's own processes, the oracle and the fake web talk to each other on loopback: it is the run's, not the host's network",
    "lab": "the documentation ranges the lab gives its fake servers and exits (LAB_NETWORKS) are the lab's, not a destination on the internet",
    "private": "RFC 1918, link-local, shared (100.64/10), unique local and the other ranges that are not global are the machine's own network",
    "multicast": "a multicast group (mDNS, SSDP) is the local link's, not a public destination",
}
NET_WINDOWS = (
    "not covered on Windows. The equivalent of a connect or a send is the Microsoft-Windows-Kernel-Network provider (TCP connect, events 12 and 28; "
    "UDP send, 42 and 58; the process in the event's PID field, not in its header): it is not in the session, because logman takes a second "
    "provider only through a providers file, the provider logs every packet the whole machine sends and receives (the runner's own traffic and "
    "the fake web's loopback included), and no trace of a hosted runner has shown yet how tracerpt names its fields, so a reader written now "
    "would need a canary of its own (a loopback datagram of the oracle's) not to be blind. The equivalent of netlink is not a socket: "
    "GetAdaptersAddresses and NotifyIpInterfaceChange go through nsi.dll to the NSI driver by IOCTL, which no Kernel-File Create event carries "
    "(the open of the device is a Create, in the file set).")
_SOCK_INODE = re.compile(r"<socket:\[(\d+)\]>")
_FD_ARG = re.compile(r"^-?\d+(?:<socket:\[(\d+)\]>)?")
_INET4 = re.compile(r'sa_family=AF_INET, sin_port=htons\((\d+)\), sin_addr=inet_addr\("([^"]+)"\)')
_INET6 = re.compile(r'sa_family=AF_INET6, sin6_port=htons\((\d+)\),[^}]*?inet_pton\(AF_INET6, "([^"]+)"')
_NL_ADDR = re.compile(r"sa_family=AF_NETLINK, nl_pid=-?\d+, nl_groups=([0-9a-fA-Fx]+)")
#: The header of a netlink message as strace decodes it: `{nlmsg_len=20, nlmsg_type=RTM_GETADDR, ...` (and `{len=20, type=RTM_GETADDR, ...` of the
#: older builds); an unknown type is a number (`nlmsg_type=0x1d /* RTM_??? */`).
_NL_HEADER = re.compile(r"\{(?:nlmsg_len|len)=\d+, (?:nlmsg_type|type)=([A-Za-z0-9_]+)")
#: The requests of NETLINK_ROUTE (linux/rtnetlink.h), for a message strace printed as bytes (a socket it could not name).
ROUTE_TYPES = {16: "RTM_NEWLINK", 18: "RTM_GETLINK", 20: "RTM_NEWADDR", 22: "RTM_GETADDR", 24: "RTM_NEWROUTE", 26: "RTM_GETROUTE",
               28: "RTM_NEWNEIGH", 30: "RTM_GETNEIGH", 34: "RTM_GETRULE", 38: "RTM_GETQDISC", 74: "RTM_GETNETCONF"}
_TRANSPORT = {"SOCK_STREAM": "tcp", "SOCK_DGRAM": "udp", "SOCK_RAW": "raw", "SOCK_SEQPACKET": "seqpacket"}


def destination_class(address: str) -> str:
    """`public`, or the class of NET_NOT_COMPARED of an address (an IPv4 address mapped in IPv6 is its IPv4). An address that cannot be read is
    `public`: it is compared, not dropped."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return "public"
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return "loopback"
    if any(ip in net for net in LAB_NETWORKS):
        return "lab"
    if ip.is_multicast:
        return "multicast"
    if not ip.is_global:
        return "private"
    return "public"


def _socket_args(rest: str) -> list:
    """The three arguments of a socket() line: `AF_NETLINK, SOCK_RAW|SOCK_CLOEXEC, NETLINK_ROUTE) = 5<socket:[1]>`."""
    text = rest.split(" <unfinished ...>", 1)[0]
    text = text.rsplit(") =", 1)[0] if ") =" in text else text.rstrip(")")
    args = [a.strip() for a in text.split(",", 2)]
    return args + ["?"] * (3 - len(args))


def _socket_of(args: list) -> dict:
    """{"family", "proto"} of the arguments of a socket(): the protocol of a netlink socket (`NETLINK_ROUTE`, or its number), the transport of an
    internet socket (`tcp`, `udp`, `icmp`, ...)."""
    family, kind, proto = args[0], args[1].split("|", 1)[0], args[2].split(" ", 1)[0]
    if family == "AF_NETLINK":
        return {"family": family, "proto": proto}
    if proto.startswith("IPPROTO_") and proto != "IPPROTO_IP":
        return {"family": family, "proto": proto[len("IPPROTO_"):].lower()}
    return {"family": family, "proto": _TRANSPORT.get(kind, kind.lower())}


def _raw_netlink_types(rest: str, proto: str) -> list:
    """The type of a netlink message strace printed as bytes (the first string of the call): the u16 after the length, named for NETLINK_ROUTE."""
    s = _STR.search(rest)
    if not s:
        return []
    data = codecs.escape_decode(s.group(1).encode("latin-1", errors="replace"))[0]
    if len(data) < 6:
        return []
    n = int.from_bytes(data[4:6], "little")
    return [(ROUTE_TYPES.get(n) if proto == "NETLINK_ROUTE" else None) or "type=%d" % n]


def network_events(lines) -> tuple:
    """`([(pid, what)], {class: n})` of a strace log read as lines: `what` is `netlink NETLINK_ROUTE` (a socket opened, failed attempts included),
    `netlink NETLINK_ROUTE RTM_GETADDR` (a request sent on it), `netlink NETLINK_ROUTE bind nl_groups=00000111` (a subscription to its groups),
    `connect tcp AF_INET 1.0.0.1:443` or `send udp AF_INET6 [2001:4860::8888]:53` (a destination that is public); the destinations that are
    not public are counted by class. A socket is known by the inode `-y` printed for it, so a send names the protocol of the socket() that made it,
    whichever thread or process sends."""
    socks: dict = {}
    pending: dict = {}
    out, skipped = [], collections.Counter()

    def opened(pid, args, result):
        sock = _socket_of(args)
        if sock["family"] == "AF_NETLINK":
            out.append((pid, "netlink %s" % sock["proto"]))
        inode = _SOCK_INODE.search(result)
        if inode:
            socks[inode.group(1)] = sock

    for line in lines:
        m = _LINE.match(line)
        if not m:
            r = _RESUMED.match(line)
            if r and r.group(2) == "socket" and int(r.group(1)) in pending:
                opened(int(r.group(1)), pending.pop(int(r.group(1))), r.group(3))
            continue
        pid, call, rest = int(m.group(1)), m.group(2), m.group(3)
        if call == "socket":
            if "<unfinished ...>" in rest:
                pending[pid] = _socket_args(rest)
            else:
                opened(pid, _socket_args(rest), rest.rsplit(") =", 1)[-1])
            continue
        if call not in NET_CALLS:
            continue
        fd = _FD_ARG.match(rest)
        sock = socks.get(fd.group(1)) if fd and fd.group(1) else None
        headers = _NL_HEADER.findall(rest)
        if (sock and sock["family"] == "AF_NETLINK") or "sa_family=AF_NETLINK" in rest or headers:
            proto = sock["proto"] if sock and sock["family"] == "AF_NETLINK" else "NETLINK_?"
            if call == "bind":
                groups = _NL_ADDR.search(rest)
                if groups and int(groups.group(1), 16):
                    out.append((pid, "netlink %s bind nl_groups=%s" % (proto, groups.group(1))))
            elif call.startswith("send"):
                for t in headers or _raw_netlink_types(rest, proto):
                    out.append((pid, "netlink %s %s" % (proto, t)))
            continue
        if call == "bind":
            continue  # a local address, not a destination
        verb, transport = ("connect" if call == "connect" else "send"), (sock["proto"] if sock else "?")
        found = [("AF_INET", port, addr, addr) for port, addr in _INET4.findall(rest)]
        found += [("AF_INET6", port, addr, "[%s]" % addr) for port, addr in _INET6.findall(rest)]
        for family, port, addr, shown in found:
            cls = destination_class(addr)
            if cls == "public":
                out.append((pid, "%s %s %s %s:%s" % (verb, transport, family, shown, port)))
            else:
                skipped[cls] += 1
    return out, dict(sorted(skipped.items()))


def net_by_kind(events, label_of) -> list:
    """The sorted set `<kind>: <what>` of `network_events`, `label_of(pid)` naming the process (a thread is its process's)."""
    return sorted({"%s: %s" % (label_of(pid), what) for pid, what in events})


def _read_lines(log: Path):
    """The lines of a strace log that are calls and exits. The signal lines (`1234 --- SIGSEGV {si_addr=...} ---`, recorded so that a crash can be told, tests/_strace_stand_in.py
    RECORDED_SIGNALS) are evidence of a bad end and never data of the comparison: they are dropped here, the one place the log is read for it."""
    with open(log, encoding="utf-8", errors="replace") as f:
        for line in f:
            if crash.SIGNAL_LINE.match(line):
                continue
            yield line.rstrip("\n")


# -
# Windows: the processes with their command lines (the audit, event 4688) and the files (an ETW session on Kernel-File).

AUDIT_GUID = "{0CCE922B-69AE-11D9-BED3-505054503030}"  # the subcategory "Process Creation"
AUDIT_KEY = r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System\Audit"
AUDIT_VALUE = "ProcessCreationIncludeCmdLine_Enabled"
FILE_PROVIDER, FILE_CREATE_KEYWORD = "Microsoft-Windows-Kernel-File", "0x80"  # KERNEL_FILE_KEYWORD_CREATE
PROCESS_PROVIDER, PROCESS_KEYWORD = "Microsoft-Windows-Kernel-Process", "0x10"  # WINEVENT_KEYWORD_PROCESS: starts and stops, with the exit code


class TraceError(RuntimeError):
    """The trace of a Windows run could not be taken (not an administrator, no audit, no session): said as such, never an empty trace."""


def split_command_line(text: str) -> list:
    """The arguments of a Windows command line, by the rules of CommandLineToArgvW (quotes, and backslashes before a quote)."""
    args, cur, quoted, started, i, n = [], [], False, False, 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            j = i
            while j < n and text[j] == "\\":
                j += 1
            slashes = j - i
            if j < n and text[j] == '"':
                cur.append("\\" * (slashes // 2))
                started = True
                if slashes % 2:
                    cur.append('"')
                    j += 1
                i = j
                continue
            cur.append("\\" * slashes)
            started, i = True, j
            continue
        if c == '"':
            quoted, started, i = not quoted, True, i + 1
            continue
        if c in " \t" and not quoted:
            if started:
                args.append("".join(cur))
                cur, started = [], False
            i += 1
            continue
        cur.append(c)
        started, i = True, i + 1
    if started:
        args.append("".join(cur))
    return args


def _number(text: str) -> int:
    text = (text or "").strip()
    return int(text, 16) if text.lower().startswith("0x") else int(text or 0)


def parse_audit_events(xml_text: str) -> list:
    """The process creations of what `wevtutil qe Security /f:xml` printed (event 4688, one `<Event>` after the other):
    `[{"rid", "pid", "ppid", "image", "cmd"}]`, `ppid` being the creator's pid."""
    body = re.sub(r"<\?xml[^>]*\?>", "", xml_text)
    root = ET.fromstring("<Events>%s</Events>" % body)
    out = []
    for ev in root:
        data = {d.get("Name"): (d.text or "") for d in ev.iter() if d.tag.rsplit("}", 1)[-1] == "Data"}
        rid = next((int(e.text) for e in ev.iter() if e.tag.rsplit("}", 1)[-1] == "EventRecordID" and e.text), 0)
        if "NewProcessId" not in data:
            continue
        when = next((e.get("SystemTime", "") for e in ev.iter() if e.tag.rsplit("}", 1)[-1] == "TimeCreated"), "")
        out.append({"rid": rid, "pid": _number(data["NewProcessId"]), "ppid": _number(data.get("ProcessId", "")),
                    "image": data.get("NewProcessName", ""), "cmd": data.get("CommandLine", ""), "time": when})
    return out


def process_tree(events, roots) -> list:
    """The events of `parse_audit_events` that are a root or come from a process of the tree, by the creator's pid each says, in their order."""
    wanted, chosen, changed = set(roots), set(), True
    floor = min((e.get("rid", 0) for e in events if e["pid"] in wanted), default=0)  # a creator's pid that Windows reused: what came before the root is not its tree
    events = [e for e in events if e.get("rid", 0) >= floor]
    while changed:
        changed = False
        for i, e in enumerate(events):
            if i not in chosen and (e["pid"] in wanted or e["ppid"] in wanted):
                chosen.add(i)
                wanted.add(e["pid"])
                changed = True
    return [events[i] for i in sorted(chosen)]


def windows_processes(tree, real: str) -> dict:
    """{pid: {"label", "image", "cmd"}} of a tree of 4688 events, each named by what it ran with its command line (`label_of_exec`)."""
    out = {}
    for e in tree:
        argv = split_command_line(e["cmd"])
        out[e["pid"]] = {"label": label_of_exec(e["image"], argv, real), "image": e["image"], "cmd": e["cmd"]}
    return out


#: How a file was asked for (the high byte of the Create event's CreateOptions, FILE_OPEN = 1 ...): the dispositions that can make a file.
DISPOSITIONS = {0: "SUPERSEDE", 1: "OPEN", 2: "CREATE", 3: "OPEN_IF", 4: "OVERWRITE", 5: "OVERWRITE_IF"}
MAKING = (0, 2, 3, 4, 5)


def etw_files(path, pids=None, exits=None, order=None, dispositions=None) -> tuple:
    """`({(pid, file name)}, events)` of the Create events a `tracerpt -of XML` file holds, for the processes in `pids` (every process when None). Read as a
    stream: the file has every open of the machine for the length of the run. With a list in `exits`, the process stops of the same file (Kernel-Process, event 2)
    are appended to it as `{"pid", "code", "time", "image"}`: how each process ended; with a dict in `order`, the position in the file (the order the system
    logged them in, never a time) of the first open of each `(pid, name)` is put in it."""
    seen, events = set(), 0
    for _ev, el in ET.iterparse(path, events=("end",)):
        if el.tag.rsplit("}", 1)[-1] != "Event":
            continue
        events += 1
        pid, name, data, when, event_id = None, None, {}, "", ""
        for child in el.iter():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "Execution":
                raw = child.get("ProcessID") or child.get("ProcessId")
                pid = _number(raw) if raw else None
            elif tag == "Data":
                data[child.get("Name")] = child.text or ""
                if child.get("Name") == "FileName":
                    name = child.text
            elif tag == "TimeCreated":
                when = child.get("SystemTime", "")
            elif tag == "EventID":
                event_id = (child.text or "").strip()
        if exits is not None and event_id == "2" and "ExitCode" in data and data.get("ProcessID"):
            exits.append({"pid": _number(data["ProcessID"]), "code": _number(data["ExitCode"]), "time": when, "image": data.get("ImageName", "")})
        if name and pid is not None and (pids is None or pid in pids):
            seen.add((pid, name))
            if order is not None:
                order.setdefault((pid, name), events)
            if dispositions is not None and data.get("CreateOptions"):
                dispositions.setdefault((pid, name), set()).add(_number(data["CreateOptions"]) >> 24)
        el.clear()
    return seen, events


def parse_audit_setting(csv_text: str) -> str:
    """The inclusion setting of the subcategory in `auditpol /get /subcategory:{GUID} /r` ("No Auditing", "Success", ...), '' when unreadable."""
    lines = [l for l in csv_text.splitlines() if l.strip()]
    if len(lines) < 2:
        return ""
    head, row = [h.strip() for h in lines[0].split(",")], [c.strip() for c in lines[1].split(",")]
    return row[head.index("Inclusion Setting")] if "Inclusion Setting" in head and len(row) == len(head) else ""


def _tool(argv, timeout: float = 300.0) -> str:
    """A Windows tool's output, or a TraceError that says what failed."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TraceError("%s: %s" % (argv[0], e))
    if done.returncode != 0:
        raise TraceError("%s %s failed with %d: %s" % (argv[0], argv[1] if len(argv) > 1 else "", done.returncode,
                                                       (done.stdout + done.stderr).strip()[:300]))
    return done.stdout


class WindowsTrace:
    """The trace of one Windows run. `start()` BEFORE the launch (the audit on, the session open, the first canary), `finish(roots, alive)` after
    the browser was closed: it waits for the tree to be gone, stops the session, and returns what the tree did. Raises TraceError when a part of
    the trace cannot be taken (the run is then red and says why)."""

    def __init__(self, work: Path) -> None:
        self.work = Path(work)
        self.name = "abhost-%d-%d" % (os.getpid(), time.time_ns() % 10 ** 9)
        self.etl, self.xml = self.work / "files.etl", self.work / "files.xml"
        self.first_record = 0
        self.before = None
        self.session = False
        self.images: dict = {}
        self.exits_ok = False

    def _canary(self, which: str) -> str:
        """A file of the oracle's own, written and read now: the session must have a Create event of it."""
        path = self.work / ("canary-%s-%s" % (which, self.name))
        path.write_text(which, encoding="utf-8")
        path.read_text(encoding="utf-8")
        return str(path)

    def start(self) -> None:
        self.before = {"setting": parse_audit_setting(_tool(["auditpol", "/get", "/subcategory:" + AUDIT_GUID, "/r"])),
                       "value": self._registry_value()}
        _tool(["auditpol", "/set", "/subcategory:" + AUDIT_GUID, "/success:enable"])
        _tool(["reg", "add", AUDIT_KEY, "/v", AUDIT_VALUE, "/t", "REG_DWORD", "/d", "1", "/f"])
        newest = _tool(["wevtutil", "qe", "Security", "/c:1", "/rd:true", "/f:xml"])
        m = re.search(r"<EventRecordID>(\d+)</EventRecordID>", newest)
        self.first_record = int(m.group(1)) if m else 0
        _tool(["logman", "create", "trace", self.name, "-p", FILE_PROVIDER, FILE_CREATE_KEYWORD, "0xff", "-o", str(self.etl), "-ets",
               "-bs", "1024", "-nb", "64", "256"])
        self.session = True
        try:  # how each process ended (the exit code: 0 by itself, 1 for the core's TerminateProcess) is evidence: without it the run is not red, it says so
            _tool(["logman", "update", "trace", self.name, "-p", PROCESS_PROVIDER, PROCESS_KEYWORD, "0xff", "-ets"])
            self.exits_ok = True
        except TraceError:
            self.exits_ok = False
        # The provider is enabled a moment after the session exists (the first canary of the first launch of run 44963364060 was not seen): the canaries are written
        # with a round trip to the session between them, and it is the LAST one, written just before the launch, that must be in the trace.
        for n in (1, 2, 3):
            _tool(["logman", "query", self.name, "-ets"])
            self._start_canary = self._canary("start%d" % n)

    def _registry_value(self):
        try:
            out = _tool(["reg", "query", AUDIT_KEY, "/v", AUDIT_VALUE])
        except TraceError:
            return None
        m = re.search(r"REG_DWORD\s+(0x[0-9a-fA-F]+)", out)
        return int(m.group(1), 16) if m else None

    def tree(self, roots) -> list:
        xml = _tool(["wevtutil", "qe", "Security", "/q:*[System[(EventID=4688) and (EventRecordID>%d)]]" % self.first_record, "/f:xml"])
        events = parse_audit_events(xml)
        self.images = {e["pid"]: _base(e["image"]).lower() for e in events}
        return process_tree(events, roots)

    def finish(self, roots, alive=None, sleep=time.sleep, now=time.monotonic, namer=None) -> dict:
        """`{"tree": [4688 events], "files": {(pid, name)}, "outside": {process name: paths}, "exited": {...}, "events": n}`: what the TREE did from its
        first process to its last (the ancestry by the creator's pid from the launched process). What a process outside the tree opened (a service, the
        antivirus, the oracle itself apart) is not the engine's: it is counted by process name and left out of the comparison."""
        if alive is None:
            import psutil

            alive = psutil.pid_exists
        if namer is None:
            def namer(pid):
                try:
                    import psutil

                    return psutil.Process(pid).name().lower()
                except Exception:  # noqa: BLE001 - a process that is gone has no name
                    return "pid %d" % pid
        tree, exited = wait_exited(lambda: self.tree(roots), alive, sleep=sleep, now=now)
        end_canary = self._canary("end")
        _tool(["logman", "stop", self.name, "-ets"])
        self.session = False
        summary = self.work / "files.txt"
        _tool(["tracerpt", str(self.etl), "-of", "XML", "-o", str(self.xml), "-summary", str(summary), "-y"])
        exits: list = []
        order: dict = {}
        dispositions: dict = {}
        everything, events = etw_files(self.xml, None, exits, order, dispositions)
        tree_pids = {e["pid"] for e in tree}
        seen = {(p, n) for p, n in everything if p in tree_pids or p == os.getpid()}
        outside: dict = {}
        for p, n in everything:
            if p not in tree_pids and p != os.getpid():
                outside.setdefault(self.images.get(p) or namer(p), set()).add(n)
        problems = []
        mine = {name for pid, name in seen if pid == os.getpid()}
        for label, canary in (("last start", self._start_canary), ("end", end_canary)):
            if not any(m.lower().endswith(os.path.basename(canary).lower()) for m in mine):
                problems.append("the file trace did not see the %s canary file of the oracle (%s): the session was not recording" % (label, os.path.basename(canary)))
        lost = re.search(r"(?i)events?\s+lost\D{0,20}(\d+)", summary.read_text(encoding="utf-8", errors="replace") if summary.exists() else "")
        if lost and int(lost.group(1)):
            problems.append("the file trace lost %s events" % lost.group(1))
        missing = sorted(tree_pids - {pid for pid, _n in seen})
        if missing:
            problems.append("the file trace holds no open of the process(es) %s" % ", ".join(str(p) for p in missing[:10]))
        return {"tree": tree, "files": {(p, n) for p, n in seen if p in tree_pids}, "outside": {k: len(v) for k, v in sorted(outside.items())},
                "exited": exited, "events": events, "problems": problems, "exits": [e for e in exits if e["pid"] in tree_pids], "exits_available": self.exits_ok,
                "order": {k: v for k, v in order.items() if k[0] in tree_pids},
                "dispositions": {k: sorted(v) for k, v in dispositions.items() if k[0] in tree_pids}}

    def cleanup(self) -> None:
        """Put the machine back: the session closed, the audit as it was."""
        if self.session:
            try:
                _tool(["logman", "stop", self.name, "-ets"])
            except TraceError:
                pass
            self.session = False
        if self.before is None:
            return
        flags = {"Success": ("enable", "disable"), "Failure": ("disable", "enable"), "Success and Failure": ("enable", "enable")}
        success, failure = flags.get(self.before["setting"], ("disable", "disable"))
        try:
            _tool(["auditpol", "/set", "/subcategory:" + AUDIT_GUID, "/success:" + success, "/failure:" + failure])
            if self.before["value"] is None:
                _tool(["reg", "delete", AUDIT_KEY, "/v", AUDIT_VALUE, "/f"])
            else:
                _tool(["reg", "add", AUDIT_KEY, "/v", AUDIT_VALUE, "/t", "REG_DWORD", "/d", str(self.before["value"]), "/f"])
        except TraceError:
            pass
        self.before = None


def windows_aliases(folders) -> list:
    """[(short, long)] for the folders whose 8.3 spelling differs from the long one (GetShortPathNameW / GetLongPathNameW; Windows only)."""
    import ctypes

    out, seen = [], set()
    buf = ctypes.create_unicode_buffer(32768)
    for folder in folders:
        if not folder or not os.path.isdir(folder):
            continue
        long = buf.value if ctypes.windll.kernel32.GetLongPathNameW(folder, buf, 32768) else folder
        short = buf.value if ctypes.windll.kernel32.GetShortPathNameW(long, buf, 32768) else long
        for a, b in ((short, long), (folder, long)):
            if a.lower() != b.lower() and (a.lower(), b.lower()) not in seen:
                seen.add((a.lower(), b.lower()))
                out.append((a, b))
    return out


def windows_device_map() -> dict:
    """{`\\device\\harddiskvolume3`: `c:`} of this machine, from QueryDosDeviceW (Windows only)."""
    import ctypes
    import string

    out = {}
    buf = ctypes.create_unicode_buffer(1024)
    for letter in string.ascii_uppercase:
        if ctypes.windll.kernel32.QueryDosDeviceW("%s:" % letter, buf, 1024):
            out[buf.value.lower()] = "%s:" % letter.lower()
    return out


# -
# The exit of a closed run: the quiet point is the process exit, not a wait.

def wait_exited(read_tree, alive, period=EXIT_PERIOD_S, limit_s=EXIT_LIMIT_S, sleep=time.sleep, now=time.monotonic) -> tuple:
    """Reads the tree of the run (`read_tree()` returns the events of its processes, in their order) until every process of it is gone (`alive(pid)`)
    and two reads in a row say the same thing, or until `limit_s`. Returns (the last tree, {"exited": bool, "reads": n}): a tree that was
    still running at the limit says so, it is not made to look quiet."""
    end = now() + limit_s
    previous, reads = None, 0
    while True:
        tree = read_tree()
        reads += 1
        pids = tuple(e["pid"] for e in tree)
        if not any(alive(p) for p in pids) and pids == previous:
            return tree, {"exited": True, "reads": reads}
        previous = pids
        if now() >= end:
            return tree, {"exited": False, "reads": reads}
        sleep(period)


# -
# The comparison.

#: A public destination is compared as its CLASS: the process kind, the verb, the transport and the port, whatever the address: the name the engine connects to is
#: answered by other addresses from one launch to the next (DNS rotation: 140.82.112-114.x:443, 185.199.108-110.133:443, 151.101.x.91:80 in run 44988820319), so the
#: number of distinct addresses of a class is not compared. A name is never made up from an address.
NET_CLASS_REASON = "the same name is answered by other addresses from one launch to the next (DNS rotation): a public destination is compared as its class"
_PUBLIC_DESTINATION = re.compile(r"^(?P<kind>.+?): (?P<verb>connect|send) (?P<transport>\S+) AF_INET6? \[?[^\]\s]*\]?:(?P<port>\d+)$")


def net_class(entry: str) -> str:
    """`<kind>: connect tcp AF_INET 8.8.8.8:443` is `<kind>: connect tcp public:443`; any other entry (a netlink socket, a request) is itself."""
    m = _PUBLIC_DESTINATION.match(entry)
    return "%s: %s %s public:%s" % (m.group("kind"), m.group("verb"), m.group("transport"), m.group("port")) if m else entry


def leaves_of(seed_footprint: dict, with_pingsender: bool = False) -> dict:
    """{leaf: value} of one seed: proc.<kind> = how many processes of that kind the run made (1 for a kind of type tab: its presence),
    file.<path> = 1 for every HOST path the tree touched and net.<kind>: <what> = 1 for every netlink socket, netlink request and public
    destination of a process of that kind (`network_events`)."""
    out = {}
    for kind, n in seed_footprint.get("kinds", {}).items():
        if kind == PRIVILEGED:
            continue  # absent or present per launch (PRIVILEGED_REASON): `privileged_problems`
        if kind == PINGSENDER_KIND or (kind.endswith("<-pingsender") and not with_pingsender):
            continue  # 0 or 3 of them, with their form (`pingsender_problems`), and what they spawned: there when they are
        out["proc." + kind] = 1 if is_tab(kind) and n else n
    # what only the pingsender kind touches or reaches is compared when it is on both sides of the launch (`with_pingsender`), left out when on one side only
    surface_files, surface_nets = pingsender_surface(seed_footprint)
    for path in seed_footprint.get("files", []):
        if path in surface_files and not with_pingsender:
            continue
        out["file." + path] = 1
    for entry in seed_footprint.get("net") or []:
        if not entry.startswith(PINGSENDER_KIND + ": "):
            out["net." + net_class(entry)] = 1
    if with_pingsender:
        for entry in surface_nets:
            out["net." + entry] = 1
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


def _declared_covers(entry: dict, diff: dict) -> bool:
    """A declared difference covers a difference of `proc.<name>` that is the published engine having it (count >= 1) and the candidate not
    having it: by kind."""
    if entry.get("category") != "proc" or diff["leaf"] != "proc." + entry["name"]:
        return False
    published, candidate = diff["a"], diff["b"]
    return (published != MISSING and published >= 1) and candidate == MISSING


def warmup_only(fp: dict) -> dict | None:
    """What the discarded warm-up launch of an engine did that its measured launch of the same seed did not (a first-launch-only behaviour, which stays
    visible run to run): the kinds that are in the warm-up and not in the measured launch, or more of them (not for the tab kinds, whose count
    is the idle time's), and the host paths only the warm-up touched, each with the process that did it. None for a footprint with no warm-up."""
    w = fp.get("warmup")
    if not w:
        return None
    m = next((s for s in fp.get("seeds", []) if s["seed"] == w["seed"]), None)
    if m is None:
        return {"seed": w["seed"], "kinds": [], "files": [], "net": [], "measured": False}
    kinds = []
    for kind, n in sorted(w.get("kinds", {}).items()):
        measured = m.get("kinds", {}).get(kind, 0)
        if measured == 0 or (n > measured and not is_tab(kind)):
            kinds.append({"kind": kind, "warmup": n, "measured": measured, "spawned_by": w.get("spawned_by", {}).get(kind, [])})
    measured_files = set(m.get("files", []))
    files = [{"path": path, "touched_by": w.get("touched_by", {}).get(path, [])} for path in w.get("files", []) if path not in measured_files]
    measured_net = set(m.get("net") or [])
    net = [entry for entry in (w.get("net") or []) if entry not in measured_net]
    return {"seed": w["seed"], "kinds": kinds, "files": files, "net": net, "measured": True}


def _by_text(d: dict) -> str:
    by, calls = d.get("by") or {}, d.get("calls") or {}
    if not (by.get("A") or by.get("B")):
        return ""
    via = ", via %s" % ", ".join(sorted(set(calls.get("A", [])) | set(calls.get("B", [])))) if calls.get("A") or calls.get("B") else ""
    return " (by A: %s | B: %s%s)" % (", ".join(by.get("A", [])) or "-", ", ".join(by.get("B", [])) or "-", via)


def _calls(seed_footprint: dict, leaf: str) -> list:
    """The calls (Linux) that touched a path of a seed's footprint: which one opened it."""
    category, _dot, name = leaf.partition(".")
    return list(seed_footprint.get("touched_calls", {}).get(name, [])) if category == "file" else []


def _by(seed_footprint: dict, leaf: str) -> list:
    """The processes to blame for a leaf in a seed's footprint: who touched a path, who spawned a kind (a net leaf names its kind itself)."""
    category, _dot, name = leaf.partition(".")
    if category == "net":
        return []
    return list(seed_footprint.get("touched_by" if category == "file" else "spawned_by", {}).get(name, []))


def _windows() -> bool:
    return sys.platform == "win32"


def location_utility_kinds(kinds) -> list:
    """The kinds of process (`utility/sandboxingKind=3/notForBrowser`) that are the utility process of the Windows location provider."""
    return sorted(k for k in kinds if "sandboxingKind=%s" % LOCATION_UTILITY_KIND in k.split("/"))


def location_modules(paths) -> list:
    """The paths (host paths of a footprint, either separator) of a module the Windows location provider loads."""
    out = []
    for path in paths:
        name = _base(path).lower()
        if name in LOCATION_MODULES or name.startswith(LOCATION_MODULE_PREFIXES):
            out.append(path)
    return sorted(out)


def geolocation_problems(seed_footprint: dict, where: str) -> list:
    """What a candidate's launch did to the host that only the platform's location provider does: a process of the kind `-sandboxingKind 3` and
    the modules it loads. Absolute (it does not read the published engine): the candidate answers from the core's declaration and asks nobody."""
    problems = []
    kinds = location_utility_kinds(seed_footprint.get("kinds", {}))
    if kinds:
        problems.append("%s side B: the engine started the Windows location utility process (-sandboxingKind %s): %s"
                        % (where, LOCATION_UTILITY_KIND, ", ".join(kinds)))
    modules = location_modules(seed_footprint.get("files", []))
    if modules:
        problems.append("%s side B: the engine loaded location modules: %s" % (where, ", ".join(modules[:10])))
    return problems


def stock_control_problems(stock: dict) -> list:
    """What makes the stock control BROKEN (exit 2, the A/B says nothing): its own trace or step failed, or stock Firefox, given the permission and the
    same request, did not start the Windows location utility process, so the step does not reach the platform provider."""
    seed = stock.get("stock") or {}
    problems = ["stock control: %s" % p for p in seed.get("problems", [])]
    problems += ["stock control: step %s (%s) failed: %s" % (st.get("n"), st.get("name"), st.get("error")) for st in seed.get("steps", []) if not st.get("ok")]
    if not seed:
        problems.append("stock control: the run made no footprint")
    elif not location_utility_kinds(seed.get("kinds", {})):
        problems.append("stock control: stock Firefox did not start the Windows location utility process (-sandboxingKind %s) after getCurrentPosition with the "
                        "permission granted: the step does not reach the platform provider, so a candidate without that process proves nothing"
                        % LOCATION_UTILITY_KIND)
    return problems


def judge(a: dict, b: dict, expected: list | None = None, a_a: bool = False, platform: str | None = None,
          floors: dict | None = None, also_68: list | None = None) -> dict:
    """The verdict on two footprints (the document `run` writes). `expected` is the declared differences, applied only when `a_a` is false.
    Returns {"verdict", "problems", "differences", "declared", "ours", "tab", "leaves": {seed: {category: n}}, "mode"}: nothing is excused."""
    floors = FLOORS if floors is None else floors
    platform = platform or a.get("platform") or sys.platform
    os_name = "windows" if platform == "win32" else "linux"
    seeds_a = {s["seed"]: s for s in a.get("seeds", [])}
    seeds_b = {s["seed"]: s for s in b.get("seeds", [])}
    problems, differences, declared, counts = [], [], [], {}
    for side, fp in (("A", a), ("B", b)):
        for message in fp.get("problems", []):
            problems.append("side %s: %s" % (side, message))
    if sorted(seeds_a) != sorted(seeds_b):
        problems.append("the two footprints have different seeds: A %s, B %s" % (sorted(seeds_a), sorted(seeds_b)))
    for seed in sorted(set(seeds_a) & set(seeds_b)):
        both = bool(seeds_a[seed].get("kinds", {}).get(PINGSENDER_KIND)) and bool(seeds_b[seed].get("kinds", {}).get(PINGSENDER_KIND))
        la, lb = leaves_of(seeds_a[seed], both), leaves_of(seeds_b[seed], both)
        # The shortcut inventory (Windows) is there or it is not (`INVENTORY_MARKER`): when one side ran it and the other did not, the paths it walked that the other
        # side does not have are left out; when both ran it, its paths are compared like any other, so it has to be complete.
        inv_a, inv_b = set(seeds_a[seed].get("inventory") or []), set(seeds_b[seed].get("inventory") or [])
        if inv_a and not inv_b:
            for path in inv_a - set(seeds_b[seed].get("files", [])):
                la.pop("file." + path, None)
        elif inv_b and not inv_a:
            for path in inv_b - set(seeds_a[seed].get("files", [])):
                lb.pop("file." + path, None)
        for side, fp in (("A", seeds_a[seed]), ("B", seeds_b[seed])):
            for message in fp.get("problems", []) + pingsender_problems(fp) + privileged_problems(fp):
                problems.append("seed %s side %s: %s" % (seed, side, message))
        per = {c: 0 for c in CATEGORIES}
        for leaf in set(la) | set(lb):
            per[category_of(leaf)] = per.get(category_of(leaf), 0) + 1
        counts[str(seed)] = per
        for side, leaves in (("A", la), ("B", lb)):
            have = {c: sum(1 for k in leaves if category_of(k) == c) for c in floors}
            for c, floor in floors.items():
                if have[c] < floor:
                    problems.append("seed %s side %s has %d %s leaves, fewer than the floor of %d: the run was not seen"
                                    % (seed, side, have[c], c, floor))
        if os_name == "windows":
            for leaves in (la, lb):
                for k in [k for k in leaves if k.startswith(SYSTEM_PREFIX)]:
                    del leaves[k]
        for leaf in sorted(set(la) | set(lb)):
            va, vb = la.get(leaf, MISSING), lb.get(leaf, MISSING)
            if va != vb:
                differences.append({"seed": seed, "category": category_of(leaf), "leaf": leaf, "a": _clip(va), "b": _clip(vb),
                                    "by": {"A": _by(seeds_a[seed], leaf), "B": _by(seeds_b[seed], leaf)},
                                    "calls": {"A": _calls(seeds_a[seed], leaf), "B": _calls(seeds_b[seed], leaf)}})
    system = None
    if os_name == "windows":
        more, found, system = system_class(a, b, a_a, also_68 or ())
        differences += more
        problems += found
    if os_name == "windows" and not a_a:
        for where, fp in [("seed %s" % seed, seeds_b[seed]) for seed in sorted(seeds_b)] + ([("warm-up", b["warmup"])] if b.get("warmup") else []):
            problems += geolocation_problems(fp, where)
    problems += idle_problems(a, b)
    wa, wb = a.get("warmup") or {}, b.get("warmup") or {}
    if wa.get("kinds", {}).get(PINGSENDER_KIND) and wb.get("kinds", {}).get(PINGSENDER_KIND):
        for what, x, y in zip(("paths", "destinations"), pingsender_surface(wa), pingsender_surface(wb)):
            if x != y:
                problems.append("warm-up: the pingsender %s differ: A only %s | B only %s" % (what, sorted(set(x) - set(y))[:5], sorted(set(y) - set(x))[:5]))
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
    for d in differences:
        problems.append("seed %s %s: A %s | B %s%s" % (d["seed"], d["leaf"], json.dumps(d["a"], sort_keys=True)[:160],
                                                       json.dumps(d["b"], sort_keys=True)[:160], _by_text(d)))
    tab = {str(seed): {"A": {k: n for k, n in seeds_a[seed].get("kinds", {}).items() if is_tab(k)},
                       "B": {k: n for k, n in seeds_b[seed].get("kinds", {}).items() if is_tab(k)}}
           for seed in sorted(set(seeds_a) & set(seeds_b))}
    outside = {side: {str(seed): seeds[seed]["outside_tree"] for seed in sorted(seeds) if seeds[seed].get("outside_tree")}
               for side, seeds in (("A", seeds_a), ("B", seeds_b))}
    net_skipped = {side: {str(seed): seeds[seed]["net_not_compared"] for seed in sorted(seeds) if seeds[seed].get("net_not_compared")}
                   for side, seeds in (("A", seeds_a), ("B", seeds_b))}
    net = ({"covered": False, "reason": NET_WINDOWS} if os_name == "windows"
           else {"covered": True, "not_compared": NET_NOT_COMPARED, "counts": net_skipped})
    warm = {"A": warmup_only(a), "B": warmup_only(b)}
    both = None
    if warm["A"] and warm["B"]:
        both = {"kinds": sorted({k["kind"] for k in warm["A"]["kinds"]} & {k["kind"] for k in warm["B"]["kinds"]}),
                "files": sorted({f["path"] for f in warm["A"]["files"]} & {f["path"] for f in warm["B"]["files"]}),
                "net": sorted(set(warm["A"].get("net", [])) & set(warm["B"].get("net", [])))}
    return {"format": FORMAT, "mode": "a-a" if a_a else "a-b", "verdict": "red" if problems else "green", "problems": problems,
            "differences": differences, "declared": declared, "ours": OURS, "tab": {"reason": TAB_REASON, "counts": tab},
            "outside_tree": outside, "warmup": dict(warm, both=both), "idle": {side: _idle_summary(fp.get("idle")) for side, fp in (("A", a), ("B", b))}, "system_modules": system,
            "bare": {side: _bare_summary((fp.get("bare") or {}).get("bare")) for side, fp in (("A", a), ("B", b))},
            "shortcuts": {side: _shortcuts_summary(((fp.get("idle") or {}).get("idle") or {}).get("shortcut_changes")) for side, fp in (("A", a), ("B", b))},
            "stale": [e.get("id") for e in stale], "leaves": counts, "net": net}


def print_report(report: dict, a_name: str, b_name: str) -> None:
    for seed, per in sorted(report["leaves"].items()):
        print("seed %s leaves: %s" % (seed, ", ".join("%s %d" % kv for kv in sorted(per.items()))))
    for d in report["differences"][:200]:
        print("DIFF seed %s %s: %s %s | %s %s%s" % (d["seed"], d["leaf"], a_name, json.dumps(d["a"], sort_keys=True)[:160],
                                                    b_name, json.dumps(d["b"], sort_keys=True)[:160], _by_text(d)))
    system = report.get("system_modules")
    if system and system["mode"] == "a-a":
        for side, name in (("A", a_name), ("B", b_name)):
            for layer in ("measured", "long"):
                c = system["counts"][layer][side]
                print("RARE system modules only %s has in its %s launches: %d counted (red above %d), %d .mui excused with their library, %d .cat excused, %d orphan .mui%s" % (
                    name, layer, c["counted"], system["limits"][layer], c["mui_excused"], c["cat_excused"], c["orphans"],
                    (": " + json.dumps(system["rare"][layer][side][:20])) if system["rare"][layer][side] else ""))
    elif system:
        print("SYSTEM modules the 68 has and the candidate lacks (not red): %d; the candidate has alone: %d counted, %d .mui excused, %d .cat excused, %d orphan .mui; .mui and .cat "
              "opened: 68 %d, candidate %d" % (system["lacking_count"], len(system["candidate_only"]), len(system["excused_candidate_only"]["mui"]),
                                                len(system["excused_candidate_only"]["cat"]), len(system["orphans"]), len(system["listed_mui_and_cat"]["68"]),
                                                len(system["listed_mui_and_cat"]["candidate"])))
    for side, name in (("A", a_name), ("B", b_name)):
        warm = (report.get("warmup") or {}).get(side)
        if warm:
            for k in warm["kinds"][:50]:
                print("WARMUP-ONLY %s seed %s proc.%s: warm-up %s, measured %s (spawned by: %s)" % (
                    name, warm["seed"], k["kind"], k["warmup"], k["measured"], ", ".join(k["spawned_by"]) or "-"))
            for f in warm["files"][:100]:
                print("WARMUP-ONLY %s seed %s file.%s (touched by: %s)" % (name, warm["seed"], f["path"], ", ".join(f["touched_by"]) or "-"))
            for entry in warm.get("net", [])[:100]:
                print("WARMUP-ONLY %s seed %s net.%s" % (name, warm["seed"], entry))
    for side, name in (("A", a_name), ("B", b_name)):
        shortcuts = (report.get("shortcuts") or {}).get(side)
        if shortcuts:
            print("SHORTCUTS (the places of the shortcut code, before and after the idle launch; evidence) %s: %d new, %d changed, %d gone %s" % (
                name, shortcuts["new"], shortcuts["changed"], shortcuts["gone"], json.dumps(shortcuts["files"])))
    for side, name in (("A", a_name), ("B", b_name)):
        bare = (report.get("bare") or {}).get(side)
        if bare:
            print("BARE (no client, no page; not compared) %s: %s" % (name, json.dumps(bare, sort_keys=True)))
    for side, name in (("A", a_name), ("B", b_name)):
        idle = (report.get("idle") or {}).get(side)
        if idle:
            print("IDLE (the pings must leave) %s: notification %s, %d pingsender %s by %s" % (
                name, json.dumps(idle["notified"]), idle["pingsenders"], idle["types"], idle["parents"]))
    for side, name in (("A", a_name), ("B", b_name)):
        for seed, names in sorted(((report.get("outside_tree") or {}).get(side) or {}).items()):
            print("OUTSIDE THE TREE (not compared) %s seed %s: %s" % (name, seed, ", ".join("%s %d paths" % kv for kv in sorted(names.items()))))
    for d in report["declared"]:
        print("DECLARED seed %s %s (%s)" % (d["seed"], d["leaf"], d["id"]))
    print("OURS (not compared): " + "; ".join("%s: %s" % kv for kv in sorted(report.get("ours", OURS).items())))
    net = report.get("net") or {}
    if net and not net.get("covered"):
        print("NET NOT COVERED: " + net["reason"])
    elif net:
        for side, name in (("A", a_name), ("B", b_name)):
            for seed, classes in sorted((net.get("counts") or {}).get(side, {}).items()):
                print("NET (not compared) %s seed %s: %s" % (name, seed, ", ".join("%s %d" % kv for kv in sorted(classes.items()))))
    n = len(report["differences"])
    for p in report["problems"][:len(report["problems"]) - n]:
        print("FAIL " + p)
    print("AB HOST (%s) %s: %d differences, %d problems"
          % (report["mode"], "OK" if report["verdict"] == "green" else "FAILED", n, len(report["problems"])))


# -
# One engine: the journey, the close, the trace, the footprint.

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


def _context(binary: str, windows: bool, work: Path) -> Context:
    """The run's own folders: the package (the link and the real folder), the temp root of this process (the oracle's subprocess runs with a
    TMPDIR / TEMP of its own, `run_subprocess`) and the folder of the work of the seed, and the cache folder of the run."""
    real = os.path.realpath(binary)
    app = {os.path.dirname(real), os.path.dirname(os.path.abspath(binary))}
    temp = {tempfile.gettempdir(), str(work)}
    if windows:
        folders = [*app, *temp, os.path.expanduser("~"), *(os.environ.get(v, "") for v in ("USERPROFILE", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "ProgramData", "SystemRoot"))]
        return Context("win32", app=app, temp=temp, devices=windows_device_map(), aliases=windows_aliases(folders))
    return Context("linux", app=app, temp=temp, cache={os.environ.get("XDG_CACHE_HOME", "")})


def start_geo_lab():
    """The oracle's own lab page of the geolocation step: one static page on 127.0.0.1 (a secure context). Returns (origin, stop)."""
    body = b"<!doctype html><meta charset=utf-8><title>geo</title><p>geo"

    class Handler(BaseHTTPRequestHandler):
        timeout = 5

        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop():
        server.shutdown()
        server.server_close()
        thread.join()

    return "http://127.0.0.1:%d" % server.server_address[1], stop


def geolocation_step(browser, origin: str, declared: dict | None, n: int) -> dict:
    """The step after the journey: a context (with the declared position, or none for the stock control), the permission granted to the lab page,
    the page opened, `getCurrentPosition` called and awaited. Returns the step as the journey's are recorded, with `geo` (what the page was answered)."""
    step = {"n": n, "name": GEO_STEP}
    context = None
    try:
        context = browser.new_context(geolocation=dict(declared)) if declared else browser.new_context()
        context.grant_permissions(["geolocation"], origin=origin)
        page = context.new_page()
        page.goto(origin + GEO_PATH)
        step["geo"] = page.evaluate(GEO_READ)
        step["ok"] = True
    except Exception as exc:  # noqa: BLE001 - recorded, and a step that failed is a problem of the run
        step.update(ok=False, error=str(exc).strip().splitlines()[0][:300] if str(exc).strip() else repr(exc))
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
    return step


def run_seed(binary: str, seed: int, work: Path, name: str | None = None, idle: bool = False, bare: bool = False, crash_with: int | None = None) -> dict:
    """One browser, one journey, one close, one trace. Returns the seed's footprint; `name` is the folder of its work (the warm-up has one of its own).

    `bare` is the old idle launch, kept as a case of its own and never compared: no client and no page, as the run 44988820319 made it (a crash reporter ran in it, on
    both systems). It records how the browser ended and what a crash leaves (`crash_evidence`), on Linux with core dumps on, and waits as the idle launch does.

    `idle` is the launch in which the exit pings MUST leave (see `run_engine`): no journey and no pref changed; the launch waits until the notification of the data
    policy is recorded in its profile (the precondition of `canUpload`, seen in a file of the launch) and then closes; its footprint carries `idle` (what the wait saw).

    EVERY launch is judged by how its parent ended: a root exit code other than 0 is a problem of the launch (`crash.exit_problem`) and the launch keeps what it left
    (`result["crash"]`, `crash.collect`: the strace tail with the signal line, the core through gdb, the build ids, the `.extra` and the minidump names). The cores are
    enabled in every launch (`ulimit -c unlimited` in the stand-in, the working directory untouched) and a crash watcher reads the crash folders while any launch lives.

    `crash_with` is the recorder's own test (`crash-probe`): the signal sent to the parent of the browser once it is up, to show that a launch that dies is red and keeps its evidence."""
    from playwright.sync_api import sync_playwright

    from fakeweb import journey_site, profile_p4
    from invisible_playwright._juggler.remote import launch_server

    windows = sys.platform == "win32"
    seed_work = work / (name or "seed%d" % seed)
    seed_work.mkdir(parents=True)
    exe, log, pidfile, pgid = binary, seed_work / "strace.log", seed_work / "stand-in.pid", None
    trace = None
    problems = []
    if windows:
        trace = WindowsTrace(seed_work)
        try:
            trace.start()
        except TraceError as e:
            trace.cleanup()
            return {"seed": seed, "steps": [], "kinds": {}, "files": [], "ours": {}, "trace": {"source": "etw+audit", "exited": False},
                    "problems": ["the trace of a Windows run could not be started (it needs an administrator): %s" % e]}
    else:
        from _strace_stand_in import _stand_in

        exe = _stand_in(binary, seed_work / "engine", log, pidfile, syscalls=STRACE_CALLS, fd_paths=True, network=True, exit_status=True,
                        core_dir=seed_work if bare else None, cores=True)
    origin_a, origin_o, stop_site = journey_site.start((0, 0))
    geo_origin, stop_geo = start_geo_lab()
    server, steps, root_pid = None, [], None
    notified, watcher = None, None
    places = shortcut_places() if (windows and idle) else None
    shortcuts_before = snapshot_places(places) if places else None
    before = set(Path(tempfile.gettempdir()).glob("invisible_profile_*"))

    def crash_dirs():
        new = sorted(set(Path(tempfile.gettempdir()).glob("invisible_profile_*")) - before)
        return crash_report_dirs(new[0] if new else None, windows)

    started_at = time.time()
    already = crash.existing_crash_files(crash_dirs())  # the reports of earlier crashes are not this launch's

    try:
        server = launch_server(headless=True, binary_path=exe, seed=seed, locale="en-US", timezone="Europe/Rome",
                               launch_timeout_ms=180_000)
        root_pid = server.process.pid
        if not windows:
            from _strace_stand_in import _group_of

            pgid = _group_of(pidfile)
        watcher = CrashWatcher(crash_dirs, ignore=already)
        watcher.start()
        with (contextlib.nullcontext() if (bare or crash_with is not None) else sync_playwright()) as p:
            if crash_with is not None:
                crash_the_parent(root_pid, server.process, crash_with)  # the recorder's own test: the browser is up, its parent is sent a fatal signal
            elif bare:
                notified = wait_for_notification(profile_prefs_reader(before))  # nothing drives the bare browser: the launch the idle one was
            elif idle:
                # Run 44988820319: with no client and no page the notification was never recorded in 120 s on either system (no window, so no
                # `sessionstore-windows-restored` for the policy to start from) and the launch had a crash reporter: the idle launch has the page of a launch, and nothing else.
                browser = p.firefox.connect(server.ws_endpoint)
                context = browser.new_context()
                context.new_page()
                notified = wait_for_notification(profile_prefs_reader(before))
                context.close()
                browser.close()
            else:
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
                steps.append(geolocation_step(browser, geo_origin, GEO_DECLARED, len(JOURNEY) + 1))
                context.close()
                browser.close()
        # The close: the run ends with the exit of the browser, and the trace is read after it.
        root = server.process
        started = time.monotonic()
        server.close()
        server = None
        # evidence of HOW the browser ended, never a threshold: the core closes the pipe and waits for the browser to exit by itself (`Connection.close`), and only
        # ends it when it does not (`terminate`, exit code 1 on Windows, SIGTERM on Linux); the return code of the root says which one happened
        close = {"root_returncode": root.poll() if root is not None else None, "close_s": round(time.monotonic() - started, 2)}
        exited = _wait_for_tree_exit(pgid)
        if exited is not None and not exited:
            problems.append("the tree did not exit within %d s of the close: the trace is cut short" % EXIT_LIMIT_S)
        result = _seed_result(binary, seed, seed_work, steps, log, trace, root_pid, windows, close)
        if idle:
            result["idle"] = {"notified": notified, "shortcut_changes": diff_snapshots(shortcuts_before, snapshot_places(places)) if places else None}
        watched, watcher = watcher.stop(), None
        if bare:
            result["bare"] = {"notified": notified, "crash": crash_evidence(watched, result, core_info(seed_work, binary) if not windows else None)}
        bad, left = end_of_launch(close["root_returncode"], windows, seed, name or "measured", result.get("lifecycle"), binary, log, watched,
                                  seed_work if bare else None, started_at)
        problems += bad
        result["exit_problems"] = bad
        if left:
            result["crash"] = left
    except TraceError as e:
        result = {"seed": seed, "steps": steps, "kinds": {}, "files": [], "ours": {}, "trace": {"exited": False},
                  "problems": ["the trace of the run could not be taken: %s" % e]}
    finally:
        stop_site()
        stop_geo()
        if watcher is not None:
            watcher.stop()
        if server is not None:
            try:
                server.close()
            except Exception:  # noqa: BLE001
                pass
        if not windows:
            from _strace_stand_in import _kill_group

            _kill_group(pidfile)
        elif trace is not None:
            trace.cleanup()
    result["problems"] = result.get("problems", []) + problems
    return result


def end_of_launch(code, windows: bool, seed: int, launch: str, lifecycle, binary: str, log, watched: dict, bare_dir, started_at: float, folders=None, run=subprocess.run) -> tuple:
    """How a launch ended, as the verdict reads it: `(problems, evidence)`. A root exit code other than 0 (or not seen) is a problem that names the seed, the launch and the
    code (`crash.exit_problem`), and so is a process of the kind parent that the lifecycle says did not exit 0 when the root said nothing of it (Windows: the core's spawn
    may be a launcher). A launch with a problem keeps what it left (`crash.collect`): the strace tail and the fatal signal lines, the cores through gdb with the build ids
    (the folders of the job's core pattern, and the launch's own when it moved there: the bare one), the `.extra` and the minidump names the watcher read. A launch that
    ended with 0 has none and costs nothing."""
    bad = [x for x in [crash.exit_problem(code, windows, seed=seed, launch=launch)] + crash.parent_exit_problems(lifecycle, code, seed=seed, launch=launch) if x]
    if not bad:
        return [], None
    if folders is None:
        folders = [] if windows else crash.core_dirs() + ([bare_dir] if bare_dir else [])
    return bad, crash.collect(code, binary, strace_log=None if windows else log, watched=watched, windows=windows, core_folders=folders, since=started_at, run=run)


def browser_parent(root_pid: int, children=None) -> int:
    """The pid of the browser's own parent process: under the stand-in the root is strace, whose child is the browser (a content process is not it)."""
    if children is None:
        import psutil

        children = psutil.Process(root_pid).children
    for child in children():
        try:
            if "-contentproc" not in [str(a) for a in child.cmdline()]:
                return child.pid
        except Exception:  # noqa: BLE001 - a child that is already gone
            continue
    raise RuntimeError("the root process %s has no browser parent to send the signal to" % root_pid)


def crash_the_parent(root_pid: int, process, signum: int, limit_s: float = 60.0, kill=os.kill, parent=None, sleep=time.sleep, now=time.monotonic) -> int:
    """The recorder's own test: `signum` is sent to the browser's parent process with `os.kill`, and the launch waits for the root to end (a limit of acquisition, never a
    verdict: what ended it is read from the code the close sees). Returns the pid it was sent to."""
    pid = parent if parent is not None else browser_parent(root_pid)
    kill(pid, signum)
    end = now() + limit_s
    while process.poll() is None and now() < end:
        sleep(0.1)
    return pid


#: The crash evidence of the BARE launch (no client, no page: the launch of run 44988820319 that had a crash reporter): where a crash leaves its files.
CRASH_PROCESS = re.compile(r"crashreporter|minidump-analyzer", re.I)


def crash_evidence(watched: dict, fp: dict, core: dict | None = None) -> dict:
    """What the bare launch says about how the browser ended: the exit code or signal of its parent processes (from the lifecycle), every crash reporter or
    minidump analyzer process with its command line, the `.dmp` names and `.extra` contents the watcher read, and the core dump if there is one."""
    lifecycle = fp.get("lifecycle")
    exits = [e["how"] for e in lifecycle or [] if e["event"] == "end" and e["kind"] == "parent"]
    procs = [{"kind": p["label"], "argv": p.get("cmd") or p.get("exec")} for p in fp.get("processes", []) if CRASH_PROCESS.search(p["label"])]
    return {"parent_exits": exits if lifecycle is not None else None, "root_returncode": (fp.get("close") or {}).get("root_returncode"), "crash_processes": procs,
            "files": watched.get("files", []), "scans": watched.get("scans"), "core": core}


def shortcut_places(env=None) -> list:
    """`[(folder, recursive)]` of the places the shortcut code of Windows reads and writes (StartupOSIntegration.sys.mjs, nsWindowsShellService.cpp): the Start menu of the
    user and of ProgramData (its Programs below it), the Desktop of the user and the Public one, the pinned taskbar shortcuts of the user (and ImplicitAppShortcuts) and every
    `Mozilla-*` folder of ProgramData (the shortcuts log is there)."""
    env = os.environ if env is None else env
    appdata, programdata = env.get("APPDATA"), env.get("ProgramData") or env.get("PROGRAMDATA")
    profile, public = env.get("USERPROFILE"), env.get("PUBLIC")
    places = []
    for base in (appdata and Path(appdata) / "Microsoft" / "Windows" / "Start Menu", programdata and Path(programdata) / "Microsoft" / "Windows" / "Start Menu"):
        if base:
            places += [(base, False), (base / "Programs", True)]
    for desktop in (profile and Path(profile) / "Desktop", public and Path(public) / "Desktop"):
        if desktop:
            places.append((desktop, False))
    if appdata:
        pinned = Path(appdata) / "Microsoft" / "Internet Explorer" / "Quick Launch" / "User Pinned"
        places += [(pinned / "TaskBar", True), (pinned / "ImplicitAppShortcuts", True)]
    if programdata:
        try:
            places += [(p, True) for p in sorted(Path(programdata).glob("Mozilla-*")) if p.is_dir()]
        except OSError:
            pass
    return places


def snapshot_places(places, limit: int = 20000) -> dict:
    """`{path: {"size", "mtime_ns"}}` of the files in `places` (`shortcut_places`), a folder walked below itself when it says so."""
    out: dict = {}
    for folder, recursive in places:
        try:
            entries = Path(folder).rglob("*") if recursive else Path(folder).iterdir()
            for entry in entries:
                if len(out) >= limit:
                    return out
                try:
                    if entry.is_file():
                        st = entry.stat()
                        out[str(entry)] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
                except OSError:
                    continue
        except OSError:
            continue
    return out


def diff_snapshots(before: dict, after: dict) -> dict:
    """The files that are new, changed (another size or modification time) or gone between two snapshots, each `{"path", "size", "mtime"}` (the mtime as an ISO
    text, evidence): the direct proof of whether the engine left a shortcut or a log on the host."""
    def entry(path, info):
        stamp = datetime.datetime.fromtimestamp(info["mtime_ns"] / 1e9, datetime.timezone.utc).isoformat()
        return {"path": path, "size": info["size"], "mtime": stamp}

    return {"new": [entry(p, after[p]) for p in sorted(set(after) - set(before))],
            "changed": [entry(p, after[p]) for p in sorted(set(after) & set(before)) if after[p] != before[p]],
            "gone": [entry(p, before[p]) for p in sorted(set(before) - set(after))]}


def _shortcuts_summary(changes: dict | None) -> dict | None:
    """A line's worth of `diff_snapshots`: how many files, and which ones by name."""
    if changes is None:
        return None
    return {"new": len(changes["new"]), "changed": len(changes["changed"]), "gone": len(changes["gone"]),
            "files": [(kind, Path(e["path"]).name, e["size"]) for kind in ("new", "changed", "gone") for e in changes[kind]][:20]}


def wait_for_notification(read, period=NOTIFY_PERIOD_S, limit_s=NOTIFY_LIMIT_S, sleep=time.sleep, now=time.monotonic) -> dict:
    """Reads the profile's prefs (`read()` returns the text of prefs.js, or None while there is none) until the notification of the data policy is recorded in it, or
    until `limit_s`: `{"observed": bool, "polls": n}`. The event is the verdict; the limit only ends the wait of a launch that never gets there."""
    end, polls = now() + limit_s, 0
    while True:
        text = read()
        polls += 1
        if text and NOTIFIED_PREF in text:
            return {"observed": True, "polls": polls}
        if now() >= end:
            return {"observed": False, "polls": polls}
        sleep(period)


def profile_prefs_reader(before: set):
    """`read()` for `wait_for_notification`: the prefs.js of the profile the launch made (the `invisible_profile_*` folder of the run's temp root that was not
    there before it)."""
    root = Path(tempfile.gettempdir())

    def read():
        for folder in sorted(set(root.glob("invisible_profile_*")) - before):
            try:
                return (folder / "prefs.js").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        return None

    return read


def _wait_for_tree_exit(pgid):
    """Linux: whether the process group of the run is empty (strace follows every process of the tree, so it ends with its last one); None on
    Windows, where `WindowsTrace.finish` waits for its own tree."""
    if pgid is None:
        return None
    from _strace_stand_in import _wait_for_exit

    return not _wait_for_exit(pgid, EXIT_LIMIT_S)


def _seed_result(binary: str, seed: int, seed_work: Path, steps: list, log: Path, trace, root_pid, windows: bool, close: dict | None = None) -> dict:
    real = os.path.realpath(binary)
    ctx = _context(binary, windows, seed_work)
    if windows:
        got = trace.finish({root_pid})
        procs = windows_processes(got["tree"], real)
        pairs = list(got["files"])
        labels = {pid: p["label"] for pid, p in procs.items()}
        parents = {e["pid"]: e["ppid"] for e in got["tree"]}
        labels = derive_labels(labels, parents)
        label_of = lambda pid: labels.get(pid, "?")  # noqa: E731
        inventory = inventory_of(got["order"], label_of, ctx)
        creates = creates_of(got["dispositions"], label_of, ctx)
        info = {"source": "etw+audit", "events": got["events"], "exited": got["exited"]["exited"], "reads": got["exited"]["reads"]}
        problems = list(got["problems"])
        if not got["exited"]["exited"]:
            problems.append("the tree did not exit within %d s of the close: the trace is cut short" % EXIT_LIMIT_S)
        detail = sorted(({"label": p["label"], "cmd": p["cmd"][:300]} for p in procs.values()), key=lambda d: (d["label"], d["cmd"]))
        outside = got["outside"]
        lifecycle, fd_opens = (windows_lifecycle(got["tree"], got["exits"], procs) if got["exits_available"] else None), None
        net, net_skipped = None, None  # NET_WINDOWS
    else:
        lines = list(_read_lines(log))
        procs = spawned_processes(lines, real)
        owners = thread_owners(lines)
        by_pid = touched_by_pid(lines)
        pairs = [(pid, path) for path, pids in by_pid.items() for pid in pids]
        labels = {pid: p["label"] for pid, p in procs.items()}
        parents = {pid: owner_of(p["parent"], owners) for pid, p in procs.items() if p["parent"] is not None}
        labels = derive_labels(labels, parents)
        label_of = lambda pid: labels.get(owner_of(pid, owners), "?")  # noqa: E731
        inventory, creates = [], None
        calls = touched_calls(lines)
        lifecycle, fd_opens = process_lifecycle(lines, procs), proc_fd_opens(lines, owners, label_of)
        info, problems, outside = {"source": "strace", "exited": True}, [], {}  # strace follows our tree and nothing else
        events, net_skipped = network_events(lines)
        net = net_by_kind(events, label_of)
        detail = sorted(({"label": p["label"], "exec": (" ".join([os.path.basename(p["exec"][0])] + p["exec"][1][1:6]) if p["exec"] else None),
                          "name": p["name"]} for p in procs.values()), key=lambda d: (d["label"], d["exec"] or ""))
    raw = {path for _pid, path in pairs}
    host, ours = classify_paths(raw, ctx)
    kinds = dict(sorted(collections.Counter(labels.values()).items()))
    info.update({"processes": len(procs), "paths": len(raw)})
    # touched_by, touched_calls and spawned_by are evidence, never compared: the process and the call to blame for a path or a kind that differs
    out = {"seed": seed, "steps": steps, "kinds": kinds, "files": host, "ours": ours, "trace": info, "problems": problems, "processes": detail,
           "pingsenders": ping_records(procs, labels, parents, windows), "inventory": inventory, "creates": creates,
           "touched_by": by_kind(pairs, label_of, ctx), "spawned_by": spawners(labels, parents), "outside_tree": outside,
           "net": net, "net_not_compared": net_skipped, "lifecycle": lifecycle, "proc_fd_opens": fd_opens, "close": close}
    if not windows:
        named: dict = {}
        for path, names in calls.items():
            got = classify(path, ctx)
            if got and got[0] == "host":
                named.setdefault(got[1], set()).update(names)
        out["touched_calls"] = {path: sorted(v) for path, v in sorted(named.items())}
    return out


def keep_crash(fp: dict, launch: str, crash_dir) -> None:
    """The evidence of a launch that did not end with 0 (`result["crash"]`) as files in the oracle's artifact, `<crash_dir>/<launch>/`, and said on the output."""
    if not fp.get("crash"):
        return
    where = None
    if crash_dir:
        where = Path(crash_dir) / launch
        crash.write_crash_files(fp["crash"], where)
    print("CRASH EVIDENCE %s (seed %s): exit %s; %s%s" % (launch, fp.get("seed"), fp["crash"]["exit"]["text"], "saved in %s" % where if where else "not saved (no folder)",
                                                        "; NOT FOUND: " + " | ".join(fp["crash"]["said"]) if fp["crash"]["said"] else ""))
    sys.stdout.flush()


def run_engine(binary: str, leg: str, seeds: list, label: str, crash_dir=None) -> dict:
    """The footprint document of one engine. A discarded WARM-UP launch first, of the first seed, then every seed's journey, one browser at a time.

    Every launch (the warm-up, each seed, the idle and the bare ones) that does not end with 0 is a problem of the run, and what it left is kept in `crash_dir` (`keep_crash`).

    The warm-up is a control of the experiment and not an excuse: what a machine does the first time it runs an engine (its files scanned, its caches
    cold, a first-run behaviour of the engine) is in a launch that is not compared, whose trace is kept in the document (`warmup`) so that what it
    did and the measured launch did not stays visible, run to run (`warmup_only`). The order is the same in every run: the warm-up of an engine, then its
    measured launches; the runs of an A/A or an A/B follow one another."""
    work = Path(tempfile.mkdtemp(prefix="ab-host-"))
    problems, out_seeds = [], []
    try:
        warm = run_seed(binary, seeds[0], work, name="warmup")
        keep_crash(warm, "warmup", crash_dir)
        problems += ["warm-up: %s" % p for p in warm.get("problems", [])]
        problems += ["warm-up: seed %d: step %d (%s) failed: %s" % (seeds[0], st["n"], st["name"], st.get("error")) for st in warm["steps"] if not st["ok"]]
        print("warm-up (seed %d, discarded): %s; %d host paths" % (seeds[0], json.dumps(warm["kinds"], sort_keys=True), len(warm["files"])))
        sys.stdout.flush()
        for seed in seeds:
            fp = run_seed(binary, seed, work)
            keep_crash(fp, "seed%d" % seed, crash_dir)
            out_seeds.append(fp)
            for step in fp["steps"]:
                if not step["ok"]:
                    problems.append("seed %d: step %d (%s) failed: %s" % (seed, step["n"], step["name"], step.get("error")))
            print("seed %d: %s; %d host paths, %d net entries, %s" % (seed, json.dumps(fp["kinds"], sort_keys=True), len(fp["files"]),
                                                                      len(fp.get("net") or []), json.dumps(fp.get("ours", {}), sort_keys=True)))
            sys.stdout.flush()
        idle = run_seed(binary, seeds[0], work, name="idle", idle=True)
        keep_crash(idle, "idle", crash_dir)
        problems += ["idle: %s" % p for p in idle.get("problems", [])]
        print("idle (seed %d): %s; notification %s" % (seeds[0], json.dumps(idle["kinds"], sort_keys=True), json.dumps((idle.get("idle") or {}).get("notified"))))
        sys.stdout.flush()
        # The bare launch: the old idle launch (no client, no page), a case of its own that is never compared; what it records is evidence (`crash_evidence`).
        bare = run_seed(binary, seeds[0], work, name="bare", bare=True)
        keep_crash(bare, "bare", crash_dir)
        problems += ["bare: %s" % p for p in bare.get("exit_problems", [])]  # the bare launch is never compared, but how its parent ended is red like any other's
        print("bare (seed %d, not compared): %s; %s" % (seeds[0], json.dumps(bare["kinds"], sort_keys=True), json.dumps(_bare_summary(bare.get("bare")))))
        sys.stdout.flush()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"format": FORMAT, "engine": label, "leg": leg, "platform": sys.platform, "seeds": out_seeds, "warmup": warm, "idle": idle, "bare": bare, "problems": problems,
            "binary": os.path.basename(os.path.dirname(os.path.realpath(binary)))}


def run_stock(leg: str) -> dict:
    """The negative control of the geolocation step, Windows only: Playwright's own Firefox (`p.firefox.launch()`, as the fake web's stock control), with
    no declared position, the same step, the same trace. It must start the Windows location utility process (`stock_control_problems`). The tree is the
    one of this process (the driver and the browser it launches): only the kinds and the modules of the run are read."""
    from playwright.sync_api import sync_playwright

    work = Path(tempfile.mkdtemp(prefix="ab-host-stock-"))
    trace = WindowsTrace(work)
    stop_geo = None
    steps = []

    def document(result):
        return {"format": FORMAT, "engine": "stock", "leg": leg, "platform": sys.platform, "stock": result}

    try:
        try:
            trace.start()
        except TraceError as e:
            return document({"seed": 0, "steps": [], "kinds": {}, "files": [], "ours": {}, "trace": {"source": "etw+audit", "exited": False},
                             "problems": ["the trace of a Windows run could not be started (it needs an administrator): %s" % e]})
        geo_origin, stop_geo = start_geo_lab()
        try:
            with sync_playwright() as p:
                exe = p.firefox.executable_path
                browser = p.firefox.launch(headless=True)
                try:
                    steps.append(geolocation_step(browser, geo_origin, None, len(JOURNEY) + 1))
                finally:
                    browser.close()
            return document(_seed_result(exe, 0, work, steps, work / "none.log", trace, os.getpid(), True))
        except TraceError as e:
            return document({"seed": 0, "steps": steps, "kinds": {}, "files": [], "ours": {}, "trace": {"exited": False},
                             "problems": ["the trace of the run could not be taken: %s" % e]})
    finally:
        if stop_geo is not None:
            stop_geo()
        trace.cleanup()
        shutil.rmtree(work, ignore_errors=True)


def write_json(path: Path, document) -> None:
    Path(path).write_text(json.dumps(document, sort_keys=True, indent=1), encoding="utf-8")


def read_json(path: Path) -> dict:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("format") != FORMAT:
        raise SystemExit("%s is not an %s footprint" % (path, FORMAT))
    return document


# -
# The run of an engine as a subprocess (the seal a process runs under is read at import), and the orchestration.

def run_subprocess(binary: str, seal: Path | None, leg: str, out: Path, label: str, seeds: list, crash_with: str | None = None) -> int:
    """`run` in a process of its own, with a cache folder and a temp root of its own: they are the run's, so what the engine does in them is `ours`
    (the cache of the bundle's fonts, the profile and the temp dirs of the launch) and the rest of the machine is the host's.

    `crash_with` (a signal name) makes the run the recorder's own test: one launch, whose parent is sent that signal (`crash-probe`)."""
    env = dict(os.environ, INVISIBLE_CORE_AUTOFIX="off", INVISIBLE_CORE_PIN="allow")
    if seal:
        env["INVISIBLE_SEAL_FILE"] = str(seal)
    for name in ("MOZ_LOG", "MOZ_LOG_FILE"):
        env.pop(name, None)
    cmd = [sys.executable, "-u", str(Path(__file__).resolve()), "run", binary, "--leg", leg, "--out", str(out), "--label", label,
           "--seeds", ",".join(str(s) for s in seeds)]
    if crash_with:
        cmd += ["--crash-with", crash_with]
    try:
        with tempfile.TemporaryDirectory(prefix="ab-host-cache-") as cache, tempfile.TemporaryDirectory(prefix="ab-host-tmp-") as temp:
            env = oracle.own_cache_env(env, cache)
            env.update(TMPDIR=temp, TEMP=temp, TMP=temp)
            return subprocess.run(cmd, env=env, timeout=RUN_TIMEOUT_S).returncode
    except subprocess.TimeoutExpired:
        return 124


def run_crash_probe(binary: str, leg: str, seed: int, label: str, crash_dir=None, signame: str = "SIGSEGV") -> dict:
    """The recorder's own test: ONE launch of the engine whose parent process is sent a fatal signal with `os.kill` once the browser is up. The footprint's problems must
    name the code (`exit_problem`) and `crash` must hold what the launch left (`keep_crash`): a recorder that does not turn this into a red line with its evidence is broken."""
    import signal

    work = Path(tempfile.mkdtemp(prefix="ab-host-"))
    try:
        fp = run_seed(binary, seed, work, name="crash", crash_with=getattr(signal, signame))
        keep_crash(fp, "crash", crash_dir)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"format": FORMAT, "engine": label, "leg": leg, "platform": sys.platform, "seeds": [], "probe": fp, "signal": signame, "problems": list(fp.get("problems", []))}


def crash_probe(binary: str, leg: str, out: Path, seed: int, run=None) -> int:
    """`crash-probe`: the engine is launched once, its parent is sent SIGSEGV, and this reports what the oracle says of it. 1 is RED, which is what a launch that dies must
    be (the e2e test asserts it, with the evidence); 0 means the recorder did not see the crash (the test fails on that); 2 the probe itself did not run."""
    out.mkdir(parents=True, exist_ok=True)
    path = out / "footprint-crash.json"
    rc = (run or run_subprocess)(binary, None, leg, path, "crash probe", [seed], "SIGSEGV")
    if rc != 0 or not path.exists():
        print("FAIL the crash probe did not run: its process ended with %s" % rc)
        return 2
    document = read_json(path)
    for line in document["problems"]:
        print("FAIL " + line)
    exit_lines = [x for x in document["problems"] if x.startswith("the parent process exited with code")]
    print("AB HOST CRASH PROBE %s: %d problems, %d of them a parent that did not end with 0" % ("FAILED" if exit_lines else "OK", len(document["problems"]), len(exit_lines)))
    return 1 if exit_lines else 0


def run_stock_subprocess(leg: str, out: Path) -> int:
    """`stock` in a process of its own (its tree is the one of that process), with a temp root of its own."""
    env = dict(os.environ)
    for name in ("MOZ_LOG", "MOZ_LOG_FILE"):
        env.pop(name, None)
    cmd = [sys.executable, "-u", str(Path(__file__).resolve()), "stock", "--leg", leg, "--out", str(out)]
    try:
        with tempfile.TemporaryDirectory(prefix="ab-host-tmp-") as temp:
            env.update(TMPDIR=temp, TEMP=temp, TMP=temp)
            return subprocess.run(cmd, env=env, timeout=RUN_TIMEOUT_S).returncode
    except subprocess.TimeoutExpired:
        return 124


def run_stock_control(leg: str, out: Path) -> dict | None:
    """The stock run of the geolocation step and its verdict: the footprint, or None when the control is broken (said on the output)."""
    print("=== negative control of the geolocation step: Playwright's own Firefox")
    sys.stdout.flush()
    rc = run_stock_subprocess(leg, out / "footprint-stock.json")
    broken = ["the run of the stock control ended with %s" % rc] if rc != 0 or not (out / "footprint-stock.json").exists() else []
    stock = None
    if not broken:
        stock = read_json(out / "footprint-stock.json")
        broken = stock_control_problems(stock)
    if broken:
        print("\n".join("FAIL " + b for b in broken))
        print("BROKEN CONTROL: the geolocation step does not reach the platform provider on stock Firefox; the A/B says nothing")
        return None
    print("GEOLOCATION CONTROL: stock Firefox started %s; modules: %s" % (
        ", ".join(location_utility_kinds(stock["stock"]["kinds"])), ", ".join(location_modules(stock["stock"]["files"])) or "-"))
    return stock


def ab(candidate: str | None, candidate_seal: Path | None, leg: str, out: Path, engine_tag: str, seeds: list,
       published: tuple | None = None, expected_file: Path = EXPECTED_FILE) -> int:
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
            rc = run_subprocess(exe, Path(seal_file) if seal_file else None, leg, out / name, label, seeds)
            if rc != 0 or not (out / name).exists():
                failed.append("the run with %s ended with %s" % (label, rc))
        if failed:
            write_json(out / "diff-a-a.json", {"format": FORMAT, "mode": "a-a", "verdict": "red", "problems": failed,
                                               "differences": [], "declared": [], "stale": [], "leaves": {}})
            print("\n".join("FAIL " + f for f in failed))
            return 2
        first, again = (read_json(out / runs[0][1]), read_json(out / runs[1][1]))
        control = judge(first, again, a_a=True)
        write_json(out / "diff-a-a.json", control)
        print("--- A/A: %s against itself" % engine_tag)
        print_report(control, engine_tag, engine_tag + " again")
        if control["verdict"] != "green":
            print("BROKEN CONTROL: the published engine differs from itself on this machine; the A/B says nothing")
            return 2
        if not candidate:
            return 0
        if _windows():
            stock = run_stock_control(leg, out)
            if stock is None:
                return 2
        expected = load_expected(expected_file)
        report = judge(first, read_json(out / runs[2][1]), expected=expected, a_a=False, also_68=[again])
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
    r.add_argument("--crash-with", choices=["SIGSEGV"], default=None, help="the recorder's own test: one launch, its parent sent this signal (see crash-probe)")
    k = sub.add_parser("stock", help="the negative control of the geolocation step (Windows): Playwright's own Firefox")
    k.add_argument("--leg", required=True)
    k.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("compare", help="two footprints: the diff and the verdict")
    c.add_argument("a", type=Path)
    c.add_argument("b", type=Path)
    c.add_argument("--out", type=Path)
    c.add_argument("--a-a", action="store_true", help="the control: no declared difference is applied; a difference is a broken control (exit 2)")
    c.add_argument("--expected", type=Path, default=EXPECTED_FILE)
    q = sub.add_parser("crash-probe", help="the recorder's own test: one launch whose parent is sent SIGSEGV; exit 1 (red, with the evidence) is the right answer")
    q.add_argument("binary")
    q.add_argument("--leg", required=True)
    q.add_argument("--out", type=Path, required=True)
    q.add_argument("--seed", type=int, default=None)
    x = sub.add_parser("ab", help="A/A, then with a candidate the published engine against it")
    x.add_argument("candidate", nargs="?")
    x.add_argument("--candidate-seal", type=Path, default=os.environ.get("INVISIBLE_SEAL_FILE"))
    x.add_argument("--leg", required=True)
    x.add_argument("--out", type=Path, required=True)
    x.add_argument("--engine-tag", default=oracle.ENGINE_TAG)
    x.add_argument("--seeds", default=None)
    x.add_argument("--published-binary", type=Path, help="use this binary for the published engine instead of fetching it")
    x.add_argument("--published-seal", type=Path, help="its seal (default: the environment's)")
    args = ap.parse_args(argv)
    if args.cmd == "compare":
        a, b = read_json(args.a), read_json(args.b)
        report = judge(a, b, expected=None if args.a_a else load_expected(args.expected), a_a=args.a_a)
        if args.out:
            write_json(args.out, report)
        print_report(report, a.get("engine", "A"), b.get("engine", "B"))
        if report["verdict"] == "green":
            return 0
        return 2 if args.a_a else 1
    if args.cmd == "stock":
        if not _windows():
            print("the stock control is for Windows: Linux has no Windows location provider to reach")
            return 3
        write_json(args.out, run_stock(args.leg))
        return 0
    if args.cmd == "crash-probe":
        if not sys.platform.startswith("linux"):
            print("the crash probe sends a signal to the parent of the browser: Linux only")
            return 3
        return crash_probe(args.binary, args.leg, args.out, args.seed if args.seed is not None else default_seeds()[0])
    seeds = [int(s) for s in args.seeds.split(",")] if args.seeds else default_seeds()
    if args.cmd == "run":
        if sys.platform.startswith("linux") and not shutil.which("strace"):
            print("needs strace (apt install strace): without it the processes and the files of the run are not seen")
            return 3
        crash_dir = args.out.parent / (args.out.stem + ".crash")  # footprint-68.json -> footprint-68.crash/<launch>/: what a launch that did not end with 0 left
        if args.crash_with:
            document = run_crash_probe(args.binary, args.leg, seeds[0], args.label, crash_dir, args.crash_with)
        else:
            document = run_engine(args.binary, args.leg, seeds, args.label, crash_dir)
        write_json(args.out, document)
        return 0
    if args.candidate and not args.candidate_seal:
        ap.error("--candidate-seal (or INVISIBLE_SEAL_FILE) is required with a candidate: it runs under its own seal")
    given = None
    if args.published_binary:
        given = (str(args.published_binary), args.published_seal)
    return ab(args.candidate, Path(args.candidate_seal) if args.candidate_seal else None, args.leg, args.out, args.engine_tag, seeds, given)


if __name__ == "__main__":
    sys.exit(main())
