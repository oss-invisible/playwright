"""Where the operating system's own cursor is while a headed browser window is on the desktop the test runs on.

A real window that appears under the OS cursor receives a native ``WM_MOUSEMOVE`` for it, and the first native move of a process always passes Gecko's
``LastMouseMoveData`` filter, so the page sees ``mousemove`` at the cursor's place with ``buttons=0``, at a moment the test does not choose. On a GitHub
Windows runner the cursor sits at (512, 384) and the headed window covers (-8, -8) to (1928, 1040): the move reached the page once in the middle of a drag
(``test_drag_motion.py::test_a_real_page_sees_a_drag_that_travelled`` saw ``mousemove 512,299 buttons=0`` after the press). The engine keeps delivering native
input to content, so the arrangement is the test's: the cursor is parked on a point of the desktop that no window of the browser covers, before the browser
starts, and a test that records pointer events checks it still is when its gesture begins.

* Windows: ``user32`` through ctypes. The windows are the visible top-level ones of the processes below the test process (the browsers it launched), seen from
  the desktop the test runs on: a ``headless=True`` browser runs on a Win32 desktop of its own (``make_virtual_display``), is not enumerated here and cannot
  receive the interactive cursor.
* Linux and macOS: nothing to do. The Linux e2e run under ``xvfb-run``: an X server with virtual pointer and keyboard devices (XTEST) and no physical one,
  so nothing moves a pointer there; every function answers "outside" and ``park`` does nothing.
"""
from __future__ import annotations

import ctypes
import os
import sys
from typing import List, Optional, Sequence, Tuple

#: ``(left, top, right, bottom)`` as ``GetWindowRect`` gives it: ``right`` and ``bottom`` are the first pixel outside.
Rect = Tuple[int, int, int, int]
Point = Tuple[int, int]

#: How far from every window the parked cursor is, in pixels: a window is moved or grown by a border or a shadow, and the cursor must stay outside of it.
MARGIN = 8


class TheCursorIsInside(AssertionError):
    """The OS cursor is over a window of the browser while a gesture is about to start."""


class TheCursorCannotBeParked(RuntimeError):
    """No point of the desktop is free of the browser's windows, or the system did not move the cursor."""


def _inflate(rect: Rect, margin: int) -> Rect:
    return (rect[0] - margin, rect[1] - margin, rect[2] + margin, rect[3] + margin)


def _contains(rect: Rect, point: Point) -> bool:
    return rect[0] <= point[0] < rect[2] and rect[1] <= point[1] < rect[3]


def cursor_is_outside(window_rects: Sequence[Rect], cursor: Optional[Point] = None, margin: int = 0, *,
                      api: Optional["_Win32"] = None) -> bool:
    """True when ``cursor`` (read from the system when None) is outside every rectangle, ``margin`` pixels beyond its edge included.

    Not Windows: True, there is no OS cursor that reaches a window."""
    if cursor is None:
        api = api or _api()
        if api is None:
            return True
        cursor = api.cursor_position()
    return not any(_contains(_inflate(r, margin), cursor) for r in window_rects)


def park_outside(window_rects: Sequence[Rect], screen_rect: Rect, margin: int = MARGIN) -> Point:
    """A pixel of the virtual screen that is at least ``margin`` pixels from every window rectangle: the bottom-right corner when it is free.

    ``screen_rect`` is ``(left, top, right, bottom)`` of the virtual screen, ``right`` and ``bottom`` exclusive (its origin is negative when a monitor lies left of or
    above the primary one). The candidates are the screen's edges and the places just beyond each window's inflated edges; a free pixel exists only if one
    of them is free (the free pixel can be slid left and up until it touches an edge, and it stays free), so a ``TheCursorCannotBeParked`` is exact."""
    left, top, right, bottom = screen_rect
    if right <= left or bottom <= top:
        raise TheCursorCannotBeParked(
            "park_outside: the virtual screen %r is empty. Where: the screen rectangle read from the system. What: no pixel to put the cursor on. "
            "Fix: run the e2e on an interactive desktop session." % (screen_rect,))
    inflated = [_inflate(r, margin) for r in window_rects]
    xs = sorted({right - 1, left, *(min(max(r[2], left), right - 1) for r in inflated), *(min(max(r[0] - 1, left), right - 1) for r in inflated)},
                reverse=True)
    ys = sorted({bottom - 1, top, *(min(max(r[3], top), bottom - 1) for r in inflated), *(min(max(r[1] - 1, top), bottom - 1) for r in inflated)},
                reverse=True)
    for y in ys:
        for x in xs:
            if not any(_contains(r, (x, y)) for r in inflated):
                return (x, y)
    raise TheCursorCannotBeParked(
        "park_outside: the windows %r cover every pixel of the screen %r with a margin of %d. Where: the headed browser window the test launched. What: the "
        "OS cursor would be over the page whatever the point, and a native mouse move would reach it. Fix: shrink the window below the screen (the test's own "
        "geometry; it is not this helper's to change) or run the test on a bigger desktop." % (list(window_rects), screen_rect, margin))


