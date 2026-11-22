"""Fullscreen is decided from the declared screen, with no window manager to answer it.

What it fixes: on an X display with no window manager (Xvfb, the way the engine runs on Linux servers) GTK's fullscreen is a request nobody answers, so
Mozilla refuses it: the DOM goes fullscreen and the window does not (``display-mode: fullscreen`` false, ``innerHeight`` the work area). Retail Windows
decides it itself and gives the screen. With the engine active the widget decides it too, from the screen the core declared: after ``requestFullscreen()``
the page reads ``innerWidth x innerHeight`` equal to the declared screen and ``display-mode: fullscreen``.

The hook: ``nsWindow::MakeFullScreen`` in ``widget/gtk/nsWindow.cpp`` of the series, folder ``fingerprint/screen-window/occlusion`` (the marker names
``fingerprint/screen-window/geometry``, whose function ``StealthMakeFullScreenWithoutWM`` in ``widget/gtk/StealthFullscreenGtk.cpp`` it calls). On Windows
the same reading is held by ``nsIWidget::InfallibleMakeFullScreen`` and the Win32 rules of the same folder (``geometry``).

The screen is pinned to 1600x900, which no Linux runner has (the display is 1920x1080); on Windows the test reads the runner's monitor first (the primary one, in
device pixels, and in CSS pixels at the usual scale factors) and declares the first of 1600x900, 1366x768 and 1280x720 that is none of them, so a window that
filled the host always reads differently from the declared one, whatever monitor the runner has.

The control, in the same test: the engine inactive (``zoom.stealth.fpp.hw_seed`` 0: the engine is on when it is above 0) keeps Mozilla's behaviour. On
Linux without a window manager that is the refusal (``display-mode: fullscreen`` false and the window not the declared screen); on Windows, which always has a
window manager and a widget that decides fullscreen, it is the host's monitor, not the declared screen. The same click, the same pinned screen; only the engine
differs.

What the verdict waits for is the condition itself, not a count of equal looks: with the engine on, until the page reads the declared size AND ``display-mode:
fullscreen`` AND a ``fullscreenElement`` (a red names the last geometry seen); with the engine off, until ``fullscreenchange`` (or ``fullscreenerror``) was heard
and, where the window really changes size (Windows), the ``resize`` the change causes was heard too, then one round trip to the page's own process.

The click is real input (``page.mouse``): ``requestFullscreen`` needs a user gesture. A page reads it from ``innerWidth``, ``innerHeight``,
``matchMedia('(display-mode: fullscreen)')`` and ``document.fullscreenElement``.
"""
from __future__ import annotations

import http.server
import sys
import threading

import pytest

from _os_focus import host_monitor_size
from _servers import LocalServer
from _waits import watch
from _window_sessions import DEADLINE_S, launch

pytestmark = pytest.mark.e2e

#: The device pixel ratio is pinned too: the core declares 1.5 for some personas (Windows runner), and a page reads the screen and the window in
#: CSS pixels, declared / dpr (1067x600), which is not what this test is about.
CANDIDATES = ((1600, 900), (1366, 768), (1280, 720))
#: The scale factors at which a monitor of the host reads, in CSS pixels, smaller than it is.
SCALES = (1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0)


def declared_screen():
    """The declared screen: 1600x900 unless the host's monitor (Windows) is that size, at any usual scale; then the first candidate that it is not."""
    monitor = host_monitor_size()
    if monitor is None:
        return CANDIDATES[0]
    taken = {(round(monitor[0] / scale), round(monitor[1] / scale)) for scale in SCALES}
    for width, height in CANDIDATES:
        if not any(abs(width - w) <= 1 and abs(height - h) <= 1 for w, h in taken):
            return width, height
    raise AssertionError("every candidate declared screen %r is a size of the host's monitor %r" % (CANDIDATES, monitor))

