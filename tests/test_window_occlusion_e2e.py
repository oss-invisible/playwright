"""A window that another one covers stays visible, unless occlusion tracking is enabled.

What it fixes: with ``widget.windows.window_occlusion_tracking.enabled`` and ``zoom.stealth.widget.occlusion_tracking.enabled`` false, as the core declares
them from one value, a page whose window is fully covered by a second window keeps ``document.visibilityState`` ``visible`` and gets no ``visibilitychange``.
Retail Windows with the tracker on says ``hidden`` there (and then throttles ``requestAnimationFrame`` to 1 Hz and the timers to 1000 ms); a session nobody
looks at must not read as backgrounded.

The hook: ``nsWindow::OnVisibilityNotifyEvent`` in ``widget/gtk/nsWindow.cpp`` of the series, folder ``fingerprint/screen-window/occlusion``: the condition of the
ternary that decides ``OCCLUDED`` or ``UNKNOWN`` reads the pref (``StealthOcclusionTracked``). On Windows the same declaration is Mozilla's own pref, so the same
page reading is the proof that the two builds do not disagree.

The control, in the same test: the two prefs true. The same two sessions, the same covering, and the covered page turns ``hidden``. Without it the first half
proves nothing (a window that was never covered is visible too); it runs first, so a runner on which two windows do not cover each other is red here and not a
green that means nothing.

Two sessions are launched on one display and both are headed. Linux: the display is a private Xvfb started for this test and shared only by its two sessions
(``_os_focus.a_display_of_its_own``, ``DISPLAY`` set with ``monkeypatch`` before either session starts), so no other worker's headed window can sit above or
inside the pair. No window manager runs there. The engine turns a window ``OCCLUDED`` only on a ``VisibilityNotify`` with ``FullyObscured`` of its toplevel,
so the test asks X itself, through a connection of its own (``_x_visibility.XVisibility``, libX11 as the focus test): it selects ``VisibilityChangeMask``
on the covered toplevel before the cover is mapped; once the covering toplevel is up it raises it (``XRaiseWindow``) and reads back ONCE, asserted, the
stacking (``XQueryTree``: the cover above the covered window) and the geometries (the covered window inside the cover); then it waits for X's
``VisibilityNotify FullyObscured`` of the covered toplevel, in both halves. A red there says "arrangement" (X never told the window it is covered, so the page
proves nothing). With tracking enabled, X's ``FullyObscured`` arrived and a page that never turns ``hidden`` is red with a message that starts with "ENGINE".
Without tracking, the same ``FullyObscured`` arrived and the page must stay ``visible``. The X record (the two window ids, the stacking and geometries read
back, every visibility event with its time since the watch began, and whether ``FullyObscured`` arrived) is saved on every run, green or red, with the
Windows record below. Windows: opening the second window is not enough. ``WinWindowOcclusionTracker`` marks a root window
``OCCLUDED`` only when the windows ABOVE it in the Z-order already cover its rectangle (``widget/windows/WinWindowOcclusionTracker.cpp``,
``ProcessComputeNativeWindowOcclusionStatusCallback``: ``it->second = (mUnoccludedDesktopRegion == currUnoccludedDestkop) ? OCCLUDED : VISIBLE``); the second
window lands above the first only when the system lets its process take the foreground (``nsWindow::Show``: ``::ShowWindow(mWnd, SW_SHOWNORMAL)``, an
activation the system's foreground rules may refuse to a process the foreground one did not start); and a later move of another process's window is heard only
once that process was seen with a visible window (``RegisterEventHookForProcess``). So on Windows the test puts the covering in place itself, in two
``SetWindowPos`` calls that need no foreground right: first the second window becomes ``HWND_TOPMOST`` (the system keeps a topmost window above every
non-topmost one, whoever has the foreground; a plain "insert after" from the test's process is a Z-order request the system may not keep), then the first window
moves inside the second one's rectangle as the tracker reads it (a maximized window counts as its monitor's work area) with ``SWP_NOZORDER | SWP_NOACTIVATE``.
That move, last, is the tracker's own signal (``EVENT_OBJECT_LOCATIONCHANGE`` of the first window's process). Right after, the test reads back ONCE what the
tracker checks: the second window visible, not minimized, not cloaked, not layered-transparent, not a popup, above the first in the ``GW_HWNDPREV`` chain, and
the first window inside it. Every red carries the two calls' results and ``GetLastError``, the Z-order from the top down to the first window (process, class,
topmost), and the foreground window and its process, read once after the arrangement and again after the page has answered, so a covered window that something
raised back is told from an arrangement the system refused. Before the topmost arrangement each half first does the plain one (the covered window
inserted right after the cover, no topmost), waits for the page's answer it waits for anyway, and records, as evidence and never as a verdict, whether the
system kept that order and whether the foreground window belongs to one of the two engines; ``FailureEvidence`` saves that record under
``$RUNNER_TEMP/invpw-evidence/test_window_occlusion/`` on every run, green or red, and every message carries one line per half
("order without topmost: kept / not kept, foreground: pid X (engine yes/no)").

A page reads it from ``document.visibilityState`` and the ``visibilitychange`` event.
"""
from __future__ import annotations

