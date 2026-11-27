#!/usr/bin/env python3
"""Host-footprint A/B oracle: what the engine does to the HOST, over the whole of a closed run.

scripts/ci_ab_oracle.py compares what a PAGE reads. This compares what the engine does to the host, which a page cannot read but a release can
change (a helper that is no longer spawned, a path of the machine it did not touch before). It is not a photograph of one instant: it is the
TRACE of the run, from the launch to the exit of the browser (the quiet point is the exit of the process, not a wait), after the same short
journey (the first steps of the fake web's P4 walk, tests/fakeweb/profile_p4.py, no internet, driven by the stock Playwright client).

After the four steps of the journey comes a fifth: the geolocation permission is granted to a page of the oracle's own and `getCurrentPosition` is called and awaited
(the callback or the error, an event). On Windows the candidate must start NO utility process of the kind WINDOWS_UTILS (`-sandboxingKind 3`) and load none of the modules of
the platform's location provider (an absolute rule, `geolocation_problems`); the NEGATIVE CONTROL is Playwright's own Firefox on the same runner with the same step
(`run_stock`, command `stock`), which MUST read the host for the position: its tree must load `capabilityaccessmanagerclient.dll`, the FIRST read of the host that
Firefox makes for the position on Windows (`CheckAccess` of dom/geolocation/GeolocationSystemWin.cpp, the consent check that runs before any provider is created). If it
does not, the step does not reach the platform and the leg is a broken control (exit 2). The utility process of the provider (`-sandboxingKind 3`) is recorded and listed,
never required of the stock: on the same windows-2022 image it started in some runs and not in others (the runner has no location source and the provider answers
`code 2` at once), while the consent check ran in every one.
On Linux the same step runs and its footprint is compared like the rest.

Three things are compared, over the whole trace:

  * the processes, by KIND, from the command lines of the launch: the parent, `-contentproc` with its type, `-sandboxingKind N` and
    `-isForBrowser` / `-notForBrowser`, and the helpers (glxtest, pingsender, ...) by what they ran;
  * the files: every path the tree touched (opened, stat'ed, accessed, read as a link, executed), classified `ours` or `host`. ONE rule judges the host paths AND the kinds of process
    (`proc.<kind>`), in the A/A and in the A/B, on Linux and on Windows, the system modules and the shortcut inventory included (`file_rule`): a path, or a kind, is red if and only if it
    is in ALL the measured launches (the warm-up and the seeds) of one engine and in NO launch of the other (its idle launch included). A kind counts by its presence, never by how many
    processes of it a launch has (the content processes of type `tab` followed the idle time, and so does the start of Mozilla's pingsender). Every other path or kind that is not in the same
    share of the launches of both engines is LISTED, not judged: the log and the file `not-judged-a-a.json` / `not-judged.json` of the artifact give, per path and per kind, in how many launches
    of how many each engine has it (measured and long) and the processes that touched the path or spawned the kind, and mark `watch` the ones in at least half of the launches of one engine and
    in none of the other's. `ours` leaves the comparison as a class (OURS
    below: reading its own package is not touching the host);
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
helper spawned at the first shutdown of a cold machine) stays visible from run to run. The measured launches are compared as they are: a path or a kind that one engine has in all of them
and the other in none is red.

The exit pings (`firefox --backgroundtask pingsender`, through the session's route, never the separate `pingsender[.exe]`) leave only when Mozilla's `canUpload()` is
true (TelemetrySend.sys.mjs:1083), which the core's declared terms of use make true from the start; whether a launch starts them, and how many, varies from a launch to the next (run
45228081536: 3 in every launch of one run of the 68 and 3, 0, 2, 0, 3 in the other, the sockets they start in 3 launches and in none): their presence, their number, their types and what spawned them are listed, not judged (`listed_not_judged_kind`: run 45364818483, the A/A of the 68, had the socket of a pingsender in every launch of one run and in none of the other). The paths only they touch are paths like any other (`file_rule`); the destinations only they reach are compared when they are
there on both sides of a launch, and left out when they are there on one side only (a destination is `public, telemetry`: the telemetry host is the third party's, and
whether the connection succeeded is not in the verdict). The IDLE launch of each engine run (a page, no pref changed, up to 120 s) is evidence: which pings leave in a long launch is not a rule until it is proven (the core declares
the terms of use accepted, so `canUpload()` is true from the start and the notification of the data policy is never recorded; Linux started 1 pingsender in it, Windows 0).
Only a separate `pingsender[.exe]` there is red. Behind a session proxy a direct pingsender is the network oracle's.

ACCEPTED LOSSES (the rule of the host paths trades them for a verdict that does not follow the machine's timing; for the 69):
  * a path that one engine has in every measured launch and the other in only some of them is not red: always against sometimes is listed, never judged;
  * a reading tied to one seed is not judged: no path is compared seed by seed;
  * what happens only in the long launch (idle) is only listed: judging it would take two idle launches per engine;
  * the cause of the variable start of the pingsender is not known: whether a launch has it, how many, and what it spawns are listed, never red (a candidate whose pingsender starts in every launch where the 68's never does is not seen by the kinds; its paths and destinations are); no type of the pings is held, no preference for it;
  * how many processes of a kind a launch has is not compared (a candidate that spawns two gpu processes where the 68 spawns one is not seen): only that a kind is there in every launch of one
    engine and in none of the other's;

How the trace is taken:

  * Linux: `strace -f -y` of the whole tree (the stand-in of tests/_strace_stand_in.py), calls execve, clone, fork, prctl, the file calls
    (open, openat, stat, access, readlink, ...) and the network calls (socket, connect, bind, sendto, sendmsg, sendmmsg). The tree is named by
    what each process ran, or called itself when it forked without exec (the forkserver's children), and the log is read after the tree has exited.
    A socket is known by the inode `-y` prints for it (`5<socket:[1234]>`), so a send names the protocol of the socket() that made it.
  * Windows: the process starts WITH their command lines come from the Security log, event 4688 (the process-creation audit with the command
    line, switched on for the run and put back after it): the kernel writes one per process, so a short-lived child cannot be missed, which a
    poll of the job's pids can. The files come from an ETW session on Microsoft-Windows-Kernel-File (the Create events: every path a process
    opens, a library included) taken with `logman` and read with `tracerpt`, both part of Windows. It needs an administrator (the hosted runners
    are): a failure says so only when Windows answered "access denied" (0x80070005), any other failure says its command, its code and Windows' text.
    Every open is attributed to the life of its process in the same stream (Kernel-Process start and stop, matched to the 4688 by pid, creator's pid and image, in file order):
    Windows hands the pid of a process that ended to the next one (run 46058507264: the opens of the oracle's own `logman` and `wevtutil` were counted as a pingsender's and a tab's), so
    the opens of another life of a pid of the tree are listed (`<footprint>.audit/<launch>-<seed>.lives.json`) and never judged, and a life of a tool of the oracle counted as the tree's is red.
    The session of a launch is gone from the list of `logman query -ets` before the next launch may start. Two canary files of the oracle's own, opened after the session starts and before it stops, must be in the trace: a session that did not
    see them is red, not empty.

EVERY launch is also judged by how its parent ended (scripts/ci_crash_evidence.py): the warm-up, each measured seed and the idle launch alike. A root exit code other
than 0, or one that was not seen, is a RED line that names the seed, the run side, the launch and the code; the verdict of a closed run is not "the trace is equal" alone. A launch
that did not end with 0 keeps what it left in `<footprint>.crash/<launch>/`, next to the footprint (the artifact): the last 100 lines of the strace log and the fatal signal lines of
the whole log (`--- SIGSEGV {si_addr=...} ---`: the trace records the fatal signals now, `tests/_strace_stand_in.py` RECORDED_SIGNALS, and the lines are dropped where the log is read, so they
are never data of the comparison), the core run through `gdb -batch` (`thread apply all bt`, the MOZ_CRASH reason, `info sharedlibrary`) with the build ids, the crash reporter's `.extra` and
the minidump names, and `said.txt` for every collector that found nothing. Cores are enabled in every launch (`ulimit -c unlimited` in the stand-in; the working directory is not touched, the
job's `kernel.core_pattern` decides where the kernel writes them: INVPW_CORE_DIR), and a crash watcher reads the crash folders in every launch. On Windows the `.extra`
files and the minidump names are collected the same way. `crash-probe` is the recorder's own test (one launch, its parent sent SIGSEGV; red is the right answer, tests/test_ab_host_crash_linux_e2e.py).

    python ci_ab_host.py run <firefox> [--seal SEAL] --leg linux-x86_64 --out footprint.json [--seeds 11,12,13]
    python ci_ab_host.py crash-probe <firefox> --leg linux-x86_64 --out DIR
    python ci_ab_host.py compare A.json B.json --out diff.json [--a-a]
    python ci_ab_host.py ab [<candidate> --candidate-seal SEAL] --leg L --out DIR [--published-binary B [--published-seal S]]

`ab` without a candidate is the A/A control (the published engine twice). With a candidate: the A/A first, then the published engine against the
candidate. Exit 0 green, 1 red, 2 the control is broken (A/A differs: what is compared is not stable, the A/B says nothing). Nothing here reads
the clock for a verdict, and nothing excuses a difference: the A/A is green or the comparison is not stable.

Every engine run (the published engine, again, the candidate) is launched from the SAME absolute folder, `<RUNNER_TEMP>/ab-host/engine/firefox` (`staged_engine`: the package is copied
there and removed after the run): the browser's processes read the link of every ancestor of the install folder, so where the engine sits would otherwise be a difference of the paths.

Declared differences (ci/ab-host-expected.json) are applied to the A/B only, never to the A/A, by kind, and one that does not show is red as stale. A declared `proc` difference covers the
process kind and the file leaves whose only toucher on the published side is that kind (a leaf another process touched too is still a difference).

Evidence of the tree (Windows): every launch (the warm-up, each seed, the idle launch and the stock control) saves the raw events of the audit that its tree was read from, the 4688 births
and the 4689 ends, the excluded ones as well, in `<footprint>.audit/<launch>-<seed>.json` (`audit_listing`: record id, event, pid, creator's pid, image, `ParentProcessName`, time, command
line, whether it is in the tree and why), the lives of every pid of the tree (`<launch>-<seed>.lives.json`: each life with its creator, image, positions, exit code, whether it is the tree's and its
opens, and the opens dropped as another life's) and the session XML filtered to those pids and the Kernel-Process starts and stops (`<launch>-<seed>.etw.xml.gz`). They change no verdict and are not a leaf of the footprint.
"""
from __future__ import annotations

import argparse
import codecs
import contextlib
import datetime
import collections
import gzip
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
from ci_crash_evidence import CrashWatcher, crash_report_dirs  # noqa: E402  (the collectors every launch shares)

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
#: Our engine has no caller of it left (acceptance contract, item 4, of the engine's geolocation_windows_provider_never_selected.test.mjs). It is the RULE on our engine
#: (the candidate, and the 68 of the A/A), never the proof that the stock control reached the platform: the stock starts it only when the runner lets the provider run.
LOCATION_UTILITY_KIND = "3"
#: The first read of the host that Firefox makes for the position on Windows: `CheckAccess` (dom/geolocation/GeolocationSystemWin.cpp) loads the Capability Access Manager
#: client to ask the consent store, before the provider is created and whatever the provider then answers. This is the signal of the stock control. Measured on every footprint
#: with a stock control: it is in the tree (with or without the utility process `-sandboxingKind 3`) of 45165805370, 45170547415, 45252267809 (PR, windows-2022), 45206303882
#: (gate, windows-2025) and 45257636942 (gate republish, windows-2022); the utility process is in 3 of those 5 (45206303882 and 45257636942 have none). The engine, the 68 and the
#: 69 candidate, has neither in any launch (warm-up, idle, seeds) of 45121964904, 45206303882, 45257629988 and 45257636942.
LOCATION_FIRST_READ = "capabilityaccessmanagerclient.dll"
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
#: a launch that stays short may or may not have got there (the first run sends new-profile, first-shutdown and event; the shutdown ping waits for the second session,
#: TelemetrySession.sys.mjs:1011). Run 45228081536 (A/A of the 68, Windows): the pingsenders of the 68 were 3, 3, 3, 3, 3 in the seeds of one run and 3, 0, 2, 0, 3 in the other, and the
#: sockets they start (`socket<-pingsender`) 0 to 3: the start of Mozilla's pingsender is not the engine's to hold, so HOW MANY of them a launch has is listed, not judged.
PINGSENDER_KIND = "backgroundtask/pingsender"
PINGSENDER_EXES = ("pingsender", "pingsender.exe")
#: The pref the policy writes when the notification is recorded (TelemetryUtils.sys.mjs:80, TelemetryReportingPolicy.sys.mjs:1025): the profile's prefs.js says
#: it, a file of the launch's own, so the precondition of the pings is observed without any channel into the engine.
NOTIFIED_PREF = "datareporting.policy.dataSubmissionPolicyNotifiedTime"
#: The idle launch: how long it waits for the notification to be recorded (the first-run infobar is at 60 s, NOTIFICATION_DELAY_FIRST_RUN_MSEC, :51) and how
#: often it looks. A limit of acquisition: that the pref was seen is the verdict, never how long it took.
NOTIFY_LIMIT_S, NOTIFY_PERIOD_S = 120.0, 0.5


