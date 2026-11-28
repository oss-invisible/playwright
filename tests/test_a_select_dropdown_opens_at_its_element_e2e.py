"""The dropdown of a ``<select>`` opens on the real screen at the element the page showed, in the top document and in a frame of another site.

THIS IS A PRODUCT MEASUREMENT, NOT A PAGE-VISIBLE ONE. A page cannot read where a dropdown opens. But the engine declares to the content process the window origin and the
chrome offset the page reads (not the host's), and the chrome actors of the browser that anchor a popup on screen (the select dropdown, the autocomplete list, the pickers,
the form validation bubble) compute their anchor in the content process with ``windowUtils.getElementBoundingScreenRect``: the declared window origin plus the box of the
element. The parent then opens the popup at those numbers, on the REAL screen. Unless the parent maps the declared point back to the real one, the dropdown opens away
from its element by the difference between the declared window and the real one, and a person who uses the browser sees it (and, in a frame of another process, by the
declared chrome offset too: the matrix of that process).

What it does, per display case (the four of the window tests): the browser is launched with Marionette (``-marionette -remote-allow-system-access``, the route of
``test_software_compositing_is_declared_e2e.py``), so the test can read the chrome of the browser. A page of the top site has a ``<select>`` (160x28 at (30, 20)) and a frame
of another site, 420x200 at (30, 120), with the same ``<select>`` at (40, 20) of its document. For each of the two selects:

  * the client's own click (``locator.click``, the mouse the client sends) opens the dropdown, and the test waits for the menulist popup of ``ContentSelectDropdown`` to be ``open``;
  * from chrome, two rectangles are read, in the CSS pixels of the chrome: the popup's real screen rectangle (``popup.getOuterScreenRect()``, the bounds of the popup's
    own widget) and the select's REAL screen rectangle, computed in the parent from the real geometry of the browser: the content-area rectangle of the ``<browser>`` plus
    the select's rectangle in the page (the page's own ``getBoundingClientRect``, plus the frame's box for the second one), converted with the chrome window's
    ``windowUtils.toScreenRectInCSSUnits`` (the widget of the window as it really is);
  * the popup is anchored below the element: its left is the element's left and its top is the element's bottom, each within ONE device pixel (``1 / devicePixelRatio`` CSS
    pixels of the chrome window), and the frame's popup is anchored as the top one is (offset to the element within one device pixel of the top's offset), so a CSS margin of the
    popup, if the build has one, is not mistaken for a displacement.

RED WITHOUT THE MAP OF THE POPUPS (design §3). Once the content process is told the declared window origin and the declared chrome offset, ``getElementBoundingScreenRect``
answers declared screen coordinates, and ``SelectParent`` opens the popup at them. The real window is not at the declared place (on Windows the real chrome has other
heights; on Linux the window manager's frame and the host's chrome are not retail's), so the popup lands at the real screen rectangle shifted by (declared - real) and its left
or top is more than a device pixel from the element's. With the map, ``p_real = p_decl - (W_decl - W_real) - d`` in the parent, the two rectangles agree. This test is what shows
the shift in the top document (the window origin) and in the frame (the window origin and the matrix).

The autocomplete popup is not measured: it needs a form-history entry written to the profile from chrome and a typed field, and nothing in this suite says that the engine
keeps form history on; a test that skips there is red in the verdict.

The engine that answers these values is not released yet: this test is red on an engine that declares the window origin without the map, on purpose. It enters the suite with it.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Tuple

import pytest
from _fission_frames import Sites, wait_frame
from _window_origin import CASES, DEADLINE_S, launch_case, wait_for

pytestmark = pytest.mark.e2e

MARIONETTE_ARGS = ["-marionette", "-remote-allow-system-access"]

TOP = """<!doctype html><meta charset=utf-8><title>select</title>
<style>html,body{margin:0;padding:0}
select{position:absolute;box-sizing:border-box;width:160px;height:28px;margin:0}
#s{left:30px;top:20px}#f{position:absolute;left:30px;top:120px;width:420px;height:200px;border:0}</style>
<body><select id=s><option>one</option><option>two</option><option>three</option></select><iframe id=f src="%s"></iframe>"""

FRAME = """<!doctype html><meta charset=utf-8><title>select in a frame</title>
<style>html,body{margin:0;padding:0}select{position:absolute;box-sizing:border-box;width:160px;height:28px;margin:0;left:40px;top:20px}</style>
<body><select id=s><option>one</option><option>two</option><option>three</option></select>"""

#: The select's rectangle in the page's own viewport, CSS pixels.
SELECT_BOX = "() => { const r = document.getElementById('s').getBoundingClientRect(); return {x: r.left, y: r.top, w: r.width, h: r.height}; }"
FRAME_BOX = "() => { const r = document.getElementById('f').getBoundingClientRect(); return {x: r.left, y: r.top}; }"

# The reads of the browser chrome, in the script of Marionette (chrome scope).
POPUP_STATE_JS = """
const win = Services.wm.getMostRecentWindow("navigator:browser");
const popup = win.document.querySelector("#ContentSelectDropdown > menupopup");
return JSON.stringify({state: popup ? popup.state : "no popup"});
"""

MEASURE_JS = """
const [rect] = arguments;
const win = Services.wm.getMostRecentWindow("navigator:browser");
const popup = win.document.querySelector("#ContentSelectDropdown > menupopup");
const browser = win.gBrowser.selectedBrowser;
const b = browser.getBoundingClientRect();
const el = win.windowUtils.toScreenRectInCSSUnits(b.left + rect.x, b.top + rect.y, rect.w, rect.h);
const p = popup.getOuterScreenRect();
const plain = r => ({left: r.left, top: r.top, right: r.right, bottom: r.bottom});
return JSON.stringify({state: popup.state, element: plain(el), popup: plain(p), dpr: win.devicePixelRatio});
"""

CLOSE_JS = """
const win = Services.wm.getMostRecentWindow("navigator:browser");
const popup = win.document.querySelector("#ContentSelectDropdown > menupopup");
popup.hidePopup();
return JSON.stringify({state: popup.state});
"""


def _marionette():
    import webfont_probe

    m = webfont_probe._Marionette(webfont_probe.marionette_active_port())
    m.call("WebDriver:NewSession", {"capabilities": {"alwaysMatch": {}}})
    m.call("Marionette:SetContext", {"value": "chrome"})
    return m


def _script(marionette, script: str, args=()) -> Dict[str, Any]:
    return json.loads(marionette.call("WebDriver:ExecuteScript", {"script": script, "args": list(args)})["value"])


def _wait_state(page, marionette, state: str) -> Dict[str, Any]:
    """The popup's state once it is ``state`` (open or closed), polled; the last reading at the deadline."""
    seen: Dict[str, Any] = {}

    def look() -> bool:
        seen.update(_script(marionette, POPUP_STATE_JS))
        return seen["state"] == state

    wait_for(page, look, DEADLINE_S)
    return seen