PAGE = (b"<!doctype html><html><head><meta charset=utf-8><title>fullscreen</title></head><body style=\"margin:0\">"
        b"<button id=go style=\"position:fixed;left:20px;top:20px;width:160px;height:40px;margin:0\">fullscreen</button>"
        b"<script>"
        b"window.__how = null; window.__error = null; window.__order = [];"
        b"for (const t of ['fullscreenchange', 'fullscreenerror']) document.addEventListener(t, () => { window.__how = window.__how || t; window.__order.push(t); });"
        b"window.addEventListener('resize', () => window.__order.push('resize'));"
        b"document.getElementById('go').addEventListener('click', () => {"
        b"  document.documentElement.requestFullscreen().catch(e => { window.__error = String(e); });"
        b"});"
        b"</script></body></html>")

GEOMETRY = """() => ({
  innerWidth, innerHeight,
  screen: [screen.width, screen.height],
  displayModeFullscreen: matchMedia('(display-mode: fullscreen)').matches,
  fullscreenElement: document.fullscreenElement !== null,
})"""
HOW = "() => [window.__how, window.__error]"
ORDER = "() => window.__order"
#: A round trip to the page's own process: it has handled what the parent sent it before this ran.
ROUND_TRIP = "() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))"


@pytest.fixture
def page_url():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)

        def log_message(self, *args):
            pass

    with LocalServer(("127.0.0.1", 0), Handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            yield "http://127.0.0.1:%d/" % server.server_address[1]
        finally:
            server.shutdown()


def fullscreen_geometry(firefox_binary, page_url, engine_on, declared):
    """The page's geometry once the fullscreen request has been answered: the condition with the engine on, the events with it off (see the module docstring)."""
    extra = None if engine_on else {"zoom.stealth.fpp.hw_seed": 0}
    pin = {"screen.width": declared[0], "screen.height": declared[1], "screen.dpr": 1.0}
    with launch(firefox_binary, extra_prefs=extra, pin=pin, headless=True) as browser:
        page = browser.new_page()
        page.goto(page_url)
        page.mouse.move(90, 40)
        page.mouse.click(100, 40)

        def pump():
            page.wait_for_timeout(100)

        answered = watch(lambda: page.evaluate(HOW), lambda how: how[0] is not None, DEADLINE_S, pump)
        assert answered.ok, ("the click on a button that asks for fullscreen was followed by neither fullscreenchange nor fullscreenerror; seen: %s"
                             % answered.render())
        if engine_on:
            def is_the_declared_fullscreen(geometry):
                return ((geometry["innerWidth"], geometry["innerHeight"]) == declared
                        and geometry["displayModeFullscreen"] is True and geometry["fullscreenElement"] is True)

            # the assertions of the test judge the last geometry seen: a window that never gets there is red there, with what it read
            watch(lambda: page.evaluate(GEOMETRY), is_the_declared_fullscreen, DEADLINE_S, pump)
        else:
            if sys.platform == "win32":
                # the window really changes size: the change is over once the resize that goes with fullscreenchange was heard (a window that was
                # already the size of the screen has none to hear: the geometry read below is then what judges)
                watch(lambda: page.evaluate(ORDER), lambda order: "fullscreenchange" in order and "resize" in order, DEADLINE_S, pump)
            page.evaluate(ROUND_TRIP)
        return dict(page.evaluate(GEOMETRY), how=page.evaluate(HOW))


def test_fullscreen_without_a_window_manager_is_the_declared_screen(firefox_binary, page_url):
    declared = declared_screen()
    on = fullscreen_geometry(firefox_binary, page_url, engine_on=True, declared=declared)
    assert on["screen"] == list(declared), "the pinned screen is not what the page reads: %r" % on
    assert (on["innerWidth"], on["innerHeight"]) == declared, "with the engine on the fullscreen window is not the declared screen: %r" % on
    assert on["displayModeFullscreen"] is True and on["fullscreenElement"] is True, "with the engine on the page does not read fullscreen: %r" % on

    off = fullscreen_geometry(firefox_binary, page_url, engine_on=False, declared=declared)
    assert (off["innerWidth"], off["innerHeight"]) != declared, "control: the engine inactive also gives the declared screen: %r" % off
    if sys.platform != "win32":
        # no window manager: Mozilla's refusal, the DOM fullscreen without the window
        assert off["displayModeFullscreen"] is False, "control: without a window manager and without the engine the window went fullscreen: %r" % off
    assert (on["innerWidth"], on["innerHeight"], on["displayModeFullscreen"]) != (off["innerWidth"], off["innerHeight"], off["displayModeFullscreen"])
