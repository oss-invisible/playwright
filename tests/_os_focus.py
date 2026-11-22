"""What the window e2e tests ask the operating system itself: which window holds the input focus, on a display of the test's own.

A page's ``document.hasFocus()`` is what the engine reports. A test that judges "the first session kept the focus when another one took the display" must
first know that the OS did give the focus to the second session's window; otherwise a green means only that nothing happened (the second window never took
it, or took it later than the read). The OS answers that without the engine:

* Linux: the X input focus (``XGetInputFocus`` on the display the sessions share); the display is a private Xvfb started for the test (``a_display_of_its_own``),
  so no other test's headed window moves the focus in it. No window manager runs there, so nobody ever sets the X input focus: it stays ``PointerRoot``
  and GDK activates whichever toplevel is under the pointer (``has_pointer_focus``, ``gdk/x11/gdkdevicemanager-core-x11.c``), and Firefox raises a window
  itself only when it is not active already (``nsWindow::SetFocus``, ``gtk_window_present_with_time``). The test therefore does what a window manager does
  when the user clicks a window: ``XSetInputFocus`` on the toplevel the step names (found by its ``_NET_WM_NAME``), and reads ``XGetInputFocus`` back;
* Windows: the foreground window (``GetForegroundWindow``), the window of the session found by the title its page gave it.
"""
from __future__ import annotations

import contextlib
import ctypes
import subprocess
import sys
import threading
from typing import Iterator, Optional

#: ``RevertToParent`` and ``CurrentTime`` of ``XSetInputFocus`` (as GDK's own ``gdk_x11_window_focus`` without a window manager); ``IsViewable``.
_REVERT_TO_PARENT, _CURRENT_TIME, _IS_VIEWABLE = 2, 0, 2


class _XWindowAttributes(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int), ("height", ctypes.c_int), ("border_width", ctypes.c_int),
                ("depth", ctypes.c_int), ("visual", ctypes.c_void_p), ("root", ctypes.c_ulong), ("c_class", ctypes.c_int), ("bit_gravity", ctypes.c_int),
                ("win_gravity", ctypes.c_int), ("backing_store", ctypes.c_int), ("backing_planes", ctypes.c_ulong), ("backing_pixel", ctypes.c_ulong),
                ("save_under", ctypes.c_int), ("colormap", ctypes.c_ulong), ("map_installed", ctypes.c_int), ("map_state", ctypes.c_int),
                ("all_event_masks", ctypes.c_long), ("your_event_mask", ctypes.c_long), ("do_not_propagate_mask", ctypes.c_long),
                ("override_redirect", ctypes.c_int), ("screen", ctypes.c_void_p)]


#: An X error (a window gone between two requests, a focus asked for a window not yet viewable) is ignored instead of ending the process as Xlib's
#: default handler does; the wait that asked reads the focus again on its next look.
_X_ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
_ignore_x_error = _X_ERROR_HANDLER(lambda display, event: 0)


