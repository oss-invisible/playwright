"""Click, hover, drag and wheel reach the element under the cursor, in the top document and in a frame of another site, at every display scale.

The parent turns a point of the window into a point of the content with the matrix it keeps of each content process (``mChildToParentConversionMatrix``), and the
mouse, the wheel and the drag go through it. The matrix the parent SENDS to the child is a different one (it carries the window origin the page reads), and a change
there must not move the input: the point the parent hit-tests, the element the event is dispatched at and the ``clientX/Y`` it carries must stay the cursor's.
Playwright's own input (``page.mouse``) is what a client sends, so this is what a script sees break.

What it does, per display case (the four of the window tests), once for the top document and once for a frame of another site, 500x240 at (20, 200) of the same
page, both holding the same four boxes (CSS pixels of the document): a button ``click`` (60x40 at (20, 20)), a box ``hover`` (60x40 at (100, 20)), a box ``drag``
(140x90 at (180, 20)) and a scroller ``scroll`` (140x100 at (20, 80), 600 high inside). The mouse is the client's (``page.mouse``), the point is the box's own
centre plus the document's origin in the page:

  * click: ``mouse.click`` at the centre of ``click``: the document hears trusted ``mousedown``, ``mouseup`` and ``click`` AT ``click``, with ``clientX/Y`` at the point
    within one CSS pixel (the point is a device pixel, which at a fractional scale is not a whole CSS pixel);
  * hover: ``mouse.move`` to the centre of ``hover``: ``mouseover`` is dispatched at ``hover``, with the point, and the element matches ``:hover``;
  * drag: ``mousedown`` at the centre of ``drag``, six moves to a point 30 right and 20 below, ``mouseup``: ``mousedown`` and ``mouseup`` AT ``drag`` with the two points,
    the ``mousemove`` events with the left button held and the last at the end point;
  * wheel: ``mouse.wheel(0, 120)`` over the centre of ``scroll``: the document hears a trusted ``wheel`` at ``scroll``, with the point, and ``scroll.scrollTop`` grows.

The existing suites that read input in a frame of another site and are in the candidate's rows already: ``test_nested_frame_actions.py`` (a click, and a pointer at a large
offset, with the scales 1, 1.25, 1.5 and 2, in one-, two- and three-process pages, and the wheel at scales 1 and 1.25 under a zoom),
``test_a_click_in_an_iframe_lands_at_the_frames_own_screen_origin_e2e.py`` (one click), ``test_fission_frames_tree_input_and_pixels_e2e.py`` (clicks under a transform, after a
scroll, typing), ``test_a_drag_keeps_its_buttons_pressed_e2e.py`` and ``test_drag_motion.py`` (a drag in the top document), ``test_input_is_born_before_the_page_sees_it_e2e.py``
(keys and moves). None puts the four kinds of input in both documents at the four scales: this file does, and only this.

The engine that answers these values is not released yet: it must pass on the engine of the pin, where the matrix is the real one; it is red if the matrix the
parent sends moves the input.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest
from _fission_frames import Sites, wait_frame
from _window_origin import CASES, DEADLINE_S, launch_case, wait_for

pytestmark = pytest.mark.e2e

#: The boxes' centres in the document, CSS pixels.
CLICK, HOVER, DRAG, SCROLL = (50, 40), (130, 40), (250, 65), (90, 130)
DRAG_TO = (280, 85)
#: The frame's place in the top document.
FRAME_AT = (20, 200)
#: One CSS pixel: the point of an event is a device pixel, rounded to CSS.
POINT_TOLERANCE = 1.0

BODY = """<style>html,body{margin:0;padding:0}.b{position:absolute;box-sizing:border-box;margin:0;padding:0}
#click{left:20px;top:20px;width:60px;height:40px}#hover{left:100px;top:20px;width:60px;height:40px;background:#eee}
#drag{left:180px;top:20px;width:140px;height:90px;background:#9bd}
#scroll{left:20px;top:80px;width:140px;height:100px;overflow:auto;background:#bd9}#scroll div{height:600px}</style>
<button id=click class=b>click</button><div id=hover class=b>hover</div><div id=drag class=b>drag</div><div id=scroll class=b><div></div></div>
<script>
window.__ev = [];
const idOf = e => { const n = e.target.closest ? e.target.closest('[id]') : null; return n ? n.id : null; };
for (const t of ['click', 'mouseover', 'mousedown', 'mousemove', 'mouseup', 'wheel'])
  document.addEventListener(t, e => { window.__ev.push({t: t, id: idOf(e), cx: e.clientX, cy: e.clientY, trusted: e.isTrusted, buttons: e.buttons}); }, true);
