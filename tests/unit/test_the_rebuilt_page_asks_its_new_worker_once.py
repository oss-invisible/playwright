"""The page built again after `go_back` asks its new worker ONE question. No browser.

The worker of the test counts its answers (`postMessage(++n)`). `_assert_a_new_page_with_a_new_worker` asks the new worker once, as the sentinel that
proves it is fresh (it answers 1). A second question to it answered 2 and failed every time Firefox rebuilt the page instead of restoring it from the
cache (run 45064266867, [oop-frame-other-site-goto]). The fake page here is a real counting worker: every `window.__ask()` answers one more than the
last, so a helper that asks twice is red.
"""
import pytest

pytestmark = pytest.mark.unit

import test_a_worker_comes_back_with_its_page_from_the_cache_e2e as e2e


class _CountingPage:
    """A page whose worker counts every question (`n` is what the worker of the test keeps)."""

    def __init__(self):
        self.asked = 0
        self.frames = []

    def evaluate(self, expression):
        assert expression == "window.__ask()"
        self.asked += 1
        return self.asked


def _events():
    return [("Page.workerCreated", {"workerId": "old", "url": "http://a.test:1/worker.js"}),
            ("Page.workerDestroyed", {"workerId": "old"}),
            ("Page.eventFired", {"name": "back"}),
            ("Page.workerCreated", {"workerId": "new", "url": "http://a.test:1/worker.js"})]


def test_the_page_built_again_asks_its_new_worker_once():
    page, events = _CountingPage(), _events()
    e2e._assert_a_new_page_with_a_new_worker(page, events, "top", "old", 3)
    assert page.asked == 1, "the new worker was asked %d times: the second answer is 2, not 1" % page.asked


def test_a_new_worker_that_is_not_fresh_is_still_refused():
    page, events = _CountingPage(), _events()
    page.asked = 1   # the worker had answered before the sentinel
    with pytest.raises(AssertionError, match="not a fresh worker"):
        e2e._assert_a_new_page_with_a_new_worker(page, events, "top", "old", 3)
