"""What the window e2e tests ask the operating system itself: which window holds the input focus, on a display of the test's own.

A page's ``document.hasFocus()`` is what the engine reports. A test that judges "the first session kept the focus when another one took the display" must
first know that the OS did give the focus to the second session's window; otherwise a green means only that nothing happened (the second window never took
it, or took it later than the read). The OS answers that without the engine:

* Linux: the X input focus (``XGetInputFocus`` on the display the sessions share); the display is a private Xvfb started for the test (``a_display_of_its_own``),
  so no other test's headed window moves the focus in it;
* Windows: the foreground window (``GetForegroundWindow``), the window of the session found by the title its page gave it.
"""
from __future__ import annotations

import contextlib
import ctypes
import subprocess
import sys
import threading
from typing import Iterator, Optional

#: ``None`` and ``PointerRoot``: what the X input focus is when no client holds it.
_X_NO_WINDOW = (0, 1)


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
    """The X input focus of a display, read through a connection of its own (libX11, as the engine's own client library)."""

    def __init__(self, display: str) -> None:
        self.x11 = ctypes.CDLL("libX11.so.6")
        self.x11.XOpenDisplay.restype, self.x11.XOpenDisplay.argtypes = ctypes.c_void_p, (ctypes.c_char_p,)
        self.x11.XGetInputFocus.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int))
        self.x11.XCloseDisplay.argtypes = (ctypes.c_void_p,)
        self.connection = self.x11.XOpenDisplay(display.encode())
        assert self.connection, "no connection to the display %r" % display

    def holder(self) -> int:
        window, revert = ctypes.c_ulong(), ctypes.c_int()
        self.x11.XGetInputFocus(self.connection, ctypes.byref(window), ctypes.byref(revert))
        return int(window.value)

    def is_a_window(self, holder: int) -> bool:
        return holder not in _X_NO_WINDOW

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

    def is_a_window(self, holder: int) -> bool:
        return bool(holder)

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
