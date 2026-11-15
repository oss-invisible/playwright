"""The waits of the e2e tests are conditions with a deadline, and each can still fail.

An e2e test that sleeps a fixed time and then reads the state is red on a slow machine for no fault of the product.
`_waits.py` is what those tests wait with now: a condition polled until it holds or a deadline passes, a record of what
was seen on the way, and the judgements the tests make of it. This file runs them on a clock of its own (no browser, no
sleep), for the case each test must still call red and the case it must call green:

* keepalive request: an end that never comes is red when the deadline passes; one that comes late is green;
* an iframe that has not reached its address, and a count of processes that stays above the bound (a frame process that
  is never released), are red after their deadline, with what was seen in the message;
* `networkidle`: red when it comes while a request is in flight or too soon after the last one, green after a real
  silence even when the late requests of the frame never came;
* the page of the TURN test stops its tick the moment a srflx arrives, before the POST.
"""
import asyncio

import pytest

import _fission_frames as ff
import _turn_chain as chain_mod
from _waits import Timeline, awatch, quiet_problem, spare_processes, wait_until, watch, without_end

pytestmark = pytest.mark.unit


class Clock:
    """A clock that moves only when the test's pump says so."""

    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def pump(self, seconds=0.1):
        def step():
            self.now += seconds
        return step


def test_a_condition_that_never_holds_is_false_after_the_deadline_and_says_what_it_saw():
    clock = Clock()
    seen = watch(lambda: 1, lambda value: value > 5, 15.0, clock.pump(0.5), clock)
    assert not seen.ok and 15.0 <= seen.took < 15.6
    assert seen.history == [(0.0, 1)] and "+0.00s 1" in seen.render()


def test_a_condition_that_holds_late_is_true_and_a_sample_at_the_deadline_is_not_missed():
    clock = Clock()
    state = {"n": 0}

    def pump():
        clock.now += 1.0
        state["n"] += 1

    seen = watch(lambda: state["n"], lambda value: value >= 9, 15.0, pump, clock)
    assert seen.ok and [value for _at, value in seen.history] == list(range(10))
    # the last look is taken at the deadline: true on the very last pump still counts
    clock = Clock()
    state["n"] = 0
    assert watch(lambda: state["n"], lambda value: value >= 3, 3.0, pump, clock).ok
    assert wait_until(lambda: True, 1.0, lambda: None, clock)


def test_the_async_wait_is_the_same_wait():
    clock = Clock()

    async def pump():
        clock.now += 0.25

    async def run(seconds, wanted):
        return await awatch(lambda: round(clock.now - 100.0, 2), lambda value: value >= wanted, seconds, pump, clock)

    assert asyncio.run(run(8.0, 3.0)).ok
    lost = asyncio.run(run(8.0, 99.0))
    assert not lost.ok and lost.took >= 8.0


def test_an_end_that_never_comes_keeps_the_keepalive_test_red_and_one_that_comes_late_does_not():
    expected = ["/seen/beacon", "/seen/keepalive", "/seen/keepalive-nocors", "/seen/plain"]
    clock = Clock()
    timeline = Timeline(clock)
    started, ended = [], []

    def begin(url):
        started.append(url)
        timeline.add("request", url)

    def end(url):
        ended.append(url)
        timeline.add("finished", url)

    def done():
        return sorted(set(started)) == expected and not without_end(started, ended)

    # three of the four end; the end of the beacon is lost: the wait runs to its deadline and the answer is "no"
    for url in expected:
        begin(url)
    for url in expected[1:]:
        end(url)
    assert not wait_until(done, 15.0, clock.pump(0.05), clock)
    assert without_end(started, ended) == ["/seen/beacon"]
    # the message has every event with its time
    text = timeline.render()
    assert "+0.000s request   /seen/beacon" in text and "finished  /seen/plain" in text

    # the same four, the ends arriving up to 9 s late (a loaded machine): green
    clock = Clock()
    started.clear()
    ended.clear()
    pending = list(expected)
    for url in expected:
        begin(url)

    def pump():
        clock.now += 1.0
        if clock.now >= 109.0:
            while pending:
                end(pending.pop())
        elif pending and clock.now >= 101.0:
            end(pending.pop())

    assert wait_until(done, 15.0, pump, clock)


