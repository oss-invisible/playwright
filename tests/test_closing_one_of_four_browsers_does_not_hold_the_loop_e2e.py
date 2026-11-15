"""Closing one of four browsers in one process does not hold the event loop that serves the other three.

The close of a browser WAITS (for its processes to exit, for its profile to be removed): that is the contract, Playwright's `close()`
returns once the processes are gone. What was wrong was WHO waited. In the async API the user's own event loop did, three times:

* `InProcessTransport.request_stop` ran `server.shutdown()` (the hooks that close the engine's pipe and wait for its tree, and the
  removal of the profile) on the loop that asked for the stop;
* `InProcessTransport.wait_until_stopped` joined the four `juggler-N` workers on the loop, up to 5 s each, and a worker is busy
  for as long as the engine's `conn.close()` waits (`BrowserDispatcher.op_close`);
* `InvisiblePlaywright._teardown` reaped the session's token (`reap` waits for the tree) on the loop.

Measured with py-spy on four browsers in one Python process: closing one stopped the loop that serves the other three for as long as
it waited. Now the same waits run in threads of their own (`invisible_core._offloop.off_the_loop`, from invisible-core 64.72.0).

The test runs the real engine, four of them, in a client program of its own (the loop under test is that program's main thread). A
sentinel task asks to wake every 5 ms; a watcher thread notes how late it woke and where the main thread was: waiting in the
selector (the machine did not run the loop: on a loaded host an idle loop is 100 ms late now and then, which is not the close's
doing and is not counted) or INSIDE something (held). The instrument is the one of the core's
`tests/test_a_close_does_not_hold_the_loop.py`. A control with the same instrument holds the loop on purpose first, so a watcher
that could not see a held loop does not pass the test for nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.e2e

#: The client. Four browsers by the async API, each with a page; one is closed while a sentinel measures the loop and one pinger per
#: other browser keeps asking its page a question. Prints one JSON line.
_CLIENT = textwrap.dedent("""
    import asyncio, json, os, shutil, sys, threading, time, traceback
    import psutil
    from invisible_playwright.async_api import InvisiblePlaywright

    BINARY = sys.argv[1]
    main_id = threading.get_ident()


    class Instrument:
        \"\"\"A sentinel on the loop and a watcher thread. `held` is the longest wake that was late while the main thread was INSIDE
        something, not idle in the loop's selector.\"\"\"

        def __init__(self):
            self.gaps, self.holes, self.held = [], [], 0.0
            self.beat = [time.monotonic()]
            self.stop = asyncio.Event()
            self.over = threading.Event()
            self.on_the_loop = []
            self._real = {}
            #: Set to make the sentinel's next wait that long: the loop then waits in its selector, late by that much, as on a loaded
            #: machine that does not schedule it.
            self.pause_once = 0

        async def sentinel(self):
            last = time.monotonic()
            while not self.stop.is_set():
                await asyncio.sleep(self.pause_once or 0.005)
                self.pause_once = 0
                now = time.monotonic()
                self.gaps.append(now - last)
                last = self.beat[0] = now

        @staticmethod
        def idle(frame):
            # True when the main thread is waiting in the event loop's selector: it is not running anything of anybody's.
            for _ in range(3):
                if frame is None:
                    return False
                code = frame.f_code
                if code.co_name in ("_poll", "select") and code.co_filename.endswith(("windows_events.py", "selectors.py")):
                    return True
                frame = frame.f_back
            return False

        def stacks(self):
            frames = sys._current_frames()
            names = {t.ident: t.name for t in threading.enumerate()}
            return {
                "main": "".join(traceback.format_stack(frames[main_id])[-6:]),
                "others": {names.get(i, str(i)): "".join(traceback.format_stack(f)[-3:])
                           for i, f in frames.items() if i != main_id and i != threading.get_ident()}}

        def watcher(self):
            looked = 0
            while not self.over.is_set():
                time.sleep(0.003)
                late = time.monotonic() - self.beat[0]
                level = int(late / 0.03)
                if late > 0.03 and not self.idle(sys._current_frames().get(main_id)):
                    self.held = max(self.held, late)
                if level > looked:
                    self.holes.append({"late_ms": round(late * 1000), **self.stacks()})
                looked = level

        def watched(self, owner, name):
            self._real[(owner, name)] = original = getattr(owner, name)

            def call(*x, **k):
                if threading.get_ident() == main_id and not self.over.is_set():
                    self.on_the_loop.append(name)
                return original(*x, **k)
            setattr(owner, name, call)

        async def start(self):
            self.watched(time, "sleep")
            self.watched(shutil, "rmtree")
            self.watched(psutil, "wait_procs")
            self.watched(threading.Thread, "join")
            threading.Thread(target=self.watcher, daemon=True, name="the-watcher").start()
            self.task = asyncio.create_task(self.sentinel())
            await asyncio.sleep(0.1)

        async def finish(self):
            self.stop.set()
            await self.task
            self.over.set()
            for (owner, name), original in self._real.items():
                setattr(owner, name, original)
            self._real.clear()

        def report(self):
            return {"held": self.held, "gap": max(self.gaps), "holes": self.holes[:4], "on_the_loop": sorted(set(self.on_the_loop))}


    def engines():
        out = []
        for child in psutil.Process().children(recursive=True):
            try:
                if "firefox" in child.name().lower():
                    out.append(child.pid)
            except psutil.Error:
                pass
        return out


    async def pinger(page, record, closing):
        while not record["stop"]:
            started = time.monotonic()
            await page.evaluate("1 + 1")
            took = time.monotonic() - started
            if closing.is_set():
                record["answers"] += 1
                record["slowest"] = max(record["slowest"], took)
            await asyncio.sleep(0.02)


    async def main():
        # The control: the same instrument on a loop that is held on purpose.
        control = Instrument()
        await control.start()
        await asyncio.sleep(0.05)
        time.sleep(0.4)
        await asyncio.sleep(0.05)
        await control.finish()

        # The second control: a loop that is LATE without being held. The sentinel's next wake is 150 ms away, so the loop waits in its
        # selector (as when the machine does not schedule it), and 130 ms in it runs 15 ms of a callback that was due then, while the
        # sentinel is still 125 ms late. Only those 15 ms are the loop's thread at work: the lateness before them must not count.
        unscheduled = Instrument()
        await unscheduled.start()
        await asyncio.sleep(0.05)
        unscheduled.pause_once = 0.15
        loop = asyncio.get_running_loop()

        def burst():
            end = time.monotonic() + 0.015
            while time.monotonic() < end:
                pass
        loop.call_later(0.13, burst)
        await asyncio.sleep(0.4)
        await unscheduled.finish()

        # A slow disk: removing a profile takes 150 ms more (a scanner of files at work, a spinning disk, a loaded runner). On a fast
        # disk the removal of a profile is a few ms and a close made on the loop hides in the noise; with a disk that is slow the
        # wait is what the loop pays, wherever it is made. Every thread pays it the same.
        real_rmtree = shutil.rmtree

        def slow_rmtree(*x, **k):
            time.sleep(0.15)
            return real_rmtree(*x, **k)

        shutil.rmtree = slow_rmtree

        sessions = [InvisiblePlaywright(seed=7100 + i, binary_path=BINARY, headless=True) for i in range(4)]
        browsers = []
        for session in sessions:
            browsers.append(await session.__aenter__())
        pages = [await b.new_page() for b in browsers]
        for i, page in enumerate(pages):
            await page.set_content("<p id=n>browser %d</p>" % i)
        launched = len(engines())

        closing, records = asyncio.Event(), []
        tasks = []
        for page in pages[1:]:
            record = {"stop": False, "answers": 0, "slowest": 0.0}
            records.append(record)
            tasks.append(asyncio.create_task(pinger(page, record, closing)))
        await asyncio.sleep(0.5)

        instrument = Instrument()
        await instrument.start()
        closing.set()
        started = time.monotonic()
        await sessions[0].__aexit__(None, None, None)
        took = time.monotonic() - started
        closing.clear()
        await instrument.finish()
        report = instrument.report()
        report.update({
            "took": took, "launched": launched,
            "control": control.report(),
            "unscheduled": unscheduled.report(),
            "answers": [r["answers"] for r in records], "slowest": [r["slowest"] for r in records],
        })
        # The other three still work after it.
        shutil.rmtree = real_rmtree
        report["texts"] = [await p.text_content("#n") for p in pages[1:]]
        for record in records:
            record["stop"] = True
        await asyncio.gather(*tasks)
        for session in sessions[1:]:
            await session.__aexit__(None, None, None)
        time.sleep(0.5)
        report["left"] = engines()
        print("REPORT", json.dumps(report), flush=True)

    try:
        asyncio.run(asyncio.wait_for(main(), 240))
    finally:
        for pid in engines():
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass
""")


def _holes(report):
    out = []
    for hole in report.get("holes") or []:
        out.append("--- the loop was %d ms late. MAIN THREAD:\n%s" % (hole["late_ms"], hole["main"]))
        for name, stack in hole["others"].items():
            out.append("--- thread %s:\n%s" % (name, stack))
    return "\n".join(out) or "(no look was taken)"


@pytest.fixture(scope="module")
def report(firefox_binary, tmp_path_factory):
    """One run of the client: its numbers are asserted one claim at a time below."""
    script = tmp_path_factory.mktemp("close-loop") / "client.py"
    script.write_text(_CLIENT, encoding="utf-8")
    import invisible_playwright

    # The client imports the checkout under test, the one this run imported.
    source = os.path.dirname(os.path.dirname(os.path.abspath(invisible_playwright.__file__)))
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([source, os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep))
    done = subprocess.run([sys.executable, str(script), str(firefox_binary)], capture_output=True, text=True, timeout=300, env=env)
    lines = [x for x in done.stdout.splitlines() if x.startswith("REPORT ")]
    assert done.returncode == 0 and lines, (
        "the client failed (%s):\n%s\n%s" % (done.returncode, done.stdout[-2000:], done.stderr[-4000:]))
    return json.loads(lines[-1][len("REPORT "):])


@pytest.mark.wall_clock
def test_the_instrument_sees_a_loop_that_is_held(report):
    """The control: the loop held on purpose for 400 ms is seen as held. Without this, a watcher that could not tell a held loop from
    a starved one would pass the test below for nothing."""
    control = report["control"]
    assert control["held"] > 0.3, (
        "the watcher did not see a loop held for 400 ms: held %.0f ms" % (control["held"] * 1000))
    assert "sleep" in control["on_the_loop"]


@pytest.mark.wall_clock
def test_the_instrument_does_not_charge_a_loop_the_machine_did_not_schedule(report):
    """The second control: a loop that waited 130 ms in its selector and then ran a 15 ms callback was not held by anybody.
    Charging it the whole lateness made a loaded Windows runner fail the bound below with 94 ms and nothing on the loop's thread
    (candidate 43727128285)."""
    unscheduled = report["unscheduled"]
    assert unscheduled["gap"] > 0.12, "the sentinel was not late: %r" % unscheduled
    assert unscheduled["held"] < 0.03, "a loop late by the machine, not by a call, was charged %.0f ms" % (unscheduled["held"] * 1000)


@pytest.mark.wall_clock
def test_closing_one_of_four_browsers_does_not_hold_the_loop(report):
    """Four browsers, one closes: the loop of the program is not held for 50 ms, counting only the time its thread is running."""
    assert report["launched"] >= 4, "fewer than four engines were running before the close: %r" % report["launched"]
    assert report["took"] > 0.3, "the close finished before the engine could have exited: nothing was waited for"
    assert report["held"] < 0.05, (
        "the loop was held for %.0f ms while one of four browsers closed (the close took %.0f ms; the longest wake was %.0f ms late; "
        "calls made by the loop's own thread: %s). Where the threads were while it was late:\n%s"
        % (report["held"] * 1000, report["took"] * 1000, report["gap"] * 1000, report["on_the_loop"], _holes(report)))


def test_no_blocking_step_of_the_close_runs_on_the_loop_s_thread(report):
    """The cause, asked directly and without the clock: `time.sleep`, `rmtree`, `psutil.wait_procs` and `Thread.join` are what a close
    does while it waits and removes, and none of them may be called by the thread that runs the loop. Red on a close made on it."""
    assert report["on_the_loop"] == [], "the loop's own thread made these calls while it awaited the close: %s" % report["on_the_loop"]


@pytest.mark.wall_clock
def test_the_other_three_keep_answering_during_the_close(report):
    """A call on each of the other three browsers is answered while the first one closes, and the three are alive after."""
    assert len(report["answers"]) == 3
    assert all(n >= 3 for n in report["answers"]), "the others stopped answering during the close: %r" % report["answers"]
    assert max(report["slowest"]) < 2.0, "a call of another browser waited %.1f s during the close" % max(report["slowest"])
    assert report["texts"] == ["browser 1", "browser 2", "browser 3"]


def test_every_engine_is_gone_when_the_four_have_closed(report):
    assert report["left"] == [], "engine processes are left after every browser closed: %r" % report["left"]
