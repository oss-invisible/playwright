"""The X record of the occlusion e2e test, on known inputs and without a display: ``tests/_x_visibility.py``.

On Linux the occlusion test rests its verdict on what X says: the cover above the covered toplevel and the covered one inside it (read back once after
``XRaiseWindow``), and a ``VisibilityNotify FullyObscured`` of the covered toplevel. A wrong reading here would make an "arrangement" red of a good run, or
let a run whose windows were never covered pass as a proof; so the parsing of the event, the judgement of the read-back and the record are fixed here.

The cover is a window of the test's own, an override-redirect one that never takes the focus, and the pointer is kept out of its way: with no window manager
the focus follows the pointer, a window mapped over the pointer makes it leave the covered toplevel, and GDK turns that crossing into a focus-out that makes GTK
draw the window again after ``FullyObscured`` (hunt run 45603211231; the reading "a second session took the focus" was refuted by run 45620136865). So the
crossing and focus events are fixed here too: how they are read, kept with their time and shown in the line of the record; the precondition "the covering moved
neither the pointer nor the focus" judged on a record with a ``LeaveNotify`` (red), a ``FocusOut`` (red), a pointer read back inside a rectangle (red) and
otherwise (green); the cover that leaves the pointer outside, and the error when the pointer is inside the covered window; the warp of the pointer to the
corner computed from the root's geometry; how the cover is made (override-redirect, rectangle, no ``XSetInputFocus``); and that the engine's log of a test
names a file per process (``%PID``).
"""
from __future__ import annotations

import ctypes
import json
import os
import pathlib

import pytest

from _evidence_on_failure import MOZ_LOG_BASE, FailureEvidence
from _x_visibility import (COVER_MARGIN, CW_OVERRIDE_REDIRECT, ENTER_NOTIFY, ENTER_WINDOW_MASK, FOCUS_CHANGE_MASK, FOCUS_IN, FOCUS_OUT, LEAVE_NOTIFY,
                           LEAVE_WINDOW_MASK, VISIBILITY_CHANGE_MASK, VISIBILITY_NOTIFY, WATCH_MASK, VisibilityRecord, XCrossingEvent, XEvent,
                           XVisibility, arrangement, control_sees_hidden, corner_of, cover_rectangle, crossing_of, focus_of, outer,
                           pointer_or_focus_moved, product_stays_visible, visibility_of)

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


# --- the focus: the precondition of the arrangement ---------------------------------------------------------------------------------------------------

def a_focus_event(kind, window, mode=0, detail=3):
    event = XEvent()
    event.xfocus.type = kind
    event.xfocus.window = window
    event.xfocus.mode = mode
    event.xfocus.detail = detail
    return event


def test_a_focus_event_is_read_with_its_window_kind_mode_and_detail():
    assert FOCUS_IN == 9 and FOCUS_OUT == 10 and FOCUS_CHANGE_MASK == 1 << 21 and VISIBILITY_CHANGE_MASK == 1 << 16
    assert focus_of(a_focus_event(FOCUS_OUT, COVERED)) == (COVERED, "FocusOut", "NotifyNormal", "NotifyNonlinear")
    assert focus_of(a_focus_event(FOCUS_IN, COVER, mode=3, detail=5)) == (COVER, "FocusIn", "NotifyWhileGrabbed", "NotifyPointer")
    assert focus_of(a_focus_event(FOCUS_OUT, COVERED, mode=9, detail=9)) == (COVERED, "FocusOut", "mode 9", "detail 9")
    assert focus_of(an_event(VISIBILITY_NOTIFY, COVERED, 2)) is None
    assert visibility_of(a_focus_event(FOCUS_OUT, COVERED)) is None