def is_pingsender(kind: str) -> bool:
    return kind == PINGSENDER_KIND or kind in PINGSENDER_EXES


def listed_not_judged_kind(kind: str) -> bool:
    """A kind whose PRESENCE is listed, never red: the pingsender of the engine (`backgroundtask/pingsender`) and what it spawns (`<kind><-pingsender`). Whether a launch
    starts one is Mozilla's `canUpload()`, not the engine's to hold, so the rule of every kind cannot hold it: run 45364818483 (A/A of the 68, Windows) had the socket of a
    pingsender in every launch of one run and in none of the other, and the control was broken. Their paths, their destinations and the separate `pingsender[.exe]` stay judged."""
    return kind == PINGSENDER_KIND or kind.endswith("<-pingsender")


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


#: The graphics probe the 68 spawns on Linux is called `gfxtest` (widget/gtk/GfxInfo.cpp:55 GFX_PROBE_BINARY; `gfxtest glx -f 18`), not `glxtest`: its kind is that name.
GFX_PROBE = "gfxtest"


def label_of_exec(path: str, argv: list, real: str) -> str:
    """What a process that ran `path` with `argv` is called: the engine's kinds, `glxtest` (argv0 or `-glxtest`), `gfxtest` (argv0 or the file it ran), else the
    program's name (lower case); a background task of the engine (`firefox.exe --backgroundtask pingsender ...`) is `backgroundtask/<name>`."""
    if "-contentproc" in argv:
        return kind_of(argv)  # whatever path it was started by (the binary, /proc/self/exe)
    for flag in ("--backgroundtask", "-backgroundtask"):
        if flag in argv:
            i = argv.index(flag)
            return "backgroundtask/%s" % (argv[i + 1].lower() if i + 1 < len(argv) else "?")
    if any(a.lstrip("-") == "glxtest" for a in argv[1:]) or _base(argv[0] if argv else "").lower() == "glxtest":
        return "glxtest"
    if GFX_PROBE in (_base(path).lower(), _base(argv[0] if argv else "").lower()):
        return GFX_PROBE
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
    """What is wrong with the exit pings of one launch: the separate executable ran. HOW MANY pingsenders a launch starts, of which type and spawned by whom, is not judged (it varies from
    a launch to the next): it is listed with the other kinds (`file_rule`)."""
    kinds = fp.get("kinds", {})
    return ["the separate %s ran (B266: the pings leave as `firefox --backgroundtask pingsender`, through the session's route)" % exe for exe in PINGSENDER_EXES if kinds.get(exe)]


def privileged_problems(fp: dict) -> list:
    """More than one preloaded about:newtab process in a launch (PRIVILEGED_REASON)."""
    n = fp.get("kinds", {}).get(PRIVILEGED, 0)
    return ["%d %s processes: the preloaded about:newtab is absent or present, never more than one (%s)" % (n, PRIVILEGED, PRIVILEGED_REASON)] if n > 1 else []


def pingsender_destinations(fp: dict) -> list:
    """The destinations of the pingsender kind in a launch: `<kind>: connect public, telemetry` (or `send`), whatever the address and the port: the telemetry host is the
    third party's."""
    return sorted({"%s: %s public, telemetry" % (PINGSENDER_KIND, e.split(": ", 1)[1].split(" ", 1)[0])
                   for e in fp.get("net") or [] if e.startswith(PINGSENDER_KIND + ": ")})


#: THE RULE OF THE HOST PATHS, one for the A/A and the A/B, Linux and Windows, over every host path (the system modules and the shortcut inventory included). A path is
#: RED if and only if it is in ALL the measured launches (the warm-up and the seeds) of one engine and in NO launch of the other (its long idle launch included). In
#: the A/A that is a broken control, in the A/B a difference (the declared ones apply to it as they always did). Every other path that is not in all the launches of both engines is
#: LISTED, not judged (`file_rule`, the file `not-judged[-a-a].json` of the artifact, the log): the paths that are not in the same share of the launches of both engines, with in
#: how many launches of how many each engine has them and the processes that touched them; the ones in at least half of the launches of one engine and in none of the other's are
#: marked `watch`.
NOT_JUDGED_FORMAT = "ab-host-not-judged/1"
NOT_JUDGED_REASON = ("a path, or a kind of process, in all the measured launches of one engine and in no launch of the other is red; the other paths and kinds that differ are listed, "
                     "not judged; `watch` is one in at least half of the launches of one engine and in none of the other's")
LISTED_IN_LOG = 50


def launches_of(fp: dict) -> list:
    """`[(name, measured, launch)]` of a footprint in the order of the run: the warm-up and the seeds (`measured`), then the idle launch (the long one)."""
    out = [("warmup", True, fp["warmup"])] if fp.get("warmup") else []
    out += [("s%s" % s["seed"], True, s) for s in fp.get("seeds", [])]
    return out + [(name, False, fp[name]) for name in ("idle",) if fp.get(name)]


def presence_table(fps: list, items_of, by_of) -> dict:
    """What the launches of ONE engine (the footprints of its runs) have of a set of things: `{"launches": {"measured": n, "long": n}, "seen": {thing: {"measured": k, "long": k}},
    "by": {thing: [who]}}`. `items_of(launch)` is the set of the things a launch has (host paths, kinds of process), `by_of(launch, thing)` who is to blame for it there."""
    launches, seen, by = {"measured": 0, "long": 0}, {}, {}
    for fp in fps:
        for _name, measured, launch in launches_of(fp):
            layer = "measured" if measured else "long"
            launches[layer] += 1
            for thing in items_of(launch):
                seen.setdefault(thing, {"measured": 0, "long": 0})[layer] += 1
                by.setdefault(thing, set()).update(by_of(launch, thing))
    return {"launches": launches, "seen": seen, "by": {thing: sorted(who) for thing, who in by.items()}}


def presence_rule(ta: dict, tb: dict, key: str, by_key: str, listed_only=lambda thing: False) -> tuple:
    """`(red, entries)` of two `presence_table`s by the rule above, whatever the things are. `red` is `{"A": [things only A has, in all its measured launches], "B": [...]}`; the
    entries are the other things that are not in the same share of the launches of both engines, `{key: thing, "watch", "A": {layer: [in n launches, of m]}, "B": ..., by_key: {"A": [who],
    "B": [who]}}`, the ones to watch first."""
    red = {side: sorted(p for p, c in mine["seen"].items()
                        if mine["launches"]["measured"] and c["measured"] == mine["launches"]["measured"] and p not in other["seen"] and not listed_only(p))
           for side, mine, other in (("A", ta, tb), ("B", tb, ta))}
    judged = set(red["A"]) | set(red["B"])
    every = {side: sum(t["launches"].values()) for side, t in (("A", ta), ("B", tb))}

    def same(seen: dict) -> bool:
        return all(seen["A"][layer] * tb["launches"][layer] == seen["B"][layer] * ta["launches"][layer] for layer in ("measured", "long"))

    entries = []
    for thing in sorted(set(ta["seen"]) | set(tb["seen"])):
        if thing in judged:
            continue
        seen = {side: t["seen"].get(thing, {"measured": 0, "long": 0}) for side, t in (("A", ta), ("B", tb))}
        if same(seen):
            continue  # in the same share of the launches of both engines (every one of them, for a thing that is there always): nothing to list
        watch = any(sum(seen[mine].values()) * 2 >= every[mine] and not sum(seen[other].values()) for mine, other in (("A", "B"), ("B", "A")))
        entries.append({key: thing, "watch": watch,
                        **{side: {layer: [seen[side][layer], t["launches"][layer]] for layer in ("measured", "long")} for side, t in (("A", ta), ("B", tb))},
                        by_key: {"A": ta["by"].get(thing, []), "B": tb["by"].get(thing, [])}})
    entries.sort(key=lambda e: (not e["watch"], e[key]))
    return red, entries


def judged_kinds(launch: dict) -> set:
    """The kinds of process of a launch that the rule looks at: every kind that ran, as a kind and not by how many of it there are (a kind of type tab is by its presence like
    the rest). Not here: the separate `pingsender[.exe]`, which is red where it is (`pingsender_problems`, `idle_problems`), and the preloaded about:newtab (PRIVILEGED_REASON:
    absent or present per launch, never more than one, never compared between the sides)."""
    return {kind for kind, n in launch.get("kinds", {}).items() if n and kind not in PINGSENDER_EXES and kind != PRIVILEGED}


def path_table(fps: list) -> dict:
    """The `presence_table` of the host paths of the launches of ONE engine, `"paths"` being what is `"seen"`."""
    table = presence_table(fps, lambda launch: set(launch.get("files", [])), lambda launch, path: (launch.get("touched_by") or {}).get(path, []))
    return dict(table, paths=table["seen"])


def kind_table(fps: list) -> dict:
    """The `presence_table` of the kinds of process of the launches of ONE engine (who is to blame for a kind: the kinds that spawned it)."""
    return presence_table(fps, judged_kinds, lambda launch, kind: (launch.get("spawned_by") or {}).get(kind, []))


def file_rule(a: dict, b: dict, a_a: bool, also_68=()) -> tuple:
    """`(red, not_judged)` of two footprints, by the rule above, over the host paths and over the KINDS of process (`proc.<kind>`, presence only: not how many of a kind a launch has).
    `red` is `{"files": {"A": [paths only A has, in all its measured launches], "B": [...]}, "kinds": {"A": [...], "B": [...]}}`. `also_68` are the other footprints of the engine of A in
    an A/B (the published engine measured twice): its launches are all of them the engine's. `not_judged` is the document of the file `not-judged[-a-a].json`: the paths and the kinds that
    differ and are not red."""
    fa, fb = [a, *also_68], [b]
    ta, tb = path_table(fa), path_table(fb)
    ka, kb = kind_table(fa), kind_table(fb)
    red_files, paths = presence_rule(ta, tb, "path", "touched_by")
    red_kinds, kinds = presence_rule(ka, kb, "kind", "spawned_by", listed_only=listed_not_judged_kind)
    return ({"files": red_files, "kinds": red_kinds},
            {"format": NOT_JUDGED_FORMAT, "mode": "a-a" if a_a else "a-b", "reason": NOT_JUDGED_REASON, "launches": {"A": ta["launches"], "B": tb["launches"]},
             "paths": paths, "kinds": kinds})


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


#: The shortcut inventory of Windows (a launch that ran it touches these): StartupOSIntegration.sys.mjs `onStartupIdle` (:165) -> `ensurePrivateBrowsingShortcutExists`
#: (:238, `hasPinnableShortcut` :266 -> nsWindowsShellService.cpp `HasPinnableShortcut` :1672 -> `FindPinnableShortcut` :1625) and `CustomIconManager.ensureAppliedOrRevert` ->
#: `enumerateInstallShortcuts` (CustomIconManager.sys.mjs:189 -> nsWindowsShellService.cpp `EnumerateInstallShortcutsImpl` :2960): Mozilla code at startup idle, which a short
#: launch may or may not have reached before it closed. The marker is a path of the places it walks. It is evidence (`inventory` of a launch): its paths are judged like any other (`file_rule`).
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
AUDIT_END_GUID = "{0CCE922C-69AE-11D9-BED3-505054503030}"  # the subcategory "Process Termination" (event 4689): when each process ended
AUDIT_KEY = r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System\Audit"
AUDIT_VALUE = "ProcessCreationIncludeCmdLine_Enabled"
FILE_PROVIDER, FILE_CREATE_KEYWORD = "Microsoft-Windows-Kernel-File", "0x80"  # KERNEL_FILE_KEYWORD_CREATE
PROCESS_PROVIDER, PROCESS_KEYWORD = "Microsoft-Windows-Kernel-Process", "0x10"  # WINEVENT_KEYWORD_PROCESS: starts and stops, with the exit code


