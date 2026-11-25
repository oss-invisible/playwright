"""The OS cursor is parked outside the browser window, and a gesture that records pointer events checks it.

A headed window that appears under the OS cursor gets a native ``WM_MOUSEMOVE`` for it, and the first native move of a process passes Gecko's filter: the page
sees a ``mousemove`` with ``buttons=0`` at the cursor's place, in the middle of whatever the test records. On a GitHub Windows runner the cursor is at
(512, 384) and the window covers (-8, -8) to (1928, 1040) of a 1920x1080 screen (``test_drag_motion.py::test_a_real_page_sees_a_drag_that_travelled`` saw
``mousemove 512,299 buttons=0`` after the press). Nothing here launches a browser or touches the real cursor: the rectangles are the runner's, the system is a fake.
"""
from __future__ import annotations

import pytest

import _os_cursor
from _os_cursor import (MARGIN, TheCursorCannotBeParked, TheCursorIsInside, cursor_is_outside, park, park_outside, require_outside)

pytestmark = pytest.mark.unit

#: What a GitHub Windows runner shows while a headed browser is up.
RUNNER_SCREEN = (0, 0, 1920, 1080)
RUNNER_WINDOW = (-8, -8, 1928, 1040)
RUNNER_CURSOR = (512, 384)


def _free(point, windows, margin=MARGIN):
    return all(not (w[0] - margin <= point[0] < w[2] + margin and w[1] - margin <= point[1] < w[3] + margin) for w in windows)


def _on_screen(point, screen):
    return screen[0] <= point[0] < screen[2] and screen[1] <= point[1] < screen[3]


class _Desktop:
    """The five answers of the desktop, with a cursor that obeys ``SetCursorPos`` unless ``obeys`` is False."""

    def __init__(self, screen=RUNNER_SCREEN, windows=(RUNNER_WINDOW,), cursor=RUNNER_CURSOR, obeys=True):
        self._screen, self.windows, self.cursor, self.obeys = screen, list(windows), cursor, obeys
        self.asked = []

    def screen(self):
        return self._screen

    def windows_below(self, root_pid):
        return list(self.windows)

    def cursor_position(self):
        return self.cursor

    def set_cursor_position(self, x, y):
        self.asked.append((x, y))
        if self.obeys:
            self.cursor = (x, y)


# ── the arrangement of the runner ────────────────────────────────────────────

def test_the_runners_cursor_is_inside_the_window_the_engine_opens():
    """The arrangement that put a native move into a drag: the cursor on the runner is inside the window, so a window that appears under it is told."""
    assert cursor_is_outside([RUNNER_WINDOW], RUNNER_CURSOR) is False


def test_a_parked_point_is_outside_the_runners_window_with_a_margin():
    point = park_outside([RUNNER_WINDOW], RUNNER_SCREEN)
    assert _on_screen(point, RUNNER_SCREEN)
    assert _free(point, [RUNNER_WINDOW])
    assert cursor_is_outside([RUNNER_WINDOW], point, MARGIN)


def test_without_a_window_the_cursor_goes_to_the_bottom_right_corner():
    """Before the browser starts there is no window to read: the corner where the engine puts none."""
    assert park_outside([], RUNNER_SCREEN) == (1919, 1079)


def test_the_cursor_on_the_edge_of_a_window_is_inside_and_one_pixel_beyond_it_is_outside():
    window = (100, 100, 200, 200)
    assert not cursor_is_outside([window], (100, 100))
    assert not cursor_is_outside([window], (199, 199))
    assert cursor_is_outside([window], (200, 199))
    assert cursor_is_outside([window], (99, 150))


def test_the_margin_widens_the_window_in_every_direction():
    window = (100, 100, 200, 200)
    assert not cursor_is_outside([window], (205, 150), margin=8)
    assert cursor_is_outside([window], (208, 150), margin=8)
    assert not cursor_is_outside([window], (150, 93), margin=8)


# ── where the free point is ──────────────────────────────────────────────────

def test_a_window_on_the_corner_sends_the_cursor_to_another_one():
    window = (1000, 600, 1920, 1080)
    point = park_outside([window], RUNNER_SCREEN)
    assert _on_screen(point, RUNNER_SCREEN) and _free(point, [window])


