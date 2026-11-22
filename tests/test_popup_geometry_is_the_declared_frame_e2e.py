"""A popup reads the window frame the core declares, not the one of the host.

What it fixes: the geometry of a window a page opens with `window.open(url, 'p', 'width=500,height=400')`, read from
inside the popup. A popup is its real inner size plus the declared frame (retail Windows Firefox, measured by the core at
scale 1.0: a border of 8 on the left, right and bottom, the popup UI of 72 above the content, and a default position of
4,4 when the page names no left/top):

  * `outerWidth  == innerWidth + 2 * border`
  * `outerHeight == innerHeight + popup_ui + border`
  * `screenX == screenY ==` the declared default position (the page named none)
  * `mozInnerScreenX == screenX + border`, `mozInnerScreenY == screenY + popup_ui`

Engine hook: `fingerprint/screen-window/geometry` of the engine series (`nsGlobalWindowOuter::GetOuterSize`,
`GetScreenXY`, `GetMozInnerScreenX/Y`, with the content origin shift of `StealthScreenCoords`) and
`fingerprint/screen-window/popup-position` (`nsWindowWatcher`, the default position of a popup).

Negative control, in the same test: the same page in a launch declared with a SECOND frame that no runner has (border 6,
popup UI 53, default position 9). The popup reads that one (outer - inner = 12 x 59, screen 9, mozInnerScreen 15 x 62) and
none of the first declaration's numbers, and the two readings DIFFER. The engine off is not the control: the declared frame
is the one of a retail Windows window at scale 1.0, so on a Windows runner the host's own popup has the very same frame
(measured: 16 x 80 with the engine off) and the test could not tell a hook that answers from one that does not.

A popup that names ONLY `left` (`window.open(url, 'p', 'width=500,height=400,left=137')`, `test_a_popup_naming_only_left_..`): the
hook (`StealthPlacePopupAtDefault`, `fingerprint/screen-window/popup-position`) fills in only the coordinate the page did not give, so
`screenX` is the 137 the page asked for (the opener's CSS-to-desktop scale is 1.0 here, no second conversion) and `screenY` is the
declared default position, 4 (9 under the second frame); the frame relations are the same as for a popup with no position at all.

Why a page reads it: `outerWidth - innerWidth`, `screenX` and `mozInnerScreenX/Y` are one line each, and a popup whose
frame is not a Windows frame is a browser that does not run on Windows. Linux and Windows run the same assertions: the
declaration, not the host, is what is read.
"""
from __future__ import annotations

import pytest
from _popup_geometry import (OTHER_BORDER, OTHER_FRAME, OTHER_POPUP_UI, OTHER_POSITION, frame_extra, frame_of_the_declaration,
                             read_popup)

pytestmark = pytest.mark.e2e


