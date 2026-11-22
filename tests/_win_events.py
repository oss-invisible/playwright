"""Who has the foreground of the Windows desktop, and when: a recorder of the system's window events for the occlusion e2e test.

In the product half of ``test_window_occlusion_e2e.py`` on Windows the covered window can come back above the one that covers it, with the foreground
in ITS engine. Two explanations are open: a focus move in the covered engine that ends in ``::SetFocus`` on its native window (the engine keeps
believing its window is active after the system deactivated it), or a covering engine that never had the foreground at all. The verdict of the test
does not rest on this record; it only says which one happened:

* ``ForegroundRecorder``: ``SetWinEventHook(EVENT_SYSTEM_FOREGROUND .. EVENT_OBJECT_FOCUS, WINEVENT_OUTOFCONTEXT)`` for every process, set from a thread
  of its own that runs its own ``GetMessage`` loop (an out-of-context hook is called from the message loop of the thread that set it), from before the
  first engine starts until the read-back. Each event is kept with ``time.perf_counter()``, its id, its window, and the process and thread that own the
  window (``GetWindowThreadProcessId``). ``snapshot`` reads ``GetForegroundWindow`` once, right before and right after the insert. ``stop`` posts
  ``WM_QUIT`` to the loop, joins the thread, and the thread unhooks.
  An out-of-context hook asks the system for a copy of each event; it activates nothing and changes no rule of the foreground, so the arrangement and
  the page read the same with it.
* ``foreground_line``: one line from that record, relative to the insert: who had the foreground at the insert, which of the two engines ever had it
  before, who took it after and when, and the focus events after it. A record that cannot be trusted (the hook never set, the loop dead or ended
  before it was stopped, no event heard, events lost) gives "NOT RECORDED" with the reasons, never "nobody took it".

The Windows calls live in ``Win32Events``, built only on Windows; this module imports on every system, and the recorder takes any object with the same
methods, so ``test_the_foreground_record_of_the_occlusion_test.py`` drives it with fakes (a loop that dies, a hook that hears nothing) on both systems.
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional

#: The range of the hook: ``EVENT_SYSTEM_FOREGROUND`` .. ``EVENT_OBJECT_FOCUS`` (``winuser.h``).
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_FOCUS = 0x8005
WINEVENT_OUTOFCONTEXT = 0x0000
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000
#: The names of the events of the range that a window of the test makes (``winuser.h``); any other is written by its number.
NAMES = {
    0x0003: "EVENT_SYSTEM_FOREGROUND", 0x0004: "EVENT_SYSTEM_MENUSTART", 0x0005: "EVENT_SYSTEM_MENUEND", 0x0006: "EVENT_SYSTEM_MENUPOPUPSTART",
    0x0007: "EVENT_SYSTEM_MENUPOPUPEND", 0x0008: "EVENT_SYSTEM_CAPTURESTART", 0x0009: "EVENT_SYSTEM_CAPTUREEND", 0x000A: "EVENT_SYSTEM_MOVESIZESTART",
    0x000B: "EVENT_SYSTEM_MOVESIZEEND", 0x0014: "EVENT_SYSTEM_SWITCHSTART", 0x0015: "EVENT_SYSTEM_SWITCHEND", 0x0016: "EVENT_SYSTEM_MINIMIZESTART",
    0x0017: "EVENT_SYSTEM_MINIMIZEEND", 0x0020: "EVENT_SYSTEM_DESKTOPSWITCH", 0x8000: "EVENT_OBJECT_CREATE", 0x8001: "EVENT_OBJECT_DESTROY",
    0x8002: "EVENT_OBJECT_SHOW", 0x8003: "EVENT_OBJECT_HIDE", 0x8004: "EVENT_OBJECT_REORDER", 0x8005: "EVENT_OBJECT_FOCUS",
}
#: The most events one record keeps; one more makes the record untrustworthy (a lost event could be the one that took the foreground).
MOST_EVENTS = 200000
#: The deadline of the loop thread's start and stop, in seconds: they must happen.
DEADLINE_S = 30


def name_of_event(event: int) -> str:
    return NAMES.get(event, "0x%04X" % event)


class Win32Events:
    """The user32 calls of the recorder. Built only on Windows."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes, self.wintypes = ctypes, wintypes
        u = self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.PROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG, wintypes.LONG, wintypes.DWORD,
                                       wintypes.DWORD)
        u.SetWinEventHook.restype = wintypes.HANDLE
        u.SetWinEventHook.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, self.PROC, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD)
        u.UnhookWinEvent.restype, u.UnhookWinEvent.argtypes = wintypes.BOOL, (wintypes.HANDLE,)
        u.GetMessageW.restype = wintypes.BOOL
        u.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
        u.PeekMessageW.restype = wintypes.BOOL
        u.PeekMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT)
        u.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
        u.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
        u.PostThreadMessageW.restype = wintypes.BOOL
        u.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        u.GetForegroundWindow.restype = wintypes.HWND
        self._proc = None

    def make_queue(self) -> None:
        """Creates the calling thread's message queue, so that ``WM_QUIT`` can be posted to it before its first ``GetMessage``."""
        msg = self.wintypes.MSG()
        self.user32.PeekMessageW(self.ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)

    def hook(self, on_event: Callable[..., None]):
        """Sets the hook for every process and thread; raises with ``GetLastError`` when the system refuses it."""

        def called(_hook, event, hwnd, obj, child, thread, ms):
            on_event(event, hwnd or 0, obj, child, thread, ms)

        self._proc = self.PROC(called)            # kept alive as long as the hook
        self.ctypes.set_last_error(0)
        handle = self.user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_FOCUS, None, self._proc, 0, 0, WINEVENT_OUTOFCONTEXT)
        if not handle:
            raise OSError("SetWinEventHook answered NULL, GetLastError %d" % self.ctypes.get_last_error())
        return handle

    def pump(self) -> None:
        """The loop the hook is called from; returns on ``WM_QUIT``."""
        msg = self.wintypes.MSG()
        while True:
            got = self.user32.GetMessageW(self.ctypes.byref(msg), None, 0, 0)
            if got == 0:
                return
            if got == -1:
                raise OSError("GetMessageW answered -1, GetLastError %d" % self.ctypes.get_last_error())
            self.user32.TranslateMessage(self.ctypes.byref(msg))
            self.user32.DispatchMessageW(self.ctypes.byref(msg))

    def quit(self, thread_id: int) -> bool:
        return bool(self.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0))

    def unhook(self, handle) -> bool:
        return bool(self.user32.UnhookWinEvent(handle))

    def owner(self, hwnd: int):
        """(process id, thread id) of the window; (0, 0) for a window that is gone."""
        pid = self.wintypes.DWORD()
        tid = self.user32.GetWindowThreadProcessId(hwnd, self.ctypes.byref(pid))
        return pid.value, tid

    def foreground(self) -> int:
        return self.user32.GetForegroundWindow() or 0