def test_what_began_and_never_ended_counts_each_url_once_per_missing_end():
    assert without_end(["/a", "/a", "/b"], ["/a", "/b"]) == ["/a"]
    assert without_end(["/a"], ["/a", "/a"]) == []      # an extra end is not a missing one (the test's equality names it)
    assert without_end([], []) == []


def test_an_iframe_that_never_reaches_its_address_is_red_after_its_deadline_with_the_urls_seen():
    clock = Clock()
    frames = {"urls": ["http://a.test:1/plain"]}

    async def pump():
        clock.now += 0.1
        if clock.now - 100.0 > 0.5:
            frames["urls"] = ["http://a.test:1/plain", "http://b.test:1/other"]

    async def run():
        return await awatch(lambda: list(frames["urls"]), lambda urls: any(u.endswith("/first") for u in urls), 8.0, pump,
                            clock)

    seen = asyncio.run(run())
    assert not seen.ok and seen.took >= 8.0
    assert "b.test:1/other" in seen.render()


def test_a_frame_process_that_is_never_released_passes_the_bound_and_a_refilled_pool_does_not():
    spare = spare_processes(16 * 1024 ** 3)
    first = 5                       # the count after the first closed context: the reference
    clock = Clock()

    def after_close(counts_over_time):
        steps = iter(counts_over_time)
        last = {"n": next(steps)}

        def pump():
            clock.now += 0.25
            last["n"] = next(steps, last["n"])

        return watch(lambda: last["n"], lambda count: count <= first + spare, 25.0, pump, clock)

    # the pool refills one process at a time (5 -> 8) while the frames' processes end: within the bound, green
    assert after_close([10, 8, 8, 8]).ok       # 10 at the first look (frames still ending), then 8
    # one frame process left behind per context: the fifth context leaves 4 more than the first, past the pool of 3
    seen = after_close([first + 4] * 10)
    assert not seen.ok and "9" in seen.render()


def test_the_pool_of_spare_content_processes_is_retails():
    assert spare_processes(16 * 1024 ** 3) == 3        # desktop, Fission: dom.ipc.processPrelaunch.fission.number
    assert spare_processes(4 * 1024 ** 3) == 1         # at or under lowmem_mb (4096): one
    assert spare_processes(4 * 1024 ** 3 + 1) == 3
    assert spare_processes(None) == 3                  # unknown memory: the pref's value


def test_networkidle_is_red_while_a_request_is_in_flight_and_too_soon_after_the_last_end():
    quiet = 0.4
    chain = [("/late-top", 0.0, 0.01), ("/late-frame", 0.02, 0.03), ("/late/1", 0.23, 0.53), ("/late/2", 0.53, 0.83)]
    # a product that comes with the top page alone: at 0.5 /late/1 is still in flight
    assert "in flight" in quiet_problem(chain[:2] + [("/late/1", 0.23, None)], 0.5, quiet)
    assert "in flight" in quiet_problem(chain, 0.7, quiet)                   # /late/2 ends at 0.83
    # a product that comes 100 ms after the last end instead of half a second
    assert "after only 0.100 s" in quiet_problem(chain, 0.93, quiet)
    # the promise kept: 0.5 s after the last end
    assert quiet_problem(chain, 1.33, quiet) is None
    assert quiet_problem([], 1.0, quiet) is not None


def test_networkidle_is_green_after_a_real_silence_even_if_the_late_requests_never_came():
    quiet = 0.4
    # the frame's script was held up for more than half a second: the page was idle, and networkidle came 0.52 s after
    # the last end, before /late/1 arrived; that is what the product promises, so it is not a failure
    seen = [("/late-top", 0.0, 0.01), ("/late-frame", 0.02, 0.03)]
    assert quiet_problem(seen, 0.55, quiet) is None
    assert quiet_problem(seen, 0.20, quiet) is not None                      # but not at 0.2 s


