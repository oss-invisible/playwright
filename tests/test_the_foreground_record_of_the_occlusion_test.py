"""The foreground record of the occlusion e2e test, with fakes of the Windows calls: ``tests/_win_events.py``.

On Windows the occlusion test records the system's window events from a thread of its own (``SetWinEventHook`` out of context, its own ``GetMessage``
loop) to say who had the foreground at the insert without topmost and who took it after. That line is evidence, never a verdict, so the danger is a
false one: a hook that was never set, a loop that died, or a hook that heard nothing would all read as "nobody took the foreground". Here the
recorder runs on fakes that fail in each of those ways, and its record must say so ("NOT RECORDED" and why), never the false line; a fake that
delivers a known sequence gives the line those events mean. The last test sets the real hook on Windows and sees it stop cleanly; on Linux the
recorder says it records nothing.
"""
from __future__ import annotations

import sys
import threading

import pytest

from _win_events import EVENT_OBJECT_FOCUS, EVENT_SYSTEM_FOREGROUND, ForegroundRecorder, foreground_line

pytestmark = pytest.mark.unit

COVERED, COVER, OTHER = 8960, 11256, 4242
ROLES = {COVERED: "the covered session's engine", COVER: "the covering session's engine"}
WINDOWS = {101: COVERED, 202: COVER, 303: OTHER}


class FakeEvents:
    """The methods of ``Win32Events``: the loop delivers ``script`` (event, hwnd) and then waits for ``quit``, or ``dies`` on its first turn."""

    def __init__(self, script=(), dies=False, refuses=False, ignores_quit=False, foreground=202) -> None:
        self.script, self.dies, self.refuses, self.ignores_quit = list(script), dies, refuses, ignores_quit
        self.window = foreground
        self.quitting = threading.Event()
        self.release = threading.Event()
        self.unhooked = []
        self.on_event = None

    def make_queue(self):
        pass

    def hook(self, on_event):
        if self.refuses:
            raise OSError("SetWinEventHook answered NULL, GetLastError 5")
        self.on_event = on_event
        return "hook"

    def pump(self):
        if self.dies:
            raise RuntimeError("the loop thread died")
        for event, hwnd in self.script:
            self.on_event(event, hwnd, 0, 0, 1, 0)
        (self.release if self.ignores_quit else self.quitting).wait(60)

    def quit(self, thread_id):
        self.quitting.set()
        return True

    def unhook(self, handle):
        self.unhooked.append(handle)
        return True

    def owner(self, hwnd):
        return WINDOWS.get(hwnd, 0), 7

    def foreground(self):
        return self.window


def recorded(api):
    recorder = ForegroundRecorder(api).start()
    before = recorder.snapshot("right before the insert")
    after = recorder.snapshot("right after the insert")
    recorder.stop()
    return recorder, before, after


def test_a_loop_thread_that_dies_is_reported_and_no_one_is_said_to_have_taken_the_foreground():
    recorder, before, after = recorded(FakeEvents(dies=True))
    line = recorder.line(before, after, ROLES)
    assert any("died" in p for p in recorder.problems()), recorder.problems()
    assert "who took it after: NOT RECORDED" in line and "died" in line, line
    assert "nobody" not in line, line


def test_a_hook_that_hears_nothing_is_blind_and_says_so():
    api = FakeEvents()
    recorder, before, after = recorded(api)
    line = recorder.line(before, after, ROLES)
    assert recorder.problems() == ["the hook heard no event: it is blind"]
    assert "NOT RECORDED (the hook heard no event: it is blind)" in line and "nobody" not in line, line
    assert api.unhooked == ["hook"] and recorder.loop_ended == "quit"


def test_a_hook_the_system_refuses_is_reported():
    recorder, before, after = recorded(FakeEvents(refuses=True))
    assert any("the hook was never set" in p and "GetLastError 5" in p for p in recorder.problems()), recorder.problems()
    assert "NOT RECORDED" in recorder.line(before, after, ROLES)


def test_a_loop_that_does_not_stop_is_reported():
    api = FakeEvents(script=[(EVENT_SYSTEM_FOREGROUND, 202)], ignores_quit=True)
    recorder = ForegroundRecorder(api)
    recorder.deadline_s = 0.2
    recorder.start()
    before = recorder.snapshot("right before the insert")
    recorder.stop()
    try:
        assert any("did not stop" in p for p in recorder.problems()), recorder.problems()
        assert "NOT RECORDED" in recorder.line(before, before, ROLES)
    finally:
        api.release.set()
        recorder.thread.join(30)


