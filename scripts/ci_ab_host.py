#!/usr/bin/env python3
"""Host-footprint A/B oracle: what the engine does to the HOST, over the whole of a closed run.

scripts/ci_ab_oracle.py compares what a PAGE reads. This compares what the engine does to the host, which a page cannot read but a release can
change (a helper that is no longer spawned, a path of the machine it did not touch before). It is not a photograph of one instant: it is the
TRACE of the run, from the launch to the exit of the browser (the quiet point is the exit of the process, not a wait), after the same short
journey (the first steps of the fake web's P4 walk, tests/fakeweb/profile_p4.py, no internet, driven by the stock Playwright client).

Two things are compared, over the whole trace:

  * the processes, by KIND, from the command lines of the launch: the parent, `-contentproc` with its type, `-sandboxingKind N` and
    `-isForBrowser` / `-notForBrowser`, and the helpers (glxtest, pingsender, ...) by what they ran. Every kind and its COUNT must be identical,
    with one exception: for the content processes of type `tab` only the presence of the kind counts, because how many are alive follows the idle
    time (a spare process is prelaunched after a delay and closed after a grace period);
  * the files: every path the tree touched (opened, stat'ed, accessed, read as a link, executed), classified `ours` or `host`. The SET of host
    paths must be identical, no exception. `ours` leaves the comparison as a class (OURS below: reading its own package is not touching the host).

How the trace is taken:

  * Linux: `strace -f -y` of the whole tree (the stand-in of tests/_strace_stand_in.py), calls execve, clone, fork, prctl and the file calls
    (open, openat, stat, access, readlink, ...). The tree is named by what each process ran, or called itself when it forked without exec
    (the forkserver's children), and the log is read after the tree has exited.
  * Windows: the process starts WITH their command lines come from the Security log, event 4688 (the process-creation audit with the command
    line, switched on for the run and put back after it): the kernel writes one per process, so a short-lived child cannot be missed, which a
    poll of the job's pids can. The files come from an ETW session on Microsoft-Windows-Kernel-File (the Create events: every path a process
    opens, a library included) taken with `logman` and read with `tracerpt`, both part of Windows. It needs an administrator (the hosted runners
    are). Two canary files of the oracle's own, opened after the session starts and before it stops, must be in the trace: a session that did not
    see them is red, not empty.

    python ci_ab_host.py run <firefox> [--seal SEAL] --leg linux-x86_64 --out footprint.json [--seeds 11,12,13]
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
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tests"))
import ci_ab_oracle as oracle  # noqa: E402  (fetch_published, ENGINE_TAG)
import ci_host_gate as gate  # noqa: E402  (SEEDS)

FORMAT = "ab-host/2"
EXPECTED_FORMAT = "ab-host-expected/1"
EXPECTED_FILE = ROOT / "ci" / "ab-host-expected.json"
SEEDS_FILE = ROOT / "ci" / "persona-seeds.json"
#: The steps of the journey (tests/fakeweb/profile_p4.py STEPS, by method name).
JOURNEY = (("open home", "s1_open_home"),
           ("hover and click a teaser", "s2_hover_click_teaser"),
           ("scroll to the bottom, next article", "s3_scroll_next"),
           ("nav Category", "s7_nav_category"))
#: What a seed's footprint must hold at least, per category of leaf: a footprint that holds less did not see the run.
#: Provisional: the first A/A on both systems gives the real numbers (they are in diff.json, "leaves").
FLOORS = {"proc": 3, "file": 100}
#: The categories of leaf a footprint has.
CATEGORIES = ("proc", "file")
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
    program's name (lower case)."""
    if "-contentproc" in argv:
        return kind_of(argv)  # whatever path it was started by (the binary, /proc/self/exe)
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
    launch), `cache` (the folders the oracle made the cache of the run in) and, on Windows, `devices` ({`\\device\\harddiskvolume3`: `c:`})."""

    def __init__(self, platform: str, app=(), temp=(), cache=(), devices: dict | None = None) -> None:
        self.platform = platform
        self.windows = platform == "win32"
        self.app = [self._key(p) for p in app if p]
        self.temp = [self._key(p) for p in temp if p]
        self.cache = [self._key(p) for p in cache if p]
        self.devices = {k.lower(): v.lower() for k, v in (devices or {}).items()}

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
    pipe = _NT_PIPE.match(low)
    if pipe:
        return ("ipc", low) if _OWN_PIPE.search(low) else ("host", low)
    own = ctx.own(low)
    if own:
        return own
    return "host", low.rstrip("\\") or low


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
STRACE_CALLS = "clone,clone3,fork,vfork,prctl," + ",".join(PATH_CALLS + PATH_AT_CALLS)
#: A path relative to the working directory, which the strace log does not carry: the same text on every run, so the same on both sides.
CWD = "<cwd>"


def _unescape(text: str) -> str:
    return codecs.escape_decode(text.encode("latin-1", errors="replace"))[0].decode("utf-8", errors="replace")


def touched_paths(lines) -> set:
    """Every path a strace log (read as lines) says a process of the tree touched, failed attempts included: opening, stat, access, readlink,
    execve. A path relative to a directory descriptor is joined to the path `-y` printed for it, one relative to the working directory is
    spelled `<cwd>/...`, an empty path (the descriptor itself) is not a path."""
    found = set()
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
            found.add((base, raw))
    out = set()
    for base, raw in found:
        path = _unescape(raw)
        out.add(path if path.startswith("/") else _unescape(base).rstrip("/") + "/" + path)
    return out


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


def _read_lines(log: Path):
    with open(log, encoding="utf-8", errors="replace") as f:
        for line in f:
            yield line.rstrip("\n")


# -
# Windows: the processes with their command lines (the audit, event 4688) and the files (an ETW session on Kernel-File).

AUDIT_GUID = "{0CCE922B-69AE-11D9-BED3-505054503030}"  # the subcategory "Process Creation"
AUDIT_KEY = r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System\Audit"
AUDIT_VALUE = "ProcessCreationIncludeCmdLine_Enabled"
FILE_PROVIDER, FILE_CREATE_KEYWORD = "Microsoft-Windows-Kernel-File", "0x80"  # KERNEL_FILE_KEYWORD_CREATE


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
        out.append({"rid": rid, "pid": _number(data["NewProcessId"]), "ppid": _number(data.get("ProcessId", "")),
                    "image": data.get("NewProcessName", ""), "cmd": data.get("CommandLine", "")})
    return out


def process_tree(events, roots) -> list:
    """The events of `parse_audit_events` that are a root or come from a process of the tree, by the creator's pid each says, in their order."""
    wanted, chosen, changed = set(roots), set(), True
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


