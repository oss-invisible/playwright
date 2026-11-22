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

Two sessions are launched on one display and both are headed. Linux: the e2e runs under ``xvfb-run`` (no window manager, so the second window is mapped over
the first and X tells the first one it is fully obscured). Windows: opening the second window is not enough. ``WinWindowOcclusionTracker`` marks a root window
``OCCLUDED`` only when the windows ABOVE it in the Z-order already cover its rectangle (``widget/windows/WinWindowOcclusionTracker.cpp``,
``ProcessComputeNativeWindowOcclusionStatusCallback``: ``it->second = (mUnoccludedDesktopRegion == currUnoccludedDestkop) ? OCCLUDED : VISIBLE``); the second
window lands above the first only when the system lets its process take the foreground (``nsWindow::Show``: ``::ShowWindow(mWnd, SW_SHOWNORMAL)``, an
activation the system's foreground rules may refuse to a process the foreground one did not start); and a later move of another process's window is heard only
once that process was seen with a visible window (``RegisterEventHookForProcess``). So on Windows the test puts the covering in place itself, as a user who drags
one window over the other: the first window goes right under the second one, inside its rectangle as the tracker reads it (a maximized window counts as its
monitor's work area), in one ``SetWindowPos`` with ``SWP_NOACTIVATE``. That move of the first window is the tracker's own signal (``EVENT_OBJECT_LOCATIONCHANGE``
of its process), and before waiting the test checks what the tracker checks: the second window visible, not minimized, not cloaked, not layered-transparent,
not a popup, above the first, and the first window inside it. A red says which of these did not hold.

A page reads it from ``document.visibilityState`` and the ``visibilitychange`` event.
"""
from __future__ import annotations

import sys

import pytest

from _window_sessions import in_a_thread_of_its_own, launch, wait_for_condition

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

    SWP_NOACTIVATE = 0x0010
    GW_HWNDPREV = 3
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

    def put_under(self, covered, cover):
        """Moves ``covered`` right under ``cover`` in the Z-order, inside ``cover``'s rectangle and its work area, activating nothing."""
        cl, ct, cr, cb = self.tracker_rect(cover)
        wl, wt, wr, wb = self.work_area(cover)
        left, top, right, bottom = max(cl, wl) + INSET, max(ct, wt) + INSET, min(cr, wr) - INSET, min(cb, wb) - INSET
        assert right - left > 2 * INSET and bottom - top > 2 * INSET, "no room inside the covering window %r (work area %r)" % (
            (cl, ct, cr, cb), (wl, wt, wr, wb))
        moved = self.user32.SetWindowPos(covered, cover, left, top, right - left, bottom - top, self.SWP_NOACTIVATE)
        assert moved, "SetWindowPos of the covered window failed (WinError %d)" % self.ctypes.get_last_error()

    def coverage(self, covered, cover):
        """What the tracker's calculation depends on, for the two windows; ``ok`` when the second one covers the first."""
        inner, outer = self.tracker_rect(covered), self.tracker_rect(cover)
        inside = outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]
        refused = self.why_it_cannot_cover(cover)
        above = self.above(cover, covered)
        return {"ok": inside and above and not refused, "covered": inner, "cover": outer, "inside": inside, "cover above": above,
                "cover refused for": refused or None, "covered maximized": self.maximized(covered),
                "foreground is the cover": self.user32.GetForegroundWindow() == cover}


def covers_on_windows(covered):
    """Windows only: finds the two windows, puts the covered one under the covering one, and checks the covering as the tracker reads it."""
    win = Win32()
    found = {}
    for title in (COVERED_TITLE, COVER_TITLE):
        seen = wait_for_condition(covered, lambda title=title: win.window_titled(title), lambda hwnd: hwnd is not None,
                                  "no single visible top-level window is titled %r" % title)
        found[title] = seen.history[-1][1]
    first, cover = found[COVERED_TITLE], found[COVER_TITLE]
    wait_for_condition(covered, lambda: win.why_it_cannot_cover(cover), lambda why: why == "",
                       "the covering window is not one the tracker counts as covering")
    win.put_under(first, cover)
    seen = wait_for_condition(covered, lambda: win.coverage(first, cover), lambda state: state["ok"],
                              "the second window does not cover the first one as the tracker reads them")
    return seen.history[-1][1]


def second_window_over_the_first(firefox_binary, tracking):
    """(state, changes) of the first session's page after a second session's window is up over it.

    ``tracking`` is None for the product as it launches, or the value both tracking prefs are given."""
    extra = None if tracking is None else {name: tracking for name in TRACKING_PREFS}
    with launch(firefox_binary, extra_prefs=extra) as first, in_a_thread_of_its_own(firefox_binary) as second:
        covered = first.new_page()
        covered.goto("about:blank")
        covered.evaluate(NAME, COVERED_TITLE)
        assert covered.evaluate(WATCH) == "visible", "the page was not visible before anything covered it"
        second.run(opens_the_covering_window)
        windows = covers_on_windows(covered) if sys.platform == "win32" else None
        if tracking:
            wait_for_condition(covered, lambda: covered.evaluate(STATE), lambda state: state == "hidden",
                               "a window that another one covers never turned hidden with occlusion tracking enabled (windows: %r)" % (windows,))
        else:
            covered.evaluate(TWO_FRAMES)
        return covered.evaluate(STATE), covered.evaluate(CHANGES)


def test_a_covered_window_stays_visible_unless_occlusion_tracking_is_enabled(firefox_binary):
    tracked_state, tracked_changes = second_window_over_the_first(firefox_binary, True)
    assert tracked_state == "hidden" and tracked_changes == ["visible", "hidden"], (
        "control: with tracking enabled the covered page reads %r, changes %r" % (tracked_state, tracked_changes))

    state, changes = second_window_over_the_first(firefox_binary, None)
    assert state == "visible" and changes == ["visible"], (
        "the product's covered page reads %r, changes %r: it must stay visible, with no visibilitychange" % (state, changes))
    assert state != tracked_state, "enabled and disabled read the same: the declaration decides nothing"
