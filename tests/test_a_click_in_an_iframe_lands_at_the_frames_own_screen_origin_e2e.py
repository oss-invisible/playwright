"""A real click inside an iframe: `screenX - clientX` is the iframe's own `mozInnerScreenX`.

What it fixes: the relation a page checks with one subtraction on every mouse event, `event.screenX - event.clientX ==
window.mozInnerScreenX` (and the same on Y), INSIDE an iframe placed at (220, 150) of the page. The defect of 2026-08-10:
`mozInnerScreenX/Y` answered the origin of the top-level content to every window, so the iframe read (top origin) while
its events still carried the iframe's offset, and `screenX - clientX` was 220 against a `mozInnerScreenX` of 0.

Engine hook: `fingerprint/screen-window/geometry` of the engine series: `nsGlobalWindowOuter::GetMozInnerScreenX/Y` and
`Event::GetScreenCoords` both add the ONE content origin shift (`nsGlobalWindowOuter::StealthContentOriginShift`, declared
window against real one) to the window's OWN real rectangle, so the offset between frames stays true at every depth.

Control, in the same test: the top window's `mozInnerScreenX/Y` is a different number (the iframe sits 220 and 150 CSS
pixels off it). The test asserts the iframe's `mozInnerScreenX/Y` is the top's plus the iframe's rectangle, and that what
the events carry (`screenX - clientX`) is NOT the top's origin: a window that answered the top origin to every frame fails
both.

The click is a real one (`page.mouse.click` at a point inside the iframe, the pointer events the engine makes), not a
dispatched event. The page and the iframe are one site, so the page may read the iframe's `mozInnerScreenX`.
"""
from __future__ import annotations

import pytest
from _fission_frames import Sites
from _waits import wait_until

pytestmark = pytest.mark.e2e

LEFT, TOP = 220, 150

PARENT = """<!doctype html><meta charset=utf-8><title>parent</title>
<style>html,body{margin:0;padding:0;height:100%%}
#f{position:absolute;left:%dpx;top:%dpx;width:360px;height:220px;border:0}</style>
<body><iframe id=f src="/child"></iframe>
<script>
window.__geom = () => {
  const w = document.getElementById('f').contentWindow;
  const r = document.getElementById('f').getBoundingClientRect();
  return {top_x: window.mozInnerScreenX, top_y: window.mozInnerScreenY,
          ifr_x: w.mozInnerScreenX, ifr_y: w.mozInnerScreenY, rect_x: r.x, rect_y: r.y};
};
</script>""" % (LEFT, TOP)

CHILD = """<!doctype html><meta charset=utf-8><title>child</title>
<style>html,body{margin:0;padding:0;height:100%}
#g{position:absolute;left:30px;top:40px;width:220px;height:130px;background:#06c}</style>
<body><div id=g>y</div>
<script>
window.__ev = [];
for (const t of ['mousemove', 'mousedown', 'mouseup', 'click']) {
  window.addEventListener(t, e => {
    window.__ev.push({t: t, cx: e.clientX, cy: e.clientY, sx: e.screenX, sy: e.screenY});
  }, true);
}
window.__inner = () => [window.mozInnerScreenX, window.mozInnerScreenY];
</script>"""


def test_a_real_click_in_an_iframe_has_the_screen_origin_of_the_iframe(firefox_binary):
    from invisible_playwright import InvisiblePlaywright

    with Sites() as s:
        s.route("A", "/parent", PARENT)
        s.route("A", "/child", CHILD)
        with InvisiblePlaywright(seed=42, binary_path=firefox_binary, headless=True, humanize=False) as browser:
            page = browser.new_page()
            page.goto(s.url("A", "/parent"), wait_until="load", timeout=30000)
            geom = page.evaluate("() => window.__geom()")
            frame = page.frames[1]
            frame.evaluate("() => { window.__ev = []; }")
            # a point inside the blue square of the iframe, in the coordinates of the page
            page.mouse.click(LEFT + 30 + 100, TOP + 40 + 60)
            kinds = {"mousedown", "mouseup", "click"}
            assert wait_until(lambda: kinds <= {e["t"] for e in frame.evaluate("() => window.__ev")}, 30,
                              lambda: page.wait_for_timeout(100)), (
                "the iframe heard %r of the click" % [e["t"] for e in frame.evaluate("() => window.__ev")])
            events = frame.evaluate("() => window.__ev")
            inner = frame.evaluate("() => window.__inner()")

    # the iframe is where it was put, and answers its OWN origin: the top's plus the iframe's rectangle
    assert (geom["rect_x"], geom["rect_y"]) == (LEFT, TOP)
    assert (geom["ifr_x"], geom["ifr_y"]) == (geom["top_x"] + geom["rect_x"], geom["top_y"] + geom["rect_y"]), (
        "the iframe reads mozInnerScreen (%r, %r), the page's is (%r, %r) and the iframe sits at (%d, %d)"
        % (geom["ifr_x"], geom["ifr_y"], geom["top_x"], geom["top_y"], LEFT, TOP))
    assert (inner[0], inner[1]) != (geom["top_x"], geom["top_y"]), "the iframe answers the origin of the top-level content"
    clicks = [e for e in events if e["t"] == "click"]
    assert clicks, "no click event in the iframe: %r" % events
    for e in events:
        assert e["sx"] - e["cx"] == inner[0], (
            "%s in the iframe: screenX - clientX = %r against mozInnerScreenX = %r" % (e["t"], e["sx"] - e["cx"], inner[0]))
        assert e["sy"] - e["cy"] == inner[1], (
            "%s in the iframe: screenY - clientY = %r against mozInnerScreenY = %r" % (e["t"], e["sy"] - e["cy"], inner[1]))
        # the control: the top window's origin is NOT what the events carry
        assert (e["sx"] - e["cx"], e["sy"] - e["cy"]) != (geom["top_x"], geom["top_y"])
