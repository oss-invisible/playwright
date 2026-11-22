"""Profile P2 of ci-release-18: ONE context and ONE page, reused for every address, driven by the stock Playwright client.

The shape of a production client (tf-fetch): a stock Playwright client connected to our served browser, a context route
(plain synchronous function: abort image and media, continue the rest), `goto(wait_until="commit")` and then the client-side
wait for `networkidle`. After every difficult page the `quiet` canary is loaded on the same page: a state that survives a
navigation makes the canary fail. The same driver runs against the stock Firefox, launched by the same client, so the
verdict is "same outcome as stock".
"""
from __future__ import annotations

import json
import re
import time

BLOCKED = ("image", "media")


def install_route(context):
    context.route("**/*", lambda route: route.abort() if route.request.resource_type in BLOCKED else route.continue_())


def navigate(page, url, *, idle_timeout_ms, client_log=None, lifecycle_probe=None):
    """`lifecycle_probe`, a function (default None: nothing), is called the moment the wait for networkidle expires and its answer is kept in `lifecycle_open`
    of the cell (`lifecycle_open_requests`). `client_log`, a list (default None: nothing kept), gets one entry per event the client heard (wall time `t`, time.time(), and `m`, time.perf_counter()):
    the navigation's start and end, and every request, its response and its end. A test that saves evidence when it fails passes it."""
    rec = {"idle": False, "err": None, "seconds": None}
    # The requests the client has heard of and not yet heard the end of: a navigation that does not idle says who is still in flight
    # (type, method, url, frame). The process that hosts a request is not readable by a client.
    open_requests = {}

    def note(event, request=None, **more):
        if client_log is None:
            return
        entry = {"ev": event, "t": round(time.time(), 4), "m": round(time.perf_counter(), 6), "url": url}
        if request is not None:
            entry.update(req=id(request), type=request.resource_type, method=request.method, target=request.url[:160])
        entry.update(more)
        client_log.append(entry)

    def started(request):
        note("request", request)
        try:
            frame = request.frame.url[:100]
        except Exception:
            frame = None
        open_requests[id(request)] = {"type": request.resource_type, "method": request.method, "url": request.url[:160],
                                      "frame": frame, "t": round(time.monotonic() - t0, 2)}

    def ended(request):
        open_requests.pop(id(request), None)
        note("ended", request)

    def answered(response):
        note("response", response.request, status=response.status)

    t0 = time.monotonic()
    page.on("request", started)
    if client_log is not None:
        page.on("response", answered)
    page.on("requestfinished", ended)
    page.on("requestfailed", ended)
    note("navigation_start")
    try:
        page.goto(url, wait_until="commit", timeout=15000)
        page.wait_for_load_state("networkidle", timeout=idle_timeout_ms)
        rec["idle"] = True
    except Exception as exc:        # recorded, the run goes on: the verdict compares outcomes
        text = str(exc).strip()
        rec["err"] = (text.splitlines()[0] if text else repr(exc))[:200]
        # First thing at the expiry, before anything else is read: what the served browser's own lifecycle holds as open is what keeps networkidle away.
        if lifecycle_probe is not None:
            rec["expired_at"] = {"t": round(time.time(), 4), "m": round(time.perf_counter(), 6)}
            try:
                rec["lifecycle_open"] = lifecycle_probe()
            except Exception as problem:        # the evidence never changes the outcome it is about
                rec["lifecycle_open"] = [{"error": repr(problem)}]
        # The WHOLE list, not a head of it: what is left open is the question.
        rec["in_flight"] = list(open_requests.values())
    finally:
        for event, handler in (("request", started), ("response", answered), ("requestfinished", ended), ("requestfailed", ended)):
            try:
                page.remove_listener(event, handler)
            except Exception:
                pass
    rec["seconds"] = round(time.monotonic() - t0, 2)
    note("navigation_end", idle=rec["idle"], err=rec["err"])
    return rec


def lifecycle_open_requests(server):
    """What the in-process server of a served browser (`launch_server()`) holds as OPEN, per page: the lifecycle's own map of the requests it waits the end of
    (`Lifecycle._open`: request id -> (owner frame, navigation)), each with what the lifecycle knows of its owner frame, and the frame tree. A client cannot
    read it: the test reaches the lifecycle through the registry of the server (`RemoteServer._endpoint.server._objects`, whose `Page` objects are the
    `PageDispatcher`s, each with its `lifecycle`). Read-only; the lifecycle's lock is taken for at most two seconds, and the dump says when it was not."""
    pages = []
    for obj in list(server._endpoint.server._objects.values()):
        if getattr(obj, "TYPE", None) != "Page":
            continue
        try:
            lifecycle = obj.lifecycle
        except Exception:
            continue
        locked = lifecycle._cv.acquire(timeout=2)
        try:
            frames = dict(lifecycle.frames)
            held = list(lifecycle._open.items())
            main = lifecycle.main_frame
            tree = lifecycle.frame_tree()
        finally:
            if locked:
                lifecycle._cv.release()
        opened = []
        for rid, (owner, navigation) in held:
            frame = frames.get(owner or main)
            opened.append({"rid": rid, "owner": owner, "navigation": navigation, "owner_is_known": (owner in frames) if owner else None,
                           "owner_url": frame.url if frame else None, "owner_parent": frame.parent if frame else None,
                           "owner_navigation": frame.navigation if frame else None, "owner_states": sorted(frame.states) if frame else None})
        pages.append({"page": obj.guid, "main_frame": main, "lock_taken": locked, "open": opened, "frame_tree": tree})
    return pages


