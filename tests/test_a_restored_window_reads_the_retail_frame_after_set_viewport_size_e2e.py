"""`page.set_viewport_size` leaves a restored window with the frame of retail Windows Firefox 159.0: what a page reads of it, step by step.

What it checks: the numbers a page reads (``outerWidth``, ``outerHeight``, ``innerWidth``, ``innerHeight``, ``screenX``, ``screenY``,
``mozInnerScreenX/Y``) and the offset the mouse events carry (``screenX - clientX``, ``screenY - clientY``) in four steps, against retail Firefox 159.0 on
Windows 11 measured live (GitHub runs oss-invisible/core 45657528859, 45672953042, 45679625406 and 45695770162; RetailCaptures
``notes/window-geometry.md``):

  1. the window as it opens, maximized;
  2. after ``set_viewport_size(w1, h1)``: the window is restored, so it sits at a small positive position (the frame's default, 4 at scale 1.0) and its content
     origin is that position plus the frame: ``outer`` is the requested inner size plus the frame;
  3. after ``set_viewport_size(w2, h2)``: only the height of the window changes, the position and the content origin stay;
  4. after ``set_viewport_size`` back to the maximized inner size: the window STAYS restored (a page that asks for the size it had does not get the maximized
     window back), so its outer size is the inner size plus the restored frame, not the maximized one.

Four display scales: 1.0, 1.25 and 1.5 on a 1920x1080 panel (device pixels) and 2.0 on a 1600x1200 one. At 1.5 and 2.0 the browser chrome is the compact bar
(``screen.availHeight`` 672 CSS pixels at 1.5, 552 at 2.0).
A page reads everything in CSS pixels, so the frame in CSS pixels is the frame in device pixels over the scale, rounded the way retail rounds it: the numbers
below are what retail answers, not a formula of this test.

Fullscreen, in every case, twice (steps F0 and F1): the page enters fullscreen with a real click on a button whose handler calls
``document.documentElement.requestFullscreen()`` (the click is the client's own, so it is a user gesture, as in ``scripts/ci_host_gate.py``) and reads the window
(outer == inner == the declared CSS screen, ``screenX/Y`` 0, ``mozInnerScreen`` 0, the events' offset 0), then ``document.exitFullscreen()`` and reads it again: EVERY
value must be the one it had before, exactly. F0 is right after the maximized reading, before any ``set_viewport_size``; F1 is after call 2. Retail goes back to the
window it had; an engine that treats a change of the page's size as a resize of the window does not, and this is the step that tells.

Alongside the window, the page's probe (a copy of the one of RetailCaptures' ``window_geometry/page.html``) reads, at every step, what a page reads of the
viewport: ``matchMedia`` width, height, aspect-ratio and resolution (found exactly by binary search over ``min-`` queries), ``documentElement.clientWidth`` and
``clientHeight``, and ``visualViewport`` width, height, scale, offsetLeft, offsetTop, pageLeft and pageTop. They are compared with retail's (``RETAIL_PROBE``,
printed from the raw files of capture round 4), in the maximized step and after each call; after leaving fullscreen they must equal the reading before it.

The invariant, in every step: ``event.screenX - event.clientX == mozInnerScreenX`` and ``event.screenY - event.clientY == mozInnerScreenY``, up to the rounding of
the event's integer coordinates (``screenX`` and ``clientX`` of an event are whole numbers, ``mozInnerScreen`` is not at a fractional scale). At scale 1.0 the
frame is whole and the offset is exactly equal.

The engine that answers these values is not released yet: on the engine of the pin this test is RED, on purpose. It enters the suite with the engine release,
and the wrapper pull request that bumps the pin carries it.

Why a page reads it: ``outerWidth - innerWidth``, ``screenX`` and ``mozInnerScreenX/Y`` are one line each, and a window whose frame is not the frame of a Windows
window at that scale (or that goes back to maximized when the page asks for the size it had) is a browser that does not run on Windows. Linux and Windows
run the same assertions: the declaration, not the host, is what is read.

The page is a local one (``Sites``); the mouse is the client's own (``page.mouse.move``), so the events are trusted ones.

Frames and popups, against retail round 5 (GitHub run oss-invisible/core 45952171443; RetailCaptures ``captures/WINDOW_GEOMETRY_POPUP_FRAME.csv``, whose values are
copied by a script into ``RETAIL_FRAMES`` and ``RETAIL_POPUPS`` below, with the file's sha256). This is the comparison with RETAIL; the popup test of the repo
(``test_popup_geometry_is_the_declared_frame_e2e.py``) compares with the core's declaration.

  * Frames, in the maximized state and after call 1: a same-origin frame (300x120 at x 10, y 60, from the page's own site) and a cross-site one (300x120 at x 10,
    y 200, another local site), added to the page for the reading and removed after it. From inside each frame the test reads ``mozInnerScreenX/Y``, ``outerWidth``,
    ``outerHeight``, ``screenX/Y`` and ``devicePixelRatio``, and the offset the events of a real mouse move over the frame carry. ``outer``, ``screenX/Y`` and ``dpr``
    are the parent's; ``mozInnerScreen`` is the parent's plus the frame's rect, EXCEPT the cross-site frame at scale 1.25, which retail reads 0.4 CSS pixel further
    right (maximized: same 10, 144.8; cross 10.4, 284.8; after call 1: same 20.4, 148; cross 20.8, 288).
  * A popup opened from the maximized page after a real click on a button: ``window.open(url, '_blank', 'width=500,height=400')`` at every case, and, at scale 2.0,
    ``'left=100,top=80,width=500,height=400'``, which retail moves up into the work area (inner 500x400, outer 513x476, screenX/Y 100, 76, mozInnerScreen 106.5, 146).
    An unpositioned popup sits at 4 device pixels (``screenX/Y`` 4 over the scale, rounded). Everything is compared exactly.
  * The offset of the events: retail's mouse points are its own (device pixels of its window), ours are CSS pixels of the page, and the integer ``screenX - clientX``
    rounds by the point, so away from scale 1.0 the offset may differ from retail's by one; at scale 1.0 it is equal. In every case it is within one pixel of
    ``mozInnerScreen`` (the invariant above).

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

import json
import sys
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
from _evidence_on_failure import FailureEvidence
from _fission_frames import Sites, wait_frame
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
POINTS: List[Tuple[int, int]] = [(60, 70), (120, 150), (80, 110), (150, 170), (70, 90), (130, 160), (90, 120), (140, 175),
                                 (65, 100), (125, 155), (85, 115), (145, 165), (75, 95), (135, 150), (95, 125), (155, 170)]
#: The button whose click asks for fullscreen: ``position:fixed`` at (20, 20), 160x40 CSS pixels; its centre is the point of the real click.
BUTTON = (100, 40)

PAGE = """<!doctype html><meta charset=utf-8><title>restored window</title>
<style>html,body{margin:0;padding:0;height:100%}
#fs,#open{position:fixed;top:20px;width:160px;height:40px;margin:0;z-index:2147483647}
#fs{left:20px}#open{left:200px}</style>
<body><button id=fs>fullscreen</button><button id=open>popup</button><script>
window.__last = null; window.__moves = 0; window.__fsRequest = 'not asked';
document.getElementById('fs').addEventListener('click', () => {
  window.__fsRequest = 'pending';
  document.documentElement.requestFullscreen().then(() => { window.__fsRequest = 'resolved'; }, e => { window.__fsRequest = 'rejected: ' + e; });
});
document.getElementById('open').addEventListener('click', () => {
  window.open(window.__popupUrl, '_blank', window.__popupFeatures);
});
window.addEventListener('mousemove', e => {
  if (!e.isTrusted) return;
  window.__moves++;
  window.__last = {n: window.__moves, cx: e.clientX, cy: e.clientY, sx: e.screenX, sy: e.screenY,
                   dx: e.screenX - e.clientX, dy: e.screenY - e.clientY};
}, true);
// Everything a page can read that may differ under responsive-design mode (inRDMPane), read here in the page itself and
// returned as a JSON string (the content script parses it): the screen, the pixel ratio, the media queries (the integers
// found by binary search over min- queries, the booleans as .matches), touch, the visual viewport, the root's client size.
window.__probe = () => {
  const matches = (query) => matchMedia(query).matches;
  // The largest integer n in [0, hi] for which make(n) matches; every min- query is true up to the value and false after.
  const largest = (make, hi) => {
    let lo = 0;
    while (lo < hi) {
      const mid = Math.ceil((lo + hi) / 2);
      if (matches(make(mid))) lo = mid; else hi = mid - 1;
    }
    return lo;
  };
  const found = {
    width: largest((n) => `(min-width: ${n}px)`, 65536),
    height: largest((n) => `(min-height: ${n}px)`, 65536),
    "device-width": largest((n) => `(min-device-width: ${n}px)`, 65536),
    "device-height": largest((n) => `(min-device-height: ${n}px)`, 65536),
    // the ratio times 10000, rounded down
    "aspect-ratio_x10000": largest((n) => `(min-aspect-ratio: ${n}/10000)`, 1000000),
    "device-aspect-ratio_x10000": largest((n) => `(min-device-aspect-ratio: ${n}/10000)`, 1000000),
    // the resolution in dppx times 100, rounded down (0.01 dppx)
    "resolution_x100": largest((n) => `(min-resolution: ${n / 100}dppx)`, 2000),
  };
  const queries = {};
  for (const q of [
    `(width: ${found.width}px)`, `(height: ${found.height}px)`,
    `(device-width: ${found["device-width"]}px)`, `(device-height: ${found["device-height"]}px)`,
    "(pointer: fine)", "(pointer: coarse)", "(pointer: none)",
    "(any-pointer: fine)", "(any-pointer: coarse)", "(any-pointer: none)",
    "(hover: hover)", "(hover: none)", "(any-hover: hover)", "(any-hover: none)",
    "(orientation: portrait)", "(orientation: landscape)",
  ]) queries[q] = matches(q);
  const s = screen;
  const vv = window.visualViewport;
  return JSON.stringify({
    screen: {width: s.width, height: s.height, availWidth: s.availWidth, availHeight: s.availHeight,
             availLeft: s.availLeft, availTop: s.availTop, colorDepth: s.colorDepth,
             orientation: s.orientation ? {type: s.orientation.type, angle: s.orientation.angle} : null},
    devicePixelRatio: window.devicePixelRatio,
    found: found,
    matches: queries,
    touch: {maxTouchPoints: navigator.maxTouchPoints, ontouchstart: "ontouchstart" in window, touchEvent: typeof TouchEvent},
    visualViewport: vv ? {width: vv.width, height: vv.height, scale: vv.scale, offsetLeft: vv.offsetLeft, offsetTop: vv.offsetTop,
                          pageLeft: vv.pageLeft, pageTop: vv.pageTop} : null,
    client: {width: document.documentElement.clientWidth, height: document.documentElement.clientHeight},
  });
};
</script>"""

# The page of a frame and of a popup: it only hears the mouse, as the top page does.
FRAME_PAGE = """<!doctype html><meta charset=utf-8><title>probe</title>
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

