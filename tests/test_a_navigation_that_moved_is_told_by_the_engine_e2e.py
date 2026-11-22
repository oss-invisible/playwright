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
- A ``goto`` between two pages of 127.0.0.1 switches no process: no line.

On an engine before firefox-69 the hook does not exist: the same ``goto`` lands and the trace holds no such line at all. Not a skip: a required suite
allows none (scripts/ci_verdict.py), so the test asserts what its engine does, as the tests of `engine_reaches` do.

Known-bad input: an engine that does not tell the decision (firefox-69 with the hook gone) -> the cross-site test finds no line; an engine that
tells it for every load -> the same-site test finds one.
"""
from __future__ import annotations

import http.server
import threading

import pytest
from _evidence_on_failure import FailureEvidence
from _navigation_moved_trace import (
    assert_not_moved,
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


def test_a_same_site_goto_is_not_told_moved_by_the_engine(page, site, evidence):
    one, two = site + "/one?moved=d", site + "/two?moved=e"
    with _on_failure(evidence):
        _go(page, one)
        _go(page, two)
        lines = read_trace(evidence.trace_dir)
        assert_not_moved(lines, two)
