"""`page.set_viewport_size` leaves a restored window with the frame of retail Windows Firefox 159.0: what a page reads of it, step by step.

What it checks: the numbers a page reads (``outerWidth``, ``outerHeight``, ``innerWidth``, ``innerHeight``, ``screenX``, ``screenY``,
``mozInnerScreenX/Y``) and the offset the mouse events carry (``screenX - clientX``, ``screenY - clientY``) in four steps, against retail Firefox 159.0 on
Windows 11 measured live (GitHub runs oss-invisible/core 45679625406 and 45695770162; RetailCaptures ``notes/window-geometry.md``):

  1. the window as it opens, maximized;
  2. after ``set_viewport_size(w1, h1)``: the window is restored, so it sits at a small positive position (the frame's default, 4 at scale 1.0) and its content
     origin is that position plus the frame: ``outer`` is the requested inner size plus the frame;
  3. after ``set_viewport_size(w2, h2)``: only the height of the window changes, the position and the content origin stay;
  4. after ``set_viewport_size`` back to the maximized inner size: the window STAYS restored (a page that asks for the size it had does not get the maximized
     window back), so its outer size is the inner size plus the restored frame, not the maximized one.

Three display scales on the same 1920x1080 panel (device pixels): 1.0, 1.25 and 1.5. At 1.5 the browser chrome is the compact bar (``screen.availHeight`` 672 CSS pixels).
A page reads everything in CSS pixels, so the frame in CSS pixels is the frame in device pixels over the scale, rounded the way retail rounds it: the numbers
below are what retail answers, not a formula of this test.

The invariant, in every step: ``event.screenX - event.clientX == mozInnerScreenX`` and ``event.screenY - event.clientY == mozInnerScreenY``, up to the rounding of
the event's integer coordinates (``screenX`` and ``clientX`` of an event are whole numbers, ``mozInnerScreen`` is not at a fractional scale). At scale 1.0 the
frame is whole and the offset is exactly equal.

The engine that answers these values is not released yet: on the engine of the pin this test is RED, on purpose. It enters the suite with the engine release,
and the wrapper pull request that bumps the pin carries it.

Why a page reads it: ``outerWidth - innerWidth``, ``screenX`` and ``mozInnerScreenX/Y`` are one line each, and a window whose frame is not the frame of a Windows
window at that scale (or that goes back to maximized when the page asks for the size it had) is a browser that does not run on Windows. Linux and Windows
run the same assertions: the declaration, not the host, is what is read.

The page is a local one (``Sites``); the mouse is the client's own (``page.mouse.move``), so the events are trusted ones.

The restored state survives the replacement of the top browsing context
(``test_the_restored_state_survives_a_browsing_context_replacement``, 1920x1080 at scale 1.0): after ``set_viewport_size(390, 844)`` the same tab
navigates to a page served with ``Cross-Origin-Opener-Policy: same-origin`` (and ``Cross-Origin-Embedder-Policy: require-corp``) from another site of
the local server, while the first page has none. The opener policy differs, so the parent does not keep the top browsing context: it REPLACES it
(``CanonicalBrowsingContext::ReplacedBy``) with one of a new browsing context group. The window is the same window, so the page must read the same
restored window (inner 390x844, outer 516x945, screen 4,4, mozInnerScreen 12,89, and the events' offset 12,89). Without the engine line that carries the
restored state across the replacement, the new browsing context starts from the maximized window and the page reads the maximized numbers instead
(outer 1936x1048, screen -8,-8, mozInnerScreen 0,85).

The replacement is proved twice, so the test cannot pass by accident: the page reads ``crossOriginIsolated`` False before and True after (the opener
policy took effect: a new browsing context group), and the engine's own Juggler trace holds the ``onNavigationMoved`` line of that navigation, naming the
browsing context the load left (``tests/_navigation_moved_trace.py``): a load that left its browsing context is a load that replaced it.
"""
from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional, Tuple

import pytest
from _evidence_on_failure import FailureEvidence
from _fission_frames import Sites
from _navigation_moved_trace import moved_origin, read_trace
from _waits import wait_until

