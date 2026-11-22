"""The launch counter is ONE plain GET to the address the engine carries, sent by the browser when it starts, before any page exists (firefox-68, Linux and Windows).

What it fixes, in the published engine (firefox-68), for the hooks of `platform/launch-counter` (`browser/components/BrowserGlue.sys.mjs`: the hook in
`_beforeUIStartup`, which loads `StealthLaunchCounter.sys.mjs`): at start the parent process does `fetch(<counter address>, {method: "GET",
credentials: "omit", cache: "no-store"})` once and swallows the errors. The counter is mandatory (no pref turns it off or moves it: the core refuses both)
and it is part of the product, so a hook that is lost passes every test that never starts a browser and looks: the node test calls `fire()` directly. This
is the test that starts the browser and counts what leaves.

How it observes a request to a host of the internet without a network. The browser is given an HTTP proxy of the test (the road of the browser's own
requests: the counter is made by the parent, in no page). The proxy answers the `CONNECT` to the counter's host and terminates TLS with a leaf of a
lab CA that the profile trusts (the CA is added to the profile with the engine's own xpcshell, `_lab_pki.add_ca_to_profile`), so it reads the GET itself:
its method, its headers, its order. Every other request through the proxy is refused and recorded. Nothing leaves the machine. The address is read from the
module the binary runs (`resource:///modules/StealthLaunchCounter.sys.mjs`, in the `browser/omni.ja` next to the executable): in core 68.80.0
the core declares no counter address (no pref, no `USAGE_PING_URL`), the engine carries one constant, the same in every binary, and the test follows
that constant in the binary under test, it does not copy it.

What the test asserts:
* the GET arrives while NO page exists (the test waits for it before it opens a page, and the page server has received nothing at that moment);
* it is a GET with no `Cookie`, `Authorization`, `Referer` or `Origin` (no credentials, nothing of a page) and it asks not to be cached (`Cache-Control:
  no-cache`, which is what `cache: "no-store"` sends);
* once a page has been loaded and has made its own request to the same address, the proxy has seen exactly two GETs to it: the counter's and the page's.

Negative control: that page request. It goes through the same proxy to the same address, and the test tells it from the counter's by its `Referer`
(a request of a page carries it, the counter's has none), so the count of two shows the proxy sees a page's request and that the counter's is not one
of them, and a launch that sent the counter twice, or from the page, is red.
"""
from __future__ import annotations

import itertools
import re
import socket
import ssl
import threading
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from invisible_core.jar import open_jar

from _lab_egress import LAB_EGRESS_IP
from _lab_pki import LabCa, add_ca_to_profile
from _servers import LocalServer
from _socks_servers import declare_exit, forget_exit
from _waits import wait_until

pytestmark = pytest.mark.e2e

PAGE = b"<!doctype html><html><head><meta charset=utf-8><title>counter</title></head><body><p>page</p></body></html>"


class Order:
    """One order for what the two servers hear: the number of each request, across both."""

    def __init__(self):
        self._numbers = itertools.count(1)
        self._lock = threading.Lock()

    def next(self):
        with self._lock:
            return next(self._numbers)


class CounterProxy:
    """An HTTP proxy on 127.0.0.1 that stands in for the counter's host: it terminates TLS for it and records the requests it reads there."""

    def __init__(self, host, port, leaf, key, order):
        self.host, self.port, self.order = host, port, order
        self.gets = []          # every request read inside TLS for the counter's host, in order
        self.other = []         # every CONNECT or request for anything else: refused
        self.handshakes = []
        self._lock = threading.Lock()
        self._tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._tls.load_cert_chain(str(leaf), str(key))
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(32)
        self.server = "http://127.0.0.1:%d" % self._listener.getsockname()[1]
        self._closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self._closed = True
        try:
            self._listener.close()
        except OSError:
            pass

    def _accept(self):
        while not self._closed:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    @staticmethod
    def _read_head(sock):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                return None
            data += chunk
        return data.split(b"\r\n\r\n", 1)[0].decode("latin-1")

    @staticmethod
    def _parse(head):
        lines = head.split("\r\n")
        method, target = lines[0].split(" ")[:2]
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        return method, target, headers

    def _handle(self, conn):
        try:
            conn.settimeout(30)
            head = self._read_head(conn)
            if head is None:
                return
            method, target, _headers = self._parse(head)
            if method == "CONNECT" and target == "%s:%d" % (self.host, self.port):
                conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self._serve_tls(conn)
            else:
                with self._lock:
                    self.other.append("%s %s" % (method, target))
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except (OSError, ssl.SSLError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _serve_tls(self, conn):
        try:
            tls = self._tls.wrap_socket(conn, server_side=True)
        except (ssl.SSLError, OSError) as failure:
            with self._lock:
                self.handshakes.append("failed: %s" % failure)
            return
        with self._lock:
            self.handshakes.append("ok")
        head = self._read_head(tls)
        if head is None:        # a speculative connection that never sends a request
            return
        method, target, headers = self._parse(head)
        with self._lock:
            self.gets.append({"order": self.order.next(), "method": method, "target": target, "headers": headers})
        body = b"ping\n"
        tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))

    def seen(self):
        with self._lock:
            return list(self.gets)


