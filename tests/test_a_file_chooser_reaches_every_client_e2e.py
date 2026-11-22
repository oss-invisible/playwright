"""Which path loses the `filechooser` event: a matrix that tells the candidates apart.

A user with a STOCK Playwright client on a served browser reported that
`page.set_content("<input type=file>")`, a direct click on the input and `expect_file_chooser` never fires.
`tests/test_file_chooser.py` is in-process only and clicks a button whose script calls `input.click()`, so nothing
here says whether the event is lost by the client, by the served mode, by the order of the subscription and the click,
or whether the event comes and only the upload is refused. Each test below changes ONE thing and fails with a message
that says WHAT was lost:

  LOST      the `filechooser` event never reached the client within the deadline;
  UPLOAD    the event came and `set_files` failed (the error is in the message);
  EMPTY     `set_files` answered and the input holds no file.

Nothing is slept on: `expect_file_chooser(timeout=30_000)` waits on the event (the deadline of the whole suite), the
handler form waits with `wait_until` on a condition.

WHAT THE STOCK CLIENT SENDS OVER `connect` (playwright 1.61.0, `playwright/_impl/_set_input_files_helpers.py`,
`convert_input_files`):
  * a PATH (str / Path): the branch `context._channel._connection.is_remote` (`connect` marks the connection remote)
    streams the file: `createTempFiles` on the context, then `WritableStream.copy` per file, then `setInputFiles`
    with `streams`. Our server does not serve `createTempFiles` (`perimeter.py`: "the streamed upload path"), and
    `_upload_paths` (`server.py`) reads only `localPaths`, `localDirectory` and `payloads`. So the path form is
    expected to be refused or to arrive empty over `connect`.
  * an in-memory payload `{"name", "mimeType", "buffer"}`: the branch after the path one, no `createTempFiles`:
    `setInputFiles` with `payloads` (base64), which `_upload_paths` stages as a file of the session.
`remote.py` (module docstring, "LIMITS OF THIS FIRST VERSION") says file choosers "are not carried to a remote
client"; tests 3 to 7 are what says whether that is still true.

Not a test of a fix: this is the evidence. On GitHub the red tests name the path that loses the event.
"""
from __future__ import annotations

import importlib

import pytest

from _waits import wait_until

pytestmark = pytest.mark.e2e

DEADLINE_MS = 30_000
FILE_NAME = "chooser-probe.txt"
FILE_BYTES = b"hello chooser"
PAGE = "<!doctype html><title>t</title><input type=file>"
READ = "() => { const f = document.querySelector('input').files; return f.length ? [f[0].name, f[0].size] : null; }"
FRESH_PAGES = 5


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  What every test uses
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def probe_file(tmp_path):
    path = tmp_path / FILE_NAME
    path.write_bytes(FILE_BYTES)
    return path


def _stock_playwright():
    try:     # the stock client (a pinned dev dependency), imported by name: the fork's guard forbids a static import
        return importlib.import_module("playwright.sync_api").sync_playwright
    except ImportError as exc:
        pytest.fail("the stock Playwright client is missing (%s): install the wrapper with its dev extra" % exc, pytrace=False)


@pytest.fixture(scope="module")
def served(firefox_binary):
    from invisible_playwright._juggler.remote import launch_server
    server = launch_server(headless=True, binary_path=firefox_binary, locale="en-US", timezone="Europe/Rome")
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def stock_browser(served):
    """The stock client connected to the served browser, closed after the test (the served browser stays)."""
    with _stock_playwright()() as p:
        browser = p.firefox.connect(served.ws_endpoint)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def in_process_browser(firefox_binary):
    from invisible_playwright import InvisiblePlaywright
    with InvisiblePlaywright(seed=4271, binary_path=firefox_binary, headless=True) as browser:
        yield browser


