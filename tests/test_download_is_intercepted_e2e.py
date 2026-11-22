"""A download reaches Playwright: downloadCreated, then downloadFinished, and the
file is where the context's download options say.

**Why this file exists.** The engine accepted Browser.setDownloadOptions and
registered Juggler's interceptor (nsExternalHelperAppService::SetDownloadInterceptor),
but nothing ever called it: no Browser.downloadCreated, no downloadFinished, and
a navigation that ends in a download got no event telling it so. Playwright
Firefox calls the interceptor from nsExternalAppHandler::OnStartRequest, and
reports completion from OnSaveComplete and Cancel. Reported 2026-10-30 on the
151 and 159 engines.

Known-bad input: the engine before the fix -> every test here goes red
(expect_download times out).

**The end of a download, and what is left to Firefox.** Two more tests hold the
other half of the hook (`StealthDownloadIntercept`, folder `automation/downloads`
of the engine series, hooks in `uriloader/exthandler/nsExternalHelperAppService.cpp`):

* a download the client cancels (`download.cancel()`) reports its end ONCE, as
  `canceled`: `Cancel` and `OnSaveComplete` both end in the interceptor's
  `onDownloadComplete`, and a second `Browser.downloadFinished` for the same
  uuid would be a second report of one download. Counted on the engine's own
  events (the connection's listener), not on the client's objects: the client
  ignores a repeated finish.
* a context that has NO download options is not intercepted: the engine sends
  no `Browser.downloadCreated` and no `Browser.downloadFinished`, and the file
  goes where Firefox puts it (`browser.download.dir`). The same context, with
  its options, is the control: its download is announced and saved in the
  wrapper's directory, so the silence of the second download is the absence of
  the hook and not a page that never downloaded.

**A slow save is kept, not judged.** Firefox's own save of the last test has taken, on the runners, from about ten seconds to
more than eighty for the same bytes: that is the product (a user's download would wait as long), so nothing of it is switched
off. The seconds from the click to the file are recorded (a junit property), and a run of about ten seconds or more
(``SLOW_SAVE_S``, judged after the fact, never a verdict) keeps what says where the time went, in
``$RUNNER_TEMP/invpw-evidence/download-left-to-firefox/`` (``tests/_evidence_on_failure.py``, the same on a failure): the requests
the test's server saw, with times; every request the client heard (request, response, requestfinished, requestfailed), with
times; the Juggler wire and the engine's Juggler trace; the engine's log of the download machinery (``DOWNLOAD_LOG``).

Why a page cares: a download that ends twice, or that lands in the wrong place,
is a script that waits for a file that never comes, and a context that was given
no options must behave as retail Firefox does.
"""
from __future__ import annotations

import http.server
import os
import threading
import time
import types

import pytest

from _evidence_on_failure import FailureEvidence
from _waits import wait_until, watch

BODY = b"invisible download body\n" * 64

#: Seconds from the click to the file in Firefox's directory beyond which a run keeps its evidence. A file served from localhost
#: that takes about ten seconds already needs an explanation, so the threshold sits just under ten. Judged after the fact, never a
#: verdict (the test's deadline is the `watch` below).
SLOW_SAVE_S = 8.0

#: The engine's log of the download machinery, kept in the evidence: the external helper (`nsExternalHelperAppService.cpp`,
#: `HelperAppService`), the URI loader that hands it the response (`URILoader`), the reputation check of a finished download
#: (`ApplicationReputation`) and the url-classifier it asks (`UrlClassifierDbService`, whose log also carries the C++ hash
#: completer base, and `UrlClassifierStreamUpdater`), plus the network under them: the HTTP stack (`nsHttp`, the name of
#: `gHttpLog` in `nsHttpHandler.cpp`) and the host resolver (`nsHostResolver`, `gHostResolverLog` in `nsHostResolver.cpp`).
#: `UrlClassifierHashCompleter` is not a log module: that completer is JS (`UrlClassifierHashCompleter.sys.mjs`) and prints only
#: under `browser.safebrowsing.debug`, a Safe Browsing pref this test never touches.
DOWNLOAD_LOG = ("HelperAppService:5,URILoader:5,ApplicationReputation:5,UrlClassifierDbService:5,UrlClassifierStreamUpdater:5,"
                "nsHttp:3,nsHostResolver:5,timestamp,sync")