def test_the_record_keeps_the_focus_events_of_the_watched_window_with_their_times_and_shows_them():
    record = VisibilityRecord(COVERED)
    record.add_focus(OTHER, "FocusOut", "NotifyNormal", "NotifyNonlinear", 0.1)
    assert record.focus == [] and record.focus_out() is None
    record.add(COVERED, "PartiallyObscured", 0.25)
    record.add_focus(COVERED, "FocusIn", "NotifyNormal", "NotifyAncestor", 0.26)
    record.add_focus(COVERED, "FocusOut", "NotifyNormal", "NotifyNonlinear", 0.31234)
    assert record.focus_out() == {"event": "FocusOut", "mode": "NotifyNormal", "detail": "NotifyNonlinear", "t": 0.3123}
    assert record.line() == ("X sent window 0x400001: PartiallyObscured at +0.250 s; focus: FocusIn at +0.260 s (NotifyNormal, NotifyAncestor), "
                             "FocusOut at +0.312 s (NotifyNormal, NotifyNonlinear)")
    saved = record.as_dict()
    assert saved["focus out arrived"] is True and saved["focus out at"] == 0.3123 and len(saved["focus events"]) == 2
    assert json.loads(json.dumps(saved)) == saved


def test_a_record_without_focus_events_says_so_and_its_line_is_the_one_of_the_visibility_alone():
    record = VisibilityRecord(COVERED)
    record.add(COVERED, "FullyObscured", 0.2)
    assert record.line() == "X sent window 0x400001: FullyObscured at +0.200 s"
    saved = record.as_dict()
    assert saved["focus out arrived"] is False and saved["focus out at"] is None and saved["focus events"] == []
    assert VisibilityRecord(COVERED).line() == "X sent window 0x400001 no VisibilityNotify"


def a_crossing_event(kind, window, mode=0, detail=3, x_root=0, y_root=0):
    event = XEvent()
    event.xcrossing.type = kind
    event.xcrossing.window = window
    event.xcrossing.mode = mode
    event.xcrossing.detail = detail
    event.xcrossing.x_root = x_root
    event.xcrossing.y_root = y_root
    return event


def test_a_crossing_event_is_read_with_its_window_kind_mode_detail_and_root_position():
    assert ENTER_NOTIFY == 7 and LEAVE_NOTIFY == 8 and ENTER_WINDOW_MASK == 1 << 4 and LEAVE_WINDOW_MASK == 1 << 5
    assert crossing_of(a_crossing_event(LEAVE_NOTIFY, COVERED, 0, 3, 960, 540)) == (COVERED, "LeaveNotify", "NotifyNormal", "NotifyNonlinear", 960, 540)
    assert crossing_of(a_crossing_event(ENTER_NOTIFY, COVER, 1, 0)) == (COVER, "EnterNotify", "NotifyGrab", "NotifyAncestor", 0, 0)
    assert crossing_of(a_focus_event(FOCUS_OUT, COVERED)) is None and focus_of(a_crossing_event(LEAVE_NOTIFY, COVERED)) is None
    assert visibility_of(a_crossing_event(LEAVE_NOTIFY, COVERED)) is None


def test_the_crossing_member_has_xlibs_layout():
    offsets = {name: getattr(XCrossingEvent, name).offset for name, _ in XCrossingEvent._fields_}
    word = ctypes.sizeof(ctypes.c_long)
    if word == 8:      # LP64: the layout of Xlib's XCrossingEvent
        assert offsets["window"] == 32 and offsets["root"] == 40 and offsets["subwindow"] == 48 and offsets["time"] == 56
        assert offsets["x"] == 64 and offsets["x_root"] == 72 and offsets["mode"] == 80 and offsets["detail"] == 84 and offsets["state"] == 96
    assert ctypes.sizeof(XEvent) == 24 * word


def test_the_record_keeps_the_crossings_of_the_watched_window_with_their_times_and_shows_them():
    record = VisibilityRecord(COVERED)
    record.add_crossing(OTHER, "LeaveNotify", "NotifyNormal", "NotifyNonlinear", 0.1, 1, 2)
    assert record.crossings == []
    record.add_crossing(COVERED, "LeaveNotify", "NotifyNormal", "NotifyNonlinear", 0.31234, 960, 540)
    record.add_crossing(COVERED, "EnterNotify", "NotifyNormal", "NotifyAncestor", 0.4, 10, 11)
    assert record.crossings[0] == {"event": "LeaveNotify", "mode": "NotifyNormal", "detail": "NotifyNonlinear", "t": 0.3123, "at": [960, 540]}
    assert record.line() == ("X sent window 0x400001 no VisibilityNotify; pointer: LeaveNotify at +0.312 s (NotifyNormal, NotifyNonlinear), "
                             "EnterNotify at +0.400 s (NotifyNormal, NotifyAncestor)")
    saved = record.as_dict()
    assert len(saved["crossings"]) == 2 and saved["pointer read back"] is None
    assert json.loads(json.dumps(saved)) == saved