#: The frames of the reading, added to the page: ``[same-origin url, cross-site url]``, 300x120 at x 10, y 60 and y 200 (retail round 5's rects).
FRAMES_ON = """([same, cross]) => {
  for (const [id, src, top] of [['same', same, 60], ['cross', cross, 200]]) {
    const f = document.createElement('iframe');
    f.id = 'frame-' + id; f.src = src;
    f.style.cssText = 'position:absolute;left:10px;top:' + top + 'px;width:300px;height:120px;border:0;margin:0;padding:0';
    document.body.appendChild(f);
  }
}"""
FRAMES_OFF = "() => { document.querySelectorAll('iframe').forEach(f => f.remove()); }"
#: The real mouse move over each frame, in the page's CSS pixels: its centre.
FRAME_POINTS = {"same": (160, 120), "cross": (160, 260)}
#: The click that opens the popup: the centre of the ``#open`` button (``position:fixed`` at (200, 20), 160x40 CSS pixels), and the point of the mouse move in the popup.
OPEN = (280, 40)
POPUP_POINT = (150, 170)

READ = """() => ({ow: outerWidth, oh: outerHeight, iw: innerWidth, ih: innerHeight, sx: screenX, sy: screenY,
                 mx: mozInnerScreenX, my: mozInnerScreenY, dpr: devicePixelRatio, availH: screen.availHeight,
                 scw: screen.width, sch: screen.height, fs: document.fullscreenElement !== null})"""

