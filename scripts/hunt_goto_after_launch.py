#!/usr/bin/env python3
"""The hunt of a `goto` that never ends: the navigation is told started and then neither committed nor aborted.

Seen twice, both on Windows, both through a served browser driven by the stock Playwright client, both on the FIRST
`goto` of a page made right after the browser came up (`new_context()` then `new_page()`), both `nav-21`:

* gate-only 44746170375 (firefox-68 candidate), `test_popup_destinations_e2e.py::test_a_popup_to_a_204_is_never_announced
  [stock-client-open]`: `Page.goto: load not reached in 30s (our navigation nav-21 started and has neither committed nor
  been aborted, inflight requests: 0)`;
* gate 43889076032 (firefox-66 candidate), `test_networkidle_through_a_served_browser_with_routes_e2e.py::
  test_a_plain_route_handler_is_not_poisoned_by_a_request_the_page_cancelled[cancel_fetch]`: the same, networkidle.

This repeats that shape until it catches one, with the engine's own diagnostics on (e2e-hunt.yml, job goto-after-launch):
the Juggler's trace of its channels and navigations (`INVISIBLE_JUGGLER_TRACE_DIR`, an engine that has it) and the
engine's MOZ_LOG of the docshell, the document channel and the process isolation, one folder per browser session. The
folder of a session that answered is removed; the one of the session that caught it is kept, with the round written in
`caught.json`.

A round is a browser session. Shapes, one per round in turn:
  launch       a browser launched for the round: launch_server, connect, new_context(accept_downloads=True), new_page, goto
  launch-route the same, with a context route on **/* that continues every request and a page that cancels fetches
  reuse        one served browser for `--reuse` pages: new_context, new_page, goto, context.close

Exit 1 when it caught one (red means found), 0 when it did not.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OPENER = ("<!doctype html><title>opener</title>"
          + "".join("<a id=link%s target=_blank href='/d%s'>a</a> <button id=open%s onclick=\"window.open('/d%s')\">w</button><br>"
                    % (d, d, d, d) for d in "1234"))
CANCEL = ("<!doctype html><title>routed</title><p>x</p><script>for(let i=0;i<30;i++){const c=new AbortController();"
          "fetch('/api/data?i='+i,{signal:c.signal}).catch(()=>{});setTimeout(()=>c.abort(),i%7)}</script>")
MOZ_LOG = "timestamp,sync,DocumentChannel:5,ProcessIsolation:5,nsDocShell:4,DocLoader:4,BCWebProgress:4,JSActorService:4"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/main":
            body, ctype = OPENER.encode(), "text/html"
        elif path == "/cancel":
            body, ctype = CANCEL.encode(), "text/html"
        elif path == "/api/data":
            body, ctype = b'{"ok":1}', "application/json"
        else:
            body, ctype = b"not found", "text/plain"
        try:
            self.send_response(200 if body != b"not found" else 404)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            self.close_connection = True


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _session_dir(root, label):
    path = os.path.join(root, label)
    os.makedirs(path, exist_ok=True)
    # Read by the engine's parent at launch (the core passes the variable on; MOZ_LOG and MOZ_LOG_FILE are diagnostics it lets through).
    os.environ["INVISIBLE_JUGGLER_TRACE_DIR"] = os.path.abspath(path)
    os.environ["MOZ_LOG"] = MOZ_LOG
    os.environ["MOZ_LOG_FILE"] = os.path.join(os.path.abspath(path), "moz.log")
    return path


def _one_page(browser, port, shape, timeout_ms):
    context = browser.new_context(accept_downloads=True)
    try:
        if shape == "launch-route":
            context.route("**/*", lambda route: route.continue_())
        page = context.new_page()
        target = "/cancel" if shape == "launch-route" else "/main"
        page.goto("http://127.0.0.1:%d%s" % (port, target), timeout=timeout_ms,
                  wait_until="networkidle" if shape == "launch-route" else "load")
    finally:
        try:
            context.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("binary")
    ap.add_argument("--rounds", type=int, default=200, help="browser sessions")
    ap.add_argument("--reuse", type=int, default=3, help="pages of the reuse shape per served browser")
    ap.add_argument("--evidence", default="evidence/goto-hunt")
    ap.add_argument("--timeout-ms", type=int, default=30000, help="the goto's own timeout, the tests' (30 s)")
    ap.add_argument("--shapes", default="launch,launch-route,launch,reuse")
    a = ap.parse_args()

    sync_playwright = importlib.import_module("playwright.sync_api").sync_playwright
    from invisible_playwright._juggler.remote import launch_server

    os.makedirs(a.evidence, exist_ok=True)
    srv = _serve()
    port = srv.server_address[1]
    shapes = [s for s in a.shapes.split(",") if s]
    done = {s: 0 for s in shapes}
    caught = None
    with sync_playwright() as p:
        n = session = 0
        while n < a.rounds and caught is None:
            shape = shapes[session % len(shapes)]
            session += 1
            label = "r%04d-%s" % (n, shape)
            folder = _session_dir(a.evidence, label)
            per_browser = a.reuse if shape == "reuse" else 1
            started = time.time()
            server = launch_server(headless=True, binary_path=a.binary, locale="en-US", timezone="Europe/Rome")
            try:
                browser = p.firefox.connect(server.ws_endpoint)
                try:
                    for k in range(per_browser):
                        try:
                            _one_page(browser, port, shape, a.timeout_ms)
                        except Exception as exc:     # the goto's own timeout, or anything else: kept, and the hunt ends
                            text = "".join(traceback.format_exception_only(type(exc), exc)).strip()
                            caught = {"round": n, "shape": shape, "page_of_browser": k, "folder": label, "error": text,
                                      "seconds": round(time.time() - started, 1)}
                            print("CAUGHT round %d (%s, page %d of this browser): %s" % (n, shape, k, text), flush=True)
                            break
                        done[shape] += 1
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
            finally:
                server.close()
            n += 1
            if caught is None:
                shutil.rmtree(folder, ignore_errors=True)
                print("round %d (%s x%d) ok in %.1fs" % (n, shape, per_browser, time.time() - started), flush=True)
    srv.shutdown()
    summary = {"rounds": n, "per_shape": done, "caught": caught}
    with open(os.path.join(a.evidence, "caught.json" if caught else "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)
    print(json.dumps(summary), flush=True)
    return 1 if caught else 0


if __name__ == "__main__":
    sys.exit(main())
