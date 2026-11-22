"""The sampler of the host GL probe's e2e catches a child that lives for one sample, and a miss is a red, never a "no probe" (no browser here).

tests/test_the_host_gl_probe_runs_only_on_an_undeclared_engine_linux_e2e.py asserts that a short-lived child (``gfxtest glx``) does or does not appear
below the browser. A poll can fall between two samples of such a child, and in the case "absent" that miss would read as a pass. These tests drive the
real ``TreeSampler`` thread with a fake tree (a world the test changes, never a pause) and a counter for a clock, and the real ``reconcile`` with strace
lines written by hand:

* a child that exists for exactly one read of the tree (first with its parent's name, before its exec) is caught, as the probe, with its parent;
* known-bad: the sampler stopped before the child was born, or a child that came and went between two reads, or a log with no exec of the browser, is
  a problem that ``reconcile`` returns; the e2e asserts ``problems == []`` first, so each is red, never "absent";
* the strace lines are read as strace -f writes them: a call interrupted by another thread, a failed exec, a content process.
"""
from __future__ import annotations

import itertools
import threading

import pytest

from _process_sampler import TreeSampler, is_glx_probe, read_execs, reconcile

pytestmark = pytest.mark.unit

REAL = "/opt/engine/firefox"
PROBE = "/opt/engine/gfxtest"
BROWSER = 10
CHILD = 21


class World:
    """A process tree the test changes. ``read`` returns the tree as it is now; ``hold`` keeps a state until the sampler has read it once."""

    def __init__(self, tree):
        self._tree = dict(tree)
        self._lock = threading.Lock()
        self._held = None
        self.read_it = threading.Event()

    def set(self, tree, hold=False):
        with self._lock:
            self._tree = dict(tree)
            self._held = hold
            self.read_it.clear()

    def read(self, _root):
        with self._lock:
            tree = dict(self._tree)
            if self._held:
                self._held = False
                self.read_it.set()
            return tree


class Clock:
    def __init__(self):
        self._count = itertools.count()
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return float(next(self._count))


ONLY_BROWSER = {BROWSER: ("firefox", 1, [REAL])}
BEFORE_EXEC = {**ONLY_BROWSER, CHILD: ("firefox", BROWSER, [REAL])}
PROBE_RUNS = {**ONLY_BROWSER, CHILD: ("gfxtest", BROWSER, [PROBE, "glx", "-f", "33"])}
PROBE_ZOMBIE = {**ONLY_BROWSER, CHILD: ("gfxtest", BROWSER, [])}

LOG = [
    '10 execve("%s", ["%s", "-foreground"], 0x7ffd /* 40 vars */) = 0' % (REAL, REAL),
    '10 clone3({flags=CLONE_VM|CLONE_VFORK, exit_signal=SIGCHLD, stack=0x7f, stack_size=0x9000}, 88) = 21',
    '21 execve("%s", ["%s", "glx", "-f", "33"], 0x7ffd /* 40 vars */ <unfinished ...>' % (PROBE, PROBE),
    '12 futex(0x7f, FUTEX_WAKE_PRIVATE, 1) = 0',
    '21 <... execve resumed>) = 0',
]


def _run(world, steps, clock):
    """Start the sampler, then walk the world through ``steps`` (each held until the sampler has read it), then stop it. ``(sampler, launched, ready)``."""
    sampler = TreeSampler(BROWSER - 9, read=world.read, clock=clock, step=0).start()
    launched = clock()
    for tree in steps:
        world.set(tree, hold=True)
        assert world.read_it.wait(30), "the sampler never read the tree"
    ready = clock()
    sampler.stop()
    return sampler, launched, ready


def test_a_child_that_lives_for_one_read_is_caught_as_the_probe_with_its_parent():
    clock = Clock()
    world = World({})
    sampler, launched, ready = _run(world, [ONLY_BROWSER, BEFORE_EXEC, PROBE_RUNS, ONLY_BROWSER], clock)
    seen, first, last, error = sampler.view()
    probes, problems = reconcile(seen, first, last, error, read_execs(LOG), REAL, launched, ready)
    assert problems == []
    assert [e.pid for e in probes if is_glx_probe(e)] == [CHILD]
    child = seen[CHILD]
    assert child.is_probe() and child.ppid == BROWSER and child.names == ["firefox", "gfxtest"], child