def test_a_window_that_covers_the_screen_has_no_free_point_and_says_so():
    with pytest.raises(TheCursorCannotBeParked) as caught:
        park_outside([(-8, -8, 1928, 1088)], RUNNER_SCREEN)
    message = str(caught.value)
    assert "Where:" in message and "What:" in message and "Fix:" in message
    assert "shrink the window" in message


def test_a_window_that_leaves_less_than_the_margin_free_has_no_free_point():
    """A strip of 4 pixels is free but the cursor would be 4 pixels from the page: not parked there."""
    with pytest.raises(TheCursorCannotBeParked):
        park_outside([(0, 0, 1920, 1076)], RUNNER_SCREEN)


def test_a_strip_wider_than_the_margin_is_used():
    window = (0, 0, 1920, 1060)
    point = park_outside([window], RUNNER_SCREEN)
    assert _on_screen(point, RUNNER_SCREEN) and _free(point, [window])


def test_two_monitors_leave_the_second_one_free():
    screen = (0, 0, 3840, 1080)
    point = park_outside([RUNNER_WINDOW], screen)
    assert _on_screen(point, screen) and _free(point, [RUNNER_WINDOW])
    assert point[0] > RUNNER_WINDOW[2]


def test_a_virtual_screen_with_a_negative_origin_is_read_in_its_own_coordinates():
    """A monitor left of and above the primary one: the virtual screen starts at a negative point and a window on the primary monitor covers part of it."""
    screen = (-1920, -200, 1920, 1080)
    point = park_outside([RUNNER_WINDOW], screen)
    assert _on_screen(point, screen) and _free(point, [RUNNER_WINDOW])


def test_a_virtual_screen_with_a_negative_origin_and_a_window_on_every_monitor_but_the_strip():
    screen = (-1920, 0, 1920, 1080)
    windows = [(-1920, 0, 0, 1000), (0, 0, 1920, 1000)]
    point = park_outside(windows, screen)
    assert _on_screen(point, screen) and _free(point, windows)
    assert point[1] >= 1000 + MARGIN


def test_several_windows_are_all_avoided():
    windows = [(0, 0, 900, 1080), (1000, 0, 1920, 500), (1000, 600, 1900, 1080)]
    point = park_outside(windows, RUNNER_SCREEN)
    assert _on_screen(point, RUNNER_SCREEN) and _free(point, windows)


def test_an_empty_screen_is_refused_with_a_message():
    with pytest.raises(TheCursorCannotBeParked) as caught:
        park_outside([], (0, 0, 0, 0))
    assert "Fix:" in str(caught.value)


# ── the system calls ─────────────────────────────────────────────────────────

def test_park_moves_the_cursor_out_of_the_window_and_reads_it_back():
    desktop = _Desktop()
    assert not cursor_is_outside(desktop.windows, desktop.cursor)
    point = park(api=desktop)
    assert desktop.asked == [point] and desktop.cursor == point
    assert cursor_is_outside(desktop.windows, desktop.cursor, MARGIN)


def test_park_fails_with_a_message_when_the_system_does_not_move_the_cursor():
    desktop = _Desktop(obeys=False)
    with pytest.raises(TheCursorCannotBeParked) as caught:
        park(api=desktop)
    message = str(caught.value)
    assert "Where:" in message and "What:" in message and "Fix:" in message
    assert "interactive desktop" in message


def test_the_precondition_fails_on_the_runners_arrangement():
    """The red of the e2e: the cursor at (512, 384) inside the window, nothing parked it."""
    with pytest.raises(TheCursorIsInside) as caught:
        require_outside(api=_Desktop())
    assert "(512, 384)" in str(caught.value)


def test_the_precondition_passes_once_the_cursor_is_parked():
    desktop = _Desktop()
    park(api=desktop)
    require_outside(api=desktop)


def test_the_precondition_looks_at_every_window_of_the_browsers():
    desktop = _Desktop(windows=[RUNNER_WINDOW, (400, 300, 700, 500)], cursor=(2000 - 100, 1079))
    require_outside(api=desktop)
    desktop.cursor = (500, 400)
    with pytest.raises(TheCursorIsInside):
        require_outside(api=desktop)


# ── not Windows ──────────────────────────────────────────────────────────────

def test_without_windows_nothing_is_parked_and_every_precondition_holds(monkeypatch):
    monkeypatch.setattr(_os_cursor, "_is_windows", lambda: False)
    assert park() is None
    require_outside()
    assert cursor_is_outside([RUNNER_WINDOW]) is True
    assert _os_cursor.browser_windows() == []