def _note(state, **fields):
    """One line of a record, on the clocks of the evidence (`t` wall, `m` perf_counter)."""
    with state.lock:
        state.records.append({"t": round(time.time(), 4), "m": round(time.perf_counter(), 6), **fields})


def _serve(record=None):
    """The site of the quick download. `record` (``.lock``, ``.records``), when given, gets every request the server saw and the
    end of every answer it wrote, with times."""
    def note(handler, what, **more):
        if record is not None:
            _note(record, what=what, method=handler.command, path=handler.path, peer=handler.client_address[1],
                  agent=handler.headers.get("User-Agent"), **more)

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            note(self, "request")
            if self.path.startswith("/file"):
                self.send_response(200)
                self.send_header("content-type", "application/octet-stream")
                self.send_header("content-disposition", 'attachment; filename="report.bin"')
                self.send_header("content-length", str(len(BODY)))
                self.end_headers()
                self.wfile.write(BODY)
                note(self, "answered", bytes=len(BODY))
                return
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.end_headers()
            self.wfile.write(b'<title>d</title><a id=dl href="/file">get</a>')
            note(self, "answered")

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/" % srv.server_address[1]


@pytest.fixture
def served():
    srv, url = _serve()
    try:
        yield url
    finally:
        srv.shutdown()


@pytest.fixture
def served_and_recorded():
    """The same site, and its record of every request (``.lock``, ``.records``), for the evidence of a slow run."""
    record = types.SimpleNamespace(lock=threading.Lock(), records=[])
    srv, url = _serve(record)
    try:
        yield url, record
    finally:
        srv.shutdown()


@pytest.mark.e2e
def test_a_clicked_download_is_reported_and_saved(firefox_binary, served, tmp_path):
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=4242, binary_path=firefox_binary, headless=True) as browser:
        page = browser.new_context(accept_downloads=True).new_page()
        page.goto(served, timeout=30_000)
        with page.expect_download(timeout=30_000) as info:
            page.click("#dl")
        download = info.value
        assert download.suggested_filename == "report.bin"
        target = tmp_path / "report.bin"
        download.save_as(target)
        assert target.read_bytes() == BODY
        assert download.failure() is None


@pytest.mark.e2e
def test_a_navigation_that_ends_in_a_download_is_reported(firefox_binary, served):
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=4242, binary_path=firefox_binary, headless=True) as browser:
        page = browser.new_context(accept_downloads=True).new_page()
        with page.expect_download(timeout=30_000) as info:
            with pytest.raises(Exception) as nav:
                page.goto(served + "file", timeout=20_000)
        # Playwright reports such a goto as "Download is starting"; it must END,
        # not hang until the timeout.
        assert "download" in str(nav.value).lower(), str(nav.value)
        assert info.value.suggested_filename == "report.bin"


# --------------------------------------------------------------------------------------------------------------------
#  The end of a download, and the context that has no download options
# --------------------------------------------------------------------------------------------------------------------
#: What the held answer promises: far more than the first chunk, so the transfer cannot end by itself.
HELD_LENGTH = 64 * 1024 * 1024

#: The longest the held answer waits for the test to let it go (only so that a failed test leaves no thread behind).
HELD_GIVES_UP_AFTER = 120


def _serve_held():
    """A site with `/file` (a quick attachment) and `/slow` (an attachment whose answer stops after its first chunk
    and goes on only when `release` is set): the held transfer is what a `cancel()` interrupts, with no clock."""
    release = threading.Event()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            try:
                if self.path.startswith("/file"):
                    self.send_response(200)
                    self.send_header("content-type", "application/octet-stream")
                    self.send_header("content-disposition", 'attachment; filename="report.bin"')
                    self.send_header("content-length", str(len(BODY)))
                    self.end_headers()
                    self.wfile.write(BODY)
                elif self.path.startswith("/slow"):
                    self.send_response(200)
                    self.send_header("content-type", "application/octet-stream")
                    self.send_header("content-disposition", 'attachment; filename="held.bin"')
                    self.send_header("content-length", str(HELD_LENGTH))
                    self.end_headers()
                    self.wfile.write(b"x" * 65536)
                    self.wfile.flush()
                    release.wait(HELD_GIVES_UP_AFTER)
                else:
                    self.send_response(200)
                    self.send_header("content-type", "text/html")
                    self.end_headers()
                    self.wfile.write(b'<title>d</title><a id=dl href="/file">get</a>'
                                     b'<a id=slow href="/slow">slow</a>')
            except OSError:
                pass   # the browser dropped the connection: that is the cancel

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/" % srv.server_address[1], release


