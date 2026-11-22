"""The X record of the occlusion e2e test, on known inputs and without a display: ``tests/_x_visibility.py``.

On Linux the occlusion test rests its verdict on what X says: the cover above the covered toplevel and the covered one inside it (read back once after
``XRaiseWindow``), and a ``VisibilityNotify FullyObscured`` of the covered toplevel. A wrong reading here would make an "arrangement" red of a good run, or
let a run whose windows were never covered pass as a proof; so the parsing of the event, the judgement of the read-back and the record are fixed here.
"""
from __future__ import annotations

import json

from _x_visibility import VISIBILITY_NOTIFY, VisibilityRecord, XEvent, arrangement, outer, visibility_of

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
