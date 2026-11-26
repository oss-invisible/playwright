#!/usr/bin/env python3
"""A browser that ended with a code other than 0 is RED in every oracle, and what it left is saved (one place, used by the page, network and host oracles).

The three A/B oracles (scripts/ci_ab_oracle.py, scripts/ci_ab_network.py, scripts/ci_ab_host.py) launch many browsers and used to look at how each one ended only as
evidence: run 45030727459 had a firefox-68 parent die with -11 (SIGSEGV) in a normal close of 2.3 s, and nothing was kept to find out why. Now:

  * `exit_problem`: the exit code of the root (parent) process of a launch is 0 or the launch is a verdict line that names the seed, the run, the launch and the code.
    A launch whose code was not seen is red too (an oracle that does not see how its browsers end does not see them).
  * `collect`: for a launch that did not end with 0, on Linux the last 100 strace lines and the lines of the fatal signals of the whole log (the host oracle's
    trace; the page and network oracles run no strace, and say so), the core run through `gdb -batch` (`thread apply all bt`, the MOZ_CRASH reason, `info
    sharedlibrary`) with the build ids of the binary and of the core, and the crash reporter's `.extra` and the minidump names; on Windows the `.extra` files and the
    minidump list. Whatever a collector does not find is SAID (`said`), never left silent.
  * `LaunchProbe`: the observer the page and network oracles use. They launch through the wrapper, which keeps the browser's process inside the core's `Connection`;
    the probe wraps `Connection.__init__` and `Connection.close` (it reads the code after the close the launch already makes, and a watcher thread of the same
    crash folders that the host oracle's launches read too), so nothing about the launch changes: same environment, same prefs, same durations, same launch count.

Three more things the probe keeps, in EVERY launch and not only in a bad one:
  * what the browser wrote (`stderr/<launch>.txt` in the crash folder): the core reads the browser's stdout and stderr (one pipe) through `ProcessOutput`, one reader for the whole life of
    the process; the probe puts a tee in front of that reader, so the bytes are the ones the reader gets, the whole output and not the last 200 lines the core keeps, and the browser
    cannot tell. A launch whose output could not be tee'd says so (`stderr_said`), and that is red;
  * the cores written since the launch began (Linux): a core of a content process or of the parent goes through gdb whatever the code of the parent was (a tab can die and the parent
    end with 0); a launch with a core, or with a minidump, whose parent ended with 0 is red, and a launch with none says where it looked (`core_said`);
  * a tab that died is not here: it is the dump's leaf `crash.tab.<launch>` (`tab_lost`, `tab_crash_item`), which the page oracle's comparison puts first.

A probe never changes the outcome it observes: nothing here sets an environment variable of the browser, a pref, a crash reporter option or a working directory.
Cores are enabled the one way that cannot reach a launch that does not crash: the soft core limit of the oracle's own process is raised to the hard one (a child
inherits it), and where the kernel writes the core is the job's `kernel.core_pattern` (`INVPW_CORE_DIR` names that folder).
"""
from __future__ import annotations

import contextlib
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

FORMAT = "launch-exits/1"
#: The folder the job points the kernel's core pattern at (`sudo sysctl kernel.core_pattern=$INVPW_CORE_DIR/core.%e.%p.%t`).
CORE_DIR_ENV = "INVPW_CORE_DIR"
#: Where an oracle's launch probe writes what it saw (a folder of the oracle's artifact), named by the oracle that starts the run process.
CRASH_DIR_ENV = "AB_CRASH_DIR"
#: Where the probe saves what each browser wrote (a folder of the crash folder), and the most of it one launch keeps (the core's own reader keeps 200 lines; a launch writes a few KB).
STDERR_DIR = "stderr"
STDERR_LIMIT = 4 * 1024 * 1024
#: The prefix of the leaf a dump records when the tab of a visit died or its session was lost, and what says it (the lowercased text of the error: the core's `cannot find session`,
#: the client's `Target closed` / `has been closed`, a closed pipe, the page's own `crashed`).
TAB_PREFIX = "crash.tab."
TAB_LOST = ("cannot find session", "target closed", "targetclosederror", "session closed", "has been closed", "pipe is closed", "pipe closed", "page crashed", "tab crashed")
STRACE_TAIL_LINES = 100
SIGNAL_LINES = 20
def argv_and_env_keys(word, cstring, argc, argv, environ, most=1024):
    """The command line of a dead process and the NAMES of its environment, read from its memory: `word(address)` reads 8 bytes (little endian), `cstring(address)`
    a NUL-ended string; `argc`/`argv` are glibc's `__libc_argc`/`__libc_argv` (the arguments main received), `environ` is libc's `environ` (the environment at the
    end). Only the names of the variables are returned, never their values (a CI environment carries tokens). ([args], [names], what could not be read)."""
    args, names, problems = [], [], []
    if not 0 <= argc <= most:
        problems.append("argc %r is not a count" % (argc,))
        argc = 0
    for i in range(argc):
        try:
            args.append(cstring(word(argv + 8 * i)))
        except Exception as problem:
            problems.append("argv[%d]: %s" % (i, problem))
            break
    at = environ
    while at and len(names) < most:
        try:
            pointer = word(at)
            if pointer == 0:
                break
            names.append(cstring(pointer).split("=", 1)[0])
        except Exception as problem:
            problems.append("environ: %s" % (problem,))
            break
        at += 8
    return args, names, problems


#: The readers gdb gives `argv_and_env_keys` (its memory of the core), and what it prints. Which process died (a parent, a background task, a content process) is in
#: its arguments; the name of the crashing thread only says which thread. The core of run 45755178008 named a thread FSBroker7536 of a process the oracle did not launch.
ARGV_GDB_READERS = r'''
import gdb
_inf = gdb.selected_inferior()
def _word(a):
    return int.from_bytes(bytes(_inf.read_memory(a, 8)), "little")
def _cstring(a):
    out = b""
    while len(out) < 65536:
        chunk = bytes(_inf.read_memory(a + len(out), 64))
        if b"\0" in chunk:
            return (out + chunk.split(b"\0", 1)[0]).decode("utf-8", "replace")
        out += chunk
    return out.decode("utf-8", "replace")
def _value(expression):
    try:
        return int(gdb.parse_and_eval(expression))
    except Exception as problem:
        print("process: %s unreadable: %s" % (expression, problem))
        return 0
'''
ARGV_GDB_PRINT = r'''
_args, _names, _problems = argv_and_env_keys(_word, _cstring, _value("*(int*)&__libc_argc"), _value("(long)*(char***)&__libc_argv"),
                                             _value("(long)*(char***)&environ"))
print("process argv (%d): %r" % (len(_args), _args))
print("process environment names (%d): %s" % (len(_names), " ".join(sorted(set(_names)))))
for _p in _problems:
    print("process: %s" % _p)
'''


