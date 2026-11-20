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

Any exception of a round is kept, not only the goto's: `caught.json` says the `phase` it happened in (`server`: the stand-in web
server of the hunt; `launch`: launch_server, which starts the served browser; `connect`: the client's start and its connection to the
server; `context`; `page`; `goto`; `close`: the closes of the context, the browser and the server), its `error` and full `traceback`,
the `folder` of the session (kept), and a `kind`: `goto-hung` when the message is the goto's timeout with "neither committed nor been
aborted" (the defect that is hunted), `other` for anything else (a defect of the hunt or of the machine, not the one hunted). A later
exception of the same round (a close after a goto that hung) goes to `also`.
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


HUNG = "neither committed nor been aborted"
#: What the end of a session may take, in seconds: a safety net, the wait ends when the session's processes are gone.
SESSION_END_S = 30.0


def classify(text):
    """`goto-hung` for the goto's timeout that says the navigation neither committed nor been aborted, `other` for the rest."""
    return "goto-hung" if HUNG in text else "other"


class Round:
    """What happened in one round: the phase it is in, and every exception with the phase it was raised in."""

    def __init__(self):
        self.phase = "launch"
        self.errors = []

    def fail(self, exc, phase=None):
        text = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        self.errors.append({"phase": phase or self.phase, "kind": classify(text), "error": text,
                            "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))})
        return self.errors[-1]

    def caught(self, n, shape, folder, started, page_of_browser=None):
        """The entry of `caught.json`: the first exception, the later ones under `also`; None when the round had none."""
        if not self.errors:
            return None
        first = self.errors[0]
        entry = {"round": n, "shape": shape, "phase": first["phase"], "kind": first["kind"],
                 "page_of_browser": page_of_browser, "folder": folder, "error": first["error"],
                 "traceback": first["traceback"], "seconds": round(time.time() - started, 1)}
        if len(self.errors) > 1:
            entry["also"] = self.errors[1:]
        return entry


def _one_page(browser, port, shape, timeout_ms, rnd):
    """One page. An exception is recorded in `rnd` (with its phase) BEFORE the context is closed, so a close that fails too comes after it."""
    context = None
    try:
        rnd.phase = "context"
        context = browser.new_context(accept_downloads=True)
        if shape == "launch-route":
            context.route("**/*", lambda route: route.continue_())
        rnd.phase = "page"
        page = context.new_page()
        target = "/cancel" if shape == "launch-route" else "/main"
        rnd.phase = "goto"
        page.goto("http://127.0.0.1:%d%s" % (port, target), timeout=timeout_ms,
                  wait_until="networkidle" if shape == "launch-route" else "load")
    except Exception as exc:
        rnd.fail(exc)
    finally:
        if context is not None:
            try:
                context.close()
            except Exception as exc:
                rnd.fail(exc, "close")


def _name(proc):
    try:
        return proc.name()
    except Exception:
        return "?"


def _wait_session_gone(token, timeout=SESSION_END_S):
    """After the closes: wait for the session's reaper and for its processes to be gone (what the e2e tests do, tests/_session_end.py),
    so that the folder of the session can be removed on Windows. Returns what is left, for a message: [] when nothing."""
    if not token:
        return []
    try:
        from invisible_core.process import SessionToken, find_processes, wait_until_gone
        from invisible_playwright._juggler._reaped import wait_reaped
    except Exception as exc:
        return ["cannot wait for the session (%s: %s)" % (type(exc).__name__, exc)]
    token = SessionToken(getattr(token, "value", token))
    left = []
    if not wait_reaped(token, timeout=timeout):
        left.append("the reaper of the launch has not finished in %s s" % timeout)
    if not wait_until_gone(token, timeout=timeout):
        left.append("processes of the session alive after %s s: %s" % (
            timeout, [(getattr(proc, "pid", None), _name(proc)) for proc in find_processes(token)]))
    return left


def _session_token(server):
    """The value of the session's token, read before the close: the teardown empties the session's own."""
    session = getattr(server, "_session", None)
    return getattr(getattr(session, "_session_token", None), "value", None)


def _run_round(playwright, launch_server, a, port, shape):
    """One browser session. Returns (Round, pages answered, session token); every exception is in the Round."""
    rnd = Round()
    per_browser = a.reuse if shape == "reuse" else 1
    answered = 0
    server = browser = None
    token = None
    try:
        rnd.phase = "launch"
        server = launch_server(headless=True, binary_path=a.binary, locale="en-US", timezone="Europe/Rome")
        token = _session_token(server)
        rnd.phase = "connect"
        browser = playwright.firefox.connect(server.ws_endpoint)
        for _ in range(per_browser):
            _one_page(browser, port, shape, a.timeout_ms, rnd)
            if rnd.errors:
                break
            answered += 1
    except Exception as exc:         # the goto's own timeout, or anything else: kept, and the hunt ends
        rnd.fail(exc)
    finally:
        rnd.phase = "close"
        for closing in (browser, server):
            if closing is not None:
                try:
                    closing.close()
                except Exception as exc:
                    rnd.fail(exc, "close")
    return rnd, answered, token


def _remove_folder(folder, token):
    """Removes the session's folder once the session's processes are gone. What cannot be removed is no error: it is printed."""
    left = _wait_session_gone(token)
    shutil.rmtree(folder, ignore_errors=True)
    if os.path.exists(folder):
        print("folder left: %s (%s)" % (folder, "; ".join(left) or "nothing of the session alive: files in use by something else"),
              flush=True)
    elif left:
        print("folder removed, but: %s" % "; ".join(left), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("binary")
    ap.add_argument("--rounds", type=int, default=200, help="browser sessions")
    ap.add_argument("--reuse", type=int, default=3, help="pages of the reuse shape per served browser")
    ap.add_argument("--evidence", default="evidence/goto-hunt")
    ap.add_argument("--timeout-ms", type=int, default=30000, help="the goto's own timeout, the tests' (30 s)")
    ap.add_argument("--shapes", default="launch,launch-route,launch,reuse")
    a = ap.parse_args()

    os.makedirs(a.evidence, exist_ok=True)
    shapes = [s for s in a.shapes.split(",") if s]
    done = {s: 0 for s in shapes}
    caught = None
    n = 0
    srv = pw = launch_server = None

    def startup_failure(phase, exc):
        rnd = Round()
        rnd.fail(exc, phase)
        return rnd.caught(0, None, None, time.time())

    try:
        srv = _serve()
    except Exception as exc:
        caught = startup_failure("server", exc)
    if caught is None:
        try:
            sync_playwright = importlib.import_module("playwright.sync_api").sync_playwright
            from invisible_playwright._juggler.remote import launch_server
            pw = sync_playwright().start()
        except Exception as exc:
            caught = startup_failure("connect", exc)
    if caught is None:
        port = srv.server_address[1]
        session = 0
        while n < a.rounds and caught is None:
            shape = shapes[session % len(shapes)]
            session += 1
            label = "r%04d-%s" % (n, shape)
            folder = _session_dir(a.evidence, label)
            started = time.time()
            rnd, answered, token = _run_round(pw, launch_server, a, port, shape)
            done[shape] += answered
            caught = rnd.caught(n, shape, label, started, page_of_browser=answered)
            n += 1
            if caught is None:
                _remove_folder(folder, token)
                print("round %d (%s x%d) ok in %.1fs" % (n, shape, answered, time.time() - started), flush=True)
            else:
                print("CAUGHT round %d (%s, phase %s, kind %s): %s\n%s" % (
                    caught["round"], shape, caught["phase"], caught["kind"], caught["error"], caught["traceback"]), flush=True)
                left = _wait_session_gone(token)     # the folder stays; its moz.log is complete once the processes are gone
                if left:
                    print("session not gone: %s" % "; ".join(left), flush=True)
    if pw is not None:
        try:
            pw.stop()
        except Exception as exc:
            print("playwright.stop(): %s: %s" % (type(exc).__name__, exc), flush=True)
    if srv is not None:
        srv.shutdown()
    summary = {"rounds": n, "per_shape": done, "caught": caught}
    with open(os.path.join(a.evidence, "caught.json" if caught else "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)
    brief = dict(summary, caught=None if caught is None else {k: v for k, v in caught.items() if k not in ("traceback", "also")})
    print(json.dumps(brief), flush=True)
    return 1 if caught else 0


if __name__ == "__main__":
    sys.exit(main())