pytestmark = pytest.mark.e2e

#: The panel is 1920x1080 device pixels at every case; only the scale changes.
PANEL = (1920, 1080)
#: Rounding of the event's integer coordinates against the fractional ``mozInnerScreen`` of a fractional scale: less than one pixel.
EVENT_ROUNDING = 1.0
#: Floats of a page (``mozInnerScreenY`` 72.667 is 72.6666...).
FLOAT = 0.001
DEADLINE_S = 30

#: Points the mouse moves to, in CSS pixels of the content; a move to the point the pointer is on makes no event, so each step uses another one.
POINTS: List[Tuple[int, int]] = [(60, 70), (120, 150), (80, 110), (150, 170)]

PAGE = """<!doctype html><meta charset=utf-8><title>restored window</title>
<style>html,body{margin:0;padding:0;height:100%}</style>
<body><script>
window.__last = null; window.__moves = 0;
window.addEventListener('mousemove', e => {
  if (!e.isTrusted) return;
  window.__moves++;
  window.__last = {n: window.__moves, cx: e.clientX, cy: e.clientY, sx: e.screenX, sy: e.screenY,
                   dx: e.screenX - e.clientX, dy: e.screenY - e.clientY};
}, true);
</script>"""

READ = """() => ({ow: outerWidth, oh: outerHeight, iw: innerWidth, ih: innerHeight, sx: screenX, sy: screenY,
                 mx: mozInnerScreenX, my: mozInnerScreenY, dpr: devicePixelRatio, availH: screen.availHeight})"""

# What retail answers, per case: the maximized reading, then the calls of set_viewport_size in order (the last one goes back to the maximized inner size).
# A key that is not in an entry is a value the measurement did not name: it is not asserted (the invariant of the events is, always).
CASES: Dict[str, Dict[str, Any]] = {
    "1920x1080-dpr1.0": {
        "dpr": 1.0,
        "maximized": {"ow": 1936, "oh": 1048, "iw": 1920, "ih": 947, "sx": -8, "sy": -8, "mx": 0, "my": 85},
        "calls": [
            {"size": (390, 844), "ow": 516, "oh": 945, "sx": 4, "sy": 4, "mx": 12, "my": 89, "dx": 12, "dy": 89},
            {"size": (414, 896), "ow": 516, "oh": 989, "sx": 4, "sy": 4, "mx": 12, "my": 89, "dx": 12, "dy": 89},
            {"size": (1920, 947), "ow": 1936, "oh": 1040, "sx": 4, "sy": 4, "mx": 12, "my": 89, "dx": 12, "dy": 89},
        ],
    },
    "1920x1080-dpr1.25": {
        "dpr": 1.25,
        "maximized": {"ow": 1550, "oh": 830, "iw": 1536, "ih": 731, "sx": -7, "sy": -7, "my": 84.8},
        "calls": [
            {"size": (390, 690), "ow": 514, "oh": 789, "sx": 3, "sy": 3, "mx": 10.4, "my": 88},
            {"size": (414, 640), "ow": 514, "oh": 733, "sx": 3, "sy": 3, "mx": 10.4, "my": 88},
            {"size": (1536, 731), "ow": 1550, "oh": 823, "sx": 3, "sy": 3, "mx": 10.4, "my": 88},
        ],
    },
    # the compact bar: availHeight is 672 CSS pixels
    "1920x1080-dpr1.5": {
        "dpr": 1.5,
        "availH": 672,
        "maximized": {"iw": 1280, "ih": 599, "my": 72.667},
        "calls": [
            {"size": (390, 550), "ow": 515, "oh": 638, "sx": 3, "sy": 3, "mx": 10, "my": 75.333},
            {"size": (414, 500), "ow": 515, "oh": 580, "sx": 3, "sy": 3, "mx": 10, "my": 75.333},
            {"size": (1280, 599), "ow": 1295, "oh": 678, "sx": 3, "sy": 3, "mx": 10, "my": 75.333},
        ],
    },
}


