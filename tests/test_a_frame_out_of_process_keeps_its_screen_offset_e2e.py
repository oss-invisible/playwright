"""A frame that runs in another process still knows where it is on the screen (today's engine: the offset of its iframe is lost).

With sites isolated an iframe of another site lives in a process of its own. A document reads the place of its viewport on the screen with `window.mozInnerScreenX/Y`, and
every mouse event of the frame carries `screenX/Y`: both are the top document's value plus the offset of the iframe in the page (its box in the top document, and its border
and padding). The retail recording (Firefox 159, Linux, device pixel ratio 1, an iframe at 100,100 CSS px with border 0, the top page's mozInnerScreen at [0, 85]):

    top   mozInnerScreen [0, 85]
    frame mozInnerScreen [100, 185]                     (the top's, plus the iframe's offset)
    a click at the top point (175, 200)  ->  in the frame  screenX/Y = (175, 285)   clientX/Y = (75, 100)

On the engine of today the frame of ANOTHER site reads `mozInnerScreen [0, 85]` (the top's value, the offset lost) and its click has screenX/Y (75, 185): the process of the
frame never learned where its iframe is. The frame of the SAME site (a document of the page's own process) is right; it is the control of this file, green today, and it is
what says the out-of-process cases are red for the process and not for the harness.

Source: retail 159 recording, 21/11. The numbers above are the reference and are not asserted, because the window of the machine is not at the same place on every
machine; what is asserted are the RELATIONS, which hold at any window position, any screen and any border:

* `frame.mozInnerScreen == top.mozInnerScreen + the iframe's box in the top document + its clientLeft/clientTop`, within 1 CSS px of rounding (the frame is read after a wait on
  that very condition, so a late arrival of the position is told from a position that never comes);
* a real click of the wrapper (`page.mouse.click`) inside the iframe, at a point given in the top's coordinates, reaches the frame with `screenX - clientX ==
  frame.mozInnerScreenX` (and Y), and with `screenX/Y == top.mozInnerScreen + that point` (screen coordinates agree between documents); a click of the top document outside the
  iframe says the top's own `screenX - clientX == top.mozInnerScreenX`, which is what the frame's offset is added to.

Three sites on one machine, told apart by host (`_fission_frames.py`): the top is A, the iframe is of A (the control) or of B (out of process, proved from inside the pages
by `assert_other_process`, so this file cannot turn into a same-process suite).
"""
from __future__ import annotations

import pytest

from _fission_frames import Sites, assert_other_process, launch, new_page, wait_for, wait_frame
from _waits import watch

pytestmark = pytest.mark.e2e

#: Where the iframe sits in the top page (CSS px, from the top's own origin) and how big it is.
LEFT, TOP_, WIDTH, HEIGHT = 100, 100, 300, 200
#: Where in the iframe's content the click goes.
INSIDE = (75, 100)
#: Where the click of the top document goes: outside the iframe, which starts at (LEFT, TOP_).
OUTSIDE = (30, 30)
#: One CSS pixel of rounding is allowed; a lost offset is 100.
TOLERANCE = 1.0
#: The deadline of a wait on a condition; no healthy run comes near it.
SECONDS = 20.0

SITES = [("same-site", "A"), ("other-site", "B")]

RECORD = ("window.__clicks = [];"
          "addEventListener('click', e => window.__clicks.push([e.screenX, e.screenY, e.clientX, e.clientY, e.isTrusted]), true);")

FRAME_PAGE = ("<!doctype html><title>frame</title><body style='margin:0;min-height:100vh'><div style='height:400px'></div>"
              "<script>" + RECORD + "</script>")


def top_page(frame_url, border):
    return ("<!doctype html><title>top</title><body style='margin:0;min-height:100vh'>"
            "<iframe id='f' src='%s' style='position:absolute;left:%dpx;top:%dpx;width:%dpx;height:%dpx;border:%dpx solid #888;padding:0'></iframe>"
            "<script>" % (frame_url, LEFT, TOP_, WIDTH, HEIGHT, border) + RECORD + "</script>")


@pytest.fixture
def sites():
    with Sites() as s:
        yield s


@pytest.fixture
def browser(firefox_binary):
    with launch(firefox_binary) as browser:
        yield browser


@pytest.fixture
def page(browser):
    context, page = new_page(browser)
    yield page
    context.close()


def open_page(sites, page, site, border):
    """The top page (site A) with an iframe of ``site``; returns the iframe's Frame. For the other site the frame is proved to be out of process."""
    sites.route("B", "/frame", FRAME_PAGE)
    sites.route("A", "/frame", FRAME_PAGE)
    frame_url = sites.url(site, "/frame")
    sites.route("A", "/top", top_page(frame_url, border))
    page.goto(sites.url("A", "/top"), wait_until="load", timeout=30000)
    frame = wait_frame(page, sites.url(site), "/frame")
    # the frame's own document has run its script (its `load`), whichever process it is in
    assert wait_for(page, lambda: frame.evaluate("Array.isArray(window.__clicks)"), SECONDS), "the frame's document never ran its script"
    if site == "B":
        assert_other_process(page.main_frame, frame)
    return frame