@pytest.fixture
def held_site():
    srv, url, release = _serve_held()
    try:
        yield url
    finally:
        release.set()
        srv.shutdown()


def _engine_side(context):
    """The browser dispatcher (with the engine connection, `.conn`) and the context dispatcher of the in-process server
    that answers this context.

    The server runs IN THIS PROCESS (as `test_injected_node_surface.py` uses it), so the test can listen to the
    engine's own events and send the one command the client API never sends: `Browser.setDownloadOptions` with no
    options, which is a context that was never given any."""
    impl = context._impl_obj
    server = impl._connection._transport._server
    dispatcher = server.object(impl._guid)
    return dispatcher.browser, dispatcher


def _record(conn):
    """Every `Browser.downloadCreated` and `Browser.downloadFinished` the engine sends, as `(method, params)`."""
    events = []

    def listener(method, params, session):
        if method in ("Browser.downloadCreated", "Browser.downloadFinished"):
            events.append((method, dict(params or {})))

    conn.add_listener(listener)
    return events


def _of(events, method, uuid=None):
    return [p for m, p in events if m == method and (uuid is None or p.get("uuid") == uuid)]


@pytest.mark.e2e
def test_a_canceled_download_reports_once_as_canceled(firefox_binary, held_site):
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=4242, binary_path=firefox_binary, headless=True) as browser:
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        engine, _context = _engine_side(context)
        events = _record(engine.conn)
        page.goto(held_site, timeout=30_000)

        with page.expect_download(timeout=30_000) as info:
            page.click("#slow")
        held = info.value
        assert held.suggested_filename == "held.bin"
        held.cancel()
        assert held.failure() == "canceled"

        # The engine announced this download once, and it must say its end once. A later duplicate of the same end
        # would follow the first one on the same stream, so a second download, run to its end, is the barrier: the
        # events are read in order, and when the second download has finished anything the first had left to say has
        # been said.
        created = _of(events, "Browser.downloadCreated")
        assert len(created) == 1, created
        held_uuid = created[0]["uuid"]

        with page.expect_download(timeout=30_000) as info:
            page.click("#dl")
        quick = info.value
        assert quick.failure() is None
        quick_uuid = _of(events, "Browser.downloadCreated")[1]["uuid"]
        assert wait_until(lambda: _of(events, "Browser.downloadFinished", quick_uuid), 60,
                          pump=lambda: page.wait_for_timeout(100)), events

        ended = _of(events, "Browser.downloadFinished", held_uuid)
        assert len(ended) == 1, "the canceled download reported its end %d times: %r" % (len(ended), events)
        assert ended[0].get("canceled") is True, ended
        assert not ended[0].get("error"), ended
        # and the quick one, after it, ends clean: a cancel does not leave the interceptor in a state of its own
        assert len(_of(events, "Browser.downloadFinished", quick_uuid)) == 1, events
        assert not _of(events, "Browser.downloadFinished", quick_uuid)[0].get("canceled"), events


