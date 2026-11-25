"""A window that another one covers stays visible, unless occlusion tracking is enabled.

What it fixes: with ``widget.windows.window_occlusion_tracking.enabled`` and ``zoom.stealth.widget.occlusion_tracking.enabled`` false, as the core declares
them from one value, a page whose window is fully covered by a second window keeps ``document.visibilityState`` ``visible`` and gets no ``visibilitychange``.
Retail Windows with the tracker on says ``hidden`` there (and then throttles ``requestAnimationFrame`` to 1 Hz and the timers to 1000 ms); a session nobody
looks at must not read as backgrounded.

The hook: ``nsWindow::OnVisibilityNotifyEvent`` in ``widget/gtk/nsWindow.cpp`` of the series, folder ``fingerprint/screen-window/occlusion``: the condition of the
ternary that decides ``OCCLUDED`` or ``UNKNOWN`` reads the pref (``StealthOcclusionTracked``). On Windows the same declaration is Mozilla's own pref, so the same
page reading is the proof that the two builds do not disagree.

The control, in the same test: the two prefs true. The same covering, and the covered page turns ``hidden``. Without it the first half
proves nothing (a window that was never covered is visible too); it runs first, so a runner on which two windows do not cover each other is red here and not a
green that means nothing.

The covering window. Linux: the display is a private Xvfb started for this test (``_os_focus.a_display_of_its_own``, ``DISPLAY`` set with ``monkeypatch`` before
the session starts), so no other worker's headed window can sit above or inside the pair. No window manager runs there. The cover is NOT a second Firefox
session: it is a window of the test's own (``_x_visibility.XVisibility.cover_window``), an override-redirect ``InputOutput`` window, larger than the covered
toplevel by 32 px on every side (inside the screen where it fits, and never over the pointer), mapped and raised, and never given the focus.

Why the pointer: with no window manager the focus is ``PointerRoot`` and GDK treats the toplevel under the pointer as the focused one, and the pointer sits at the
screen centre, inside the covered window. Any window mapped over it makes the pointer leave the covered toplevel; GDK turns that crossing into a GTK focus-out
(``OnContainerFocusOutEvent``, then a ``leave notify``) and GTK draws a window that loses the focus (``nsWindow::OnExposeEvent``,
``NotifyOcclusionState(VISIBLE)``). When X delivered ``PartiallyObscured`` and ``FullyObscured`` close to that, the draw ran 0.4 to 15 ms after the
``FullyObscured`` and turned the window ``VISIBLE`` again before the posted visibility update ran: the page never recorded ``hidden`` (hunt run 45603211231, 58
reds of 1294; shard 1 it-0014: ``VisibilityNotify state 1`` at 54.355670, ``OnContainerFocusOutEvent`` at 54.355752, ``state 2`` at 54.356290,
``OnExposeEvent`` with ``mIsFullyOccluded 0`` at 54.359166). Mozilla's own behaviour, in retail too; the engine is not changed. The first reading, that the focus
moved to a second session's window (an X ``FocusOut``), was refuted by run 45620136865: 458 reds of 463 with an X cover over the pointer, FocusOut 0 in 40
iterations of the old arrangement. So on Linux, before any session starts, the test warps the pointer through its own connection to the bottom-right pixel of the
root (``XVisibility.warp_pointer_to_the_corner``, computed from the root's geometry), and the cover leaves the pointer outside it. Windows: the second session
stays (the tracker needs its window, below).

The engine turns a window ``OCCLUDED`` only on a ``VisibilityNotify`` with ``FullyObscured`` of its toplevel, so the test asks X itself, through a connection of
its own (``_x_visibility.XVisibility``, libX11 as the focus test): it selects ``VisibilityChangeMask``, ``FocusChangeMask``, ``EnterWindowMask`` and
``LeaveWindowMask`` on the covered toplevel before the cover is created; it creates and raises the cover and reads back ONCE, asserted, the stacking
(``XQueryTree``: the cover above the covered window), the geometries (the covered window inside the cover) and the pointer (``XQueryPointer``: outside both
rectangles); then it waits for X's ``VisibilityNotify FullyObscured`` of the covered toplevel, in both halves. A red there says "arrangement" (X never told the
window it is covered, so the page proves nothing). The precondition, asserted in both halves before the page is judged: no ``EnterNotify``, ``LeaveNotify``,
``FocusIn`` or ``FocusOut`` reached the covered toplevel between the start of the watch and the verdict, and the pointer read back is outside both rectangles
("arrangement: the covering moved the pointer or the focus", with the X record). With tracking enabled, X's ``FullyObscured`` arrived and a page that never turns
``hidden`` is red with a message that starts with "ENGINE". Without tracking, the same ``FullyObscured`` arrived and the page must stay ``visible``: the product
half's meaning is unchanged. The X record (the two window ids, the stacking and geometries read back, the pointer, every visibility, crossing and focus event with
its time since the watch began, and whether ``FullyObscured`` arrived) is saved on every run, green or red, with the Windows record below.
Windows: opening the second window is not enough, and neither is putting it above the first one. ``WinWindowOcclusionTracker`` marks a root window
``OCCLUDED`` only when the windows ABOVE it in the Z-order already cover its rectangle (``widget/windows/WinWindowOcclusionTracker.cpp``,
``ProcessComputeNativeWindowOcclusionStatusCallback``: ``it->second = (mUnoccludedDesktopRegion == currUnoccludedDestkop) ? OCCLUDED : VISIBLE``), and it
computes that only when an event it hooks tells it to (``RegisterEventHooks``): a change of the foreground window, a move or resize, a minimize or restore, a
state change, a cloak, and ``EVENT_OBJECT_LOCATIONCHANGE`` of the processes that had a visible window at the last calculation. A change of the Z-order is none of
them, and neither is another process's window being shown (``ProcessEventHookCallback`` returns on ``EVENT_OBJECT_SHOW``). The foreground window is not special
for the calculation: it is a source of events, nothing more. So a cover that is put above a window that keeps the foreground, with the covered window already
where it is wanted, tells the tracker nothing, and the page stays ``visible`` for as long as the test waits; the same two windows are covered at once when the
second session's window takes the foreground, because the change of foreground is an event.

So on Windows the test builds the whole chain itself, with calls the system honours whoever has the foreground, and waits for what it reads back:

1. Before the page is asked anything, the covered window becomes ``HWND_TOPMOST`` (the system keeps a topmost window above every non-topmost one: a window of
   the same declared size that another session of the same desktop opened later cannot sit above it), it is moved by one pixel (a real
   ``EVENT_OBJECT_LOCATIONCHANGE``, the tracker's own signal), and the test waits, with a deadline, until Windows reads back that no window above it covers it
   and the page reads ``visible``. A red there says "the lab could not leave the covered window uncovered", with the Z-order.
2. The second session opens the covering window. The covering window becomes ``HWND_TOPMOST`` (above the covered one: a window made topmost goes to the top of
   the topmost windows) and the covered window moves inside its rectangle as the tracker reads it (a maximized window counts as its monitor's work area) with
   ``SWP_NOZORDER | SWP_NOACTIVATE``. The test then waits, with a deadline, until Windows reads back what the tracker checks: the covering window visible, not
   minimized, not cloaked, not layered-transparent, not a popup, above the covered one in the ``GW_HWNDPREV`` chain, and the covered one inside it. A red there
   says "the lab could not cover the window", with that read-back; it is never the verdict about the page.
3. Only then the page is waited on, and while it still reads ``visible`` the covered window is moved by one pixel again, every tenth look, so that the tracker
   has been signalled AFTER the arrangement stood. A page that never turns ``hidden`` now is the engine's: the arrangement was read back as complete.

Every message carries the calls' results and ``GetLastError``, the Z-order from the top down to the covered window (process, class, topmost), and the
foreground window and its process, read once after the arrangement and again when the test judges; ``FailureEvidence`` saves them under
``$RUNNER_TEMP/invpw-evidence/test_window_occlusion/`` on every run, green or red, with one line per half ("foreground: pid X (engine yes/no)").

A page reads it from ``document.visibilityState`` and the ``visibilitychange`` event.

What the control judges: that the page recorded ``hidden`` AFTER X's ``FullyObscured`` (``control_sees_hidden``, a list of the ``visibilitychange`` events the
page records, not the state it reads at the end). ``nsWindow::OnExposeEvent`` calls ``NotifyOcclusionState(VISIBLE)`` at every draw of the window's container,
Mozilla's own code and the same in retail: a draw after the ``FullyObscured`` turns the window ``VISIBLE`` again, X (whose state did not change) says nothing
more, and the page ends ``visible`` in a run in which it was ``hidden``: ``visible, hidden, visible``. The product half is unchanged: ``hidden`` is never seen.

That sequence is also a case of its own, on Linux, deterministic: in the control half, once the page has recorded ``hidden``, the test draws the covered window
again and leaves it covered (``XVisibility.shrink_by_one``: one pixel narrower through X, the cover above it and larger), and the page must end ``visible``
with the changes ``visible, hidden, visible``: the state at the end, which was the criterion of the control before, is ``visible`` here, so it would be red.

The engine's own log of its window system (``MOZ_LOG=Widget:4``, the level of nsWindow's LOG; the core passes it, ``environment.MOZ_DIAGNOSTICS``) is written into the evidence
folder with the rest, one file per process (``MOZ_LOG_FILE`` with ``%PID``, ``FailureEvidence.arm``).
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
from _x_visibility import XVisibility, control_sees_hidden, pointer_or_focus_moved, product_stays_visible

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
#: The engine's log of its window system, written into the evidence (a diagnostic no page reads).
WIDGET_LOG = "Widget:4"

#: How far inside the covering window's rectangle the covered one is put, in device pixels.
INSET = 32

#: Windows: while a wait for the tracker's answer goes on, the covered window is moved by one pixel on the first look and on every this-many-th one after it.
SIGNAL_EVERY_LOOKS = 10


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

    def covering(self, hwnd):
        """The windows above ``hwnd`` in the Z-order that the tracker counts as covering it by themselves: each can occlude (``why_it_cannot_cover``) and
        contains ``hwnd``'s rectangle as the tracker reads it. A list of ``describe`` tuples, empty when nothing above covers it."""
        inner, found = self.tracker_rect(hwnd), []
        above = self.user32.GetWindow(hwnd, self.GW_HWNDPREV)
        while above:
            if not self.why_it_cannot_cover(above):
                outer = self.tracker_rect(above)
                if outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]:
                    found.append(self.describe(above))
            above = self.user32.GetWindow(above, self.GW_HWNDPREV)
        return found

    def make_topmost(self, hwnd):
        """``HWND_TOPMOST``: the system keeps the window above every non-topmost one, whoever has the foreground; activates nothing."""
        return self.set_window_pos(hwnd, self.HWND_TOPMOST, 0, 0, 0, 0, self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_NOACTIVATE)

    def nudge(self, hwnd, step):
        """Moves ``hwnd`` ``step`` pixels to the right (left when negative), its size and its place in the Z-order as they are: a real
        ``EVENT_OBJECT_LOCATIONCHANGE``, the signal ``WinWindowOcclusionTracker`` recalculates on (a move to where the window already is sends none)."""
        left, top, _, _ = self.rect(hwnd)
        return self.set_window_pos(hwnd, None, left + step, top, 0, 0, self.SWP_NOSIZE | self.SWP_NOZORDER | self.SWP_NOACTIVATE)

    def arrange(self, covered, cover):
        """Makes ``cover`` topmost, then moves ``covered`` inside ``cover``'s rectangle and its work area; activates nothing. The calls' answers."""
        calls = {"cover HWND_TOPMOST": self.make_topmost(cover)}
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
    """Windows only: finds the covered window, leaves it uncovered (``leave_uncovered``), finds the covering one (``find_the_cover``), arranges the covering
    (``arrange``) and signals the tracker after it (``wait_for_hidden``); each step reads back from the system what the next one needs before the page is judged."""

    def __init__(self, covered) -> None:
        self.win = Win32()
        self.cover = None
        self.looks = 0
        self.signals = 0
        self.uncovered = None
        self.calls = None
        self.after_arrangement = None
        self.first = self.find(covered, COVERED_TITLE)

    def find(self, covered, title):
        seen = wait_for_condition(covered, lambda: self.win.window_titled(title), lambda hwnd: hwnd is not None,
                                  "no single visible top-level window is titled %r" % title)
        return seen.history[-1][1]

    def signal(self):
        """The covered window one pixel to the right, then one to the left, and so on: every call is a real move."""
        self.win.nudge(self.first, 1 if self.signals % 2 == 0 else -1)
        self.signals += 1

    def wait(self, covered, sample, done, signalling):
        """Until ``done(sample())`` (the deadline of ``wait_for_condition``), reading the page between two looks; with ``signalling``, the covered window is also
        moved by one pixel on the first look and on every ``SIGNAL_EVERY_LOOKS``-th one after it. With what was seen on the way."""
        looks = []

        def pause():
            if signalling and len(looks) % SIGNAL_EVERY_LOOKS == 0:
                self.signal()
            looks.append(None)
            covered.wait_for_timeout(100)

        return watch(sample, done, DEADLINE_S, pause)

    def leave_uncovered(self, covered):
        """Before anything covers the page: the covered window topmost and moved by one pixel, and a wait until Windows reads back that no window above it covers
        it and the page reads ``visible`` (the caller asserts the page once more). A window of the same size that something else opened over it
        (another session of this desktop, the load of the runner) is under it from here on."""
        self.uncovered = {"calls": {"covered HWND_TOPMOST": self.win.make_topmost(self.first)}}
        self.signal()
        seen = self.wait(covered, lambda: (covered.evaluate(STATE), self.win.covering(self.first)), lambda now: now[0] == "visible" and not now[1], True)
        self.uncovered["z-order"] = self.win.z_order_down_to(self.first)
        if not seen.ok:
            state, above = seen.history[-1][1]
            what = ("the lab could not leave the covered window uncovered" if above else
                    "ENGINE: Windows reads back that nothing above the covered window covers it, and the page reads %r" % (state,))
            raise AssertionError("%s; seen: %s; %r" % (what, seen.render(), self.evidence()))

    def find_the_cover(self, covered):
        self.cover = self.find(covered, COVER_TITLE)
        wait_for_condition(covered, lambda: self.win.why_it_cannot_cover(self.cover), lambda why: why == "",
                           "the covering window is not one the tracker counts as covering")

    def arrange(self, covered):
        """The arrangement the precondition rests on: the cover topmost, the covered window inside it, and a wait until Windows reads back that the second
        covers the first as the tracker reads them. Red: "the lab could not cover the window", never a verdict about the page."""
        self.calls = self.win.arrange(self.first, self.cover)
        seen = self.wait(covered, lambda: self.read()["ok"], lambda ok: ok, False)
        self.after_arrangement = self.read()
        assert seen.ok, "the lab could not cover the window: the second window does not cover the first one as the tracker reads them; seen: %s; %r" % (
            seen.render(), self.evidence())

    def wait_for_hidden(self, covered):
        """Until the covered page reads ``hidden``, moving the covered window by one pixel while it still does not: the tracker is signalled after the
        arrangement (read back complete by ``arrange``), as it is when a user moves a window. With what was seen on the way."""
        return self.wait(covered, lambda: covered.evaluate(STATE), lambda state: state == "hidden", True)

    def read(self):
        return self.win.coverage(self.first, self.cover)

    def line(self, seen):
        """One line: the foreground window of a read-back, whether it belongs to one of the two engines, and how many times the tracker was signalled."""
        foreground = seen["foreground"]
        pid = foreground[1] if foreground else None
        engine = {self.win.pid(self.first): "the covered session's engine", self.win.pid(self.cover): "the covering session's engine"}.get(pid)
        return "foreground: pid %s %s (engine %s), signals: %d" % (pid, self.win.process_name(pid) if pid else "", "yes, %s" % engine if engine else "no", self.signals)

    def evidence(self):
        """The covered window left uncovered (the call, the Z-order), the topmost calls, the read-back right after them, a read-back now (a covered window
        raised back after the arrangement shows here), and the line."""
        now = self.read() if self.cover is not None else None
        return {"uncovered": self.uncovered, "calls": self.calls, "after the arrangement": self.after_arrangement, "now": now,
                "line": self.line(now) if now else "no covering window yet, signals: %d" % self.signals}