def _stable(page, ask: Optional[Tuple[int, int]]) -> Dict[str, Any]:
    """The window as the page reads it once it has settled: the inner size is the asked one (when one was asked) and two looks in a row are equal.

    A condition, not a pause. The last reading is returned either way, so a window that never settles is red where the assertions name what was read."""
    seen: Dict[str, Any] = {"now": None, "before": None}

    def settled() -> bool:
        seen["before"], seen["now"] = seen["now"], page.evaluate(READ)
        now = seen["now"]
        if ask is not None and (now["iw"], now["ih"]) != ask:
            return False
        return now == seen["before"]

    wait_until(settled, DEADLINE_S, lambda: page.wait_for_timeout(100))
    return seen["now"]


def _event_offset(page, point: Tuple[int, int]) -> Dict[str, Any]:
    """Moves the mouse to ``point`` and gives the last trusted mousemove the page heard for it (waits for it)."""
    heard = page.evaluate("() => window.__moves")
    page.mouse.move(*point)
    wait_until(lambda: page.evaluate("() => window.__moves") > heard and (page.evaluate("() => window.__last") or {}).get("cx") == point[0]
               and (page.evaluate("() => window.__last") or {}).get("cy") == point[1],
               DEADLINE_S, lambda: page.wait_for_timeout(50))
    return page.evaluate("() => window.__last")


def _check_reading(step: str, reading: Dict[str, Any], event: Dict[str, Any], expected: Dict[str, Any], dpr: float) -> None:
    """The window a page reads at one step, against retail's numbers, and the events against ``mozInnerScreen``."""
    assert reading["dpr"] == dpr, "%s: the page reads devicePixelRatio %r, the test declared %r: %r" % (step, reading["dpr"], dpr, reading)
    for key, name in (("iw", "innerWidth"), ("ih", "innerHeight"), ("ow", "outerWidth"), ("oh", "outerHeight"),
                      ("sx", "screenX"), ("sy", "screenY"), ("mx", "mozInnerScreenX"), ("my", "mozInnerScreenY")):
        if key in expected:
            assert reading[key] == pytest.approx(expected[key], abs=FLOAT), (
                "%s: %s reads %r, retail Firefox 159.0 on Windows reads %r (whole reading: %r)" % (step, name, reading[key], expected[key], reading))
    assert event is not None, "%s: the page heard no trusted mousemove" % step
    # the invariant of every step: what the events carry is the content origin the window declares
    assert abs(event["dx"] - reading["mx"]) < EVENT_ROUNDING, (
        "%s: screenX - clientX is %r, mozInnerScreenX is %r (event %r)" % (step, event["dx"], reading["mx"], event))
    assert abs(event["dy"] - reading["my"]) < EVENT_ROUNDING, (
        "%s: screenY - clientY is %r, mozInnerScreenY is %r (event %r)" % (step, event["dy"], reading["my"], event))
    for key, name in (("dx", "screenX - clientX"), ("dy", "screenY - clientY")):
        if key in expected:
            assert event[key] == expected[key], "%s: %s of the mouse event is %r, retail reads %r (event %r)" % (step, name, event[key], expected[key], event)
    # a window whose own reading and whose events say the same thing, in whole numbers where the scale is 1.0
    if dpr == 1.0:
        assert (event["dx"], event["dy"]) == (reading["mx"], reading["my"]), (
            "%s: at scale 1.0 the events carry (%r, %r) and mozInnerScreen is (%r, %r)" % (step, event["dx"], event["dy"], reading["mx"], reading["my"]))


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_restored_window_reads_the_retail_frame_after_set_viewport_size(firefox_binary, case):
    from invisible_playwright import InvisiblePlaywright

    spec = CASES[case]
    dpr = spec["dpr"]
    pin = {"screen.width": PANEL[0], "screen.height": PANEL[1], "screen.dpr": dpr}
    with Sites() as s:
        s.route("A", "/page", PAGE)
        with InvisiblePlaywright(seed=42, pin=pin, binary_path=firefox_binary, headless=True, humanize=False) as browser:
            page = browser.new_page()
            page.goto(s.url("A", "/page"), wait_until="load", timeout=30000)

            # 1. the window as it opens
            maximized = _stable(page, None)
            maximized_event = _event_offset(page, POINTS[0])
            if "availH" in spec:
                assert maximized["availH"] == spec["availH"], "the compact bar: screen.availHeight reads %r, retail %r" % (maximized["availH"], spec["availH"])
            _check_reading("maximized (%s)" % case, maximized, maximized_event, spec["maximized"], dpr)

            # 2 to 4. set_viewport_size, twice and back to the maximized inner size
            for number, call in enumerate(spec["calls"], start=1):
                width, height = call["size"]
                if number == len(spec["calls"]):
                    assert (width, height) == (maximized["iw"], maximized["ih"]), (
                        "the last call goes back to the maximized inner size %rx%r, the test asks %rx%r"
                        % (maximized["iw"], maximized["ih"], width, height))
                page.set_viewport_size({"width": width, "height": height})
                reading = _stable(page, (width, height))
                event = _event_offset(page, POINTS[number])
                _check_reading("set_viewport_size(%d, %d), call %d (%s)" % (width, height, number, case), reading, event, call, dpr)


