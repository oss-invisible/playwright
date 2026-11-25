"""Which content processes of the engine a test saw, and which of them ended, when, and with what exit code.

A test that waits for a frame that never comes cannot tell, from the client alone, a content process that exited (a crash) from a Juggler
message that was lost on the way. This watch is the record that tells them apart: it lists the engine processes below the test process
(`_cpu_by_type.engine_processes` knows them by executable and by `-contentproc ... <type>`), at the points the test names (``sample(at)``) and,
between them, every ``period`` seconds from a thread; for each process it keeps when it was first seen, and when it was no longer there.

* The engine's own PID is not needed: the browser is a descendant of the test process, and so is nothing else of the engine.
* On Windows a handle is opened the first time a pid is seen (``OpenProcess`` with ``PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE``), and
  ``GetExitCodeProcess`` is read through it when the process is gone, or already when the handle says it has ended while the process list
  still shows it (a terminated process stays listed while a handle is open). The handles are closed by ``close()``.
* On Linux the exit code of a process that is not our child cannot be read: the disappearance is recorded and says so.
* A pid that ended is not looked for again (a pid reused inside one test is not told apart).

The watch only listens: it changes no wait and no assertion of the test. Its records (``.records``, under ``.lock``) are what
``FailureEvidence.note_records("content-processes", watch)`` writes as ``content-processes.jsonl`` on a failure.
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

#: Windows: ``GetExitCodeProcess`` answers this for a process that has not ended.
STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000

#: pid -> (type of the process, name, the end of its command line).
Snapshot = Dict[int, Tuple[str, str, str]]


def describe_exit(code: Optional[int]) -> str:
    """``0``, ``0xC0000005`` (an access violation), ``unknown`` for no code: the way a code is written in a summary."""
    if code is None:
        return "unknown"
    return str(code) if 0 <= code < 256 else "0x%08X" % (code & 0xFFFFFFFF)


class NoExitCodes:
    """The probe of a system where the exit code of a process that is not our child cannot be read (Linux)."""

    readable = False

    def open(self, pid: int) -> None:
        pass

    def exit_code(self, pid: int) -> Optional[int]:
        return None

    def close(self, pid: int) -> None:
        pass


class WindowsExitCodes:
    """Handles opened when a pid is first seen, so that the exit code can be read after the process is gone."""

    readable = True

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._wintypes = wintypes
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        self._kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._handles: Dict[int, int] = {}

    def open(self, pid: int) -> None:
        if pid in self._handles:
            return
        handle = self._kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)
        if handle:
            self._handles[pid] = handle

    def exit_code(self, pid: int) -> Optional[int]:
        """The exit code of an ended process; None while it runs, or when no handle was opened for it."""
        handle = self._handles.get(pid)
        if not handle:
            return None
        code = self._wintypes.DWORD(0)
        if not self._kernel32.GetExitCodeProcess(handle, self._ctypes.byref(code)):
            return None
        return None if code.value == STILL_ACTIVE else int(code.value)

    def close(self, pid: int) -> None:
        handle = self._handles.pop(pid, None)
        if handle:
            self._kernel32.CloseHandle(handle)


def default_probe():
    return WindowsExitCodes() if sys.platform == "win32" else NoExitCodes()


def engine_snapshot(root_pid: int) -> Snapshot:
    """The engine processes below ``root_pid`` now: pid -> (type, name, the end of the command line)."""
    import psutil

    from _cpu_by_type import classify, is_engine

    out: Snapshot = {}
    try:
        below = psutil.Process(root_pid).children(recursive=True)
    except psutil.Error:
        return out
    for proc in below:
        try:
            if not is_engine(proc):
                continue
            cmdline = proc.cmdline()
            out[proc.pid] = (classify(cmdline), proc.name(), " ".join(str(a) for a in cmdline[-4:])[-160:])
        except (psutil.Error, OSError):
            continue
    return out


class ContentProcessWatch:
    """``state`` for ``FailureEvidence.note_records``: ``.lock`` and ``.records`` (a list of dicts)."""

    def __init__(self, snapshot: Callable[[], Snapshot], probe=None, platform: str = sys.platform) -> None:
        self._snapshot = snapshot
        self._probe = probe if probe is not None else default_probe()
        self._platform = "linux" if platform.startswith("linux") else platform
        self.lock = threading.Lock()
        self.records: List[dict] = []
        self._sample_lock = threading.Lock()
        #: pid -> (type, name, tail) of the processes seen and not yet gone; pid -> the record of the exit, of those that went.
        self._alive: Snapshot = {}
        self._gone: Dict[int, dict] = {}
        self._seen: Dict[int, dict] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._at = "start"

    # ---- the record ----------------------------------------------------------------------------------------------------
    def _note(self, record: dict) -> None:
        record.update(t=round(time.time(), 4), m=round(time.perf_counter(), 6))
        with self.lock:
            self.records.append(record)

    def sample(self, at: str, explicit: bool = True) -> List[dict]:
        """Looks at the processes now; returns the events (seen / gone) it found. An explicit sample also writes the pids alive."""
        with self._sample_lock:
            if explicit:
                self._at = at
            try:
                now = self._snapshot()
            except Exception as problem:          # a failed look is a note, never a failure of the test
                self._note({"event": "sample-failed", "at": at, "error": repr(problem)})
                return []
            events: List[dict] = []
            for pid, (kind, name, tail) in sorted(now.items()):
                if pid in self._alive or pid in self._gone:
                    continue
                self._alive[pid] = (kind, name, tail)
                self._probe.open(pid)
                events.append({"event": "seen", "at": at, "pid": pid, "type": kind, "name": name, "tail": tail})
            for pid in sorted(self._alive):
                code = self._probe.exit_code(pid)
                if pid in now and code is None:
                    continue
                kind, name, tail = self._alive.pop(pid)
                event = {"event": "gone", "at": at, "pid": pid, "type": kind, "name": name, "tail": tail}
                if self._probe.readable:
                    event["exit_code"] = code
                    event["exit_code_text"] = describe_exit(code)
                    if code is None:
                        event["note"] = "the process is gone and no exit code was read (no handle, or the handle could not be queried)"
                else:
                    event["exit_code"] = None
                    event["note"] = "exit code not readable on %s" % self._platform
                self._gone[pid] = event
                events.append(event)
            for event in events:
                if event["event"] == "seen":
                    self._seen[event["pid"]] = event
                self._note(dict(event))
            if explicit:
                self._note({"event": "sample", "at": at, "alive": sorted(self._alive)})
            return events

    # ---- the thread between the samples ----------------------------------------------------------------------------------
    def start(self, period: float = 0.5) -> None:
        """Samples every ``period`` seconds until ``stop``; the point it is at is the one the last explicit sample named."""
        if self._thread is not None:
            return

        def run():
            while not self._stop.wait(period):
                self.sample("between samples, after: %s" % self._at, explicit=False)

        self._thread = threading.Thread(target=run, name="content-process-watch", daemon=True)
        self._thread.start()

    def stop(self, at: str = "end") -> None:
        """Stops the thread, takes a last sample and closes the handles."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None
        self.sample(at)
        self.close()

    def close(self) -> None:
        with self._sample_lock:
            for pid in list(self._seen):
                self._probe.close(pid)

    # ---- what a message says ---------------------------------------------------------------------------------------------
    def summary(self) -> str:
        """``content processes: N seen, exits: [...]``: one line for an assertion message."""
        with self._sample_lock:
            content = [r for r in self._seen.values() if r["type"] != "parent"]
            exits = list(self._gone.values())
        shown = ["%d(%s)@%s=%s" % (e["pid"], e["type"], e["at"], e["exit_code_text"] if "exit_code_text" in e else e["note"]) for e in exits]
        return "content processes: %d seen, exits: [%s]" % (len(content), ", ".join(shown))