# What retail answers, per case: the maximized reading, then the calls of set_viewport_size in order (the last one goes back to the maximized inner size).
# A key that is not in an entry is a value the measurement did not name: it is not asserted (the invariant of the events is, always).
CASES: Dict[str, Dict[str, Any]] = {
    "1920x1080-dpr1.0": {
        "dpr": 1.0,
        "panel": (1920, 1080),
        "maximized": {"ow": 1936, "oh": 1048, "iw": 1920, "ih": 947, "sx": -8, "sy": -8, "mx": 0, "my": 85},
        "calls": [
            {"size": (390, 844), "ow": 516, "oh": 945, "sx": 4, "sy": 4, "mx": 12, "my": 89, "dx": 12, "dy": 89},
            {"size": (414, 896), "ow": 516, "oh": 989, "sx": 4, "sy": 4, "mx": 12, "my": 89, "dx": 12, "dy": 89},
            {"size": (1920, 947), "ow": 1936, "oh": 1040, "sx": 4, "sy": 4, "mx": 12, "my": 89, "dx": 12, "dy": 89},
        ],
    },
    "1920x1080-dpr1.25": {
        "dpr": 1.25,
        "panel": (1920, 1080),
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
        "panel": (1920, 1080),
        "availH": 672,
        "maximized": {"iw": 1280, "ih": 599, "my": 72.667},
        "calls": [
            {"size": (390, 550), "ow": 515, "oh": 638, "sx": 3, "sy": 3, "mx": 10, "my": 75.333},
            {"size": (414, 500), "ow": 515, "oh": 580, "sx": 3, "sy": 3, "mx": 10, "my": 75.333},
            {"size": (1280, 599), "ow": 1295, "oh": 678, "sx": 3, "sy": 3, "mx": 10, "my": 75.333},
        ],
    },
    # the compact bar again, on a 1600x1200 panel (retail live: availHeight is 552 CSS pixels)
    "1600x1200-dpr2.0": {
        "dpr": 2.0,
        "panel": (1600, 1200),
        "availH": 552,
        "maximized": {"ow": 813, "oh": 565, "iw": 800, "ih": 479, "sx": -6, "sy": -6, "mx": 0, "my": 73},
        "calls": [
            {"size": (390, 430), "ow": 513, "oh": 516, "sx": 2, "sy": 2, "mx": 8.5, "my": 75},
            {"size": (414, 380), "ow": 513, "oh": 460, "sx": 2, "sy": 2, "mx": 8.5, "my": 75},
            {"size": (800, 479), "ow": 813, "oh": 558, "sx": 2, "sy": 2, "mx": 8.5, "my": 75},
        ],
    },
}


