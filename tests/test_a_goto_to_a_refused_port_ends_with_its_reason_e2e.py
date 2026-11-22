"""A `goto` to an address Firefox refuses before it opens a channel ends, and says why.

`http://127.0.0.1:9/` is a restricted port (Firefox's list of ports it will not
connect to): the load is refused inside the docshell, before any channel exists
(`DocumentChannelChild::AsyncOpen` -> `nsDocShell::DoURILoad` fails ->
`InternalLoad` shows the error page). No channel ever reports progress, so
nothing that watches a request can end the navigation: the engine reports the
failure itself, with the load identifier and the status
(`StealthNavigationLoad::Failed` in `nsDocShell::DoURILoad`, folder
`automation/navigation` of the engine series, `docshell/base/StealthNavigation.cpp`),
and the client's `goto` raises with the name of the status,
`NS_ERROR_PORT_ACCESS_NOT_ALLOWED`. Until firefox-55 the navigation was announced
(`Page.navigationStarted`) and never ended: `goto` waited out its timeout, and
the reason was only in the URL of the error page.

**What is asserted, and not a clock.** The `goto` raises, the text of the error
is the status (and not a timeout: a deadline of 30 s is only the longest a
healthy run is given), and the page is usable afterwards.

**Negative control.** A `goto` to an allowed local port where nothing listens
also ends, with a DIFFERENT error (the connection is refused by the peer, a
channel did open): the first test's text is the answer to the port, not what
every failed `goto` says.

Why a page cares: a script that loads a refused address must learn it at once,
by the name Firefox gives, and not hang until its own deadline.
"""
from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler

import pytest

from _servers import LocalServer

pytestmark = pytest.mark.e2e

REFUSED_PORT_URL = "http://127.0.0.1:9/"
PORT_REASON = "NS_ERROR_PORT_ACCESS_NOT_ALLOWED"
REFUSED_REASON = "NS_ERROR_CONNECTION_REFUSED"


class _Page(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b"<html><title>after</title><body>after</body></html>"
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _goto_error(page, url):
    """The text of the error a `goto` ends with (the test fails if it ends without one).

    A failed `goto` is followed by Firefox's error page, a navigation of its own that commits after the call has
    raised (on Windows, late enough to land inside the next `goto`, which then reads as "interrupted by another
    navigation to" the refused address). The call is therefore made inside a wait for that navigation's
    `framenavigated`: it returns only once the error page has committed and the frame is quiet.
    """
    with page.expect_event("framenavigated", timeout=30_000):
        with pytest.raises(Exception) as ended:
            page.goto(url, timeout=30_000)
    return str(ended.value)


def test_a_goto_to_a_refused_port_ends_with_its_reason(firefox_binary):
    from invisible_playwright import InvisiblePlaywright

    # a port nobody listens on, and that Firefox allows: bound and never listening, so no other program can take it
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    closed_url = "http://127.0.0.1:%d/" % closed.getsockname()[1]
    site = LocalServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    try:
        with InvisiblePlaywright(seed=4242, binary_path=firefox_binary, headless=True) as browser:
            page = browser.new_context().new_page()

            refused = _goto_error(page, REFUSED_PORT_URL)
            assert PORT_REASON in refused, refused
            assert "Timeout" not in refused, refused

            # the control: the same call to an allowed port that is closed ends with another reason
            control = _goto_error(page, closed_url)
            assert REFUSED_REASON in control, control
            assert PORT_REASON not in control, control

            # and the page the refused navigation left behind is a page that navigates
            page.goto("http://127.0.0.1:%d/" % site.server_address[1], timeout=30_000)
            assert page.title() == "after"
    finally:
        site.shutdown()
        site.server_close()
        closed.close()
