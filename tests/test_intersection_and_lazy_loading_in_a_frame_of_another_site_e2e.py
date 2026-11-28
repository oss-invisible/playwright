"""What a frame of another site sees of the top viewport while the top scrolls: ``IntersectionObserver`` and lazy loading follow the page's own geometry.

A frame of another site runs in another process. It does not know where it is in the window: the parent tells it, as a matrix and as the part of the top viewport
that shows the frame (the remote document's visible rect). ``IntersectionObserver`` of a document in that frame, and the ``loading=lazy`` frames inside it,
read that rect. If the matrix the parent sends is moved from the real chrome offset to the declared one and the rect is not moved with it, the frame sees the
viewport shifted: its observer reports a ratio that is not the visible part of the frame, and a lazy frame loads too early or too late. Both are readable by any
page, and both have a right answer that does not depend on the window: what the top page itself reads of its own frame box.

HOW STOCK FIREFOX IS COMPARED. The wrapper's e2e jobs do not install Playwright's own Firefox: only the rows with ``stock_firefox`` in ``ci/required-suites.json``
(``wrapper.e2e.fakeweb`` and the host oracle) do, and they compare through ``scripts/``, not through a test of ``tests/``. So these tests do NOT run stock; they
compare with the value stock Firefox gives by its algorithm, computed from geometry the page reads (a ratio is the visible height over the frame height;
``rootBounds`` is null for a frame of another site, ``DOMIntersectionObserver.cpp``, ``isSimilarOrigin``; a nested lazy frame in a frame of another site loads when
its box meets the remote visible rect, which is not widened by the lazy margin, ``ComputeTheIntersection`` step 5, while a lazy frame of the top document
loads 600 CSS pixels before the viewport, ``dom.lazy-loading.margin.*``). A stock leg needs a row with ``stock_firefox`` and its legs in the gate.

  1. The intersection of a frame, per display case (the four of the window tests). The top page is tall and holds a frame of another site, 300x300, at document y 1000.
     The frame's page has a box ``#t`` of 300x300. Its script makes a NEW ``IntersectionObserver`` (threshold [0]) at every look, whose first callback always comes,
     and reports ``isIntersecting``, ``intersectionRatio``, ``rootBounds``, ``boundingClientRect`` and ``intersectionRect``. The top scrolls (``scrollTo``) so that the frame's top
     sits at a chosen place in the viewport: 100 (all visible), -50, -150, -250 (the top part clipped), -320 (all above, none visible), the viewport height less 60 (the bottom part
     clipped), the viewport height plus 20 (all below), and 100 again. At each step the page's own ``getBoundingClientRect`` of the frame and ``clientHeight`` give the
     expected visible height; the reading is waited for until it equals that, within one CSS pixel (scroll and box are snapped to device pixels), and the last one is
     asserted: ``isIntersecting`` is whether any height is visible, the ratio is the visible height over 300, ``rootBounds`` is null, the box is 300x300, and the
     intersection rect starts at the clipped height and is as high as the visible part. A frame that thinks the viewport starts a few pixels higher or lower reads another ratio.
  2. Lazy frames, per display case. (a) A ``loading=lazy`` frame of another site in the top document, at document y 4000: it is not requested while its top is 640 CSS
     pixels below the viewport, and is requested when it is 560 below. (b) A ``loading=lazy`` frame of a third site INSIDE a frame of another site, at y 200 of that frame,
     which is itself at document y 4000: it is not requested while its top is 24 CSS pixels below the viewport's bottom edge, and is requested when it is 24 above it.
     The "not requested" side is watched for 1.5 s, the requested side is waited for; the server's own record of requests says what was asked.

The engine that answers these values is not released yet: with a matrix moved and a rect not moved, the ratios and the lazy loads are off, so this test is RED there,
on purpose. It enters the suite with the engine release.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest
from _fission_frames import Sites, wait_frame
from _window_origin import CASES, DEADLINE_S, launch_case, observe, wait_for

pytestmark = pytest.mark.e2e

FRAME_SIZE = 300
#: The frame's top in the top document, in CSS pixels.
IO_TOP = 1000
LAZY_TOP = 4000
#: One CSS pixel in the visible height: scroll and boxes are snapped to device pixels.
TOLERANCE_PX = 1.0
WATCH_S = 1.5

TOP_IO = """<!doctype html><meta charset=utf-8><title>io</title>
<style>html,body{margin:0;padding:0}body{height:4000px}
#f{position:absolute;left:20px;top:%dpx;width:300px;height:300px;border:0}</style>
<body><iframe id=f src="%%s"></iframe>""" % IO_TOP

FRAME_IO = """<!doctype html><meta charset=utf-8><title>io</title>
<style>html,body{margin:0;padding:0}#t{width:300px;height:300px;background:#9bd}</style><body><div id=t></div>"""