ACCESS_DENIED = (5, 0x80070005)  # ERROR_ACCESS_DENIED, as a Win32 code and as an HRESULT: the only failure that says "it needs an administrator"
SESSION_GONE_LIMIT = 120.0  # the guard of the wait for a stopped session to leave the list of the sessions (a fence, not a pace: the wait is a condition read every 0.25 s)


class TraceError(RuntimeError):
    """The trace of a Windows run could not be taken (not an administrator, no audit, no session): said as such, never an empty trace. `code` is the exit
    code of the Windows tool that failed (None when there was none)."""

    def __init__(self, message, code=None) -> None:
        super().__init__(message)
        self.code = code

    @property
    def access_denied(self) -> bool:
        return self.code is not None and (self.code & 0xFFFFFFFF) in ACCESS_DENIED


def trace_not_started(e: TraceError) -> str:
    """The problem line of a trace that could not be started: what Windows said, and "it needs an administrator" only when Windows said access denied
    (run 45257629988, seed 6: `logman query` failed with 0x80071068, ERROR_WMI_GUID_NOT_FOUND, and the line blamed the administrator)."""
    return "the trace of a Windows run could not be started%s: %s" % (" (it needs an administrator)" if e.access_denied else "", e)


def parse_sessions(text: str) -> dict:
    """The sessions of `logman query -ets` as {name: status}: a header (Data Collector Set, Type, Status), a line of dashes, one row per session
    (columns apart by two spaces or more; a name can hold single spaces), then "The command completed successfully."."""
    sessions = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or set(line) == {"-"} or line.startswith("Data Collector Set") or line.startswith("The command completed"):
            continue
        parts = re.split(r"\s{2,}", line)
        if len(parts) < 3:
            parts = line.split()
        if len(parts) >= 2:
            sessions[parts[0]] = parts[-1]
    return sessions


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
    `[{"rid", "pid", "ppid", "image", "parent_image", "cmd", "time"}]`, `ppid` being the creator's pid and `parent_image` the image the audit says that creator runs
    ('' when the event does not carry it)."""
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
                    "image": data.get("NewProcessName", ""), "parent_image": data.get("ParentProcessName", ""), "cmd": data.get("CommandLine", ""), "time": when})
    return out


def parse_audit_ends(xml_text: str) -> list:
    """The process terminations of the same output (event 4689): `[{"rid", "pid", "image", "time"}]`, in the order of the log. A 4688 has no end in it: this is where the
    end of a process is read (the listing of the audit of a launch says when each process of the tree ended)."""
    body = re.sub(r"<\?xml[^>]*\?>", "", xml_text)
    root = ET.fromstring("<Events>%s</Events>" % body)
    out = []
    for ev in root:
        if next((e.text for e in ev.iter() if e.tag.rsplit("}", 1)[-1] == "EventID"), "") != "4689":
            continue
        data = {d.get("Name"): (d.text or "") for d in ev.iter() if d.tag.rsplit("}", 1)[-1] == "Data"}
        rid = next((int(e.text) for e in ev.iter() if e.tag.rsplit("}", 1)[-1] == "EventRecordID" and e.text), 0)
        when = next((e.get("SystemTime", "") for e in ev.iter() if e.tag.rsplit("}", 1)[-1] == "TimeCreated"), "")
        if "ProcessId" in data:
            out.append({"rid": rid, "pid": _number(data["ProcessId"]), "image": data.get("ProcessName", ""), "time": when})
    return out


def walk_tree(events, roots, ends=()) -> list:
    """Every event of the audit (the 4688 births of `parse_audit_events` and the 4689 ends of `parse_audit_ends`), in the order of the log (`rid`, monotone), each with what the
    tree made of it: `[{"event": 4688 or 4689, "record": the event, "in_tree": bool, "why": text}]`. `process_tree` is its births that are in the tree, and the audit file of a launch
    (`audit_listing`) is the whole of it, the excluded events included.

    Windows hands the pid of a process that ended to the next process it creates, so a pid is not a process: run 45221868374 (the idle launch of the 68) took into the tree
    `svchost -s wlidsvc` (a service the service manager started, never ended: the tree "did not exit within 120 s", the A/A was broken), 7 `wevtutil` and 2 `wsl` (the oracle's own
    tools), each born with a pid that an earlier process of the tree had had. The rule is the least that tells them apart (run 45228081536: the pingsender, born by the parent when
    the parent was already at its end, was left out of the tree by a rule on the end of the creator, and the A/A of the 68 was red on a process that is the engine's):
      - a process joins when its creator's pid is one of the tree (the creator was born in it) and, when both events say it, the image the audit gives for the creator is the image
        the creator was born with; the END of the creator (4689) does not exclude it: a child that is registered after the end of its creator is the creator's;
      - a pid of the tree stops being the tree's only when the same pid is BORN again as a process that does not join: the next birth of a pid is the process that has it;
      - the root joins by its own pid (its creator is the oracle); what came before it is not its tree (a creator's pid that Windows reused).
    The 4689 are read all the same: they say, in the listing, when each process of the tree ended."""
    roots = set(roots)
    floor = min((e.get("rid", 0) for e in events if e["pid"] in roots), default=0)
    born = {e["pid"] for e in events if e["pid"] in roots and e.get("rid", 0) >= floor}
    known = {pid: "" for pid in roots - born}  # a root with no birth in the log was born before the audit: of the tree from the start, its image unknown
    waiting = set(born)
    moments = sorted([(e.get("rid", 0), 0, i, e) for i, e in enumerate(events)] + [(x.get("rid", 0), 1, i, x) for i, x in enumerate(ends)], key=lambda m: m[:3])
    out = []
    for rid, is_end, _i, e in moments:
        if rid < floor:
            out.append({"event": 4689 if is_end else 4688, "record": e, "in_tree": False, "why": "before the birth of the root: not its tree"})
            continue
        if is_end:
            out.append({"event": 4689, "record": e, "in_tree": e["pid"] in known,
                        "why": "the end of a process of the tree" if e["pid"] in known else "the end of a process that is not in the tree"})
            continue
        creator = known.get(e["ppid"])
        if e["pid"] in waiting:
            waiting.discard(e["pid"])
            joined, why = True, "the root of the launch (its creator is the oracle)"
        elif e["ppid"] not in known:
            joined, why = False, "its creator (pid %d) is not a process of the tree: it is a stranger that has this pid" % e["ppid"]
        elif creator and e.get("parent_image") and _base(creator).lower() != _base(e["parent_image"]).lower():
            joined, why = False, "the creator the audit names (%s) is not the image pid %d was born with (%s)" % (_base(e["parent_image"]), e["ppid"], _base(creator))
        else:
            joined, why = True, "created by pid %d, a process of the tree" % e["ppid"]
        if joined:
            known[e["pid"]] = e["image"]
        else:
            known.pop(e["pid"], None)  # the pid is born again as a process that is not the tree's
        out.append({"event": 4688, "record": e, "in_tree": joined, "why": why})
    return out


def process_tree(events, roots, ends=()) -> list:
    """The events of `parse_audit_events` that are a root or were born from a process of the tree, in their order (the rule is in `walk_tree`)."""
    return [m["record"] for m in walk_tree(events, roots, ends) if m["event"] == 4688 and m["in_tree"]]


AUDIT_FORMAT = "ab-host-audit/1"


def audit_listing(events, roots, ends, launch: str, seed: int) -> dict:
    """The audit of one Windows launch as a document: every event the tree was read from, the excluded ones as well (`walk_tree`), each with its record id, event (4688 or 4689), pid,
    creator's pid, image, the image the audit names for the creator (`ParentProcessName`), time, and whether it is in the tree and why. Evidence for the next time a process is or
    is not in a tree: it changes no verdict and is never a leaf of the footprint."""
    rows = []
    for m in walk_tree(events, roots, ends):
        r = m["record"]
        rows.append({"rid": r.get("rid", 0), "event": m["event"], "pid": r["pid"], "ppid": r.get("ppid") if m["event"] == 4688 else None,
                     "image": r.get("image", ""), "parent_image": r.get("parent_image", ""), "time": r.get("time", ""), "cmd": (r.get("cmd") or "")[:300],
                     "in_tree": m["in_tree"], "why": m["why"]})
    return {"format": AUDIT_FORMAT, "launch": launch, "seed": seed, "roots": sorted(roots), "in_tree": sum(1 for r in rows if r["in_tree"]), "events": rows}


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


#: The Windows Overlay Filter (WOF) keeps a system file compressed (Compact OS) and serves its decompressed bytes from a cache; when they are not cached (the first access
#: after the boot) the open of the file is followed by an open of its stream `<file>:WofCompressedData`, the compressed data of the SAME file. That stream is not a path the
#: process chose: it is the file the process opened, read from where Windows keeps it. The footprint spells it as the file (the run 45043617981: 31 system paths only the first
#: run of the 68 had, all `<file>:wofcompresseddata`, while every one of the files was opened by all three runs). Any other stream (`:Zone.Identifier`, ...) is a path of its own.
WOF_STREAM = ":wofcompresseddata"


def wof_base(name: str) -> str:
    """`name` with the WOF compressed-data stream of a file taken off (the file itself), see WOF_STREAM; any other name is itself."""
    return name[:-len(WOF_STREAM)] if len(name) > len(WOF_STREAM) and name.lower().endswith(WOF_STREAM) else name


class Lives:
    """The lives of the processes of one ETW stream, built while `etw_files` reads it in order. A life is one process: Kernel-Process event 1 (start) opens a new life of its pid,
    event 2 (stop) closes it; Windows hands a pid that ended to the next process, so a pid has many lives in a stream. `items` are the lives (`{"pid", "ppid", "image", "base", "start",
    "stop", "code", "exit"}`, positions in the file and never times), `opens` the opens of the pids in `watch`, each `(pid, index of its life or None, file name, position, disposition or
    None)`: None is "no life seen" (no start of that pid came before the open, or its life was already closed)."""

    def __init__(self, watch=()) -> None:
        self.watch = set(watch)
        self.items: list = []
        self.current: dict = {}
        self.opens: list = []

    def start(self, pid: int, ppid: int, image: str, position: int) -> None:
        self.current[pid] = len(self.items)
        self.items.append({"pid": pid, "ppid": ppid, "image": image, "base": _base(image).lower(), "start": position, "stop": None, "code": None, "exit": None})

    def stop(self, pid: int, code: int, position: int, exit_record=None) -> None:
        index = self.current.pop(pid, None)
        if index is not None:
            self.items[index].update(stop=position, code=code, exit=exit_record)

    def opened(self, pid: int, name: str, position: int, disposition) -> None:
        self.opens.append((pid, self.current.get(pid), name, position, disposition))


def tree_lives(lives: Lives, tree) -> tuple:
    """`({index of a life: the 4688 of the tree it is}, [problem])`: a life is the tree's when its pid, its creator's pid and its image's base name (case-insensitive) are those of a
    4688 of the tree (`process_tree`); two 4688 and two lives of the same three are matched in order. A 4688 of the tree with no life in the stream is a problem that names its pid: the
    attribution of its opens cannot be made without it."""
    by_key: dict = {}
    for i, life in enumerate(lives.items):
        by_key.setdefault((life["pid"], life["ppid"], life["base"]), []).append(i)
    taken: dict = {}
    matched: dict = {}
    problems = []
    for e in tree:
        key = (e["pid"], e.get("ppid"), _base(e["image"]).lower())
        n = taken.get(key, 0)
        if n < len(by_key.get(key, ())):
            matched[by_key[key][n]] = e
            taken[key] = n + 1
        else:
            problems.append("the file trace has no life (Kernel-Process start) of the process of the tree pid %d (%s, created by pid %s): its opens cannot be attributed" % (
                e["pid"], _base(e["image"]), e.get("ppid")))
    return matched, problems


def oracle_tool_problems(lives: Lives, matched: dict, tools) -> list:
    """The problems of the lives counted as the tree's that are a tool of the oracle (`tools`: {image base name, lower case: the command the oracle ran}, see `ORACLE_TOOLS`): a life of
    `logman`, `wevtutil`, `tracerpt`, `auditpol` or `reg` is never the engine's, whatever way it came into the tree (run 45221868374: `walk_tree` took 7 `wevtutil` into it)."""
    out = []
    for index, record in sorted(matched.items()):
        life = lives.items[index]
        if life["base"] in tools:
            out.append("pid %d (%s, %s) is counted in the tree and is a tool the oracle runs (`%s`): a life of an oracle tool is never the engine's" % (
                life["pid"], life["base"], (record.get("cmd") or "")[:200], tools[life["base"]]))
    return out


def attribute_opens(lives: Lives, tree, tools=()) -> dict:
    """What `lives` says of the opens of the processes of `tree` (`tree_lives`): `{"files": {(pid, name)}, "order": {(pid, name): position}, "dispositions": {(pid, name): {n}},
    "missing": [pid of a life of the tree with no open], "dropped": [{"pid", "life", "file"}], "lives": [one row per life of a pid of the tree], "problems": [...]}`. Only an open inside
    a life of the tree is the tree's; an open of a pid of the tree outside it (another life of the pid, or none seen) is dropped and listed, never judged. `foreign_exits` are the
    stop records (`etw_files`' `exits`) of the lives of those pids that are not the tree's."""
    matched, problems = tree_lives(lives, tree)
    problems = problems + oracle_tool_problems(lives, matched, tools)
    pids = {e["pid"] for e in tree}
    files: set = set()
    order: dict = {}
    dispositions: dict = {}
    counted: dict = {}
    dropped: dict = {}
    seen_in: dict = {}
    for pid, index, name, position, disposition in lives.opens:
        if pid not in pids:
            continue
        seen_in[index] = seen_in.get(index, 0) + 1
        if index in matched:
            files.add((pid, name))
            order.setdefault((pid, name), position)
            if disposition is not None:
                dispositions.setdefault((pid, name), set()).add(disposition)
            counted.setdefault(index, set()).add(name)
        else:
            dropped.setdefault((pid, index, name), {"pid": pid, "life": lives.items[index]["image"] if index is not None else "no life seen", "file": name})
    rows = []
    for index, life in enumerate(lives.items):
        if life["pid"] in pids:
            rows.append({"pid": life["pid"], "ppid": life["ppid"], "image": life["image"], "start": life["start"], "stop": life["stop"], "code": life["code"],
                         "tree": index in matched, "opens": len(counted.get(index, ())) if index in matched else seen_in.get(index, 0)})
    if seen_in.get(None):
        rows.append({"pid": None, "ppid": None, "image": "no life seen", "start": None, "stop": None, "code": None, "tree": False, "opens": seen_in[None]})
    missing = sorted({lives.items[i]["pid"] for i in matched if i not in counted})
    foreign = [life["exit"] for index, life in enumerate(lives.items) if life["pid"] in pids and index not in matched and life["exit"] is not None]
    return {"files": files, "order": order, "dispositions": dispositions, "missing": missing, "dropped": list(dropped.values()), "lives": rows, "problems": problems,
            "foreign_exits": foreign}


EVENT_NS = "http://schemas.microsoft.com/win/2004/08/events/event"
ET.register_namespace("", EVENT_NS)


def etw_files(path, pids=None, exits=None, order=None, dispositions=None, lives=None, evidence=None) -> tuple:
    """`({(pid, file name)}, events)` of the Create events a `tracerpt -of XML` file holds, for the processes in `pids` (every process when None). Read as a
    stream: the file has every open of the machine for the length of the run. With a list in `exits`, the process stops of the same file (Kernel-Process, event 2)
    are appended to it as `{"pid", "code", "time", "image"}`: how each process ended; with a dict in `order`, the position in the file (the order the system
    logged them in, never a time) of the first open of each `(pid, name)` is put in it. With a `Lives` in `lives`, the starts and stops of the processes (Kernel-Process, events 1 and 2)
    make the lives of the pids, and the opens of its `watch` are each put in the life their pid has at that position (`attribute_opens`). With a text file in `evidence`, the events of
    those pids and every start and stop are written to it as they are read (the XML of the session, filtered)."""
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
        if name:
            name = wof_base(name)
        is_start = event_id == "1" and "ParentProcessID" in data and bool(data.get("ProcessID"))
        is_stop = event_id == "2" and "ExitCode" in data and bool(data.get("ProcessID"))
        stop = {"pid": _number(data["ProcessID"]), "code": _number(data["ExitCode"]), "time": when, "image": data.get("ImageName", "")} if is_stop else None
        if exits is not None and is_stop:
            exits.append(stop)
        if lives is not None:
            if is_start:
                lives.start(_number(data["ProcessID"]), _number(data["ParentProcessID"]), data.get("ImageName", ""), events)
            elif is_stop:
                lives.stop(stop["pid"], stop["code"], events, stop)
            elif name and pid is not None and pid in lives.watch:
                lives.opened(pid, name, events, _number(data["CreateOptions"]) >> 24 if data.get("CreateOptions") else None)
        if evidence is not None and (is_start or is_stop or (lives is not None and pid in lives.watch)):
            evidence.write(ET.tostring(el, encoding="unicode"))
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


#: The tools the oracle itself ran through `_tool`, by the base name of the image each one is (`logman.exe`, lower case): {image: the first command run}. A life of one of them is never the
#: engine's (`oracle_tool_problems`).
ORACLE_TOOLS: dict = {}


def tool_image(argv0) -> str:
    """The image base name (lower case, with its extension) the Windows tool `argv0` runs as: `logman` is `logman.exe`."""
    base = _base(str(argv0)).lower()
    return base if "." in base else base + ".exe"


def _tool(argv, timeout: float = 300.0) -> str:
    """A Windows tool's output, or a TraceError that says what failed."""
    ORACLE_TOOLS.setdefault(tool_image(argv[0]), " ".join(str(a) for a in argv)[:200])
    try:
        done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TraceError("%s: %s" % (" ".join(str(a) for a in argv), e))
    if done.returncode != 0:
        code = done.returncode & 0xFFFFFFFF
        raise TraceError("%s failed with 0x%08X (%d): %s" % (" ".join(str(a) for a in argv), code, code, (done.stdout + done.stderr).strip()[:500]), code)
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
        self.ends_ok = False
        self.before_end = None
        self.etw_evidence = None  # the session XML of the last `finish`, filtered to the pids of the tree (`save_lives`)
        self.audit_raw: tuple = ([], [])  # the births and the ends of the last read of the log (`audit_listing`)

    def _canary(self, which: str) -> str:
        """A file of the oracle's own, written and read now: the session must have a Create event of it."""
        path = self.work / ("canary-%s-%s" % (which, self.name))
        path.write_text(which, encoding="utf-8")
        path.read_text(encoding="utf-8")
        return str(path)

    def start(self, sleep=time.sleep, now=time.monotonic, limit=SESSION_GONE_LIMIT) -> None:
        self.before = {"setting": parse_audit_setting(_tool(["auditpol", "/get", "/subcategory:" + AUDIT_GUID, "/r"])),
                       "value": self._registry_value()}
        _tool(["auditpol", "/set", "/subcategory:" + AUDIT_GUID, "/success:enable"])
        try:  # the end of each process (4689): without it the tree still tells a pid taken by a stranger by its birth, and the run says so, it is not red
            end_setting = parse_audit_setting(_tool(["auditpol", "/get", "/subcategory:" + AUDIT_END_GUID, "/r"]))
            _tool(["auditpol", "/set", "/subcategory:" + AUDIT_END_GUID, "/success:enable"])
            self.before_end, self.ends_ok = end_setting, True
        except TraceError:
            self.ends_ok = False
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
        # with a round trip to the session between them, and it is the LAST one, written just before the launch, that must be in the trace. The round trip is the
        # LIST of the sessions, not the query of this one: `logman query <name>` resolves the GUIDs of its providers through WMI and failed with 0x80071068
        # (ERROR_WMI_GUID_NOT_FOUND) in run 45257629988, seed 6, side A, after `logman create` had succeeded.
        for n in (1, 2, 3):
            self._expect_running(sleep, now, limit)
            self._start_canary = self._canary("start%d" % n)

    def _sessions(self) -> dict:
        return parse_sessions(_tool(["logman", "query", "-ets"]))

    def _wait_sessions(self, done, timed_out, sleep, now, limit) -> None:
        """Reads the list of the sessions every 0.25 s until `done(sessions)` is true, or the guard `limit` runs out (then `timed_out(last list, last read error or None)` is the TraceError: the error when the last read failed, the list when it did not). A read that fails (4200 in run 45257629988, or any error) is read again: it is a read of a state, it changes nothing."""
        deadline = now() + limit
        sessions = error = None
        while True:
            try:
                sessions, error = self._sessions(), None
            except TraceError as e:
                error = e
            if error is None and done(sessions):
                return
            if now() >= deadline:
                raise timed_out(sessions, error)
            sleep(0.25)

    def _expect_running(self, sleep=time.sleep, now=time.monotonic, limit=SESSION_GONE_LIMIT) -> None:
        """The round trip to the session: it is in the list and it is running, or the launch does not start (the guard `limit` is the fence)."""
        def timed_out(sessions, error):
            if error is not None:
                return TraceError("the list of the sessions (`logman query -ets`) could not be read in %g s, the last error: %s" % (limit, error), getattr(error, "code", None))
            return TraceError("the session %s is not running in the list of `logman query -ets` (it says %s); the sessions read: %s" % (
                self.name, repr(sessions[self.name]) if self.name in sessions else "nothing of it",
                ", ".join("%s (%s)" % kv for kv in sorted(sessions.items())) or "none"))

        self._wait_sessions(lambda sessions: sessions.get(self.name, "").lower() == "running", timed_out, sleep, now, limit)

    def _stop_session(self, sleep=time.sleep, now=time.monotonic, limit=SESSION_GONE_LIMIT) -> None:
        """Stops the session and returns when it is gone from the list of the sessions: the next launch starts with no session of this one. A `logman stop`
        that fails because the session is no longer there (4201, ERROR_WMI_INSTANCE_NOT_FOUND) is not an error when the list confirms it is not; while the
        list says it is still there, the error of `stop` is the error."""
        failed = None
        try:
            _tool(["logman", "stop", self.name, "-ets"])
        except TraceError as e:
            failed = e

        def gone(sessions):
            if self.name in sessions and failed is not None:
                raise failed
            return self.name not in sessions

        def timed_out(sessions, error):
            if error is not None:
                return TraceError("the list of the sessions (`logman query -ets`) could not be read in %g s after `logman stop`, the last error: %s" % (limit, error), getattr(error, "code", None))
            return TraceError("the session %s is still in the list of `logman query -ets` %g s after `logman stop` (it says %s)" % (self.name, limit, repr(sessions[self.name])))

        self._wait_sessions(gone, timed_out, sleep, now, limit)

    def _registry_value(self):
        try:
            out = _tool(["reg", "query", AUDIT_KEY, "/v", AUDIT_VALUE])
        except TraceError:
            return None
        m = re.search(r"REG_DWORD\s+(0x[0-9a-fA-F]+)", out)
        return int(m.group(1), 16) if m else None

    def tree(self, roots) -> list:
        xml = _tool(["wevtutil", "qe", "Security", "/q:*[System[(EventID=4688 or EventID=4689) and (EventRecordID>%d)]]" % self.first_record, "/f:xml"])
        events = parse_audit_events(xml)
        self.images = {e["pid"]: _base(e["image"]).lower() for e in events}
        ends = parse_audit_ends(xml)
        self.audit_raw = (events, ends)
        return process_tree(events, roots, ends)

    def finish(self, roots, alive=None, sleep=time.sleep, now=time.monotonic, namer=None, gone_limit=SESSION_GONE_LIMIT, create_time=None) -> dict:
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
        tree, exited = wait_exited(lambda: self.tree(roots), alive, sleep=sleep, now=now, read_ends=lambda: self.audit_raw[1], create_time=create_time or process_create_time)
        events, ends = self.audit_raw
        audit = (events, ends, sorted(roots))
        end_canary = self._canary("end")
        self._stop_session(sleep, now, gone_limit)
        self.session = False
        summary = self.work / "files.txt"
        _tool(["tracerpt", str(self.etl), "-of", "XML", "-o", str(self.xml), "-summary", str(summary), "-y"])
        exits: list = []
        tree_pids = {e["pid"] for e in tree}
        lives = Lives(tree_pids)
        self.etw_evidence = self.work / "files.etw.xml.gz"
        try:
            with gzip.open(self.etw_evidence, "wt", encoding="utf-8") as evidence:
                evidence.write("<Events>")
                everything, events = etw_files(self.xml, None, exits, None, None, lives, evidence)
                evidence.write("</Events>")
        except OSError as e:  # the evidence is never red: a file that cannot be written is said, the trace is read as before
            print("the filtered session XML could not be written (%s): %s" % (self.etw_evidence, e))
            self.etw_evidence = None
            exits.clear()
            lives = Lives(tree_pids)
            everything, events = etw_files(self.xml, None, exits, None, None, lives)
        got = attribute_opens(lives, tree, ORACLE_TOOLS)
        seen = {(p, n) for p, n in everything if p == os.getpid()} | got["files"]
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
        problems.extend(got["problems"])
        if got["missing"]:
            problems.append("the file trace holds no open of the process(es) %s" % ", ".join(str(p) for p in got["missing"][:10]))
        foreign = {id(x) for x in got["foreign_exits"]}
        return {"tree": tree, "audit": audit, "files": got["files"], "outside": {k: len(v) for k, v in sorted(outside.items())},
                "exited": exited, "events": events, "problems": problems, "exits": [e for e in exits if e["pid"] in tree_pids and id(e) not in foreign],
                "exits_available": self.exits_ok, "order": got["order"], "dispositions": {k: sorted(v) for k, v in got["dispositions"].items()},
                "lives": {"lives": got["lives"], "dropped": got["dropped"]}, "etw_evidence": self.etw_evidence}

    def cleanup(self, sleep=time.sleep, now=time.monotonic, gone_limit=SESSION_GONE_LIMIT) -> None:
        """Put the machine back: the session closed and gone from the list, the audit as it was."""
        if self.session:
            try:
                self._stop_session(sleep, now, gone_limit)
            except TraceError:
                pass
            self.session = False
        if self.before is None:
            return
        flags = {"Success": ("enable", "disable"), "Failure": ("disable", "enable"), "Success and Failure": ("enable", "enable")}
        success, failure = flags.get(self.before["setting"], ("disable", "disable"))
        try:
            if self.ends_ok:
                end_success, end_failure = flags.get(self.before_end, ("disable", "disable"))
                _tool(["auditpol", "/set", "/subcategory:" + AUDIT_END_GUID, "/success:" + end_success, "/failure:" + end_failure])
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

#: A process whose `create_time` is later than the birth the audit gives it by more than this is not that process: the audit writes the 4688 when the process is made, so a
#: process that is the audit's cannot have been created after it.
BIRTH_SLACK_S = 1.0


def audit_epoch(text: str):
    """The seconds since the epoch of a `SystemTime` of the audit (`2026-11-24T05:05:33.1812345Z`, UTC), or None when it is missing or not a time."""
    import datetime

    m = re.match(r"^\s*(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z?\s*$", text or "")
    if not m:
        return None
    base = datetime.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()
    return base + (float("0." + m.group(2)) if m.group(2) else 0.0)


def process_has_ended(event, ends=(), create_time=None) -> bool:
    """Whether the process the audit BORE as `event` (a 4688 of the tree) has ended, whatever `pid_exists` says of its pid: Windows hands the pid of an ended process to the next
    one it makes (run 45328762166: pid 7352 of the tree ended at 05:05:33, was `TiWorker.exe` at 05:05:38, a service that lives minutes, and the wait for the exit saw the tree alive
    for 120 s). A pid is the process of the tree only while it is THAT process:
      - the audit has a 4689 of the pid after the 4688 (by record id, by time when a record id is missing): it ended;
      - no 4689: the process the pid names now (`create_time(pid)`, seconds since the epoch, None when there is none) was created after the birth the audit gives (`BIRTH_SLACK_S`):
        it is another process that has the pid, and the one of the tree ended;
      - otherwise (no way to tell: no time, no `create_time`) it is the same process, and it is alive."""
    pid, rid, born = event["pid"], event.get("rid", 0), audit_epoch(event.get("time", ""))
    for end in ends:
        if end["pid"] != pid:
            continue
        if rid and end.get("rid", 0):
            if end["rid"] > rid:
                return True
        elif born is not None and (audit_epoch(end.get("time", "")) or 0.0) >= born:
            return True
    if born is not None and create_time is not None:
        try:
            created = create_time(pid)
        except Exception:  # noqa: BLE001 - a process we cannot read says nothing: it is not made to look ended
            return False
        if created is not None and created > born + BIRTH_SLACK_S:
            return True
    return False


def process_create_time(pid: int):
    """`create_time` of the process that has `pid` now (psutil), None when no process has it."""
    import psutil

    try:
        return psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return None


def wait_exited(read_tree, alive, period=EXIT_PERIOD_S, limit_s=EXIT_LIMIT_S, sleep=time.sleep, now=time.monotonic, read_ends=None, create_time=None) -> tuple:
    """Reads the tree of the run (`read_tree()` returns the events of its processes, in their order) until every process of it is gone and two reads in a row say the same thing,
    or until `limit_s`. A process is gone when `alive(pid)` says its pid is free, or when the pid is no longer the process of the tree (`process_has_ended`: `read_ends()` are the 4689
    of the same read, `create_time(pid)` is the creation time of the process that has the pid now). Returns (the last tree, {"exited": bool, "reads": n}): a tree that was
    still running at the limit says so, it is not made to look quiet."""
    end = now() + limit_s
    previous, reads = None, 0
    while True:
        tree = read_tree()
        reads += 1
        pids = tuple(e["pid"] for e in tree)
        ends = read_ends() if read_ends is not None else ()
        if not any(alive(e["pid"]) and not process_has_ended(e, ends, create_time) for e in tree) and pids == previous:
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
    """{leaf: value} of one seed: net.<kind>: <what> = 1 for every netlink socket, netlink request and public destination of a process of that kind (`network_events`). Neither the
    host paths nor the kinds of process are leaves of a seed: both are judged over all the launches (`file_rule`)."""
    out = {}
    for entry in seed_footprint.get("net") or []:
        if not entry.startswith(PINGSENDER_KIND + ": "):
            out["net." + net_class(entry)] = 1
    if with_pingsender:
        for entry in pingsender_destinations(seed_footprint):
            out["net." + entry] = 1
    return out


def _kind_value(kind: str, n: int):
    """What a red kind of process shows in a seed: how many of it the seed has (1 for a kind of type tab: its presence)."""
    return 1 if is_tab(kind) and n else n


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


def _declared_covers(entry: dict, diff: dict, probe_seeds=()) -> bool:
    """A declared difference covers, by kind (a `proc` entry, the name of the process):

      * the leaf `proc.<name>` that is the published engine having it (count >= 1) and the candidate not having it;
      * a file leaf of a seed in `probe_seeds` (the seeds where that leaf above differs) that only the published engine has and whose ONLY toucher on that side is that kind:
        what a process the candidate no longer spawns alone touched goes with it. A leaf another process touched too stays a difference, and so does a leaf the
        candidate touches or one the candidate alone has."""
    if entry.get("category") != "proc":
        return False
    published, candidate = diff["a"], diff["b"]
    if diff["leaf"] == "proc." + entry["name"]:
        return (published != MISSING and published >= 1) and candidate == MISSING
    by = diff.get("by") or {}
    return (diff["category"] == "file" and diff["seed"] in probe_seeds and published != MISSING and candidate == MISSING
            and by.get("A") == [entry["name"]] and not by.get("B"))


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


def location_first_reads(paths) -> list:
    """The paths (host paths of a footprint, either separator) of the module that is the first read of the host for the position (`LOCATION_FIRST_READ`)."""
    return sorted(path for path in paths if _base(path).lower() == LOCATION_FIRST_READ)


def location_modules(paths) -> list:
    """The paths (host paths of a footprint, either separator) of a module the Windows location provider loads."""
    out = []
    for path in paths:
        name = _base(path).lower()
        if name in LOCATION_MODULES or name.startswith(LOCATION_MODULE_PREFIXES):
            out.append(path)
    return sorted(out)


def geolocation_problems(seed_footprint: dict, where: str, side: str = "B") -> list:
    """What a launch of OUR engine did to the host that only the platform's location check and provider do: a process of the kind `-sandboxingKind 3` and the modules
    they load, `capabilityaccessmanagerclient.dll` (the first read of the host for the position) among them. Absolute (it does not read the other engine): the engine
    answers from the core's declaration and asks nobody. `where` names the launch (`seed 11`, `warm-up`, `idle`), the message the module."""
    problems = []
    kinds = location_utility_kinds(seed_footprint.get("kinds", {}))
    if kinds:
        problems.append("%s side %s: the engine started the Windows location utility process (-sandboxingKind %s): %s"
                        % (where, side, LOCATION_UTILITY_KIND, ", ".join(kinds)))
    modules = location_modules(seed_footprint.get("files", []))
    if modules:
        problems.append("%s side %s: the engine loaded location modules: %s%s" % (
            where, side, ", ".join(modules[:10]),
            " (%s is the first read of the host that Firefox makes for the position: the engine asked the platform)" % LOCATION_FIRST_READ if location_first_reads(modules) else ""))
    return problems


def stock_control_problems(stock: dict) -> list:
    """What makes the stock control BROKEN (exit 2, the A/B says nothing): its own trace or step failed, or stock Firefox, given the permission and the
    same request, did not load `capabilityaccessmanagerclient.dll` (the first read of the host for the position), so the step does not reach the platform. The utility
    process of the provider is not asked of it: on the same image it started in some runs and not in others (see LOCATION_FIRST_READ for the runs)."""
    seed = stock.get("stock") or {}
    problems = ["stock control: %s" % p for p in seed.get("problems", [])]
    problems += ["stock control: step %s (%s) failed: %s" % (st.get("n"), st.get("name"), st.get("error")) for st in seed.get("steps", []) if not st.get("ok")]
    if not seed:
        problems.append("stock control: the run made no footprint")
    elif seed.get("started", True) and not location_first_reads(seed.get("files", [])):
        # only a stock Firefox that RAN says anything about the position: one that never started (not installed, did not launch) is a different broken control
        problems.append("stock control: stock Firefox did not load %s (the first read of the host for the position, the consent check of the Windows location code) after "
                        "getCurrentPosition with the permission granted: the step does not reach the platform, so a candidate without that read proves nothing"
                        % LOCATION_FIRST_READ)
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
        ka, kb = judged_kinds(seeds_a[seed]), judged_kinds(seeds_b[seed])
        for side, fp in (("A", seeds_a[seed]), ("B", seeds_b[seed])):
            for message in fp.get("problems", []) + pingsender_problems(fp) + privileged_problems(fp):
                problems.append("seed %s side %s: %s" % (seed, side, message))
        per = {c: 0 for c in CATEGORIES}
        for leaf in set(la) | set(lb):
            per[category_of(leaf)] = per.get(category_of(leaf), 0) + 1
        per["proc"] = len(ka | kb)
        per["file"] = len(set(seeds_a[seed].get("files", [])) | set(seeds_b[seed].get("files", [])))
        counts[str(seed)] = per
        for side, leaves, kinds, files in (("A", la, ka, seeds_a[seed].get("files", [])), ("B", lb, kb, seeds_b[seed].get("files", []))):
            have = {c: sum(1 for k in leaves if category_of(k) == c) for c in floors}
            have["proc"], have["file"] = len(kinds), len(files)
            for c, floor in floors.items():
                if have[c] < floor:
                    problems.append("seed %s side %s has %d %s leaves, fewer than the floor of %d: the run was not seen"
                                    % (seed, side, have[c], c, floor))
        for leaf in sorted(set(la) | set(lb)):
            va, vb = la.get(leaf, MISSING), lb.get(leaf, MISSING)
            if va != vb:
                differences.append({"seed": seed, "category": category_of(leaf), "leaf": leaf, "a": _clip(va), "b": _clip(vb),
                                    "by": {"A": _by(seeds_a[seed], leaf), "B": _by(seeds_b[seed], leaf)},
                                    "calls": {"A": _calls(seeds_a[seed], leaf), "B": _calls(seeds_b[seed], leaf)}})
    red, not_judged = file_rule(a, b, a_a, also_68 or ())
    for side, seeds in (("A", seeds_a), ("B", seeds_b)):
        other = {"A": "B", "B": "A"}[side]
        for category, names in (("file", red["files"][side]), ("proc", red["kinds"][side])):
            for name in names:
                leaf = "%s.%s" % (category, name)
                for seed in sorted(seeds):
                    value = 1 if category == "file" else _kind_value(name, seeds[seed].get("kinds", {}).get(name, 0))
                    differences.append({"seed": seed, "category": category, "leaf": leaf, "a": value if side == "A" else MISSING, "b": value if side == "B" else MISSING,
                                        "by": {side: _by(seeds[seed], leaf), other: []}, "calls": {side: _calls(seeds[seed], leaf), other: []}})
    differences.sort(key=lambda d: (d["seed"], d["leaf"]))
    if os_name == "windows":
        # The rule is on every launch of OUR engine (the seeds, the warm-up and the idle one): the candidate in the A/B; in the A/A the 68 on both sides (it has neither the
        # module nor the process in any launch measured, 45121964904, 45206303882, 45257629988, 45257636942), so a published engine that starts asking the platform is red
        # before the candidate is looked at. In the A/B side A is the published engine, which the A/A of the same job has judged.
        for side, fp_side, seeds_side in ((("A", a, seeds_a), ("B", b, seeds_b)) if a_a else (("B", b, seeds_b),)):
            launches = [("seed %s" % seed, seeds_side[seed]) for seed in sorted(seeds_side)]
            launches += [(label, fp_side[name]) for name, label in (("warmup", "warm-up"), ("idle", "idle")) if fp_side.get(name)]
            for where, fp in launches:
                problems += geolocation_problems(fp, where, side)
    problems += idle_problems(a, b)
    wa, wb = a.get("warmup") or {}, b.get("warmup") or {}
    if wa.get("kinds", {}).get(PINGSENDER_KIND) and wb.get("kinds", {}).get(PINGSENDER_KIND):
        x, y = pingsender_destinations(wa), pingsender_destinations(wb)
        if x != y:
            problems.append("warm-up: the pingsender destinations differ: A only %s | B only %s" % (sorted(set(x) - set(y))[:5], sorted(set(y) - set(x))[:5]))
    stale = []
    if not a_a:
        for entry in expected or []:
            if entry.get("os") != os_name:
                continue
            probe_seeds = {d["seed"] for d in differences if d["leaf"] == "proc." + entry.get("name", "") and _declared_covers(entry, d)}
            hit = [d for d in differences if _declared_covers(entry, d, probe_seeds)]
            if not any(d["leaf"] == "proc." + entry.get("name", "") for d in hit):
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
            "outside_tree": outside, "warmup": dict(warm, both=both), "idle": {side: _idle_summary(fp.get("idle")) for side, fp in (("A", a), ("B", b))}, "not_judged": not_judged,
            "shortcuts": {side: _shortcuts_summary(((fp.get("idle") or {}).get("idle") or {}).get("shortcut_changes")) for side, fp in (("A", a), ("B", b))},
            "stale": [e.get("id") for e in stale], "leaves": counts, "net": net}


def _launch_counts(counts: dict) -> str:
    """`5/6 measured, 0/2 long` of the entry of a path in the not judged list."""
    return "%d/%d measured, %d/%d long" % (*counts["measured"], *counts["long"])


def write_verdict(path: Path, report: dict, listed_name: str) -> None:
    """The verdict at `path` and, next to it, the not judged list as `listed_name` (a file of its own: the verdict stays small)."""
    write_json(path.with_name(listed_name), report["not_judged"])
    write_json(path, {k: v for k, v in report.items() if k != "not_judged"})


def print_report(report: dict, a_name: str, b_name: str) -> None:
    for seed, per in sorted(report["leaves"].items()):
        print("seed %s leaves: %s" % (seed, ", ".join("%s %d" % kv for kv in sorted(per.items()))))
    for d in report["differences"][:200]:
        print("DIFF seed %s %s: %s %s | %s %s%s" % (d["seed"], d["leaf"], a_name, json.dumps(d["a"], sort_keys=True)[:160],
                                                    b_name, json.dumps(d["b"], sort_keys=True)[:160], _by_text(d)))
    for key, label, prefix, name_key, by_key in (("paths", "paths", "", "path", "touched_by"), ("kinds", "kinds of process", "proc.", "kind", "spawned_by")):
        listed = (report.get("not_judged") or {}).get(key, [])
        if not listed:
            continue
        watch = [e for e in listed if e["watch"]]
        print("NOT JUDGED (listed in the artifact, not red): %d %s, %d to watch" % (len(listed), label, len(watch)))
        for e in watch + [e for e in listed if not e["watch"]][:LISTED_IN_LOG]:
            who = e[by_key]
            print("%s %s%s: %s %s | %s %s (by %s | %s)" % ("WATCH" if e["watch"] else "LISTED", prefix, e[name_key], a_name, _launch_counts(e["A"]), b_name, _launch_counts(e["B"]),
                                                          ", ".join(who["A"]) or "-", ", ".join(who["B"]) or "-"))
        if len(listed) - len(watch) > LISTED_IN_LOG:
            print("LISTED: %d more %s in the file of the not judged list" % (len(listed) - len(watch) - LISTED_IN_LOG, label))
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


def stock_phase(name: str, now=None) -> None:
    """`stock phase <name> at <iso, UTC>` on the output, flushed: the stock control once stood still for more than 26 minutes (run 45153612447, PR #578, Windows) and nothing said in
    which call; the last phase line is where it stood. The output of the stock process is the one of the oracle (`run_stock_subprocess` does not capture it)."""
    at = now or datetime.datetime.now(datetime.timezone.utc)
    print("stock phase %s at %s" % (name, at.isoformat(timespec="seconds")))
    sys.stdout.flush()


def geolocation_step(browser, origin: str, declared: dict | None, n: int) -> dict:
    """The step after the journey: a context (with the declared position, or none for the stock control), the permission granted to the lab page,
    the page opened, `getCurrentPosition` called and awaited. Returns the step as the journey's are recorded, with `geo` (what the page was answered).
    The stock control (no declared position) says each call on the output before it makes it (`stock_phase`), and the answer after it."""
    step = {"n": n, "name": GEO_STEP}
    context = None
    say = stock_phase if declared is None else (lambda name: None)
    try:
        say("new_context")
        context = browser.new_context(geolocation=dict(declared)) if declared else browser.new_context()
        say("grant_permissions")
        context.grant_permissions(["geolocation"], origin=origin)
        say("new_page")
        page = context.new_page()
        say("goto")
        page.goto(origin + GEO_PATH)
        say("evaluate begin")
        step["geo"] = page.evaluate(GEO_READ)
        say("evaluate end: %s" % json.dumps(step["geo"], sort_keys=True))
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


def wait_for_glean_of_launch(find_profile, seed: int, launch: str) -> str | None:
    """The wait of a launch with a page for Glean (`crash.wait_for_glean`), made right after its first page is opened: Glean is born once a window is open, and a session that
    closes before it has initialised makes it start inside the shutdown, where the parent can end with -11 (a race of Mozilla's, the same in the retail browser; run 45121964904).
    The wait is on the file Glean writes, not on a time. Returns the problem of the launch when the condition is not met in the safety limit of `crash.GLEAN_LIMIT_S` (the
    verdict is red; the launch goes on and closes as it always did), None when it is."""
    try:
        waited = crash.wait_for_glean(find_profile())
    except crash.GleanNotReady as e:
        print("glean NOT ready (seed %d, launch %s): %s" % (seed, launch, e))
        sys.stdout.flush()
        return str(e)
    print("glean ready after %.1f s (seed %d, launch %s)" % (waited, seed, launch))
    sys.stdout.flush()
    return None


def datareporting_listing(profile) -> str:
    """The text of `<profile>/datareporting/` as a table, `path<TAB>size` (relative, `/`, sorted): what Glean and the data policy left in the profile of a launch. A read error
    is a line of the table (`# error`), never a reason to stop: it is evidence, and the launch does not depend on it."""
    lines = ["path\tsize"]
    if profile is None:
        return "\n".join(lines + ["# error\tthe profile of the launch was not found"]) + "\n"
    root = Path(profile) / "datareporting"
    rows, errors = [], []

    def on_error(e):
        errors.append("# error\t%s\t%s" % (e.filename, e.strerror or e))

    if not root.is_dir():
        errors.append("# error\t%s\tno such folder" % root.as_posix())
    for folder, _dirs, files in os.walk(root, onerror=on_error):
        for name in files:
            path = Path(folder) / name
            try:
                rows.append((path.relative_to(root).as_posix(), path.stat().st_size))
            except OSError as e:
                errors.append("# error\t%s\t%s" % (path.as_posix(), e.strerror or e))
    return "\n".join(lines + ["%s\t%d" % row for row in sorted(rows)] + sorted(errors)) + "\n"


def save_datareporting(profile, folder, launch: str, seed: int) -> None:
    """Saves the listing of `<profile>/datareporting/` as `<folder>/<launch>-<seed>.tsv` (evidence for the next time: it changes no verdict and is never a leaf of the footprint).
    No folder, no file; a write that fails is said on the output."""
    if not folder:
        return
    try:
        Path(folder).mkdir(parents=True, exist_ok=True)
        (Path(folder) / ("%s-%d.tsv" % (launch, seed))).write_text(datareporting_listing(profile), encoding="utf-8", newline="")
    except OSError as e:
        print("the list of datareporting could not be saved (seed %d, launch %s): %s" % (seed, launch, e))


def save_audit(folder, launch: str, seed: int, audit) -> None:
    """Saves the raw events the tree of a Windows launch was read from as `<folder>/<launch>-<seed>.json` (`audit_listing`: the 4688 and 4689 of the launch, the excluded ones as well,
    each with the reason): evidence for the next time a process is or is not in a tree, it changes no verdict and is never a leaf of the footprint. No folder, no file; a write that
    fails is said on the output."""
    if not folder or not audit:
        return
    events, ends, roots = audit
    try:
        Path(folder).mkdir(parents=True, exist_ok=True)
        write_json(Path(folder) / ("%s-%d.json" % (launch, seed)), audit_listing(events, roots, ends, launch, seed))
    except (OSError, ValueError, TypeError) as e:
        print("the audit of the tree could not be saved (seed %d, launch %s): %s" % (seed, launch, e))


LIVES_FORMAT = "ab-host-lives/1"


def save_lives(folder, launch: str, seed: int, got) -> None:
    """Saves next to the audit of a Windows launch (`save_audit`) what says whom each open was attributed to: `<folder>/<launch>-<seed>.lives.json` (`finish`'s `lives`: every life of every pid
    of the tree in the stream with its creator, image, positions, exit code, whether it is the tree's and the opens counted, and the opens dropped because they are of another life of the
    pid) and `<folder>/<launch>-<seed>.etw.xml.gz` (the session XML of the launch filtered to the events of every pid that was ever a pid of the tree and every Kernel-Process start and
    stop: the raw XML holds every open of the machine). Evidence: it changes no verdict. No folder, no file; a write that fails is said on the output."""
    if not folder or not got or not got.get("lives"):
        return
    try:
        Path(folder).mkdir(parents=True, exist_ok=True)
        write_json(Path(folder) / ("%s-%d.lives.json" % (launch, seed)), dict(got["lives"], format=LIVES_FORMAT, launch=launch, seed=seed))
        if got.get("etw_evidence"):
            shutil.copyfile(got["etw_evidence"], Path(folder) / ("%s-%d.etw.xml.gz" % (launch, seed)))
    except (OSError, ValueError, TypeError) as e:
        print("the lives of the tree could not be saved (seed %d, launch %s): %s" % (seed, launch, e))


def run_seed(binary: str, seed: int, work: Path, name: str | None = None, idle: bool = False, crash_with: int | None = None, datareporting_dir=None, audit_dir=None) -> dict:
    """One browser, one journey, one close, one trace. Returns the seed's footprint; `name` is the folder of its work (the warm-up has one of its own).

    `idle` is the launch in which the exit pings MUST leave (see `run_engine`): no journey and no pref changed; the launch waits until the notification of the data
    policy is recorded in its profile (the precondition of `canUpload`, seen in a file of the launch) and then closes; its footprint carries `idle` (what the wait saw).

    EVERY launch is judged by how its parent ended: a root exit code other than 0 is a problem of the launch (`crash.exit_problem`) and the launch keeps what it left
    (`result["crash"]`, `crash.collect`: the strace tail with the signal line, the core through gdb, the build ids, the `.extra` and the minidump names). The cores are
    enabled in every launch (`ulimit -c unlimited` in the stand-in, the working directory untouched) and a crash watcher reads the crash folders while any launch lives.

    A launch with a page waits for Glean to have initialised right after its first page is opened (`wait_for_glean_of_launch`: Glean is born once a window is open, and a session
    that closes before it has initialised makes it start inside the shutdown, where the parent can end with -11), so that the work of the launch is always between the condition and the
    close; a condition that is not met is a problem of the launch (the verdict is red) and the launch goes on. Just before the close the list of `<profile>/datareporting/` is saved in
    `datareporting_dir` (`save_datareporting`: evidence for the next time, never part of the footprint). On Windows the raw events of the audit the tree was read from, the
    excluded ones included, are saved in `audit_dir` (`save_audit`), with the lives of the tree and the filtered session XML (`save_lives`), also evidence.

    `crash_with` is the recorder's own test (`crash-probe`): the signal sent to the parent of the browser once it is up, to show that a launch that dies is red and keeps its evidence."""
    from playwright.sync_api import sync_playwright

    from fakeweb import journey_site, profile_p4
    from invisible_playwright._juggler.remote import launch_server

    windows = sys.platform == "win32"
    seed_work = work / (name or "seed%d" % seed)
    launch_name = name or "measured"  # `name` is a loop variable of the journey below
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
                    "problems": [trace_not_started(e)]}
    else:
        from _strace_stand_in import _stand_in

        exe = _stand_in(binary, seed_work / "engine", log, pidfile, syscalls=STRACE_CALLS, fd_paths=True, network=True, exit_status=True,
                        cores=True)
    origin_a, origin_o, stop_site = journey_site.start((0, 0))
    geo_origin, stop_geo = start_geo_lab()
    server, steps, root_pid = None, [], None
    notified, watcher = None, None
    places = shortcut_places() if (windows and idle) else None
    shortcuts_before = snapshot_places(places) if places else None
    before = set(Path(tempfile.gettempdir()).glob("invisible_profile_*"))

    def profile_now():
        """The profile folder this launch made (None until it is there)."""
        return (new_profiles(before) or [None])[0]

    def crash_dirs():
        new = new_profiles(before)
        return crash_report_dirs(new[0] if new else None, windows)

    started_at = time.time()
    already = crash.existing_crash_files(crash_dirs())  # the reports of earlier crashes are not this launch's
    print(crash.launch_line(seed, launch_name, HOST_LABEL))  # which launch is alive, and since when, for the test that runs this oracle when it does not end in time
    sys.stdout.flush()

    try:
        server = launch_server(headless=True, binary_path=exe, seed=seed, locale="en-US", timezone="Europe/Rome",
                               launch_timeout_ms=180_000)
        root_pid = server.process.pid
        if not windows:
            from _strace_stand_in import _group_of

            pgid = _group_of(pidfile)
        watcher = CrashWatcher(crash_dirs, ignore=already)
        watcher.start()
        with (contextlib.nullcontext() if crash_with is not None else sync_playwright()) as p:
            if crash_with is not None:
                crash_the_parent(root_pid, server.process, crash_with)  # the recorder's own test: the browser is up, its parent is sent a fatal signal
            elif idle:
                # Run 44988820319: with no client and no page the notification was never recorded in 120 s on either system (no window, so no
                # `sessionstore-windows-restored` for the policy to start from) and the launch had a crash reporter: the idle launch has the page of a launch, and nothing else.
                browser = p.firefox.connect(server.ws_endpoint)
                context = browser.new_context()
                context.new_page()
                glean = wait_for_glean_of_launch(profile_now, seed, launch_name)
                if glean:
                    problems.append(glean)
                notified = wait_for_notification(profile_prefs_reader(before))
                save_datareporting(profile_now(), datareporting_dir, launch_name, seed)
                context.close()
                browser.close()
            else:
                browser = p.firefox.connect(server.ws_endpoint)
                context = browser.new_context()
                context.set_default_timeout(profile_p4.HANG_GUARD_MS)
                context.set_default_navigation_timeout(profile_p4.HANG_GUARD_MS)
                profile_p4.install_route(context)
                page = context.new_page()
                glean = wait_for_glean_of_launch(profile_now, seed, launch_name)
                if glean:
                    problems.append(glean)
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
                save_datareporting(profile_now(), datareporting_dir, launch_name, seed)
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
        result = _seed_result(binary, seed, seed_work, steps, log, trace, root_pid, windows, close, lambda audit: save_audit(audit_dir, launch_name, seed, audit),
                              lambda got: save_lives(audit_dir, launch_name, seed, got))
        if idle:
            result["idle"] = {"notified": notified, "shortcut_changes": diff_snapshots(shortcuts_before, snapshot_places(places)) if places else None}
        watched, watcher = watcher.stop(), None
        bad, left = end_of_launch(close["root_returncode"], windows, seed, launch_name, result.get("lifecycle"), binary, log, watched,
                                  started_at)
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