def test_a_zombie_with_no_command_line_is_still_the_probe():
    clock = Clock()
    world = World({})
    sampler, launched, ready = _run(world, [ONLY_BROWSER, PROBE_ZOMBIE, ONLY_BROWSER], clock)
    seen, first, last, error = sampler.view()
    assert reconcile(seen, first, last, error, read_execs(LOG), REAL, launched, ready)[1] == []


def test_a_sampler_stopped_before_the_probe_reports_the_miss_and_not_an_absence():
    """Known-bad: the sampler stopped early. The probe it never saw is in the witness: the result is a problem, so the declared case cannot pass."""
    clock = Clock()
    world = World(ONLY_BROWSER)
    sampler = TreeSampler(1, read=world.read, clock=clock, step=0).start()
    launched = clock()
    sampler.stop()
    world.set(PROBE_RUNS)          # born after the sampler stopped
    world.set(ONLY_BROWSER)
    ready = clock()
    seen, first, last, error = sampler.view()
    probes, problems = reconcile(seen, first, last, error, read_execs(LOG), REAL, launched, ready)
    assert CHILD not in seen
    assert any("stopped early" in p for p in problems), problems
    assert any("the sampler missed a probe" in p for p in problems), problems


def test_a_probe_that_came_and_went_between_two_reads_is_a_miss_even_when_the_window_is_covered():
    """Known-bad: the poll fell between two samples of the probe. The window is covered, the sampler saw nothing; strace saw the exec: red."""
    clock = Clock()
    world = World({})
    sampler, launched, ready = _run(world, [ONLY_BROWSER, ONLY_BROWSER], clock)
    seen, first, last, error = sampler.view()
    probes, problems = reconcile(seen, first, last, error, read_execs(LOG), REAL, launched, ready)
    assert problems == ["strace saw pid 21 exec %s glx -f and the sampler never saw the pid: the sampler missed a probe" % PROBE], problems


def test_an_empty_witness_or_a_sampler_that_died_proves_no_absence():
    clock = Clock()
    world = World({})
    sampler, launched, ready = _run(world, [ONLY_BROWSER], clock)
    seen, first, last, error = sampler.view()
    problems = reconcile(seen, first, last, error, [], REAL, launched, ready)[1]
    assert any("the witness is empty" in p for p in problems), problems

    calls = itertools.count()

    def broken(_root):
        if next(calls):
            raise OSError("no /proc")
        return ONLY_BROWSER
    dead = TreeSampler(1, read=broken, clock=clock, step=0).start()
    dead._thread.join()            # the thread ends on its first read, which raises
    dead.stop()
    seen, first, last, error = dead.view()
    problems = reconcile(seen, first, last, error, read_execs(LOG[:1]), REAL, launched, clock())[1]
    assert any("stopped on an error" in p for p in problems), problems


def test_a_probe_the_sampler_saw_and_strace_did_not_is_refused():
    clock = Clock()
    world = World({})
    sampler, launched, ready = _run(world, [ONLY_BROWSER, PROBE_RUNS, ONLY_BROWSER], clock)
    seen, first, last, error = sampler.view()
    problems = reconcile(seen, first, last, error, read_execs(LOG[:1]), REAL, launched, ready)[1]
    assert any("strace never saw exec" in p for p in problems), problems


def test_the_strace_lines_are_read_as_strace_writes_them():
    lines = LOG + [
        '30 execve("%s", ["%s", "-contentproc", "tab"], 0x7ffd /* 40 vars */) = 0' % (REAL, REAL),
        '31 execve("/opt/engine/gfxtest", ["/opt/engine/gfxtest", "vaapi", "-d", "/dev/dri/renderD128"], 0x7f /* 9 vars */) = 0',
        '32 execve("/opt/other/gfxtest", ["/opt/other/gfxtest", "glx"], 0x7f /* 9 vars */) = -1 ENOENT (No such file or directory)',
        '33 execve("%s", ["%s", "glx", "-f", "40"], 0x7f /* 9 vars */ <unfinished ...>' % (PROBE, PROBE),
    ]
    execs = read_execs(lines)
    assert [(e.pid, e.result) for e in execs] == [(10, 0), (21, 0), (30, 0), (31, 0), (32, -1), (33, None)]
    assert [e.pid for e in execs if is_glx_probe(e) and e.ran()] == [21, 33]
    assert execs[1].argv == [PROBE, "glx", "-f", "33"]
    assert execs[3].ran() and not is_glx_probe(execs[3])
    assert execs[4].ran() is False