# What retail answers to the page's probe (``window.__probe`` above, the very function of RetailCaptures' page.html), per case and step: the media queries found
# by binary search (width, height, aspect-ratio times 10000 rounded down, resolution in dppx times 100), the root's client size and the visual viewport.
# Each value below is the one printed by a script from the raw.json named in the comment above its case (round 4 of the window-geometry capture, Firefox 159.0
# retail on GitHub's Windows runners, display launch "a", whose steps are the ones of this test: maximized, juggler = call 1, juggler2 = call 2,
# juggler_back = call 3); none is retyped. The keys of a case are the capture's step names. Round 3 (window-geometry-r3) holds the same values for the steps it has.
RETAIL_PROBE: Dict[str, Dict[str, Dict[str, Any]]] = {
    # source: RetailCaptures/captures/firefox-159.0/github-runner/2026-11-25-window-geometry-r4/dpr1/1920x1080/a/raw.json, steps[...].content.probe
    # sha256 5b6eea2939717aacafb539af3f67611a151a4ae0cfabb7b60dcf5a71c8c575be
    "1920x1080-dpr1.0": {
        # maximized
        "maximized": {"mq_width": 1920, "mq_height": 947, "mq_aspect_x10000": 20274, "mq_resolution_x100": 100, "client_w": 1920, "client_h": 947, "vv_w": 1920, "vv_h": 947, "vv_scale": 1, "vv_left": 0, "vv_top": 0, "vv_page_left": 0, "vv_page_top": 0},
        # call 1 (juggler)
        "juggler": {"mq_width": 390, "mq_height": 844, "mq_aspect_x10000": 4620, "mq_resolution_x100": 100, "client_w": 390, "client_h": 844, "vv_w": 390, "vv_h": 844, "vv_scale": 1, "vv_left": 0, "vv_top": 0, "vv_page_left": 0, "vv_page_top": 0},
        # call 2 (juggler2)
        "juggler2": {"mq_width": 414, "mq_height": 896, "mq_aspect_x10000": 4620, "mq_resolution_x100": 100, "client_w": 414, "client_h": 896, "vv_w": 414, "vv_h": 896, "vv_scale": 1, "vv_left": 0, "vv_top": 0, "vv_page_left": 0, "vv_page_top": 0},
        # call 3 (juggler_back)
        "juggler_back": {"mq_width": 1920, "mq_height": 947, "mq_aspect_x10000": 20274, "mq_resolution_x100": 100, "client_w": 1920, "client_h": 947, "vv_w": 1920, "vv_h": 947, "vv_scale": 1, "vv_left": 0, "vv_top": 0, "vv_page_left": 0, "vv_page_top": 0},
    },
    # source: RetailCaptures/captures/firefox-159.0/github-runner/2026-11-25-window-geometry-r4/dpr1.25/1920x1080/a/raw.json, steps[...].content.probe
    # sha256 17ba49d7573e3aa61ff439e89392c9d3ea6dc83051f97b6dbd2a4fb7a8fb9887
    "1920x1080-dpr1.25": {
        # maximized
        "maximized": {"mq_width": 1536, "mq_height": 731, "mq_aspect_x10000": 21006, "mq_resolution_x100": 125, "client_w": 1536, "client_h": 731, "vv_w": 1536, "vv_h": 731.2000122070312, "vv_scale": 1, "vv_left": 0, "vv_top": 0, "vv_page_left": 0, "vv_page_top": 0},
        # call 1 (juggler)
        "juggler": {"mq_width": 390, "mq_height": 690, "mq_aspect_x10000": 5654, "mq_resolution_x100": 125, "client_w": 390, "client_h": 690, "vv_w": 390.3999938964844, "vv_h": 690.4000244140625, "vv_scale": 1, "vv_left": 0, "vv_top": 0, "vv_page_left": 0, "vv_page_top": 0},
        # call 2 (juggler2)
        "juggler2": {"mq_width": 414, "mq_height": 640, "mq_aspect_x10000": 6475, "mq_resolution_x100": 125, "client_w": 414, "client_h": 640, "vv_w": 414.3999938964844, "vv_h": 640, "vv_scale": 1, "vv_left": 0, "vv_top": 0, "vv_page_left": 0, "vv_page_top": 0},
        # call 3 (juggler_back)
        "juggler_back": {"mq_width": 1536, "mq_height": 731, "mq_aspect_x10000": 21006, "mq_resolution_x100": 125, "client_w": 1536, "client_h": 731, "vv_w": 1536, "vv_h": 731.2000122070312, "vv_scale": 1, "vv_left": 0, "vv_top": 0, "vv_page_left": 0, "vv_page_top": 0},
    },
    # source: RetailCaptures/captures/firefox-159.0/github-runner/2026-11-25-window-geometry-r4/dpr1.5/1920x1080/a/raw.json, steps[...].content.probe
    # sha256 6422295bf112ab74b1c4cf691fd1c0382655093037c39aa6cae96d91fca5785f
    "1920x1080-dpr1.5": {
        # maximized
        "maximized": {"mq_width": 1280, "mq_height": 599, "mq_aspect_x10000": 21357, "mq_resolution_x100": 150, "client_w": 1280, "client_h": 599, "vv_w": 1280, "vv_h": 599.3333129882812, "vv_scale": 1, "vv_left": 0, "vv_top": 0, "vv_page_left": 0, "vv_page_top": 0},
        # call 1 (juggler)
        "juggler": {"mq_width": 390, "mq_height": 550, "mq_aspect_x10000": 7090, "mq_resolution_x100": 150, "client_w": 390, "client_h": 550, "vv_w": 390, "vv_h": 550, "vv_scale": 1, "vv_left": 0, "vv_top": 0, "vv_page_left": 0, "vv_page_top": 0},
        # call 2 (juggler2)
        "juggler2": {"mq_width": 414, "mq_height": 500, "mq_aspect_x10000": 8280, "mq_resolution_x100": 150, "client_w": 414, "client_h": 500, "vv_w": 414, "vv_h": 500, "vv_scale": 1, "vv_left": 0, "vv_top": 0, "vv_page_left": 0, "vv_page_top": 0},
        # call 3 (juggler_back)
        "juggler_back": {"mq_width": 1280, "mq_height": 599, "mq_aspect_x10000": 21357, "mq_resolution_x100": 150, "client_w": 1280, "client_h": 599, "vv_w": 1280, "vv_h": 599.3333129882812, "vv_scale": 1, "vv_left": 0, "vv_top": 0, "vv_page_left": 0, "vv_page_top": 0},
    },
    # source: RetailCaptures/captures/firefox-159.0/github-runner/2026-11-25-window-geometry-r4/dpr2/1600x1200/a/raw.json, steps[...].content.probe
    # sha256 be0c1610951900a32095d4965a7c9a9eae2fbf3e14791b2a55fdc8d25c75e360
    "1600x1200-dpr2.0": {
        # maximized
        "maximized": {"mq_width": 800, "mq_height": 479, "mq_aspect_x10000": 16701, "mq_resolution_x100": 200, "client_w": 800, "client_h": 479, "vv_w": 800, "vv_h": 479, "vv_scale": 1, "vv_left": 0, "vv_top": 0, "vv_page_left": 0, "vv_page_top": 0},
        # call 1 (juggler)
        "juggler": {"mq_width": 390, "mq_height": 430, "mq_aspect_x10000": 9069, "mq_resolution_x100": 200, "client_w": 390, "client_h": 430, "vv_w": 390, "vv_h": 430, "vv_scale": 1, "vv_left": 0, "vv_top": 0, "vv_page_left": 0, "vv_page_top": 0},
        # call 2 (juggler2)
        "juggler2": {"mq_width": 414, "mq_height": 380, "mq_aspect_x10000": 10894, "mq_resolution_x100": 200, "client_w": 414, "client_h": 380, "vv_w": 414, "vv_h": 380, "vv_scale": 1, "vv_left": 0, "vv_top": 0, "vv_page_left": 0, "vv_page_top": 0},
        # call 3 (juggler_back)
        "juggler_back": {"mq_width": 800, "mq_height": 479, "mq_aspect_x10000": 16701, "mq_resolution_x100": 200, "client_w": 800, "client_h": 479, "vv_w": 800, "vv_h": 479, "vv_scale": 1, "vv_left": 0, "vv_top": 0, "vv_page_left": 0, "vv_page_top": 0},
    },
}