def argv_gdb_command() -> str:
    """The one gdb command (`python exec(...)`) that prints the dead process's arguments and the names of its environment (`argv_and_env_keys`)."""
    import inspect
    return "python exec(%r)" % (ARGV_GDB_READERS + inspect.getsource(argv_and_env_keys) + ARGV_GDB_PRINT,)


#: The gdb commands: every thread's stack, the reason MOZ_CRASH wrote, the libraries the core mapped, and which process it was (its arguments).
GDB_COMMANDS = ("thread apply all bt", "x/s *(char**)&gMozCrashReason", "info sharedlibrary", argv_gdb_command())
GDB_LIMIT = 400_000
CORE_LIMIT = 3
#: The exit code of Linux processes that a signal ended is the negative of its number (Python's `Popen`).
SIGNALS = {1: "SIGHUP", 2: "SIGINT", 3: "SIGQUIT", 4: "SIGILL", 5: "SIGTRAP", 6: "SIGABRT", 7: "SIGBUS", 8: "SIGFPE", 9: "SIGKILL", 10: "SIGUSR1", 11: "SIGSEGV",
           12: "SIGUSR2", 13: "SIGPIPE", 14: "SIGALRM", 15: "SIGTERM", 31: "SIGSYS"}
#: The NTSTATUS values a Windows crash gives as the exit code of the process.
NTSTATUS = {0xC0000005: "access violation", 0xC0000409: "stack buffer overrun / fast fail", 0xC000013A: "control-C", 0x80000003: "breakpoint",
            0xC0000374: "heap corruption", 0xC00000FD: "stack overflow", 0xC000001D: "illegal instruction", 0xC0000094: "integer divide by zero"}
_FATAL = re.compile(r"^\d+\s+--- SIG(?:SEGV|BUS|ABRT|ILL|FPE|SYS|TRAP|TERM|INT|QUIT|HUP)\b")
#: A line of the strace log that is a signal delivered (`1234 --- SIGCHLD {...} ---`): never data of the comparison.
SIGNAL_LINE = re.compile(r"^\d+\s+--- SIG")


# -
# The verdict line.

def describe_exit(code, windows: bool | None = None) -> str:
    """`-11 (SIGSEGV)`, `0xC0000005 (access violation)`, `1`, or what it is when the code was not seen."""
    if code is None:
        return "no code (the exit code was not seen)"
    windows = (sys.platform == "win32") if windows is None else windows
    if windows and (code < 0 or code >= 0x80000000):
        value = code & 0xFFFFFFFF
        name = NTSTATUS.get(value)
        return "0x%08X (%s)" % (value, name) if name else "0x%08X" % value
    if code < 0:
        return "%d (%s)" % (code, SIGNALS.get(-code, "signal %d" % -code))
    return str(code)


def tab_lost(error) -> bool:
    """Does this error (an exception, or its text) say that the tab died or the session was lost? The dump records that as a leaf of its own (`tab_crash_item`) and goes on."""
    text = ("%s: %s" % (type(error).__name__, error)) if isinstance(error, BaseException) else str(error)
    text = text.lower()
    return any(word in text for word in TAB_LOST)


def tab_crash_item(launch: str, step: str, error) -> dict:
    """The item a dump adds to a visit whose tab died: `{"crash.tab.<launch>": {"error": ..., "step": ...}}`, the error text and the step of the visit it came in."""
    text = ("%s: %s" % (type(error).__name__, error)) if isinstance(error, BaseException) else str(error)
    return {TAB_PREFIX + launch: {"error": text[:300], "step": str(step)[:200]}}


#: The order the parts of a launch's name are told in (a JSON file writes its keys sorted).
LABEL_ORDER = ("seed", "dpr", "launch")


def side_of(label: str) -> str:
    """The side of a run in the A/A and the A/B: the published engine is A, its second run and the candidate are B."""
    label = str(label)
    return "B" if label.endswith("-again") or label.endswith(" again") or label.startswith("candidate") else "A"


def exit_problem(code, windows: bool | None = None, **who) -> str | None:
    """The red line of a launch that did not end with 0, or None when it did. `who` is what names the launch (run, side, seed, dpr, launch ...)."""
    if code == 0:
        return None
    where = ", ".join("%s %s" % (k.replace("_", " "), v) for k, v in who.items() if v not in (None, ""))
    return "the parent process exited with code %s: %s" % (describe_exit(code, windows), where or "launch not named")


def exit_problems(launches, run: str, windows: bool | None = None, side: str | None = None) -> list:
    """The red lines of the launches a probe recorded (`LaunchProbe.launches`, or the `launches` of an exits file) for the run `run`."""
    out = []
    for launch in launches or []:
        who = {"run": run, "side": side or side_of(run)}
        label = launch.get("label") or {}
        who.update({k: label[k] for k in sorted(label, key=lambda k: (LABEL_ORDER.index(k) if k in LABEL_ORDER else len(LABEL_ORDER), k))})
        who["launch number"] = launch.get("n")
        line = exit_problem(launch.get("returncode"), windows, **who)
        if line:
            if launch.get("evidence"):
                line += " (evidence: %s)" % launch["evidence"]
            out.append(line)
        elif launch.get("cores") or launch.get("dmp"):
            # the parent ended with 0 and a process of the browser left a core or a minidump: a content process died (the parent does not say so)
            line = "a process of the browser crashed while the parent ended with 0: %s left %s" % (
                ", ".join("%s %s" % kv for kv in who.items() if kv[1] not in (None, "")),
                " and ".join(x for x in ("core " + ", ".join(launch["cores"]) if launch.get("cores") else "", "minidump " + ", ".join(launch["dmp"]) if launch.get("dmp") else "") if x))
            if launch.get("evidence"):
                line += " (evidence: %s)" % launch["evidence"]
            out.append(line)
    return out