#: A new observer at every look: its first callback is always queued, whatever the thresholds, and carries the state now.
LOOK = """() => new Promise(resolve => {
  const io = new IntersectionObserver(entries => {
    const e = entries[entries.length - 1];
    io.disconnect();
    const box = r => r && {x: r.x, y: r.y, w: r.width, h: r.height};
    resolve({isIntersecting: e.isIntersecting, ratio: e.intersectionRatio, rootBounds: box(e.rootBounds),
             box: box(e.boundingClientRect), inter: box(e.intersectionRect)});
  }, {threshold: [0]});
  io.observe(document.getElementById('t'));
})"""

TOP_BOX = """() => { const r = document.getElementById('f').getBoundingClientRect(); return {top: r.top, vh: document.documentElement.clientHeight}; }"""



def _top_lazy(height: int, loading: str, src: str) -> str:
    """The top page of the lazy cases: a tall page with ONE frame of another site at document y ``LAZY_TOP``, ``height`` high; ``loading`` is the attribute's value."""
    return ("<!doctype html><meta charset=utf-8><title>lazy</title>"
            "<style>html,body{margin:0;padding:0}body{height:9000px}"
            "iframe{position:absolute;left:20px;top:%dpx;width:300px;height:%dpx;border:0}</style>"
            '<body><iframe id=f loading=%s src="%s"></iframe>' % (LAZY_TOP, height, loading, src))


FRAME_LAZY_HOST = """<!doctype html><meta charset=utf-8><title>host</title>
<style>html,body{margin:0;padding:0}iframe{position:absolute;left:0;top:200px;width:100px;height:50px;border:0}</style>
<body><iframe loading=lazy src="%s"></iframe>"""

LEAF = "<!doctype html><meta charset=utf-8><title>leaf</title><body>leaf"


def _expected(step_top: float, vh: float) -> Dict[str, float]:
    """What the observer of a 300x300 frame reports when the frame's top is ``step_top`` below the top of a viewport ``vh`` high, from geometry alone."""
    shown_top = max(step_top, 0.0)
    shown_bottom = min(step_top + FRAME_SIZE, vh)
    visible = max(0.0, shown_bottom - shown_top)
    return {"visible": visible, "ratio": visible / FRAME_SIZE, "inter_y": shown_top - step_top}


def _matches(reading: Dict[str, Any], expected: Dict[str, float]) -> bool:
    ratio_tolerance = TOLERANCE_PX / FRAME_SIZE + 0.001
    if abs(reading["ratio"] - expected["ratio"]) > ratio_tolerance:
        return False
    if reading["isIntersecting"] != (expected["visible"] > 0):
        return False
    return expected["visible"] == 0 or (reading["inter"] is not None and abs(reading["inter"]["h"] - expected["visible"]) <= TOLERANCE_PX
                                        and abs(reading["inter"]["y"] - expected["inter_y"]) <= TOLERANCE_PX)