def end_of_launch(code, windows: bool, seed: int, launch: str, lifecycle, binary: str, log, watched: dict, started_at: float, folders=None, run=subprocess.run) -> tuple:
    """How a launch ended, as the verdict reads it: `(problems, evidence)`. A root exit code other than 0 (or not seen) is a problem that names the seed, the launch and the
    code (`crash.exit_problem`), and so is a process of the kind parent that the lifecycle says did not exit 0 when the root said nothing of it (Windows: the core's spawn
    may be a launcher). A launch with a problem keeps what it left (`crash.collect`): the strace tail and the fatal signal lines, the cores through gdb with the build ids
    (the folders of the job's core pattern), the `.extra` and the minidump names the watcher read. A launch that
    ended with 0 has none and costs nothing."""
    bad = [x for x in [crash.exit_problem(code, windows, seed=seed, launch=launch)] + crash.parent_exit_problems(lifecycle, code, seed=seed, launch=launch) if x]
    if not bad:
        return [], None
    if folders is None:
        folders = [] if windows else crash.core_dirs()
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


def new_profiles(before: set) -> list:
    """The profile folders (`invisible_profile_*` of the temp root) that were not there before the launch, in order."""
    return sorted(set(Path(tempfile.gettempdir()).glob("invisible_profile_*")) - before)


