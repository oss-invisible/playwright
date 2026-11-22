"""Iterating a DOM iterable from `page.evaluate` works, one case per iterable.

Upstream Playwright evaluates in the page's main world, so `[...x]` of any
iterable works there. Our wrapper evaluates in the caller's world (Xrays), and
on retail-like Windows `page.evaluate("async () => [...(await
navigator.gpu.requestAdapter()).features]")` failed with "Xray wrapping of
iterators not supported". This file holds the iterables that do work: one
test per case, each spreading an iterable inside `evaluate` and returning plain
values, so a case that fails names the iterable that does and the error text.

A page reads these iterables with its own scripts (every site does
`[...new URLSearchParams(location.search)]` or `[...document.querySelectorAll(..)]`)
and a framework or a test harness does the same from `evaluate`, so a call that
throws where stock Playwright answers is a product difference.

Cases: URLSearchParams, FormData (empty and with a field), Headers, `document.fonts`,
`querySelectorAll`, and a Map created by the PAGE's own script: all green on firefox-68
(run 44979351495, Linux and Windows), so this file characterizes them. The WebGPU
`adapter.features` Set-like is NOT here: on firefox-68 spreading it from `evaluate` throws
"Xray wrapping of iterators not supported" on both systems; its red proof is kept on the
branch tests/dom-iterables-from-evaluate.
"""
from __future__ import annotations

import http.server
import threading

import pytest

pytestmark = pytest.mark.e2e

PAGE = b"""<!doctype html><html><head><meta charset="utf-8"><title>iterables</title></head>
<body><p>one</p><p>two</p>
<script>window.m = new Map([["x", 1]]);</script></body></html>"""


# id -> (expression run by page.evaluate, check on its answer, what is expected)
CASES = {
    "url-search-params": (
        '() => [...new URLSearchParams("a=1")]',
        lambda v: v == [["a", "1"]], '[["a", "1"]]'),
    "form-data-empty": (
        "() => [...new FormData()]",
        lambda v: v == [], "[]"),
    "form-data-with-a-field": (
        '() => { const f = new FormData(); f.append("k", "v"); return [...f]; }',
        lambda v: v == [["k", "v"]], '[["k", "v"]]'),
    "headers": (
        '() => [...new Headers({a: "1"})]',
        lambda v: v == [["a", "1"]], '[["a", "1"]]'),
    "document-fonts": (
        "() => { const a = [...document.fonts]; return [Array.isArray(a), a.length >= 0]; }",
        lambda v: v == [True, True], "[true, true]"),
    "query-selector-all": (
        '() => [...document.querySelectorAll("*")].length',
        lambda v: isinstance(v, int) and v > 0, "a length > 0"),
    "a-map-made-by-the-page": (
        "() => [...window.m]",
        lambda v: v == [["x", 1]], '[["x", 1]]'),
}


@pytest.fixture(scope="module")
def site():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)

        def log_message(self, *a):
            pass

    from _servers import LocalServer

    srv = LocalServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def browser(firefox_binary):
    # One browser per case: a crash in one case reddens that case only, not the ones after it.
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=4321, binary_path=firefox_binary,
                             headless=True) as b:
        yield b


@pytest.mark.parametrize("case", list(CASES), ids=list(CASES))
def test_iterating_from_evaluate_works(browser, site, case):
    expression, ok, expected = CASES[case]
    page = browser.new_page()
    try:
        page.goto(site, wait_until="domcontentloaded")
        try:
            answer = page.evaluate(expression)
        except Exception as exc:  # noqa: BLE001 - the error text of any kind is the finding
            pytest.fail(f"{case}: evaluate threw {type(exc).__name__}: {exc}")
        assert ok(answer), f"{case}: evaluate answered {answer!r}, expected {expected}"
    finally:
        page.close()