import contextlib
import sys
import threading

import pytest

from _evidence_on_failure import FailureEvidence
from _os_focus import a_display_of_its_own
from _waits import watch
from _window_sessions import DEADLINE_S, in_a_thread_of_its_own, launch, wait_for_condition
from _x_visibility import XVisibility

pytestmark = pytest.mark.e2e

#: The one decision, under both engine names (core: ``OCCLUSION_TRACKING``).
TRACKING_PREFS = ("widget.windows.window_occlusion_tracking.enabled", "zoom.stealth.widget.occlusion_tracking.enabled")

#: Records every change of visibility of the document, and answers the current state.
WATCH = """() => {
  window.__visibility = [document.visibilityState];
  document.addEventListener('visibilitychange', () => window.__visibility.push(document.visibilityState));
  return document.visibilityState;
}"""
STATE = "() => document.visibilityState"
CHANGES = "() => window.__visibility"
#: Two painted frames: the page's own process has handled what the window system told it before this returns.
TWO_FRAMES = "() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))"
#: Names the page, so that on Windows its top-level window is found by its title.
NAME = "(name) => { document.title = name; return document.title; }"
COVERED_TITLE, COVER_TITLE = "occlusion-covered-window", "occlusion-covering-window"

#: How far inside the covering window's rectangle the covered one is put, in device pixels.
INSET = 32


def opens_the_covering_window(second):
    """Runs in the second session's own thread: opens a window on the display, names it, and waits for two painted frames."""
    cover = second.new_page()
    cover.goto("about:blank")
    cover.evaluate(NAME, COVER_TITLE)
    cover.evaluate(TWO_FRAMES)