def etw_files(path, pids) -> tuple:
    """`({(pid, file name)}, events)` of the Create events a `tracerpt -of XML` file holds, for the processes in `pids`. Read as a stream: the file
    has every open of the machine for the length of the run."""
    seen, events = set(), 0
    for _ev, el in ET.iterparse(path, events=("end",)):
        if el.tag.rsplit("}", 1)[-1] != "Event":
            continue
        events += 1
        pid, name = None, None
        for child in el.iter():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "Execution":
                raw = child.get("ProcessID") or child.get("ProcessId")
                pid = _number(raw) if raw else None
            elif tag == "Data" and child.get("Name") == "FileName":
                name = child.text
        if name and pid in pids:
            seen.add((pid, name))
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

    def _canary(self, which: str) -> str:
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
        self._start_canary = self._canary("start")

    def _registry_value(self):
        try:
            out = _tool(["reg", "query", AUDIT_KEY, "/v", AUDIT_VALUE])
        except TraceError:
            return None
        m = re.search(r"REG_DWORD\s+(0x[0-9a-fA-F]+)", out)
        return int(m.group(1), 16) if m else None

    def tree(self, roots) -> list:
        xml = _tool(["wevtutil", "qe", "Security", "/q:*[System[(EventID=4688) and (EventRecordID>%d)]]" % self.first_record, "/f:xml"])
        return process_tree(parse_audit_events(xml), roots)

    def finish(self, roots, alive=None, sleep=time.sleep, now=time.monotonic) -> dict:
        """`{"tree": [4688 events], "files": {(pid, name)}, "exited": {...}, "events": n}`: what the tree did from its first process to its last."""
        if alive is None:
            import psutil

            alive = psutil.pid_exists
        tree, exited = wait_exited(lambda: self.tree(roots), alive, sleep=sleep, now=now)
        end_canary = self._canary("end")
        _tool(["logman", "stop", self.name, "-ets"])
        self.session = False
        summary = self.work / "files.txt"
        _tool(["tracerpt", str(self.etl), "-of", "XML", "-o", str(self.xml), "-summary", str(summary), "-y"])
        pids = {e["pid"] for e in tree} | {os.getpid()}
        seen, events = etw_files(self.xml, pids)
        problems = []
        mine = {name for pid, name in seen if pid == os.getpid()}
        for label, canary in (("first", self._start_canary), ("last", end_canary)):
            if not any(m.lower().endswith(os.path.basename(canary).lower()) for m in mine):
                problems.append("the file trace did not see the %s canary file of the oracle (%s): the session was not recording" % (label, os.path.basename(canary)))
        lost = re.search(r"(?i)events?\s+lost\D{0,20}(\d+)", summary.read_text(encoding="utf-8", errors="replace") if summary.exists() else "")
        if lost and int(lost.group(1)):
            problems.append("the file trace lost %s events" % lost.group(1))
        tree_pids = {e["pid"] for e in tree}
        missing = sorted(tree_pids - {pid for pid, _n in seen})
        if missing:
            problems.append("the file trace holds no open of the process(es) %s" % ", ".join(str(p) for p in missing[:10]))
        return {"tree": tree, "files": {(p, n) for p, n in seen if p in tree_pids}, "exited": exited, "events": events, "problems": problems}

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