def _steps(vh: float) -> List[Tuple[str, float]]:
    return [("all visible", 100), ("top 50 clipped", -50), ("top 150 clipped", -150), ("top 250 clipped", -250), ("all above", -320),
            ("bottom 240 clipped", vh - 60), ("all below", vh + 20), ("all visible again", 100)]


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_intersection_of_a_frame_of_another_site_follows_the_scroll_of_the_top(firefox_binary, case):
    with Sites() as sites:
        sites.route("A", "/page", lambda handler: TOP_IO % sites.url("B", "/io"))
        sites.route("B", "/io", FRAME_IO)
        with launch_case(firefox_binary, case) as browser:
            page = browser.new_page()
            page.goto(sites.url("A", "/page"), wait_until="load", timeout=30000)
            frame = wait_frame(page, sites.origin["B"], "/io", seconds=DEADLINE_S)
            wait_for(page, lambda: frame.evaluate("() => document.readyState") == "complete")
            vh = page.evaluate(TOP_BOX)["vh"]
            for name, step_top in _steps(vh):
                page.evaluate("(y) => window.scrollTo(0, y)", IO_TOP - step_top)
                seen: Dict[str, Optional[Dict[str, Any]]] = {"reading": None, "box": None}

                def right() -> bool:
                    box = page.evaluate(TOP_BOX)
                    seen["box"] = box
                    seen["reading"] = frame.evaluate(LOOK)
                    return _matches(seen["reading"], _expected(box["top"], box["vh"]))

                wait_for(page, right)
                reading, box = seen["reading"], seen["box"]
                expected = _expected(box["top"], box["vh"])
                where = "%s, step %r (the frame's top is %.2f in a viewport %r high): the frame read %r, from geometry the page expects %r" % (
                    case, name, box["top"], box["vh"], reading, expected)
                assert _matches(reading, expected), where
                assert reading["rootBounds"] is None, "%s; rootBounds of a frame of another site is null in stock Firefox" % where
                assert (reading["box"]["w"], reading["box"]["h"]) == (FRAME_SIZE, FRAME_SIZE), "%s; the box of the target is 300x300 in the frame's own space" % where


def _scroll_so_that_the_frame_top_is(page, frame_top: float, to: float) -> None:
    """Scrolls the top so that an element at document y ``frame_top`` has its top ``to`` CSS pixels below the top of the viewport."""
    page.evaluate("(y) => window.scrollTo(0, y)", frame_top - to)


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_lazy_frame_loads_when_it_meets_the_viewport_it_is_told_about(firefox_binary, case):
    with Sites() as sites:
        sites.route("A", "/lazy-top", lambda handler: _top_lazy(100, "lazy", sites.url("B", "/leaf-top")))
        sites.route("A", "/lazy-nested", lambda handler: _top_lazy(FRAME_SIZE, "eager", sites.url("B", "/host")))
        sites.route("B", "/leaf-top", LEAF)
        sites.route("B", "/host", lambda handler: FRAME_LAZY_HOST % sites.url("C", "/leaf-nested"))
        sites.route("C", "/leaf-nested", LEAF)
        with launch_case(firefox_binary, case) as browser:
            page = browser.new_page()

            # (a) a lazy frame of another site in the top document: 600 CSS pixels before the viewport (dom.lazy-loading.margin.*)
            page.goto(sites.url("A", "/lazy-top"), wait_until="load", timeout=30000)
            vh = page.evaluate(TOP_BOX)["vh"]

            def requested_top() -> int:
                return len(sites.seen("B", "/leaf-top"))

            _scroll_so_that_the_frame_top_is(page, LAZY_TOP, vh + 640)
            assert observe(page, requested_top, WATCH_S) == [0], (
                "%s: a lazy frame of another site whose top is 640 CSS pixels below the viewport (the margin is 600) was requested" % case)
            _scroll_so_that_the_frame_top_is(page, LAZY_TOP, vh + 560)
            assert wait_for(page, lambda: requested_top() >= 1), (
                "%s: a lazy frame of another site whose top is 560 CSS pixels below the viewport (the margin is 600) was not requested in %d s" % (case, DEADLINE_S))

            # (b) a lazy frame inside a frame of another site: its box meets the remote visible rect, no margin
            page.goto(sites.url("A", "/lazy-nested"), wait_until="load", timeout=30000)
            wait_frame(page, sites.origin["B"], "/host", seconds=DEADLINE_S)
            vh = page.evaluate(TOP_BOX)["vh"]

            def requested_nested() -> int:
                return len(sites.seen("C", "/leaf-nested"))

            # the lazy frame is at y 200 of its host: its top is 24 below the bottom edge of the viewport when the host's top is vh - 176
            _scroll_so_that_the_frame_top_is(page, LAZY_TOP, vh - 200 + 24)
            assert observe(page, requested_nested, WATCH_S) == [0], (
                "%s: a lazy frame inside a frame of another site whose top is 24 CSS pixels below the bottom edge of the top viewport was requested" % case)
            _scroll_so_that_the_frame_top_is(page, LAZY_TOP, vh - 200 - 24)
            assert wait_for(page, lambda: requested_nested() >= 1), (
                "%s: a lazy frame inside a frame of another site whose top is 24 CSS pixels above the bottom edge of the top viewport was not requested in %d s"
                % (case, DEADLINE_S))