def parent_exit_problems(lifecycle, root_code, **who) -> list:
    """Windows (and strace, which names it too): the exit of a process of the kind `parent` that the lifecycle says was not `exited 0`, when the root's own code did not
    say it (the process the core spawns may be a launcher stub that exits by itself)."""
    if root_code not in (0, None):
        return []
    out = []
    for e in lifecycle or []:
        if e.get("event") == "end" and e.get("kind") == "parent" and e.get("how") not in ("exited 0", "exited 0 "):
            out.append("the process of the kind parent ended with `%s` though the root's code was %s: %s"
                       % (e.get("how"), "0" if root_code == 0 else "not seen", ", ".join("%s %s" % kv for kv in who.items() if kv[1] not in (None, ""))))
    return out


# -
# What a crash leaves: the folders, the files, the cores (the collectors that scripts/ci_ab_host.py and the probe's launches share live here).

def crash_report_dirs(profile_dir, windows: bool) -> list:
    """The folders a crash leaves its `.dmp` and `.extra` in: the profile's `minidumps` and the `Crash Reports/pending` of the user's application data."""
    dirs = [Path(profile_dir) / "minidumps"] if profile_dir else []
    if windows:
        base = os.environ.get("APPDATA")
        if base:
            dirs.append(Path(base) / "Mozilla" / "Firefox" / "Crash Reports" / "pending")
    else:
        home = Path(os.path.expanduser("~"))
        dirs += [home / ".config" / "mozilla" / "firefox" / "Crash Reports" / "pending", home / ".mozilla" / "firefox" / "Crash Reports" / "pending"]
    return dirs