def profile_prefs_reader(before: set):
    """`read()` for `wait_for_notification`: the prefs.js of the profile the launch made (the `invisible_profile_*` folder of the run's temp root that was not
    there before it)."""
    def read():
        for folder in new_profiles(before):
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


def _seed_result(binary: str, seed: int, seed_work: Path, steps: list, log: Path, trace, root_pid, windows: bool, close: dict | None = None, keep_audit=None, keep_lives=None) -> dict:
    """The footprint of one launch. On Windows `keep_audit(audit)` is given the raw events of the audit the tree was read from (`save_audit`), and `keep_lives(got)` what `finish` returned (the lives of the pids of the tree and the filtered session XML, `save_lives`), before they are read into the footprint."""
    real = os.path.realpath(binary)
    ctx = _context(binary, windows, seed_work)
    if windows:
        got = trace.finish({root_pid})
        if keep_audit is not None:
            keep_audit(got.get("audit"))
        if windows and keep_lives is not None:
            keep_lives(got)
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


def run_engine(binary: str, leg: str, seeds: list, label: str, crash_dir=None, datareporting_dir=None, audit_dir=None) -> dict:
    """The footprint document of one engine. A discarded WARM-UP launch first, of the first seed, then every seed's journey, one browser at a time.

    Every launch (the warm-up, each seed and the idle one) that does not end with 0 is a problem of the run, and what it left is kept in `crash_dir` (`keep_crash`). The list of the
    `datareporting` folder of each launch with a page is saved in `datareporting_dir` (`save_datareporting`), and on Windows the raw events of the audit of each launch, the lives of its tree and the filtered session XML in `audit_dir` (`save_audit`, `save_lives`).

    The warm-up is a control of the experiment and not an excuse: what a machine does the first time it runs an engine (its files scanned, its caches
    cold, a first-run behaviour of the engine) is in a launch that is not compared, whose trace is kept in the document (`warmup`) so that what it
    did and the measured launch did not stays visible, run to run (`warmup_only`). The order is the same in every run: the warm-up of an engine, then its
    measured launches; the runs of an A/A or an A/B follow one another."""
    work = Path(tempfile.mkdtemp(prefix="ab-host-"))
    problems, out_seeds = [], []
    try:
        warm = run_seed(binary, seeds[0], work, name="warmup", datareporting_dir=datareporting_dir, audit_dir=audit_dir)
        keep_crash(warm, "warmup", crash_dir)
        problems += ["warm-up: %s" % p for p in warm.get("problems", [])]
        problems += ["warm-up: seed %d: step %d (%s) failed: %s" % (seeds[0], st["n"], st["name"], st.get("error")) for st in warm["steps"] if not st["ok"]]
        print("warm-up (seed %d, discarded): %s; %d host paths" % (seeds[0], json.dumps(warm["kinds"], sort_keys=True), len(warm["files"])))
        sys.stdout.flush()
        for seed in seeds:
            fp = run_seed(binary, seed, work, datareporting_dir=datareporting_dir, audit_dir=audit_dir)
            keep_crash(fp, "seed%d" % seed, crash_dir)
            out_seeds.append(fp)
            for step in fp["steps"]:
                if not step["ok"]:
                    problems.append("seed %d: step %d (%s) failed: %s" % (seed, step["n"], step["name"], step.get("error")))
            print("seed %d: %s; %d host paths, %d net entries, %s" % (seed, json.dumps(fp["kinds"], sort_keys=True), len(fp["files"]),
                                                                      len(fp.get("net") or []), json.dumps(fp.get("ours", {}), sort_keys=True)))
            sys.stdout.flush()
        idle = run_seed(binary, seeds[0], work, name="idle", idle=True, datareporting_dir=datareporting_dir, audit_dir=audit_dir)
        keep_crash(idle, "idle", crash_dir)
        problems += ["idle: %s" % p for p in idle.get("problems", [])]
        print("idle (seed %d): %s; notification %s" % (seeds[0], json.dumps(idle["kinds"], sort_keys=True), json.dumps((idle.get("idle") or {}).get("notified"))))
        sys.stdout.flush()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"format": FORMAT, "engine": label, "leg": leg, "platform": sys.platform, "seeds": out_seeds, "warmup": warm, "idle": idle, "problems": problems,
            "binary": os.path.basename(os.path.dirname(os.path.realpath(binary)))}