#: The headers that give the second page its own browsing context group (and make it cross-origin isolated).
ISOLATING = [("Cross-Origin-Opener-Policy", "same-origin"), ("Cross-Origin-Embedder-Policy", "require-corp")]

#: The restored window of call 1 at scale 1.0 (the first case above), and the maximized one the new browsing context falls back to without the line.
RESTORED_390x844 = CASES["1920x1080-dpr1.0"]["calls"][0]


def test_the_restored_state_survives_a_browsing_context_replacement(firefox_binary):
    from invisible_playwright import InvisiblePlaywright

    pin = {"screen.width": PANEL[0], "screen.height": PANEL[1], "screen.dpr": 1.0}
    width, height = RESTORED_390x844["size"]
    held = FailureEvidence("a-restored-window-survives-a-browsing-context-replacement")
    held.arm()      # the engine's trace, before the browser is launched (it reads the variable at launch)
    try:
        with Sites() as s:
            s.route("A", "/first", PAGE)
            s.route("B", "/second", PAGE, headers=ISOLATING)
            first, second = s.url("A", "/first"), s.url("B", "/second")
            with InvisiblePlaywright(seed=42, pin=pin, binary_path=firefox_binary, headless=True, humanize=False) as browser:
                page = browser.new_page()
                page.goto(first, wait_until="load", timeout=30000)
                assert page.evaluate("self.crossOriginIsolated") is False, "the first page has no opener policy and reads isolated"

                # 1. the restored window of call 1
                page.set_viewport_size({"width": width, "height": height})
                reading = _stable(page, (width, height))
                event = _event_offset(page, POINTS[0])
                _check_reading("set_viewport_size(%d, %d) on the first page" % (width, height), reading, event, RESTORED_390x844, 1.0)

                # 2. the same tab goes to a page of another site with an opener policy: the top browsing context is replaced
                page.goto(second, wait_until="load", timeout=30000)
                # 3. the proof that it was: the page is isolated (a new browsing context group), and the engine told that the load left its browsing context
                assert wait_until(lambda: page.evaluate("self.crossOriginIsolated") is True, DEADLINE_S, lambda: page.wait_for_timeout(100)), (
                    "the second page is not cross-origin isolated (%r): its opener policy did not take effect, so this run replaced nothing"
                    % page.evaluate("self.crossOriginIsolated"))
                left_bc, left_child = moved_origin(read_trace(held.trace_dir), second)
                assert left_bc > 0 and left_child > 0, "the engine told no browsing context left for %s" % second

                # 4. the window the second page reads is still the restored one
                after = _stable(page, (width, height))
                after_event = _event_offset(page, POINTS[1])
                _check_reading("the second page after the replacement", after, after_event, RESTORED_390x844, 1.0)
                assert (after["ow"], after["oh"], after["sx"], after["sy"], after["mx"], after["my"]) != (1936, 1048, -8, -8, 0, 85), (
                    "the second page reads the maximized window again: %r" % after)
    finally:
        held.__exit__(*sys.exc_info())      # the evidence is saved when the body raised, and the engine's trace variable is put back