def a_pointer(at, covered=True, cover=True):
    return {"at": list(at), "outside covered": covered, "outside cover": cover}


def a_calm_record():
    record = VisibilityRecord(COVERED)
    record.add(COVERED, "PartiallyObscured", 0.25)
    record.add(COVERED, "FullyObscured", 0.31)
    record.pointer = a_pointer((1919, 1079))
    return record


def test_the_precondition_is_green_on_a_calm_record():
    kept = a_calm_record()
    assert pointer_or_focus_moved(kept) == ""
    # the events of another window and no record at all do not say the covered toplevel was crossed or lost the focus
    kept.add_focus(OTHER, "FocusOut", "NotifyNormal", "NotifyNonlinear", 0.2)
    kept.add_crossing(OTHER, "LeaveNotify", "NotifyNormal", "NotifyNonlinear", 0.2)
    assert pointer_or_focus_moved(kept) == ""
    assert pointer_or_focus_moved(None) == ""


def test_the_precondition_is_red_on_a_leave_notify_of_the_covered_toplevel():
    moved = a_calm_record()
    moved.add_crossing(COVERED, "LeaveNotify", "NotifyNormal", "NotifyNonlinear", 0.2501, 960, 540)
    message = pointer_or_focus_moved(moved)
    assert message.startswith("arrangement: the covering moved the pointer or the focus")
    assert moved.line() in message and "LeaveNotify at +0.250 s" in message


def test_the_precondition_is_red_on_an_enter_notify_and_on_a_focus_out_or_a_focus_in():
    for add in (lambda r: r.add_crossing(COVERED, "EnterNotify", "NotifyNormal", "NotifyAncestor", 0.2),
                lambda r: r.add_focus(COVERED, "FocusOut", "NotifyNormal", "NotifyNonlinear", 0.2501),
                lambda r: r.add_focus(COVERED, "FocusIn", "NotifyNormal", "NotifyAncestor", 0.1)):
        moved = a_calm_record()
        add(moved)
        message = pointer_or_focus_moved(moved)
        assert message.startswith("arrangement: the covering moved the pointer or the focus"), message
        assert moved.line() in message


def test_the_precondition_is_red_on_a_pointer_read_back_inside_the_covered_window_or_the_cover():
    for pointer in (a_pointer((100, 100), covered=False, cover=False), a_pointer((10, 10), covered=True, cover=False)):
        moved = a_calm_record()
        moved.pointer = pointer
        message = pointer_or_focus_moved(moved)
        assert message.startswith("arrangement: the covering moved the pointer or the focus"), message
        assert "the pointer read back at %r" % (pointer["at"],) in message


class _FullXlib(_Xlib):
    """The calls of libX11 that these cases need, recorded; there is no ``XSetInputFocus`` here, so a call to it is an error of the code under test."""

    def __init__(self):
        super().__init__()
        self.calls, self.created, self.attributes, self.masks, self.queue = [], [], [], [], []
        self.pointer, self.warps = (1919, 1079), []

    def XSelectInput(self, connection, window, mask):
        self.masks.append((window, mask))

    def XPending(self, connection):
        return len(self.queue)

    def XNextEvent(self, connection, reference):
        event = self.queue.pop(0)
        ctypes.memmove(ctypes.addressof(reference._obj), ctypes.addressof(event), ctypes.sizeof(XEvent))

    def XCreateSimpleWindow(self, connection, parent, x, y, width, height, border, border_pixel, background):
        self.created.append({"parent": parent, "rect": (x, y, width, height), "border": border})
        return 0x800001

    def XChangeWindowAttributes(self, connection, window, mask, reference):
        self.attributes.append({"window": window, "mask": mask, "override_redirect": reference._obj.override_redirect})

    def XMapWindow(self, connection, window):
        self.calls.append(("map", window))

    def XRaiseWindow(self, connection, window):
        self.calls.append(("raise", window))

    def XDestroyWindow(self, connection, window):
        self.calls.append(("destroy", window))

    def XWarpPointer(self, connection, source, destination, source_x, source_y, source_width, source_height, x, y):
        self.warps.append({"source": source, "destination": destination, "from": (source_x, source_y, source_width, source_height), "to": (x, y)})
        self.pointer = (x, y)

    def XQueryPointer(self, connection, window, root, child, root_x, root_y, win_x, win_y, mask):
        root_x._obj.value, root_y._obj.value = self.pointer
        return 1