#: The label of the launch line of the stock control, and of the launches of the oracle (`crash.launch_line`).
STOCK_LABEL = "stock control"
HOST_LABEL = "ab-host"
STOCK_NO_PARENT = "the parent process of the stock Firefox of Playwright was not found among the descendants of the oracle (no firefox.exe without -contentproc after the launch of %s): the trace has no root"
STOCK_NOT_INSTALLED = "the stock Firefox of Playwright is not installed: %s does not exist (the job must run `python -m playwright install firefox`)"


def stock_evidence(seed: dict) -> list:
    """What a stock Firefox that started but did not reach the location provider left, for the log: its processes by kind with their command, and the modules (the
    DLLs of the host paths of the trace) the tree loaded, the location ones first. The document (`footprint-stock.json`) has all of it."""
    lines = ["stock evidence: processes by kind: %s" % json.dumps(seed.get("kinds", {}), sort_keys=True)]
    lines += ["stock evidence: process %s: %s" % (p.get("label"), p.get("cmd") or p.get("exec") or p.get("name")) for p in seed.get("processes", [])]
    files = seed.get("files", [])
    wanted = location_modules(files)
    others = sorted({_base(f).lower() for f in files if f.lower().endswith(".dll")} - {_base(f).lower() for f in wanted})
    lines.append("stock evidence: location modules loaded: %s" % (", ".join(_base(f) for f in wanted) or "none"))
    lines.append("stock evidence: other modules loaded (%d): %s" % (len(others), ", ".join(others[:150])))
    return lines


