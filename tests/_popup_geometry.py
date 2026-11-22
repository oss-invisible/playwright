"""A popup opened the way a page opens one, and what it reads of its own window.

Shared by `test_popup_geometry_is_the_declared_frame_e2e.py` and by the inner-size test of
`test_fingerprint_surface.py`: both need a popup (`window.open` with features, from a click) whose document may carry an
iframe of another site (so, under Fission, in another process), and both read the window geometry from inside it.

The popup is read with a CONDITION, not a pause: the reading is repeated (with the client's events being read between two
looks) until the caller's `ready` holds or 30 s have passed, and the last reading is returned either way, so the caller's
assertion names what was read when it never settled.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional

from _fission_frames import Sites, wait_frame
from _waits import wait_until

#: The scale at which the declared window frame is the one of `WINDOW_FRAME_BY_DPR[1.0]`: border 8, popup UI 72, default
#: position 4 (device pixels, and CSS pixels at 1.0). The screen is the one the other surface tests pin.
PIN = {"screen.width": 1920, "screen.height": 1080, "screen.dpr": 1.0}

#: A second declared window frame that no runner has: a border of 6, a popup UI of 53 above the content, a default position of 9. The declared frame at scale 1.0
#: (8, 72, 4) is the frame of a retail Windows window, so a Windows runner's own popup has it too (outer - inner = 16 x 80) and a launch with the engine off
#: cannot tell the declaration from the host there; a launch declared with ANOTHER frame can (the prefs are applied last, over the core's declaration).
OTHER_FRAME = {"zoom.stealth.screen.frame_border": 6, "zoom.stealth.screen.popup_ui": 53, "zoom.stealth.screen.popup_position": 9}
OTHER_BORDER, OTHER_POPUP_UI, OTHER_POSITION = 6, 53, 9

READ = """() => ({iw: innerWidth, ih: innerHeight, ow: outerWidth, oh: outerHeight,
                 sx: screenX, sy: screenY, mx: mozInnerScreenX, my: mozInnerScreenY})"""


def in_a_thread_of_its_own(fn: Callable[[], Any], timeout: float = 300) -> Any:
    """Runs `fn` in a thread with no asyncio loop and gives back its result or raises its error.

    For a module whose own fixture keeps a sync client open for the whole module: an open sync client leaves its loop marked as
    running in its thread, and a second sync client started in that thread refuses with "Playwright Sync API inside the asyncio
    loop". The second browser needs a thread of its own.
    """
    box: Dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:      # handed back to the test's thread below
            box["error"] = exc

    worker = threading.Thread(target=run, name="second-sync-client", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise AssertionError("the browser of the test did not end within %d s" % timeout)
    if "error" in box:
        raise box["error"]
    return box.get("value")


def frame_of_the_declaration():
    """The window frame the core declares at scale 1.0: what the engine adds to a popup's inner size."""
    from invisible_core.constants import WINDOW_FRAME_BY_DPR

    return WINDOW_FRAME_BY_DPR[1.0]


def frame_extra(border: int, popup_ui: int):
    """What a popup adds to its inner size: (outer - inner) wide and high, for a border and a popup UI."""
    return (2 * border, popup_ui + border)


def _plausible(reading: Dict[str, Any]) -> bool:
    """Every size of every window read is above 0: the window (and its frame, if any) has been laid out."""
    return all(r["iw"] > 0 and r["ih"] > 0 and r["ow"] > 0 and r["oh"] > 0
               for r in (reading["popup"], reading["frame"]) if r is not None)


def read_popup(binary: str, extra_prefs: Optional[Dict[str, Any]] = None, iframe: bool = False,
               ready: Optional[Callable[[Dict[str, Any]], bool]] = None,
               features: str = "width=500,height=400") -> Dict[str, Any]:
    """Open `window.open(url, 'p', features)` (default `width=500,height=400`) from a click and read it.

    Returns `{"popup": {...}, "frame": {...} or None}` with `iw ih ow oh sx sy mx my` (innerWidth, innerHeight,
    outerWidth, outerHeight, screenX, screenY, mozInnerScreenX, mozInnerScreenY) of the popup and, with `iframe`, of its
    iframe (another site, 200x150). `ready(reading)` says when the reading has settled (default: every size above 0).
    """
    from invisible_playwright import InvisiblePlaywright

    with Sites() as s:
        s.route("A", "/main", "<!doctype html><title>main</title><button id='open' onclick=\"window.open('%s', 'p', "
                "'%s')\">open</button>" % (s.url("A", "/popup"), features))
        frame_html = ("<iframe id='f' src='%s' style='position:absolute;left:0;top:0;width:200px;height:150px;border:0'>"
                      "</iframe>" % s.url("B", "/frame")) if iframe else ""
        s.route("A", "/popup", "<!doctype html><title>popup</title>" + frame_html)
        s.route("B", "/frame", "<!doctype html><title>frame</title>")
        with InvisiblePlaywright(seed=42, pin=PIN, binary_path=binary, headless=True, humanize=False,
                                 extra_prefs=extra_prefs) as browser:
            page = browser.new_page()
            page.goto(s.url("A", "/main"), wait_until="load", timeout=30000)
            with page.expect_popup(timeout=30000) as info:
                page.click("#open", timeout=30000)
            popup = info.value
            popup.wait_for_load_state("load", timeout=30000)
            frame = wait_frame(popup, s.origin["B"], "/frame", seconds=30) if iframe else None
            seen: Dict[str, Any] = {}

            def settled() -> bool:
                seen["now"] = {"popup": popup.evaluate(READ), "frame": frame.evaluate(READ) if frame else None}
                return _plausible(seen["now"]) and (ready is None or bool(ready(seen["now"])))

            wait_until(settled, 30, lambda: popup.wait_for_timeout(100))
            return seen["now"]