def an_x_connection(xlib=None):
    x = object.__new__(XVisibility)
    x.x11, x.connection, x.root = xlib or _FullXlib(), object(), 0x1
    x.record, x.started = None, 0.0
    return x


def test_drain_keeps_the_crossing_and_focus_events_of_the_watched_window_and_the_visibility_ones_in_order():
    x = an_x_connection()
    x.watch(COVERED)
    x.x11.queue = [an_event(VISIBILITY_NOTIFY, COVERED, 1), a_crossing_event(LEAVE_NOTIFY, COVERED, 0, 3, 960, 540), a_focus_event(FOCUS_OUT, COVERED),
                   an_event(VISIBILITY_NOTIFY, COVERED, 2), a_focus_event(FOCUS_OUT, OTHER), a_crossing_event(ENTER_NOTIFY, OTHER)]
    record = x.drain()
    assert [event["state"] for event in record.events] == ["PartiallyObscured", "FullyObscured"]
    assert [(event["event"], event["detail"]) for event in record.focus] == [("FocusOut", "NotifyNonlinear")]
    assert [(event["event"], event["at"]) for event in record.crossings] == [("LeaveNotify", [960, 540])]
    assert pointer_or_focus_moved(record).startswith("arrangement: the covering moved the pointer or the focus")


def test_watch_selects_the_focus_and_crossing_events_together_with_the_visibility_ones():
    x = an_x_connection()
    x.watch(COVERED)
    assert WATCH_MASK == VISIBILITY_CHANGE_MASK | FOCUS_CHANGE_MASK | ENTER_WINDOW_MASK | LEAVE_WINDOW_MASK
    assert x.x11.masks == [(COVERED, WATCH_MASK)]
    mask = x.x11.masks[0][1]
    assert mask & (1 << 16) and mask & (1 << 21) and mask & (1 << 4) and mask & (1 << 5)


SCREEN = {"width": 1920, "height": 1080}


def test_the_cover_is_larger_than_the_covered_window_on_every_side_and_stays_in_the_screen():
    covered = {"x": 100, "y": 50, "width": 1280, "height": 720, "border": 0}
    assert COVER_MARGIN == 32
    assert cover_rectangle(covered, SCREEN) == (68, 18, 1344, 784)
    assert cover_rectangle(dict(covered, border=2), SCREEN, 10) == (90, 40, 1304, 744)
    left, top, width, height = cover_rectangle(covered, SCREEN)
    cover = {"x": left, "y": top, "width": width, "height": height, "border": 0}
    assert arrangement([COVERED, COVER], COVERED, COVER, {COVERED: covered, COVER: cover})["ok"]


def test_a_cover_that_the_screen_cuts_keeps_the_covered_windows_own_side_and_never_less_than_the_covered_rectangle():
    at_the_corner = {"x": 0, "y": 0, "width": 1920, "height": 1080, "border": 0}
    assert cover_rectangle(at_the_corner, SCREEN) == (0, 0, 1920, 1080)
    near = {"x": 10, "y": 10, "width": 1900, "height": 1060, "border": 0}
    assert cover_rectangle(near, SCREEN) == (0, 0, 1920, 1080)
    outside = {"x": -20, "y": -5, "width": 2000, "height": 1100, "border": 0}      # a window larger than the screen and partly off it
    assert cover_rectangle(outside, SCREEN) == (-20, -5, 2000, 1100)