class PageServer:
    """The page of the test, on 127.0.0.1; records the order of every request it gets."""

    def __init__(self, order):
        import http.server

        self.requests = []
        requests = self.requests

        class Handler(http.server.BaseHTTPRequestHandler):
            timeout = 5

            def do_GET(self):
                requests.append((order.next(), self.path))
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(PAGE)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(PAGE)

            def log_message(self, *args):
                pass

        self._server = LocalServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/" % self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


def counter_address(firefox_binary):
    """The address the engine's launch counter fetches, read from the module the browser runs (the one constant of
    `StealthLaunchCounter.sys.mjs`, inside `browser/omni.ja` beside the executable), with the core's jar reader."""
    omni = Path(firefox_binary).resolve().parent / "browser" / "omni.ja"
    # The core's own reader (`invisible_core.jar`): the omni.ja of a PGO build is Mozilla's optimized jar, which zipfile refuses.
    source = open_jar(omni).read("modules/StealthLaunchCounter.sys.mjs").decode("utf-8")
    found = re.findall(r'USAGE_PING_URL\s*=\s*"([^"]+)"', source)
    assert len(found) == 1, "the counter module of %s declares %d addresses: %s" % (omni, len(found), found)
    return found[0]


def test_the_browser_sends_one_plain_get_to_the_counter_before_any_page(firefox_binary, tmp_path):
    from invisible_playwright import InvisiblePlaywright

    USAGE_PING_URL = counter_address(firefox_binary)

    counter = urlsplit(USAGE_PING_URL)
    assert counter.scheme == "https" and counter.hostname, USAGE_PING_URL
    port = counter.port or 443

    order = Order()
    ca = LabCa(tmp_path)
    leaf, key = ca.leaf("counter", [counter.hostname], [], None)
    profile = tmp_path / "profile"
    for line in add_ca_to_profile(firefox_binary, profile, ca.ca_b64, tmp_path / "xpcshell"):
        print("LAB-CA %s" % line)
    proxy = CounterProxy(counter.hostname, port, leaf, key, order)
    pages = PageServer(order)
    stop = threading.Event()
    declare_exit(proxy.server, LAB_EGRESS_IP)      # the proxy says where it leaves from: no request of the exit goes to the internet
    try:
        with InvisiblePlaywright(seed=42, binary_path=firefox_binary, headless=True, humanize=False,
                                 profile_dir=profile, proxy={"server": proxy.server}) as context:
            # Before any page: the counter arrives with no page of ours open, and the page server has heard nothing.
            arrived = wait_until(lambda: proxy.seen(), 120, lambda: stop.wait(0.25))
            at_that_moment = list(pages.requests)
            detail = ("handshakes %s; refused through the proxy: %s" % (proxy.handshakes[:6], proxy.other[:12]))
            assert arrived, "the browser sent no request to the counter's address %s after it started. %s" % (USAGE_PING_URL, detail)
            assert at_that_moment == [], "a page request had been made when the counter arrived: %s" % (at_that_moment,)
            first = proxy.seen()[0]

            # The control: a page of ours makes its own request to the same address, through the same proxy.
            page = context.new_page()
            page.goto(pages.url, wait_until="load", timeout=60_000)
            page.evaluate("(u) => fetch(u, {mode: 'no-cors'}).then(() => 'done', () => 'failed')", USAGE_PING_URL)
            both = wait_until(lambda: len(proxy.seen()) >= 2, 60, lambda: stop.wait(0.25))
            gets = proxy.seen()
    finally:
        forget_exit(proxy.server)
        proxy.close()
        pages.close()

    assert both, "the request of the page to the counter's address never reached the proxy: the sink would be blind to a second request. %s" % (detail,)
    assert len(gets) == 2, "expected the counter's GET and the page's, got %d: %s" % (len(gets), [(g["method"], g["target"]) for g in gets])
    assert gets[0] == first
    assert first["method"] == "GET" and first["target"] == counter.path + (("?" + counter.query) if counter.query else ""), first
    for forbidden in ("cookie", "authorization", "proxy-authorization", "referer", "origin"):
        assert forbidden not in first["headers"], "the counter's GET carries %s: %s" % (forbidden, first["headers"])
    assert "no-cache" in first["headers"].get("cache-control", "").lower(), first["headers"]
    # The page's own request is told apart by what only a page's request has.
    assert gets[1]["headers"].get("referer", "").startswith(pages.url[:-1]), gets[1]["headers"]