class Win32:
    """The user32/dwmapi calls that find the two windows and put one under the other, with the rules ``WinWindowOcclusionTracker`` reads them by."""

    SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0004, 0x0010
    HWND_TOPMOST = -1
    WS_EX_TOPMOST = 0x8
    GW_HWNDFIRST, GW_HWNDNEXT, GW_HWNDPREV = 0, 2, 3
    SW_MAXIMIZE = 3
    MONITOR_DEFAULTTONEAREST = 2
    DWMWA_CLOAKED = 14
    GWL_STYLE, GWL_EXSTYLE = -16, -20
    WS_POPUP = 0x80000000
    WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_LAYERED = 0x20, 0x80, 0x80000
    LWA_COLORKEY, LWA_ALPHA = 0x1, 0x2
    COMPLEXREGION = 3

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes, self.wintypes = ctypes, wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32")
        self.dwmapi = ctypes.WinDLL("dwmapi")
        u = self.user32
        u.GetWindow.restype, u.GetWindow.argtypes = wintypes.HWND, (wintypes.HWND, wintypes.UINT)
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        u.GetWindowLongW.restype, u.GetWindowLongW.argtypes = wintypes.LONG, (wintypes.HWND, ctypes.c_int)
        u.MonitorFromWindow.restype, u.MonitorFromWindow.argtypes = wintypes.HANDLE, (wintypes.HWND, wintypes.DWORD)
        u.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT)
        u.GetWindowRgn.argtypes = (wintypes.HWND, wintypes.HANDLE)
        self.gdi32.CreateRectRgn.restype = wintypes.HANDLE
        self.gdi32.DeleteObject.argtypes = (wintypes.HANDLE,)
        self.dwmapi.DwmGetWindowAttribute.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
        self.ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        class WINDOWPLACEMENT(ctypes.Structure):
            _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT), ("showCmd", wintypes.UINT), ("ptMinPosition", wintypes.POINT),
                        ("ptMaxPosition", wintypes.POINT), ("rcNormalPosition", wintypes.RECT)]

        self.MONITORINFO, self.WINDOWPLACEMENT = MONITORINFO, WINDOWPLACEMENT

    def window_titled(self, title):
        """The one visible top-level Firefox window whose title starts with ``title``, or None (none, or more than one)."""
        ctypes = self.ctypes
        found = []

        def each(hwnd, _):
            name, text = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(512)
            self.user32.GetClassNameW(hwnd, name, 256)
            self.user32.GetWindowTextW(hwnd, text, 512)
            if name.value.startswith("MozillaWindowClass") and text.value.startswith(title) and self.user32.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True

        self.user32.EnumWindows(self.ENUM(each), 0)
        return found[0] if len(found) == 1 else None

    def rect(self, hwnd):
        r = self.wintypes.RECT()
        self.user32.GetWindowRect(hwnd, self.ctypes.byref(r))
        return (r.left, r.top, r.right, r.bottom)

    def work_area(self, hwnd):
        info = self.MONITORINFO()
        info.cbSize = self.ctypes.sizeof(info)
        self.user32.GetMonitorInfoW(self.user32.MonitorFromWindow(hwnd, self.MONITOR_DEFAULTTONEAREST), self.ctypes.byref(info))
        w = info.rcWork
        return (w.left, w.top, w.right, w.bottom)

    def maximized(self, hwnd):
        placement = self.WINDOWPLACEMENT()
        placement.length = self.ctypes.sizeof(placement)
        self.user32.GetWindowPlacement(hwnd, self.ctypes.byref(placement))
        return placement.showCmd == self.SW_MAXIMIZE

    def tracker_rect(self, hwnd):
        """The rectangle ``IsWindowVisibleAndFullyOpaque`` gives the window: ``GetWindowRect``, fitted to the work area when it is maximized."""
        left, top, right, bottom = self.rect(hwnd)
        if not self.maximized(hwnd):
            return (left, top, right, bottom)
        wl, wt, wr, wb = self.work_area(hwnd)
        width, height = min(wr - wl, right - left), min(wb - wt, bottom - top)
        x = wl if left < wl else min(wr, left + width) - width
        y = wt if top < wt else min(wb, top + height) - height
        return (x, y, x + width, y + height)

    def why_it_cannot_cover(self, hwnd):
        """What ``IsWindowVisibleAndFullyOpaque`` refuses the window for, in its order; '' when the window can cover others."""
        user32, ctypes = self.user32, self.ctypes
        if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
            return "not visible"
        if user32.IsIconic(hwnd):
            return "minimized"
        ex = user32.GetWindowLongW(hwnd, self.GWL_EXSTYLE) & 0xFFFFFFFF
        if ex & self.WS_EX_TRANSPARENT:
            return "WS_EX_TRANSPARENT"
        if ex & self.WS_EX_TOOLWINDOW:
            return "WS_EX_TOOLWINDOW"
        if ex & self.WS_EX_LAYERED:
            alpha, flags = ctypes.c_ubyte(), self.wintypes.DWORD()
            if not user32.GetLayeredWindowAttributes(hwnd, None, ctypes.byref(alpha), ctypes.byref(flags)):
                return "layered without attributes"
            if (flags.value & self.LWA_ALPHA and alpha.value < 255) or flags.value & self.LWA_COLORKEY:
                return "layered and not opaque"
        region = self.gdi32.CreateRectRgn(0, 0, 0, 0)
        kind = user32.GetWindowRgn(hwnd, region)
        self.gdi32.DeleteObject(region)
        if kind == self.COMPLEXREGION:
            return "complex window region"
        cloaked = self.wintypes.DWORD()
        self.dwmapi.DwmGetWindowAttribute(hwnd, self.DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        if cloaked.value:
            return "cloaked"
        left, top, right, bottom = self.rect(hwnd)
        if right <= left or bottom <= top:
            return "empty rectangle"
        if user32.GetWindowLongW(hwnd, self.GWL_STYLE) & 0xFFFFFFFF & self.WS_POPUP:
            return "WS_POPUP"
        return ""

    def above(self, upper, lower):
        """Whether ``upper`` comes before ``lower`` in the Z-order, the order ``EnumWindows`` walks (top first)."""
        hwnd = self.user32.GetWindow(lower, self.GW_HWNDPREV)
        while hwnd:
            if hwnd == upper:
                return True
            hwnd = self.user32.GetWindow(hwnd, self.GW_HWNDPREV)
        return False

    def set_window_pos(self, hwnd, after, x, y, cx, cy, flags):
        """``SetWindowPos`` and what it answered: (return value, ``GetLastError``)."""
        self.ctypes.set_last_error(0)
        result = self.user32.SetWindowPos(hwnd, self.wintypes.HWND(after) if after is not None else None, x, y, cx, cy, flags)
        return (result, self.ctypes.get_last_error())

    def inside(self, cover):
        """(x, y, width, height) inside ``cover``'s rectangle as the tracker reads it and inside its work area, ``INSET`` from both."""
        cl, ct, cr, cb = self.tracker_rect(cover)
        wl, wt, wr, wb = self.work_area(cover)
        left, top, right, bottom = max(cl, wl) + INSET, max(ct, wt) + INSET, min(cr, wr) - INSET, min(cb, wb) - INSET
        assert right - left > 2 * INSET and bottom - top > 2 * INSET, "no room inside the covering window %r (work area %r)" % (
            (cl, ct, cr, cb), (wl, wt, wr, wb))
        return (left, top, right - left, bottom - top)

    def insert_under(self, covered, cover):
        """The arrangement without topmost: ``covered`` inserted right after ``cover`` in the Z-order, inside its rectangle; activates nothing."""
        return {"covered inserted after the cover": self.set_window_pos(covered, cover, *self.inside(cover), self.SWP_NOACTIVATE)}

    def arrange(self, covered, cover):
        """Makes ``cover`` topmost, then moves ``covered`` inside ``cover``'s rectangle and its work area; activates nothing. The calls' answers."""
        calls = {"cover HWND_TOPMOST": self.set_window_pos(cover, self.HWND_TOPMOST, 0, 0, 0, 0, self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_NOACTIVATE)}
        calls["covered moved inside"] = self.set_window_pos(covered, None, *self.inside(cover), self.SWP_NOZORDER | self.SWP_NOACTIVATE)
        return calls

    @staticmethod
    def process_name(pid):
        try:
            import psutil

            return psutil.Process(pid).name()
        except Exception as problem:     # a process gone, or no psutil: the pid is still recorded
            return "(%s)" % type(problem).__name__

    def pid(self, hwnd):
        pid = self.wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, self.ctypes.byref(pid))
        return pid.value

    def describe(self, hwnd):
        """(hwnd, process id, class, the start of the title, topmost) of one window."""
        name, text = self.ctypes.create_unicode_buffer(256), self.ctypes.create_unicode_buffer(64)
        self.user32.GetClassNameW(hwnd, name, 256)
        self.user32.GetWindowTextW(hwnd, text, 64)
        topmost = bool(self.user32.GetWindowLongW(hwnd, self.GWL_EXSTYLE) & self.WS_EX_TOPMOST)
        return (hwnd, self.pid(hwnd), name.value, text.value, "topmost" if topmost else "")

    def z_order_down_to(self, lowest):
        """The visible top-level windows from the top of the Z-order (``GW_HWNDFIRST``, then ``GW_HWNDNEXT``) down to ``lowest``."""
        chain = []
        hwnd = self.user32.GetWindow(lowest, self.GW_HWNDFIRST)
        while hwnd:
            if hwnd == lowest or self.user32.IsWindowVisible(hwnd):
                chain.append(self.describe(hwnd))
            if hwnd == lowest:
                return chain
            hwnd = self.user32.GetWindow(hwnd, self.GW_HWNDNEXT)
        chain.append("(%r not reached)" % (lowest,))
        return chain

    def coverage(self, covered, cover):
        """What the tracker's calculation depends on, for the two windows (``ok`` when the second one covers the first), with the Z-order and the
        foreground window it was read in."""
        inner, outer = self.tracker_rect(covered), self.tracker_rect(cover)
        inside = outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]
        refused = self.why_it_cannot_cover(cover)
        above = self.above(cover, covered)
        foreground = self.user32.GetForegroundWindow()
        return {"ok": inside and above and not refused, "covered": inner, "cover": outer, "inside": inside, "cover above": above,
                "cover refused for": refused or None, "covered maximized": self.maximized(covered),
                "covered window": self.describe(covered), "cover window": self.describe(cover),
                "foreground": self.describe(foreground) if foreground else None, "z-order": self.z_order_down_to(covered)}


