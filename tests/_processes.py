"""Which of the engine's processes host a site, told by the name of the site: what the e2e tests that look at processes read.

Counting processes between two reads does not tell a site's process from the browser's own. The browser starts
content processes for itself: it refills the pool of preallocated ones (`dom.ipc.processPrelaunch.fission.number`)
whenever it gets to it, and the New Tab page starts a process for a Mozilla site of its own (remote type
`webIsolated=https://mozgcp.net`, born about a second after the process of the New Tab page, with any page, even
about:blank). Under load that process arrives after the first read of a test, and "the set of processes changed"
read it as the effect of the frame under test: the same-site control was red in 14 of 30 runs of the whole e2e suite
(`[P] -> [P, P2]`), and "at least one new process" for a cross-site frame was satisfied by that process alone.

So a test asks for the processes of ONE site, by name. With the pref `fission.processSiteNames` on (`NAMED_BY_SITE`,
test-only: the product never sets it) a content process that hosts a site is named by that site
(`ContentChild::SetProcessName`, dom/ipc/ContentChild.cpp: a leading `https://` is dropped), and on Linux
`prctl(PR_SET_NAME)` keeps 15 characters (ipc/glue/ProcessUtils_linux.cpp). Measured on firefox-62 (Linux):

- the process of `http://a.test` in a Playwright context is named `http://a.test^u`: the site, then the start of
  `^userContextId=N`, cut at 15 characters;
- the process the New Tab page starts is named `mozgcp.net`;
- a process with no site keeps its type: `Web Content`, `Privileged Cont`, `WebExtensions`.

The process gets its name before any document loads in it, so when a page of the site has run, its process already
answers to the site. Windows names no process (ProcessUtils_none.cpp): a test that looks at sites skips there and
says so.

Windows names no process, but the engine's own log names the type of every process it creates (`Setting remoteType of
process <pid> to <type>` when a process is born with its type, `Changing remoteType of process <pid> from prealloc to
<type>` when a spare one is given its site: `dom/ipc/ContentChild.cpp`, `RecvSetRemoteType`, log module `Process`). The
type of a process that hosts a site is `webIsolated=<site>^<attributes>` (`dom/ipc/RemoteType.cpp`, `Stringify`), the
same site the Linux name carries, and the process gets it before any document loads in it. So on Windows a test starts the
engine with `LOG_ENV` and a file base (`MOZ_LOG_FILE`), and `remote_types(base)` reads which type each process was given (by pid and time, a pid being reused);
liveness and the engine's own directory are still told by the process tree, so a process that has ended is not counted.
"""
from __future__ import annotations

import calendar
import os
import re
import sys

import pytest

#: The pref overlay that names a content process by the site it hosts. Test-only.
NAMED_BY_SITE = {"fission.processSiteNames": True}

#: What PR_SET_NAME keeps of a process name on Linux.
NAME_LENGTH = 15


def name_of_site(site):
    """What the process name of `site` starts with: the site without a leading `https://`, cut at 15 characters."""
    if site.startswith("https://"):
        site = site[len("https://"):]
    return site[:NAME_LENGTH]


def hosts_site(name, site):
    """True when a process called `name` is a process of `site`.

    The name is the prefix itself, or the prefix followed by `^` (the start of `^userContextId=N`). When the prefix is
    shorter than 15 characters the next character has to be `^`, so `http://a.test` does not match a process of
    `http://a.testing.example`."""
    prefix = name_of_site(site)
    if name == prefix:
        return True
    if not name.startswith(prefix):
        return False
    return len(prefix) >= NAME_LENGTH or name[len(prefix)] == "^"


#: The environment that makes the engine write the type of each process it creates (`MOZ_LOG_FILE` is added by the
#: test: a file base in its own temporary folder). Used where the system names no process (Windows).
LOG_ENV = {"MOZ_LOG": "Process:5,timestamp,sync"}

_TYPE_GIVEN = re.compile(r"(?:Setting remoteType of process (\d+) to|Changing remoteType of process (\d+) from \S+ to) (\S+)")

#: The start of a line of `MOZ_LOG` with the `timestamp` option: `2026-11-22 10:11:12.123456 UTC - [Process 12: Thread]: ...`.
_LOG_TIME = re.compile(r"^(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)\.(\d{6}) UTC - ")

#: How much earlier than the creation time of a process (psutil) its first log line may be stamped: the two clocks are
#: read at different moments of its birth.
BIRTH_SLACK = 0.25