class ForegroundRecorder:
    """The system's window events from ``start`` to ``stop`` (see the module docstring). Never raises: what went wrong is in ``problems()``."""

    def __init__(self, api: Any = None, clock: Callable[[], float] = time.perf_counter) -> None:
        self.api = api
        self.clock = clock
        self.lock = threading.Lock()
        self.events: List[Dict[str, Any]] = []
        self.heard = 0
        self.deadline_s = DEADLINE_S
        self.anchor = {"t": round(time.time(), 6), "m": round(clock(), 6)}
        self.thread: Optional[threading.Thread] = None
        self.thread_id: Optional[int] = None
        self.hooked = False
        self.loop_ended: Optional[str] = None
        self.loop_ended_at: Optional[float] = None
        self.unhooked: Optional[bool] = None
        self.stopped_at: Optional[float] = None
        self.callback_errors: List[str] = []
        self._problems: List[str] = []
        self._ready = threading.Event()

    # ---- the loop thread ---------------------------------------------------------------------------------------------------------------------------
    def start(self) -> "ForegroundRecorder":
        if self.api is None:
            if sys.platform != "win32":
                self._problems.append("not Windows: no window event is recorded")
                return self
            try:
                self.api = Win32Events()
            except Exception as problem:
                self._problems.append("the user32 calls could not be prepared: %r" % (problem,))
                return self
        self.thread = threading.Thread(target=self._run, name="win-events", daemon=True)
        self.thread.start()
        if not self._ready.wait(self.deadline_s):
            self._problems.append("the loop thread did not report within %s s" % self.deadline_s)
        return self

    def _run(self) -> None:
        try:
            self.thread_id = threading.get_native_id()
            self.api.make_queue()
            handle = self.api.hook(self._on_event)
        except BaseException as problem:
            self._problems.append("the hook was never set: %r" % (problem,))
            self._ready.set()
            return
        self.hooked = True
        self._ready.set()
        ended = "quit"
        try:
            self.api.pump()
        except BaseException as problem:
            ended = "died: %r" % (problem,)
        finally:
            try:
                self.unhooked = bool(self.api.unhook(handle))
            except BaseException as problem:
                self.unhooked = False
                self._problems.append("UnhookWinEvent raised: %r" % (problem,))
            self.loop_ended_at = self.clock()
            self.loop_ended = ended

    def _on_event(self, event, hwnd, obj, child, thread, ms) -> None:
        m = self.clock()
        try:
            pid, tid = self.api.owner(hwnd) if hwnd else (0, 0)
        except Exception as problem:
            pid = tid = None
            if len(self.callback_errors) < 5:
                self.callback_errors.append(repr(problem))
        with self.lock:
            self.heard += 1
            if len(self.events) < MOST_EVENTS:
                self.events.append({"m": round(m, 6), "event": event, "name": name_of_event(event), "hwnd": hwnd, "pid": pid, "tid": tid,
                                    "object": obj, "child": child, "event thread": thread, "ms": ms})

    def stop(self) -> None:
        """Ends the record (its last instant is ``stopped_at``): ``WM_QUIT`` to the loop, then the thread joined; the thread unhooks. Idempotent."""
        if self.stopped_at is not None:
            return
        self.stopped_at = self.clock()
        if self.thread is None:
            return
        if self.thread.is_alive() and self.thread_id is not None:
            try:
                if not self.api.quit(self.thread_id):
                    self._problems.append("PostThreadMessage(WM_QUIT) was refused")
            except Exception as problem:
                self._problems.append("PostThreadMessage(WM_QUIT) raised: %r" % (problem,))
        self.thread.join(self.deadline_s)
        if self.thread.is_alive():
            self._problems.append("the loop thread did not stop within %s s: the hook is still set" % self.deadline_s)

    def __enter__(self) -> "ForegroundRecorder":
        return self.start()

    def __exit__(self, *_exc) -> bool:
        self.stop()
        return False

    # ---- what the test reads ---------------------------------------------------------------------------------------------------------------------
    def snapshot(self, what: str) -> Dict[str, Any]:
        """``GetForegroundWindow`` now: (time, window, its process and thread), or the error."""
        m = self.clock()
        if self.api is None:
            return {"what": what, "m": round(m, 6), "error": "; ".join(self._problems) or "no user32 calls"}
        try:
            hwnd = self.api.foreground()
            pid, tid = self.api.owner(hwnd) if hwnd else (0, 0)
        except Exception as problem:
            return {"what": what, "m": round(m, 6), "error": repr(problem)}
        return {"what": what, "m": round(m, 6), "hwnd": hwnd, "pid": pid, "tid": tid}

    def problems(self) -> List[str]:
        """Why the record cannot say who took the foreground; [] when it can."""
        out = list(self._problems)
        if self.thread is not None and not self.hooked and not any("never set" in p for p in out):
            out.append("the hook was never set")
        if self.loop_ended and self.loop_ended.startswith("died"):
            out.append("the message loop %s" % self.loop_ended)
        elif self.loop_ended and (self.stopped_at is None or self.loop_ended_at < self.stopped_at):
            out.append("the message loop ended before it was stopped")
        if self.unhooked is False and not any("Unhook" in p for p in out):
            out.append("UnhookWinEvent answered FALSE")
        if self.stopped_at is None:
            out.append("the record was never stopped")
        with self.lock:
            heard, kept = self.heard, len(self.events)
        if self.hooked and heard == 0:
            out.append("the hook heard no event: it is blind")
        if heard > kept:
            out.append("%d events were heard and not kept" % (heard - kept))
        if self.callback_errors:
            out.append("the owner of a window could not be read: %s" % self.callback_errors[0])
        return out

    def recorded(self) -> List[Dict[str, Any]]:
        with self.lock:
            return list(self.events)

    def as_dict(self) -> Dict[str, Any]:
        """The state of the record and its foreground events (every event is in ``recorded()``)."""
        events = self.recorded()
        return {"anchor": self.anchor, "hooked": self.hooked, "loop ended": self.loop_ended, "unhooked": self.unhooked,
                "stopped at": None if self.stopped_at is None else round(self.stopped_at, 6), "events heard": self.heard,
                "events kept": len(events), "problems": self.problems(),
                "foreground events": [e for e in events if e["event"] == EVENT_SYSTEM_FOREGROUND]}

    def line(self, before: Dict[str, Any], after: Dict[str, Any], roles: Mapping[int, str], name_of: Optional[Callable[[int], str]] = None) -> str:
        return foreground_line(self.recorded(), before, after, self.stopped_at, roles, self.problems(), name_of)