class _Win32:
    """What the desktop answers, by ``user32``. One object so a test can stand a fake in for it."""

    def __init__(self) -> None:
        from ctypes import wintypes

        self.wintypes = wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        u = self.user32
        u.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
        u.GetCursorPos.restype = wintypes.BOOL
        u.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
        u.SetCursorPos.restype = wintypes.BOOL
        u.GetSystemMetrics.argtypes = (ctypes.c_int,)
        u.GetSystemMetrics.restype = ctypes.c_int
        u.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        u.GetWindowRect.restype = wintypes.BOOL
        u.IsWindowVisible.argtypes = (wintypes.HWND,)
        u.IsWindowVisible.restype = wintypes.BOOL
        u.IsIconic.argtypes = (wintypes.HWND,)
        u.IsIconic.restype = wintypes.BOOL
        u.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u.EnumWindows.argtypes = (self.enum_proc, wintypes.LPARAM)
        u.EnumWindows.restype = wintypes.BOOL

    def cursor_position(self) -> Point:
        point = self.wintypes.POINT()
        if not self.user32.GetCursorPos(ctypes.byref(point)):
            raise TheCursorCannotBeParked(
                "GetCursorPos failed (error %d). Where: user32, on the desktop the test runs on. What: the OS cursor cannot be read. Fix: run the e2e on an "
                "interactive desktop session (not a locked screen or a service)." % ctypes.get_last_error())
        return (point.x, point.y)

    def set_cursor_position(self, x: int, y: int) -> None:
        self.user32.SetCursorPos(x, y)

    def screen(self) -> Rect:
        """The virtual screen: ``SM_XVIRTUALSCREEN``, ``SM_YVIRTUALSCREEN``, ``SM_CXVIRTUALSCREEN``, ``SM_CYVIRTUALSCREEN``."""
        x, y, w, h = (self.user32.GetSystemMetrics(n) for n in (76, 77, 78, 79))
        return (x, y, x + w, y + h)

    def windows_below(self, root_pid: int) -> List[Rect]:
        """The rectangles of the visible, not minimised, top-level windows of the processes below ``root_pid``."""
        import psutil

        try:
            pids = {p.pid for p in psutil.Process(root_pid).children(recursive=True)}
        except psutil.Error:
            return []
        found: List[Rect] = []

        def each(hwnd, _):
            owner = self.wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value in pids and self.user32.IsWindowVisible(hwnd) and not self.user32.IsIconic(hwnd):
                rect = self.wintypes.RECT()
                if self.user32.GetWindowRect(hwnd, ctypes.byref(rect)) and rect.right > rect.left and rect.bottom > rect.top:
                    found.append((rect.left, rect.top, rect.right, rect.bottom))
            return True

        self.user32.EnumWindows(self.enum_proc(each), 0)
        return found


def _is_windows() -> bool:
    return sys.platform == "win32"


def _api() -> Optional[_Win32]:
    return _Win32() if _is_windows() else None


def browser_windows(root_pid: Optional[int] = None, *, api: Optional[_Win32] = None) -> List[Rect]:
    """The rectangles of the windows of the browsers below ``root_pid`` (the test process by default) on this desktop; empty when not Windows."""
    api = api or _api()
    if api is None:
        return []
    return api.windows_below(os.getpid() if root_pid is None else root_pid)


def park(root_pid: Optional[int] = None, *, api: Optional[_Win32] = None) -> Optional[Point]:
    """Windows: put the OS cursor on a point no window of the browsers below ``root_pid`` covers and read it back. Returns the point; None when not Windows.

    Called before the browser starts there is no window yet and the point is the bottom-right corner of the virtual screen, where the engine places no window."""
    api = api or _api()
    if api is None:
        return None
    target = park_outside(api.windows_below(os.getpid() if root_pid is None else root_pid), api.screen())
    api.set_cursor_position(*target)
    read_back = api.cursor_position()
    if read_back != target:
        raise TheCursorCannotBeParked(
            "SetCursorPos(%d, %d) left the cursor at %r. Where: user32, on the desktop the test runs on. What: the OS cursor cannot be moved, so a native "
            "mouse move for it can reach a window of the browser mid-gesture. Fix: run the e2e on an interactive desktop session (not a locked screen "
            "or a service), with no other program holding the cursor." % (target[0], target[1], read_back))
    return target


def require_outside(root_pid: Optional[int] = None, *, api: Optional[_Win32] = None, margin: int = 0) -> None:
    """The precondition of a gesture a test judges by the pointer events the page records: the OS cursor is outside every window of the browsers.

    Raises ``TheCursorIsInside`` otherwise, with the cursor and the windows. Not Windows: nothing."""
    api = api or _api()
    if api is None:
        return
    windows = api.windows_below(os.getpid() if root_pid is None else root_pid)
    cursor = api.cursor_position()
    if not cursor_is_outside(windows, cursor, margin):
        raise TheCursorIsInside(
            "the OS cursor is at %r, inside the browser window(s) %r, when a pointer gesture is about to start. Where: the desktop the test runs on. What: "
            "a native WM_MOUSEMOVE for it passes Gecko's first-move filter and reaches the page as a mousemove with buttons=0 at a moment of the "
            "system's choosing, in the middle of the gesture the test records. Fix: park the cursor outside the window before the browser starts "
            "(the autouse fixture `_the_os_cursor_is_outside_the_browser_window` of tests/conftest.py does it for every e2e test on Windows)." % (cursor, windows))
