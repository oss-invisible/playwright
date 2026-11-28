"""A real drag from the top document into a frame of another site drops on the element under the cursor, and ``dragend`` says where the drag ended.

The drag-and-drop points cross the process boundary as whole numbers: the parent sends the drop point to the frame's process, and the end point of the drag
to the source's. If either one is off by the offset between the window's real chrome and its declared chrome, the frame's element under the cursor is not the
element that gets the drop, and ``dragend`` carries a point that is not the last one the drag reported. Both are values a page reads. The expected values
do not depend on the window: they are the page's own geometry.

What it does, per display case (the four of the window tests), with the client's own mouse (``page.mouse.move``, ``down``, ``move`` in steps, ``up``): a page of
the top site holds a draggable box at its top left and, lower right, a frame of another site, 400x300 at (200, 100). The frame holds two boxes, 120x120, at (40, 60)
(``target``) and (220, 60) (``other``). The mouse goes down on the box, moves in twelve steps to the centre of ``target`` (300, 220 in the top document) and goes up.

  * the frame hears ONE ``drop``; the element it is dispatched at is ``target``, and it is the element ``document.elementFromPoint`` finds at the ``clientX/Y`` the
    event carries (the element under the cursor, read by the frame itself); and the point it carries is the centre of ``target`` within one CSS pixel (the point is
    in device pixels, which at a fractional scale are not whole CSS pixels);
  * the top document hears ``dragend``; its ``clientX/Y`` equal EXACTLY the ``clientX/Y`` of the last ``drag`` event the same document heard. A drag whose end
    point is moved by the offset reads a ``dragend`` that is not where the drag was last seen. The test is red if they differ by one.

Both are checked with the numbers the page read printed in the message.

The engine that answers these values is not released yet: with the real chrome offset in the points the drop target or ``dragend`` is off, so this test is RED on
the engine of the pin where they differ, on purpose. It enters the suite with the engine release.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest
from _fission_frames import Sites, wait_frame
from _window_origin import CASES, DEADLINE_S, launch_case, wait_for

pytestmark = pytest.mark.e2e

#: The box that is dragged: 80x50 at (20, 20) of the top document; the mouse goes down at its centre.
SOURCE = (60, 45)
#: The frame: 400x300 at (200, 100). The centre of ``target`` in the frame is (100, 120), so (300, 220) in the top document.
FRAME_AT = (200, 100)
TARGET_IN_FRAME = (100, 120)
STEPS = 12

TOP = """<!doctype html><meta charset=utf-8><title>drag</title>
<style>html,body{margin:0;padding:0;height:100%%}
#src{position:absolute;left:20px;top:20px;width:80px;height:50px;background:#9bd}
#f{position:absolute;left:200px;top:100px;width:400px;height:300px;border:0}</style>
<body><div id=src draggable=true>drag</div><iframe id=f src="%s"></iframe><script>
window.__drag = {starts: 0, drags: [], end: null};
const point = e => ({cx: e.clientX, cy: e.clientY, sx: e.screenX, sy: e.screenY});
document.getElementById('src').addEventListener('dragstart', e => {
  window.__drag.starts++;
  e.dataTransfer.setData('text/plain', 'x');
  e.dataTransfer.effectAllowed = 'copy';
});
document.addEventListener('drag', e => { window.__drag.drags.push(point(e)); }, true);
document.addEventListener('dragend', e => { window.__drag.end = point(e); }, true);
</script>"""

FRAME = """<!doctype html><meta charset=utf-8><title>drop</title>
<style>html,body{margin:0;padding:0;height:100%}
.box{position:absolute;top:60px;width:120px;height:120px}
#target{left:40px;background:#bd9}#other{left:220px;background:#db9}</style>
<body><div id=target class=box>target</div><div id=other class=box>other</div><script>
window.__drop = {drops: [], overs: 0};
document.addEventListener('dragover', e => { e.preventDefault(); window.__drop.overs++; }, true);
document.addEventListener('drop', e => {
  e.preventDefault();
  const under = document.elementFromPoint(e.clientX, e.clientY);
  window.__drop.drops.push({at: e.target.id, under: under && under.id, cx: e.clientX, cy: e.clientY});
}, true);
</script>"""


def _drag(page) -> None:
    page.mouse.move(*SOURCE)
    page.mouse.down()
    page.mouse.move(FRAME_AT[0] + TARGET_IN_FRAME[0], FRAME_AT[1] + TARGET_IN_FRAME[1], steps=STEPS)
    page.mouse.up()


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_drag_from_the_top_into_a_frame_of_another_site(firefox_binary, case):
    with Sites() as sites:
        sites.route("A", "/page", lambda handler: TOP % sites.url("B", "/drop"))
        sites.route("B", "/drop", FRAME)
        with launch_case(firefox_binary, case) as browser:
            page = browser.new_page()
            page.goto(sites.url("A", "/page"), wait_until="load", timeout=30000)
            frame = wait_frame(page, sites.origin["B"], "/drop", seconds=DEADLINE_S)
            wait_for(page, lambda: frame.evaluate("() => document.readyState") == "complete")
            origin = page.evaluate("() => { const r = document.getElementById('f').getBoundingClientRect(); return [r.left, r.top]; }")
            assert tuple(origin) == FRAME_AT, "%s: the frame is at %r in the top document, the test placed it at %r" % (case, origin, FRAME_AT)

            _drag(page)

            wait_for(page, lambda: page.evaluate("() => window.__drag.end") is not None and len(frame.evaluate("() => window.__drop.drops")) >= 1)
            drag: Dict[str, Any] = page.evaluate("() => window.__drag")
            drops: List[Dict[str, Any]] = frame.evaluate("() => window.__drop.drops")
            overs = frame.evaluate("() => window.__drop.overs")

            assert drag["starts"] == 1, "%s: the drag started %r times (dragstart), once is a drag (%r)" % (case, drag["starts"], drag)
            assert len(drops) == 1, "%s: the frame heard %d drops (%d dragovers), one is a drop (%r)" % (case, len(drops), overs, drops)
            drop = drops[0]
            assert drop["under"] == drop["at"] == "target", (
                "%s: the drop was dispatched at %r, the element under the cursor (elementFromPoint of the event's clientX/Y) is %r, and the cursor is over 'target' "
                "(the drop carries client point (%r, %r), the centre of target is %r)" % (case, drop["at"], drop["under"], drop["cx"], drop["cy"], TARGET_IN_FRAME))
            assert abs(drop["cx"] - TARGET_IN_FRAME[0]) <= 1 and abs(drop["cy"] - TARGET_IN_FRAME[1]) <= 1, (
                "%s: the drop carries client point (%r, %r) in the frame, the cursor was released over %r" % (case, drop["cx"], drop["cy"], TARGET_IN_FRAME))

            assert drag["drags"], "%s: the source heard no drag event before dragend (%r)" % (case, drag)
            last, end = drag["drags"][-1], drag["end"]
            assert (end["cx"], end["cy"]) == (last["cx"], last["cy"]), (
                "%s: dragend carries client point (%r, %r), the last drag event carried (%r, %r), they must be equal exactly (%d drag events; dragend %r; last %r)"
                % (case, end["cx"], end["cy"], last["cx"], last["cy"], len(drag["drags"]), end, last))
