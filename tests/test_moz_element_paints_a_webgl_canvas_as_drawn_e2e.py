"""``-moz-element()`` paints a WebGL canvas as it was drawn, not as a page reads it (e2e, every OS; written on firefox-68, which the engine of the series must keep).

The engine's canvas noise belongs to a PAGE's reads of a WebGL canvas (``readPixels``, ``toDataURL``, ``drawImage``...). The browser's own readers
that SHOW a canvas must not carry it: ``-moz-element`` (``nsLayoutUtils::SurfaceFromElement`` and ``nsImageRenderer::PrepareImage``), the drag image
and the taskbar preview read the snapshot AS DRAWN (``HTMLCanvasElement::GetSurfaceSnapshotAsDrawn``, flag ``kStealthSfeAsDrawn``;
``fingerprint/canvas/as-drawn``). A real Firefox paints the canvas the page drew, and a screenshot of the page is the page's own pixels. If the
painter read the noised snapshot, a page could draw a canvas, paint it with ``-moz-element`` and see through a screenshot that the two differ. The
drag image and the taskbar preview are not visible to a page: no test, the build and the hook list cover them.

What the page does: a WebGL2 canvas drawn with the scene of ``tests/_canvas_readers.py`` (ten triangles with random vertex colours, which the
noise touches; a flat clear it would not), and a ``div`` of the same size with ``background: -moz-element(#c)``. The test takes a screenshot at a
device pixel ratio of 1, cuts the div's box out of it, and compares it, pixel for pixel, with

* the canvas read by the page's own ``readPixels`` with the noise ON: they must differ (the page's road is noised; this is the control that
  fails on an engine without the noise, since a flat canvas would pass it without any);
* the canvas read by ``readPixels`` with the canvas noise declared off (``zoom.stealth.canvas.substitute_pixels`` false, a second launch of the
  same page, every other declaration the same): they must be equal, and the div's box of that launch must be the same too.

The noise seed (12345) is one no runner has.
Run: pytest tests/test_moz_element_paints_a_webgl_canvas_as_drawn_e2e.py -m e2e -v
"""
from __future__ import annotations

import pytest

import _canvas_readers as cr
import _page_bytes as pb
from _waits import wait_until

pytestmark = pytest.mark.e2e

#: A device pixel ratio of 1 and a window that holds the canvas and the div whole, at integer positions.
PIN = {"screen.dpr": 1.0}
VIEWPORT = {"width": 300, "height": 300}

PAGE_JS = r"""
async function paint() {
  const c = document.getElementById('c');
  const gl = drawGL(c, 'webgl2');
  // one frame after the drawing, so that what the painters hold is what was drawn
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  const box = id => { const r = document.getElementById(id).getBoundingClientRect();
                      return {x: r.x, y: r.y, width: r.width, height: r.height, dpr: window.devicePixelRatio}; };
  return {direct: b64(readGL(gl)), div: box('d'), canvas: box('c')};
}
"""

HTML = cr.page_html(PAGE_JS).replace(
    "<body>",
    '<style>html, body { margin: 0; background: #fff; }'
    ' #c { position: absolute; left: 10px; top: 10px; width: 128px; height: 128px; }'
    ' #d { position: absolute; left: 10px; top: 150px; width: 128px; height: 128px; background: -moz-element(#c) no-repeat 0 0; }</style>'
    '<body><canvas id="c" width="128" height="128"></canvas><div id="d"></div>')


def _div_pixels(shot, box):
    """The RGBA bytes of the div's box in a screenshot (the box must be on whole device pixels)."""
    for key in ("x", "y", "width", "height"):
        assert box[key] == int(box[key]), "the div is not on whole device pixels (%s = %r)" % (key, box[key])
    assert box["dpr"] == 1, "the device pixel ratio is %r, not 1: the box would be resampled" % box["dpr"]
    whole = pb.unfiltered(shot)
    cut = pb.crop(whole, int(box["x"]), int(box["y"]), int(box["width"]), int(box["height"]))
    assert (cut["w"], cut["h"]) == (cr.SIZE, cr.SIZE), cut
    return cr.rgba_of_image(cut)


def _paint_and_shoot(firefox_binary, noised):
    """(what the page reads with readPixels, the div's box of a screenshot taken once the painter has drawn the canvas)."""
    with cr.launched(firefox_binary, HTML, noised=noised, pin=PIN, viewport=VIEWPORT) as page:
        measured = cr.run(page, "paint()")
        direct = cr.rgba("direct", measured["direct"])
        seen = {}

        def painted():
            seen["pixels"] = _div_pixels(page.screenshot(type="png"), measured["div"])
            # a div that has not painted the canvas yet is one flat colour: the scene has hundreds
            return cr.distinct_colours(seen["pixels"]) > 100

        assert wait_until(painted, 120, lambda: page.wait_for_timeout(200)), (
            "-moz-element(#c) never painted the canvas into the div: its box holds %d colours" % cr.distinct_colours(seen["pixels"]))
        return direct, seen["pixels"]


@pytest.mark.timeout(300, method="thread")
def test_moz_element_paints_a_webgl_canvas_as_drawn(firefox_binary):
    page_on, shot_on = _paint_and_shoot(firefox_binary, True)
    page_off, shot_off = _paint_and_shoot(firefox_binary, False)

    # the control: the page's own road is noised with the engine on (and only that), so the engine is active in this launch
    n = cr.differing_pixels(page_on, page_off)
    assert n > 0, (
        "readPixels reads the canvas byte for byte the same with the canvas noise declared on and off: the page's road is not noised, so "
        "this test would pass on an engine that applies no noise to the painter either")
    # what the painter shows is what was drawn: not the page's noised read, and exactly the read of the canvas with no noise
    assert shot_on != page_on, (
        "the div painted by -moz-element holds exactly the pixels the page reads with readPixels (noised): the painter reads the page's "
        "snapshot, not the canvas as drawn")
    assert shot_on == page_off, (
        "the div painted by -moz-element is not the canvas as it was drawn: %d pixels differ from the canvas read with the noise declared off"
        % cr.differing_pixels(shot_on, page_off))
    assert shot_on == shot_off, (
        "the screenshot of the painted div differs between noise on and off (%d pixels): the browser's own readers carry the noise"
        % cr.differing_pixels(shot_on, shot_off))