def foreground_line(events, before, after, until, roles: Mapping[int, str], problems, name_of: Optional[Callable[[int], str]] = None) -> str:
    """One line, times relative to the insert (``before["m"]``, the ``GetForegroundWindow`` read right before it): who had the foreground at the
    insert and right after it, which engine ever had it before, who took it after (``EVENT_SYSTEM_FOREGROUND`` up to ``until``, the read-back) and
    the focus events after it. With ``problems`` the part that rests on the hook says "NOT RECORDED" and why."""

    def who(pid) -> str:
        if pid == 0:
            return "no window"
        if pid in roles:
            return "pid %s (%s)" % (pid, roles[pid])
        return "pid %s%s (not an engine of the test)" % (pid, " %s" % name_of(pid) if name_of and pid else "")

    def read(snap) -> str:
        if not snap or snap.get("error"):
            return "not read (%s)" % ((snap or {}).get("error") or "no snapshot")
        return who(snap["pid"])

    parts = ["foreground at the insert: %s, right after it: %s" % (read(before), read(after))]
    if problems:
        parts.append("who took it after: NOT RECORDED (%s)" % "; ".join(problems))
        return "; ".join(parts)
    if not before or before.get("error") or until is None:
        parts.append("who took it after: NOT RECORDED (no time of the insert)")
        return "; ".join(parts)
    insert = before["m"]
    foreground = [e for e in events if e["event"] == EVENT_SYSTEM_FOREGROUND]
    ever = []
    for pid, role in sorted(roles.items(), key=lambda item: item[1]):
        first = next((e for e in foreground if e["pid"] == pid and e["m"] <= insert), None)
        ever.append("%s %s" % (role, "first at %+.3f s" % (first["m"] - insert) if first else "never"))
    parts.append("had it before the insert: %s" % ", ".join(ever))
    later = [e for e in foreground if insert < e["m"] <= until]
    if later:
        parts.append("who took it after: %s" % ", ".join("%s at %+.3f s" % (who(e["pid"]), e["m"] - insert) for e in later))
    else:
        parts.append("who took it after: nobody until the read-back at %+.3f s" % (until - insert))
    focus: Dict[Any, List[float]] = {}
    for e in events:
        if e["event"] == EVENT_OBJECT_FOCUS and insert < e["m"] <= until:
            focus.setdefault(e["pid"], []).append(e["m"] - insert)
    parts.append("focus events after it: %s" % (", ".join("%s x%d, first at %+.3f s" % (who(pid), len(times), times[0]) for pid, times in focus.items())
                                                  or "none"))
    return "; ".join(parts)