def test_a_cover_leaves_the_pointer_outside_it_where_the_margin_would_include_it():
    pointer = (1919, 1079)              # the bottom-right pixel of a 1920x1080 root
    near = {"x": 600, "y": 300, "width": 1300, "height": 760, "border": 0}      # its right edge is 1900 and its bottom 1060: the margins reach the pointer
    assert cover_rectangle(near, SCREEN) == (568, 268, 1352, 812)
    left, top, width, height = cover_rectangle(near, SCREEN, pointer=pointer)
    assert not (left <= pointer[0] < left + width and top <= pointer[1] < top + height)
    assert (left, top) == (568, 268) and left + width == 1919          # the right margin shrank to the pointer, the others stay
    assert left <= 600 and top <= 300 and left + width >= 1900 and top + height >= 1060            # the covered rectangle is still inside
    # a pointer on the left or above the covered window is kept out on that side
    at_the_top_left = {"x": 20, "y": 20, "width": 800, "height": 600, "border": 0}
    x, y, width, height = cover_rectangle(at_the_top_left, SCREEN, pointer=(0, 0))
    assert (x, y) == (1, 0) and x + width == 852 and y + height == 652
    assert cover_rectangle(at_the_top_left, SCREEN, pointer=(20, 0))[:2] == (0, 1)
    # a pointer already outside the margins changes nothing
    assert cover_rectangle(near, SCREEN, pointer=(0, 0)) == cover_rectangle(near, SCREEN)


def test_the_pointer_inside_the_covered_window_is_an_arrangement_error_with_no_free_point():
    covered = {"x": 0, "y": 0, "width": 1920, "height": 1080, "border": 0}      # a covered window that fills the screen
    with pytest.raises(AssertionError, match="^arrangement: the pointer is inside the covered window, no free point on the screen"):
        cover_rectangle(covered, SCREEN, pointer=(1919, 1079))
    xlib = _FullXlib()
    x = an_x_connection(xlib)
    geometries = {COVERED: dict(covered), 0x1: dict(SCREEN, x=0, y=0, border=0)}
    x.geometry = lambda window: geometries[window]
    with pytest.raises(AssertionError, match="^arrangement: the pointer is inside the covered window, no free point on the screen"):
        x.cover_window(COVERED)
    assert xlib.created == [] and xlib.calls == []             # nothing was made


def test_the_pointer_is_warped_to_the_bottom_right_pixel_of_the_roots_own_geometry():
    for width, height in ((1920, 1080), (1280, 720), (800, 600)):
        xlib = _FullXlib()
        x = an_x_connection(xlib)
        x.geometry = lambda window, w=width, h=height: dict(width=w, height=h, x=0, y=0, border=0)
        assert corner_of({"width": width, "height": height}) == (width - 1, height - 1)
        assert x.warp_pointer_to_the_corner() == (width - 1, height - 1)
        assert xlib.warps == [{"source": 0, "destination": 0x1, "from": (0, 0, 0, 0), "to": (width - 1, height - 1)}]
        assert x.warped == {"to": [width - 1, height - 1], "root": [width, height], "read back": [width - 1, height - 1]}


def test_the_pointer_is_read_back_once_after_the_cover_and_kept_in_the_record():
    xlib = _FullXlib()
    x = an_x_connection(xlib)
    x.record = VisibilityRecord(COVERED)
    geometries = {COVERED: {"x": 100, "y": 50, "width": 1280, "height": 720, "border": 0}, 0x800001: {"x": 68, "y": 18, "width": 1344, "height": 784, "border": 0}}
    x.geometry = lambda window: geometries[window]
    outside = x.read_pointer_back(COVERED, 0x800001)
    assert outside["at"] == [1919, 1079] and outside["outside covered"] is True and outside["outside cover"] is True
    assert x.record.pointer is outside and pointer_or_focus_moved(x.record) == ""
    xlib.pointer = (500, 300)       # inside both rectangles
    inside = x.read_pointer_back(COVERED, 0x800001)
    assert inside["outside covered"] is False and inside["outside cover"] is False
    assert pointer_or_focus_moved(x.record).startswith("arrangement: the covering moved the pointer or the focus")