def test_the_turn_page_stops_its_tick_before_the_post_not_after_it():
    page = chain_mod.PAGE_HTML
    handler = page[page.index("pc.onicecandidate"):]
    arrival = handler[:handler.index("const r = await fetch('/verdict'")]
    assert "clearInterval(tick)" in arrival and "mark('srflx')" in arrival
    assert "await" not in arrival.replace("async (e)", "") and "fetch" not in arrival
    after_post = handler[handler.index("const r = await fetch('/verdict'"):]
    assert "clearInterval" not in after_post
    # the tick still ends a page that gets no srflx
    assert "finish('TIMEOUT')" in page[:page.index("pc.onicecandidate")]


# ---------------------------------------------------------------------------
# the proof that a frame is in another process (_fission_frames.assert_other_process)
# ---------------------------------------------------------------------------

def _ticks(start, end, every=10):
    return list(range(start, end, every))


def _same_process_trial(load_ms=0):
    """A frame that shares the observer's thread: its spin 1000..1600 takes the thread, so the observer's ticks stop for
    the whole spin (and for ``load_ms`` more when the machine is loaded)."""
    return _ticks(0, 1000) + _ticks(1600 + load_ms, 3000), (1000, 1600)


def _other_process_trial(starved=False):
    """A frame in another process: the observer ticks all along, unless the machine starves it for the whole spin."""
    if starved:
        return _ticks(0, 990) + _ticks(3500, 4500), (1000, 1600)     # 2.5 s stall, like the one seen on Windows
    return _ticks(0, 3000), (1000, 1600)


def test_no_tick_runs_inside_the_spin_of_a_frame_that_shares_the_process_whatever_the_load():
    for load in (0, 50, 800, 2500):
        trial = _same_process_trial(load)
        assert ff.ticks_inside(*trial) == 0
    assert not ff.other_process_proved([_same_process_trial(load) for load in (0, 200, 2500)] * 2)
    assert ff.ticks_inside([5, 10], None) == 0


def test_a_frame_of_another_process_is_proved_by_one_healthy_spin_even_after_starved_ones():
    assert ff.ticks_inside(*_other_process_trial()) > 50
    assert ff.other_process_proved([_other_process_trial()])
    # a loaded machine: the observer was starved for two whole spins, then ran: still proved
    assert ff.other_process_proved([_other_process_trial(True), _other_process_trial(True), _other_process_trial()])
    # starved every time: nothing is proved (the old gap criterion called this "same process" at the first stall)
    assert not ff.other_process_proved([_other_process_trial(True)] * 3)


def test_a_frame_that_shares_the_process_keeps_the_assertion_red_and_one_that_does_not_keeps_it_green(monkeypatch):
    class Frame:
        url = "http://127.0.0.2:1/grid"

    class Observer:
        url = "http://localhost:1/grid"

    monkeypatch.setattr(ff, "spin_trial", lambda observer, frame: _same_process_trial(2500))
    seconds = iter(range(0, 10_000, 7))              # a clock that moves 7 s at each look, so the 30 s of the proof are spent without waiting
    with pytest.raises(AssertionError) as red:
        ff.assert_other_process(Observer(), Frame(), clock=lambda: next(seconds))
    assert "shares the process of the observer" in str(red.value) and "(0, " in str(red.value)
    calls = []

    def starved_then_healthy(observer, frame):
        calls.append(1)
        return _other_process_trial(starved=len(calls) < 3)

    monkeypatch.setattr(ff, "spin_trial", starved_then_healthy)
    ff.assert_other_process(Observer(), Frame())
    assert len(calls) == 3
    # a machine that starves the observer for more spins than the fixed count of the old proof is still waited for, until the deadline
    calls.clear()

    def starved_for_a_long_time(observer, frame):
        calls.append(1)
        return _other_process_trial(starved=len(calls) <= 2 * ff.TRIALS)

    monkeypatch.setattr(ff, "spin_trial", starved_for_a_long_time)
    ff.assert_other_process(Observer(), Frame(), clock=lambda: 0)
    assert len(calls) == 2 * ff.TRIALS + 1


def test_the_proof_of_another_process_has_no_fixed_pause_and_no_fixed_count():
    import inspect
    spin = inspect.getsource(ff.spin_trial)
    assert "wait_for_timeout" not in spin, "the spin is waited for as a condition (its end), not for spin_ms + 800 ms"
    assert "range(TRIALS)" not in inspect.getsource(ff.assert_other_process), "the proof of another process is waited for until a deadline"