def leaves_of(seed_footprint: dict) -> dict:
    """{leaf: value} of one seed: proc.<kind> = how many processes of that kind the run made (1 for a kind of type tab: its presence) and
    file.<path> = 1 for every HOST path the tree touched."""
    out = {}
    for kind, n in seed_footprint.get("kinds", {}).items():
        out["proc." + kind] = 1 if is_tab(kind) and n else n
    for path in seed_footprint.get("files", []):
        out["file." + path] = 1
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


def judge(a: dict, b: dict, expected: list | None = None, a_a: bool = False, platform: str | None = None,
          floors: dict | None = None) -> dict:
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
        la, lb = leaves_of(seeds_a[seed]), leaves_of(seeds_b[seed])
        for side, fp in (("A", seeds_a[seed]), ("B", seeds_b[seed])):
            for message in fp.get("problems", []):
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
    for d in differences:
        problems.append("seed %s %s: A %s | B %s" % (d["seed"], d["leaf"], json.dumps(d["a"], sort_keys=True)[:160],
                                                     json.dumps(d["b"], sort_keys=True)[:160]))
    tab = {str(seed): {"A": {k: n for k, n in seeds_a[seed].get("kinds", {}).items() if is_tab(k)},
                       "B": {k: n for k, n in seeds_b[seed].get("kinds", {}).items() if is_tab(k)}}
           for seed in sorted(set(seeds_a) & set(seeds_b))}
    return {"format": FORMAT, "mode": "a-a" if a_a else "a-b", "verdict": "red" if problems else "green", "problems": problems,
            "differences": differences, "declared": declared, "ours": OURS, "tab": {"reason": TAB_REASON, "counts": tab},
            "stale": [e.get("id") for e in stale], "leaves": counts}


