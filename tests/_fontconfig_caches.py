"""Who writes the caches of fontconfig on the runner: the listing of ``~/.fontconfig``, ``~/.cache/fontconfig`` and ``/var/cache/fontconfig`` (evidence only, no product code).

The fontconfig e2e (``tests/test_the_parent_reads_the_bundle_from_fontconfig_s_cache_linux_e2e.py``) was red once because its cold launch found a valid cache of the
bundle in ``~/.fontconfig/`` of the runner's home, written by an earlier launch on that runner (which one is unknown), and wrote none under its own ``XDG_CACHE_HOME``. This module
answers WHO, without changing what any test sees:

* ``around_a_module`` is the body of an autouse, module-scoped fixture (``tests/conftest.py``): for an e2e module on Linux it lists the three folders before the first
  module of the process and after each module, and writes one line per file that is new, changed or gone since the previous listing (the delta, so the log stays small):

      ``[fontconfig-caches] after <module>: new|changed|gone <path> mtime=<ISO UTC, nanoseconds> size=<bytes>``
      ``[fontconfig-caches] before <module>: present <path> mtime=... size=...``      (what was there when the first module started)
      ``[fontconfig-caches] after <module>: no change``
      ``[fontconfig-caches] after <module>: error <what went wrong>``

  The lines go to the terminal of pytest and, when ``RUNNER_TEMP`` is set, to ``$RUNNER_TEMP/invpw-evidence/fontconfig-caches/<worker>.log`` (the artifact of the job).
* ``listing_text`` is the same listing as one sentence, for the failure message of the fontconfig e2e.

THE RULE OF THIS MODULE: it never changes the outcome of a test. Every error is caught and becomes a line (a folder that cannot be read, a file that vanishes between its
listing and its ``stat``, a system that is not Linux, a log that cannot be written); the fixture yields exactly once whatever happens; no folder is created, nothing is
written but the log, fontconfig is not touched and this module does not isolate ``HOME`` (the fontconfig cache test does, for its own launches, and lists the runner's real home).
"""
from __future__ import annotations

import os
import stat as _stat
import sys
from datetime import datetime, timezone

PREFIX = "[fontconfig-caches]"
EVIDENCE_FOLDER = "invpw-evidence"


def folders(home=None):
    """The three folders fontconfig of a runner writes its caches to (the user's two and the system's), as strings with ``/``: they are Linux paths."""
    home = (os.path.expanduser("~") if home is None else home).rstrip("/")
    return [home + "/.fontconfig", home + "/.cache/fontconfig", "/var/cache/fontconfig"]


def _problem(path, err, what=""):
    return "%s: %s%s: %s" % (path, what, type(err).__name__, err)