class CoveringOnWindows:
    """Windows only: finds the two windows, arranges the covering (``arrange``), and reads it back as the tracker reads it (``read``)."""

    def __init__(self, covered) -> None:
        self.win = Win32()
        found = {}
        for title in (COVERED_TITLE, COVER_TITLE):
            seen = wait_for_condition(covered, lambda title=title: self.win.window_titled(title), lambda hwnd: hwnd is not None,
                                      "no single visible top-level window is titled %r" % title)
            found[title] = seen.history[-1][1]
        self.first, self.cover = found[COVERED_TITLE], found[COVER_TITLE]
        wait_for_condition(covered, lambda: self.win.why_it_cannot_cover(self.cover), lambda why: why == "",
                           "the covering window is not one the tracker counts as covering")
        self.without_topmost = None
        self.calls = None
        self.after_arrangement = None

    def record_without_topmost(self, answered):
        """The arrangement without topmost, then ``answered()`` (the page's answer the half waits for), then the Z-order and the foreground read
        back: EVIDENCE only, nothing asserted. Whether the system kept the order, and whether the foreground is one of the two engines."""
        calls = self.win.insert_under(self.first, self.cover)
        page = answered()
        seen = self.read()
        foreground = seen["foreground"]
        pid = foreground[1] if foreground else None
        engine = {self.win.pid(self.first): "the covered session's engine", self.win.pid(self.cover): "the covering session's engine"}.get(pid)
        self.without_topmost = {"calls": calls, "page": page, "read back": seen,
                                "line": "order without topmost: %s, foreground: pid %s %s (engine %s)" % (
                                    "kept" if seen["cover above"] else "not kept", pid, self.win.process_name(pid) if pid else "",
                                    "yes, %s" % engine if engine else "no")}

    def arrange(self):
        """The arrangement the precondition rests on: the cover topmost, then the covered window inside it, read back once right after."""
        self.calls = self.win.arrange(self.first, self.cover)
        self.after_arrangement = self.read()
        assert self.after_arrangement["ok"], "the second window does not cover the first one as the tracker reads them, read once right after %r: %r; %s" % (
            self.calls, self.after_arrangement, self.without_topmost["line"])

    def read(self):
        return self.win.coverage(self.first, self.cover)

    def evidence(self):
        """Without topmost (calls, the page, the read-back, the summary line), the topmost calls, the read-back right after them, and a read-back
        now: a covered window raised back after the arrangement shows here."""
        return {"without topmost": self.without_topmost, "calls": self.calls, "after the arrangement": self.after_arrangement, "now": self.read()}


