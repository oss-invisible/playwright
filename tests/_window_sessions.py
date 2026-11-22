"""What the window e2e tests share: a headed session of the product, and a wait on a condition of a page.

The sessions are HEADED on purpose. ``headless=True`` gives every session a display of its own (a fresh Xvfb on Linux, a fresh Win32 desktop on
Windows), so two of them never see each other; the focus and occlusion tests need two windows on ONE display, which is what two headed sessions of the
same run have (the Linux e2e runs under ``xvfb-run``). Both sessions use the same seed, so both windows are the same declared window and the second one
covers the first completely.

The first session is the one of the test's own thread; the second one lives in a thread of its own (``in_a_thread_of_its_own``, the pattern of
``test_popups_reach_the_client_e2e.py``): an open sync client keeps its asyncio loop marked as running in the thread that started it, and a second sync client
refuses to start there ("It looks like you are using Playwright Sync API inside the asyncio loop"). A sync client's objects belong to the thread that made
them, so everything the test does with the second session is handed to that thread (``run``).
"""
from __future__ import annotations

import contextlib
import queue
import threading
from typing import Any, Callable, Dict, Iterator, Optional

from _waits import Watch, watch

#: The seed only has to give two sessions the same window.
SEED = 21

#: The deadline of a wait for something that must happen, in seconds.
DEADLINE_S = 30


def launch(firefox_binary: str, *, extra_prefs: Optional[Dict[str, Any]] = None, pin: Optional[Dict[str, Any]] = None, headless: bool = False):
    """A session of the product (RFP off, the engine declared as the core launches it); ``extra_prefs`` and ``pin`` only where a test needs them."""
    from invisible_playwright import InvisiblePlaywright

    return InvisiblePlaywright(seed=SEED, binary_path=firefox_binary, headless=headless, locale="en-US", timezone="America/New_York",
                               extra_prefs=extra_prefs, pin=pin)


def wait_for_condition(page, sample: Callable[[], Any], done: Callable[[Any], Any], what: str, seconds: float = DEADLINE_S) -> Watch:
    """Until ``done(sample())``, reading the page between two looks. Red after ``seconds`` with what was seen on the way."""
    result = watch(sample, done, seconds, lambda: page.wait_for_timeout(100))
    assert result.ok, "%s; seen: %s" % (what, result.render())
    return result


class SecondSession:
    """A session that lives in a thread of its own: ``run(fn)`` calls ``fn(session)`` in that thread and gives back its result or raises its error."""

    def __init__(self) -> None:
        self._jobs: "queue.Queue[Optional[tuple]]" = queue.Queue()

    def run(self, fn: Callable[[Any], Any]) -> Any:
        box: Dict[str, Any] = {}
        done = threading.Event()
        self._jobs.put((fn, box, done))
        assert done.wait(300), "the second session did not answer within 300 s"
        if "error" in box:
            raise box["error"]
        return box.get("value")


@contextlib.contextmanager
def in_a_thread_of_its_own(firefox_binary: str, *, extra_prefs: Optional[Dict[str, Any]] = None) -> Iterator[SecondSession]:
    """A second headed session on the same display, started and closed in a thread with no asyncio loop; the test keeps the first one in its own thread."""
    handle = SecondSession()
    up = threading.Event()
    failure: Dict[str, BaseException] = {}

    def serve() -> None:
        try:
            with launch(firefox_binary, extra_prefs=extra_prefs) as session:
                up.set()
                while True:
                    job = handle._jobs.get()
                    if job is None:
                        return
                    fn, box, done = job
                    try:
                        box["value"] = fn(session)
                    except BaseException as exc:      # handed back to the test's thread by ``run``
                        box["error"] = exc
                    finally:
                        done.set()
        except BaseException as exc:                  # the launch or the close of the session
            failure["error"] = exc
        finally:
            up.set()

    worker = threading.Thread(target=serve, name="second-session", daemon=True)
    worker.start()
    assert up.wait(300), "the second session did not start within 300 s"
    if "error" in failure:
        raise failure["error"]
    try:
        yield handle
    finally:
        handle._jobs.put(None)
        worker.join(120)
        assert not worker.is_alive(), "the second session did not close within 120 s"
        if "error" in failure:
            raise failure["error"]
