"""The X record of the occlusion e2e test, on known inputs and without a display: ``tests/_x_visibility.py``.

On Linux the occlusion test rests its verdict on what X says: the cover above the covered toplevel and the covered one inside it (read back once after
``XRaiseWindow``), and a ``VisibilityNotify FullyObscured`` of the covered toplevel. A wrong reading here would make an "arrangement" red of a good run, or
let a run whose windows were never covered pass as a proof; so the parsing of the event, the judgement of the read-back and the record are fixed here.
"""
from __future__ import annotations

import json

from _x_visibility import (VISIBILITY_NOTIFY, VisibilityRecord, XEvent, XVisibility, arrangement, control_sees_hidden, outer, product_stays_visible,
                           visibility_of)

COVERED, COVER, OTHER = 0x400001, 0x600001, 0x200001
SAME = {"x": 0, "y": 0, "width": 1280, "height": 720, "border": 0}


def an_event(kind, window, state):
    event = XEvent()
    event.xvisibility.type = kind
    event.xvisibility.window = window
    event.xvisibility.state = state
    return event


def test_a_visibility_notify_is_read_with_its_window_and_state():
    assert visibility_of(an_event(VISIBILITY_NOTIFY, COVERED, 2)) == (COVERED, "FullyObscured")
    assert visibility_of(an_event(VISIBILITY_NOTIFY, COVERED, 1)) == (COVERED, "PartiallyObscured")
    assert visibility_of(an_event(VISIBILITY_NOTIFY, COVERED, 0)) == (COVERED, "Unobscured")


def test_another_event_is_not_a_visibility_and_an_unknown_state_is_named_by_its_number():
    assert visibility_of(an_event(VISIBILITY_NOTIFY - 1, COVERED, 2)) is None
    assert visibility_of(an_event(22, COVERED, 2)) is None
    assert visibility_of(an_event(VISIBILITY_NOTIFY, COVERED, 7)) == (COVERED, "state 7")


def test_the_union_has_the_size_of_xlibs_xevent():
    import ctypes

    assert ctypes.sizeof(XEvent) == 24 * ctypes.sizeof(ctypes.c_long)


def test_a_cover_above_with_the_same_rectangle_is_the_arrangement():
    seen = arrangement([OTHER, COVERED, COVER], COVERED, COVER, {COVERED: SAME, COVER: SAME})
    assert seen["ok"] and seen["cover above"] and seen["inside"]
    assert (seen["covered position"], seen["cover position"]) == (1, 2)


def test_a_cover_below_the_covered_window_is_not():
    seen = arrangement([COVER, COVERED, OTHER], COVERED, COVER, {COVERED: SAME, COVER: SAME})
    assert not seen["ok"] and not seen["cover above"] and seen["inside"]


def test_a_cover_that_leaves_a_strip_of_the_covered_window_is_not():
    smaller = dict(SAME, width=1279)
    moved = dict(SAME, y=1)
    for cover in (smaller, moved):
        seen = arrangement([COVERED, COVER], COVERED, COVER, {COVERED: SAME, COVER: cover})
        assert not seen["ok"] and seen["cover above"] and not seen["inside"], cover


def test_the_border_counts_in_the_rectangle():
    assert outer(dict(SAME, x=10, y=20, border=2)) == (10, 20, 1294, 744)
    bordered = dict(SAME, border=1)
    assert arrangement([COVERED, COVER], COVERED, COVER, {COVERED: SAME, COVER: bordered})["ok"]
    assert not arrangement([COVERED, COVER], COVERED, COVER, {COVERED: bordered, COVER: SAME})["ok"]


def test_a_window_missing_from_the_tree_or_without_a_geometry_is_not_the_arrangement():
    assert not arrangement([COVERED], COVERED, COVER, {COVERED: SAME, COVER: SAME})["ok"]
    assert not arrangement([COVERED, COVER], COVERED, COVER, {COVERED: SAME, COVER: None})["ok"]
    assert not arrangement([], COVERED, COVER, {})["ok"]