# What retail answers to a frame and to a popup (round 5 of the window-geometry capture: Firefox 159.0 retail on GitHub's Windows runners, GitHub run
# oss-invisible/core 45952171443, display launch "a"; launch "b" equals it in every value below). Every value is printed by a script from
# RetailCaptures/captures/WINDOW_GEOMETRY_POPUP_FRAME.csv (sha256 64f1d437a261e0305604f16ac9a6b60c62546101ac994712fbdec251e8a9c2af); none is retyped.
# RETAIL_FRAMES[case][state][kind]: a 300x120 frame at ``rect`` (CSS pixels of the page) in a same-origin or a cross-site page, read from inside the frame:
# the parent's mozInnerScreen (parent_mx, parent_my), the frame's (mx, my), what it reads beyond parent + rect (``extra``: 0.4 CSS pixel in x for the cross-site
# frame at scale 1.25, nothing anywhere else), outer, screenX/Y, devicePixelRatio and the events' offset (dx, dy). The capture's state "juggler" is the parent
# restored at Juggler's SECOND size (its step iframes_juggler, parent outer 516x989 at scale 1.0); the parent's content origin is the same after call 1 and
# call 2 at the four cases, which is why the frame's mozInnerScreen below holds after call 1 too.
RETAIL_FRAMES: Dict[str, Dict[str, Dict[str, Dict[str, Any]]]] = {
    '1600x1200-dpr2.0': {
        'maximized': {
            'same': {'rect': (10, 60), 'parent_mx': 0, 'parent_my': 73, 'mx': 10, 'my': 133, 'extra': (0, 0), 'ow': 813, 'oh': 565, 'sx': -6, 'sy': -6, 'dpr': 2, 'dx': 10, 'dy': 133},
            'cross': {'rect': (10, 200), 'parent_mx': 0, 'parent_my': 73, 'mx': 10, 'my': 273, 'extra': (0, 0), 'ow': 813, 'oh': 565, 'sx': -6, 'sy': -6, 'dpr': 2, 'dx': 10, 'dy': 273},
        },
        'juggler': {
            'same': {'rect': (10, 60), 'parent_mx': 8.5, 'parent_my': 75, 'mx': 18.5, 'my': 135, 'extra': (0, 0), 'ow': 513, 'oh': 460, 'sx': 2, 'sy': 2, 'dpr': 2, 'dx': 19, 'dy': 135},
            'cross': {'rect': (10, 200), 'parent_mx': 8.5, 'parent_my': 75, 'mx': 18.5, 'my': 275, 'extra': (0, 0), 'ow': 513, 'oh': 460, 'sx': 2, 'sy': 2, 'dpr': 2, 'dx': 19, 'dy': 275},
        },
    },
    '1920x1080-dpr1.0': {
        'maximized': {
            'same': {'rect': (10, 60), 'parent_mx': 0, 'parent_my': 85, 'mx': 10, 'my': 145, 'extra': (0, 0), 'ow': 1936, 'oh': 1048, 'sx': -8, 'sy': -8, 'dpr': 1, 'dx': 10, 'dy': 145},
            'cross': {'rect': (10, 200), 'parent_mx': 0, 'parent_my': 85, 'mx': 10, 'my': 285, 'extra': (0, 0), 'ow': 1936, 'oh': 1048, 'sx': -8, 'sy': -8, 'dpr': 1, 'dx': 10, 'dy': 285},
        },
        'juggler': {
            'same': {'rect': (10, 60), 'parent_mx': 12, 'parent_my': 89, 'mx': 22, 'my': 149, 'extra': (0, 0), 'ow': 516, 'oh': 989, 'sx': 4, 'sy': 4, 'dpr': 1, 'dx': 22, 'dy': 149},
            'cross': {'rect': (10, 200), 'parent_mx': 12, 'parent_my': 89, 'mx': 22, 'my': 289, 'extra': (0, 0), 'ow': 516, 'oh': 989, 'sx': 4, 'sy': 4, 'dpr': 1, 'dx': 22, 'dy': 289},
        },
    },
    '1920x1080-dpr1.25': {
        'maximized': {
            'same': {'rect': (10, 60), 'parent_mx': 0, 'parent_my': 84.80000305175781, 'mx': 10, 'my': 144.8000030517578, 'extra': (0, 0), 'ow': 1550, 'oh': 830, 'sx': -7, 'sy': -7, 'dpr': 1.25, 'dx': 10, 'dy': 145},
            'cross': {'rect': (10, 200), 'parent_mx': 0, 'parent_my': 84.80000305175781, 'mx': 10.399999618530273, 'my': 284.79998779296875, 'extra': (0.4, 0), 'ow': 1550, 'oh': 830, 'sx': -7, 'sy': -7, 'dpr': 1.25, 'dx': 10, 'dy': 285},
        },
        'juggler': {
            'same': {'rect': (10, 60), 'parent_mx': 10.399999618530273, 'parent_my': 88, 'mx': 20.399999618530273, 'my': 148, 'extra': (0, 0), 'ow': 514, 'oh': 733, 'sx': 3, 'sy': 3, 'dpr': 1.25, 'dx': 21, 'dy': 148},
            'cross': {'rect': (10, 200), 'parent_mx': 10.399999618530273, 'parent_my': 88, 'mx': 20.799999237060547, 'my': 288, 'extra': (0.4, 0), 'ow': 514, 'oh': 733, 'sx': 3, 'sy': 3, 'dpr': 1.25, 'dx': 20, 'dy': 288},
        },
    },
    '1920x1080-dpr1.5': {
        'maximized': {
            'same': {'rect': (10, 60), 'parent_mx': 0, 'parent_my': 72.66666412353516, 'mx': 10, 'my': 132.6666717529297, 'extra': (0, 0), 'ow': 1295, 'oh': 687, 'sx': -7, 'sy': -7, 'dpr': 1.5, 'dx': 10, 'dy': 133},
            'cross': {'rect': (10, 200), 'parent_mx': 0, 'parent_my': 72.66666412353516, 'mx': 10, 'my': 272.6666564941406, 'extra': (0, 0), 'ow': 1295, 'oh': 687, 'sx': -7, 'sy': -7, 'dpr': 1.5, 'dx': 10, 'dy': 273},
        },
        'juggler': {
            'same': {'rect': (10, 60), 'parent_mx': 10, 'parent_my': 75.33333587646484, 'mx': 20, 'my': 135.3333282470703, 'extra': (0, 0), 'ow': 515, 'oh': 580, 'sx': 3, 'sy': 3, 'dpr': 1.5, 'dx': 20, 'dy': 135},
            'cross': {'rect': (10, 200), 'parent_mx': 10, 'parent_my': 75.33333587646484, 'mx': 20, 'my': 275.3333435058594, 'extra': (0, 0), 'ow': 515, 'oh': 580, 'sx': 3, 'sy': 3, 'dpr': 1.5, 'dx': 20, 'dy': 275},
        },
    },
}