def test_a_popup_is_its_inner_size_plus_the_declared_frame(firefox_binary):
    frame = frame_of_the_declaration()
    declared = (2 * frame.border, frame.popup_ui + frame.border)

    def has_the_frame(reading):
        p = reading["popup"]
        return (p["ow"] - p["iw"], p["oh"] - p["ih"]) == declared

    on = read_popup(firefox_binary, ready=has_the_frame)["popup"]
    assert (on["ow"] - on["iw"], on["oh"] - on["ih"]) == declared, (
        "the popup reads outer %dx%d for inner %dx%d: the frame is %r, the declaration is %r"
        % (on["ow"], on["oh"], on["iw"], on["ih"], (on["ow"] - on["iw"], on["oh"] - on["ih"]), declared))
    assert (on["sx"], on["sy"]) == (frame.popup_position, frame.popup_position), (
        "a popup that names no position reads screen %r, the declared default is %d" % ((on["sx"], on["sy"]),
                                                                                      frame.popup_position))
    assert on["mx"] == pytest.approx(on["sx"] + frame.border, abs=0.01), (
        "mozInnerScreenX %r is not screenX %r plus the border %d" % (on["mx"], on["sx"], frame.border))
    assert on["my"] == pytest.approx(on["sy"] + frame.popup_ui, abs=0.01), (
        "mozInnerScreenY %r is not screenY %r plus the popup UI %d" % (on["my"], on["sy"], frame.popup_ui))

    # The control: a launch declared with another frame reads that one, and none of the first declaration's relations.
    other_declared = frame_extra(OTHER_BORDER, OTHER_POPUP_UI)
    assert other_declared != declared

    def has_the_other_frame(reading):
        p = reading["popup"]
        return (p["ow"] - p["iw"], p["oh"] - p["ih"]) == other_declared

    other = read_popup(firefox_binary, extra_prefs=OTHER_FRAME, ready=has_the_other_frame)["popup"]
    assert (other["ow"] - other["iw"], other["oh"] - other["ih"]) == other_declared, (
        "declared with another frame the popup reads outer %dx%d for inner %dx%d: the frame is %r, the second declaration is %r"
        % (other["ow"], other["oh"], other["iw"], other["ih"], (other["ow"] - other["iw"], other["oh"] - other["ih"]), other_declared))
    assert (other["sx"], other["sy"]) == (OTHER_POSITION, OTHER_POSITION), (
        "declared with another default position the popup reads screen %r, the second declaration is %d" % ((other["sx"], other["sy"]), OTHER_POSITION))
    assert other["mx"] == pytest.approx(other["sx"] + OTHER_BORDER, abs=0.01)
    assert other["my"] == pytest.approx(other["sy"] + OTHER_POPUP_UI, abs=0.01)
    assert (other["ow"] - other["iw"], other["oh"] - other["ih"]) != (on["ow"] - on["iw"], on["oh"] - on["ih"]), (
        "the two declared frames read the same frame: the declaration decides nothing")
    assert (other["sx"], other["sy"]) != (on["sx"], on["sy"]) and other["my"] - other["sy"] != on["my"] - on["sy"], (
        "the two declared frames read the same position or content origin: %r vs %r" % (other, on))


LEFT = 137


def test_a_popup_naming_only_left_reads_that_x_and_the_declared_default_y(firefox_binary):
    frame = frame_of_the_declaration()
    features = "width=500,height=400,left=%d" % LEFT
    declared = (2 * frame.border, frame.popup_ui + frame.border)

    def has_the_frame(reading):
        p = reading["popup"]
        return (p["ow"] - p["iw"], p["oh"] - p["ih"]) == declared

    on = read_popup(firefox_binary, ready=has_the_frame, features=features)["popup"]
    assert (on["ow"] - on["iw"], on["oh"] - on["ih"]) == declared, (
        "the popup reads outer %dx%d for inner %dx%d: the frame is %r, the declaration is %r"
        % (on["ow"], on["oh"], on["iw"], on["ih"], (on["ow"] - on["iw"], on["oh"] - on["ih"]), declared))
    assert on["sx"] == LEFT, "a popup that names left=%d reads screenX %r: the page's own x is lost" % (LEFT, on["sx"])
    assert on["sy"] == frame.popup_position, (
        "a popup that names no top reads screenY %r, the declared default is %d" % (on["sy"], frame.popup_position))
    assert on["mx"] == pytest.approx(on["sx"] + frame.border, abs=0.01)
    assert on["my"] == pytest.approx(on["sy"] + frame.popup_ui, abs=0.01)

    # The control: a launch declared with a second frame keeps the page's x and reads the second default for y.
    other_declared = frame_extra(OTHER_BORDER, OTHER_POPUP_UI)

    def has_the_other_frame(reading):
        p = reading["popup"]
        return (p["ow"] - p["iw"], p["oh"] - p["ih"]) == other_declared

    other = read_popup(firefox_binary, extra_prefs=OTHER_FRAME, ready=has_the_other_frame, features=features)["popup"]
    assert (other["ow"] - other["iw"], other["oh"] - other["ih"]) == other_declared
    assert other["sx"] == LEFT, "declared with another frame the popup reads screenX %r, the page asked for %d" % (other["sx"], LEFT)
    assert other["sy"] == OTHER_POSITION, (
        "declared with another default position the popup reads screenY %r, the second declaration is %d" % (other["sy"], OTHER_POSITION))
    assert other["mx"] == pytest.approx(LEFT + OTHER_BORDER, abs=0.01)
    assert other["my"] == pytest.approx(OTHER_POSITION + OTHER_POPUP_UI, abs=0.01)
    assert other["sy"] != on["sy"] and other["sx"] == on["sx"], (
        "the declaration decides y alone when the page names x: %r vs %r" % (other, on))
