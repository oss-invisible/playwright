#!/usr/bin/env python3
"""A MEASUREMENT, not a gate: one scenario, run on this package's browser and on stock Playwright's Firefox, side by side.

The scenario is the one of tests/test_navigation_across_processes_e2e.py::test_a_second_goto_to_a_third_site_interrupts_the_first_and_lands:

  * sites isolated as in that file (`fission.autostart`, strategy 1), a local server on a thread, three hosts a.test, b.test and c.test
    on its one port, told to the browser by `network.dns.localDomains` (a standard Firefox pref: the same line reaches both browsers);
  * the page is at a.test/plain; goto 1 goes to b.test/partial, whose headers and first bytes the server sends and whose rest it HOLDS
    until the scenario is over; as soon as the client has heard the response of /partial, goto 2 goes to c.test/second
    (domcontentloaded, timeout 15 s).

Per repetition it records what the question "did the second goto leave the browser, and how did the two gotos end" needs:

  * whether the request of /second reached the server, and at how many ms after the start of goto 2 (the server and the client read the
    same clock, `time.perf_counter()`, in this one process), and whether it came before or after goto 2 had ended;
  * how goto 1 ended (the error and its class), and at how many ms;
  * how goto 2 ended (it returned, or the error), at how many ms.

A new BROWSER per repetition, not a new context: what the navigation depends on is the processes the browser has (the spare process it
preallocated, the one that holds a.test, the ones it starts for b.test and c.test); a new context of a warmed browser would inherit them and
every repetition after the first would measure another state. A cold browser per repetition is the state a user's first navigations have,
and it is the same for both. The two browsers alternate (ours, stock, ours, ...) so a runner that slows down in the middle of the run slows both.

Nothing here decides: the exit code is non-zero only when the script itself could not measure (a browser that never launched in any
repetition, a binary that is not there). The result is one JSON per browser and system and a summary printed to the log.

    python scripts/hunt_second_goto_vs_stock.py --binary "$FF" --out-dir out            # both browsers, 20 repetitions each
    python scripts/hunt_second_goto_vs_stock.py --browser stock --repetitions 3 --out-dir out
    python scripts/hunt_second_goto_vs_stock.py --binary "$FF" --browser invisible --trace --out-dir out   # the protocol trace of ours

`--trace` (our browser only) adds to every repetition what the client heard of the protocol, and when: see "the protocol trace" below.
With `--trace` the summary also lists every HANG (a goto 2 that ended by its own timeout): whether the request of goto 2 was in the parent
(a `Network.requestWillBeSent` carrying the navigation id of goto 2), how that request ended, and whether the server saw it; the line
`HUNTTOTAL` counts the repetitions and the hangs. `--budget-min N` starts no new repetition after N minutes.
Stock Firefox is the one `python -m playwright install firefox` downloads for the `playwright` package installed (scripts/playwright_pin.txt).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingTCPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent

#: The prefs of the file the scenario comes from (FISSION_ON there): the same for both browsers.
FISSION_ON = {
    "fission.autostart": True,
    "fission.webContentIsolationStrategy": 1,
    "dom.ipc.processCount.webIsolated": 4,
    "network.dns.localDomains": "a.test,b.test,c.test",
}

SEED = 42                      # the seed of the test this comes from (only ours reads it)
REPETITIONS = 20
GOTO1_TIMEOUT_MS = 30000       # the first goto ends when the second replaces it, or when the held answer is let go
GOTO2_TIMEOUT_MS = 15000       # as asked: domcontentloaded, 15 s
HEAR_RESPONSE_S = 15.0         # how long the client waits to hear the response of /partial before it gives the repetition up
SETTLE_S = 1.0                 # after the end of the scenario and the release: a /second that comes late is still counted
LAUNCH_S = 90.0                # a launch that takes longer is a repetition lost, not a hang
CLOSE_S = 30.0
REPETITION_S = 150.0           # the whole scenario of one repetition: nothing may hold the run


def _ms(seconds):
    return None if seconds is None else round(seconds * 1000.0, 1)


class _Server(ThreadingTCPServer):
    """A thread per connection and daemon threads: a connection the browser opens and leaves cannot hold the server or its shutdown."""
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        ThreadingTCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class Sites:
    """The local server of one repetition. `arrived` has the time (perf_counter) each path was first asked for."""

    def __init__(self):
        self.arrived = {}                 # path -> perf_counter of the first request
        self.release = threading.Event()  # what the server holds until the scenario is over
        owner = self

        class Handler(BaseHTTPRequestHandler):
            timeout = 10

            def log_message(self, *args):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                owner.arrived.setdefault(path, time.perf_counter())
                try:
                    if path == "/partial":
                        # the headers and the start of the body arrive, the rest is held: the response is there, the navigation has
                        # changed process, the document is not complete
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html")
                        self.send_header("Content-Length", "200")
                        self.end_headers()
                        self.wfile.write(b"<!doctype html><title>partial</title><p>start")
                        self.wfile.flush()
                        owner.release.wait()
                        self.wfile.write(b" " * 156)
                        return
                    body = ("<!doctype html><title>%s</title><p>%s" % (path, path)).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def at(self, host, path):
        return "http://%s:%d%s" % (host, self.port, path)

    def close(self):
        self.release.set()
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
        self.thread.join(timeout=10)


# ---------------------------------------------------------------- the two browsers

class _Ours:
    """invisible_playwright, the published engine ($FF), the prefs of the scenario."""
    name = "invisible"

    def __init__(self, binary):
        self.binary = binary
        self._cm = None

    async def launch(self):
        from invisible_playwright.async_api import InvisiblePlaywright

        self._cm = InvisiblePlaywright(seed=SEED, binary_path=self.binary, humanize=False, headless=True, extra_prefs=FISSION_ON)
        browser = await self._cm.__aenter__()
        return browser

    async def close(self):
        cm, self._cm = self._cm, None
        if cm is not None:
            await cm.__aexit__(None, None, None)


class _Stock:
    """Stock Playwright with its own Firefox: no engine of ours, no seed, the same prefs."""
    name = "stock"

    def __init__(self, binary=None):
        self._pw = None
        self._browser = None

    async def launch(self):
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.firefox.launch(headless=True, firefox_user_prefs=dict(FISSION_ON))
        return self._browser

    async def close(self):
        browser, self._browser = self._browser, None
        pw, self._pw = self._pw, None
        try:
            if browser is not None:
                await browser.close()
        finally:
            if pw is not None:
                await pw.stop()


# ---------------------------------------------------------------- one repetition

def _error_class(message):
    if message is None:
        return None
    if "is interrupted by another navigation" in message:
        return "interrupted by another navigation"
    if "NS_BINDING_ABORTED" in message:
        return "NS_BINDING_ABORTED"
    if "Timeout" in message and "exceeded" in message:
        return "timeout"
    if "load not reached" in message:
        return "load not reached"
    return "other"


async def _scenario(page, sites, trace=None):
    """The measurement. Every time is perf_counter; the row says them in ms from the start of goto 1 (`t=0`)."""
    if trace is not None:
        trace.attach(page)         # before the first goto: nothing the navigations of the page say is missed
    heard = {}                                              # path -> perf_counter the client heard its response
    page.on("response", lambda r: heard.setdefault(urlsplit(r.url).path, time.perf_counter()))

    await page.goto(sites.at("a.test", "/plain"), wait_until="domcontentloaded", timeout=GOTO2_TIMEOUT_MS)

    ends = {}

    async def goto1():
        try:
            await page.goto(sites.at("b.test", "/partial"), wait_until="domcontentloaded", timeout=GOTO1_TIMEOUT_MS)
            return None
        except Exception as e:
            return str(e)
        finally:
            ends["first"] = time.perf_counter()

    t_first = time.perf_counter()
    if trace is not None:
        trace.t0 = t_first
    first = asyncio.ensure_future(goto1())
    deadline = time.monotonic() + HEAR_RESPONSE_S
    while "/partial" not in heard or "/partial" not in sites.arrived:
        if first.done() or time.monotonic() > deadline:
            break
        await asyncio.sleep(0.02)
    row = {"response_heard": "/partial" in heard}
    second_error = None
    second_status = None
    t_second = None
    if row["response_heard"]:
        t_second = time.perf_counter()
        if trace is not None:
            trace.t_second = t_second
        try:
            response = await page.goto(sites.at("c.test", "/second"), wait_until="domcontentloaded", timeout=GOTO2_TIMEOUT_MS)
            second_status = response.status if response is not None else None
        except Exception as e:
            second_error = str(e)
        ends["second"] = time.perf_counter()
    # the rest of the first response is sent only now: it can have ended only by being replaced (or, if it did not, by this)
    sites.release.set()
    try:
        first_error = await asyncio.wait_for(first, 20)
    except asyncio.TimeoutError:
        first.cancel()
        first_error = "(goto 1 still pending 20 s after the held response was let go)"
        ends.setdefault("first", time.perf_counter())
    await asyncio.sleep(SETTLE_S)

    def at(stamp):
        return None if stamp is None else _ms(stamp - t_first)

    second_request = sites.arrived.get("/second")
    row.update({
        "partial_request_ms": at(sites.arrived.get("/partial")),
        "partial_response_heard_ms": at(heard.get("/partial")),
        "goto2_start_ms": at(t_second),
        "second_request_arrived": second_request is not None,
        "second_request_ms": at(second_request),
        "second_request_after_goto2_start_ms": None if second_request is None or t_second is None else _ms(second_request - t_second),
        "second_request_before_goto2_end": None if second_request is None or "second" not in ends else second_request <= ends["second"],
        "goto1_end_ms": at(ends.get("first")),
        "goto1_error": first_error,
        "goto1_error_class": _error_class(first_error) if first_error is not None else "none (it returned)",
        "goto2_outcome": None if t_second is None else ("error" if second_error is not None else "commit"),
        "goto2_ms": None if t_second is None else _ms(ends["second"] - t_second),
        "goto2_error": second_error,
        "goto2_error_class": _error_class(second_error),
        "goto2_status": second_status,
        "url_after": page.url,
        "requests_at_the_server": sorted(sites.arrived),
    })
    return row


async def _repetition(maker, index, binary, trace_on=False):
    row = {"repetition": index, "browser": maker.name, "launched": False}
    sites = Sites()
    kind = maker(binary)
    trace = Trace() if trace_on else None
    try:
        try:
            browser = await asyncio.wait_for(kind.launch(), LAUNCH_S)
            row["launched"] = True
            try:
                row["browser_version"] = browser.version
            except Exception:
                pass
        except Exception as e:
            row["launch_error"] = "%s: %s" % (type(e).__name__, e)
            return row
        try:
            page = await browser.new_page()
            row.update(await asyncio.wait_for(_scenario(page, sites, trace), REPETITION_S))
        except Exception as e:
            row["scenario_error"] = "%s: %s" % (type(e).__name__, e)
    finally:
        if trace is not None:
            try:
                row.update(trace.finish(row))
            except Exception as e:                      # the trace never costs the measurement
                row["trace_error"] = "%s: %s" % (type(e).__name__, e)
        sites.close()
        try:
            await asyncio.wait_for(kind.close(), CLOSE_S)
        except BaseException as e:
            row["close_error"] = "%s: %s" % (type(e).__name__, e)
    return row


# ---------------------------------------------------------------- the protocol trace

#: What the client hears, by method: the events of the navigations of the pages, of the requests of their documents, of their contexts, of the targets.
_TRACED = frozenset((
    "Page.navigationStarted", "Page.navigationCommitted", "Page.navigationAborted", "Page.sameDocumentNavigation", "Page.eventFired",
    "Page.frameAttached", "Page.frameDetached",
    "Network.requestWillBeSent", "Network.responseReceived", "Network.requestFinished", "Network.requestFailed",
    "Runtime.executionContextsCleared", "Runtime.executionContextCreated",
    "Browser.attachedToTarget", "Browser.detachedFromTarget",
))
#: The commands that start, stop or replace a navigation: what the client SAID, with the reply of `Page.navigate` (the id of its navigation).
_SAID = frozenset(("Page.navigate", "Page.stop", "Page.reload", "Page.goBack", "Page.goForward", "Page.close"))
#: The params kept, per method (the rest is the bulk: headers, timing, security details).
_KEPT = {
    "Page.navigationStarted": ("frameId", "navigationId"),
    "Page.navigationCommitted": ("frameId", "navigationId", "url"),
    "Page.navigationAborted": ("frameId", "navigationId", "errorText"),
    "Page.sameDocumentNavigation": ("frameId", "url"),
    "Page.eventFired": ("frameId", "name"),
    "Page.frameAttached": ("frameId", "parentFrameId"),
    "Page.frameDetached": ("frameId",),
    "Network.requestWillBeSent": ("requestId", "frameId", "navigationId", "url", "redirectedFrom", "cause"),
    "Network.responseReceived": ("requestId", "status", "fromCache"),
    "Network.requestFinished": ("requestId",),
    "Network.requestFailed": ("requestId", "errorCode"),
    "Runtime.executionContextCreated": ("executionContextId", "auxData"),
    "Runtime.executionContextsCleared": (),
    "Browser.attachedToTarget": ("sessionId",),
    "Browser.detachedFromTarget": ("sessionId",),
}


def _reduced(method, params):
    out = {k: params[k] for k in _KEPT.get(method, ()) if params.get(k) is not None}
    if method == "Browser.attachedToTarget":
        info = params.get("targetInfo") or {}
        out.update({k: info[k] for k in ("targetId", "type", "url") if k in info})
    return out


class Trace:
    """Every protocol event of the traced kinds that the CLIENT receives, with the time it received it.

    The hook is the public one of the connection of the core, `Connection.add_listener(fn)` (invisible_core.juggler.connection): `fn(method,
    params, session)` is called on the reader thread of the connection for every event the engine sends, after the subscribers the page
    registered earlier. The connection is found the way tests/_screencast_evidence.py finds it:
    `page._impl_obj._connection._transport._server.object(guid).conn`. The commands that start, stop or replace a navigation are seen on the
    same connection (`send_placed`, wrapped on THIS instance only and passing everything through unchanged; removed in `finish`), so that the id of
    each goto is the one the engine answered to its `Page.navigate`. Nothing of src/ is touched.

    Times are `time.perf_counter()`, the clock of the rest of the script; `finish` turns them into ms from the start of goto 1 (`t=0`).
    Of the network only the requests of DOCUMENTS are kept: the ones that carry a navigation id (or whose cause says document), and what follows them.
    """

    def __init__(self):
        self.t0 = None            # perf_counter of the start of goto 1
        self.t_second = None      # perf_counter of the start of goto 2
        self._raw = []            # (perf_counter, method, params, session)
        self._documents = set()   # request ids of documents
        self._conn = None

    # -- the hook
    def attach(self, page):
        impl = page._impl_obj
        conn = impl._connection._transport._server.object(impl._guid).conn
        self._conn = conn
        conn.add_listener(self._on_event)
        original = conn.send_placed
        raw = self._raw

        def send_placed(method, params=None, session=None, timeout=30.0):
            said = method in _SAID
            if said:
                raw.append((time.perf_counter(), "send " + method, dict(params or {}), session))
            try:
                answer = original(method, params, session, timeout)
            except BaseException as failure:
                if said:
                    raw.append((time.perf_counter(), "error " + method, {"error": str(failure)[:300]}, session))
                raise
            if said:
                result = answer[0] if isinstance(answer, tuple) else answer
                raw.append((time.perf_counter(), "reply " + method, dict(result) if isinstance(result, dict) else {}, session))
            return answer

        conn.send_placed = send_placed

    def _on_event(self, method, params, session):
        if method not in _TRACED:
            return
        now = time.perf_counter()
        params = params if isinstance(params, dict) else {}
        if method == "Network.requestWillBeSent":
            if params.get("navigationId") or "document" in str(params.get("cause", "")).lower():
                self._documents.add(params.get("requestId"))
        elif method.startswith("Network.") and params.get("requestId") not in self._documents:
            return
        self._raw.append((now, method, params, session))

    def detach(self):
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.remove_listener(self._on_event)
            conn.__dict__.pop("send_placed", None)          # back to the method of the class

    # -- the result
    def finish(self, row):
        self.detach()
        raw = sorted(self._raw, key=lambda item: item[0])
        base = self.t0 if self.t0 is not None else (raw[0][0] if raw else 0.0)
        events = []
        for stamp, method, params, session in raw:
            if method.startswith(("reply ", "error ")):
                kept = dict(params)
            elif method.startswith("send "):
                kept = {k: params[k] for k in ("url", "frameId") if k in params}
            else:
                kept = _reduced(method, params)
            if session:
                kept["session"] = session
            events.append([_ms(stamp - base), method, kept])

        def navigate_id(path):
            """The id of the navigation the engine answered to the `Page.navigate` of `path`."""
            sent = None
            for _, method, params, _ in raw:
                if method == "send Page.navigate":
                    sent = params.get("url", "")
                elif method == "reply Page.navigate" and sent is not None and urlsplit(sent).path == path:
                    return params.get("navigationId")
            return None

        def request_of(navigation_id):
            """The request of the document of a navigation as the PARENT reported it: the `requestWillBeSent` that carries the navigation id,
            and how that request ended (finished, failed with its code, or never ended in the trace)."""
            first = None
            if navigation_id is not None:
                first = next(((stamp, params) for stamp, method, params, _ in raw
                              if method == "Network.requestWillBeSent" and params.get("navigationId") == navigation_id), None)
            if first is None:
                return {"seen": False}
            sent, params = first
            found = {"seen": True, "request_id": params.get("requestId"), "url": params.get("url"), "sent_ms": _ms(sent - base),
                     "status": None, "ended": "not ended", "ended_ms": None}
            for stamp, method, later, _ in raw:
                if stamp < sent or later.get("requestId") != found["request_id"]:
                    continue
                if method == "Network.responseReceived" and found["status"] is None:
                    found["status"] = later.get("status")
                elif method == "Network.requestFinished":
                    found["ended"], found["ended_ms"] = "finished", _ms(stamp - base)
                elif method == "Network.requestFailed":
                    found["ended"], found["ended_ms"] = "failed: %s" % later.get("errorCode"), _ms(stamp - base)
            return found

        g1, g2 = navigate_id("/partial"), navigate_id("/second")
        source = "the reply of Page.navigate"
        if g1 is None or g2 is None:
            # no reply to say it: the navigationStarted that follows each goto
            started = [(stamp, params.get("navigationId")) for stamp, method, params, _ in raw if method == "Page.navigationStarted"]
            after1 = [nid for stamp, nid in started if self.t0 is not None and stamp >= self.t0]
            after2 = [nid for stamp, nid in started if self.t_second is not None and stamp >= self.t_second]
            g1 = g1 or (after1[0] if after1 else None)
            g2 = g2 or (after2[0] if after2 else None)
            source = "the navigationStarted after each goto"
        if row.get("goto2_outcome") is None:
            esito = "no goto 2 (%s)" % ("scenario error" if row.get("response_heard") else "the response of the first was never heard")
        elif row["goto2_outcome"] == "commit":
            esito = "commit"
        else:
            esito = "hung" if row.get("goto2_error_class") == "timeout" else "error: %s" % row.get("goto2_error_class")
        return {"goto1_id": g1, "goto2_id": g2, "goto_ids_from": source, "esito": esito,
                "goto1_request": request_of(g1), "goto2_request": request_of(g2), "events": events}


def _token(event, goto1, goto2):
    """One event as a short word: its method, the goto its navigation id belongs to (G1, G2, other) and what tells it apart."""
    _, method, params = event
    nid = params.get("navigationId")
    who = None if not nid else "G1" if nid == goto1 else "G2" if nid == goto2 else "other"
    short = method.replace("Page.", "").replace("Network.", "net.").replace("Runtime.", "").replace("Browser.", "")
    path = urlsplit(params["url"]).path if params.get("url") else None
    detail = [d for d in (who, path, params.get("errorText") or params.get("errorCode"), params.get("status"), params.get("name")) if d is not None]
    return "%s(%s)" % (short, " ".join(str(d) for d in detail)) if detail else short


def trace_sequences(rows):
    """Per outcome (hung, committed, other), the sequences of the events after the `navigationStarted` of goto 2, most frequent first:
    {outcome: [(count, [[word, ms after that navigationStarted, repeats], ...])]}. The ms are those of the first repetition with that sequence."""
    groups = {}
    for r in rows:
        events, g1, g2 = r.get("events"), r.get("goto1_id"), r.get("goto2_id")
        if not events or g2 is None:
            continue
        kind = {"hung": "hung", "commit": "good (committed)"}.get(r.get("esito"), r.get("esito") or "?")
        start = next((i for i, e in enumerate(events) if e[1] == "Page.navigationStarted" and e[2].get("navigationId") == g2), None)
        if start is None:
            groups.setdefault(kind + ", no navigationStarted for goto 2", {}).setdefault((), [0, []])[0] += 1
            continue
        t_start = events[start][0]
        folded = []
        for e in events[start + 1:]:
            if e[1] in ("send Page.navigate", "reply Page.navigate"):
                continue                           # said once per goto: not what the browser does after
            word = _token(e, g1, g2)
            if folded and folded[-1][0] == word:   # consecutive repeats collapse: "x3"
                folded[-1][2] += 1
            else:
                folded.append([word, round(e[0] - t_start, 1), 1])
        slot = groups.setdefault(kind, {}).setdefault(tuple(w for w, _, _ in folded), [0, folded])
        slot[0] += 1
    return {kind: sorted(((n, f) for n, f in seqs.values()), key=lambda item: -item[0]) for kind, seqs in groups.items()}


def render_trace(sequences, top=3):
    out = []
    for kind, seqs in sorted(sequences.items()):
        out.append("== trace, repetitions %s: %d" % (kind, sum(n for n, _ in seqs)))
        for n, folded in seqs[:top]:
            out.append("   x%d, after the navigationStarted of goto 2 (G1 = goto 1, G2 = goto 2):" % n)
            if not folded:
                out.append("        (nothing)")
            for word, at, count in folded:
                out.append("        +%8.1f ms  %s%s" % (at, word, " x%d" % count if count > 1 else ""))
    return "\n".join(out)


# ---------------------------------------------------------------- the hangs

def hangs_of(rows):
    """One record per repetition whose goto 2 hung (it ended by its own timeout): was the request of goto 2 in the parent (a
    `Network.requestWillBeSent` with the navigation id of goto 2), how that request ended, and whether the server saw it."""
    found = []
    for r in rows:
        if r.get("esito") != "hung":
            continue
        request = r.get("goto2_request") or {}
        found.append({
            "repetition": r.get("repetition"),
            "goto2_request_in_parent": bool(request.get("seen")),
            "request_sent_ms": request.get("sent_ms"),
            "request_status": request.get("status"),
            "request_ended": request.get("ended") if request.get("seen") else None,
            "request_ended_ms": request.get("ended_ms"),
            "request_reached_the_server": r.get("second_request_arrived"),
            "goto2_ms": r.get("goto2_ms"),
            "goto2_error": _first_line(r.get("goto2_error")),
            "goto1_error_class": r.get("goto1_error_class"),
            "goto2_id": r.get("goto2_id"),
        })
    return found


def render_hangs(name, system, rows, hangs):
    traced = sum(1 for r in rows if r.get("esito") is not None)
    out = ["== HANGS of %s on %s: %d of %d traced repetitions (expected 0)" % (name, system, len(hangs), traced)]
    for h in hangs:
        if h["goto2_request_in_parent"]:
            request = "yes, sent +%s ms after the start of goto 1, ended: %s%s" % (
                h["request_sent_ms"], h["request_ended"], "" if h["request_ended_ms"] is None else " at +%s ms" % h["request_ended_ms"])
        else:
            request = "NO (no requestWillBeSent with the navigation id of goto 2)"
        out.append("   repetition %s: request of goto 2 in the parent: %s; reached the server: %s; goto 2: %s after %s ms"
                   % (h["repetition"], request, "yes" if h["request_reached_the_server"] else "no", h["goto2_error"], h["goto2_ms"]))
    out.append("HUNTTOTAL browser=%s system=%s repetitions=%d traced=%d hung=%d" % (name, system, len(rows), traced, len(hangs)))
    return "\n".join(out)


# ---------------------------------------------------------------- the summary

def _first_line(message):
    return (message or "").strip().splitlines()[0][:160] if message else None


def summarise(rows):
    measured = [r for r in rows if "second_request_arrived" in r]
    gotos2 = [r for r in measured if r.get("goto2_outcome")]
    ms2 = [r["goto2_ms"] for r in gotos2]
    asked = [r["second_request_after_goto2_start_ms"] for r in measured if r.get("second_request_after_goto2_start_ms") is not None]

    def median_max(values):
        return {"median": round(statistics.median(values), 1), "max": round(max(values), 1)} if values else {"median": None, "max": None}

    return {
        "repetitions": len(rows),
        "launched": sum(1 for r in rows if r.get("launched")),
        "measured": len(measured),
        "never_heard_the_response_of_the_first": sum(1 for r in measured if not r["response_heard"]),
        "second_request_arrived": {"yes": sum(1 for r in measured if r["second_request_arrived"]),
                                   "no": sum(1 for r in measured if not r["second_request_arrived"])},
        "second_request_arrived_after_goto2_ended": sum(1 for r in measured if r.get("second_request_before_goto2_end") is False),
        "goto2": {"commit": sum(1 for r in gotos2 if r["goto2_outcome"] == "commit"),
                  "error": sum(1 for r in gotos2 if r["goto2_outcome"] == "error")},
        "goto2_errors": dict(Counter(_first_line(r["goto2_error"]) for r in gotos2 if r["goto2_error"])),
        "goto1_end": dict(Counter(r["goto1_error_class"] for r in measured if "goto1_error_class" in r)),
        "goto1_errors": dict(Counter(_first_line(r["goto1_error"]) for r in measured if r.get("goto1_error"))),
        "goto2_ms": median_max(ms2),
        "second_request_after_goto2_start_ms": median_max(asked),
        "launch_errors": dict(Counter(_first_line(r["launch_error"]) for r in rows if r.get("launch_error"))),
        "scenario_errors": dict(Counter(_first_line(r["scenario_error"]) for r in rows if r.get("scenario_error"))),
    }


def render(name, summary):
    out = ["== %s: %d repetitions, %d launched, %d measured" % (name, summary["repetitions"], summary["launched"], summary["measured"])]
    s = summary["second_request_arrived"]
    out.append("   /second reached the server: yes %d, no %d (of the yes, %d after goto 2 had ended)"
               % (s["yes"], s["no"], summary["second_request_arrived_after_goto2_ended"]))
    g = summary["goto2"]
    out.append("   goto 2: committed %d, error %d" % (g["commit"], g["error"]))
    out.append("   goto 2 ms: median %(median)s, max %(max)s" % summary["goto2_ms"])
    out.append("   /second request, ms after goto 2 started: median %(median)s, max %(max)s" % summary["second_request_after_goto2_start_ms"])
    out.append("   goto 1 ended as: %s" % (", ".join("%s x%d" % kv for kv in sorted(summary["goto1_end"].items())) or "-"))
    for label, key in (("goto 2 errors", "goto2_errors"), ("goto 1 errors", "goto1_errors"), ("launch errors", "launch_errors"),
                       ("scenario errors", "scenario_errors")):
        for message, count in summary[key].items():
            out.append("   %s: x%d  %s" % (label, count, message))
    if summary["never_heard_the_response_of_the_first"]:
        out.append("   the client never heard the response of the first goto in %d repetition(s)"
                   % summary["never_heard_the_response_of_the_first"])
    return "\n".join(out)


# ---------------------------------------------------------------- the run

def _versions():
    found = {}
    for module, label in (("playwright", "playwright"), ("invisible_playwright", "invisible_playwright"), ("invisible_core", "invisible_core")):
        try:
            found[label] = __import__(module).__version__
        except Exception:
            found[label] = None
    try:
        from importlib.metadata import version
        found["playwright"] = version("playwright")
    except Exception:
        pass
    return found


def _declare_the_lab(directory):
    """Where an e2e launch leaves from is declared, as the e2e suite does (tests/_lab_egress.py): no service of the internet is asked."""
    sys.path.insert(0, str(ROOT / "tests"))
    import pytest

    import _lab_egress

    patch = pytest.MonkeyPatch()
    _lab_egress.install(patch, Path(directory))
    return patch


async def _run(makers, repetitions, binary, out_dir, trace_on=False, budget_min=0.0):
    rows = {m.name: [] for m in makers}
    started = time.monotonic()
    stopped_at = None
    for index in range(1, repetitions + 1):
        if budget_min and time.monotonic() - started > budget_min * 60:
            stopped_at = index
            print("budget of %.0f min spent: no repetition from %d on (%d done)" % (budget_min, index, index - 1), flush=True)
            break
        for maker in makers:                       # alternate: ours, stock, ours, stock, ...
            row = await _repetition(maker, index, binary, trace_on)
            rows[maker.name].append(row)
            shown = ["launched", "second_request_arrived", "second_request_after_goto2_start_ms", "goto1_error_class",
                     "goto2_outcome", "goto2_ms", "launch_error", "scenario_error"]
            if trace_on:
                shown += ["esito", "goto1_id", "goto2_id", "trace_error"]
            print("[%s %d/%d] %s" % (maker.name, index, repetitions, json.dumps({k: row.get(k) for k in shown}, default=str)), flush=True)
    system = {"win32": "windows", "linux": "linux", "darwin": "macos"}.get(sys.platform, sys.platform)
    result = {}
    for maker in makers:
        summary = summarise(rows[maker.name])
        payload = {
            "meta": {"browser": maker.name, "system": system, "platform": platform.platform(), "python": sys.version.split()[0],
                     "versions": _versions(), "binary": binary if maker.name == "invisible" else "the Firefox of `playwright install firefox`",
                     "prefs": FISSION_ON, "hosts": "a.test, b.test, c.test -> the local server, by network.dns.localDomains",
                     "goto1_timeout_ms": GOTO1_TIMEOUT_MS, "goto2_timeout_ms": GOTO2_TIMEOUT_MS,
                     "trace": trace_on, "repetitions_asked": repetitions, "stopped_before_repetition": stopped_at, "budget_min": budget_min,
                     "fresh_browser_per_repetition": True, "browsers_alternate": len(makers) > 1,
                     "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            "summary": summary,
            "rows": rows[maker.name],
        }
        if trace_on:
            payload["trace_sequences"] = {
                kind: [{"repetitions": n, "events": [{"word": w, "ms_after_goto2_started": at, "times": c} for w, at, c in folded]}
                       for n, folded in seqs]
                for kind, seqs in trace_sequences(rows[maker.name]).items()}
            payload["hangs"] = hangs_of(rows[maker.name])
        path = Path(out_dir) / ("second-goto-%s%s-%s.json" % ("trace-" if trace_on else "", maker.name, system))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        print("written", path, flush=True)
        result[maker.name] = summary
    print()
    for maker in makers:
        print(render(maker.name, result[maker.name]), flush=True)
        if trace_on:
            print(render_trace(trace_sequences(rows[maker.name])), flush=True)
            print(render_hangs(maker.name, system, rows[maker.name], hangs_of(rows[maker.name])), flush=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--browser", choices=("both", "invisible", "stock"), default="both",
                        help="which browser: both alternate, repetition by repetition (default)")
    parser.add_argument("--binary", default=os.environ.get("INVPW_BINARY_PATH") or os.environ.get("FF"),
                        help="the engine of invisible_playwright (default: $INVPW_BINARY_PATH, then $FF)")
    parser.add_argument("--repetitions", type=int, default=REPETITIONS, help="per browser (default %d)" % REPETITIONS)
    parser.add_argument("--out-dir", default="second-goto", help="where the JSON of each browser is written")
    parser.add_argument("--budget-min", type=float, default=0.0,
                        help="start no new repetition after this many minutes (default 0: no limit)")
    parser.add_argument("--trace", action="store_true",
                        help="record, in every repetition, the protocol events the client hears with their time (needs --browser invisible)")
    args = parser.parse_args(argv)
    if args.trace and args.browser != "invisible":
        parser.error("--trace records the protocol of OUR browser: say --browser invisible")

    makers = {"both": [_Ours, _Stock], "invisible": [_Ours], "stock": [_Stock]}[args.browser]
    if _Ours in makers and not args.binary:
        parser.error("the engine of invisible_playwright is needed: --binary, or $INVPW_BINARY_PATH / $FF")
    with tempfile.TemporaryDirectory(prefix="second-goto-") as lab:
        patch = _declare_the_lab(lab) if _Ours in makers else None
        try:
            result = asyncio.run(_run(makers, args.repetitions, args.binary, args.out_dir, args.trace, args.budget_min))
        finally:
            if patch is not None:
                patch.undo()
    # the exit code says the script could measure, not what it measured
    dead = [name for name, s in result.items() if s["measured"] == 0]
    if dead:
        print("::error::no repetition of %s could be measured" % ", ".join(dead), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