def _natural(path):
    """The sort key that puts `base.child-2` before `base.child-10` (the plain order of names puts it after)."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", path.name)]


def _stamp(line):
    """The time of a log line in seconds since the epoch (UTC), or None when the line carries no timestamp."""
    match = _LOG_TIME.match(line)
    if match is None:
        return None
    year, month, day, hour, minute, second, micro = (int(g) for g in match.groups())
    return calendar.timegm((year, month, day, hour, minute, second)) + micro / 1e6


def remote_types(base):
    """`{pid: [(time, remote type), ...]}` of the processes the engine has logged to the files that start with the path
    `base` (`MOZ_LOG_FILE`: the parent writes `base`, each child its own `base.child-N`), each list in the order of the
    time the lines were logged (then the child number as a number, then the line). A pid alone does not name a process:
    the system gives the number of an ended process to a later one, and a later process of another site must not
    inherit the type of the old one. So the types stay stamped, and `type_of_process` chooses the ones of the process
    instance in hand. A process that logs twice (a spare one, then the site it is given) has two entries, the last wins."""
    entries = {}
    for order, path in enumerate(sorted(base.parent.glob(base.name + "*"), key=_natural)):
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines()):
            match = _TYPE_GIVEN.search(line)
            if match is None:
                continue
            stamp = _stamp(line)
            key = (stamp if stamp is not None else float("-inf"), order, number)
            entries.setdefault(int(match.group(1) or match.group(2)), []).append((key, stamp, match.group(3)))
    return {pid: [(stamp, remote_type) for _key, stamp, remote_type in sorted(found, key=lambda entry: entry[0])]
            for pid, found in entries.items()}


def type_of_process(types, pid, created):
    """The remote type the process `pid` born at `created` (epoch seconds, `psutil.Process.create_time()`) was given: the
    last one logged for the pid at or after its birth (less `BIRTH_SLACK`). Entries logged before the process existed
    are the ones of an earlier process that had the same pid, and are ignored. A line with no timestamp counts for any
    birth. `""` when the process has not logged its type yet."""
    chosen = ""
    for stamp, remote_type in types.get(pid, ()):
        if stamp is None or stamp >= created - BIRTH_SLACK:
            chosen = remote_type
    return chosen


def hosts_site_by_type(remote_type, site):
    """True when a process of the remote type `remote_type` hosts `site` (`http://a.test`): `<kind>=<site>` or
    `<kind>=<site>^<attributes>`. A process with no site (`web`, `prealloc`, `privilegedabout`, `extension`) has no `=`."""
    meta = remote_type.partition("=")[2]
    return meta == site or meta.startswith(site + "^")


def site_pids_by_type(processes, site, types, engine_dir=None):
    """Like `site_pids`, told by the remote types the engine logged (`remote_types`) instead of by the process name."""
    return _content_pids(processes, engine_dir, lambda proc, _name: hosts_site_by_type(_type_of(types, proc), site))


def _type_of(types, proc):
    import psutil

    try:
        return type_of_process(types, proc.pid, proc.create_time())
    except psutil.Error:
        return ""


def site_pids(processes, site, engine_dir=None):
    """The pids, among `processes` (psutil-like), of the content processes (`-contentproc ... tab`) that host `site`;
    with `engine_dir`, only those of the engine in that directory."""
    return _content_pids(processes, engine_dir, lambda _proc, name: hosts_site(name, site))


def _content_pids(processes, engine_dir, hosts):
    import psutil

    pids = set()
    for proc in processes:
        try:
            if engine_dir is not None and not os.path.normcase(proc.exe()).startswith(os.path.normcase(engine_dir)):
                continue
            name, cmd = proc.name(), proc.cmdline()
        except psutil.Error:
            continue
        if "-contentproc" in cmd and cmd and cmd[-1] == "tab" and hosts(proc, name):
            pids.add(proc.pid)
    return pids


def site_processes_below(root_pid, site, engine_dir=None, types=None):
    """The pids of the processes below `root_pid` that host `site`. On Linux the name of the process says it; on Windows
    `types` (a function that returns `remote_types(base)` of the engine's log, see `LOG_ENV`) does. With neither, the test
    skips: no name says it."""
    import psutil

    if not sys.platform.startswith("linux") and types is None:
        pytest.skip("only Linux names a content process by the site it hosts; %s names none (pass `types`)" % sys.platform)
    try:
        children = psutil.Process(root_pid).children(recursive=True)
    except psutil.Error:
        return set()
    if sys.platform.startswith("linux"):
        return site_pids(children, site, engine_dir)
    return site_pids_by_type(children, site, types(), engine_dir)