# RETAIL_POPUPS[case][step]: the popup opened from the maximized page after a real click (``popup``: no position asked; ``popup_pos``: left and top asked, only at
# scale 2.0, where retail moves the window up into the work area: screenY 76 for the 80 asked), read from inside the popup. The events' offset (dx, dy) is the
# one of the capture's own mouse points.
RETAIL_POPUPS: Dict[str, Dict[str, Dict[str, Any]]] = {
    '1600x1200-dpr2.0': {
        'popup': {'features': 'width=500,height=400', 'iw': 500, 'ih': 400, 'ow': 513, 'oh': 476, 'sx': 2, 'sy': 2, 'mx': 8.5, 'my': 72, 'dpr': 2, 'dx': 9, 'dy': 72},
        'popup_pos': {'features': 'left=100,top=80,width=500,height=400', 'iw': 500, 'ih': 400, 'ow': 513, 'oh': 476, 'sx': 100, 'sy': 76, 'mx': 106.5, 'my': 146, 'dpr': 2, 'dx': 107, 'dy': 146},
    },
    '1920x1080-dpr1.0': {
        'popup': {'features': 'width=500,height=400', 'iw': 500, 'ih': 400, 'ow': 516, 'oh': 480, 'sx': 4, 'sy': 4, 'mx': 12, 'my': 76, 'dpr': 1, 'dx': 12, 'dy': 76},
    },
    '1920x1080-dpr1.25': {
        'popup': {'features': 'width=500,height=400', 'iw': 500, 'ih': 400, 'ow': 514, 'oh': 478, 'sx': 3, 'sy': 3, 'mx': 10.399999618530273, 'my': 74.4000015258789, 'dpr': 1.25, 'dx': 11, 'dy': 74},
    },
    '1920x1080-dpr1.5': {
        'popup': {'features': 'width=500,height=400', 'iw': 500, 'ih': 400, 'ow': 515, 'oh': 478, 'sx': 3, 'sy': 3, 'mx': 10, 'my': 73.33333587646484, 'dpr': 1.5, 'dx': 10, 'dy': 73},
    },
}


def _stable(page, ask: Optional[Tuple[int, int]], fullscreen: Optional[bool] = None, reader=None) -> Dict[str, Any]:
    """The window as the page reads it once it has settled: the inner size is the asked one (when one was asked), the document is in fullscreen or not
    (when that was asked) and two looks in a row are equal. ``reader`` is the window that is read when it is not ``page`` (a popup, a frame); ``page`` paces the wait.

    A condition, not a pause. The last reading is returned either way, so a window that never settles is red where the assertions name what was read."""
    seen: Dict[str, Any] = {"now": None, "before": None}
    reader = page if reader is None else reader

    def settled() -> bool:
        seen["before"], seen["now"] = seen["now"], reader.evaluate(READ)
        now = seen["now"]
        if ask is not None and (now["iw"], now["ih"]) != ask:
            return False
        if fullscreen is not None and now["fs"] is not fullscreen:
            return False
        return now == seen["before"]

    wait_until(settled, DEADLINE_S, lambda: page.wait_for_timeout(100))
    return seen["now"]


def _probe(page) -> Dict[str, Any]:
    """What ``window.__probe`` answers, reduced to the values RETAIL_PROBE holds (the page gives its own JSON string)."""
    raw = json.loads(page.evaluate("() => window.__probe()"))
    found, client, vv = raw["found"], raw["client"], raw["visualViewport"]
    return {"mq_width": found["width"], "mq_height": found["height"], "mq_aspect_x10000": found["aspect-ratio_x10000"],
            "mq_resolution_x100": found["resolution_x100"], "client_w": client["width"], "client_h": client["height"],
            "vv_w": vv["width"], "vv_h": vv["height"], "vv_scale": vv["scale"], "vv_left": vv["offsetLeft"], "vv_top": vv["offsetTop"],
            "vv_page_left": vv["pageLeft"], "vv_page_top": vv["pageTop"]}


PROBE_NAMES = {"mq_width": "matchMedia width", "mq_height": "matchMedia height", "mq_aspect_x10000": "matchMedia aspect-ratio (x10000)",
               "mq_resolution_x100": "matchMedia resolution (dppx x100)", "client_w": "documentElement.clientWidth",
               "client_h": "documentElement.clientHeight", "vv_w": "visualViewport.width", "vv_h": "visualViewport.height",
               "vv_scale": "visualViewport.scale", "vv_left": "visualViewport.offsetLeft", "vv_top": "visualViewport.offsetTop",
               "vv_page_left": "visualViewport.pageLeft", "vv_page_top": "visualViewport.pageTop"}


def _check_probe(step: str, probe: Dict[str, Any], expected: Dict[str, Any], who: str) -> None:
    """The probe of one step against ``expected`` (retail's values, or the reading before a fullscreen round trip), key by key."""
    assert set(expected) == set(PROBE_NAMES), "%s: the expected probe names %r" % (step, sorted(expected))
    for key, name in PROBE_NAMES.items():
        assert probe[key] == pytest.approx(expected[key], abs=FLOAT), (
            "%s: %s reads %r, %s reads %r (whole probe: %r)" % (step, name, probe[key], who, expected[key], probe))