def scan_crash_files(dirs, seen: dict) -> None:
    """Adds to `seen` ({path: {"name", "dir", "size", "content"}}) every `.dmp` and `.extra` of `dirs`: a `.extra` is read at once, as the profile is deleted when the
    launch ends, and again while its size keeps changing (a file seen half written)."""
    for folder in dirs:
        try:
            entries = list(Path(folder).iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.suffix not in (".dmp", ".extra"):
                continue
            try:
                size = entry.stat().st_size
                if str(entry) in seen and seen[str(entry)]["size"] == size:
                    continue
                item = {"name": entry.name, "dir": str(entry.parent), "size": size}
                if entry.suffix == ".extra":
                    item["content"] = entry.read_text(encoding="utf-8", errors="replace")[:20000]
            except OSError:
                continue
            seen[str(entry)] = item


class CrashWatcher:
    """A thread that looks at the crash folders while the launch lives, so that what a crash leaves is read before the launch deletes its profile.

    `ignore` is a set of paths that were there before the launch (the user's `Crash Reports/pending` keeps reports of earlier crashes): they are not this launch's."""

    def __init__(self, dirs_fn, period: float = 0.25, ignore=()) -> None:
        self.dirs_fn, self.period = dirs_fn, period
        self.ignore = set(ignore)
        self.seen: dict = {}
        self.scans = 0
        self._halt = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ab-crash-watch", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def scan_once(self) -> None:
        """One look at the crash folders (what the thread does every period, and `stop` once more): a file seen empty or half written is read again when its size changes."""
        scan_crash_files(self.dirs_fn(), self.seen)
        self.scans += 1

    def _run(self) -> None:
        while not self._halt.is_set():
            self.scan_once()
            self._halt.wait(self.period)

    def stop(self) -> dict:
        self._halt.set()
        if self._thread.is_alive():
            self._thread.join(timeout=10)
        self.scan_once()
        files = [f for f in self.seen.values() if os.path.join(f["dir"], f["name"]) not in self.ignore]
        return {"files": sorted(files, key=lambda f: f["name"]), "scans": self.scans}


def existing_crash_files(dirs) -> set:
    """The paths of the crash files that are there now (what a launch that starts after this look did not make)."""
    seen: dict = {}
    scan_crash_files(dirs, seen)
    return {os.path.join(f["dir"], f["name"]) for f in seen.values()}


def core_dirs(env=None, pattern=None) -> list:
    """The folders a core of this machine can be in: the job's `INVPW_CORE_DIR` and the folder of the kernel's `core_pattern` when that is an absolute path (a pipe to
    apport or systemd-coredump leaves no file)."""
    env = os.environ if env is None else env
    out = []
    if env.get(CORE_DIR_ENV):
        out.append(Path(env[CORE_DIR_ENV]))
    if pattern is None:
        pattern = kernel_core_pattern()
    if pattern and not pattern.startswith("|") and pattern.startswith("/"):
        out.append(Path(pattern).parent)
    return out


def kernel_core_pattern():
    try:
        return Path("/proc/sys/kernel/core_pattern").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def find_cores(dirs, since=None, slack: float = 5.0) -> list:
    """The `core*` files of `dirs` written at `since` or after (a file of an earlier launch is not this one's), newest first."""
    found = []
    for folder in dirs:
        try:
            entries = list(Path(folder).glob("core*"))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_file() and (since is None or entry.stat().st_mtime >= since - slack):
                    found.append((entry.stat().st_mtime, entry))
            except OSError:
                continue
    return [p for _t, p in sorted(found, key=lambda x: x[0], reverse=True)]


def core_info(core_dir: Path, binary: str, run=subprocess.run, commands=("bt",), more_dirs=(), since=None, limit: int = 8000, which=None) -> dict:
    """What a Linux core dump of a launch is: the kernel's core pattern (a pipe to apport or systemd-coredump puts no file in `core_dir`), the cores found there (and in
    `more_dirs`, written at `since` or after) and what gdb says of each (`gdb -batch` with `commands`, `bt` by default) when gdb is installed."""
    pattern = kernel_core_pattern()
    cores = sorted(p for p in Path(core_dir).glob("core*") if p.is_file()) if core_dir else []
    if more_dirs:
        cores += [c for c in find_cores(more_dirs, since) if c not in cores]
    out = {"core_pattern": pattern, "cores": [c.name for c in cores], "backtraces": {}}
    gdb = (which or shutil.which)("gdb")
    for core in cores[:CORE_LIMIT]:
        if gdb:
            argv = [gdb, "-batch"]
            for command in commands:
                argv += ["-ex", command]
            done = run(argv + [str(binary), str(core)], capture_output=True, text=True, errors="replace", timeout=300)
            out["backtraces"][core.name] = (done.stdout + done.stderr)[-limit:]
        else:
            out["backtraces"][core.name] = "gdb is not installed"
    return out


def build_ids(binary: str, core: Path | None, run=subprocess.run, which=None) -> dict:
    """The build ids of the engine: `readelf -n` of the binary and of the `libxul.so` beside it, and `eu-unstrip -n --core` (every module the core mapped, with its id)
    when elfutils is installed. A tool that is not there is said."""
    which = which or shutil.which
    out: dict = {}
    readelf, unstrip = which("readelf"), which("eu-unstrip")
    here = Path(binary).parent
    for name, path in (("binary", Path(binary)), ("libxul", here / "libxul.so")):
        if not path.exists():
            out[name] = "%s does not exist" % path
        elif not readelf:
            out[name] = "readelf is not installed"
        else:
            done = run([readelf, "-n", str(path)], capture_output=True, text=True, errors="replace", timeout=60)
            ids = [line.strip() for line in done.stdout.splitlines() if "Build ID" in line]
            out[name] = ids[0] if ids else "no build id in %s" % path
    if core is not None:
        if not unstrip:
            out["core"] = "eu-unstrip is not installed"
        else:
            done = run([unstrip, "-n", "--core", str(core)], capture_output=True, text=True, errors="replace", timeout=120)
            out["core"] = (done.stdout + done.stderr)[-GDB_LIMIT:]
    return out


def strace_tail(log, n: int = STRACE_TAIL_LINES) -> list:
    """The last `n` lines of a strace log (read from the end: the log of a closed run is tens of megabytes)."""
    with open(log, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos, data = f.tell(), b""
        while pos > 0 and data.count(b"\n") <= n:
            step = min(65536, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    return data.decode("utf-8", errors="replace").splitlines()[-n:]


def strace_signals(log, limit: int = SIGNAL_LINES) -> list:
    """`[line number, text]` of the last fatal-signal lines of the whole strace log (`--- SIGSEGV {si_addr=...} ---`): they come before a long tail of syscalls of the
    crash reporter, so the last 100 lines may not hold them."""
    kept: list = []
    with open(log, encoding="utf-8", errors="replace") as f:
        for number, line in enumerate(f, 1):
            if _FATAL.match(line):
                kept.append([number, line.rstrip("\n")])
                if len(kept) > limit:
                    kept.pop(0)
    return kept


def collect(code, binary: str, *, strace_log=None, strace_said: str | None = None, watched: dict | None = None, core_folders=(), since=None, windows: bool | None = None,
            run=subprocess.run, which=None, cores=None) -> dict:
    """Everything a launch that did not end with 0 (or that left a core or a minidump) left, and what could not be found, said: {"exit", "strace", "cores", "crash_files", "said"}.
    `cores` is the list of cores this launch is given (the probe keeps a core for the first launch that finds it); None looks in `core_folders` for those written since `since`."""
    windows = (sys.platform == "win32") if windows is None else windows
    said: list = []
    out: dict = {"exit": {"code": code, "text": describe_exit(code, windows)}}
    if windows:
        out["strace"] = None
        out["cores"] = None
        said.append("no strace and no core on Windows: the minidumps and the `.extra` are what a Windows crash leaves")
    else:
        if strace_log is None:
            out["strace"] = None
            said.append(strace_said or "this oracle runs no strace (a trace slows the browser and would change the outcome it observes)")
        else:
            try:
                out["strace"] = {"tail": strace_tail(strace_log), "signals": strace_signals(strace_log)}
                if not out["strace"]["signals"]:
                    said.append("the strace log holds no fatal signal line")
            except OSError as e:
                out["strace"] = None
                said.append("the strace log could not be read: %s" % e)
        cores = find_cores(core_folders, since) if cores is None else list(cores)
        info = {"core_pattern": kernel_core_pattern(), "folders": [str(d) for d in core_folders], "cores": [c.name for c in cores], "backtraces": {}, "build_ids": {}}
        if not cores:
            said.append("no core file written since the launch began, in %s (core_pattern %s)" % (", ".join(str(d) for d in core_folders) or "no folder", info["core_pattern"]))
            info["build_ids"]["binary only (no core)"] = build_ids(binary, None, run, which)
        for core in cores[:CORE_LIMIT]:
            info["backtraces"][core.name] = gdb_report(binary, core, run, which)
            info["build_ids"][core.name] = build_ids(binary, core, run, which)
        out["cores"] = info
    files = (watched or {}).get("files", [])
    out["crash_files"] = {"dmp": [f["name"] for f in files if f["name"].endswith(".dmp")], "extra": [{"name": f["name"], "content": f.get("content", "")} for f in files
                                                                                                    if f["name"].endswith(".extra")], "scans": (watched or {}).get("scans")}
    if not files:
        said.append("no minidump and no `.extra` in the crash folders (the crash reporter may be off, or the process ended before it wrote them)")
    out["said"] = said
    return out


def gdb_report(binary: str, core: Path, run=subprocess.run, which=None) -> str:
    """`gdb -batch` over a core: every thread's backtrace, the MOZ_CRASH reason and the shared libraries; or the reason it could not be asked."""
    gdb = (which or shutil.which)("gdb")
    if not gdb:
        return "gdb is not installed"
    argv = [gdb, "-batch"]
    for command in GDB_COMMANDS:
        argv += ["-ex", command]
    try:
        done = run(argv + [str(binary), str(core)], capture_output=True, text=True, errors="replace", timeout=300)
    except subprocess.TimeoutExpired:
        return "gdb did not finish within 300 s"
    return (done.stdout + done.stderr)[:GDB_LIMIT]


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80]


def write_crash_files(crash: dict, directory) -> list:
    """The evidence of `collect` as files a person can read in the artifact (`strace-tail.txt`, `strace-signals.txt`, `gdb-<core>.txt`, `build-ids.txt`, `<name>.extra.txt`,
    `minidumps.txt`, `said.txt`) and `crash.json`; returns the names written."""
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    written = []

    def put(name, text):
        (folder / name).write_text(text, encoding="utf-8", newline="\n")
        written.append(name)

    strace = crash.get("strace")
    if strace:
        put("strace-tail.txt", "\n".join(strace["tail"]) + "\n")
        put("strace-signals.txt", "\n".join("%d: %s" % (n, t) for n, t in strace["signals"]) + "\n")
    cores = crash.get("cores")
    if cores:
        for name, text in cores["backtraces"].items():
            put("gdb-%s.txt" % _safe(name), text)
        ids = "\n\n".join("%s\n%s" % (name, json.dumps(value, indent=1, sort_keys=True)) for name, value in sorted(cores["build_ids"].items()))
        if ids:
            put("build-ids.txt", ids + "\n")
    files = crash.get("crash_files") or {}
    for item in files.get("extra", []):
        put("%s.txt" % _safe(item["name"]), item.get("content", "") + "\n")
    put("minidumps.txt", "\n".join(files.get("dmp", [])) + ("\n" if files.get("dmp") else ""))
    put("said.txt", "\n".join(crash.get("said", [])) + "\n")
    put("crash.json", json.dumps(crash, indent=1, sort_keys=True) + "\n")
    return written


# -
# The probe of the oracles that launch through the wrapper (the page and network oracles).

def raise_core_limit() -> str:
    """Linux: the soft core limit of this process becomes the hard one (every browser it launches inherits it); what was done, or why not."""
    try:
        import resource
    except ImportError:
        return "not Linux: no core limit"
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
        if soft != hard:
            resource.setrlimit(resource.RLIMIT_CORE, (hard, hard))
        return "RLIMIT_CORE %s" % ("unlimited" if hard == resource.RLIM_INFINITY else hard)
    except (OSError, ValueError) as e:
        return "RLIMIT_CORE could not be raised: %s" % e


class OutputRecord:
    """What one launch's browser wrote (stdout and stderr: the core merges them on one pipe), kept whole by the tee that sits in front of the core's own reader."""

    def __init__(self, n: int, label: dict, started: float) -> None:
        self.n, self.label, self.started = n, dict(label), started
        self.output = None   # the core's ProcessOutput this belongs to (set once it exists)
        self.size, self.truncated, self._chunks = 0, False, []
        self._lock = threading.Lock()
        self.written = None  # the file the probe saved it in, relative to the artifact

    def add(self, line) -> None:
        data = line if isinstance(line, bytes) else str(line).encode("utf-8", "replace")
        with self._lock:
            if self.size + len(data) > STDERR_LIMIT:
                self.truncated = True
                return
            self._chunks.append(data)
            self.size += len(data)

    def text(self) -> str:
        with self._lock:
            return b"".join(self._chunks).decode("utf-8", "replace")


class OutputTee:
    """The stream the core's `ProcessOutput` reads, with every line it reads copied to a record: the same lines in the same order to the same reader, and nothing written to the browser."""

    def __init__(self, stream, record: OutputRecord) -> None:
        self._stream, self._record = stream, record

    def readline(self, *args):
        line = self._stream.readline(*args)
        if line:
            try:
                self._record.add(line)
            except Exception:  # noqa: BLE001 - a probe never breaks a launch
                pass
        return line

    def __getattr__(self, name):
        return getattr(self._stream, name)


def core_said(folders, pattern, windows: bool) -> str | None:
    """Where a launch looked for a core and found none (Linux), told so that a missing core is never silent: and, when the job's folder is not where the kernel writes, that a core would not have been seen."""
    if windows:
        return "Windows: no core (the minidumps and the `.extra` are what a crash leaves)"
    where = ", ".join(str(f) for f in folders)
    if not where:
        return "no core folder to look in: INVPW_CORE_DIR is not set and core_pattern %r is not a path (a core, if one was written, was not seen)" % (pattern,)
    said = "no core file written since the launch began, in %s (core_pattern %s)" % (where, pattern)
    if os.environ.get(CORE_DIR_ENV) and not (pattern or "").startswith(str(Path(os.environ[CORE_DIR_ENV]))):
        said += ": the kernel does not write into INVPW_CORE_DIR, so a core would not have been seen there"
    return said


class LaunchProbe:
    """Sees every browser this process launches through the core (`invisible_core.juggler.connection.Connection`): how its parent ended, and what a bad end left.

    `install` wraps `Connection.__init__` (the launch has begun: a crash watcher starts) and `Connection.close` (the launch is over: the exit code is read with `poll()`
    after the close the launch already makes). The oracle names the launch it is about to make with `set_label` (seed, launch ...)."""

    def __init__(self, binary: str, crash_dir=None, windows: bool | None = None, run=subprocess.run, clock=time.time, which=None, folders=None, core_folders=None) -> None:
        self.binary = binary
        self.core_folders = core_folders
        self.crash_dir = Path(crash_dir) if crash_dir else None
        self.windows = (sys.platform == "win32") if windows is None else windows
        self.run, self.clock, self.which = run, clock, which
        self.folders = folders
        self.label: dict = {}
        self.launches: list = []
        self._open: dict = {}
        self._lock = threading.Lock()
        self._patched = None
        self._patched_output = None
        self.core_limit = None
        #: what the browsers wrote, a record per launch, in the order of their birth; and why no output could be kept, when none could
        self.records: list = []
        self.stderr_said = None
        self.unconnected: list = []
        #: the cores a launch has been given: one core is one launch's
        self._claimed: set = set()

    def set_label(self, **label) -> None:
        self.label = dict(label)

    def install(self, connection=None, output=None) -> "LaunchProbe":
        """Wraps `Connection.__init__` and `close`, and puts a tee in front of the reader of the browser's output (`ProcessOutput.__init__`). With a `connection` of the caller's (a
        stand-in) the output class is the caller's too, or none."""
        if connection is None:
            from invisible_core.juggler.connection import Connection as connection
            from invisible_core.juggler import connection as module

            output = getattr(module, "ProcessOutput", None)
        probe, original_init, original_close = self, connection.__init__, connection.close
        if output is None:
            self.stderr_said = "the core has no `ProcessOutput` to put a tee in front of: what the browsers write is not saved"
        else:
            original_output = output.__init__

            def output_init(out, stream, *args, **kwargs):
                record = probe._born()
                try:
                    original_output(out, OutputTee(stream, record), *args, **kwargs)
                finally:
                    record.output = out

            output_init.__wrapped__ = original_output
            output.__init__ = output_init
            self._patched_output = (output, original_output)

        def __init__(conn, *args, **kwargs):
            original_init(conn, *args, **kwargs)
            probe._began(conn)

        def close(conn, *args, **kwargs):
            try:
                return original_close(conn, *args, **kwargs)
            finally:
                probe._ended(conn)

        __init__.__wrapped__, close.__wrapped__ = original_init, original_close
        connection.__init__, connection.close = __init__, close
        self._patched = (connection, original_init, original_close)
        if not self.windows:
            self.core_limit = raise_core_limit()
        return self

    def _born(self) -> OutputRecord:
        with self._lock:
            record = OutputRecord(len(self.records) + 1, self.label, self.clock())
            self.records.append(record)
        return record

    def uninstall(self) -> None:
        if self._patched_output:
            output, original = self._patched_output
            output.__init__ = original
            self._patched_output = None
        if self._patched:
            connection, original_init, original_close = self._patched
            connection.__init__, connection.close = original_init, original_close
            self._patched = None

    def _dirs(self):
        if self.folders is not None:
            return self.folders() if callable(self.folders) else list(self.folders)
        tmp = Path(tempfile.gettempdir())
        profiles = []
        try:
            profiles = sorted(tmp.glob("invisible_profile_*"))
        except OSError:
            pass
        return [p / "minidumps" for p in profiles] + crash_report_dirs(None, self.windows)

    def _began(self, conn) -> None:
        try:
            if getattr(conn, "_process", None) is None:
                return
            watcher = CrashWatcher(self._dirs, ignore=existing_crash_files(self._dirs()))
            watcher.start()
            with self._lock:
                self._open[id(conn)] = {"label": dict(self.label), "started": self.clock(), "watcher": watcher, "pid": getattr(conn._process, "pid", None)}
        except Exception:  # noqa: BLE001 - a probe never breaks a launch
            pass

    def _core_folders(self) -> list:
        if self.core_folders is not None:
            return list(self.core_folders() if callable(self.core_folders) else self.core_folders)
        return list(core_dirs())

    def _ended(self, conn) -> None:
        with self._lock:
            state = self._open.pop(id(conn), None)
        if state is None:
            return
        entry = {"n": len(self.launches) + 1, "label": state["label"], "pid": state["pid"], "returncode": None}
        try:
            try:
                entry["returncode"] = conn._process.poll()
            except Exception as e:  # noqa: BLE001
                entry["probe_error"] = "poll failed: %s" % e
            watched = state["watcher"].stop()
            folders = [] if self.windows else self._core_folders()
            cores = [] if self.windows else [c for c in find_cores(folders, state["started"]) if c.name not in self._claimed]
            dmp = [f["name"] for f in watched.get("files", []) if f["name"].endswith(".dmp")]
            self._claimed.update(c.name for c in cores)
            # EVERY launch: what the browser wrote is saved, and the cores since it began (a content process can die and the parent still end with 0)
            self._save_output(conn, entry)
            entry["cores"], entry["dmp"] = [c.name for c in cores], dmp
            if not cores:
                entry["core_said"] = core_said(folders, kernel_core_pattern(), self.windows)
            if entry["returncode"] != 0 or cores or dmp:
                crash = collect(entry["returncode"], self.binary, watched=watched, core_folders=folders, since=state["started"], windows=self.windows,
                                run=self.run, which=self.which, cores=cores, strace_said="this oracle runs no strace (a trace slows the browser and would change the outcome it observes)")
                if self.crash_dir is not None:
                    name = "%02d-%s" % (entry["n"], _safe("-".join(str(v) for v in state["label"].values()) or "launch"))
                    write_crash_files(crash, self.crash_dir / name)
                    entry["evidence"] = "%s/%s" % (self.crash_dir.name, name)
                entry["said"] = crash["said"]
        except Exception as e:  # noqa: BLE001 - what could not be collected is said
            entry["probe_error"] = "%s: %s" % (type(e).__name__, e)
        with self._lock:
            self.launches.append(entry)

    # -
    # What the browsers wrote.

    def _record_of(self, conn) -> OutputRecord | None:
        output = getattr(conn, "output", None)
        if output is None:
            return None
        with self._lock:
            return next((r for r in self.records if r.output is output), None)

    def _output_file(self, record: OutputRecord, suffix: str = "") -> str:
        return "%02d-%s%s.txt" % (record.n, _safe("-".join(str(v) for v in record.label.values()) or "launch"), suffix)

    def _write_output(self, record: OutputRecord, code=None, suffix: str = "", said: str = "") -> str | None:
        """The browser's output of one launch, as a file of the artifact: `stderr/<NN>-<launch>.txt`, whatever the launch's end was."""
        if self.crash_dir is None:
            return None
        folder = self.crash_dir / STDERR_DIR
        folder.mkdir(parents=True, exist_ok=True)
        name = self._output_file(record, suffix)
        head = ["# launch %d: %s" % (record.n, ", ".join("%s %s" % kv for kv in record.label.items()) or "not named"),
                "# stdout and stderr of the browser, merged on one pipe by the core, as the core's reader got them: %d bytes%s" % (
                    record.size, " (kept the first %d, then the rest was dropped)" % STDERR_LIMIT if record.truncated else ""),
                "# the parent's exit code when this was written: %s" % (describe_exit(code, self.windows) if code is not None else "not seen")]
        if said:
            head.append("# " + said)
        (folder / name).write_text("\n".join(head) + "\n" + record.text(), encoding="utf-8", newline="\n")
        record.written = "%s/%s/%s" % (self.crash_dir.name, STDERR_DIR, name)
        return record.written

    def _save_output(self, conn, entry: dict) -> None:
        record = self._record_of(conn)
        if record is None:
            entry["stderr_said"] = self.stderr_said or "this launch's output was not seen by the tee (the core's `ProcessOutput` of this connection was not made while the probe was installed)"
            return
        entry["stderr_bytes"] = record.size
        path = self._write_output(record, entry["returncode"])
        if path:
            entry["stderr"] = path

    def finish(self) -> None:
        """At the end of the dump: every launch's output once more, after its reader has had the time to read to the end (a launch's file is written when it closes, and again here),
        and the output of a launch that never made a connection (it died while starting: the error said what it printed, the file says all of it)."""
        for record in list(self.records):
            try:
                if record.output is not None:
                    record.output.settle()
            except Exception:  # noqa: BLE001
                pass
            connected = next((x for x in self.launches if x.get("stderr") == record.written and record.written), None)
            if connected is not None:
                self._write_output(record, connected.get("returncode"))
            else:
                path = self._write_output(record, suffix="-no-connection", said="no connection was made over this launch (it ended while starting, or was still open)")
                if path:
                    self.unconnected.append({"label": record.label, "stderr": path, "bytes": record.size})

    def document(self) -> dict:
        return {"format": FORMAT, "core_limit": self.core_limit, "stderr_said": self.stderr_said, "launches": list(self.launches), "launches_without_connection": list(self.unconnected)}

    def write(self, path=None) -> Path | None:
        path = Path(path) if path else (self.crash_dir / "exits.json" if self.crash_dir else None)
        if path is None:
            return None
        try:
            self.finish()
        except Exception as e:  # noqa: BLE001 - said, not raised
            self.stderr_said = (self.stderr_said or "") + " finish: %s: %s" % (type(e).__name__, e)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.document(), indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        return path


#: The probe of this process, when its oracle installed one (the page oracle's dump marks the launches it makes through `mark`).
PROBE: LaunchProbe | None = None


def start_probe(binary: str, crash_dir) -> LaunchProbe:
    """Installs the probe of this process (once) for the oracle run that has a crash folder."""
    global PROBE
    if PROBE is not None:
        PROBE.uninstall()
    PROBE = LaunchProbe(binary, crash_dir).install()
    return PROBE


def stop_probe() -> LaunchProbe | None:
    global PROBE
    probe, PROBE = PROBE, None
    if probe is not None:
        probe.uninstall()
    return probe


def mark(**label) -> None:
    """Names the launch the oracle is about to make (no probe installed: nothing happens)."""
    if PROBE is not None:
        PROBE.set_label(**label)


#: The file Glean's database writes on its first write, in the profile of a launch (the folder `db` is born when the database is opened, the file at the first write).
GLEAN_DB = Path("datareporting", "glean", "db", "data.safe.bin")
#: A safety net, not a measurement: the file is there at about +8 s in normal sessions, stock and ours (run 43649596941).
GLEAN_LIMIT_S = 60.0


class GleanNotReady(Exception):
    """Glean had not initialised (no database file in the profile) when the launch was to close."""


def wait_for_glean(profile: Path | None, limit_s: float = GLEAN_LIMIT_S, period: float = 0.25, sleep=time.sleep, now=time.monotonic) -> float:
    """The seconds waited until `GLEAN_DB` exists in `profile`, read again every `period` seconds; `GleanNotReady` when `limit_s` is over or the profile is not known.

    Why: a session that closes before Glean has initialised normally makes Glean start inside the shutdown, where its `glean.init` thread can die with the panic "No database
    found" and the parent exits with -11 (Mozilla's race, the same in the retail browser; the network A/A, Linux, run 45121964904). The oracles close only after this. It only
    reads: nothing is written in the profile, and no preference of the browser changes."""
    if profile is None:
        raise GleanNotReady("Glean did not initialise in %g s: no %s in the profile of the launch: the profile of the launch is not known (the close would make it start inside the "
                            "shutdown, where it can crash: run 45121964904)" % (limit_s, GLEAN_DB.as_posix()))
    target = Path(profile) / GLEAN_DB
    start = now()
    while not target.is_file():
        waited = now() - start
        if waited >= limit_s:
            raise GleanNotReady("Glean did not initialise in %g s: no %s in the profile %s (the close would make it start inside the shutdown, where it can crash: run 45121964904)"
                                % (limit_s, GLEAN_DB.as_posix(), profile))
        sleep(period)
    return now() - start


#: Where a profile keeps what Glean and the data reporting policy write; its listing before the close is kept with the evidence of the launch (`save_datareporting`).
DATAREPORTING_DIR = "datareporting"


def datareporting_listing(profile) -> list:
    """The lines of the list of `<profile>/datareporting/`: `<path relative to it><TAB><size in bytes>` (a folder ends with `/` and has `-`), sorted. A line that starts with `# `
    says what could not be read: the list never raises."""
    if profile is None:
        return ["# the profile of the launch is not known"]
    root = Path(profile) / DATAREPORTING_DIR
    if not root.is_dir():
        return ["# %s is not a folder in the profile %s" % (DATAREPORTING_DIR, profile)]
    rows, problems = [], []
    for folder, names, files in os.walk(root, onerror=lambda e: problems.append("# cannot read %s: %s" % (getattr(e, "filename", "?"), e))):
        base = Path(folder)
        for name in names:
            rows.append("%s/\t-" % (base / name).relative_to(root).as_posix())
        for name in files:
            path = base / name
            try:
                size = str(path.stat().st_size)
            except OSError as exc:
                size = "?"
                problems.append("# cannot read the size of %s: %s" % (path.relative_to(root).as_posix(), exc))
            rows.append("%s\t%s" % (path.relative_to(root).as_posix(), size))
    return problems + sorted(rows)


def save_datareporting(profile) -> str | None:
    """Writes the list of `<profile>/datareporting/` of the launch the probe sees now, to `<crash dir>/datareporting/<NN>-<seed>-<launch>.tsv` (NN: the number of the launch, the
    one of its file in `stderr/`), and returns the path as the evidence names it. None when no probe is installed or it has no folder. What it could not read is in the file; it
    never raises. It is evidence for the next time a launch dies: it is not in the leaves that are compared."""
    probe = PROBE
    if probe is None or probe.crash_dir is None:
        return None
    try:
        with probe._lock:
            records = list(probe.records)
        number = getattr(records[-1], "n", len(records)) if records else 0
        name = "%02d-%s.tsv" % (number, _safe("-".join(str(v) for v in probe.label.values()) or "launch"))
        folder = probe.crash_dir / DATAREPORTING_DIR
        folder.mkdir(parents=True, exist_ok=True)
        head = ["# launch %d: %s" % (number, ", ".join("%s %s" % kv for kv in probe.label.items()) or "not named"), "# path (relative to datareporting/)<TAB>size in bytes"]
        (folder / name).write_text("\n".join(head + datareporting_listing(profile)) + "\n", encoding="utf-8", newline="\n")
        return "%s/%s/%s" % (probe.crash_dir.name, DATAREPORTING_DIR, name)
    except Exception:  # noqa: BLE001 - the evidence never breaks a launch
        return None


#: The line an oracle writes when it begins a launch (`launch_line`), and what reads it back (`last_launch`): the process that runs the oracle names, when the oracle does not end in
#: time, the launch that was alive.
LAUNCH_LINE = re.compile(r"^=== launch (?P<launch>\S+) seed (?P<seed>-?\d+) of (?P<label>.*?) began at (?P<at>\S+)\s*$", re.M)


def launch_line(seed, launch: str, label: str, at=None) -> str:
    """`=== launch <launch> seed <seed> of <label> began at <iso, UTC>`: what an oracle prints when `mark` names a launch."""
    import datetime

    at = at or datetime.datetime.now(datetime.timezone.utc)
    return "=== launch %s seed %s of %s began at %s" % (launch, seed, label or "?", at.isoformat(timespec="seconds"))


def last_launch(text: str, now=None) -> dict | None:
    """The last launch line of `text` as {launch, seed, label, at, alive_s} (alive_s: seconds from `at` to `now`, None when `at` does not read), or None when there is none."""
    import datetime

    found = None
    for found in LAUNCH_LINE.finditer(text or ""):
        pass
    if found is None:
        return None
    item = found.groupdict()
    try:
        began = datetime.datetime.fromisoformat(item["at"])
        item["alive_s"] = ((now or datetime.datetime.now(datetime.timezone.utc)) - began).total_seconds()
    except (ValueError, TypeError):
        item["alive_s"] = None
    return item


def kill_tree(pid: int) -> None:
    """Kills `pid` and everything that descends from it. With psutil the descendants are listed first (a parent that dies hides its children) and all are killed; without it
    Windows has `taskkill /T /F` and Linux the process group (the child was started in its own session, `run_tree`)."""
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            root = psutil.Process(pid)
            victims = root.children(recursive=True) + [root]
        except psutil.NoSuchProcess:
            victims = []
        for victim in victims:
            with contextlib.suppress(psutil.Error):
                victim.kill()
    elif sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
    if sys.platform != "win32":
        import signal

        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGKILL)            # the group of the child, which is its own (start_new_session)


class TreeRun:
    """What `run_tree` saw: the code (None when the time ran out), the text of stdout and stderr, how long it ran and the launch line the oracle wrote last."""

    def __init__(self, returncode, stdout: str, stderr: str, seconds: float, timed_out: bool) -> None:
        self.returncode, self.stdout, self.stderr, self.seconds, self.timed_out = returncode, stdout, stderr, seconds, timed_out
        self.launch = last_launch(stdout)

    def timeout_message(self, who: str, limit_s: float) -> str:
        """The red of a run that did not end: the launch that was alive and for how long."""
        if self.launch is None:
            alive = "no launch had begun"
        else:
            alive = "the launch %s of seed %s (%s) was alive for %s s" % (self.launch["launch"], self.launch["seed"], self.launch["label"],
                                                                         "%d" % self.launch["alive_s"] if self.launch["alive_s"] is not None else "?")
        return "%s did not end within %d s: %s; its tree was killed" % (who, limit_s, alive)


def run_tree(command, out_dir, timeout_s: float, env=None, cwd=None, poll: float = 0.2) -> TreeRun:
    """Runs `command` with its output in FILES under `out_dir` (never a pipe), and at `timeout_s` kills the whole tree.

    Why not `subprocess.run(capture_output=True, timeout=...)`: at the timeout it kills the child alone. The firefox that the child launched holds the write end of the pipe,
    `communicate()` does not return until it exits, and the test went on to a pytest timeout 120 s later (run 45121964904, Windows, job.log 546-557). With files nothing waits
    for a grandchild, and `kill_tree` ends them all."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path, err_path = out_dir / "run-tree.stdout", out_dir / "run-tree.stderr"
    start = time.monotonic()
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        extra = {"start_new_session": True} if sys.platform != "win32" else {}
        child = subprocess.Popen(list(command), stdout=out, stderr=err, stdin=subprocess.DEVNULL, env=env, cwd=cwd, **extra)
        timed_out = False
        while child.poll() is None:
            if time.monotonic() - start >= timeout_s:
                timed_out = True
                kill_tree(child.pid)
                break
            time.sleep(poll)
        child.wait()
    seconds = time.monotonic() - start
    read = lambda path: path.read_bytes().decode("utf-8", "replace")  # noqa: E731
    return TreeRun(None if timed_out else child.returncode, read(out_path), read(err_path), seconds, timed_out)


def read_exits(path) -> dict | None:
    """The document a probe wrote, or None when there is none."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if document.get("format") == FORMAT else None


def exits_of_run(crash_dir, run: str, windows: bool | None = None, needed: bool = True) -> list:
    """The red lines of the launches a run process recorded in `crash_dir/exits.json`. No file, or no launch in it, is red when the run was to make some: an oracle
    that did not see how its browsers ended did not see them."""
    if not crash_dir:
        return []
    document = read_exits(Path(crash_dir) / "exits.json")
    if document is None:
        return ["the run %s left no record of how its browsers ended (%s/exits.json): the probe did not see them" % (run, Path(crash_dir).name)] if needed else []
    if not document["launches"] and needed:
        return ["the run %s recorded no launch in %s/exits.json: the probe did not see a browser" % (run, Path(crash_dir).name)]
    problems = exit_problems(document["launches"], run, windows)
    if document.get("stderr_said"):
        problems.append("the run %s could not save what its browsers wrote: %s" % (run, document["stderr_said"]))
    for launch in document["launches"]:
        if launch.get("stderr_said"):
            problems.append("the run %s did not save what the browser of launch %s wrote: %s" % (run, launch.get("n"), launch["stderr_said"]))
    return problems


def current_label() -> dict:
    """The launch the oracle named last (`mark`), or {} when no probe is installed."""
    return dict(PROBE.label) if PROBE is not None else {}


@contextlib.contextmanager
def environment(**values):
    """`os.environ` with `values` set for the length of the block, put back after it (None removes a variable)."""
    old = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