def _iso(ns):
    try:
        return datetime.fromtimestamp(ns // 10**9, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + ".%09dZ" % (ns % 10**9)
    except Exception:  # a value no calendar holds: the number is the evidence
        return "ns:%s" % ns


def _scan_folder(folder, listdir, stat):
    """``(files, problems, present)`` of one folder: ``files`` = ``{path: (mtime_ns, size)}`` of its regular files. A folder that is not there is ``present`` False and
    no problem (it is not created either); a folder or a file that cannot be read is a problem."""
    try:
        names = listdir(folder)
    except (FileNotFoundError, NotADirectoryError):
        return {}, [], False
    except Exception as err:
        return {}, [_problem(folder, err)], True
    files, problems = {}, []
    for name in sorted(names):
        path = os.path.join(folder, name)
        try:
            info = stat(path)
            if _stat.S_ISREG(info.st_mode):
                files[path] = (info.st_mtime_ns, info.st_size)
        except Exception as err:
            problems.append(_problem(path, err, "vanished or unreadable at its stat: "))
    return files, problems, True


def snapshot(folder_list, platform=None, listdir=None, stat=None):
    """``({path: (mtime_ns, size)}, [problem])``: the regular files of the folders. Never raises."""
    platform = sys.platform if platform is None else platform
    if not str(platform).startswith("linux"):
        return {}, ["not Linux (%s): nothing listed" % platform]
    listdir, stat = listdir or os.listdir, stat or os.stat
    state, problems = {}, []
    for folder in folder_list:
        try:
            files, found, _present = _scan_folder(folder, listdir, stat)
        except Exception as err:  # a bug of this module is a line too
            files, found = {}, [_problem(folder, err, "listing: ")]
        state.update(files)
        problems.extend(found)
    return state, problems


def delta(before, after):
    """``[(kind, path, mtime_ns, size)]`` sorted by path: ``new`` and ``changed`` carry the new mtime and size, ``gone`` the last known ones."""
    found = []
    for path in sorted(set(before) | set(after)):
        if path not in before:
            found.append(("new", path) + after[path])
        elif path not in after:
            found.append(("gone", path) + before[path])
        elif before[path] != after[path]:
            found.append(("changed", path) + after[path])
    return found


def _file_text(path, mtime_ns, size):
    return "%s mtime=%s size=%d" % (path, _iso(mtime_ns), size)


def listing_text(folder_list=None, platform=None, listdir=None, stat=None):
    """The listing of the folders as one sentence with the mtimes, for a failure message. Never raises."""
    try:
        platform = sys.platform if platform is None else platform
        if not str(platform).startswith("linux"):
            return "not Linux (%s): nothing listed" % platform
        folder_list = folders() if folder_list is None else folder_list
        listdir, stat = listdir or os.listdir, stat or os.stat
        parts = []
        for folder in folder_list:
            try:
                files, problems, present = _scan_folder(folder, listdir, stat)
            except Exception as err:
                parts.append("%s: error %s" % (folder, _problem(folder, err, "listing: ")))
                continue
            if not present:
                parts.append("%s: absent" % folder)
                continue
            body = [_file_text(path, *files[path]) for path in sorted(files)] + ["error %s" % p for p in problems]
            parts.append("%s: %s" % (folder, "; ".join(body) if body else "empty"))
        return " | ".join(parts)
    except Exception as err:
        return "the listing failed: %s: %s" % (type(err).__name__, err)


class Watch:
    """What the process listed last: ``previous`` is None until a listing of a whole run of folders has worked."""

    def __init__(self):
        self.previous = None


WATCH = Watch()


def _say(emit, line):
    try:
        emit(line)
    except Exception:  # a log that cannot be written is not a reason to change an outcome
        pass


def around_a_module(label, active, folder_list=None, watch=None, platform=None, listdir=None, stat=None, emit=None):
    """The body of the fixture: lists before the module, yields ONCE, lists after it. Nothing it does reaches the outcome of a test."""
    watch = WATCH if watch is None else watch
    emit = print if emit is None else emit
    ready = False
    try:
        if active:
            folder_list = folders() if folder_list is None else folder_list
            ready = True
            if watch.previous is None:
                state, problems = snapshot(folder_list, platform, listdir, stat)
                for problem in problems:
                    _say(emit, "%s before %s: error %s" % (PREFIX, label, problem))
                if not problems:
                    watch.previous = state
                    for path in sorted(state):
                        _say(emit, "%s before %s: present %s" % (PREFIX, label, _file_text(path, *state[path])))
                    if not state:
                        _say(emit, "%s before %s: empty" % (PREFIX, label))
    except Exception as err:
        _say(emit, "%s before %s: error %s" % (PREFIX, label, _problem("the listing", err)))
    yield
    try:
        if ready:
            state, problems = snapshot(folder_list, platform, listdir, stat)
            for problem in problems:
                _say(emit, "%s after %s: error %s" % (PREFIX, label, problem))
            if not problems:
                changes = delta(watch.previous or {}, state)
                watch.previous = state
                for kind, path, mtime_ns, size in changes:
                    _say(emit, "%s after %s: %s %s" % (PREFIX, label, kind, _file_text(path, mtime_ns, size)))
                if not changes:
                    _say(emit, "%s after %s: no change" % (PREFIX, label))
    except Exception as err:
        _say(emit, "%s after %s: error %s" % (PREFIX, label, _problem("the listing", err)))


def emitter(config):
    """Where the lines go: the terminal of pytest when there is one (not in an xdist worker), and the evidence of the job when ``RUNNER_TEMP`` is set."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    base = os.environ.get("RUNNER_TEMP")
    where = os.path.join(base, EVIDENCE_FOLDER, "fontconfig-caches", worker + ".log") if base else None

    def emit(line):
        reporter = config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(line)
        if where is not None:
            os.makedirs(os.path.dirname(where), exist_ok=True)
            with open(where, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    return emit