def print_report(report: dict, a_name: str, b_name: str) -> None:
    for seed, per in sorted(report["leaves"].items()):
        print("seed %s leaves: %s" % (seed, ", ".join("%s %d" % kv for kv in sorted(per.items()))))
    for d in report["differences"][:200]:
        print("DIFF seed %s %s: %s %s | %s %s" % (d["seed"], d["leaf"], a_name, json.dumps(d["a"], sort_keys=True)[:160],
                                                  b_name, json.dumps(d["b"], sort_keys=True)[:160]))
    for d in report["declared"]:
        print("DECLARED seed %s %s (%s)" % (d["seed"], d["leaf"], d["id"]))
    print("OURS (not compared): " + "; ".join("%s: %s" % kv for kv in sorted(report.get("ours", OURS).items())))
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
        return Context("win32", app=app, temp=temp, devices=windows_device_map())
    return Context("linux", app=app, temp=temp, cache={os.environ.get("XDG_CACHE_HOME", "")})


def run_seed(binary: str, seed: int, work: Path) -> dict:
    """One browser, one journey, one close, one trace. Returns the seed's footprint."""
    from playwright.sync_api import sync_playwright

    from fakeweb import journey_site, profile_p4
    from invisible_playwright._juggler.remote import launch_server

    windows = sys.platform == "win32"
    seed_work = work / ("seed%d" % seed)
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

        exe = _stand_in(binary, seed_work / "engine", log, pidfile, syscalls=STRACE_CALLS, fd_paths=True)
    origin_a, origin_o, stop_site = journey_site.start((0, 0))
    server, steps, root_pid = None, [], None
    try:
        server = launch_server(headless=True, binary_path=exe, seed=seed, locale="en-US", timezone="Europe/Rome",
                               launch_timeout_ms=180_000)
        root_pid = server.process.pid
        if not windows:
            from _strace_stand_in import _group_of

            pgid = _group_of(pidfile)
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
            context.close()
            browser.close()
        # The close: the run ends with the exit of the browser, and the trace is read after it.
        server.close()
        server = None
        exited = _wait_for_tree_exit(pgid)
        if exited is not None and not exited:
            problems.append("the tree did not exit within %d s of the close: the trace is cut short" % EXIT_LIMIT_S)
        result = _seed_result(binary, seed, seed_work, steps, log, trace, root_pid, windows)
    except TraceError as e:
        result = {"seed": seed, "steps": steps, "kinds": {}, "files": [], "ours": {}, "trace": {"exited": False},
                  "problems": ["the trace of the run could not be taken: %s" % e]}
    finally:
        stop_site()
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


def _wait_for_tree_exit(pgid):
    """Linux: whether the process group of the run is empty (strace follows every process of the tree, so it ends with its last one); None on
    Windows, where `WindowsTrace.finish` waits for its own tree."""
    if pgid is None:
        return None
    from _strace_stand_in import _wait_for_exit

    return not _wait_for_exit(pgid, EXIT_LIMIT_S)


def _seed_result(binary: str, seed: int, seed_work: Path, steps: list, log: Path, trace, root_pid, windows: bool) -> dict:
    real = os.path.realpath(binary)
    ctx = _context(binary, windows, seed_work)
    if windows:
        got = trace.finish({root_pid})
        procs = windows_processes(got["tree"], real)
        raw = {name for _pid, name in got["files"]}
        info = {"source": "etw+audit", "events": got["events"], "exited": got["exited"]["exited"], "reads": got["exited"]["reads"]}
        problems = list(got["problems"])
        if not got["exited"]["exited"]:
            problems.append("the tree did not exit within %d s of the close: the trace is cut short" % EXIT_LIMIT_S)
        detail = sorted(({"label": p["label"], "cmd": p["cmd"][:300]} for p in procs.values()), key=lambda d: (d["label"], d["cmd"]))
    else:
        procs = spawned_processes(_read_lines(log), real)
        raw = touched_paths(_read_lines(log))
        info, problems = {"source": "strace", "exited": True}, []
        detail = sorted(({"label": p["label"], "exec": (" ".join([os.path.basename(p["exec"][0])] + p["exec"][1][1:6]) if p["exec"] else None),
                          "name": p["name"]} for p in procs.values()), key=lambda d: (d["label"], d["exec"] or ""))
    host, ours = classify_paths(raw, ctx)
    kinds = dict(sorted(collections.Counter(p["label"] for p in procs.values()).items()))
    info.update({"processes": len(procs), "paths": len(raw)})
    return {"seed": seed, "steps": steps, "kinds": kinds, "files": host, "ours": ours, "trace": info, "problems": problems,
            "processes": detail}


