"""What the window-origin e2e files share: the four display cases, a browser pinned to one of them, and waits on a reading of a page.

The four cases are the ones of the window tests against retail (``test_the_maximized_window_frames_and_popups_read_retail_e2e.py``): scale 1.0, 1.25 and 1.5
on a 1920x1080 panel (device pixels) and 2.0 on a 1600x1200 one. The engine declares the window, so the page reads the same on Linux and on Windows.

Waits are CONDITIONS with a deadline (``_waits``), never a pause; ``observe`` is the one bounded look at a value that must NOT change, and it says so.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Tuple

from _waits import wait_until

#: The deadline of a wait for something that must happen, in seconds.
DEADLINE_S = 30
#: Floats of a page (``mozInnerScreenY`` 72.667 is 72.6666...).
FLOAT = 0.001

#: case id -> (display scale, panel in device pixels).
CASES: Dict[str, Tuple[float, Tuple[int, int]]] = {
    "1920x1080-dpr1.0": (1.0, (1920, 1080)),
    "1920x1080-dpr1.25": (1.25, (1920, 1080)),
    "1920x1080-dpr1.5": (1.5, (1920, 1080)),
    "1600x1200-dpr2.0": (2.0, (1600, 1200)),
}


def launch_case(firefox_binary: str, case: str, **extra: Any):
    """The product on one case: seed 42, the panel and the scale pinned, headless, no humanizing. ``extra`` is for the keywords a test needs on top
    (``extra_args``, ``extra_prefs``)."""
    from invisible_playwright import InvisiblePlaywright

    dpr, panel = CASES[case]
    pin = {"screen.width": panel[0], "screen.height": panel[1], "screen.dpr": dpr}
    return InvisiblePlaywright(seed=42, pin=pin, binary_path=firefox_binary, headless=True, humanize=False, **extra)


def settled(page, read: Callable[[], Any], seconds: float = DEADLINE_S) -> Any:
    """``read()`` once two looks in a row, 100 ms apart, give the same value (or the last one at the deadline: a window that never settles is red where
    the caller's assertion names what was read)."""
    seen: Dict[str, Any] = {"now": None, "before": None}

    def look() -> bool:
        seen["before"], seen["now"] = seen["now"], read()
        return seen["now"] == seen["before"]

    wait_until(look, seconds, lambda: page.wait_for_timeout(100))
    return seen["now"]


def wait_for(page, condition: Callable[[], Any], seconds: float = DEADLINE_S) -> bool:
    """Until ``condition()`` is true, reading the client's events between two looks; False at the deadline."""
    return wait_until(condition, seconds, lambda: page.wait_for_timeout(100))


def observe(page, read: Callable[[], Any], seconds: float) -> List[Any]:
    """The distinct values ``read()`` gives over ``seconds``, in the order they were seen (the first look is always taken).

    The ONE bounded look of these files: for a value that must stay as it is (a window after ``resizeBy(0, 0)``, a frame that must not have loaded yet).
    A change that comes later than ``seconds`` is not seen; a change inside it always is, so the check can be late to catch a bug and never red for nothing."""
    values: List[Any] = []
    end = time.monotonic() + seconds
    while True:
        value = read()
        if not values or values[-1] != value:
            values.append(value)
        if time.monotonic() >= end:
            return values
        page.wait_for_timeout(100)
