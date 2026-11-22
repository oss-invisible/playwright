"""The Accept-Language of a shared worker and of a service worker is the one of the context that made them.

Characterisation of firefox-68 for the hook of fingerprint/locale-time/accept-language (nsHttpHandler.cpp) and of the data a worker with no
document of its own is started with. This test is RED on firefox-68: it states what the engine has to do, not what the published engine
does, and it is not to be merged into the suite of the 68. On firefox-68 the four requests below carry the SESSION's de-DE header even when
the context asked for it-IT, on Linux and on Windows alike.

A browser launched de-DE, a context that asks for it-IT and one that asks for nothing. The page of each registers a shared worker and a
service worker; the script of each and the ``fetch`` each makes are read where a server reads them, the raw header line on a local socket.
A retail Firefox 151 whose only pref is the ``intl.accept_languages`` the core writes for it-IT sends ``it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7``
on all four. The control is the other context, which keeps the launch's de-DE: its four requests carry the de-DE header and none carries
Italian, so what the first context sends is its own and not the session's.
"""
from __future__ import annotations

import socket
import threading

import pytest

pytestmark = pytest.mark.e2e

#: invisible_core's list for it-IT / de-DE, prepared the way Firefox prepares intl.accept_languages (retail 151, measured).
RETAIL_IT_IT = "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7"
RETAIL_DE_DE = "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"

#: A 1x1 PNG.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cfc0f01f0005000201a5f645400000000049454e44ae426082")

PAGE = b"""<!doctype html><meta charset=utf-8><title>al</title>
<link rel=stylesheet href=/style.css><script src=/script.js></script>
<iframe src=/frame></iframe><img src=/image.png>
<script>
(async () => {
  await fetch('/fetch').then((r) => r.text());
  await new Promise((ok) => { const x = new XMLHttpRequest(); x.open('GET', '/xhr'); x.onloadend = ok; x.send(); });
  await new Promise((ok) => { const w = new Worker('/worker.js'); w.onmessage = ok; });
  await new Promise((ok) => { const s = new SharedWorker('/shared.js'); s.port.onmessage = ok; });
  await navigator.serviceWorker.register('/sw.js');
  await navigator.serviceWorker.ready;
  document.title = 'done';
})();
</script>"""

BODIES = {
    "/": (PAGE, b"text/html"),
    "/frame": (b"<!doctype html><title>frame</title>", b"text/html"),
    "/style.css": (b"body{}", b"text/css"),
    "/script.js": (b"window.loaded = 1;", b"text/javascript"),
    "/image.png": (PNG, b"image/png"),
    "/worker.js": (b"fetch('/worker-fetch').then((r) => r.text()).then(() => postMessage(1));", b"text/javascript"),
    "/shared.js": (b"onconnect = (e) => { fetch('/shared-fetch').then((r) => r.text()).then(() => e.ports[0].postMessage(1)); };",
                   b"text/javascript"),
    # The install waits for the service worker's own fetch, so `ready` resolves after it.
    "/sw.js": (b"addEventListener('install', (e) => e.waitUntil(fetch('/sw-fetch').then((r) => r.text())));", b"text/javascript"),
}

#: The paths whose request is read: the script of a shared worker and a service worker, and the ``fetch`` each makes. The page also makes the
#: requests of the other kinds (the workers of the page need them to be started), which the sibling file reads.
KINDS = {
    "/shared.js": "shared worker script", "/shared-fetch": "shared worker fetch", "/sw.js": "service worker script",
    "/sw-fetch": "service worker fetch",
}


def _serve(seen):
    """A server that keeps each request's path and raw Accept-Language lines."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(16)

    def one(conn):
        buf = b""
        try:
            while True:
                while b"\r\n\r\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                head, buf = buf.split(b"\r\n\r\n", 1)
                lines = head.split(b"\r\n")
                path = lines[0].split(b" ")[1].decode("latin-1").split("?", 1)[0]
                seen.append((path, [ln.decode("latin-1") for ln in lines[1:] if ln.lower().startswith(b"accept-language:")]))
                body, ctype = BODIES.get(path, (b"ok", b"text/plain"))
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: " + ctype + b"\r\nCache-Control: no-store\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\n\r\n" + body)
        except OSError:
            return
        finally:
            conn.close()

    def loop():
        while True:
            try:
                conn, _ = s.accept()
            except OSError:
                return
            threading.Thread(target=one, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return s


def _headers_by_kind(seen):
    """Each kind of request -> the Accept-Language lines of EVERY request of that kind (a script is fetched again on an update check)."""
    out = {}
    for path, lines in seen:
        if path in KINDS:
            out.setdefault(KINDS[path], []).append(lines)
    return out


def test_a_context_list_is_the_retail_accept_language_in_shared_and_service_workers(firefox_binary):
    """⛔ A SHARED WORKER AND A SERVICE WORKER HAVE NO BROWSINGCONTEXT, AND TOOK THE SESSION'S LANGUAGE. ``new_context(locale=...)`` is the
    language of that context's pages and of its requests, and a worker that the context's page makes sends the same header as the page:
    the script of a shared worker and of a service worker and the ``fetch`` each makes carry the retail it-IT header of the it-IT context
    and the retail de-DE one of the context that asked for nothing, never the other's.
    """
    from invisible_playwright import InvisiblePlaywright

    seen = {"it": [], "de": []}
    servers = {name: _serve(log) for name, log in seen.items()}
    urls = {name: "http://127.0.0.1:%d/" % srv.getsockname()[1] for name, srv in servers.items()}
    try:
        with InvisiblePlaywright(seed=5150, binary_path=firefox_binary, headless=True, locale="de-DE", timezone="Europe/Berlin") as browser:
            for name, options in (("it", {"locale": "it-IT"}), ("de", {})):
                page = browser.new_context(**options).new_page()
                page.goto(urls[name], wait_until="load", timeout=30_000)
                page.wait_for_function("document.title === 'done'", timeout=30_000)
                languages = page.evaluate("[...navigator.languages]")
                assert languages == (["it-IT", "it", "en-US", "en"] if name == "it" else ["de-DE", "de", "en-US", "en"]), (
                    "navigator.languages of the %s context" % name)
    finally:
        for srv in servers.values():
            srv.close()
    for name, retail, other in (("it", RETAIL_IT_IT, RETAIL_DE_DE), ("de", RETAIL_DE_DE, RETAIL_IT_IT)):
        by_kind = _headers_by_kind(seen[name])
        assert set(by_kind) == set(KINDS.values()), "the %s context: kinds of request seen %r" % (name, sorted(by_kind))
        wrong = {kind: sent for kind, sent in by_kind.items() if any(lines != ["Accept-Language: " + retail] for lines in sent)}
        assert not wrong, "the %s context must send %r on the shared and service worker requests: %r" % (name, retail, wrong)
        assert not any(other in " ".join(lines) for sent in by_kind.values() for lines in sent), (
            "the %s context sent the other context's language" % name)
    assert RETAIL_IT_IT != RETAIL_DE_DE