# ---- the history of the requests still open, read from the Juggler wire (tests/_evidence_on_failure.py writes it) -----------------------------------
#: The commands that answer an intercepted request, and the events of a frame that matter around a request of it.
INTERCEPT_COMMANDS = ("Network.resumeInterceptedRequest", "Network.abortInterceptedRequest", "Network.fulfillInterceptedRequest")
FRAME_EVENTS = ("Page.frameAttached", "Page.frameDetached", "Page.navigationStarted", "Page.navigationCommitted", "Page.navigationAborted",
                "Page.sameDocumentNavigation")
#: Seconds of a frame's events kept before a request and after it.
FRAME_WINDOW = (1.0, 20.0)
_FIELDS = ("requestId", "frameId", "url", "navigationId", "errorCode")


def _string_field(text, name):
    found = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % name, text)
    return found.group(1) if found else None


def _wire_events(lines):
    """The records of the wire log (one JSON per line) as events: time, direction and the message; a message the log cut is read from the fields it still shows."""
    events = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        text = record.get("msg") or ""
        try:
            msg = json.loads(text)
        except ValueError:
            msg = None
        if not isinstance(msg, dict):
            number = re.search(r'"id"\s*:\s*(\d+)', text)
            msg = {"method": _string_field(text, "method"), "id": int(number.group(1)) if number else None,
                   "params": {name: _string_field(text, name) for name in _FIELDS}, "_cut": True}
        events.append({"t": record.get("t"), "m": record.get("m"), "dir": record.get("dir") or "", "msg": msg})
    return events


def _brief(msg, *names):
    params = msg.get("params") or {}
    return " ".join("%s=%s" % (n, params.get(n)) for n in names if params.get(n) is not None)


def open_requests_history(wire_lines, cells):
    """The text of `p2-open-requests-history.txt`: for each request id the lifecycle held open when a wait for networkidle expired (the `lifecycle_open` of
    the cells), in time order, what the Juggler wire says of it: `requestWillBeSent` (frame, interception, address, navigation), each command that answered
    the interception and its reply, `responseReceived`, `requestFinished` or `requestFailed` if any, and, around it, the `Page` events of its frame
    (frameAttached/Detached, navigationStarted/Committed/Aborted): the history that says who ended it and who did not."""
    events = _wire_events(wire_lines)
    wanted = {}
    for cell in cells or []:
        for page in cell.get("lifecycle_open") or []:
            for entry in page.get("open") or []:
                wanted.setdefault(entry["rid"], []).append((cell, entry))
    out = ["The requests the served browser's lifecycle held open when the wait for networkidle expired, and what the Juggler wire says of each.",
           "t: wall clock (seconds since the epoch), m: perf_counter of the test process, +s: seconds from the request's requestWillBeSent. Around a request "
           "the events of its frame are kept from %.0f s before to %.0f s after. A message longer than the wire log's limit is read from the fields "
           "still readable (cut)." % FRAME_WINDOW, ""]
    if not wanted:
        out.append("No request was held open at an expiry (no cell was not idle, or the lifecycle could not be read).")
        return "\n".join(out) + "\n"
    by_request, replies = {}, {}
    for ev in events:
        msg = ev["msg"]
        method = msg.get("method")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        rid = params.get("requestId")
        if rid is not None and method and method.startswith("Network."):
            by_request.setdefault(rid, []).append(ev)
        elif not method and msg.get("id") is not None and ev["dir"] == "in":
            replies.setdefault(msg["id"], []).append(ev)

    def line(ev, base, text):
        t = ev["t"]
        after = (" (+%.3fs)" % (t - base)) if (t is not None and base is not None) else ""
        return "  t=%s m=%s %s%s  %s" % (t, ev["m"], ev["dir"].ljust(3), after, text)

    def first_expiry(rid):
        return min(((c.get("expired_at") or {}).get("t") or 0) for c, _e in wanted[rid])

    for rid in sorted(wanted, key=first_expiry):
        out.append("== request %s" % rid)
        for cell, entry in wanted[rid]:
            expired = cell.get("expired_at") or {}
            out.append("  open at the expiry of round %s %s%s (t=%s m=%s): owner frame %s (known to the lifecycle: %s, url %s, its navigation %s, states %s), "
                       "the request's navigation %s" % (cell.get("round"), cell.get("pattern"), " (canary)" if cell.get("canary") else "",
                                                         expired.get("t"), expired.get("m"), entry.get("owner"), entry.get("owner_is_known"),
                                                         entry.get("owner_url"), entry.get("owner_navigation"), entry.get("owner_states"),
                                                         entry.get("navigation")))
        mine = by_request.get(rid, [])
        sent = [ev for ev in mine if ev["msg"].get("method") == "Network.requestWillBeSent"]
        base = sent[0]["t"] if sent else None
        frame = None
        if not mine:
            out.append("  not on the wire: its requestWillBeSent came before the wire was logged, or from another connection")
        for ev in mine:
            msg = ev["msg"]
            method = msg["method"]
            if method == "Network.requestWillBeSent":
                frame = frame or (msg.get("params") or {}).get("frameId")
                out.append(line(ev, base, "requestWillBeSent %s%s" % (_brief(msg, "frameId", "isIntercepted", "navigationId", "redirectedFrom", "method", "url"),
                                                                       " (cut)" if msg.get("_cut") else "")))
            elif method in INTERCEPT_COMMANDS:
                out.append(line(ev, base, "%s sent, id=%s %s" % (method.split(".")[1], msg.get("id"), _brief(msg, "requestId"))))
                answers = replies.get(msg.get("id"), [])
                for reply in answers:
                    body = reply["msg"]
                    verdict = ("error " + json.dumps(body.get("error"), default=str)[:300]) if body.get("error") else "ok"
                    out.append(line(reply, base, "  reply to id=%s: %s" % (msg.get("id"), verdict)))
                if not answers:
                    out.append("      no reply to id=%s on the wire" % msg.get("id"))
            else:
                out.append(line(ev, base, "%s %s" % (method.split(".")[1], _brief(msg, "errorCode", "url"))))
        if mine and not [ev for ev in mine if ev["msg"].get("method") in ("Network.requestFinished", "Network.requestFailed")]:
            out.append("      no requestFinished or requestFailed for it on the wire")
        if frame is not None and base is not None:
            out.append("  events of its frame %s, %.0f s before to %.0f s after the request:" % ((frame,) + FRAME_WINDOW))
            shown = 0
            for ev in events:
                msg = ev["msg"]
                params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
                if msg.get("method") in FRAME_EVENTS and params.get("frameId") == frame and ev["t"] is not None \
                        and base - FRAME_WINDOW[0] <= ev["t"] <= base + FRAME_WINDOW[1]:
                    shown += 1
                    out.append(line(ev, base, "%s %s" % (msg["method"].split(".")[1], _brief(msg, "navigationId", "url", "parentFrameId", "errorText"))))
            if not shown:
                out.append("      none")
        out.append("")
    return "\n".join(out) + "\n"


