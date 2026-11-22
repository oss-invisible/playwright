"""History across a frame of another site, and the processes a context leaves behind.

Three sites (see `_fission_frames.py`): the top is A (`127.0.0.1`), the frame B (`localhost`), the frame C
(`127.0.0.2`). With Fission each site is a content process of its own, and a navigation of the frame to another
site changes its process.

* History. The page holds a frame of B. A navigation inside the frame adds a session-history entry, so
  `page.go_back()` moves the FRAME back (the top url does not change, the frame is at the url it had) and
  `go_forward()` restores it; the same when the frame went to a third site (a new process). A top-level
  navigation and `go_back()` restore the page with its frame of another site, and the frame answers `evaluate`.
  The outcome asserted is what stock Firefox does (see the docstrings).
* Processes. A context whose page had frames of other sites leaves no content process behind once it is closed:
  judged by identity (the processes that host the frames' sites, by name on Linux and by the engine's logged remote
  type on Windows), not by a count that also holds the pool of spare processes (see the test).

The first test proves the frame is in another process, so the file cannot turn into a same-process suite.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from _fission_frames import Sites, assert_other_process, launch, new_page, wait_for, wait_frame
from _processes import LOG_ENV, NAMED_BY_SITE, remote_types, site_processes_below
from _waits import watch

pytestmark = pytest.mark.e2e


@pytest.fixture
def sites():
    with Sites() as s:
        s.route("A", "/h-top", "<!doctype html><title>h-top</title><iframe id='b' name='b' src='%s' width='500' "
                "height='200'></iframe>" % s.url("B", "/h1"))
        for n in (1, 2):
            s.route("B", "/h%d" % n, "<!doctype html><title>h%d</title><p id='p'>h%d" % (n, n))
        s.route("C", "/h3", "<!doctype html><title>h3</title><p id='p'>h3")
        s.route("A", "/other", "<!doctype html><title>other</title><p>other")
        yield s


@pytest.fixture
def browser(firefox_binary):
    with launch(firefox_binary) as browser:
        yield browser


@pytest.fixture
def page(browser):
    context, page = new_page(browser)
    yield page
    context.close()


def traverse(page, direction):
    """``page.go_back()`` or ``page.go_forward()``. When the entry belongs to a frame, no navigation of the main
    frame follows, and the call gives up with a ``TimeoutError`` after the history has moved: as stock Firefox
    does. A call that returns is just as good; what the tests judge is where the page and its frame are after."""
    try:
        getattr(page, direction)(timeout=3000)
    except Exception as error:  # the client's own TimeoutError class: the wrapper ships its own copy
        if type(error).__name__ != "TimeoutError":
            raise


def _frame_url(page):
    """The url of the one frame of the page, once there is one."""
    wait_for(page, lambda: len(page.frames) == 2)
    return page.frames[1].url


def test_the_frame_is_in_another_process(page, sites):
    page.goto(sites.url("A", "/h-top"), wait_until="load")
    assert_other_process(page.main_frame, wait_frame(page, sites.url("B"), "/h1"))


def test_back_and_forward_move_the_frame_not_the_top_after_a_navigation_inside_it(page, sites):
    """The frame goes h1 -> h2 (same site). Back: the top is where it was, the frame is at h1. Forward: at h2."""
    page.goto(sites.url("A", "/h-top"), wait_until="load")
    frame = wait_frame(page, sites.url("B"), "/h1")
    frame.goto(sites.url("B", "/h2"))
    assert frame.url == sites.url("B", "/h2") and page.url == sites.url("A", "/h-top")
    traverse(page, "go_back")
    assert wait_for(page, lambda: _frame_url(page) == sites.url("B", "/h1")), _frame_url(page)
    assert page.url == sites.url("A", "/h-top")
    assert page.frames[1].evaluate("document.title") == "h1"
    traverse(page, "go_forward")
    assert wait_for(page, lambda: _frame_url(page) == sites.url("B", "/h2")), _frame_url(page)
    assert page.url == sites.url("A", "/h-top")
    assert page.frames[1].evaluate("document.title") == "h2"


def test_back_and_forward_across_a_frame_that_went_to_a_third_site(page, sites):
    """The frame goes h1 (B) -> h2 (B) -> h3 (C: a change of site and of process). Each back moves it one entry; forward goes the way back came."""
    page.goto(sites.url("A", "/h-top"), wait_until="load")
    frame = wait_frame(page, sites.url("B"), "/h1")
    frame.goto(sites.url("B", "/h2"))
    frame.goto(sites.url("C", "/h3"))
    assert wait_for(page, lambda: _frame_url(page) == sites.url("C", "/h3")), _frame_url(page)
    assert_other_process(page.main_frame, page.frames[1])
    for expected in ("B/h2", "B/h1"):
        traverse(page, "go_back")
        site, path = expected.split("/")
        assert wait_for(page, lambda: _frame_url(page) == sites.url(site, "/" + path)), (expected, _frame_url(page))
        assert page.url == sites.url("A", "/h-top")
        assert page.frames[1].evaluate("document.title") == path
    traverse(page, "go_forward")
    assert wait_for(page, lambda: _frame_url(page) == sites.url("B", "/h2")), _frame_url(page)
    traverse(page, "go_forward")
    assert wait_for(page, lambda: _frame_url(page) == sites.url("C", "/h3")), _frame_url(page)
    assert page.frames[1].evaluate("document.title") == "h3"
    assert page.url == sites.url("A", "/h-top")


def test_a_top_level_navigation_and_back_restore_the_page_with_its_frame_usable(page, sites):
    page.goto(sites.url("A", "/h-top"), wait_until="load")
    frame = wait_frame(page, sites.url("B"), "/h1")
    frame.goto(sites.url("B", "/h2"))
    page.goto(sites.url("A", "/other"), wait_until="load")
    assert page.url == sites.url("A", "/other")
    traverse(page, "go_back")
    assert page.url == sites.url("A", "/h-top")
    assert wait_for(page, lambda: len(page.frames) == 2 and page.frames[1].url.startswith(sites.url("B"))), \
        [f.url for f in page.frames]
    restored = page.frames[1]
    # the frame is where it was when the page was left (h2), as stock Firefox restores it
    assert restored.url == sites.url("B", "/h2")
    assert restored.evaluate("document.title") == "h2"
    assert restored.evaluate("location.origin") == sites.url("B")
    assert restored.locator("#p").inner_text() == "h2"
    assert_other_process(page.main_frame, restored)


# ---------------------------------------------------------------------------
# processes
# ---------------------------------------------------------------------------


CONTEXTS = 5
#: How long a process may take to appear or to end (one starts or ends in well under a second; this is the only clock
#: the verdict has).
DEADLINE = 25.0


def _site(url):
    """The site of a url: scheme and host, never the port."""
    parts = urlsplit(url)
    return "%s://%s" % (parts.scheme, parts.hostname)


def test_a_closed_context_with_frames_of_other_sites_leaves_no_content_process(
        firefox_binary, sites, monkeypatch, tmp_path):
    """Five contexts in a row, each with a page whose frames are on B and (nested) on C. The processes the frames
    took are gone once the context is closed.

    The verdict is told by identity, not by a count: the number of content processes under the engine also holds the
    preallocated pool and whatever the browser starts for itself (the New Tab page's own Mozilla site), which move
    with the load of the machine and are not what the test is about. A process that hosts a site says so: on Linux
    its name is the site (`fission.processSiteNames`, test-only), on Windows the remote type the engine logs for
    every process it creates (`webIsolated=<site>^userContextId=N`, `_processes.py`). So:

    * while a context is open the frames of B and C each have a content process of their own (the processes that host
      B and C exist: a lookup that finds nothing cannot pass);
    * once the context is closed no process that hosts B or C is left below this test (B and C are only ever framed,
      by a context that is closed, so nothing may host them).

    On Linux a name keeps 15 characters, so `http://127.0.0.2` is told from nothing but the start `http://127.0.0.` (the
    site of A, which closes with the context as well): the lookup of C is then no narrower than that of A and B's is the
    exact one.

    Each look is a WAIT for that condition (up to 25 s), not a look after a fixed pause; the message gives what was
    seen over time for every context."""
    sites.route("B", "/leak-b", "<!doctype html><title>b</title><iframe src='%s'></iframe>" % sites.url("C", "/h3"))
    sites.route("A", "/leak", "<!doctype html><title>leak</title><iframe id='b' src='%s'></iframe>"
                % sites.url("B", "/leak-b"))
    engine_dir = str(Path(firefox_binary).resolve().parent)
    framed = {"B": _site(sites.url("B")), "C": _site(sites.url("C"))}
    base = tmp_path / "process-types"
    if sys.platform == "win32":
        # Windows names no process: the engine's log tells the remote type of each one it creates
        monkeypatch.setenv("MOZ_LOG", LOG_ENV["MOZ_LOG"])
        monkeypatch.setenv("MOZ_LOG_FILE", str(base))

    def hosting():
        """`{name of the site: pids}` of the live processes below this test that host a framed site."""
        return {name: site_processes_below(os.getpid(), site, engine_dir, types=lambda: remote_types(base))
                for name, site in framed.items()}

    def both_hosted(found):
        return all(found.values())

    def none_hosted(found):
        return not any(found.values())

    def pause():
        time.sleep(0.25)

    with launch(firefox_binary, extra_prefs=NAMED_BY_SITE) as browser:
        trail = []
        for n in range(CONTEXTS):
            context, page = new_page(browser)
            page.goto(sites.url("A", "/leak"), wait_until="load")
            wait_frame(page, sites.url("C"), "/h3")
            opened = watch(hosting, both_hosted, DEADLINE, pause)
            trail.append("context %d open: %s" % (n, opened.render()))
            assert opened.ok, (
                "the frames of B and C took no process of their own (a site with no process of its own shows an empty "
                "set): %s" % " | ".join(trail))
            context.close()
            closed = watch(hosting, none_hosted, DEADLINE, pause)
            trail.append("context %d closed: %s" % (n, closed.render()))
            assert closed.ok, (
                "a process that hosts the site of a frame of the closed context is still there: %s"
                % " | ".join(trail))
