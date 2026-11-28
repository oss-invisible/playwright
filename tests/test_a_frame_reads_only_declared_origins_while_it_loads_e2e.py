"""A frame reads its ``mozInnerScreen`` only as (0, 0), as the client origin of the window, or as its settled value, from the first line of its script to ``load``.

Retail Firefox 159.0 on Windows, round 7 of the window-geometry capture (RetailCaptures ``captures/WINDOW_GEOMETRY_FRAME_EARLY.csv``, steps ``iframes_max``
and ``iframes_transformed``, columns ``moment``, ``mozInnerScreenX/Y``, ``settled_mozInnerScreenX/Y``, ``client_origin_css_x/y``): in a maximized window every
reading of a frame, at parse, ``DOMContentLoaded``, the first two animation frames and ``load``, is one of
three values and no other:

  * (0, 0): the frame has no place yet (a frame of another process before the parent has sent the first matrix, and any frame before its first layout);
  * the window's client origin, ``client_origin_css_x/y``: (0, 0) in every row of the maximized window (the popup of the same round has (12, 4) at scale 1.0,
    and is not read here);
  * the value the frame settles at.

A reading that is none of them is a value the page can tell from retail: a frame of another process that, before its first matrix, reads the real offset of the
browser chrome in the host's window, or the real window origin, instead of what the declaration gives. Those values are what the host's window has, and they
are not the declared ones.

What it does, per display case (the four of the window tests): a page of the top site loads a frame of the same site and a frame of another site, and the same two
inside a container under ``transform: scale(1.5)`` and ``rotate(10deg)`` (``transform-origin: 0 0``), the six kinds of round 7. The frame's page has an inline
script FIRST in ``<head>``: it records ``mozInnerScreenX/Y`` at parse, at ``DOMContentLoaded``, in the first two ``requestAnimationFrame`` callbacks it asked for at
parse, and at ``load``. Each kind loads three times (a new frame each time: the reading at parse is a race the first load can win by luck). The settled value is the
frame's reading once two looks 100 ms apart are equal, after ``load``. The assertion names the case, the kind, the moment and the three values it allowed.

The engine that answers these values is not released yet: on the engine of the pin this test is RED where the frame reads the real offsets, on purpose. It enters
the suite with the engine release.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest
from _fission_frames import Sites, wait_frame
from _window_origin import CASES, DEADLINE_S, FLOAT, launch_case, settled, wait_for

pytestmark = pytest.mark.e2e

#: The window's client origin in CSS pixels: ``client_origin_css_x/y`` of every ``iframes_max`` and ``iframes_transformed`` row of round 7 (maximized: the client
#: area starts at the window's origin).
CLIENT_ORIGIN: Tuple[float, float] = (0.0, 0.0)
#: The moments of the capture, and the frames each load must have recorded before it is read.
MOMENTS = ("parse", "dcl", "raf1", "raf2", "load")
#: Loads of each kind.
LOADS = 3

TOP = """<!doctype html><meta charset=utf-8><title>early</title>
<style>html,body{margin:0;padding:0;height:100%}</style><body></body>"""

# The page of every frame. The script is the first thing in <head>, as in the capture.
EARLY = """<!doctype html><html><head><script>
window.__early = [];
function rec(moment) { window.__early.push({m: moment, x: mozInnerScreenX, y: mozInnerScreenY}); }
rec('parse');
document.addEventListener('DOMContentLoaded', function () { rec('dcl'); });
requestAnimationFrame(function () { rec('raf1'); requestAnimationFrame(function () { rec('raf2'); }); });
window.addEventListener('load', function () { rec('load'); });
</script><meta charset=utf-8><style>html,body{margin:0;padding:0;height:100%}</style></head><body></body></html>"""

#: A frame in the page, 300x120 at (10, 60) (same site) or (10, 200) (another site), or in a container at (10, 200) under a transform.
ADD = """([src, top, transform]) => {
  const f = document.createElement('iframe');
  f.src = src;
  f.style.cssText = 'display:block;width:300px;height:120px;border:0;margin:0;padding:0';
  const box = document.createElement('div');
  box.className = 'early-box';
  if (transform) {
    box.style.cssText = 'position:fixed;left:10px;top:200px;width:300px;height:120px;margin:0;padding:0;transform-origin:0 0;transform:' + transform;
  } else {
    box.style.cssText = 'position:absolute;left:10px;top:' + top + 'px;width:300px;height:120px;margin:0;padding:0';
  }
  box.appendChild(f);
  document.body.appendChild(box);
}"""
REMOVE = "() => { document.querySelectorAll('.early-box').forEach(b => b.remove()); }"

#: kind -> (site of the frame, top in the page, transform)
KINDS: Dict[str, Tuple[str, int, str]] = {
    "same": ("A", 60, ""),
    "cross": ("B", 200, ""),
    "same_scale": ("A", 200, "scale(1.5)"),
    "cross_scale": ("B", 200, "scale(1.5)"),
    "same_rotate": ("A", 200, "rotate(10deg)"),
    "cross_rotate": ("B", 200, "rotate(10deg)"),
}


def _near(reading: Tuple[float, float], other: Tuple[float, float]) -> bool:
    return abs(reading[0] - other[0]) <= FLOAT and abs(reading[1] - other[1]) <= FLOAT


def _recorded(frame) -> List[Dict[str, Any]]:
    try:
        return frame.evaluate("() => window.__early || []")
    except Exception:       # the frame is between two documents: not recorded yet
        return []


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_frame_reads_only_declared_origins_while_it_loads(firefox_binary, case):
    problems: List[str] = []
    with Sites() as sites:
        sites.route("A", "/page", TOP)
        sites.route("A", "/early", EARLY)
        sites.route("B", "/early", EARLY)
        with launch_case(firefox_binary, case) as browser:
            page = browser.new_page()
            page.goto(sites.url("A", "/page"), wait_until="load", timeout=30000)
            for kind, (site, top, transform) in KINDS.items():
                for load in range(LOADS):
                    path = "/early?%s&load=%d" % (kind, load)
                    page.evaluate(ADD, [sites.url(site, path), top, transform])
                    try:
                        frame = wait_frame(page, sites.origin[site], path, seconds=DEADLINE_S)
                        wait_for(page, lambda: {r["m"] for r in _recorded(frame)} >= set(MOMENTS))
                        records = {r["m"]: r for r in _recorded(frame)}
                        missing = [m for m in MOMENTS if m not in records]
                        if missing:
                            problems.append("%s, %s, load %d: the frame never recorded %s (recorded %r)" % (case, kind, load, missing, _recorded(frame)))
                            continue
                        final = settled(page, lambda: tuple(frame.evaluate("() => [mozInnerScreenX, mozInnerScreenY]")))
                        allowed = [(0.0, 0.0), CLIENT_ORIGIN, final]
                        for moment in MOMENTS:
                            reading = (records[moment]["x"], records[moment]["y"])
                            if not any(_near(reading, a) for a in allowed):
                                problems.append(
                                    "%s, %s frame, load %d, at %s: mozInnerScreen reads (%r, %r); allowed (0, 0), the client origin %r, or the settled value %r"
                                    % (case, kind, load, moment, reading[0], reading[1], CLIENT_ORIGIN, final))
                    finally:
                        page.evaluate(REMOVE)
    assert not problems, "a frame read a value that is not declared while it loaded (%d):\n  %s" % (len(problems), "\n  ".join(problems))