@pytest.mark.e2e
def test_a_context_without_download_options_is_left_to_firefox(firefox_binary, served_and_recorded, tmp_path, record_property):
    """The negative control of the interception: the same context, the same link, the same file.

    With its options the download is taken (announced, saved in the wrapper's directory). After
    `Browser.setDownloadOptions` with no options the engine does not take it: no `downloadCreated`, no
    `downloadFinished`, no `download` event of the client, and the file is where Firefox's own preference says."""
    from invisible_playwright import InvisiblePlaywright

    served, record = served_and_recorded
    where = tmp_path / "firefox"
    where.mkdir()
    prefs = {"browser.download.folderList": 2,
             "browser.download.dir": str(where),
             "browser.download.useDownloadDir": True,
             # the JS side of the download machinery (`DownloadsCommon.sys.mjs`, `HelperAppDlg.sys.mjs`: `maxLogLevelPref`), at its most
             # verbose level (`all` in `Console.sys.mjs` LOG_LEVELS; the default in `all.js` is "Error")
             "toolkit.download.loglevel": "All"}
    with FailureEvidence("download-left-to-firefox") as evidence:
        evidence.note_records("server-record", record)
        client = types.SimpleNamespace(lock=threading.Lock(), records=[])
        evidence.client_log = client.records
        evidence.arm(moz_log=DOWNLOAD_LOG)
        with InvisiblePlaywright(seed=4242, binary_path=firefox_binary, headless=True,
                                 extra_prefs=prefs) as browser:
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            # what the client heard of every request, with times (it only listens)
            page.on("request", lambda r: _note(client, what="request", url=r.url, navigation=r.is_navigation_request()))
            page.on("response", lambda r: _note(client, what="response", url=r.url, status=r.status))
            page.on("requestfinished", lambda r: _note(client, what="requestfinished", url=r.url))
            page.on("requestfailed", lambda r: _note(client, what="requestfailed", url=r.url, failure=str(r.failure)))
            engine, ctx = _engine_side(context)
            events = _record(engine.conn)
            announced = []

            def on_download(download):
                announced.append(download)
                _note(client, what="download", url=download.url)

            page.on("download", on_download)
            page.goto(served, timeout=30_000)

            # the control: with its options the download is taken
            with page.expect_download(timeout=30_000) as info:
                page.click("#dl")
            info.value.path()
            assert wait_until(lambda: _of(events, "Browser.downloadFinished"), 60,
                              pump=lambda: page.wait_for_timeout(100)), events
            assert len(_of(events, "Browser.downloadCreated")) == 1, events
            assert len(_of(events, "Browser.downloadFinished")) == 1, events
            downloads_dir = engine._downloads_dir(ctx.context_id)
            saved_by_the_wrapper = sorted(os.listdir(downloads_dir))
            assert len(saved_by_the_wrapper) == 1, saved_by_the_wrapper
            assert os.listdir(str(where)) == [], "the taken download landed in Firefox's directory"

            # the context loses its download options: Firefox's own path takes the download
            engine.conn.send("Browser.setDownloadOptions",
                             {"browserContextId": ctx.context_id, "downloadOptions": None}, timeout=None)
            # The click on a download Firefox keeps plans a navigation that never completes (nothing commits: the response is a download),
            # so the click does not wait for it; the end of the test waits on the CONDITION, the file in Firefox's directory.
            _note(client, what="click", target="#dl", download_options=None)
            clicked = time.perf_counter()
            page.click("#dl", no_wait_after=True, timeout=30_000)
            landed = where / "report.bin"
            landing = watch(lambda: landed.exists() and landed.read_bytes() == BODY, bool, 120,
                            pump=lambda: page.wait_for_timeout(100))
            seconds_to_the_file = time.perf_counter() - clicked
            _note(client, what="file-landed" if landing.ok else "file-not-landed", history=landing.history)
            # not a verdict: how long Firefox's own save took, from the click to the file
            record_property("seconds_until_the_file_landed", "%.1f" % seconds_to_the_file)
            assert landing.ok, os.listdir(str(where))

            assert len(_of(events, "Browser.downloadCreated")) == 1, "the engine took a download of a context with no options"
            assert len(_of(events, "Browser.downloadFinished")) == 1, events
            assert len(announced) == 1, announced
            assert sorted(os.listdir(downloads_dir)) == saved_by_the_wrapper, "the wrapper's directory got a second file"
        # The browser is closed, so its log and its trace are whole. A save slower than any healthy run is kept to be read, not judged.
        slow = seconds_to_the_file > SLOW_SAVE_S
        record_property("evidence_kept_for_a_slow_save", str(slow))
        if slow:
            evidence.keep("Firefox's own save took %.1f s from the click to the file (more than %.0f s)"
                          % (seconds_to_the_file, SLOW_SAVE_S))