def stock_browser_parent(root_pid: int | None = None) -> int | None:
    """The pid of the parent process of the Firefox that Playwright launched: among the descendants of this process (`root_pid`, default this process), the oldest `firefox.exe` whose
    command line has no `-contentproc`. None when there is none. It is the ROOT of the trace of the stock control, as the parent of the engine is the root of an engine's (run
    45162128126: with this process as the root `WindowsTrace.finish` waited the whole `EXIT_LIMIT_S` for a tree that cannot exit, and the 120 s of the control ended it)."""
    import psutil

    found = []
    for child in psutil.Process(os.getpid() if root_pid is None else root_pid).children(recursive=True):
        try:
            if child.name().lower() == "firefox.exe" and "-contentproc" not in [str(a) for a in child.cmdline()]:
                found.append((child.create_time(), child.pid))
        except Exception:  # noqa: BLE001 - a process that is already gone
            continue
    return min(found)[1] if found else None


def run_stock(leg: str, audit_dir=None) -> dict:
    """The negative control of the geolocation step, Windows only: Playwright's own Firefox (`p.firefox.launch()`, as the fake web's stock control), with
    no declared position, the same step, the same trace. Its tree must load the first read of the host for the position (`stock_control_problems`). The tree is the
    one of this process (the driver and the browser it launches): only the kinds and the modules of the run are read."""
    from playwright.sync_api import sync_playwright

    print(crash.launch_line(0, "stock", STOCK_LABEL))  # the line `crash.last_launch` reads when the run does not end in time
    sys.stdout.flush()
    work = Path(tempfile.mkdtemp(prefix="ab-host-stock-"))
    trace = WindowsTrace(work)
    stop_geo = None
    steps = []

    def document(result):
        return {"format": FORMAT, "engine": "stock", "leg": leg, "platform": sys.platform, "stock": result}

    def not_started(reason):
        """The control that never got a Firefox to run: `started` False, so the verdict names this reason and not the provider (run 45121964904: the binary of Playwright
        was not installed in the job, and the oracle said that the step does not reach the provider on stock Firefox)."""
        return document({"seed": 0, "steps": [], "kinds": {}, "files": [], "ours": {}, "trace": {"source": "etw+audit", "exited": False}, "started": False, "problems": [reason]})

    try:
        stock_phase("trace start")
        try:
            trace.start()
        except TraceError as e:
            return not_started(trace_not_started(e))
        stock_phase("geolocation lab")
        geo_origin, stop_geo = start_geo_lab()
        try:
            with sync_playwright() as p:
                exe = p.firefox.executable_path
                if not Path(exe).exists():
                    return not_started(STOCK_NOT_INSTALLED % exe)
                stock_phase("launch")
                try:
                    browser = p.firefox.launch(headless=True)
                except Exception as exc:  # noqa: BLE001 - the reason is the verdict's
                    return not_started("the stock Firefox of Playwright did not start: %s" % (str(exc).strip().splitlines() or [repr(exc)])[0][:300])
                parent = stock_browser_parent()
                stock_phase("browser parent %s" % parent)
                try:
                    steps.append(geolocation_step(browser, geo_origin, None, len(JOURNEY) + 1))
                finally:
                    stock_phase("browser close")
                    browser.close()
            if parent is None:
                # never this process as the root in silence: its tree does not exit and the trace would wait for it
                return document({"seed": 0, "steps": steps, "kinds": {}, "files": [], "ours": {}, "trace": {"source": "etw+audit", "exited": False}, "started": False,
                                 "problems": [STOCK_NO_PARENT % exe]})
            stock_phase("seed result")
            result = _seed_result(exe, 0, work, steps, work / "none.log", trace, parent, True, None, lambda audit: save_audit(audit_dir, "stock", 0, audit),
                                   lambda got: save_lives(audit_dir, "stock", 0, got))
            result["started"] = True
            return document(result)
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