def drive(browser, origin, patterns, repetitions, idle_timeout_ms=10000, client_log=None, lifecycle_probe=None):
    """Every pattern `repetitions` times, each followed by the canary. Returns a list of cells."""
    cells = []
    context = browser.new_context()
    try:
        install_route(context)
        page = context.new_page()
        for rep in range(1, repetitions + 1):
            for name in patterns:
                for pattern, canary in ((name, False), ("quiet", True)):
                    cell = navigate(page, "%s/p/%s" % (origin, pattern), idle_timeout_ms=idle_timeout_ms, client_log=client_log,
                                    lifecycle_probe=lifecycle_probe)
                    cell.update(round=rep, pattern=pattern, canary=canary, after=name if canary else None)
                    cells.append(cell)
    finally:
        try:
            context.close()
        except Exception:
            pass
    return cells


def run_on_candidate(sync_playwright, binary, origin, patterns, repetitions, lifecycle_dump=False, **kw):
    """Our engine, served over the Playwright WebSocket and driven by the stock client. With `lifecycle_dump`, a cell that does not idle keeps what the
    server's lifecycle held open when its wait expired (`lifecycle_open_requests`)."""
    from invisible_playwright._juggler.remote import launch_server

    server = launch_server(headless=True, binary_path=binary, locale="en-US", timezone="Europe/Rome")
    if lifecycle_dump:
        kw["lifecycle_probe"] = lambda: lifecycle_open_requests(server)
    try:
        with sync_playwright() as p:
            browser = p.firefox.connect(server.ws_endpoint)
            try:
                return drive(browser, origin, patterns, repetitions, **kw)
            finally:
                browser.close()
    finally:
        server.close()


def run_on_stock(sync_playwright, origin, patterns, repetitions, **kw):
    with sync_playwright() as p:
        browser = p.firefox.launch()
        try:
            return drive(browser, origin, patterns, repetitions, **kw)
        finally:
            browser.close()


def verdict(candidate, stock, declared):
    """The cells where the candidate fails and stock idles, and the cells where stock is not what pages.json declares."""
    key = lambda c: (c["round"], c["pattern"], c["canary"], c["after"])      # noqa: E731
    stock_by = {key(c): c for c in stock}
    fails, broken = [], []
    for c in candidate:
        s = stock_by.get(key(c))
        if s is None:
            continue
        if s["idle"] and not c["idle"]:
            fails.append((key(c), c["err"]))
    for s in stock:
        if declared.get(s["pattern"]) == "idle" and not s["idle"]:
            broken.append((key(s), s["err"]))
    return fails, broken