def inner_screen(frame):
    return frame.evaluate("() => [window.mozInnerScreenX, window.mozInnerScreenY]")


def iframe_offset(page):
    """The iframe's content origin in the top document's coordinates: its box plus its border."""
    return page.evaluate("""() => { const f = document.getElementById('f'); const r = f.getBoundingClientRect();
        return [r.left + f.clientLeft, r.top + f.clientTop, f.clientLeft, f.clientTop]; }""")


def close(a, b):
    return abs(a - b) <= TOLERANCE


@pytest.mark.parametrize("border", [0, 3])
@pytest.mark.parametrize("kind,site", SITES)
def test_a_frame_reads_its_screen_origin_with_the_offset_of_its_iframe(sites, page, kind, site, border):
    frame = open_page(sites, page, site, border)
    left, top, client_left, client_top = iframe_offset(page)
    # the layout is what the page was made with: the iframe is where it was put, and its border is inside its box
    assert (left, top) == (LEFT + border, TOP_ + border) and (client_left, client_top) == (border, border), (left, top, client_left, client_top)

    def sample():
        t = inner_screen(page.main_frame)
        f = inner_screen(frame)
        return {"top": t, "frame": f, "want": [t[0] + left, t[1] + top]}

    seen = watch(sample, lambda s: close(s["frame"][0], s["want"][0]) and close(s["frame"][1], s["want"][1]), SECONDS,
                 lambda: page.wait_for_timeout(100))
    assert seen.ok, (
        "the %s iframe (border %d) at (%d, %d) of the top page reads window.mozInnerScreen %s in its own document: the top document's is %s, so it should be the "
        "top's plus the iframe's offset %s = %s (retail 159: top [0, 85], frame [100, 185] for an iframe at 100,100). Seen: %s" % (
            kind, border, LEFT, TOP_, seen.history[-1][1]["frame"], seen.history[-1][1]["top"], [left, top], seen.history[-1][1]["want"], seen.render()))


@pytest.mark.parametrize("kind,site", SITES)
def test_a_click_in_a_frame_has_the_screen_point_of_its_place_in_the_page(sites, page, kind, site):
    frame = open_page(sites, page, site, 0)
    left, top, _, _ = iframe_offset(page)
    # the top document alone: a click outside the iframe says where the screen is for the top (screenX - clientX == mozInnerScreenX)
    page.mouse.click(*OUTSIDE)
    assert wait_for(page, lambda: page.evaluate("window.__clicks.length") >= 1, SECONDS), "the click of the top document never arrived"
    sx, sy, cx, cy, trusted = page.evaluate("window.__clicks[0]")
    top_x, top_y = inner_screen(page.main_frame)
    assert trusted and (cx, cy) == OUTSIDE, (sx, sy, cx, cy, trusted)
    assert close(sx - cx, top_x) and close(sy - cy, top_y), (
        "a click of the top document at %s has screenX/Y (%s, %s) and clientX/Y (%s, %s): its offset (%s, %s) is not its mozInnerScreen (%s, %s)" % (
            OUTSIDE, sx, sy, cx, cy, sx - cx, sy - cy, top_x, top_y))

    # a click inside the iframe, at a point given in the top's coordinates
    point = (left + INSIDE[0], top + INSIDE[1])
    page.mouse.click(*point)
    assert wait_for(page, lambda: frame.evaluate("window.__clicks.length") >= 1, SECONDS), "the click inside the iframe never arrived in the frame"
    fsx, fsy, fcx, fcy, ftrusted = frame.evaluate("window.__clicks[0]")
    frame_x, frame_y = inner_screen(frame)
    assert ftrusted and (fcx, fcy) == INSIDE, "the frame's own coordinates of the click are the point inside it: %s" % ((fsx, fsy, fcx, fcy, ftrusted),)
    # the frame's screen offset is what its document says it is (retail 159: screen (175, 285), client (75, 100), mozInnerScreen [100, 185])
    assert close(fsx - fcx, frame_x) and close(fsy - fcy, frame_y), (
        "the %s frame's click at the top point %s has screenX/Y (%s, %s) and clientX/Y (%s, %s): screen - client is (%s, %s) but its window.mozInnerScreen is (%s, %s)" % (
            kind, point, fsx, fsy, fcx, fcy, fsx - fcx, fsy - fcy, frame_x, frame_y))
    # and screen coordinates agree between the documents: the point of the top, on the screen of the top
    assert close(fsx, top_x + point[0]) and close(fsy, top_y + point[1]), (
        "the %s frame's click at the top point %s has screenX/Y (%s, %s), while the screen point of that point is the top's mozInnerScreen (%s, %s) plus it = (%s, %s) "
        "(retail 159: (175, 285) for (175, 200) and a top mozInnerScreen of [0, 85])" % (kind, point, fsx, fsy, top_x, top_y, top_x + point[0], top_y + point[1]))