def test_a_record_with_a_problem_says_not_recorded_whatever_its_events():
    events = [{"m": 2.0, "event": EVENT_SYSTEM_FOREGROUND, "pid": COVERED}]
    line = foreground_line(events, {"m": 1.0, "pid": COVER}, {"m": 1.1, "pid": COVER}, 3.0, ROLES, ["the message loop ended before it was stopped"])
    assert line == ("foreground at the insert: pid 11256 (the covering session's engine), right after it: pid 11256 (the covering session's engine); "
                    "who took it after: NOT RECORDED (the message loop ended before it was stopped)")


def test_the_line_names_who_had_the_foreground_who_took_it_after_and_when():
    events = [
        {"m": 10.0, "event": EVENT_SYSTEM_FOREGROUND, "pid": COVERED},
        {"m": 11.0, "event": EVENT_SYSTEM_FOREGROUND, "pid": OTHER},
        {"m": 12.0, "event": EVENT_OBJECT_FOCUS, "pid": COVERED},
        {"m": 12.4, "event": EVENT_OBJECT_FOCUS, "pid": COVERED},
        {"m": 12.5, "event": EVENT_SYSTEM_FOREGROUND, "pid": COVERED},
        {"m": 14.0, "event": EVENT_SYSTEM_FOREGROUND, "pid": COVER},
    ]
    line = foreground_line(events, {"m": 11.5, "pid": OTHER}, {"m": 11.6, "pid": OTHER}, 13.0, ROLES, [], name_of=lambda pid: "explorer.exe")
    assert line == ("foreground at the insert: pid 4242 explorer.exe (not an engine of the test), right after it: pid 4242 explorer.exe (not an engine of "
                    "the test); had it before the insert: the covered session's engine first at -1.500 s, the covering session's engine never; "
                    "who took it after: pid 8960 (the covered session's engine) at +1.000 s; "
                    "focus events after it: pid 8960 (the covered session's engine) x2, first at +0.500 s")


def test_no_foreground_event_after_the_insert_is_nobody_until_the_read_back():
    events = [{"m": 1.0, "event": EVENT_SYSTEM_FOREGROUND, "pid": COVER}]
    line = foreground_line(events, {"m": 2.0, "pid": COVER}, {"m": 2.0, "pid": COVER}, 3.25, ROLES, [])
    assert line.endswith("had it before the insert: the covered session's engine never, the covering session's engine first at -1.000 s; "
                         "who took it after: nobody until the read-back at +1.250 s; focus events after it: none"), line


def test_the_recorder_keeps_each_event_with_its_window_and_owner():
    api = FakeEvents(script=[(EVENT_SYSTEM_FOREGROUND, 101), (EVENT_OBJECT_FOCUS, 202), (0x8000, 0)])
    recorder = ForegroundRecorder(api).start()
    before = recorder.snapshot("right before the insert")
    recorder.stop()
    kept = [(e["name"], e["hwnd"], e["pid"], e["tid"]) for e in recorder.recorded()]
    assert kept == [("EVENT_SYSTEM_FOREGROUND", 101, COVERED, 7), ("EVENT_OBJECT_FOCUS", 202, COVER, 7), ("EVENT_OBJECT_CREATE", 0, 0, 0)]
    assert before["hwnd"] == 202 and before["pid"] == COVER
    assert recorder.problems() == [] and recorder.as_dict()["foreground events"][0]["pid"] == COVERED


def test_on_this_system_the_real_recorder_starts_and_stops_cleanly_or_says_it_records_nothing():
    recorder = ForegroundRecorder()
    with recorder:
        snapshot = recorder.snapshot("now")
    if sys.platform == "win32":
        assert recorder.hooked and recorder.loop_ended == "quit" and recorder.unhooked is True, recorder.as_dict()
        assert not recorder.thread.is_alive()
        assert "error" not in snapshot, snapshot
        assert not [p for p in recorder.problems() if "blind" not in p], recorder.problems()
    else:
        assert recorder.problems() == ["not Windows: no window event is recorded"], recorder.problems()
        assert "NOT RECORDED (not Windows" in recorder.line(snapshot, snapshot, ROLES)
