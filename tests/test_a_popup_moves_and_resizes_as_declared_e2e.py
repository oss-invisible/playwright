"""A popup of the maximized window moves, resizes and clamps in the DECLARED window, not in the host's.

The page's ``moveBy``, ``moveTo``, ``resizeBy`` and ``resizeTo`` read the declared position and size of the window, add what the page asked, and send the
absolute result to the parent, which applies it to the real window. A popup whose declared position or frame is not its real one (on Linux the window manager
puts the popup where it likes, and its frame is the host's) must still answer these calls as the retail window does. Three things, per display case (the four of
the window tests), each in a popup of its own opened by a real click, ``window.open(url, '_blank', 'width=500,height=400')``:

  1. ``resizeBy(0, 0)``: ``innerWidth``, ``innerHeight``, ``outerWidth`` and ``outerHeight`` stay what they were. A call that adds nothing must change nothing; if the
     parent converted the declared outer size into the real one with the wrong frame, the real inner size would move by the difference of the two frames (retail
     measures nothing here: the call is a no-op in its window). The page is watched for 1.5 s after the call, so a change that arrives late is seen.
  2. ``moveBy(10, 10)``: ``screenX`` and ``screenY`` grow by exactly 10 CSS pixels, from the position the popup read before. If the parent took the real position of
     the window as the base, the declared and the real positions would differ by what the host's window manager decided, and the popup would jump.
  3. ``moveTo(99999, 0)``: ``screenX`` is ``screen.availLeft + screen.availWidth - outerWidth``, the three read by the page. The clamp is the page's
     (``nsGlobalWindowOuter::CheckSecurityLeftAndTop``: it keeps the window inside the available rectangle of the screen the page reads, with the outer size the page
     reads), so the page can learn the real outer width of the window from where the popup stops if the clamp reads any of them from the host.

Why a page reads it: a script that moves a window by ten and sees it move by another number, or that throws a window to the far right and reads where it stops,
is reading the host's window manager and its frame.

The engine that answers these values is not released yet: on the engine of the pin the clamp and the moves are the host's where the declared and the real window
differ, so this test is RED there, on purpose. It enters the suite with the engine release.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

import pytest
from _fission_frames import Sites
from _window_origin import CASES, DEADLINE_S, launch_case, observe, settled, wait_for

pytestmark = pytest.mark.e2e

#: The button that opens the popup: ``position:fixed`` at (20, 20), 160x40 CSS pixels; its centre is the point of the real click.
OPEN = (100, 40)
FEATURES = "width=500,height=400"
#: How long the window is watched after a call that must change nothing, in seconds.
WATCH_S = 1.5

TOP = """<!doctype html><meta charset=utf-8><title>popup</title>
<style>html,body{margin:0;padding:0;height:100%}
#open{position:fixed;left:20px;top:20px;width:160px;height:40px;margin:0}</style>
<body><button id=open>popup</button><script>
document.getElementById('open').addEventListener('click', () => { window.open(window.__popupUrl, '_blank', window.__popupFeatures); });
</script>"""

POPUP = "<!doctype html><meta charset=utf-8><title>popup</title><body style=margin:0>x"

READ = """() => ({iw: innerWidth, ih: innerHeight, ow: outerWidth, oh: outerHeight, sx: screenX, sy: screenY,
                 availL: screen.availLeft, availW: screen.availWidth})"""
SIZE = "() => [innerWidth, innerHeight, outerWidth, outerHeight]"
POSITION = "() => [screenX, screenY]"


def _open(page, sites) -> Any:
    """``window.open`` from a real click on the page's button; the popup, loaded."""
    page.evaluate("([url, features]) => { window.__popupUrl = url; window.__popupFeatures = features; }", [sites.url("A", "/popup"), FEATURES])
    with page.expect_popup(timeout=30000) as opened:
        page.mouse.click(*OPEN)         # the client's own click: a trusted event, so window.open is allowed
    popup = opened.value
    popup.wait_for_load_state("load", timeout=30000)
    return popup


def _settled_popup(popup) -> Dict[str, Any]:
    """The popup once it is the size it was asked and two looks in a row agree."""
    return settled(popup, lambda: popup.evaluate(READ))


def _phase_resize_by_zero(page, sites, case: str) -> None:
    popup = _open(page, sites)
    try:
        before = _settled_popup(popup)
        assert (before["iw"], before["ih"]) == (500, 400), "%s: the popup is %rx%r inside before the call, it was asked 500x400 (%r)" % (
            case, before["iw"], before["ih"], before)
        popup.evaluate("() => { window.resizeBy(0, 0); }")
        after = observe(popup, lambda: tuple(popup.evaluate(SIZE)), WATCH_S)
        was = (before["iw"], before["ih"], before["ow"], before["oh"])
        assert after == [was], (
            "%s: resizeBy(0, 0) changed the window: (innerWidth, innerHeight, outerWidth, outerHeight) was %r and the page then read %r" % (case, was, after))
    finally:
        popup.close()


def _phase_move_by(page, sites, case: str) -> None:
    popup = _open(page, sites)
    try:
        before = _settled_popup(popup)
        popup.evaluate("() => { window.moveBy(10, 10); }")
        expected: Tuple[int, int] = (before["sx"] + 10, before["sy"] + 10)
        wait_for(popup, lambda: tuple(popup.evaluate(POSITION)) == expected, DEADLINE_S)
        after = settled(popup, lambda: tuple(popup.evaluate(POSITION)))
        assert tuple(after) == expected, (
            "%s: moveBy(10, 10) from (screenX, screenY) = (%r, %r) leaves it at %r, it must be exactly %r" % (case, before["sx"], before["sy"], tuple(after), expected))
    finally:
        popup.close()


def _phase_move_to_the_far_right(page, sites, case: str) -> None:
    popup = _open(page, sites)
    try:
        before = _settled_popup(popup)
        popup.evaluate("() => { window.moveTo(99999, 0); }")
        # the clamp's own inputs, read by the page after the call: the declared avail rectangle and the declared outer width
        wait_for(popup, lambda: popup.evaluate("() => screenX") == popup.evaluate("() => screen.availLeft + screen.availWidth - outerWidth"), DEADLINE_S)
        after = settled(popup, lambda: popup.evaluate(READ))
        expected = after["availL"] + after["availW"] - after["ow"]
        assert after["sx"] == expected, (
            "%s: after moveTo(99999, 0) screenX is %r; the declared avail right (availLeft %r + availWidth %r) less outerWidth %r is %r (popup was %r before the call)"
            % (case, after["sx"], after["availL"], after["availW"], after["ow"], expected, before))
    finally:
        popup.close()


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_popup_moves_and_resizes_as_declared(firefox_binary, case):
    with Sites() as sites:
        sites.route("A", "/page", TOP)
        sites.route("A", "/popup", POPUP)
        with launch_case(firefox_binary, case) as browser:
            page = browser.new_page()
            page.goto(sites.url("A", "/page"), wait_until="load", timeout=30000)
            _phase_resize_by_zero(page, sites, case)
            _phase_move_by(page, sites, case)
            _phase_move_to_the_far_right(page, sites, case)