def _measure(page, marionette, rect: Dict[str, float]) -> Dict[str, Any]:
    """Opens nothing: reads the open popup and the select's real screen rectangle, both in CSS pixels of the chrome."""
    read = _script(marionette, MEASURE_JS, [rect])
    # the popup's widget may still be sizing itself: read until two looks agree
    end = time.monotonic() + DEADLINE_S
    while True:
        page.wait_for_timeout(100)
        again = _script(marionette, MEASURE_JS, [rect])
        if again == read or time.monotonic() >= end:
            return again
        read = again


def _anchor_offset(read: Dict[str, Any]) -> Tuple[float, float]:
    """(popup left - element left, popup top - element bottom), CSS pixels of the chrome."""
    return (read["popup"]["left"] - read["element"]["left"], read["popup"]["top"] - read["element"]["bottom"])


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_select_dropdown_opens_at_its_element(firefox_binary, monkeypatch, case):
    monkeypatch.setenv("MOZ_REMOTE_ALLOW_SYSTEM_ACCESS", "1")      # the chrome-scope reads below
    marionette = None
    reads: Dict[str, Dict[str, Any]] = {}
    with Sites() as sites:
        sites.route("A", "/page", lambda handler: TOP % sites.url("B", "/select"))
        sites.route("B", "/select", FRAME)
        try:
            with launch_case(firefox_binary, case, extra_args=MARIONETTE_ARGS, extra_prefs={"marionette.port": 0}) as browser:
                page = browser.new_page()
                page.goto(sites.url("A", "/page"), wait_until="load", timeout=30000)
                frame = wait_frame(page, sites.origin["B"], "/select", seconds=DEADLINE_S)
                wait_for(page, lambda: frame.evaluate("() => document.readyState") == "complete")
                marionette = _marionette()

                top_rect = page.evaluate(SELECT_BOX)
                frame_origin = page.evaluate(FRAME_BOX)
                in_frame = frame.evaluate(SELECT_BOX)
                frame_rect = {"x": frame_origin["x"] + in_frame["x"], "y": frame_origin["y"] + in_frame["y"], "w": in_frame["w"], "h": in_frame["h"]}

                for name, document, rect in (("top", page, top_rect), ("frame", frame, frame_rect)):
                    document.locator("#s").click()
                    opened = _wait_state(page, marionette, "open")
                    assert opened["state"] == "open", "%s, %s document: the dropdown did not open after a click on the select (popup state %r)" % (case, name, opened)
                    reads[name] = _measure(page, marionette, rect)
                    _script(marionette, CLOSE_JS)
                    closed = _wait_state(page, marionette, "closed")
                    assert closed["state"] == "closed", "%s, %s document: the dropdown did not close (popup state %r)" % (case, name, closed)
        finally:
            if marionette is not None:
                marionette.close()

    for name, read in reads.items():
        tolerance = 1.0 / read["dpr"] + 0.01
        left, top = _anchor_offset(read)
        assert abs(left) <= tolerance and abs(top) <= tolerance, (
            "%s, %s document: the dropdown opened at (left %r, top %r) on the real screen and the select's real rectangle is %r: left is off by %r and top (against the "
            "element's bottom) by %r CSS pixels of the chrome, one device pixel is %r (the popup is %r)"
            % (case, name, read["popup"]["left"], read["popup"]["top"], read["element"], left, top, tolerance, read["popup"]))
    top_offset, frame_offset = _anchor_offset(reads["top"]), _anchor_offset(reads["frame"])
    tolerance = 1.0 / reads["top"]["dpr"] + 0.01
    assert abs(frame_offset[0] - top_offset[0]) <= tolerance and abs(frame_offset[1] - top_offset[1]) <= tolerance, (
        "%s: the dropdown of the select in the frame is anchored %r from its element, the one of the top document %r: a frame of another process is shifted from the top by %r"
        % (case, frame_offset, top_offset, (frame_offset[0] - top_offset[0], frame_offset[1] - top_offset[1])))