def _lost(where):
    pytest.fail("LOST: the `filechooser` event never reached the client within %d s (%s)" % (DEADLINE_MS // 1000, where), pytrace=False)


def _chooser_after(page, click, where):
    """Click, and wait on the event. A failure of the click is not a lost event, and says so."""
    clicked = []
    try:
        with page.expect_file_chooser(timeout=DEADLINE_MS) as info:
            click()
            clicked.append(True)
    except Exception as failure:
        if not clicked:
            pytest.fail("the click itself failed before any event could come (%s): %s: %s" % (where, type(failure).__name__, failure), pytrace=False)
        _lost("%s; the wait said %s: %s" % (where, type(failure).__name__, failure))
    return info.value


def _chooser_by_handler(page, click, where):
    """The `page.on("filechooser")` form: the handler is registered, then the click, then a wait on the condition."""
    heard = []
    page.on("filechooser", heard.append)
    click()
    if not wait_until(lambda: heard, DEADLINE_MS // 1000, lambda: page.wait_for_timeout(100)):
        _lost("%s, handler form" % where)
    return heard[0]


def _upload(chooser, files, where):
    try:
        chooser.set_files(files, timeout=DEADLINE_MS)
    except Exception as failure:
        pytest.fail("UPLOAD: the event came and set_files failed (%s): %s: %s" % (where, type(failure).__name__, failure), pytrace=False)


def _the_input_holds_the_file(page, where):
    held = page.evaluate(READ)
    assert held is not None, "EMPTY: set_files answered (%s) and the input holds no file" % where
    assert list(held) == [FILE_NAME, len(FILE_BYTES)], "WRONG FILE: the input holds %r (%s), expected %r" % (held, where, [FILE_NAME, len(FILE_BYTES)])


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  1-2. Our own client, in process: the control. Green here and red below puts the loss on the served path.
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def test_1_in_process_a_direct_click_on_the_input_gives_a_chooser_that_uploads(in_process_browser, probe_file):
    page = in_process_browser.new_page()
    page.set_content(PAGE)
    chooser = _chooser_after(page, lambda: page.click("input"), "in process, expect_file_chooser")
    _upload(chooser, str(probe_file), "in process, a path")
    _the_input_holds_the_file(page, "in process, a path")


def test_2_in_process_the_filechooser_handler_hears_a_direct_click_on_the_input(in_process_browser, probe_file):
    page = in_process_browser.new_page()
    page.set_content(PAGE)
    chooser = _chooser_by_handler(page, lambda: page.click("input"), "in process")
    _upload(chooser, str(probe_file), "in process, a path, handler form")
    _the_input_holds_the_file(page, "in process, a path, handler form")


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  3-6. A served browser and a STOCK client.
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def test_3_served_with_a_stock_client_expect_file_chooser_then_a_path_uploads(stock_browser, probe_file):
    page = stock_browser.new_context().new_page()
    page.set_content(PAGE)
    chooser = _chooser_after(page, lambda: page.click("input"), "served + stock client, expect_file_chooser")
    _upload(chooser, str(probe_file), "served + stock client, a path (createTempFiles)")
    _the_input_holds_the_file(page, "served + stock client, a path")


def test_4_served_with_a_stock_client_the_filechooser_handler_hears_the_click(stock_browser, probe_file):
    page = stock_browser.new_context().new_page()
    page.set_content(PAGE)
    chooser = _chooser_by_handler(page, lambda: page.click("input"), "served + stock client")
    _upload(chooser, str(probe_file), "served + stock client, a path (createTempFiles), handler form")
    _the_input_holds_the_file(page, "served + stock client, a path, handler form")


def test_5_served_with_a_stock_client_the_event_alone_comes_with_the_input_element(stock_browser):
    """The event and nothing else: no `set_files`. Red here is a lost EVENT; green here and red in test 3 is a refused UPLOAD."""
    page = stock_browser.new_context().new_page()
    page.set_content(PAGE)
    chooser = _chooser_after(page, lambda: page.click("input"), "served + stock client, event only")
    assert chooser.page is page, "the chooser belongs to another page"
    described = chooser.element.evaluate("e => e.tagName + ':' + e.type")
    assert described == "INPUT:file", "the chooser's element is %r, not the input" % described
    assert chooser.is_multiple() is False, "the chooser says a single-file input takes several files"


def test_6a_served_with_a_stock_client_set_files_with_a_path_streams_through_create_temp_files(stock_browser, probe_file):
    """A path over `connect` is the streamed branch (`createTempFiles`), which the perimeter refuses: the message names what came back."""
    page = stock_browser.new_context().new_page()
    page.set_content(PAGE)
    chooser = _chooser_after(page, lambda: page.click("input"), "served + stock client, path form")
    _upload(chooser, str(probe_file), "served + stock client, a path: createTempFiles + streams")
    _the_input_holds_the_file(page, "served + stock client, a path")


def test_6b_served_with_a_stock_client_set_files_with_an_in_memory_payload_avoids_create_temp_files(stock_browser):
    """A payload over `connect` is sent as `payloads` in `setInputFiles` (no `createTempFiles`): green here and red in 6a is the streamed path alone."""
    page = stock_browser.new_context().new_page()
    page.set_content(PAGE)
    chooser = _chooser_after(page, lambda: page.click("input"), "served + stock client, payload form")
    _upload(chooser, {"name": FILE_NAME, "mimeType": "text/plain", "buffer": FILE_BYTES}, "served + stock client, a payload")
    _the_input_holds_the_file(page, "served + stock client, a payload")


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  7. The order of the subscription and the click, on a page born a moment before.
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def _every_fresh_page_gets_its_chooser(new_page, where):
    """`updateSubscription` is sent with no reply and the server's workers run in no fixed order: on a page created a moment ago, the click
    comes right behind the subscription. Each of the pages must hear its chooser; the one that did not is named."""
    lost = []
    for n in range(FRESH_PAGES):
        context, page = new_page()
        try:
            page.set_content(PAGE)
            clicked = []
            try:
                with page.expect_file_chooser(timeout=DEADLINE_MS):
                    page.click("input")
                    clicked.append(True)
            except Exception as failure:
                lost.append("page %d: %s (%s: %s)" % (n + 1, "event never came" if clicked else "the click failed", type(failure).__name__, failure))
        finally:
            context.close()
    assert not lost, "LOST on %d of %d fresh pages (%s): %s" % (len(lost), FRESH_PAGES, where, "; ".join(lost))


def test_7a_served_with_a_stock_client_a_fresh_page_hears_its_chooser_on_the_first_click(stock_browser):
    def new_page():
        context = stock_browser.new_context()
        return context, context.new_page()
    _every_fresh_page_gets_its_chooser(new_page, "served + stock client")


def test_7b_in_process_a_fresh_page_hears_its_chooser_on_the_first_click(in_process_browser):
    def new_page():
        context = in_process_browser.new_context()
        return context, context.new_page()
    _every_fresh_page_gets_its_chooser(new_page, "in process")