def test_the_record_keeps_only_the_watched_window_in_order_with_its_times():
    record = VisibilityRecord(COVERED)
    record.add(OTHER, "FullyObscured", 0.1)
    assert record.first() is None
    assert record.line() == "X sent window 0x400001 no VisibilityNotify"
    record.add(COVERED, "PartiallyObscured", 0.25)
    record.add(COVERED, "FullyObscured", 0.31234)
    record.add(COVERED, "Unobscured", 1.5)
    assert record.first() == {"state": "FullyObscured", "t": 0.3123}
    assert record.line() == "X sent window 0x400001: PartiallyObscured at +0.250 s, FullyObscured at +0.312 s, Unobscured at +1.500 s"
    saved = record.as_dict()
    assert saved["fully obscured arrived"] is True and saved["fully obscured at"] == 0.3123 and saved["window"] == "0x400001"
    assert json.loads(json.dumps(saved)) == saved


def test_a_record_without_fully_obscured_says_it_did_not_arrive():
    record = VisibilityRecord(COVERED)
    record.add(COVERED, "PartiallyObscured", 0.2)
    saved = record.as_dict()
    assert saved["fully obscured arrived"] is False and saved["fully obscured at"] is None


# --- the verdicts of the two halves, on the sequences of changes a page can record -----------------------------------------------------------------

# The control's criterion before: the state the page reads at the end of the run, which is the last of the changes it recorded.
def the_old_control_passes(changes):
    return changes[-1] == "hidden"


# Firefox draws the covered window again after X's FullyObscured (``nsWindow::OnExposeEvent``: VISIBLE at every draw): hidden, then visible again.
DRAWN_AFTER = ["visible", "hidden", "visible"]


def test_a_covered_window_drawn_again_ends_visible_and_the_old_criterion_is_red_on_it_while_the_new_one_is_green():
    assert the_old_control_passes(["visible", "hidden"]) is True
    assert the_old_control_passes(DRAWN_AFTER) is False
    assert control_sees_hidden(["visible", "hidden"]) is True
    assert control_sees_hidden(DRAWN_AFTER) is True


def test_the_control_is_red_when_the_page_never_recorded_hidden():
    assert control_sees_hidden(["visible"]) is False
    assert control_sees_hidden(["visible", "visible"]) is False
    assert control_sees_hidden([]) is False


def test_a_hidden_before_the_last_look_that_had_no_fully_obscured_is_not_its_effect():
    # the page had recorded hidden when X had still said nothing (3 entries read before the event arrived): only what comes after counts
    assert control_sees_hidden(["visible", "hidden", "visible"], not_before=3) is False
    assert control_sees_hidden(["visible", "hidden", "visible", "hidden"], not_before=3) is True
    assert control_sees_hidden(["visible", "hidden"], not_before=1) is True
    assert control_sees_hidden(["visible", "hidden"], not_before=2) is False


def test_the_product_half_is_red_as_soon_as_hidden_shows():
    assert product_stays_visible(["visible"]) is True
    assert product_stays_visible(["visible", "hidden"]) is False
    assert product_stays_visible(DRAWN_AFTER) is False
    assert product_stays_visible([]) is False


class _Xlib:
    def __init__(self):
        self.resized = []

    def XResizeWindow(self, connection, window, width, height):
        self.resized.append((window, width, height))

    def XSync(self, connection, discard):
        pass


def test_shrink_by_one_asks_x_for_one_pixel_less_in_width_and_keeps_the_height():
    x = object.__new__(XVisibility)
    x.x11, x.connection = _Xlib(), object()
    sizes = iter([{"width": 1280, "height": 720}, {"width": 1279, "height": 720}])
    x.geometry = lambda window: next(sizes)
    assert x.shrink_by_one(COVERED) == {"before": {"width": 1280, "height": 720}, "after": {"width": 1279, "height": 720}}
    assert x.x11.resized == [(COVERED, 1279, 720)]