</script>"""

TOP = ("<!doctype html><meta charset=utf-8><title>input</title><style>#f{position:absolute;left:20px;top:200px;width:500px;height:240px;border:0}</style>"
       + BODY + '<iframe id=f src="%s"></iframe>')
FRAME = "<!doctype html><meta charset=utf-8><title>input frame</title>" + BODY


def _events(doc) -> List[Dict[str, Any]]:
    return doc.evaluate("() => window.__ev")


def _where(case: str, doc_name: str, what: str, events: List[Dict[str, Any]]) -> str:
    return "%s, %s document, %s: events heard %r" % (case, doc_name, what, events)


def _near(event: Dict[str, Any], point: Tuple[float, float]) -> bool:
    return abs(event["cx"] - point[0]) <= POINT_TOLERANCE and abs(event["cy"] - point[1]) <= POINT_TOLERANCE


def _of(events: List[Dict[str, Any]], kind: str, box: str) -> List[Dict[str, Any]]:
    return [e for e in events if e["t"] == kind and e["id"] == box]


def _fresh(doc) -> None:
    doc.evaluate("() => { window.__ev.length = 0; }")


def _check_click(page, doc, origin, case, name) -> None:
    _fresh(doc)
    page.mouse.click(origin[0] + CLICK[0], origin[1] + CLICK[1])
    wait_for(page, lambda: _of(_events(doc), "click", "click"))
    events = _events(doc)
    where = _where(case, name, "click at %r" % (CLICK,), events)
    for kind in ("mousedown", "mouseup", "click"):
        heard = _of(events, kind, "click")
        assert heard, "%s; no trusted %s at 'click'" % (where, kind)
        assert heard[-1]["trusted"] and _near(heard[-1], CLICK), "%s; %s is not the trusted event at the point" % (where, kind)


def _check_hover(page, doc, origin, case, name) -> None:
    _fresh(doc)
    page.mouse.move(origin[0] + HOVER[0], origin[1] + HOVER[1])
    wait_for(page, lambda: _of(_events(doc), "mouseover", "hover"))
    where = _where(case, name, "hover at %r" % (HOVER,), _events(doc))
    over = _of(_events(doc), "mouseover", "hover")
    assert over and over[-1]["trusted"], "%s; no trusted mouseover at 'hover'" % where
    assert wait_for(page, lambda: doc.evaluate("() => document.getElementById('hover').matches(':hover')")), "%s; 'hover' does not match :hover" % where
    moves = [e for e in _events(doc) if e["t"] == "mousemove"]
    assert moves and _near(moves[-1], HOVER), "%s; the last mousemove is not at the point" % where


def _check_drag(page, doc, origin, case, name) -> None:
    _fresh(doc)
    page.mouse.move(origin[0] + DRAG[0], origin[1] + DRAG[1])
    page.mouse.down()
    page.mouse.move(origin[0] + DRAG_TO[0], origin[1] + DRAG_TO[1], steps=6)
    page.mouse.up()
    wait_for(page, lambda: _of(_events(doc), "mouseup", "drag"))
    events = _events(doc)
    where = _where(case, name, "drag from %r to %r" % (DRAG, DRAG_TO), events)
    down, up = _of(events, "mousedown", "drag"), _of(events, "mouseup", "drag")
    assert down and _near(down[-1], DRAG), "%s; mousedown at 'drag' is not at the start point" % where
    assert up and _near(up[-1], DRAG_TO), "%s; mouseup at 'drag' is not at the end point" % where
    held = [e for e in events if e["t"] == "mousemove" and e["buttons"] == 1]
    assert held and _near(held[-1], DRAG_TO), "%s; the last mousemove with the left button held is not at the end point" % where


def _check_wheel(page, doc, origin, case, name) -> None:
    _fresh(doc)
    page.mouse.move(origin[0] + SCROLL[0], origin[1] + SCROLL[1])
    page.mouse.wheel(0, 120)
    wait_for(page, lambda: doc.evaluate("() => document.getElementById('scroll').scrollTop") > 0 and _of(_events(doc), "wheel", "scroll"))
    events = _events(doc)
    where = _where(case, name, "wheel over %r" % (SCROLL,), events)
    wheel = _of(events, "wheel", "scroll")
    assert wheel and wheel[-1]["trusted"] and _near(wheel[-1], SCROLL), "%s; no trusted wheel at 'scroll' at the point" % where
    assert doc.evaluate("() => document.getElementById('scroll').scrollTop") > 0, "%s; the scroller did not scroll" % where


@pytest.mark.parametrize("case", sorted(CASES))
def test_input_reaches_the_top_and_a_frame_of_another_site(firefox_binary, case):
    with Sites() as sites:
        sites.route("A", "/page", lambda handler: TOP % sites.url("B", "/input"))
        sites.route("B", "/input", FRAME)
        with launch_case(firefox_binary, case) as browser:
            page = browser.new_page()
            page.goto(sites.url("A", "/page"), wait_until="load", timeout=30000)
            frame = wait_frame(page, sites.origin["B"], "/input", seconds=DEADLINE_S)
            wait_for(page, lambda: frame.evaluate("() => document.readyState") == "complete")
            box = page.evaluate("() => { const r = document.getElementById('f').getBoundingClientRect(); return [r.left, r.top]; }")
            assert tuple(box) == FRAME_AT, "%s: the frame is at %r in the top document, the test placed it at %r" % (case, box, FRAME_AT)
            for name, doc, origin in (("top", page, (0, 0)), ("frame", frame, FRAME_AT)):
                _check_click(page, doc, origin, case, name)
                _check_hover(page, doc, origin, case, name)
                _check_drag(page, doc, origin, case, name)
                _check_wheel(page, doc, origin, case, name)