class CoveringOnX:
    """Linux only: what X says about the two toplevels, through the test's own connection ``x`` to the private display (see the module docstring)."""

    def __init__(self, x) -> None:
        self.x = x
        self.first = self.cover = None
        self.visibility = None
        self.after_raise = None

    def watch_the_covered_window(self, covered):
        """Finds the covered page's toplevel by its title and selects its visibility events, before the cover is mapped."""
        self.first = wait_for_condition(covered, lambda: self.x.window_titled(COVERED_TITLE), lambda window: window is not None,
                                        "arrangement: no single viewable toplevel is titled %r" % COVERED_TITLE).history[-1][1]
        self.visibility = self.x.watch(self.first)

    def raise_the_cover_and_read_back(self, covered):
        """Raises the covering toplevel, reads the stacking and the geometries back once, and asserts them."""
        self.cover = wait_for_condition(covered, lambda: self.x.window_titled(COVER_TITLE), lambda window: window is not None,
                                        "arrangement: no single viewable toplevel is titled %r" % COVER_TITLE).history[-1][1]
        self.after_raise = self.x.raise_and_read(self.first, self.cover)
        assert self.after_raise["ok"], "arrangement: after XRaiseWindow the cover is not above the covered toplevel with the covered one inside it: %r" % (
            self.evidence(),)

    def wait_until_x_says_fully_obscured(self, covered):
        """The ground truth: X sends the covered toplevel ``VisibilityNotify FullyObscured``."""
        seen = watch(lambda: self.x.drain().first(), lambda first: first is not None, DEADLINE_S, lambda: covered.wait_for_timeout(100))
        assert seen.ok, "arrangement: X never sent the covered toplevel VisibilityNotify FullyObscured (%s), so the page proves nothing: %r" % (
            self.line(), self.evidence())

    def engine_message(self, seen):
        return ("ENGINE: X sent the covered toplevel VisibilityNotify FullyObscured (%s) and with occlusion tracking enabled its page never turned hidden "
                "(nsWindow::OnVisibilityNotifyEvent did not make the window OCCLUDED, or the state never reached the page); seen: %s; X: %r" % (
                    self.line(), seen.render(), self.evidence()))

    def line(self):
        """One line: the read-back after the raise and the visibility events X sent the covered toplevel so far."""
        if self.visibility is None:
            return "X: the covered toplevel was never found"
        self.x.drain()
        placed = "not read back" if self.after_raise is None else "cover above: %s, inside: %s" % (
            self.after_raise["cover above"], self.after_raise["inside"])
        return "X: %s; %s" % (placed, self.visibility.line())

    def evidence(self):
        """The window ids, the read-back after the raise, and every visibility event of the covered toplevel with its time."""
        line = self.line()
        return {"line": line, "covered": None if self.first is None else "0x%x" % self.first, "cover": None if self.cover is None else "0x%x" % self.cover,
                "after the raise": self.after_raise, "visibility": self.visibility.as_dict() if self.visibility else None}