class CoveringOnX:
    """Linux only: what X says about the covered toplevel and the test's own cover window, through the test's own connection ``x`` to the private display (see
    the module docstring)."""

    def __init__(self, x) -> None:
        self.x = x
        self.first = self.cover = None
        self.visibility = None
        self.after_raise = None
        self.pointer = None
        #: The length of the page's list of changes at the last look made before X's ``FullyObscured`` showed up (1: the start).
        self.not_before = 1
        self.drawn = None

    def watch_the_covered_window(self, covered):
        """Finds the covered page's toplevel by its title and selects its visibility events, before the cover is mapped."""
        self.first = wait_for_condition(covered, lambda: self.x.window_titled(COVERED_TITLE), lambda window: window is not None,
                                        "arrangement: no single viewable toplevel is titled %r" % COVERED_TITLE).history[-1][1]
        self.visibility = self.x.watch(self.first)

    def cover_the_window_and_read_back(self):
        """Creates the cover (an override-redirect window of the test's own, the pointer outside it, no focus asked), raises it, reads the stacking, the
        geometries and the pointer back once, and asserts them."""
        self.cover = self.x.cover_window(self.first)
        self.after_raise = self.x.raise_and_read(self.first, self.cover)
        self.pointer = self.x.read_pointer_back(self.first, self.cover)
        assert self.after_raise["ok"], "arrangement: after XRaiseWindow the cover is not above the covered toplevel with the covered one inside it: %r" % (
            self.evidence(),)

    def wait_until_x_says_fully_obscured(self, covered):
        """The ground truth: X sends the covered toplevel ``VisibilityNotify FullyObscured``."""

        def look():
            changes = covered.evaluate(CHANGES)     # read before X is asked: what it holds happened before an event X has not yet delivered
            first = self.x.drain().first()
            if first is None:
                self.not_before = len(changes)
            return first

        seen = watch(look, lambda first: first is not None, DEADLINE_S, lambda: covered.wait_for_timeout(100))
        assert seen.ok, "arrangement: X never sent the covered toplevel VisibilityNotify FullyObscured (%s), so the page proves nothing: %r" % (
            self.line(), self.evidence())

    def assert_the_pointer_and_the_focus_did_not_move(self):
        """The precondition, before the page is judged: no crossing and no focus event reached the covered toplevel since the watch began, and the pointer read
        back after the cover was up is outside both rectangles (``pointer_or_focus_moved``)."""
        self.x.drain()
        problem = pointer_or_focus_moved(self.visibility) or ("" if self.pointer is not None else "arrangement: the covering moved the pointer or the focus: "
                                                                "the pointer was never read back after the cover was up")
        assert not problem, "%s; X: %r" % (problem, self.evidence())

    def remove_the_cover(self):
        if self.cover is not None:
            self.x.destroy_window(self.cover)

    def engine_message(self, seen, changes):
        return ("ENGINE: X sent the covered toplevel VisibilityNotify FullyObscured (%s) and with occlusion tracking enabled its page never recorded hidden "
                "after it (nsWindow::OnVisibilityNotifyEvent did not make the window OCCLUDED, or the state never reached the page); the changes it "
                "recorded: %r (from index %d on); seen: %s; X: %r" % (self.line(), changes, self.not_before, seen.render(), self.evidence()))

    def draw_while_covered(self, covered):
        """The covered toplevel one pixel narrower through X, the cover above it and larger, and a wait until the page reads ``visible``: the draw
        (``nsWindow::OnExposeEvent``) says ``VISIBLE`` and X, whose state of the window did not change, says nothing more. With the state and the changes."""
        resized = self.x.shrink_by_one(self.first)
        before, after = resized["before"], resized["after"]
        assert before and after and after["width"] == before["width"] - 1, (
            "arrangement: XResizeWindow did not make the covered toplevel one pixel narrower: %r" % (resized,))
        seen = watch(lambda: covered.evaluate(STATE), lambda state: state == "visible", DEADLINE_S, lambda: covered.wait_for_timeout(100))
        covered.evaluate(TWO_FRAMES)
        self.drawn = {"resize": resized, "state": covered.evaluate(STATE), "changes": covered.evaluate(CHANGES), "seen": seen.render()}
        assert seen.ok, "the covered window was drawn again, still covered, and the page never read visible again; %r" % ((self.drawn, self.evidence()),)
        return self.drawn

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
        return {"line": line, "covered": None if self.first is None else "0x%x" % self.first, "cover": None if self.cover is None else "0x%x" % self.cover, "pointer warped": self.x.warped, "pointer read back": self.pointer,
                "after the raise": self.after_raise, "forced draw": self.drawn, "not before": self.not_before, "visibility": self.visibility.as_dict() if self.visibility else None}