def test_the_cover_is_an_override_redirect_window_mapped_and_raised_and_nothing_asks_for_the_focus():
    xlib = _FullXlib()
    x = an_x_connection(xlib)
    geometries = {COVERED: {"x": 100, "y": 50, "width": 1280, "height": 720, "border": 0}, 0x1: dict(SCREEN, x=0, y=0, border=0)}
    x.geometry = lambda window: geometries[window]
    cover = x.cover_window(COVERED)
    assert cover == 0x800001
    assert xlib.created == [{"parent": 0x1, "rect": (68, 18, 1344, 784), "border": 0}]
    assert xlib.attributes == [{"window": cover, "mask": CW_OVERRIDE_REDIRECT | (1 << 1), "override_redirect": 1}]
    assert xlib.calls == [("map", cover), ("raise", cover)]
    assert not hasattr(xlib, "XSetInputFocus")          # the fake has none: the call would have raised
    x.destroy_window(cover)
    assert xlib.calls[-1] == ("destroy", cover)


def test_a_cover_window_without_the_covered_geometry_is_an_arrangement_red():
    x = an_x_connection()
    x.geometry = lambda window: None
    with pytest.raises(AssertionError, match="^arrangement: the geometry of the covered toplevel"):
        x.cover_window(COVERED)


# --- the precondition as the e2e test asserts it ------------------------------------------------------------------------------------------------------

def the_covering_on_x(record):
    import test_window_occlusion_e2e as e2e

    class FakeX:
        warped = {"to": [1919, 1079]}

        def drain(self):
            return record

    covering = e2e.CoveringOnX(FakeX())
    covering.visibility = record
    covering.first = COVERED
    covering.pointer = record.pointer
    return covering


def test_the_test_is_red_with_the_arrangement_message_when_the_covered_toplevel_was_crossed_or_lost_the_focus():
    for add in (lambda r: r.add_crossing(COVERED, "LeaveNotify", "NotifyNormal", "NotifyNonlinear", 0.2501, 960, 540),
                lambda r: r.add_focus(COVERED, "FocusOut", "NotifyNormal", "NotifyNonlinear", 0.2501)):
        record = a_calm_record()
        add(record)
        with pytest.raises(AssertionError) as red:
            the_covering_on_x(record).assert_the_pointer_and_the_focus_did_not_move()
        assert str(red.value).startswith("arrangement: the covering moved the pointer or the focus")
        assert "at +0.250 s" in str(red.value) and "focus out arrived" in str(red.value)


def test_the_test_is_red_when_the_pointer_read_back_is_inside_or_was_never_read():
    record = a_calm_record()
    record.pointer = a_pointer((100, 100), covered=False, cover=False)
    with pytest.raises(AssertionError, match="^arrangement: the covering moved the pointer or the focus: the pointer read back at \\[100, 100\\]"):
        the_covering_on_x(record).assert_the_pointer_and_the_focus_did_not_move()
    record = a_calm_record()
    covering = the_covering_on_x(record)
    covering.pointer = None
    with pytest.raises(AssertionError, match="^arrangement: the covering moved the pointer or the focus: the pointer was never read back"):
        covering.assert_the_pointer_and_the_focus_did_not_move()


def test_the_test_goes_on_when_nothing_crossed_or_focused_the_covered_toplevel_and_the_pointer_is_outside():
    the_covering_on_x(a_calm_record()).assert_the_pointer_and_the_focus_did_not_move()


# --- the engine's log of a test names a file per process ----------------------------------------------------------------------------------------------

def test_the_engine_log_of_the_evidence_is_named_with_the_pid_so_that_every_process_writes_a_file_of_its_own():
    before = os.environ.get("MOZ_LOG_FILE")
    with FailureEvidence("a-unit-case") as evidence:
        evidence.arm(moz_log="Widget:4")
        named = os.environ["MOZ_LOG_FILE"]
        folder = evidence.moz_log_dir
    assert "%PID" in named
    assert pathlib.Path(named).parent == folder and pathlib.Path(named).name == MOZ_LOG_BASE + "%PID"
    assert os.environ.get("MOZ_LOG_FILE") == before          # the helper puts the environment back