@contextlib.contextmanager
def a_display_of_its_own(monkeypatch) -> Iterator[str]:
    """Linux: a private Xvfb (the number chosen by the server, ``-displayfd``), set as ``DISPLAY`` for the test and stopped at its end.

    Two headed sessions started inside it see each other and nothing else; the Xvfb of ``xvfb-run`` that the parallel workers share is never touched."""
    process = subprocess.Popen(["Xvfb", "-displayfd", "1", "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    answer = {}
    reader = threading.Thread(target=lambda: answer.setdefault("line", process.stdout.readline()), daemon=True)
    reader.start()
    try:
        reader.join(60)
        number = answer.get("line", "").strip()
        assert number.isdigit(), "the private Xvfb did not give a display number (it said %r, exit %r)" % (number, process.poll())
        monkeypatch.setenv("DISPLAY", ":" + number)
        for name in ("WAYLAND_DISPLAY", "XAUTHORITY"):
            monkeypatch.delenv(name, raising=False)
        yield ":" + number
    finally:
        process.terminate()
        try:
            process.wait(30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(30)
        process.stdout.close()


class XFocus:
    """The X input focus of a display, read and given through a connection of its own (libX11, as the engine's own client library)."""

    def __init__(self, display: str) -> None:
        x11 = self.x11 = ctypes.CDLL("libX11.so.6")
        ulong_p, int_p = ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int)
        x11.XOpenDisplay.restype, x11.XOpenDisplay.argtypes = ctypes.c_void_p, (ctypes.c_char_p,)
        x11.XSetErrorHandler.argtypes = (_X_ERROR_HANDLER,)
        x11.XDefaultRootWindow.restype, x11.XDefaultRootWindow.argtypes = ctypes.c_ulong, (ctypes.c_void_p,)
        x11.XInternAtom.restype, x11.XInternAtom.argtypes = ctypes.c_ulong, (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int)
        x11.XQueryTree.argtypes = (ctypes.c_void_p, ctypes.c_ulong, ulong_p, ulong_p, ctypes.POINTER(ulong_p), ctypes.POINTER(ctypes.c_uint))
        x11.XGetWindowAttributes.argtypes = (ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_XWindowAttributes))
        x11.XGetWindowProperty.argtypes = (ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long, ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
                                           ulong_p, int_p, ulong_p, ulong_p, ctypes.POINTER(ctypes.c_void_p))
        x11.XFree.argtypes = (ctypes.c_void_p,)
        x11.XSetInputFocus.argtypes = (ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        x11.XSync.argtypes = (ctypes.c_void_p, ctypes.c_int)
        x11.XGetInputFocus.argtypes = (ctypes.c_void_p, ulong_p, int_p)
        x11.XCloseDisplay.argtypes = (ctypes.c_void_p,)
        x11.XSetErrorHandler(_ignore_x_error)
        self.connection = x11.XOpenDisplay(display.encode())
        assert self.connection, "no connection to the display %r" % display
        self.root = x11.XDefaultRootWindow(self.connection)
        self.net_wm_name = x11.XInternAtom(self.connection, b"_NET_WM_NAME", False)
        self.utf8_string = x11.XInternAtom(self.connection, b"UTF8_STRING", False)

    def holder(self) -> int:
        window, revert = ctypes.c_ulong(), ctypes.c_int()
        self.x11.XGetInputFocus(self.connection, ctypes.byref(window), ctypes.byref(revert))
        return int(window.value)

    def _title(self, window: int) -> str:
        kind, size, count, left, data = ctypes.c_ulong(), ctypes.c_int(), ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_void_p()
        if self.x11.XGetWindowProperty(self.connection, window, self.net_wm_name, 0, 1024, False, self.utf8_string, ctypes.byref(kind),
                                       ctypes.byref(size), ctypes.byref(count), ctypes.byref(left), ctypes.byref(data)) != 0 or not data.value:
            return ""
        try:
            return ctypes.string_at(data.value, count.value).decode("utf-8", "replace") if size.value == 8 else ""
        finally:
            self.x11.XFree(data)

    def _viewable(self, window: int) -> bool:
        attributes = _XWindowAttributes()
        return bool(self.x11.XGetWindowAttributes(self.connection, window, ctypes.byref(attributes))) and attributes.map_state == _IS_VIEWABLE

    def window_titled(self, title: str) -> Optional[int]:
        """The one viewable toplevel (a child of the root: no window manager reparents it) whose ``_NET_WM_NAME`` starts with ``title``, or None."""
        root, parent, children, count = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.POINTER(ctypes.c_ulong)(), ctypes.c_uint()
        if not self.x11.XQueryTree(self.connection, self.root, ctypes.byref(root), ctypes.byref(parent), ctypes.byref(children), ctypes.byref(count)):
            return None
        try:
            windows = [int(children[i]) for i in range(count.value)]
        finally:
            if children:
                self.x11.XFree(children)
        found = [window for window in windows if self._title(window).startswith(title) and self._viewable(window)]
        return found[0] if len(found) == 1 else None

    def ask_for_the_foreground(self, window: int) -> int:
        """What a window manager does when the user clicks the window (none runs on this display): ``XSetInputFocus`` on it, then the focus is read back."""
        self.x11.XSetInputFocus(self.connection, window, _REVERT_TO_PARENT, _CURRENT_TIME)
        self.x11.XSync(self.connection, False)
        return self.holder()

    def close(self) -> None:
        self.x11.XCloseDisplay(self.connection)
        self.connection = None


class ForegroundWindow:
    """Windows: the foreground window of the desktop the sessions run on, and the Firefox window a title names."""

    def __init__(self) -> None:
        from ctypes import wintypes

        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        self.enum = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def holder(self) -> int:
        return self.user32.GetForegroundWindow() or 0

    def window_titled(self, title: str) -> Optional[int]:
        """The one visible top-level Firefox window whose title starts with ``title``, or None (none, or more than one)."""
        found = []

        def each(hwnd, _):
            name, text = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(512)
            self.user32.GetClassNameW(hwnd, name, 256)
            self.user32.GetWindowTextW(hwnd, text, 512)
            if name.value.startswith("MozillaWindowClass") and text.value.startswith(title) and self.user32.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True

        self.user32.EnumWindows(self.enum(each), 0)
        return found[0] if len(found) == 1 else None

    def ask_for_the_foreground(self, hwnd: int) -> int:
        """What a user does by clicking the window: the system may refuse it, and then the foreground is what the test reads."""
        self.user32.SetForegroundWindow(hwnd)
        return self.holder()


def host_monitor_size() -> Optional[tuple]:
    """Windows: the primary monitor in device pixels (the reading of a per-monitor-aware process, whatever the test's own awareness is); None elsewhere."""
    if sys.platform != "win32":
        return None
    user32 = ctypes.WinDLL("user32")
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.SetThreadDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
    previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    try:
        return (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
    finally:
        if previous:
            user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(previous))
