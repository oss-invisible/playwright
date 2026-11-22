"""The engine tells that a navigation moved to another process, and only then (firefox-69).

The parent process decides, in ``DocumentLoadListener::TriggerProcessSwitch``, that a document load leaves the docShell that started it. From
then on the stand-in of the request that docShell still holds counts for nothing, and what the old page says of the navigation (its own
``location.replace`` stopping it) is not the navigation's end. The engine tells the registry the decision, with the browsing context and the child
id of the process the load LEFT (observer topic ``juggler-navigation-moved``), and the registry writes it in the engine's own Juggler trace as
``onNavigationMoved`` (`tests/_navigation_moved_trace.py` says which line). A page cannot see any of it: this is the engine's own record, read from
the trace folder the evidence helper hands the engine.

- A ``goto`` from a page on 127.0.0.1 to a page on localhost is a cross-site process switch: one line for that navigation, naming the process the
  127.0.0.1 page lived in. The ``goto`` lands.
- A ``goto`` back, from localhost to 127.0.0.1, leaves the process of localhost: its origin is not the process the first move left (two sites,
  two processes).
- A ``goto`` between two pages of 127.0.0.1 is told too, though no process changes: with the back/forward cache in the parent a top-level navigation
  that can enter it replaces the browsing context even on the same site (``IsolationOptionsForNavigation``, ``dom/ipc/ProcessIsolation.cpp``, the
  block of ``mTryUseBFCache``), so ``MaybeTriggerProcessSwitch`` goes on to ``TriggerProcessSwitch``. The process it left is the one it stays in,
  and the next such ``goto`` leaves the browsing context the first one created: another id, the same child id.
- A same-site navigation of an iframe is told by no one: the back/forward cache is for top-level loads only (``!aParentWindow`` in the same
  condition), the load finds the process of the frame that holds its site (``mReplaceBrowsingContext`` stays false, the remote type is the current
  one) and ``MaybeTriggerProcessSwitch`` stops at "type is compatible" (``netwerk/ipc/DocumentLoadListener.cpp``), before ``TriggerProcessSwitch``.

On an engine before firefox-69 the hook does not exist: the same ``goto`` lands and the trace holds no such line at all. Not a skip: a required suite
allows none (scripts/ci_verdict.py), so the test asserts what its engine does, as the tests of `engine_reaches` do.

Known-bad input: an engine that does not tell the decision (firefox-69 with the hook gone) -> the cross-site test finds no line; an engine that
tells it for a load that stays (the iframe's) -> the iframe test finds one; an engine that does not replace the browsing context of a same-site top-level
load -> the same-site test finds the same id twice.
"""
from __future__ import annotations

import http.server
import threading

import pytest
from _evidence_on_failure import FailureEvidence
from _navigation_moved_trace import (
    assert_nothing_moved_since,
    assert_replaced_in_the_same_process,
    moved_lines,
    moved_origin,
    navigation_ids_of,
    read_trace,
)

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def site():
    """A test server on 127.0.0.1; the same server answers as ``localhost`` (another site, so another process)."""

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/withframe"):
                body = b"<!doctype html><html><body><iframe src='/inner?moved=inner-a'></iframe></body></html>"
            else:
                body = b"<!doctype html><html><body><p id=p>" + self.path.encode() + b"</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()


@pytest.fixture(scope="module")
def evidence():
    """The engine's trace folder, armed before the browser is launched (the engine reads it at launch); what a failing test leaves for a reader
    is saved as in the other e2e of the wrapper."""
    held = FailureEvidence("a-navigation-that-moved-is-told-by-the-engine")
    held.arm()
    try:
        yield held
    finally:
        held.__exit__(None, None, None)


@pytest.fixture(scope="module")
def _browser(firefox_binary, evidence):
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=4344, binary_path=firefox_binary, headless=True) as browser:
        yield browser


@pytest.fixture
def page(_browser):
    page = _browser.new_page()
    try:
        yield page
    finally:
        page.close()


class _on_failure:
    """Around the body of a test: when it raises, the evidence held for the module is saved (the folder the e2e jobs upload)."""

    def __init__(self, held):
        self.held = held

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.held._save(exc_type, exc)
        return False


def _go(page, url):
    page.goto(url, wait_until="load", timeout=30_000)
    assert page.url == url, page.url


def test_a_cross_site_goto_is_told_moved_by_the_engine_with_the_process_it_left(page, site, engine_reaches, evidence):
    other = site.replace("127.0.0.1", "localhost")
    first, second, back = site + "/first?moved=a", other + "/second?moved=b", site + "/third?moved=c"
    with _on_failure(evidence):
        _go(page, first)
        _go(page, second)
        _go(page, back)
        lines = read_trace(evidence.trace_dir)
        if not engine_reaches("firefox-69"):
            # The trace is on (it saw our navigation), and it holds no such line.
            assert navigation_ids_of(lines, second), "the engine's trace is not on"
            assert moved_lines(lines) == [], "an engine before firefox-69 has no such hook: %s" % moved_lines(lines)
            return
        left_127, left_localhost = moved_origin(lines, second), moved_origin(lines, back)
        # The first goto left the process of the page on 127.0.0.1, the goto back left the process of the page on localhost: two sites, two processes.
        assert left_127[1] != left_localhost[1], (left_127, left_localhost)


def test_a_same_site_top_level_goto_is_told_moved_in_the_process_it_stays_in_with_a_browsing_context_it_replaces(page, site, engine_reaches, evidence):
    one, two, three = site + "/one?moved=d", site + "/two?moved=e", site + "/three?moved=f"
    with _on_failure(evidence):
        _go(page, one)
        _go(page, two)
        _go(page, three)
        lines = read_trace(evidence.trace_dir)
        if not engine_reaches("firefox-69"):
            assert navigation_ids_of(lines, three), "the engine's trace is not on"
            assert moved_lines(lines) == [], "an engine before firefox-69 has no such hook: %s" % moved_lines(lines)
            return
        # `two` left the browsing context of `one`, `three` the one that `two` created: the same process, another browsing context.
        assert_replaced_in_the_same_process(moved_origin(lines, two), moved_origin(lines, three))


def test_a_same_site_navigation_of_an_iframe_is_not_told_moved_by_the_engine(page, site, evidence):
    # No navigation id names an iframe's load (the registry gives one to the main document's channel only), so the proof is the count of lines:
    # nothing but the iframe navigates between the two readings.
    with _on_failure(evidence):
        _go(page, site + "/withframe?moved=g")
        page.wait_for_function("() => document.querySelector('iframe').contentDocument.readyState === 'complete'"
                               " && document.querySelector('iframe').contentWindow.location.search === '?moved=inner-a'", timeout=30_000)
        lines = read_trace(evidence.trace_dir)
        assert navigation_ids_of(lines, site + "/withframe?moved=g"), "the engine's trace is not on"
        already = len(moved_lines(lines))
        page.evaluate("() => { document.querySelector('iframe').src = '/inner?moved=inner-b'; }")
        page.wait_for_function("() => document.querySelector('iframe').contentWindow.location.search === '?moved=inner-b'"
                               " && document.querySelector('iframe').contentDocument.readyState === 'complete'", timeout=30_000)
        assert page.url == site + "/withframe?moved=g"
        assert_nothing_moved_since(read_trace(evidence.trace_dir), already)