#: Where the engine under test is run from, whichever engine it is: `<RUNNER_TEMP>/ab-host/engine/firefox` (the temp root of the machine without it). The content processes and the
#: forkserver call `realpath(<install>/omni.ja)` (Omnijar::ChildProcessInit -> XRE_GetFileFromPath -> nsToolkitProfileService.cpp realpath), which reads the link of EVERY ancestor of
#: the install folder: the oracle sees `readlink /srv`, `/srv/lab/work`, ... for an engine that sits in the checkout and `readlink /tmp/ab-host-fetch-<random>/engines` for one
#: that was fetched (run 45043617981: 10 leaves of that on each of 5 seeds). The place of the install is a variable of the experiment, so it is the same in the three runs of an
#: A/B, and the ancestors of the package are the same leaves in each: nothing is normalised in the comparison.
STAGE_ENGINE = ("ab-host", "engine")
STAGE_PACKAGE = "firefox"


def stage_dir(base=None) -> Path:
    """The folder the engine under test is run from: the same absolute path for every engine and every run."""
    return Path(base or os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()).joinpath(*STAGE_ENGINE) / STAGE_PACKAGE


@contextlib.contextmanager
def staged_engine(binary: str, base=None):
    """The whole package of `binary` (its folder, every file, links kept, the bytes as they are) copied to `stage_dir`, yielding the path of the binary there; the copy is
    removed when the run is over, so the next engine is staged at the very same path."""
    source = Path(os.path.abspath(binary))
    dest = stage_dir(base)
    shutil.rmtree(dest, ignore_errors=True)
    if dest.exists():
        raise OSError("the folder the engine is run from, %s, could not be emptied" % dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source.parent, dest, symlinks=True)
    try:
        yield str(dest / source.name)
    finally:
        shutil.rmtree(dest, ignore_errors=True)


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


#: The whole life of the stock control, from the launch of its process to its end: a Firefox that starts, answers one `getCurrentPosition` and closes takes seconds. Never again 26 silent
#: minutes (run 45153612447, PR #578, Windows: the A/A was green and the stock control stood still until the guard of the test killed the python child alone). A control that does not end in
#: this time is a BROKEN control that says in which phase it stood (`last_stock_phase`).
STOCK_LIMIT_S = 120

STOCK_PHASE = re.compile(r"^stock phase (?P<phase>.*?) at (?P<at>\S+)\s*$", re.M)


def last_stock_phase(text: str, now=None) -> tuple:
    """`(phase, seconds since it began)` of the last `stock phase` line of `text`; `(None, None)` when there is none (`seconds` None when its time does not read)."""
    found = None
    for found in STOCK_PHASE.finditer(text or ""):
        pass
    if found is None:
        return None, None
    try:
        began = datetime.datetime.fromisoformat(found.group("at"))
        return found.group("phase"), (now or datetime.datetime.now(datetime.timezone.utc)) - began
    except (ValueError, TypeError):
        return found.group("phase"), None


def run_stock_subprocess(leg: str, out: Path, limit_s: float = STOCK_LIMIT_S, command=None) -> int:
    """`stock` in a process of its own (its tree is the one of that process), with a temp root of its own, run by `crash.run_tree` (its output in files under `<out>.tree`, the whole
    tree killed at `limit_s`) and then printed on the output of the oracle, so that the phases it said are in the log.

    A process that does not end in `limit_s` leaves no footprint: this writes the document of a control that never ran to its end (`started` False, the reason in its problems: the
    last phase it said and since when), so that the verdict names that and not the platform provider. Returns the exit code (0 for that document: the reason is in it)."""
    env = dict(os.environ)
    for name in ("MOZ_LOG", "MOZ_LOG_FILE"):
        env.pop(name, None)
    cmd = command or [sys.executable, "-u", str(Path(__file__).resolve()), "stock", "--leg", leg, "--out", str(out)]
    with tempfile.TemporaryDirectory(prefix="ab-host-tmp-") as temp:
        env.update(TMPDIR=temp, TEMP=temp, TMP=temp)
        done = crash.run_tree(cmd, Path(out).with_suffix(".tree"), limit_s, env=env)
    sys.stdout.write(done.stdout)
    sys.stdout.flush()
    sys.stderr.write(done.stderr)
    sys.stderr.flush()
    if not done.timed_out:
        return done.returncode
    phase, since = last_stock_phase(done.stdout)
    where = "no phase had been said" if phase is None else "the last phase was `%s`, begun %s before the end" % (phase, "%d s" % since.total_seconds() if since is not None else "an unknown time")
    reason = "the stock control did not end within %g s (never again 26 silent minutes, run 45153612447): %s; its tree was killed" % (limit_s, where)
    print("stock control: " + reason)
    sys.stdout.flush()
    write_json(out, {"format": FORMAT, "engine": "stock", "leg": leg, "platform": sys.platform,
                     "stock": {"seed": 0, "steps": [], "kinds": {}, "files": [], "ours": {}, "trace": {"source": "etw+audit", "exited": False}, "started": False, "problems": [reason]}})
    return 0


def run_stock_control(leg: str, out: Path) -> dict | None:
    """The stock run of the geolocation step and its verdict: the footprint, or None when the control is broken (said on the output)."""
    print("=== negative control of the geolocation step: Playwright's own Firefox")
    sys.stdout.flush()
    rc = run_stock_subprocess(leg, out / "footprint-stock.json")
    broken = ["the run of the stock control ended with %s (its own output is above)" % rc] if rc != 0 or not (out / "footprint-stock.json").exists() else []
    stock = None
    if not broken:
        stock = read_json(out / "footprint-stock.json")
        broken = stock_control_problems(stock)
    if broken:
        print("\n".join("FAIL " + b for b in broken))
        seed = (stock or {}).get("stock") or {}
        if seed and seed.get("started", True) and not location_first_reads(seed.get("files", [])):
            # the claim of the step is kept; the proof that goes with it is what the tree that did start left
            print("\n".join(stock_evidence(seed)))
            print("BROKEN CONTROL: the geolocation step does not reach the platform on stock Firefox (%s is not loaded); the A/B says nothing" % LOCATION_FIRST_READ)
        else:
            print("BROKEN CONTROL: the stock control did not run to the end (the reason is above); the A/B says nothing")
        return None
    print("GEOLOCATION CONTROL: stock Firefox loaded %s; location utility process: %s; modules: %s" % (
        LOCATION_FIRST_READ, ", ".join(location_utility_kinds(stock["stock"]["kinds"])) or "none (the runner has no location source, the provider answered at once)",
        ", ".join(location_modules(stock["stock"]["files"])) or "-"))
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
            print("=== footprint with %s: %s (run from %s)" % (label, exe, stage_dir()))
            sys.stdout.flush()
            try:
                with staged_engine(exe) as staged:
                    rc = run_subprocess(staged, Path(seal_file) if seal_file else None, leg, out / name, label, seeds)
            except OSError as e:
                print("the engine of %s could not be staged: %s" % (label, e))
                rc = 125
            if rc != 0 or not (out / name).exists():
                failed.append("the run with %s ended with %s" % (label, rc))
        if failed:
            write_json(out / "diff-a-a.json", {"format": FORMAT, "mode": "a-a", "verdict": "red", "problems": failed,
                                               "differences": [], "declared": [], "stale": [], "leaves": {}})
            print("\n".join("FAIL " + f for f in failed))
            return 2
        first, again = (read_json(out / runs[0][1]), read_json(out / runs[1][1]))
        control = judge(first, again, a_a=True)
        write_verdict(out / "diff-a-a.json", control, "not-judged-a-a.json")
        print("--- A/A: %s against itself" % engine_tag)
        print_report(control, engine_tag, engine_tag + " again")
        if control["verdict"] != "green":
            print("BROKEN CONTROL: the published engine differs from itself on this machine; the A/B says nothing")
            return 2
        # The control of the geolocation step is a control of the laboratory on Playwright's own Firefox and does not depend on a candidate: on Windows it runs after a
        # green A/A on every run, a pull request included (run 45121964904, the gate of the series: it ran only with a candidate, and what it said there was never seen on a PR).
        if _windows():
            stock = run_stock_control(leg, out)
            if stock is None:
                return 2
        if not candidate:
            return 0
        expected = load_expected(expected_file)
        report = judge(first, read_json(out / runs[2][1]), expected=expected, a_a=False, also_68=[again])
        write_verdict(out / "diff.json", report, "not-judged.json")
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
            write_verdict(args.out, report, args.out.stem + "-not-judged.json")
        print_report(report, a.get("engine", "A"), b.get("engine", "B"))
        if report["verdict"] == "green":
            return 0
        return 2 if args.a_a else 1
    if args.cmd == "stock":
        if not _windows():
            print("the stock control is for Windows: Linux has no Windows location provider to reach")
            return 3
        write_json(args.out, run_stock(args.leg, args.out.parent / (args.out.stem + ".audit")))
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
            document = run_engine(args.binary, args.leg, seeds, args.label, crash_dir, args.out.parent / (args.out.stem + ".datareporting"),
                                  args.out.parent / (args.out.stem + ".audit"))  # footprint-68.json -> footprint-68.audit/<launch>-<seed>.json
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