def run_engine(binary: str, leg: str, seeds: list, label: str) -> dict:
    """The footprint document of one engine: every seed's journey, one browser at a time."""
    work = Path(tempfile.mkdtemp(prefix="ab-host-"))
    problems, out_seeds = [], []
    try:
        for seed in seeds:
            fp = run_seed(binary, seed, work)
            out_seeds.append(fp)
            for step in fp["steps"]:
                if not step["ok"]:
                    problems.append("seed %d: step %d (%s) failed: %s" % (seed, step["n"], step["name"], step.get("error")))
            print("seed %d: %s; %d host paths, %s" % (seed, json.dumps(fp["kinds"], sort_keys=True), len(fp["files"]),
                                                      json.dumps(fp.get("ours", {}), sort_keys=True)))
            sys.stdout.flush()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"format": FORMAT, "engine": label, "leg": leg, "platform": sys.platform, "seeds": out_seeds, "problems": problems,
            "binary": os.path.basename(os.path.dirname(os.path.realpath(binary)))}


def write_json(path: Path, document) -> None:
    Path(path).write_text(json.dumps(document, sort_keys=True, indent=1), encoding="utf-8")


def read_json(path: Path) -> dict:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("format") != FORMAT:
        raise SystemExit("%s is not an %s footprint" % (path, FORMAT))
    return document


# -
# The run of an engine as a subprocess (the seal a process runs under is read at import), and the orchestration.

def run_subprocess(binary: str, seal: Path | None, leg: str, out: Path, label: str, seeds: list) -> int:
    """`run` in a process of its own, with a cache folder and a temp root of its own: they are the run's, so what the engine does in them is `ours`
    (the cache of the bundle's fonts, the profile and the temp dirs of the launch) and the rest of the machine is the host's."""
    env = dict(os.environ, INVISIBLE_CORE_AUTOFIX="off", INVISIBLE_CORE_PIN="allow")
    if seal:
        env["INVISIBLE_SEAL_FILE"] = str(seal)
    for name in ("MOZ_LOG", "MOZ_LOG_FILE"):
        env.pop(name, None)
    cmd = [sys.executable, "-u", str(Path(__file__).resolve()), "run", binary, "--leg", leg, "--out", str(out), "--label", label,
           "--seeds", ",".join(str(s) for s in seeds)]
    try:
        with tempfile.TemporaryDirectory(prefix="ab-host-cache-") as cache, tempfile.TemporaryDirectory(prefix="ab-host-tmp-") as temp:
            env = oracle.own_cache_env(env, cache)
            env.update(TMPDIR=temp, TEMP=temp, TMP=temp)
            return subprocess.run(cmd, env=env, timeout=RUN_TIMEOUT_S).returncode
    except subprocess.TimeoutExpired:
        return 124


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
        expected = load_expected(expected_file)
        report = judge(first, read_json(out / runs[2][1]), expected=expected, a_a=False)
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
    c = sub.add_parser("compare", help="two footprints: the diff and the verdict")
    c.add_argument("a", type=Path)
    c.add_argument("b", type=Path)
    c.add_argument("--out", type=Path)
    c.add_argument("--a-a", action="store_true", help="the control: no declared difference is applied; a difference is a broken control (exit 2)")
    c.add_argument("--expected", type=Path, default=EXPECTED_FILE)
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
    seeds = [int(s) for s in args.seeds.split(",")] if args.seeds else default_seeds()
    if args.cmd == "run":
        if sys.platform.startswith("linux") and not shutil.which("strace"):
            print("needs strace (apt install strace): without it the processes and the files of the run are not seen")
            return 3
        document = run_engine(args.binary, args.leg, seeds, args.label)
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