class Record:
    """A record the test keeps itself, in the shape ``FailureEvidence.note_records`` saves (``lock``, ``records``)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.records = []

    def add(self, record) -> None:
        with self.lock:
            self.records.append(record)


def second_window_over_the_first(firefox_binary, tracking, record, x):
    """(state, changes, windows) of the first session's page after a second session's window is up over it; ``windows`` is the evidence of the
    covering (Windows: the tracker's view, Linux: X's), also added to ``record`` for the evidence file.

    ``tracking`` is None for the product as it launches, or the value both tracking prefs are given; ``x`` is the test's own connection to the private
    display on Linux, None on Windows."""
    half = "control (tracking enabled)" if tracking else "product"
    extra = None if tracking is None else {name: tracking for name in TRACKING_PREFS}
    with launch(firefox_binary, extra_prefs=extra) as first, in_a_thread_of_its_own(firefox_binary) as second:
        covered = first.new_page()
        covered.goto("about:blank")
        covered.evaluate(NAME, COVERED_TITLE)
        assert covered.evaluate(WATCH) == "visible", "the page was not visible before anything covered it"
        on_x = CoveringOnX(x) if x is not None else None
        covering = None

        def answered():
            """The page's answer this half waits for: ``hidden`` with tracking (the wait, not asserted here), two frames without."""
            if tracking:
                return {"hidden": watch_state(covered).render()}
            return {"two frames": covered.evaluate(TWO_FRAMES), "state": covered.evaluate(STATE)}

        try:
            if on_x:
                on_x.watch_the_covered_window(covered)
            second.run(opens_the_covering_window)
            if sys.platform == "win32":
                covering = CoveringOnWindows(covered)
                covering.record_without_topmost(answered)
                covering.arrange()
            if on_x:
                on_x.raise_the_cover_and_read_back(covered)
                on_x.wait_until_x_says_fully_obscured(covered)
            if tracking:
                seen = watch_state(covered)
                assert seen.ok, on_x.engine_message(seen) if on_x else (
                    "a window that another one covers never turned hidden with occlusion tracking enabled; seen: %s; windows: %r" % (
                        seen.render(), covering.evidence() if covering else None))
            else:
                covered.evaluate(TWO_FRAMES)
            windows = covering.evidence() if covering else on_x.evidence() if on_x else None
            return covered.evaluate(STATE), covered.evaluate(CHANGES), windows
        finally:
            if covering:
                record.add({"half": half, **covering.evidence()})
            if on_x:
                record.add({"half": half, **on_x.evidence()})