def _event_offset(page, point: Tuple[int, int], target=None, origin: Tuple[int, int] = (0, 0)) -> Dict[str, Any]:
    """Moves the mouse to ``point`` and gives the last trusted mousemove the page heard for it (waits for it).

    ``target`` is the frame that hears it when it is not the page, and ``origin`` the frame's rect x, y in the page: the event carries ``point`` less ``origin``."""
    target = page if target is None else target
    client = (point[0] - origin[0], point[1] - origin[1])
    heard = target.evaluate("() => window.__moves")
    page.mouse.move(*point)
    wait_until(lambda: target.evaluate("() => window.__moves") > heard and (target.evaluate("() => window.__last") or {}).get("cx") == client[0]
               and (target.evaluate("() => window.__last") or {}).get("cy") == client[1],
               DEADLINE_S, lambda: page.wait_for_timeout(50))
    return target.evaluate("() => window.__last")


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


def _fullscreen_round_trip(page, step: str, before: Dict[str, Any], before_event: Dict[str, Any], before_probe: Dict[str, Any],
                           screen: Tuple[int, int], dpr: float, points: Iterator[Tuple[int, int]]) -> None:
    """Enters fullscreen with a real click, reads the window, leaves it and reads it again: it must be exactly the window of ``before``.

    ``before_probe`` is the probe (media queries, client size, visual viewport) read with ``before``: after leaving fullscreen it is read again, equal.
    ``screen`` is the declared screen in CSS pixels. In fullscreen outer == inner == screen, the window sits at the origin and so does its content."""
    assert before["fs"] is False, "%s: the window is in fullscreen before the test enters it: %r" % (step, before)
    page.mouse.click(*BUTTON)           # the client's own click: a trusted event, so a user gesture
    wait_until(lambda: page.evaluate("() => window.__fsRequest") not in ("not asked", "pending"), DEADLINE_S, lambda: page.wait_for_timeout(50))
    request = page.evaluate("() => window.__fsRequest")
    assert request == "resolved", "%s: requestFullscreen after a real click answers %r, retail enters fullscreen" % (step, request)
    scw, sch = screen
    inside = _stable(page, screen, fullscreen=True)
    inside_event = _event_offset(page, next(points))
    assert inside["fs"] is True and (inside["scw"], inside["sch"]) == screen, (
        "%s: in fullscreen the page reads fullscreenElement set=%r and screen %rx%r, the declared screen is %rx%r (%r)"
        % (step, inside["fs"], inside["scw"], inside["sch"], scw, sch, inside))
    _check_reading("%s, in fullscreen" % step, inside, inside_event,
                   {"ow": scw, "oh": sch, "iw": scw, "ih": sch, "sx": 0, "sy": 0, "mx": 0, "my": 0, "dx": 0, "dy": 0}, dpr)

    page.evaluate("() => { document.exitFullscreen(); }")
    after = _stable(page, (before["iw"], before["ih"]), fullscreen=False)
    after_event = _event_offset(page, next(points))
    assert after["fs"] is False, "%s: the page reads fullscreenElement set after exitFullscreen: %r" % (step, after)
    same = {key: before[key] for key in ("iw", "ih", "ow", "oh", "sx", "sy", "mx", "my")}
    _check_reading("%s, after leaving fullscreen" % step, after, after_event, same, dpr)
    assert after["availH"] == before["availH"], "%s: availHeight reads %r after fullscreen, %r before" % (step, after["availH"], before["availH"])
    _check_probe("%s, after leaving fullscreen" % step, _probe(page), before_probe, "the reading before fullscreen")
    for key in ("dx", "dy"):
        assert abs(after_event[key] - before_event[key]) < EVENT_ROUNDING, (
            "%s: the offset of the events (%s) was %r before fullscreen and is %r after it" % (step, key, before_event[key], after_event[key]))

def _check_events_against_retail(step: str, event: Dict[str, Any], retail: Dict[str, Any], dpr: float) -> None:
    """The offset of the events (``screenX - clientX``, ``screenY - clientY``) against the one of retail's own mouse move: equal at scale 1.0, within one pixel at
    the others (the capture's points are device pixels of its window, ours CSS pixels of the page, and the integer coordinates round by the point)."""
    tolerance = 0 if dpr == 1.0 else 1
    for key, name in (("dx", "screenX - clientX"), ("dy", "screenY - clientY")):
        assert abs(event[key] - retail[key]) <= tolerance, (
            "%s: %s of the mouse event is %r, retail reads %r (event %r)" % (step, name, event[key], retail[key], event))


def _check_frames(page, sites, step: str, case: str, state: str, parent: Dict[str, Any], dpr: float) -> None:
    """A same-origin and a cross-site frame in the page, read from inside: ``outer``, ``screenX/Y`` and ``dpr`` are the parent's, ``mozInnerScreen`` is retail's
    (the parent's plus the frame's rect, and retail's 0.4 CSS pixel in x for the cross-site frame at scale 1.25), and so are the events' offsets.

    ``parent`` is the reading of the page at this step; ``state`` is the key of RETAIL_FRAMES. The frames are added for the reading and removed after it."""
    url = "/frame?%s" % state
    page.evaluate(FRAMES_ON, [sites.url("A", url), sites.url("B", url)])
    try:
        for kind, site in (("same", "A"), ("cross", "B")):
            retail = RETAIL_FRAMES[case][state][kind]
            label = "%s, the %s frame" % (step, "same-origin" if kind == "same" else "cross-site")
            frame = wait_frame(page, sites.origin[site], url, seconds=30)
            reading = _stable(page, (300, 120), reader=frame)
            event = _event_offset(page, FRAME_POINTS[kind], target=frame, origin=retail["rect"])
            _check_reading(label, reading, event,
                           {"iw": 300, "ih": 120, "ow": parent["ow"], "oh": parent["oh"], "sx": parent["sx"], "sy": parent["sy"],
                            "mx": retail["mx"], "my": retail["my"]}, dpr)
            # the parent's content origin plus the frame's rect, plus what retail reads beyond it
            assert reading["mx"] == pytest.approx(parent["mx"] + retail["rect"][0] + retail["extra"][0], abs=FLOAT), (
                "%s: mozInnerScreenX reads %r, the parent's %r plus the rect's %r plus retail's %r is %r"
                % (label, reading["mx"], parent["mx"], retail["rect"][0], retail["extra"][0], parent["mx"] + retail["rect"][0] + retail["extra"][0]))
            assert reading["my"] == pytest.approx(parent["my"] + retail["rect"][1] + retail["extra"][1], abs=FLOAT), (
                "%s: mozInnerScreenY reads %r, the parent's %r plus the rect's %r plus retail's %r is %r"
                % (label, reading["my"], parent["my"], retail["rect"][1], retail["extra"][1], parent["my"] + retail["rect"][1] + retail["extra"][1]))
            assert (reading["ow"], reading["oh"], reading["sx"], reading["sy"], reading["dpr"]) == (
                parent["ow"], parent["oh"], parent["sx"], parent["sy"], parent["dpr"]), (
                "%s: outer, screenX/Y and devicePixelRatio of the frame are not the parent's (frame %r, parent %r)" % (label, reading, parent))
            _check_events_against_retail(label, event, retail, dpr)
    finally:
        page.evaluate(FRAMES_OFF)