class Record:
    """A record the test keeps itself, in the shape ``FailureEvidence.note_records`` saves (``lock``, ``records``)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.records = []

    def add(self, record) -> None:
        with self.lock:
            self.records.append(record)


def second_window_over_the_first(firefox_binary, tracking, record, x):
    """A dict of what the first session's page recorded after a window is up over it (on Windows the second session's, on Linux the test's own): ``state`` and ``changes`` at the end, ``verdict_changes``
    and ``not_before`` (the list of changes when the wait for ``hidden`` ended, and the index it counts from), ``windows`` (the evidence of the covering: on
    Windows the tracker's view, on Linux X's, also added to ``record`` for the evidence file) and, in the control half on Linux, ``drawn`` (the covered window
    drawn again while it stays covered).

    ``tracking`` is None for the product as it launches, or the value both tracking prefs are given; ``x`` is the test's own connection to the private
    display on Linux (the cover is its own window, no second session), None on Windows (the second session's window covers)."""
    half = "control (tracking enabled)" if tracking else "product"
    extra = None if tracking is None else {name: tracking for name in TRACKING_PREFS}
    second_session = contextlib.nullcontext() if x is not None else in_a_thread_of_its_own(firefox_binary)
    with launch(firefox_binary, extra_prefs=extra) as first, second_session as second:
        covered = first.new_page()
        covered.goto("about:blank")
        covered.evaluate(NAME, COVERED_TITLE)
        on_x = CoveringOnX(x) if x is not None else None
        covering = None
        verdict_changes, drawn = None, None
        try:
            if sys.platform == "win32":
                covering = CoveringOnWindows(covered)
                covering.leave_uncovered(covered)
            assert covered.evaluate(WATCH) == "visible", "the page was not visible before anything covered it"
            if on_x:
                on_x.watch_the_covered_window(covered)
            if covering:
                second.run(opens_the_covering_window)
                covering.find_the_cover(covered)
                covering.arrange(covered)
            if on_x:
                on_x.cover_the_window_and_read_back()
                on_x.wait_until_x_says_fully_obscured(covered)
            not_before = on_x.not_before if on_x else 1
            if tracking:
                seen = covering.wait_for_hidden(covered) if covering else watch_hidden_change(covered, not_before)
                if on_x:
                    on_x.assert_the_pointer_and_the_focus_did_not_move()
                verdict_changes = covered.evaluate(CHANGES)
                assert seen.ok and control_sees_hidden(verdict_changes, not_before), on_x.engine_message(seen, verdict_changes) if on_x else (
                    "a window that another one covers never turned hidden with occlusion tracking enabled; the changes the page recorded: %r; seen: %s; "
                    "windows: %r" % (verdict_changes, seen.render(), covering.evidence() if covering else None))
                if on_x:
                    drawn = on_x.draw_while_covered(covered)
            else:
                covered.evaluate(TWO_FRAMES)
                if on_x:
                    on_x.assert_the_pointer_and_the_focus_did_not_move()
            windows = covering.evidence() if covering else on_x.evidence() if on_x else None
            return {"state": covered.evaluate(STATE), "changes": covered.evaluate(CHANGES), "verdict_changes": verdict_changes, "not_before": not_before,
                    "windows": windows, "drawn": drawn}
        finally:
            if covering:
                record.add({"half": half, **covering.evidence()})
            if on_x:
                record.add({"half": half, **on_x.evidence()})
                on_x.remove_the_cover()


def watch_hidden_change(covered, not_before):
    """Until the page's list of changes holds ``hidden`` from index ``not_before`` on (the deadline of ``wait_for_condition``; a ``hidden`` that a draw has
    already undone is in it), with the lists seen on the way."""
    return watch(lambda: covered.evaluate(CHANGES), lambda changes: control_sees_hidden(changes, not_before), DEADLINE_S,
                 lambda: covered.wait_for_timeout(100))


def lines(record):
    """Per half, the one line of the arrangement: X's on Linux, the foreground and the signals on Windows."""
    with record.lock:
        return ["%s: %s" % (r["half"], r.get("line")) for r in record.records]


@contextlib.contextmanager
def the_display(monkeypatch):
    """Linux: a private Xvfb for this test and the test's own X connection to it. Windows: the runner's desktop, None."""
    if sys.platform == "win32":
        yield None
        return
    with a_display_of_its_own(monkeypatch) as display:
        x = XVisibility(display)
        try:
            x.warp_pointer_to_the_corner()      # before any session starts: no toplevel is under the pointer, none gets a crossing from it
            yield x
        finally:
            x.close()


def test_a_covered_window_stays_visible_unless_occlusion_tracking_is_enabled(firefox_binary, monkeypatch):
    record = Record()
    with FailureEvidence("test_window_occlusion") as evidence, the_display(monkeypatch) as x:
        evidence.note_records("windows", record)
        evidence.arm(moz_log=WIDGET_LOG)
        tracked = second_window_over_the_first(firefox_binary, True, record, x)
        assert control_sees_hidden(tracked["verdict_changes"], tracked["not_before"]), (
            "control: with tracking enabled the covered page never recorded hidden after X's FullyObscured; the changes it recorded: %r (from index %d on); %s; "
            "windows: %r" % (tracked["verdict_changes"], tracked["not_before"], lines(record), tracked["windows"]))
        drawn = tracked["drawn"]
        if x is not None:
            assert drawn["state"] == "visible" and drawn["changes"] == ["visible", "hidden", "visible"], (
                "a draw of the covered window that stays covered must turn its page visible again, and the page records visible, hidden, visible: it reads %r, "
                "the changes it recorded: %r; %s; windows: %r" % (drawn["state"], drawn["changes"], lines(record), tracked["windows"]))

        product = second_window_over_the_first(firefox_binary, None, record, x)
        assert product["state"] == "visible" and product_stays_visible(product["changes"]), (
            "the product's covered page reads %r, changes %r: it must stay visible, with no visibilitychange; %s; windows: %r" % (
                product["state"], product["changes"], lines(record), product["windows"]))
        assert product["changes"] != tracked["verdict_changes"], "enabled and disabled recorded the same changes: the declaration decides nothing"
        evidence.keep("always kept: %s" % (lines(record),))
        sys.stderr.write("occlusion arrangement: %s\n" % (lines(record),))