def watch_state(covered):
    """Until the covered page reads ``hidden`` (the deadline of ``wait_for_condition``), with what was seen on the way."""
    return watch(lambda: covered.evaluate(STATE), lambda state: state == "hidden", DEADLINE_S, lambda: covered.wait_for_timeout(100))


def lines(record):
    """Per half, the one line of the arrangement: X's on Linux, the one without topmost on Windows."""
    with record.lock:
        return ["%s: %s" % (r["half"], r.get("line") or (r.get("without topmost") or {}).get("line")) for r in record.records]


@contextlib.contextmanager
def the_display(monkeypatch):
    """Linux: a private Xvfb for the two sessions of this test and the test's own X connection to it. Windows: the runner's desktop, None."""
    if sys.platform == "win32":
        yield None
        return
    with a_display_of_its_own(monkeypatch) as display:
        x = XVisibility(display)
        try:
            yield x
        finally:
            x.close()


def test_a_covered_window_stays_visible_unless_occlusion_tracking_is_enabled(firefox_binary, monkeypatch):
    record = Record()
    with FailureEvidence("test_window_occlusion") as evidence, the_display(monkeypatch) as x:
        evidence.note_records("windows", record)
        tracked_state, tracked_changes, tracked_windows = second_window_over_the_first(firefox_binary, True, record, x)
        assert tracked_state == "hidden" and tracked_changes == ["visible", "hidden"], (
            "control: with tracking enabled the covered page reads %r, changes %r; %s; windows: %r" % (
                tracked_state, tracked_changes, lines(record), tracked_windows))

        state, changes, windows = second_window_over_the_first(firefox_binary, None, record, x)
        assert state == "visible" and changes == ["visible"], (
            "the product's covered page reads %r, changes %r: it must stay visible, with no visibilitychange; %s; windows: %r" % (
                state, changes, lines(record), windows))
        assert state != tracked_state, "enabled and disabled read the same: the declaration decides nothing"
        evidence.keep("always kept: %s" % (lines(record),))
        sys.stderr.write("occlusion arrangement: %s\n" % (lines(record),))