def _check_popup(page, sites, step: str, retail: Dict[str, Any], dpr: float) -> None:
    """Opens ``window.open(url, '_blank', features)`` with a real click on the page's button and reads the popup from inside, against retail, exactly.

    The popup is closed after the reading."""
    page.evaluate("([url, features]) => { window.__popupUrl = url; window.__popupFeatures = features; }", [sites.url("A", "/popup"), retail["features"]])
    with page.expect_popup(timeout=30000) as opened:
        page.mouse.click(*OPEN)       # the client's own click: a trusted event, so window.open is allowed
    popup = opened.value
    try:
        popup.wait_for_load_state("load", timeout=30000)
        reading = _stable(popup, (retail["iw"], retail["ih"]))
        event = _event_offset(popup, POPUP_POINT)
        _check_reading(step, reading, event, {key: retail[key] for key in ("iw", "ih", "ow", "oh", "sx", "sy", "mx", "my")}, dpr)
        _check_events_against_retail(step, event, retail, dpr)
    finally:
        popup.close()


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_restored_window_reads_the_retail_frame_after_set_viewport_size(firefox_binary, case):
    from invisible_playwright import InvisiblePlaywright

    spec = CASES[case]
    dpr = spec["dpr"]
    panel = spec["panel"]
    # the declared screen a page reads: the panel over the scale, rounded as the engine rounds it
    screen = (int(panel[0] / dpr + 0.5), int(panel[1] / dpr + 0.5))
    points = iter(POINTS)
    pin = {"screen.width": panel[0], "screen.height": panel[1], "screen.dpr": dpr}
    with Sites() as s:
        s.route("A", "/page", PAGE)
        s.route("A", "/frame", FRAME_PAGE)      # the same-origin frame
        s.route("B", "/frame", FRAME_PAGE)      # the cross-site frame
        s.route("A", "/popup", FRAME_PAGE)
        with InvisiblePlaywright(seed=42, pin=pin, binary_path=firefox_binary, headless=True, humanize=False) as browser:
            page = browser.new_page()
            page.goto(s.url("A", "/page"), wait_until="load", timeout=30000)

            # 1. the window as it opens
            maximized = _stable(page, None)
            maximized_event = _event_offset(page, next(points))
            if "availH" in spec:
                assert maximized["availH"] == spec["availH"], "the compact bar: screen.availHeight reads %r, retail %r" % (maximized["availH"], spec["availH"])
            _check_reading("maximized (%s)" % case, maximized, maximized_event, spec["maximized"], dpr)
            maximized_probe = _probe(page)
            _check_probe("maximized (%s)" % case, maximized_probe, RETAIL_PROBE[case]["maximized"], "retail Firefox 159.0 on Windows")

            # frames, in the maximized window: retail's mozInnerScreen, outer, screen, dpr and event offsets
            _check_frames(page, s, "maximized (%s)" % case, case, "maximized", maximized, dpr)

            # F0. fullscreen and back, before any set_viewport_size: the window it had, exactly
            _fullscreen_round_trip(page, "F0 (%s)" % case, maximized, maximized_event, maximized_probe, screen, dpr, points)

            # popups opened from the maximized page: no position at every case, and the positioned one at scale 2.0, as retail places them
            for popup_step, retail_popup in RETAIL_POPUPS[case].items():
                _check_popup(page, s, "window.open(%r) from the maximized page, %s (%s)" % (retail_popup["features"], popup_step, case), retail_popup, dpr)

            # 2 to 4. set_viewport_size, twice and back to the maximized inner size
            for number, (call, retail_step) in enumerate(zip(spec["calls"], ("juggler", "juggler2", "juggler_back")), start=1):
                width, height = call["size"]
                if number == len(spec["calls"]):
                    assert (width, height) == (maximized["iw"], maximized["ih"]), (
                        "the last call goes back to the maximized inner size %rx%r, the test asks %rx%r"
                        % (maximized["iw"], maximized["ih"], width, height))
                page.set_viewport_size({"width": width, "height": height})
                reading = _stable(page, (width, height))
                event = _event_offset(page, next(points))
                _check_reading("set_viewport_size(%d, %d), call %d (%s)" % (width, height, number, case), reading, event, call, dpr)
                probe = _probe(page)
                _check_probe("set_viewport_size(%d, %d), call %d (%s)" % (width, height, number, case), probe, RETAIL_PROBE[case][retail_step],
                             "retail Firefox 159.0 on Windows")
                if number == 1:
                    # frames again, in the restored window of call 1
                    _check_frames(page, s, "set_viewport_size(%d, %d), call %d (%s)" % (width, height, number, case), case, "juggler", reading, dpr)
                if number == 2:
                    # F1. fullscreen and back after call 2: the window of call 2, exactly
                    _fullscreen_round_trip(page, "F1 (%s)" % case, reading, event, probe, screen, dpr, points)


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
