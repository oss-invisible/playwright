"""The record of the engine's content processes tells a process that ended from one that is still there (no browser here).

tests/test_goto_to_the_same_url_e2e.py keeps `ContentProcessWatch` while it runs: a frame that never comes is, from the client alone, a content process
that exited or a message that was lost. These tests drive the real bookkeeping with a world of processes the test changes by hand (no browser, no
psutil) and a probe that answers the exit codes:

* a process first seen is recorded with its pid, type and the end of its command line; a process that is no longer listed is recorded as gone at the
  sample that did not find it, with the exit code the probe reads (Windows) or the note that it cannot be read (Linux);
* a process that the list still shows but whose handle says it ended (a terminated process stays listed while a handle is open) is gone too;
* a pid that has gone is never recorded twice, and one that stays is never reported;
* the summary is one line with the number of content processes seen (the parent is not one) and every exit with its place and code;
* the exit codes are written as a reader looks for them (0, 1, 0xC0000005);
* known-bad: a snapshot that raises is a note, not an exception of the test; a stop closes every handle opened.
"""
from __future__ import annotations

import time

import pytest

from _content_processes import ContentProcessWatch, NoExitCodes, describe_exit

pytestmark = pytest.mark.unit


class World:
    """The processes of the engine now, as `engine_snapshot` would list them."""

    def __init__(self):
        self.processes = {}
        self.fail = False

    def born(self, pid, kind="tab", tail="-contentproc tab"):
        self.processes[pid] = (kind, "firefox.exe", tail)

    def gone(self, pid):
        del self.processes[pid]

    def snapshot(self):
        if self.fail:
            raise RuntimeError("the process list is not readable")
        return dict(self.processes)


class Codes:
    """A probe like the Windows one: `ended` maps the pids whose handle says they ended to their exit code."""

    readable = True

    def __init__(self):
        self.ended = {}
        self.opened = []
        self.closed = []

    def open(self, pid):
        self.opened.append(pid)

    def exit_code(self, pid):
        return self.ended.get(pid)

    def close(self, pid):
        self.closed.append(pid)


def events(watch, kind):
    with watch.lock:
        return [r for r in watch.records if r["event"] == kind]


def test_a_process_first_seen_is_recorded_with_its_type_and_tail():
    world = World()
    world.born(10, "parent", "firefox.exe -marionette")
    world.born(21, "tab", "-parentPid 10 tab")
    watch = ContentProcessWatch(world.snapshot, Codes())
    found = watch.sample("before the first goto")
    assert [(e["event"], e["pid"], e["type"], e["tail"]) for e in found] == [("seen", 10, "parent", "firefox.exe -marionette"),
                                                                           ("seen", 21, "tab", "-parentPid 10 tab")]
    assert events(watch, "sample")[0]["alive"] == [10, 21]
    assert watch.sample("after goto 0") == []                     # nothing changed: nothing is reported again


def test_a_process_that_is_no_longer_listed_is_gone_with_the_exit_code_the_handle_reads():
    world, codes = World(), Codes()
    world.born(21)
    world.born(22)
    watch = ContentProcessWatch(world.snapshot, codes)
    watch.sample("before the first goto")
    world.gone(22)
    codes.ended[22] = 0xC0000005
    found = watch.sample("after goto 3")
    assert [(e["event"], e["pid"], e["at"], e["exit_code"], e["exit_code_text"]) for e in found] == [("gone", 22, "after goto 3", 0xC0000005, "0xC0000005")]
    assert watch.sample("after goto 4") == []                      # recorded once
    assert watch.summary() == "content processes: 2 seen, exits: [22(tab)@after goto 3=0xC0000005]"


def test_a_process_the_list_still_shows_but_whose_handle_says_it_ended_is_gone():
    world, codes = World(), Codes()
    world.born(21)
    watch = ContentProcessWatch(world.snapshot, codes)
    watch.sample("start")
    codes.ended[21] = 1                                            # still listed: a handle keeps a terminated process in the list
    found = watch.sample("after goto 0")
    assert [(e["event"], e["pid"], e["exit_code_text"]) for e in found] == [("gone", 21, "1")]
    assert watch.sample("after goto 1") == []                      # the same pid, still listed, is not seen again as a new process


def test_a_gone_process_without_a_readable_code_says_so_on_windows():
    world, codes = World(), Codes()
    world.born(21)
    watch = ContentProcessWatch(world.snapshot, codes)
    watch.sample("start")
    world.gone(21)                                                 # no code was read: the handle could not be opened
    (event,) = watch.sample("after goto 0")
    assert event["exit_code"] is None and "no exit code was read" in event["note"]


def test_on_linux_a_disappearance_is_recorded_and_says_the_exit_code_cannot_be_read():
    world = World()
    world.born(21)
    watch = ContentProcessWatch(world.snapshot, NoExitCodes(), platform="linux")
    watch.sample("start")
    world.gone(21)
    (event,) = watch.sample("after goto 2")
    assert event["event"] == "gone" and event["exit_code"] is None and event["note"] == "exit code not readable on linux"
    assert watch.summary() == "content processes: 1 seen, exits: [21(tab)@after goto 2=exit code not readable on linux]"


def test_the_parent_is_not_a_content_process_in_the_summary_and_a_run_without_exits_says_so():
    world = World()
    world.born(10, "parent", "firefox.exe")
    world.born(21)
    world.born(22, "gpu", "-contentproc gpu")
    watch = ContentProcessWatch(world.snapshot, Codes())
    watch.sample("start")
    assert watch.summary() == "content processes: 2 seen, exits: []"


def test_a_process_born_between_two_samples_is_seen_at_the_next_one():
    world = World()
    watch = ContentProcessWatch(world.snapshot, Codes())
    watch.sample("start")
    world.born(31)
    found = watch.sample("after goto 0")
    assert [(e["event"], e["pid"], e["at"]) for e in found] == [("seen", 31, "after goto 0")]


def test_the_exit_codes_are_written_as_a_reader_looks_for_them():
    assert [describe_exit(c) for c in (0, 1, 255, 0xC0000005, -1073741819, None)] == ["0", "1", "255", "0xC0000005", "0xC0000005", "unknown"]


def test_a_look_at_the_processes_that_fails_is_a_note_and_never_an_exception():
    world = World()
    world.born(21)
    watch = ContentProcessWatch(world.snapshot, Codes())
    watch.sample("start")
    world.fail = True
    assert watch.sample("after goto 0") == []
    (note,) = events(watch, "sample-failed")
    assert note["at"] == "after goto 0" and "not readable" in note["error"]
    world.fail = False
    assert watch.sample("after goto 1") == []                      # the process is still there: the failed look did not make it gone


def test_stop_takes_a_last_sample_and_closes_every_handle_that_was_opened():
    world, codes = World(), Codes()
    world.born(21)
    world.born(22)
    watch = ContentProcessWatch(world.snapshot, codes)
    watch.sample("start")
    world.gone(22)
    watch.stop("end")
    assert codes.opened == [21, 22] and sorted(codes.closed) == [21, 22]
    assert [(e["pid"], e["at"]) for e in events(watch, "gone")] == [(22, "end")]


def test_the_thread_samples_between_the_explicit_samples():
    world = World()
    watch = ContentProcessWatch(world.snapshot, Codes())
    watch.sample("after goto 0")
    watch.start(period=0.01)
    world.born(41)
    deadline = time.monotonic() + 5
    while not events(watch, "seen") and time.monotonic() < deadline:
        time.sleep(0.01)
    watch.stop("end")
    (seen,) = events(watch, "seen")
    assert seen["pid"] == 41 and seen["at"] == "between samples, after: after goto 0"
