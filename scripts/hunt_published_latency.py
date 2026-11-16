"""HUNT ONLY, never merged: the published packages against stock Playwright, per browser: launch, evaluate round trip, goto -> the
request at the server, close, and CPU. Runs with the python of a venv that has the published invisible-playwright installed (not this
checkout).

    python hunt_published_latency.py ARM N_BROWSERS ROUND [binary]

  inproc     our wrapper in-process, N browsers in this Python process
  remote     launch_server, one Python process per browser, stock Playwright's client connects
  stock      Playwright's own Firefox and client, N browsers in this process
(the "one browser per process" arm is `inproc 1`, started four times at once by the workflow)

Each browser prints one HUNTROW JSON line: launch_ms, close_ms, eval_ms (100 round trips of evaluate("1+1")), goto_req_ms (10 gotos:
the client's goto call -> the request arriving at the local server), cpu_s (CPU seconds of the browser's processes during the work).
The launch is split in launch() / new_context / new_page / first load, each with its time and the CPU of the Firefox parent
processes during it (enter_ms, ctx_ms, new_page_ms, first_load_ms and *_cpu_s). For one browser per process of the inproc arms,
every evaluate is stamped along its whole path (HUNTEVAL), see install_evalsplit.

    python hunt_published_latency.py retail N ROUND FIREFOX_BINARY [close_after_s]

  retail     the real Firefox 159 (not Playwright), headed, N together, a fresh profile each, started with a page of the local server
             that sends a beacon on load; closed normally close_after_s after its first load. HUNTROW: start_to_load_ms,
             start_to_window_ms, close_ms (close -> every process of its tree gone), session_s, parent CPU; HUNTSHUT: the lines of
             the shutdown log with their time against the close.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import psutil

ARRIVED = {}
#: tag -> wall ms at which the retail page's onload beacon reached the server.
BEACONS = {}
EVALS = 100
GOTOS = 10
SEED = 42
TEXT = "someone@example.com"
#: Browser i of a process types TEXTS[i]: same length and shape, a different first letter, so the keys the client sent name the
#: browser they went to (exact pairing by identity, no estimate).
TEXTS = ["someone@example.com", "tomeone@example.com", "pomeone@example.com", "lomeone@example.com"]
#: The page stamps every keydown and keyup with two clocks, the event's birth (performance.timeOrigin + event.timeStamp) and the
#: handler (performance.timeOrigin + performance.now()); every mousemove and click with the handler's.
TYPE_PAGE = (b"<!doctype html><html><body style='margin:0'><div style='height:300px'></div><input id=f style='margin-left:300px'>"
             b"<script>window.__k=[];window.__u=[];window.__m=[];window.__c=[];window.__ev=[];const T=()=>performance.timeOrigin+performance.now();"
             b"document.addEventListener('keydown',e=>__k.push([e.key,performance.timeOrigin+e.timeStamp,T()]),true);document.addEventListener('keyup',e=>__u.push([e.key,performance.timeOrigin+e.timeStamp,T()]),true);"
             b"document.addEventListener('mousemove',e=>__m.push(T()),true);document.addEventListener('click',e=>__c.push(T()),true);"
             b"for(const t of ['mousemove','mousedown','mouseup','click'])document.addEventListener(t,e=>__ev.push([t,performance.timeOrigin+e.timeStamp,T()]),true);</script></body></html>")


def typing_plan(text=TEXT):
    """The core's plan for the session's first field: (dwell, gap) per character, for the persona of SEED and typing nonce 1."""
    from invisible_core.juggler._behaviour import TypingPersona, plan_typing
    return plan_typing(text, TypingPersona.from_seed(SEED), nonce=1)


def click_plan_ms():
    """The core's planned dwell (ms the button stays down) of the session's first click of the first page: `plan_click(persona, 1,
    nonce)` with the persona of SEED and the nonce `PageActs.next("click")` gives for page 0, act 1, which is `act_nonce(0, 1) = 1`
    (the first page the script asks for is number 0, and the count of a kind starts at 1). EXACT as far as the page number and the
    count are what that says: the first click of the page, no click before it. None where the core has no `plan_click`."""
    try:
        from invisible_core.juggler._behaviour import PointerPersona, plan_click
        return plan_click(PointerPersona.from_seed(SEED), 1, nonce=1)[0][0]
    except Exception:   # noqa: BLE001
        return None


def press_settle_plan_ms():
    try:
        from invisible_core.juggler._behaviour import PointerPersona, plan_press_settle
        return plan_press_settle(PointerPersona.from_seed(SEED), nonce=1)
    except Exception:   # noqa: BLE001
        return None


def mouse_report(ev, dwell_plan, settle_plan=None):
    """The mouse the page saw, on event.timeStamp (unprefixed) and on the handler's clock (prefix h_). The PATH before the press: the
    gaps between consecutive mousemoves up to the first mousedown, their longest, how many are over 100 ms, the compressions (a gap
    under half the median gap right after a gap over 100 ms: the catch-up after a stall). The path's own plan (the Pacer's instants)
    comes from the wrapper's cursor and the geometry of the page, and is not reproduced here: the shape is judged by its gaps. The CLICK:
    mousedown -> mouseup against the planned dwell. The engine births a mouse event `latency` ms before its handler (2-30 ms, drawn per
    event on the page's count of acts, not reproduced here), so on timeStamp the dwell differs from the plan by the difference of the
    two draws (at most 28 ms, typically a few): read the slip against that."""
    out = {"click_dwell_plan": round(dwell_plan, 1) if dwell_plan is not None else None,
           "press_settle_plan": round(settle_plan, 1) if settle_plan is not None else None}
    for prefix, i in (("", 1), ("h_", 2)):
        downs = sorted(e[i] for e in ev if e[0] == "mousedown")
        ups = sorted(e[i] for e in ev if e[0] == "mouseup")
        first_down = downs[0] if downs else None
        mv = sorted(e[i] for e in ev if e[0] == "mousemove" and (first_down is None or e[i] <= first_down))
        gaps = [b - a for a, b in zip(mv, mv[1:])]
        med = sorted(gaps)[len(gaps) // 2] if gaps else None
        out[prefix + "mv_n"] = len(mv)
        out[prefix + "mv_gap_med"] = round(med, 1) if med is not None else None
        out[prefix + "mv_gap_max"] = round(max(gaps), 1) if gaps else None
        out[prefix + "mv_gaps_over_100ms"] = sum(1 for g in gaps if g > 100)
        out[prefix + "mv_compressions"] = sum(1 for k in range(1, len(gaps)) if gaps[k - 1] > 100 and med is not None and gaps[k] < 0.5 * med)
        if downs and ups and ups[0] >= downs[0]:
            dwell = ups[0] - downs[0]
            out[prefix + "click_dwell"] = round(dwell, 1)
            if dwell_plan is not None:
                out[prefix + "click_dwell_slip"] = round(dwell - dwell_plan, 1)
        if mv and first_down is not None:
            out[prefix + "press_settle"] = round(first_down - mv[-1], 1)
    out["mouse_events"] = {k: sum(1 for e in ev if e[0] == k) for k in ("mousemove", "mousedown", "mouseup", "click")}
    return out


def _against_plan(times, planned, prefix):
    """Slip of each instant against the plan, both anchored at their first: p50 and max of |slip|, keys over 100 ms, compressions (a
    gap under half the planned gap, the catch-up after a stall)."""
    page = [x - times[0] for x in times]
    slip = [p - q for p, q in zip(page, planned)]
    rel = sorted(abs(s) for s in slip)
    gaps_p = [page[i + 1] - page[i] for i in range(len(page) - 1)]
    gaps_q = [planned[i + 1] - planned[i] for i in range(len(planned) - 1)]
    return {prefix + "slip_abs_p50": round(rel[len(rel) // 2], 1), prefix + "slip_abs_max": round(rel[-1], 1),
            prefix + "keys_over_100ms": sum(1 for s in rel if s > 100), prefix + "slip_last": round(slip[-1], 1),
            prefix + "compressions": sum(1 for a, b in zip(gaps_p, gaps_q) if b >= 40 and a < 0.5 * b)}


def typing_report(arm, keys, ups, moves, clicks, plan, sends=None):
    """What the page saw against the plan, on TWO clocks of each keydown: event.timeStamp (the event's birth: criterion 1b is judged
    on it, unprefixed keys) and performance.now() in the handler (prefix h_). With the client's sends of the keydowns (wall ms
    before the command went out, after its answer came back), per key: planned, sent, answered, timeStamp, handler, all in ms from
    the first send; and the client's own slip against the plan (prefix c_). The mouse: longest gap between two mousemoves and the
    bursts (gaps under 2 ms)."""
    downs = [(ts, h) for k, ts, h in keys if k != "Shift"]
    planned, t = [], 0.0
    for dwell, gap in plan:
        planned.append(t)
        t += dwell + gap
    out = {"keys_seen": len(downs), "keys_planned": len(plan)}
    if downs and len(downs) == len(plan):
        ts, hd = [d[0] for d in downs], [d[1] for d in downs]
        out.update(_against_plan(ts, planned, ""))
        out.update(_against_plan(hd, planned, "h_"))
        out["handler_minus_ts"] = [round(h - s, 1) for s, h in downs]
        if sends and len(sends) == len(plan):
            s0 = sends[0][0]
            out.update(_against_plan([s for s, _r in sends], planned, "c_"))
            out["detail"] = [[round(p, 1), round(s - s0, 1), round(r - s0, 1), round(a - s0, 1), round(b - s0, 1)]
                             for p, (s, r), (a, b) in zip(planned, sends, downs)]
    mg = [moves[i + 1] - moves[i] for i in range(len(moves) - 1)]
    out.update({"mousemoves": len(moves), "mouse_gap_max": round(max(mg), 1) if mg else None,
                "mouse_bursts_under_2ms": sum(1 for g in mg if g < 2), "clicks": len(clicks)})
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        # Through the hunt's proxy the request line carries the absolute URI.
        path = urlsplit(self.path).path
        if path.startswith("/beacon"):
            # The retail page's onload: the server's arrival time is the first-page-loaded instant.
            BEACONS.setdefault(parse_qs(urlsplit(self.path).query).get("b", [""])[0], time.time() * 1000)
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if path.startswith("/retail"):
            tag = re.sub(r"[^\w-]", "", parse_qs(urlsplit(self.path).query).get("b", [""])[0])
            body = ("<!doctype html><title>r</title><body>r<script>addEventListener('load',()=>{new Image().src='/beacon?b=" + tag
                    + "&t='+Date.now()})</script></body>").encode()
        elif path.startswith("/type"):
            body = TYPE_PAGE
        else:
            q = parse_qs(urlsplit(self.path).query)
            key = (q.get("b", [""])[0], q.get("i", [""])[0])
            ARRIVED.setdefault(key, time.time() * 1000)
            body = b"<!doctype html><title>p</title><body>p</body>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def engine_cpu():
    """CPU seconds of every firefox process under this one, by pid."""
    out = {}
    for p in psutil.Process().children(recursive=True):
        try:
            if "firefox" in p.name().lower() or "plugin-container" in p.name().lower():
                t = p.cpu_times()
                out[p.pid] = t.user + t.system
        except psutil.Error:
            pass
    return out


def parent_cpu():
    """CPU seconds (user + system) of the Firefox PARENT processes under this one (the sum of every ff:parent; the content,
    GPU and utility processes are left out)."""
    total = 0.0
    for p in psutil.Process().children(recursive=True):
        try:
            if "firefox" in p.name().lower() and proc_kind(p) == "ff:parent":
                t = p.cpu_times()
                total += t.user + t.system
        except psutil.Error:
            pass
    return total


#: Whether this process stamps the evaluates of its browser number 0 (the inproc arms; set by main).
TRACE_EVAL = False
SLOW = []          # (kind, tag, t0, t1) in time.monotonic(): every event over SLOW_MS
SLOW_MS = 1000
#: The late arm (HUNT_TYPE_LATE_S): typing waits until every browser of the process launched, plus this many seconds.
TYPE_LATE_S = float(os.environ.get("HUNT_TYPE_LATE_S") or 0)
LAUNCHED = {"n": 0, "of": 1, "t": None, "event": None}
#: The proxy arm (HUNT_PROXY=1): a local forwarding proxy (CONNECT and absolute-URI GET) in this process.
PROXY_PORT = 0
#: Every keydown and keyup our client sent through the core's Keyboard: (id of the keyboard, "down" or "up", key, wall ms before
#: the command went out, wall ms after its answer came back).
KEYSENDS = []


class ProxyHandler(socketserver.StreamRequestHandler):
    """HUNT: a forwarding proxy, so the proxy arm needs nothing outside the runner but the internet the egress echo already uses."""

    def handle(self):
        line = self.rfile.readline(65536)
        parts = line.split()
        if len(parts) < 2:
            return
        head = [line]
        while True:
            h = self.rfile.readline(65536)
            head.append(h)
            if h in (b"\r\n", b"\n", b""):
                break
        try:
            if parts[0] == b"CONNECT":
                host, port = parts[1].decode().rsplit(":", 1)
                up = socket.create_connection((host.strip("[]"), int(port)), 15)
                self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                self.wfile.flush()
            else:
                u = urlsplit(parts[1].decode())
                up = socket.create_connection((u.hostname, u.port or 80), 15)
                up.sendall(b"".join(head))
        except OSError:
            return
        up.settimeout(None)
        self.connection.settimeout(None)

        def pump(a, b):
            try:
                while True:
                    d = a.recv(65536)
                    if not d:
                        break
                    b.sendall(d)
            except OSError:
                pass
            finally:
                for x in (a, b):
                    try:
                        x.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
        t = threading.Thread(target=pump, args=(up, self.connection), daemon=True)
        t.start()
        pump(self.connection, up)
        t.join(30)
        up.close()


class ThreadingProxy(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def install_key_probe():
    """Wrap the core's Keyboard.down / up (the only senders of Page.dispatchKeyEvent in our client): the wall time before the command
    goes out and after its answer, per keyboard instance (one per page)."""
    try:
        # The stroke core (#281): a key event leaves through `_stroke.Stroke.send` at its planned instant; Keyboard.down / up only
        # plan it. Recorded there: the stroke id (one text = one stroke), the event's type and key, the wall time before the command
        # goes out (after the wait for its instant) and after `send_async` returned (the reply is collected at the end of the stroke).
        from invisible_core.juggler import _stroke
    except ImportError:
        _stroke = None
    if _stroke is not None:
        real_send = _stroke.Stroke.send
        if not getattr(real_send, "_hunt", False):
            def send(self, method, params, at_ms, *a, **k):
                if method != "Page.dispatchKeyEvent":
                    return real_send(self, method, params, at_ms, *a, **k)
                if not k.get("paced"):
                    self.hold_until(at_ms)        # the wait for the instant, so `before` is the instant the command leaves
                before = time.time() * 1000
                try:
                    return real_send(self, method, params, at_ms, *a, **k)
                finally:
                    KEYSENDS.append((self.id, "down" if params.get("type") == "keydown" else "up", params.get("key"), before,
                                     time.time() * 1000))
            send._hunt = True
            _stroke.Stroke.send = send
        return
    try:
        from invisible_core.juggler import keyboard as kb
    except Exception as exc:   # noqa: BLE001
        PROBE.append(("probe-error keyboard", "", 0, 0, repr(exc)[:200]))
        return
    for verb in ("down", "up"):
        real = getattr(kb.Keyboard, verb)
        if getattr(real, "_hunt", False):
            continue

        def call(self, key, *a, _real=real, _verb=verb, **k):
            t = time.time() * 1000
            try:
                return _real(self, key, *a, **k)
            finally:
                KEYSENDS.append((id(self), _verb, key, t, time.time() * 1000))
        call._hunt = True
        setattr(kb.Keyboard, verb, call)


def match_sends(rows):
    """Give each browser of our arms the keydowns its own keyboard sent, by identity: the characters a keyboard sent spell the text
    that only that browser typed."""
    groups = {}
    for kid, verb, key, t, r in KEYSENDS:
        if verb == "down" and key != "Shift":
            groups.setdefault(kid, []).append((key, t, r))
    by_text = {}
    for g in groups.values():
        by_text.setdefault("".join(k for k, _t, _r in g), []).append([(t, r) for _k, t, r in g])
    for r in rows:
        t = r.get("typing") if isinstance(r, dict) else None
        if not t or "raw" not in t or t["raw"][5] is not None:
            continue
        found = by_text.get(t["text"], [])
        t["paired"] = len(found)
        if len(found) == 1:
            t["raw"][5] = found[0]


SURVIVORS_SEEN = set()


def note_survivor(pid, cmd):
    """Print once the full command line of a process Firefox starts to outlive itself (the sender of the shutdown pings), so the real
    line of each OS is on record."""
    if pid not in SURVIVORS_SEEN:
        SURVIVORS_SEEN.add(pid)
        print("HUNTSURVIVOR " + json.dumps({"os": sys.platform, "pid": pid, "cmdline": cmd[:6]}), flush=True)


def proc_kind(p):
    try:
        name = p.name().lower()
        if "firefox" in name or "plugin-container" in name:
            cmd = p.cmdline()
            # The shutdown-ping sender is firefox(.exe) --backgroundtask pingsender (seen on Windows runners): until this it was
            # counted as a second "ff:parent".
            if any(a.lstrip("-/").lower() == "backgroundtask" for a in cmd[1:]):
                note_survivor(p.pid, cmd)
                return "ff:backgroundtask"
            return "ff:" + (cmd[-1] if "-contentproc" in cmd else "parent")
        return name
    except psutil.Error:
        return "?"


class StackSampler:
    """Every 25 ms the top frames of every Python thread of this process; every 200 ms the CPU of every process under it, by kind.
    An event over SLOW_MS is then explained from what was sampled in its window."""

    def __init__(self):
        self.stacks, self.cpu, self.stop = [], [], threading.Event()
        self.me = threading.get_ident()

    def start(self):
        threading.Thread(target=self._stacks, daemon=True, name="hunt-stacks").start()
        threading.Thread(target=self._cpu, daemon=True, name="hunt-cpu").start()

    def _stacks(self):
        import traceback
        while not self.stop.is_set():
            now = time.monotonic()
            names = {t.ident: t.name for t in threading.enumerate()}
            snap = {}
            for ident, frame in sys._current_frames().items():
                if ident == self.me or names.get(ident, "").startswith("hunt-"):
                    continue
                snap[names.get(ident, str(ident))] = tuple("%s:%d %s" % (f.filename.replace("\\", "/").rsplit("/", 2)[-1], f.lineno, f.name)
                                                          for f in traceback.extract_stack(frame)[-6:])
            self.stacks.append((now, snap))
            time.sleep(0.025)

    def _cpu(self):
        while not self.stop.is_set():
            now, row = time.monotonic(), {}
            try:
                procs = [psutil.Process()] + psutil.Process().children(recursive=True)
            except psutil.Error:
                procs = []
            for p in procs:
                try:
                    t = p.cpu_times()
                    row[p.pid] = (proc_kind(p), t.user + t.system)
                except psutil.Error:
                    pass
            # The pingsender outlives the browser and may leave our tree when its parent exits: look for it machine-wide.
            for p in psutil.process_iter(["name"]):
                try:
                    nm = (p.info["name"] or "").lower()
                    if p.pid in row or not ("pingsender" in nm or "firefox" in nm):
                        continue
                    kind = "pingsender" if "pingsender" in nm else proc_kind(p)
                    if kind in ("pingsender", "ff:backgroundtask"):
                        if kind == "pingsender":
                            note_survivor(p.pid, p.cmdline())
                        t = p.cpu_times()
                        row[p.pid] = (kind, t.user + t.system)
                except psutil.Error:
                    pass
            self.cpu.append((now, row))
            time.sleep(0.1)

    def explain(self, t0, t1):
        counts = {}
        n = 0
        for t, snap in self.stacks:
            if t0 <= t <= t1:
                n += 1
                for th, frames in snap.items():
                    key = th + " | " + " < ".join(reversed(frames[-3:]))
                    counts[key] = counts.get(key, 0) + 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:8]
        before = [r for t, r in self.cpu if t <= t0][-1:] or [{}]
        after = [r for t, r in self.cpu if t >= t1][:1] or [self.cpu[-1][1] if self.cpu else {}]
        b, a = before[0], after[0]
        by_kind = {}
        for pid, (kind, c) in a.items():
            d = c - (b.get(pid, (kind, 0.0))[1])
            if d > 0.01:
                by_kind[kind] = round(by_kind.get(kind, 0) + d, 2)
        # Who was alive during the window: per process kind, the first and last time it was seen (ms from the start of the event)
        # and how many processes of that kind.
        alive = {}
        for t, row in self.cpu:
            if t0 - 0.1 <= t <= t1 + 0.1:
                for pid, (kind, _c) in row.items():
                    a = alive.setdefault(kind, [None, None, set()])
                    ms = round((t - t0) * 1000)
                    a[0] = ms if a[0] is None else a[0]
                    a[1] = ms
                    a[2].add(pid)
        return {"samples": n, "top_stacks": [[k, v] for k, v in top], "cpu_s_by_kind": by_kind,
                "alive_ms": {k: [v[0], v[1], len(v[2])] for k, v in alive.items() if k != "python.exe" and k != "python"}}


SAMPLER = StackSampler()


# ---- the path of one evaluate, stamped (inproc arms, one browser per process: the one whose tag ends in -0) -----------------------
#: Set only around the evaluate loop of the traced browser; every wrapper below does nothing unless it is on AND the call carries a record.
EV_ON = {"on": False}
#: The record of the evaluate the current task is making (the contextvar travels with the task, so the transport's send sees it).
EV_CUR = contextvars.ContextVar("hunt_ev", default=None)
#: The server's worker thread (the one that runs `Server.handle`, and with it the core's pipe write) carries the record here.
EV_TLS = threading.local()
EV_BY_MSG = {}      # (id of the JugglerServer, the client's message id) -> EvalRec
EV_CORE = {}        # (id of the core's Connection, the engine command id) -> EvalRec
#: The engine's replies start with the id: Dispatcher.js sends JSON.stringify({id, sessionId, result}).
EV_ID_RE = re.compile(rb'^\{\s*"id"\s*:\s*(\d+)')
EVAL_OUT = []       # (tag, segments) of the traced browsers, printed by main as HUNTEVAL
EV_SEGMENTS = ("call_to_send", "send_to_pickup", "pickup_to_pipe", "pipe_to_reply_read", "read_to_ready", "ready_to_reply",
               "reply_to_loop", "dispatch", "resolve_to_return")


class EvalRec:
    """The instants (time.perf_counter, one clock for every thread) of one page.evaluate: the user's call, the client's transport.send
    (on the loop), the worker thread picking the message up (Server.handle), the engine commands written (after os.write) and the
    replies read by the reader thread (entry of _deliver), the end of that delivery (the waiter woken), the worker's reply(), the loop
    running the client's dispatch (entry and exit), the user's await returning."""
    __slots__ = ("t_call", "t_send", "t_handle", "writes", "reads", "t_deliver_end", "t_reply", "t_dispatch", "t_dispatch_end",
                 "t_return")

    def __init__(self):
        self.t_call = self.t_send = self.t_handle = self.t_deliver_end = self.t_reply = None
        self.t_dispatch = self.t_dispatch_end = self.t_return = None
        self.writes, self.reads = [], []


def eval_segments(recs):
    """Per consecutive segment, the ms of every evaluate whose stamps are all there. The engine's own time (receive -> reply) cannot
    be separated: the published engine's Juggler has no timing option (Dispatcher.js only dumps errors), so pipe_to_reply_read is the
    engine plus the two pipe hops (first command written -> last reply read)."""
    out = {name: [] for name in EV_SEGMENTS}
    out["total"], out["cmds"] = [], []
    missing = 0
    for r in recs:
        if None in (r.t_call, r.t_send, r.t_handle, r.t_deliver_end, r.t_reply, r.t_dispatch, r.t_dispatch_end, r.t_return) \
                or not r.writes or not r.reads:
            missing += 1
            continue
        pts = [r.t_call, r.t_send, r.t_handle, r.writes[0], r.reads[-1], r.t_deliver_end, r.t_reply, r.t_dispatch, r.t_dispatch_end,
               r.t_return]
        for name, a, b in zip(EV_SEGMENTS, pts, pts[1:]):
            out[name].append(round((b - a) * 1000, 3))
        out["total"].append(round((r.t_return - r.t_call) * 1000, 3))
        out["cmds"].append(len(r.writes))
    out["missing"] = missing
    return out


def install_evalsplit():
    """HUNT: stamp the path of one evaluate through the published code, without changing what it does. Wrapped, each by the name the
    published code calls it with:
      invisible_playwright._juggler.transport.InProcessTransport.send   the client hands the message to the server (queue.put)
      invisible_playwright._juggler.dispatcher.Server.handle            the worker thread `juggler-N` picks it up
      invisible_core.juggler.connection.Connection._write               the engine command written to the pipe (after os.write)
      invisible_core.juggler.connection.Connection._deliver             the reader thread has an engine reply (entry and exit)
      invisible_playwright._juggler.transport.InProcessTransport.reply / reply_error   the worker posts the reply to the loop
      invisible_playwright._pw._impl._connection.Connection.dispatch    the loop delivers the reply (entry and exit: future resolved)
    """
    import importlib
    pc = time.perf_counter

    def patch(owner, name, make):
        real = getattr(owner, name, None)
        if real is None or getattr(real, "_hunt", False):
            return
        wrapped = make(real)
        wrapped._hunt = True
        setattr(owner, name, wrapped)

    def make_send(real):
        def send(self, message):
            if EV_ON["on"]:
                rec = EV_CUR.get()
                if rec is not None:
                    if rec.t_send is None:
                        rec.t_send = pc()
                    EV_BY_MSG[(id(self._server), message.get("id"))] = rec
            return real(self, message)
        return send

    def make_handle(real):
        def handle(self, message):
            rec = EV_BY_MSG.get((id(self), message.get("id"))) if EV_BY_MSG else None
            if rec is None:
                return real(self, message)
            if rec.t_handle is None:
                rec.t_handle = pc()
            EV_TLS.rec = rec
            try:
                return real(self, message)
            finally:
                EV_TLS.rec = None
        return handle

    def make_write(real):
        def _write(self, msg):
            rec = getattr(EV_TLS, "rec", None)
            if rec is None:
                return real(self, msg)
            EV_CORE[(id(self), msg.get("id"))] = rec     # before the write: the reply may beat the line after it
            try:
                return real(self, msg)
            finally:
                rec.writes.append(pc())
        return _write

    def make_deliver(real):
        def _deliver(self, raw):
            if not EV_CORE:
                return real(self, raw)
            m = EV_ID_RE.match(raw[:64])
            rec = EV_CORE.pop((id(self), int(m.group(1))), None) if m else None
            if rec is None:
                return real(self, raw)
            t = pc()
            try:
                return real(self, raw)
            finally:
                rec.reads.append(t)
                rec.t_deliver_end = pc()
        return _deliver

    def make_reply(real):
        def reply(self, msg_id, *a, **k):
            rec = EV_BY_MSG.get((id(self._server), msg_id)) if EV_BY_MSG else None
            if rec is not None:
                rec.t_reply = pc()
            return real(self, msg_id, *a, **k)
        return reply

    def make_dispatch(real):
        def dispatch(self, msg):
            rec = None
            if EV_BY_MSG and msg.get("id"):
                rec = EV_BY_MSG.pop((id(getattr(getattr(self, "_transport", None), "_server", None)), msg.get("id")), None)
            if rec is None:
                return real(self, msg)
            rec.t_dispatch = pc()
            try:
                return real(self, msg)
            finally:
                rec.t_dispatch_end = pc()
        return dispatch

    for mod, cls, name, make in (
            ("invisible_playwright._juggler.transport", "InProcessTransport", "send", make_send),
            ("invisible_playwright._juggler.dispatcher", "Server", "handle", make_handle),
            ("invisible_core.juggler.connection", "Connection", "_write", make_write),
            ("invisible_core.juggler.connection", "Connection", "_deliver", make_deliver),
            ("invisible_playwright._juggler.transport", "InProcessTransport", "reply", make_reply),
            ("invisible_playwright._juggler.transport", "InProcessTransport", "reply_error", make_reply),
            ("invisible_playwright._pw._impl._connection", "Connection", "dispatch", make_dispatch)):
        try:
            patch(getattr(importlib.import_module(mod), cls), name, make)
        except Exception as exc:   # noqa: BLE001
            PROBE.append(("probe-error evalsplit %s.%s" % (cls, name), "", 0, 0, repr(exc)[:200]))


async def work(page, port, tag, trace=False):
    goto = []
    for i in range(GOTOS):
        t0, m0 = time.time() * 1000, time.monotonic()
        await page.goto("http://127.0.0.1:%d/p?b=%s&i=%d" % (port, tag, i), wait_until="load", timeout=30000)
        arrived = ARRIVED.get((tag, str(i)))
        goto.append(round(arrived - t0, 1) if arrived else None)
        if arrived and arrived - t0 > SLOW_MS:
            SLOW.append(("goto->request", tag, m0, m0 + (arrived - t0) / 1000))
    ev = []
    recs = []
    if trace:
        EV_ON["on"] = True
    try:
        for _ in range(EVALS):
            rec = None
            if trace:
                rec = EvalRec()
                EV_CUR.set(rec)
                rec.t_call = time.perf_counter()
            t0 = time.monotonic()
            await page.evaluate("1 + 1")
            ev.append(round((time.monotonic() - t0) * 1000, 2))
            if rec is not None:
                rec.t_return = time.perf_counter()
                EV_CUR.set(None)
                recs.append(rec)
            if ev[-1] > SLOW_MS:
                SLOW.append(("evaluate", tag, t0, time.monotonic()))
    finally:
        if trace:
            EV_ON["on"] = False
            EV_CUR.set(None)
            EVAL_OUT.append((tag, eval_segments(recs)))
    return goto, ev


async def typing(page, port, arm, text=TEXT):
    """A click into a field (with the cursor's path when humanised) and the fixed text: ours through press_sequentially, which
    follows the core's plan; stock through the same plan driven by hand (it has none of its own: only the runner's delay)."""
    plan = typing_plan(text)
    await page.goto("http://127.0.0.1:%d/type" % port, wait_until="load", timeout=30000)
    if TYPE_LATE_S:
        # The late arm: typing starts TYPE_LATE_S after the last browser of this process finished launching (browsers settled).
        try:
            await asyncio.wait_for(LAUNCHED["event"].wait(), 300)
            await asyncio.sleep(max(0.0, LAUNCHED["t"] + TYPE_LATE_S - time.monotonic()))
        except asyncio.TimeoutError:
            pass
    m0 = time.monotonic()
    cpu0 = psutil.cpu_times()
    sends = []
    if arm == "stock":
        await page.mouse.move(10, 10)
        await page.click("#f")
        for (dwell, gap), ch in zip(plan, text):
            if ch == "@":
                await page.keyboard.down("Shift")
            ts = time.time() * 1000
            await page.keyboard.down(ch)
            sends.append((ts, time.time() * 1000))
            await asyncio.sleep(dwell / 1000)
            await page.keyboard.up(ch)
            if ch == "@":
                await page.keyboard.up("Shift")
            await asyncio.sleep(gap / 1000)
    else:
        await page.locator("#f").click()
        await page.locator("#f").press_sequentially(text)
    took = time.monotonic() - m0
    cpu1 = psutil.cpu_times()
    tot = sum(cpu1) - sum(cpu0)
    keys, ups, moves, clicks, ev = await page.evaluate("[window.__k, window.__u, window.__m, window.__c, window.__ev]")
    # The report is made in main(), once the client's sends of our arms are matched to their browser.
    return {"text": text, "raw": [keys, ups, moves, clicks, plan, sends or None, ev],
            "click_plan": click_plan_ms() if arm != "stock" else None, "settle_plan": press_settle_plan_ms() if arm != "stock" else None,
            "typing_wall_ms": round(took * 1000),
            "machine_cpu_busy_pct": round(100 * (1 - (cpu1.idle - cpu0.idle) / tot), 1) if tot > 0 else None}


async def serve_endpoint(binary):
    proc = await asyncio.create_subprocess_exec(sys.executable, __file__, "serve", binary,
                                                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    while True:
        line = (await asyncio.wait_for(proc.stdout.readline(), 120)).decode().strip()
        if not line:
            err = (await proc.stderr.read()).decode(errors="replace")[-600:]
            raise RuntimeError("the server process ended before it served: " + err)
        if line.startswith("ENDPOINT "):
            return proc, line.split(" ", 1)[1]


def serve_one(binary):
    from invisible_playwright._juggler.remote import launch_server
    server = launch_server(headless=True, binary_path=binary, seed=42, humanize=True)
    print("ENDPOINT " + server.ws_endpoint, flush=True)
    try:
        sys.stdin.read()
    finally:
        server.close()


# ---- the shutdown arms (shut20 / shut70 / shut180): one browser per process, its output kept with the time of each line --------------
#: (wall ms, text) of every line the engine printed, recorded by the replacement of ProcessOutput._pump (HUNT_SHUT=1).
ENGINE_LINES = []
ENGINE_LINES_BY = {}
#: Pids of the processes the core spawned (Connection.__init__'s `process`), and the engine's tree at the close.
ENGINE_PIDS = []
TREE_PIDS = []
#: The probes that end an engine's processes: a background task that ends inside one of them was ended by the core.
BG_PROBES = ("EngineTree.end", "EngineTree.release", "process.terminate", "process.reap_session", "_profile.terminate")
#: Seconds after the start of the launch at which the browser is closed (HUNT_SESSION_S); 0 = right after the typing.
SESSION_S = float(os.environ.get("HUNT_SESSION_S") or 0)
SHUT_PREFS = {"toolkit.asyncshutdown.log": True, "browser.dom.window.dump.enabled": True}
#: HUNT_RS=1: the Remote Settings client's own log (ConsoleAPI prefix "services.settings", dumped to stdout as
#: "console.debug: services.settings: <message>" while the dump pref is on), from the birth of the browser.
RS_PREFS = {"services.settings.loglevel": "debug"}


def install_output_capture():
    """HUNT: the core's ProcessOutput._pump (invisible_core/juggler/connection.py, published 64.73.0) replaced by a copy of its body
    that also records each line with its wall time in ENGINE_LINES. Everything else it does is the same: the kept lines, the
    readiness marker, the notifications, the end of the stream."""
    from invisible_core.juggler import connection as cmod
    real = cmod.ProcessOutput._pump
    if getattr(real, "_hunt", False):
        return

    def _pump(self) -> None:
        mine = ENGINE_LINES_BY.setdefault(id(self), [])       # this engine's lines (id of its ProcessOutput = id(conn.output))
        try:
            for raw in iter(self._stream.readline, b""):
                text = raw.decode("utf-8", "replace").rstrip("\r\n")
                stamped = (time.time() * 1000, text)
                ENGINE_LINES.append(stamped)
                mine.append(stamped)
                with self._cv:
                    if text.strip():
                        self._lines.append(text)
                    if cmod._READY in text:
                        self._ready = True
                    self._cv.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            with self._cv:
                self._eof = True
                self._cv.notify_all()
    _pump._hunt = True
    cmod.ProcessOutput._pump = _pump
    # And the pid of the process the core spawned (the real parent of the engine): Connection.__init__ gets it as `process`.
    real_init = cmod.Connection.__init__

    def __init__(self, *a, **k):
        real_init(self, *a, **k)
        proc = k.get("process") if "process" in k else (a[2] if len(a) > 2 else None)
        pid = getattr(proc, "pid", None)
        if pid:
            ENGINE_PIDS.append(pid)
    cmod.Connection.__init__ = __init__


class ExitWatch(threading.Thread):
    """After the close starts: the wall time at which every direct child of this process (the engine's parent) is gone, and the time
    at which every process of its tree is. Polled every 10 ms on the Process objects taken before the close."""

    def __init__(self):
        super().__init__(daemon=True, name="hunt-exitwatch")
        me = psutil.Process()
        # The real parent: the process the core spawned (its pid from Connection.__init__), else the direct children of this process.
        self.main = []
        for pid in ENGINE_PIDS:
            try:
                self.main.append(psutil.Process(pid))
            except psutil.Error:
                pass
        self.main = self.main or me.children()
        self.tree = me.children(recursive=True)
        TREE_PIDS.extend(p.pid for p in self.tree)
        self.main_gone = self.tree_gone = None

    @staticmethod
    def alive(p):
        try:
            return p.is_running() and (sys.platform == "win32" or p.status() != psutil.STATUS_ZOMBIE)
        except psutil.Error:
            return False

    def run(self):
        stop = time.time() + 90
        while time.time() < stop:
            if self.main_gone is None and not any(self.alive(p) for p in self.main):
                self.main_gone = time.time() * 1000
            if not any(self.alive(p) for p in self.tree):
                self.tree_gone = time.time() * 1000
                if self.main_gone is None:
                    self.main_gone = self.tree_gone
                return
            time.sleep(0.01)


_PHASE_RE = re.compile(r"(Starting|Finished) phase (\S+)")
_ADD_RE = re.compile(r"Adding blocker (.+) for phase (\S+)")
_DONE_RE = re.compile(r"Completed blocker (.+) for phase (\S+)")


def shut_report(lines, t_close, main_gone=None, tree_gone=None):
    """From the lines of an engine's output (wall ms, text), the close's start (wall ms) and when its main process and its whole tree
    were gone: (a) the close -> the quit (the first "Starting phase" line after the close: AsyncShutdown, with its log on, prints
    "DEBUG: Starting phase <topic>" as the first phase starts) and (b) the quit -> the main process gone, -> the tree gone; and the
    lines to show: the matching ones from the close on (capped at 160) plus the "Adding blocker" lines before it (capped at 80),
    each with its time against the close (t_ms) and against the quit (q_ms)."""
    quit_t = next((t for t, text in lines if t >= t_close and "Starting phase" in text), None)
    out = {"close_wall_ms": round(t_close, 1), "a_close_to_quit_ms": round(quit_t - t_close, 1) if quit_t else None,
           "close_to_main_gone_ms": round(main_gone - t_close, 1) if main_gone else None,
           "b_quit_to_main_gone_ms": round(main_gone - quit_t, 1) if quit_t and main_gone else None,
           "b_quit_to_tree_gone_ms": round(tree_gone - quit_t, 1) if quit_t and tree_gone else None,
           "c_main_to_tree_gone_ms": round(tree_gone - main_gone, 1) if main_gone and tree_gone else None,
           "shut_lines": len(lines)}
    # What the engine was in when it exited: the last phase/blocker line before the parent's exit, and the blockers added and not
    # completed by then.
    upto = [(t, x) for t, x in lines if t <= (main_gone or float("inf"))]
    last = next(((t, x) for t, x in reversed(upto) if t >= t_close and ("phase" in x or "blocker" in x)), None)
    out["last_phase_line"] = last[1][:160] if last else None
    out["last_phase_line_ms"] = round(last[0] - t_close, 1) if last else None
    added, done = {}, set()
    for _t, x in upto:
        m = _ADD_RE.search(x)
        if m:
            added[(m.group(2), m.group(1))] = True
        m = _DONE_RE.search(x)
        if m:
            done.add((m.group(2), m.group(1)))
    out["pending_blockers"] = ["%s / %s" % (ph, nm[:50]) for (ph, nm) in added if (ph, nm) not in done][:12]
    phases = {}
    for t, x in lines:
        m = _PHASE_RE.search(x)
        if m and t >= t_close:
            phases.setdefault(m.group(2), [None, None])[0 if m.group(1) == "Starting" else 1] = round(t - quit_t, 1) if quit_t else None
    out["phases"] = phases
    after = [(t, x) for t, x in lines if t >= t_close and SHUT_RE.search(x)][:160]
    before = [(t, x) for t, x in lines if t < t_close and "Adding blocker" in x][:80]
    shown = [(round(t - t_close, 1), round(t - quit_t, 1) if quit_t else None, x) for t, x in before + after]
    return out, shown


# ---- the close of the core with a reaper (nclose arms): the steps of one close, stamped ------------------------------------------------
#: id(core Connection) -> CloseRec of the browser it belongs to; the fds of every connection, to see who closes which end.
CONN_REC = {}
FD_READ, FD_WRITE = {}, {}
CONNS = []


class CloseRec:
    """The instants (time.perf_counter) of one close: the user's close() call, the core's Connection.close entry, the pipe closed by
    the core (the write end: the engine's EOF is the exit command, no Browser.close is sent on the pipe), the engine's end of the pipe
    seen by the core's reader (the empty os.read, then the release of the read end), the real parent's exit (the process the core
    spawned), Connection.close returning, the user's close() returning, the reaper finished (after, joined outside the timing)."""
    __slots__ = ("conn", "pid", "mapped", "t_call", "t_entry", "t_pipe", "t_eof", "t_reader_end", "t_parent", "t_conn_return",
                 "t_return", "t_reaper", "reaper_finished", "wall_call", "wall_parent", "parent_gone_before")

    def __init__(self, conn, mapped):
        self.conn, self.mapped = conn, mapped
        self.pid = getattr(getattr(conn, "_process", None), "pid", None)
        self.t_call = self.t_entry = self.t_pipe = self.t_eof = self.t_reader_end = self.t_parent = None
        self.t_conn_return = self.t_return = self.t_reaper = self.reaper_finished = None
        self.wall_call = self.wall_parent = None
        self.parent_gone_before = False

    def fields(self):
        def ms(t):
            return round((t - self.t_call) * 1000, 1) if t is not None and self.t_call is not None else None
        return {"nc_entry": ms(self.t_entry), "nc_pipe": ms(self.t_pipe), "nc_eof": ms(self.t_eof), "nc_reader_end": ms(self.t_reader_end),
                "nc_parent": ms(self.t_parent), "nc_conn_return": ms(self.t_conn_return), "nc_return": ms(self.t_return),
                "nc_reaper": ms(self.t_reaper), "nc_reaper_finished": self.reaper_finished, "nc_mapped": self.mapped, "nc_pid": self.pid,
                "nc_parent_gone_before_close": self.parent_gone_before}


def install_close_stamps():
    """HUNT: stamp the steps of a close of the core (git main, with the reaper), without changing what it does. Wrapped:
      invisible_core/juggler/connection.py: Connection.__init__   the fds of the pipe and the connection (to see who closes them)
      invisible_core/juggler/connection.py: Connection.close      the entry, and the return of the call that did the work
      invisible_core/juggler/connection.py: os (a copy of the module in its namespace) .close / .read: the close of the write end (the
          pipe closed by the core: Connection.close does `os.close(fd)` of `_to_browser`), the empty read of the reader (the engine's
          EOF) and the release of the read end
      invisible_core/process.py: Reaper.run                       the reaper finished
    """
    import importlib
    import types
    cmod = importlib.import_module("invisible_core.juggler.connection")
    pmod = importlib.import_module("invisible_core.process")
    pc = time.perf_counter
    if not getattr(cmod, "_hunt_os", False):
        proxy = types.ModuleType("os")
        proxy.__dict__.update(os.__dict__)
        real_close, real_read = os.close, os.read

        def close(fd):
            c = FD_WRITE.pop(fd, None)
            if c is not None:
                r = CONN_REC.get(id(c))
                if r is not None and r.t_pipe is None:
                    r.t_pipe = pc()
            else:
                c = FD_READ.pop(fd, None)
                if c is not None:
                    r = CONN_REC.get(id(c))
                    if r is not None and r.t_reader_end is None:
                        r.t_reader_end = pc()
            return real_close(fd)

        def read(fd, n):
            data = real_read(fd, n)
            if not data:
                c = FD_READ.get(fd)
                if c is not None:
                    r = CONN_REC.get(id(c))
                    if r is not None and r.t_eof is None:
                        r.t_eof = pc()
            return data
        proxy.close, proxy.read = close, read
        cmod.os = proxy
        cmod._hunt_os = True
    real_init = cmod.Connection.__init__
    if not getattr(real_init, "_hunt", False):
        def __init__(self, *a, **k):
            to_b = k.get("to_browser", a[0] if a else None)
            from_b = k.get("from_browser", a[1] if len(a) > 1 else None)
            FD_WRITE[to_b], FD_READ[from_b] = self, self       # before the reader thread starts
            CONNS.append(self)
            real_init(self, *a, **k)
        __init__._hunt = True
        cmod.Connection.__init__ = __init__
    real_close_m = cmod.Connection.close
    if not getattr(real_close_m, "_hunt", False):
        def close_m(self, *a, **k):
            rec = CONN_REC.get(id(self))
            mine = rec is not None and rec.t_entry is None
            if mine:
                rec.t_entry = pc()
            if os.environ.get("HUNT_CLOSE_TIMEOUT"):
                # The core's own deadline is 5.0 s (the default of `timeout`, also what aclose passes): raised in this hunt only.
                wide = float(os.environ["HUNT_CLOSE_TIMEOUT"])
                if a and a[0] == 5.0:
                    a = (wide,) + tuple(a[1:])
                elif not a and k.get("timeout", 5.0) == 5.0:
                    k["timeout"] = wide
            try:
                return real_close_m(self, *a, **k)
            finally:
                if mine:
                    rec.t_conn_return = pc()
        close_m._hunt = True
        cmod.Connection.close = close_m
    real_run = pmod.Reaper.run
    if not getattr(real_run, "_hunt", False):
        def run(self):
            try:
                return real_run(self)
            finally:
                self.t_done = pc()
        run._hunt = True
        pmod.Reaper.run = run


def find_conn(browser):
    """The core Connection of a browser: through the client's connection -> transport -> server -> its browser object, else the first
    connection nobody has claimed (exact for one browser per process)."""
    try:
        impl = getattr(browser, "_impl_obj", browser)        # the public Browser wraps the impl, which owns the connection
        for obj in list(impl._connection._transport._server._objects.values()):
            c = getattr(obj, "conn", None)
            if c is not None and type(c).__name__ == "Connection":
                return c, "server"
    except Exception:   # noqa: BLE001
        pass
    for c in CONNS:
        if id(c) not in CONN_REC:
            return c, "guess"
    return None, None


class ParentWatch(threading.Thread):
    """The instant the process the core spawned is gone (polled every 5 ms on its Process object)."""

    def __init__(self, rec):
        super().__init__(daemon=True, name="hunt-parentwatch")
        self.rec = rec
        # A psutil race (the process gone before it is looked at) is a fact about that browser, never a crash of its row.
        try:
            self.proc = psutil.Process(rec.pid) if rec.pid else None
        except psutil.Error:
            self.proc = None
            rec.t_parent = time.perf_counter()
            rec.parent_gone_before = True

    def run(self):
        if self.proc is None:
            return
        stop = time.monotonic() + 120
        while time.monotonic() < stop:
            if not ExitWatch.alive(self.proc):
                self.rec.t_parent = time.perf_counter()
                self.rec.wall_parent = time.time() * 1000
                return
            time.sleep(0.005)


class TreeWatch(threading.Thread):
    """At the close instant (the snapshot is taken in the constructor, before the close is asked for): the process tree of one browser
    by type (parent, tab, gpu, rdd, socket, utility, ...), then every 20 ms the CPU seconds of each process until it exits. The CPU of
    the tree between the close and the parent's exit, and between the close and the last process gone, is the sum over its processes
    of the last CPU read minus the first (an exited process keeps its last read: at most 20 ms lost)."""

    def __init__(self, main_pid):
        super().__init__(daemon=True, name="hunt-treewatch")
        self.main_pid = main_pid
        self.procs = {}          # pid -> [Process, type, first cpu, last cpu, alive]
        self.wall_parent = self.wall_gone = None
        self.cpu_to_parent = self.cpu_to_gone = None
        self.parents = []        # the processes classified "parent" (no -contentproc): pid, parent pid, full command line
        try:
            main = psutil.Process(main_pid)
            for p in [main] + main.children(recursive=True):
                self._add(p)
        except psutil.Error:
            pass

    def _add(self, p):
        try:
            kind = proc_kind(p)
            t = p.cpu_times()
            c = t.user + t.system
            self.procs[p.pid] = [p, kind[3:] if kind.startswith("ff:") else kind, c, c, True]
            if kind == "ff:parent" and len(self.parents) < 6:
                self.parents.append({"pid": p.pid, "ppid": p.ppid(), "cmd": p.cmdline(), "created": round(p.create_time() * 1000)})
        except psutil.Error:
            pass

    def _cpu(self):
        return sum(v[3] - v[2] for v in list(self.procs.values()))

    def run(self):
        stop = time.monotonic() + 120
        tick = 0
        while time.monotonic() < stop:
            tick += 1
            for pid, v in list(self.procs.items()):
                if not v[4]:
                    continue
                try:
                    if not ExitWatch.alive(v[0]):
                        v[4] = False
                        continue
                    t = v[0].cpu_times()
                    v[3] = t.user + t.system
                except psutil.Error:
                    v[4] = False
            now = time.time() * 1000
            main = self.procs.get(self.main_pid)
            if self.wall_parent is None and (main is None or not main[4]):
                self.wall_parent, self.cpu_to_parent = now, self._cpu()
            if tick % 5 == 0 and main is not None and main[4]:
                try:
                    for c in main[0].children(recursive=True):
                        if c.pid not in self.procs:
                            self._add(c)
                except psutil.Error:
                    pass
            if not any(v[4] for v in list(self.procs.values())):
                self.wall_gone, self.cpu_to_gone = now, self._cpu()
                if self.wall_parent is None:
                    self.wall_parent, self.cpu_to_parent = now, self.cpu_to_gone
                return
            time.sleep(0.02)

    def fields(self):
        types = {}
        for v in list(self.procs.values()):
            types[v[1]] = types.get(v[1], 0) + 1
        return {"tree_types": types, "tree_procs": len(self.procs),
                "tree_cpu_to_parent_exit_s": round(self.cpu_to_parent, 3) if self.cpu_to_parent is not None else None,
                "tree_cpu_to_tree_gone_s": round(self.cpu_to_gone, 3) if self.cpu_to_gone is not None else None}


#: tag -> "open" / "closing" / "closed" / "close-failed" of every browser this process launched.
OPEN_BROWSERS = {}
ARM_CTX = {"arm": None, "round": None}


def print_alive(why, daemons=True):
    """Every live thread (name, daemon flag, its Python stack) and the browsers whose close() was not completed."""
    frames = sys._current_frames()
    for t in threading.enumerate():
        if t.daemon and not daemons:
            continue
        fr = frames.get(t.ident)
        stack = []
        if fr is not None:
            stack = ["%s:%d %s" % (f.filename.replace("\\", "/").rsplit("/", 2)[-1], f.lineno, f.name) for f in traceback.extract_stack(fr)[-8:]]
        print("HUNTALIVE " + json.dumps({**ARM_CTX, "why": why, "os": sys.platform, "thread": t.name, "daemon": t.daemon,
                                         "main": t is threading.main_thread(), "stack": stack}), flush=True)
    print("HUNTALIVE " + json.dumps({**ARM_CTX, "why": why, "os": sys.platform, "browsers": dict(OPEN_BROWSERS),
                                     "not_closed": [k for k, v in OPEN_BROWSERS.items() if v != "closed"]}), flush=True)


def start_alive_watchdog():
    """30 s before the arm's limit (HUNT_LIMIT_S, set by the workflow) every live thread is printed; faulthandler writes the stacks of
    all threads to a file beside the pid file 5 s before it, as a fallback should this thread not get to run."""
    limit = float(os.environ.get("HUNT_LIMIT_S") or 0)
    if limit <= 40:
        return
    t = threading.Timer(limit - 30, print_alive, args=("30s-before-the-limit",))
    t.daemon = True
    t.start()
    if os.environ.get("HUNT_PIDFILE"):
        import faulthandler
        global _FAULT_FILE
        _FAULT_FILE = open(os.environ["HUNT_PIDFILE"] + ".fault", "w")
        faulthandler.dump_traceback_later(limit - 5, file=_FAULT_FILE)


_FAULT_FILE = None


def is_bg(arg):
    return arg.lstrip("-/").lower() == "backgroundtask"


class SurvivorWatch(threading.Thread):
    """Every `firefox --backgroundtask ...` process on the machine (the sender of the shutdown pings is one), born after `since` (wall
    ms): scanned every 200 ms, every 20 ms once `fast` is set (the close). Each is followed by a waiter thread that gets its end time
    and, where the OS gives it (Windows: any process; POSIX: only a child), its exit code. `roots_fn()` gives {pid: browser tag} of
    the pids of the browsers' trees (dead ones too); a task is attributed to the browser whose pid is its parent or an ancestor."""

    def __init__(self, since_ms, roots_fn):
        super().__init__(daemon=True, name="hunt-survivors")
        self.since, self.roots_fn = since_ms, roots_fn
        self.found, self.skip = {}, set()
        self.fast, self.stop_ev = threading.Event(), threading.Event()

    def run(self):
        while not self.stop_ev.is_set():
            try:
                self.scan()
            except Exception:   # noqa: BLE001
                pass
            time.sleep(0.02 if self.fast.is_set() else 0.2)

    def attribute(self, ppid):
        roots = self.roots_fn()
        cur, pm = ppid, None
        for _ in range(8):
            if cur in roots:
                return roots[cur]
            if pm is None:
                pm = ppid_map()
            cur = pm.get(cur)
            if not cur:
                break
        return None

    def scan(self):
        for p in psutil.process_iter(["name"]):
            if p.pid in self.found or p.pid in self.skip or "firefox" not in (p.info["name"] or "").lower():
                continue
            try:
                born = p.create_time() * 1000
                if born < self.since:
                    self.skip.add(p.pid)
                    continue
                cmd = p.cmdline()
                if not any(is_bg(a) for a in cmd[1:]):
                    self.skip.add(p.pid)
                    continue
                rec = {"pid": p.pid, "ppid": p.ppid(), "born_wall": born, "cmd": [a[:80] for a in cmd[:4]], "end_wall": None,
                       "end_mono": None, "rc": None}
                rec["attrib"] = self.attribute(rec["ppid"])
            except psutil.Error:
                continue
            self.found[p.pid] = rec
            threading.Thread(target=self._wait, args=(p, rec), daemon=True, name="hunt-survivor-wait").start()

    @staticmethod
    def _wait(p, rec):
        try:
            rc = p.wait(timeout=120)
        except psutil.TimeoutExpired:
            rc = "timeout"
        except psutil.Error:
            rc = None
        rec["end_wall"], rec["end_mono"], rec["rc"] = time.time() * 1000, time.monotonic(), rc

    def finish(self, grace=20.0):
        """The records, once every task found has ended (at most `grace` s) and a last second has been scanned."""
        self.fast.set()
        time.sleep(1.0)
        end = time.monotonic() + grace
        while time.monotonic() < end and any(r["end_wall"] is None for r in list(self.found.values())):
            time.sleep(0.05)
        self.stop_ev.set()
        return list(self.found.values())


def bg_report(recs, t_close, arm, rnd, tag):
    """The background tasks of one browser, each with its birth against the close start, its lifetime and how it ended: rc 0 = by
    itself; another rc (a kill is 1 on Windows, -9 on POSIX) = killed; rc unreadable (POSIX, not our child) = killed when the end
    falls inside a core probe that ends processes (BG_PROBES), else unknown."""
    out = []
    for r in sorted(recs, key=lambda r: r["born_wall"]):
        ended_in = None
        if r["end_mono"] is not None:
            for label, _thread, start, ms, _extra in PROBE:
                if label in BG_PROBES and start - 0.05 <= r["end_mono"] <= start + ms / 1000 + 0.05:
                    ended_in = label
                    break
        rc = r["rc"]
        if rc == 0:
            by = "self"
        elif isinstance(rc, int):
            by = "killed(rc=%s)" % rc
        elif rc == "timeout":
            by = "alive at +120 s"
        else:
            by = ("killed?(%s)" % ended_in) if ended_in else "unknown"
        out.append({"arm": arm, "round": rnd, "tag": tag, "os": sys.platform, "pid": r["pid"], "ppid": r["ppid"],
                    "attrib": r["attrib"] or "assumed", "born_ms": round(r["born_wall"] - t_close, 1),
                    "life_ms": round(r["end_wall"] - r["born_wall"], 1) if r["end_wall"] else None,
                    "end_ms": round(r["end_wall"] - t_close, 1) if r["end_wall"] else None,
                    "cmd": r["cmd"], "rc": rc, "ended_by": by, "ended_in": ended_in})
    return out


def print_bg(rows):
    for d in rows:
        print("HUNTBGTASK " + json.dumps(d), flush=True)


_RS_LINE = re.compile(r"services\.settings: (.*)$")
_RS_SYNC_START = re.compile(r"^(\S+) (?:local timestamp:|Local DB is empty, pull data from server|Fetch changes from server|was not part of startup bundle)")
_RS_SYNC_END = re.compile(r"^(\S+) sync status is (\S+)")
_RS_DUMP_START = re.compile(r"^(\S+) try to restore dump")
_RS_DUMP_END = re.compile(r"^(\S+) (?:imported \d+ records from dump|no dump available)")
_IDB_DONE = re.compile(r"Completed blocker RemoteSettingsClient - finish IDB access\.")


def rs_report(rs_lines, shut_lines, t_launch, t_close, quit_t=None, exit_t=None):
    """The Remote Settings client's log (`console.<level>: services.settings: <message>`, with services.settings.loglevel = debug), from
    the browser's birth to its exit, read as:
      poll        "Start polling for changes (trigger=..)"  ..  "Polling for changes done (duration=..ms)"
      bundle      "Download Remote Settings startup changesets bundle."  ..  "Import of changesets bundle done (..)"
      sync        "<bucket/collection> " + one of "local timestamp:", "Local DB is empty, pull data from server", "Fetch changes from
                  server", "was not part of startup bundle" (first of them)  ..  "<bucket/collection> sync status is <status>"
      dump import "<id> try to restore dump"  ..  "<id> imported N records from dump" or "no dump available"
      attachment  any line naming an attachment (a download or a read), taken as activity within 3 s of the close
    First sync = the earliest start of a poll, a bundle or a collection's sync, in ms from `t_launch`. At the close instant: what has
    started and not ended, and when it ended (ms after the close; None = never, before the exit). The finish-IDB blocker's completion
    ("Completed blocker RemoteSettingsClient - finish IDB access." in the AsyncShutdown log) against the quit."""
    ev = []
    for t, x in rs_lines:
        m = _RS_LINE.search(x)
        if m:
            ev.append((t, m.group(1)))
    syncs, dumps, polls, bundles, attach = {}, {}, [], [], []
    for t, x in ev:
        if x.startswith("Start polling for changes"):
            polls.append([t, None])
        elif x.startswith("Polling for changes done") and polls and polls[-1][1] is None:
            polls[-1][1] = t
        elif x.startswith("Download Remote Settings startup changesets bundle"):
            bundles.append([t, None])
        elif x.startswith("Import of changesets bundle done") and bundles and bundles[-1][1] is None:
            bundles[-1][1] = t
        elif "ttachment" in x:
            attach.append(t)
        else:
            m = _RS_SYNC_END.match(x)
            if m:
                w = syncs.get(m.group(1))
                if w and w[-1][1] is None:
                    w[-1][1] = t
                continue
            m = _RS_SYNC_START.match(x)
            if m:
                w = syncs.setdefault(m.group(1), [])
                if not w or w[-1][1] is not None:
                    w.append([t, None])
                continue
            m = _RS_DUMP_END.match(x)
            if m:
                w = dumps.get(m.group(1))
                if w and w[-1][1] is None:
                    w[-1][1] = t
                continue
            m = _RS_DUMP_START.match(x)
            if m:
                dumps.setdefault(m.group(1), []).append([t, None])
    starts = [w[0] for ws in syncs.values() for w in ws] + [w[0] for w in polls] + [w[0] for w in bundles]
    out = {"rs_lines": len(ev), "rs_lines_after_close": sum(1 for t, _x in ev if t >= t_close),
           "rs_first_poll_ms": round(polls[0][0] - t_launch, 1) if polls and t_launch else None,
           "rs_first_sync_ms": round(min(starts) - t_launch, 1) if starts and t_launch else None,
           "rs_polls": len(polls), "rs_collections_n": len(syncs), "rs_collections": sorted(syncs)[:40]}

    def open_at_close(ws):
        return [w for w in ws if w[0] <= t_close and (w[1] is None or w[1] > t_close)]
    busy = {"syncs": [k for k, ws in syncs.items() if open_at_close(ws)], "dumps": [k for k, ws in dumps.items() if open_at_close(ws)],
            "poll": bool(open_at_close(polls)), "bundle": bool(open_at_close(bundles)),
            "attachment_recent": any(t_close - 3000 <= t <= t_close for t in attach)}
    out["rs_busy_at_close"] = busy
    out["rs_busy"] = bool(busy["syncs"] or busy["dumps"] or busy["poll"] or busy["bundle"] or busy["attachment_recent"])
    pending = open_at_close(polls) + open_at_close(bundles) + [w for ws in syncs.values() for w in open_at_close(ws)] + \
        [w for ws in dumps.values() for w in open_at_close(ws)]
    ends = [w[1] for w in pending if w[1] is not None]
    out["rs_busy_lasts_ms"] = round(max(ends) - t_close, 1) if ends else None
    out["rs_busy_never_ended"] = sum(1 for w in pending if w[1] is None)
    idb = next((t for t, x in shut_lines if _IDB_DONE.search(x)), None)
    out["rs_idb_done_q_ms"] = round(idb - quit_t, 1) if idb is not None and quit_t else None
    out["rs_idb_done_after_exit"] = bool(idb is not None and exit_t and idb > exit_t)
    return out


def print_shut(arm, rnd, tag, shown):
    for t_ms, q_ms, text in shown:
        print("HUNTSHUT " + json.dumps({"arm": arm, "round": rnd, "tag": tag, "os": sys.platform, "t_ms": t_ms, "q_ms": q_ms,
                                        "line": text[:160]}), flush=True)


SV_OF = {}


async def one_browser(arm, binary, port, tag):
    row = {"arm": arm, "tag": tag}
    sv = None
    if os.environ.get("HUNT_SHUT") == "1":
        sv = SurvivorWatch(time.time() * 1000, lambda: {pid: tag for pid in set(ENGINE_PIDS) | set(TREE_PIDS)})
        sv.start()
        SV_OF[tag] = sv
    c_start = await asyncio.to_thread(parent_cpu)
    t0 = time.monotonic()
    row["launch_wall_ms"] = time.time() * 1000
    if arm == "inproc":
        from invisible_playwright.async_api import InvisiblePlaywright
        extra = json.loads(os.environ.get("HUNT_EXTRA_PREFS") or "{}") or None
        if os.environ.get("HUNT_SHUT") == "1" or os.environ.get("HUNT_NCSHUT") == "1":
            extra = {**(extra or {}), **SHUT_PREFS, **(RS_PREFS if os.environ.get("HUNT_RS") == "1" else {})}
        more = {}
        if os.environ.get("HUNT_TIMEZONE"):
            more["timezone"] = os.environ["HUNT_TIMEZONE"]
        if PROXY_PORT:
            more["proxy"] = {"server": "http://127.0.0.1:%d" % PROXY_PORT}
        cm = InvisiblePlaywright(seed=42, binary_path=binary, humanize=True, headless=True, extra_prefs=extra, **more)
        browser = await cm.__aenter__()
        row["enter_ms"] = round((time.monotonic() - t0) * 1000)
        close = lambda: cm.__aexit__(None, None, None)
    elif arm == "remote":
        from playwright.async_api import async_playwright
        proc, endpoint = await serve_endpoint(binary)
        pw = await async_playwright().start()
        browser = await pw.firefox.connect(endpoint)

        async def close(browser=browser, pw=pw, proc=proc):
            await browser.close()
            await pw.stop()
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), 60)
    else:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        browser = await pw.firefox.launch(headless=True)
        row["enter_ms"] = round((time.monotonic() - t0) * 1000)

        async def close(browser=browser, pw=pw):
            await browser.close()
            await pw.stop()
    OPEN_BROWSERS[tag] = "open"
    try:
        await _browser_work(arm, port, tag, row, browser, close, t0, c_start)
    except Exception as exc:   # noqa: BLE001
        # A failure of the work (or of the hunt's own probes) is a fact of this row; the browser is closed all the same, below.
        row["error_before_close"] = "%s: %s" % (type(exc).__name__, str(exc)[:300])
    return await _close_browser(arm, port, tag, row, browser, close, t0)


async def _browser_work(arm, port, tag, row, browser, close, t0, c_start):
    # The launch in four segments, each with its time and the CPU of the Firefox parent processes (every browser of this process
    # added: exact for one browser per process) during it. A CPU read takes tens of ms on Windows (the process list): it is taken in
    # a thread, and its time is taken out of the segment times and of launch_ms.
    reads = {"ms": 0.0}

    async def cpu_mark():
        r0 = time.monotonic()
        c = await asyncio.to_thread(parent_cpu)
        reads["ms"] += (time.monotonic() - r0) * 1000
        return c
    c_enter = await cpu_mark()
    row["enter_cpu_s"] = round(c_enter - c_start, 3)
    t_ctx = time.monotonic()
    ctx = await browser.new_context()
    row["ctx_ms"] = round((time.monotonic() - t_ctx) * 1000)
    c_ctx = await cpu_mark()
    row["ctx_cpu_s"] = round(c_ctx - c_enter, 3)
    t_np = time.monotonic()
    page = await ctx.new_page()
    row["new_page_ms"] = round((time.monotonic() - t_np) * 1000)
    t_np_end = time.monotonic()
    # launch_ms is what it always was (the start of the launch to the end of new_page), less the CPU reads made in between.
    row["launch_ms"] = round((t_np_end - t0) * 1000 - reads["ms"])
    LAUNCHED["n"] += 1
    if LAUNCHED["n"] >= LAUNCHED["of"]:
        LAUNCHED["t"] = time.monotonic()
        LAUNCHED["event"].set()
    if row["launch_ms"] > SLOW_MS:
        SLOW.append(("launch", tag, t0, time.monotonic()))
    c_np = await cpu_mark()
    row["new_page_cpu_s"] = round(c_np - c_ctx, 3)
    reads_before_load = reads["ms"]
    t_fl = time.monotonic()
    await page.goto("http://127.0.0.1:%d/first?b=%s" % (port, tag), wait_until="load", timeout=30000)
    row["first_load_ms"] = round((time.monotonic() - t_fl) * 1000)
    row["to_first_load_ms"] = round((time.monotonic() - t0) * 1000 - reads_before_load)
    c_fl = await cpu_mark()
    row["first_load_cpu_s"] = round(c_fl - c_np, 3)
    row["goto_req_ms"], row["eval_ms"] = await work(page, port, tag, trace=(arm == "inproc" and TRACE_EVAL
                                                                          and int(tag.rsplit("-", 1)[1]) == 0))
    try:
        row["typing"] = await typing(page, port, arm, TEXTS[int(tag.rsplit("-", 1)[1]) % len(TEXTS)])
    except Exception as exc:   # noqa: BLE001
        row["typing"] = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
    if SESSION_S:
        # The shutdown arms: idle until the session is SESSION_S old (from the start of the launch), then close.
        await asyncio.sleep(max(0.0, t0 + SESSION_S - time.monotonic()))


async def _close_browser(arm, port, tag, row, browser, close, t0):
    """The close of one browser, always reached: what failed before it is in the row, what fails in the probes around it is too."""
    row["session_s_at_close"] = round(time.monotonic() - t0, 2)
    OPEN_BROWSERS[tag] = "closing"
    rec = pwatch = tw = None
    try:
        if os.environ.get("HUNT_NCLOSE") == "1":
            conn, how = find_conn(browser)
            if conn is not None:
                rec = CloseRec(conn, how)
                CONN_REC[id(conn)] = rec
                pwatch = ParentWatch(rec)
                pwatch.start()
                if os.environ.get("HUNT_NCSHUT") == "1":
                    tw = TreeWatch(rec.pid)          # the tree by type at this instant, before the close is asked for
                    tw.start()
                    try:
                        impl_contexts = list(browser.contexts)
                        row["ncontexts"] = len(impl_contexts)
                        row["npages"] = sum(len(c.pages) for c in impl_contexts)
                    except Exception as exc:   # noqa: BLE001
                        row["ncontexts_error"] = repr(exc)[:120]
    except Exception as exc:   # noqa: BLE001
        row["probe_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
    watch = None
    try:
        if os.environ.get("HUNT_SHUT") == "1":
            watch = ExitWatch()
            watch.start()
            row["parent_pids"] = [p.pid for p in watch.main]
            row["close_wall_ms"] = time.time() * 1000
    except Exception as exc:   # noqa: BLE001
        row["probe_error_exitwatch"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
    sv = SV_OF.get(tag)
    if sv is not None:
        sv.fast.set()
    t1 = time.monotonic()
    if rec is not None:
        rec.t_call = time.perf_counter()
        rec.wall_call = time.time() * 1000
    try:
        await close()
        OPEN_BROWSERS[tag] = "closed"
    except Exception as exc:   # noqa: BLE001
        OPEN_BROWSERS[tag] = "close-failed"
        row["close_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:300])
    if rec is not None:
        rec.t_return = time.perf_counter()
    row["close_ms"] = round((time.monotonic() - t1) * 1000)
    try:
        if rec is not None:
            # After the close's own timing: the parent's exit, and the reaper (joined here, never inside the close).
            await asyncio.to_thread(pwatch.join, 125)
            reaper = getattr(rec.conn, "reaper", None)
            if reaper is not None:
                await asyncio.to_thread(reaper.join, 60)
                rec.t_reaper = getattr(reaper, "t_done", None) or time.perf_counter()
                rec.reaper_finished = bool(reaper.finished)
            row.update(rec.fields())
            if tw is not None:
                await asyncio.to_thread(tw.join, 125)
                row.update(tw.fields())
                lines = ENGINE_LINES_BY.get(id(getattr(rec.conn, "output", None)), [])
                fields, shown = shut_report(lines, rec.wall_call, rec.wall_parent, tw.wall_gone)
                row.update(fields)
                if os.environ.get("HUNT_RS") == "1":
                    quit_t = rec.wall_call + fields["a_close_to_quit_ms"] if fields.get("a_close_to_quit_ms") is not None else None
                    row.update(rs_report(lines, lines, row.get("launch_wall_ms"), rec.wall_call, quit_t, rec.wall_parent))
                row["_parents"] = tw.parents
                row["_shown"] = shown
                if row.get("nc_parent") is not None and row["nc_parent"] > 4900:
                    row["_slow"] = {"tag": tag, "parent_exit_ms": row["nc_parent"], "last_phase_line": fields.get("last_phase_line"),
                                    "last_phase_line_ms": fields.get("last_phase_line_ms"), "pending_blockers": fields.get("pending_blockers"),
                                    "tail": [[t, x[:140]] for t, _q, x in shown if 0 <= t <= row["nc_parent"] + 1][-8:]}
        if watch is not None:
            await asyncio.to_thread(watch.join, 95)
            row["_main_gone"], row["_tree_gone"] = watch.main_gone, watch.tree_gone
            row["_bg"] = await asyncio.to_thread(sv.finish)
    except Exception as exc:   # noqa: BLE001
        row["probe_error_after"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
    if row["close_ms"] > SLOW_MS:
        SLOW.append(("close", tag, t1, time.monotonic()))
    return row


PROBE = []   # (name, thread, start monotonic, duration ms, extra)


def install_probes():
    """HUNT: time the suspects inside the published packages of this process, without changing what they do.
    - the module lock of the egress discovery (_geo._family_lock): for each acquire, the waiting and the holding, and the holder;
    - the steps of a close and of a launch: Connection.close, SessionFiles.remove, remove_profile, reap_session, EngineTree.end /
      wait_job_empty, discover_egress_ip, ensure_geoip_mmdb, wait_for_ready, prepare_session_geo."""
    import functools
    import importlib

    def timed(owner, name, label):
        real = getattr(owner, name, None)
        if real is None or getattr(real, "_hunt", False):
            return

        @functools.wraps(real)
        def call(*a, **k):
            t = time.monotonic()
            try:
                return real(*a, **k)
            finally:
                PROBE.append((label, threading.current_thread().name, t, round((time.monotonic() - t) * 1000, 1), None))
        call._hunt = True
        setattr(owner, name, call)

    try:
        geo = importlib.import_module("invisible_core._geo")

        class TimedLock:
            def __init__(self, real):
                self.real = real

            def __enter__(self):
                t = time.monotonic()
                self.real.acquire()
                self.got = time.monotonic()
                self.waited = round((self.got - t) * 1000, 1)
                return self

            def __exit__(self, *exc):
                held = round((time.monotonic() - self.got) * 1000, 1)
                PROBE.append(("geo._family_lock", threading.current_thread().name, self.got, held, {"waited_ms": self.waited}))
                self.real.release()

            def acquire(self, *a, **k):
                return self.real.acquire(*a, **k)

            def release(self):
                return self.real.release()
        geo._family_lock = TimedLock(geo._family_lock)
        timed(geo, "discover_egress_ip", "geo.discover_egress_ip")
        timed(geo, "prepare_session_geo", "geo.prepare_session_geo")
    except Exception as exc:   # noqa: BLE001
        PROBE.append(("probe-error geo", "", 0, 0, repr(exc)[:200]))
    # The first round's prepare_session_geo took 3.8 s with discover_egress_ip at 0.1-0.4 s, and the probe on
    # `_geoip_db.ensure_geoip_mmdb` never fired: `_geo` imports it by name from `invisible_core.download` (a re-export), inside the
    # function, so the copy to wrap is `download.ensure_geoip_mmdb`. Every helper prepare_session_geo calls, by the name `_geo` uses:
    # the mmdb (location, check of the latest tag, download), the reader (open + lookup), the language decision, the position.
    try:
        geo = importlib.import_module("invisible_core._geo")
        for name in ("_geo_record", "ip_to_timezone", "ip_to_coordinates", "ip_to_country", "position_to_declare", "_decide",
                     "_geoip_database", "_egress_country"):
            timed(geo, name, "geo." + name)
        udp = importlib.import_module("invisible_core._udp_exit")
        for name in ("stun_servers", "start_measurement", "remember"):
            timed(udp, name, "udp_exit." + name)
        timed(importlib.import_module("invisible_core.download"), "ensure_geoip_mmdb", "download.ensure_geoip_mmdb")
        gdb = importlib.import_module("invisible_core._geoip_db")
        for name in ("_cached_geoip_mmdb", "_check_is_due", "_resolve_latest_geoip_tag", "_latest_geoip_tag_api",
                     "_download_geoip_tag", "_record_check", "_prune_old_geoip_tags"):
            timed(gdb, name, "geoip_db." + name)
        import requests
        timed(requests, "head", "requests.head")       # the permalink HEAD of _resolve_latest_geoip_tag
    except Exception as exc:   # noqa: BLE001
        PROBE.append(("probe-error geo helpers", "", 0, 0, repr(exc)[:200]))
    for mod, attrs in (("invisible_core._geoip_db", ["ensure_geoip_mmdb"]),
                       ("invisible_core.juggler._profile", ["remove_profile"]),
                       ("invisible_core.process", ["reap_session", "find_processes", "terminate"])):
        try:
            m = importlib.import_module(mod)
            for a in attrs:
                timed(m, a, mod.rsplit(".", 1)[-1] + "." + a)
        except Exception as exc:   # noqa: BLE001
            PROBE.append(("probe-error " + mod, "", 0, 0, repr(exc)[:200]))
    # _profile imports find_processes and terminate by name: its copies are the ones SessionFiles.remove calls.
    try:
        prof = importlib.import_module("invisible_core.juggler._profile")
        for a in ("find_processes", "terminate"):
            if hasattr(prof, a):
                timed(prof, a, "_profile." + a)
    except Exception as exc:   # noqa: BLE001
        PROBE.append(("probe-error _profile", "", 0, 0, repr(exc)[:200]))
    # Every command to the engine that took over 100 ms, by method: where the launch spends what connection.launch does not.
    try:
        cmod = importlib.import_module("invisible_core.juggler.connection")
        real_send = cmod.Connection.send
        if not getattr(real_send, "_hunt", False):
            @functools.wraps(real_send)
            def send(self, method, *a, **k):
                t = time.monotonic()
                try:
                    return real_send(self, method, *a, **k)
                finally:
                    ms = round((time.monotonic() - t) * 1000, 1)
                    if ms > 100:
                        PROBE.append(("send " + str(method), threading.current_thread().name, t, ms, None))
            send._hunt = True
            cmod.Connection.send = send
    except Exception as exc:   # noqa: BLE001
        PROBE.append(("probe-error send", "", 0, 0, repr(exc)[:200]))
    for mod, cls, attrs in (("invisible_core.juggler.connection", "Connection", ["close", "startup_send"]),
                            ("invisible_core.juggler._profile", "SessionFiles", ["remove", "__init__"]),
                            ("invisible_core.process", "EngineTree", ["end", "wait_job_empty", "release"])):
        try:
            c = getattr(importlib.import_module(mod), cls)
            for a in attrs:
                timed(c, a, cls + "." + a)
        except Exception as exc:   # noqa: BLE001
            PROBE.append(("probe-error " + cls, "", 0, 0, repr(exc)[:200]))
    try:
        conn = importlib.import_module("invisible_core.juggler.connection")
        timed(conn, "wait_for_ready", "connection.wait_for_ready")
        timed(conn, "launch", "connection.launch")
    except Exception as exc:   # noqa: BLE001
        PROBE.append(("probe-error connection", "", 0, 0, repr(exc)[:200]))
    # The wrapper imports some of these by name: rebind its copies too.
    for mod in ("invisible_playwright.async_api", "invisible_playwright._session", "invisible_playwright._juggler.server"):
        try:
            m = importlib.import_module(mod)
            for name in ("prepare_session_geo", "remove_profile"):
                if hasattr(m, name):
                    timed(m, name, mod.rsplit(".", 1)[-1] + "." + name)
        except Exception:   # noqa: BLE001
            pass


class CpuSampler:
    """The last CPU seconds seen of every engine process under this one, sampled every 0.2 s for the whole round: a process that
    ends between two samples loses at most 0.2 s of its CPU."""

    def __init__(self):
        self.last, self.stop = {}, threading.Event()
        self.t = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.is_set():
            self.last.update(engine_cpu())
            time.sleep(0.2)

    def total(self):
        self.stop.set()
        self.t.join(2)
        return sum(self.last.values())


async def main(arm, n, rnd, binary):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    # HUNT_NO_SAMPLER=1 (the inproc4quiet arm): none of the hunt's own polling threads (StackSampler, CpuSampler), so the cost of the
    # polling on the evaluate round trip can be read against the arm that has them. The CPU per browser is then None.
    quiet = os.environ.get("HUNT_NO_SAMPLER") == "1"
    sampler = None if quiet else CpuSampler()
    if sampler is not None:
        sampler.t.start()
        SAMPLER.start()
    if arm in ("inproc", "remote") and os.environ.get("HUNT_PROBES") == "1":
        install_probes()
    global TRACE_EVAL
    if arm == "inproc":
        install_key_probe()
        install_evalsplit()
        TRACE_EVAL = True
        if os.environ.get("HUNT_SHUT") == "1" or os.environ.get("HUNT_NCSHUT") == "1":
            install_output_capture()
        if os.environ.get("HUNT_NCLOSE") == "1":
            install_close_stamps()
    global PROXY_PORT
    if os.environ.get("HUNT_PROXY") == "1":
        proxy = ThreadingProxy(("127.0.0.1", 0), ProxyHandler)
        threading.Thread(target=proxy.serve_forever, daemon=True, name="hunt-proxy").start()
        PROXY_PORT = proxy.server_address[1]
    LAUNCHED.update({"of": n, "event": asyncio.Event()})
    t0, t_mono0 = time.time(), time.monotonic()
    rows = await asyncio.gather(*(one_browser(arm, binary, port, "%s-%s-%d" % (arm, rnd, i)) for i in range(n)), return_exceptions=True)
    cpu = sampler.total() if sampler is not None else None
    match_sends(rows)
    for r in rows:
        if isinstance(r, dict) and "raw" in (r.get("typing") or {}):
            t = r["typing"]
            keys, ups, moves, clicks, plan, sends, ev = t.pop("raw")
            cplan, splan = t.pop("click_plan", None), t.pop("settle_plan", None)
            try:
                t.update(typing_report(arm, keys, ups, moves, clicks, plan, sends))
                t.update(mouse_report(ev, cplan, splan))
            except Exception as exc:   # noqa: BLE001
                t["error"] = "report %s: %s" % (type(exc).__name__, str(exc)[:200])
    for r in rows:
        if isinstance(r, Exception):
            print("HUNTROW " + json.dumps({"arm": arm, "round": rnd, "crash": repr(r)[:800]}), flush=True)
            continue
        r.update({"round": rnd, "n": n, "os": sys.platform, "wall_s": round(time.time() - t0, 1),
                  "cpu_s_per_browser": round(cpu / n, 2) if cpu is not None else None})
        shown = []
        if r.get("close_wall_ms"):
            # One browser per process: every line the engine printed is this browser's.
            fields, shown = shut_report(ENGINE_LINES, r["close_wall_ms"], r.pop("_main_gone", None), r.pop("_tree_gone", None))
            r.update(fields)
            r["engine_lines_total"] = len(ENGINE_LINES)
        bgs = bg_report(r.pop("_bg", []), r["close_wall_ms"], arm, rnd, r["tag"]) if r.get("close_wall_ms") else []
        r["bg_n"] = len(bgs)
        shown = r.pop("_shown", shown)
        slow = r.pop("_slow", None)
        parents = r.pop("_parents", None)
        print("HUNTROW " + json.dumps(r), flush=True)
        if parents is not None:
            print("HUNTPARENTS " + json.dumps({"arm": arm, "round": rnd, "os": sys.platform, "tag": r["tag"], "parents": parents}), flush=True)
        print_shut(arm, rnd, r["tag"], shown)
        print_bg(bgs)
        if slow:
            print("HUNTSLOWCLOSE " + json.dumps({"arm": arm, "round": rnd, "os": sys.platform, **slow}), flush=True)
    print_alive("end-of-main", daemons=False)
    for tag, segs in EVAL_OUT:
        print("HUNTEVAL " + json.dumps({"arm": arm, "round": rnd, "os": sys.platform, "tag": tag, "segs": segs}), flush=True)
    SAMPLER.stop.set()
    for name, thread, start, ms, extra in PROBE:
        print("HUNTPROBE " + json.dumps({"arm": arm, "round": rnd, "os": sys.platform, "name": name, "thread": thread,
                                         "t": round(start - t_mono0, 3), "ms": ms, "extra": extra}), flush=True)
    for kind, tag, a, b in SLOW:
        print("HUNTSLOW " + json.dumps({"arm": arm, "round": rnd, "os": sys.platform, "kind": kind, "tag": tag, "ms": round((b - a) * 1000),
                                        **SAMPLER.explain(a, b)}), flush=True)
    httpd.shutdown()


# ---- the retail reference: the real Firefox, not Playwright --------------------------------------------------------------------------
IS_WIN = sys.platform == "win32"
#: What the shutdown log is searched for (AsyncShutdown's own lines and the phases' names).
SHUT_RE = re.compile(r"asyncshutdown|shutdown|quit|phase|profile-before-change|profile-change|xpcom|barrier|blocker", re.I)
USER_JS = 'user_pref("toolkit.asyncshutdown.log", true);\nuser_pref("browser.dom.window.dump.enabled", true);\n'


def ppid_map():
    """{pid: parent pid} of every process, from one snapshot (psutil's own call, the one `children()` uses)."""
    try:
        return psutil._psplatform.ppid_map()
    except Exception:   # noqa: BLE001
        out = {}
        for p in psutil.process_iter():
            try:
                out[p.pid] = p.ppid()
            except psutil.Error:
                pass
        return out


def visible_windows():
    """Windows: (pid, window class) of every visible top-level window."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            out.append((pid.value, cls.value))
        return True
    user32.EnumWindows(each, 0)
    return out


def now_ms():
    return time.time() * 1000


class RetailBrowser:
    """One retail Firefox: started on a fresh profile, watched every 20 ms by a thread of its own (its window, its process tree, its
    log), closed normally, and timed until every process of its tree is gone."""

    def __init__(self, tag, binary, work):
        self.tag, self.binary = tag, binary
        self.profile = os.path.join(work, tag)
        os.makedirs(self.profile)
        with open(os.path.join(self.profile, "user.js"), "w", encoding="utf-8", newline="\n") as f:
            f.write(USER_JS + ('user_pref("services.settings.loglevel", "debug");\n' if os.environ.get("HUNT_RS") == "1" else ""))
        self.logpath = os.path.join(work, tag + ".log")
        self.proc = None
        self.t_start = None
        self.procs = {}            # pid -> psutil.Process of every process seen in the tree (the launcher too, when there is one)
        self.names = {}
        self.exit_ms = {}          # pid -> ms after the close at which it was found gone
        self.window_ms = self.window_pid = self.window_class = None
        self.window_note = None
        self.seen_vis = {}         # Windows: pid -> (first wall ms seen with a visible top-level window, class)
        self.next_tree = 0
        self.t_close = None
        self.cpu_last = None
        self.log_matches = []      # (wall ms, line)
        self.rs_lines = []         # (wall ms, line) of the Remote Settings client's log, all of it
        self.log_lines = 0
        self.log_bytes = 0
        self._log_buf = b""
        self.finished = threading.Event()
        self.row = {}
        self.error = None

    # -- the watcher thread
    def watch(self):
        logf = None
        try:
            logf = open(self.logpath, "rb")
        except OSError:
            pass
        while not self.finished.is_set():
            try:
                t = now_ms()
                if self.window_ms is None:
                    self.poll_window(t)
                if t >= self.next_tree:
                    self.refresh_tree()
                    self.next_tree = t + 100
                if logf is not None:
                    self.tail_log(logf)
                if self.t_close is not None:
                    self.sample_cpu()
                if self.proc is not None:
                    self.proc.poll()    # reaps the child: a zombie would look alive on Linux
            except Exception as exc:   # noqa: BLE001
                self.error = self.error or "%s: %s" % (type(exc).__name__, str(exc)[:160])
            time.sleep(0.02)
        if logf is not None:
            try:
                self.tail_log(logf)
                logf.close()
            except OSError:
                pass

    def refresh_tree(self):
        pm = ppid_map()
        kids = {}
        for pid, pp in pm.items():
            kids.setdefault(pp, []).append(pid)
        tree = set(self.procs) | {self.proc.pid}
        stack = list(tree)
        while stack:
            cur = stack.pop()
            for c in kids.get(cur, ()):
                if c not in tree:
                    tree.add(c)
                    stack.append(c)
        for pid in tree:
            if pid not in self.procs:
                try:
                    p = psutil.Process(pid)
                    self.names[pid] = p.name()
                    self.procs[pid] = p
                except psutil.Error:
                    pass
        if IS_WIN and self.window_ms is None:
            self.match_windows()

    def match_windows(self):
        hits = [(self.seen_vis[pid][0], pid) for pid in list(self.procs) if pid in self.seen_vis]
        if hits:
            ms, pid = min(hits)
            self.window_ms, self.window_pid, self.window_class = ms, pid, self.seen_vis[pid][1]

    def poll_window(self, t):
        """The top-level window: Windows, EnumWindows + IsWindowVisible + GetWindowThreadProcessId, matched with the process tree
        (a window seen before its pid joined the tree keeps its first-seen time); Linux, `xdotool search --onlyvisible --pid`."""
        if IS_WIN:
            for pid, cls in visible_windows():
                self.seen_vis.setdefault(pid, (t, cls))
            self.match_windows()
            return
        if self.window_note:
            return
        cands = {self.proc.pid} | {pid for pid, nm in list(self.names.items()) if nm.startswith("firefox")}
        for pid in cands:
            try:
                r = subprocess.run(["xdotool", "search", "--onlyvisible", "--pid", str(pid)], capture_output=True, text=True, timeout=5)
            except FileNotFoundError:
                self.window_note = "xdotool is not installed"
                return
            except subprocess.TimeoutExpired:
                continue
            if r.stdout.strip():
                self.window_ms, self.window_pid, self.window_class = now_ms(), pid, None
                self.x_window = r.stdout.split()[0]
                return

    def tail_log(self, logf):
        data = logf.read()
        if not data:
            return
        self.log_bytes += len(data)
        self._log_buf += data
        *lines, self._log_buf = self._log_buf.split(b"\n")
        t = now_ms()
        for raw in lines:
            self.log_lines += 1
            text = raw.decode("utf-8", "replace").rstrip("\r")
            if SHUT_RE.search(text):
                self.log_matches.append((t, text))
            if "services.settings:" in text:
                self.rs_lines.append((t, text))

    def main_pid(self):
        return self.window_pid or (self.proc.pid if self.proc is not None else None)

    def sample_cpu(self):
        try:
            t = psutil.Process(self.main_pid()).cpu_times()
            self.cpu_last = t.user + t.system
        except psutil.Error:
            pass

    def alive_procs(self):
        out = []
        for pid, p in list(self.procs.items()):
            try:
                if p.is_running() and (IS_WIN or p.status() != psutil.STATUS_ZOMBIE):
                    out.append(pid)
            except psutil.Error:
                pass
        return out

    # -- the session
    def session(self, port, close_after):
        row = self.row
        try:
            self.log = open(self.logpath, "wb")
            self.t_start = now_ms()
            url = "http://127.0.0.1:%d/retail?b=%s" % (port, self.tag)
            # Windows: the launcher process hands over to the browser and exits at once (the first run of this arm saw every
            # browser "exit before close", no window, the close timing out); -wait-for-browser keeps it until the browser ends,
            # with the browser as its child, so the tree is followed. The browser itself is the retail one either way.
            extra = ["-wait-for-browser"] if sys.platform == "win32" else []
            self.proc = subprocess.Popen([self.binary, *extra, "-no-remote", "-new-instance", "-profile", self.profile, url],
                                         stdout=self.log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            try:
                self.procs[self.proc.pid] = psutil.Process(self.proc.pid)
                self.names[self.proc.pid] = self.procs[self.proc.pid].name()
            except psutil.Error:
                pass
            self.watcher = threading.Thread(target=self.watch, daemon=True, name="hunt-watch-" + self.tag)
            self.watcher.start()
            deadline = time.monotonic() + 120
            while self.tag not in BEACONS and time.monotonic() < deadline and self.proc.poll() is None:
                time.sleep(0.02)
            loaded = BEACONS.get(self.tag)
            row["start_to_load_ms"] = round(loaded - self.t_start, 1) if loaded else None
            row["cpu_at_load_s"] = self._cpu_now()
            if loaded:
                while now_ms() < loaded + close_after * 1000 and self.proc.poll() is None:
                    time.sleep(0.05)
            row["start_to_window_ms"] = round(self.window_ms - self.t_start, 1) if self.window_ms else None
            row["window_class"] = self.window_class
            row["window_note"] = self.window_note
            row["cpu_at_close_s"] = self._cpu_now()
            row["tree_cpu_at_close_s"] = self._tree_cpu()
            row["procs_at_close"] = len(self.alive_procs())
            row["exited_before_close"] = self.proc.poll() is not None
            self.close()
        except Exception as exc:   # noqa: BLE001
            self.error = "%s: %s" % (type(exc).__name__, str(exc)[:300])
        finally:
            self.finished.set()
            try:
                self.watcher.join(3)      # its last read of the log is part of the report
            except (AttributeError, RuntimeError):
                pass
            try:
                self.log.close()
            except (AttributeError, OSError):
                pass

    def _cpu_now(self):
        try:
            t = psutil.Process(self.main_pid()).cpu_times()
            return round(t.user + t.system, 3)
        except (psutil.Error, TypeError):
            return None

    def _tree_cpu(self):
        total = 0.0
        for pid in self.alive_procs():
            try:
                t = self.procs[pid].cpu_times()
                total += t.user + t.system
            except psutil.Error:
                pass
        return round(total, 3)

    def close(self):
        """A normal close: Windows `taskkill /PID <the process with the window>` without /F (WM_CLOSE), Linux SIGTERM to the process
        started. close_ms is from the command's return to the instant no process of the tree is alive."""
        row = self.row
        target = self.main_pid()
        try:
            tw = TreeWatch(target)       # the tree by type at the instant before the close is asked for
        except Exception as exc:   # noqa: BLE001
            tw = None
            row["tree_error"] = repr(exc)[:120]
        c0 = now_ms()
        if IS_WIN:
            r = subprocess.run(["taskkill", "/PID", str(target)], capture_output=True, text=True, timeout=30)
            row["close_cmd"] = [r.returncode, (r.stdout + r.stderr).strip()[:140]]
        elif getattr(self, "x_window", None):
            # Linux: SIGTERM is not a normal close (the first run saw the browser gone in 65 ms with no shutdown phase logged).
            # A window manager's close (_NET_CLOSE_WINDOW, what the title bar's button sends) is.
            try:
                r = subprocess.run(["wmctrl", "-i", "-c", hex(int(self.x_window))], capture_output=True, text=True, timeout=30)
                row["close_cmd"] = [r.returncode, ("wmctrl " + r.stdout + r.stderr).strip()[:140]]
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                row["close_cmd"] = [1, repr(exc)[:140]]
        else:
            try:
                os.kill(self.proc.pid, signal.SIGTERM)
                row["close_cmd"] = [0, "SIGTERM %d (no window to close)" % self.proc.pid]
            except OSError as exc:
                row["close_cmd"] = [1, repr(exc)[:140]]
        self.t_close = now_ms()
        if tw is not None:
            tw.start()
        if RETAIL_SV is not None:
            RETAIL_SV.fast.set()
        row["parent_pid"], row["parent_is_window_owner"] = target, self.window_pid is not None
        row["close_cmd_ms"] = round(self.t_close - c0, 1)
        row["session_s"] = round((self.t_close - self.t_start) / 1000, 2)
        deadline = self.t_close + 120000
        gone = None
        while now_ms() < deadline:
            alive = set(self.alive_procs())
            t = now_ms()
            for pid in list(self.procs):
                if pid not in alive and pid not in self.exit_ms:
                    self.exit_ms[pid] = round(t - self.t_close, 1)
            if not alive:
                gone = t
                break
            time.sleep(0.02)
        if gone is None:
            row["close_timeout"] = True
            row["close_ms"] = None
            for pid in self.alive_procs():
                try:
                    self.procs[pid].kill()
                except psutil.Error:
                    pass
        else:
            row["close_ms"] = round(gone - self.t_close, 1)
        self.gone_wall = gone
        main_exit = self.exit_ms.get(target)
        self.main_gone_wall = self.t_close + main_exit if main_exit is not None else None
        row["close_main_ms"] = main_exit
        last = sorted(((ms, self.names.get(pid, "?")) for pid, ms in self.exit_ms.items()), reverse=True)[:4]
        row["last_exits"] = [[nm, ms] for ms, nm in last]
        row["procs_seen"] = len(self.procs)
        if tw is not None:
            tw.join(30)
            row.update(tw.fields())
        row["parent_cpu_s"] = round(self.cpu_last, 3) if self.cpu_last is not None else row.get("cpu_at_close_s")


RETAIL_SV = None


def retail_main(n, rnd, binary, close_after):
    global RETAIL_SV
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    work = tempfile.mkdtemp(prefix="hunt-retail-")
    browsers = [RetailBrowser("retail-%s-%d" % (rnd, i), binary, work) for i in range(n)]
    t0 = time.time()
    RETAIL_SV = SurvivorWatch(t0 * 1000, lambda: {pid: b.tag for b in browsers for pid in list(b.procs)})
    RETAIL_SV.start()
    threads = [threading.Thread(target=b.session, args=(port, close_after), daemon=True) for b in browsers]
    for t in threads:
        t.start()
    stop = time.monotonic() + close_after + 400
    for t in threads:
        t.join(max(1.0, stop - time.monotonic()))
    bg_all = RETAIL_SV.finish()
    for b in browsers:
        b.finished.set()
        for pid in b.alive_procs():
            try:
                b.procs[pid].kill()
            except psutil.Error:
                pass
    for b in browsers:
        r = b.row
        if b.error or not r:
            print("HUNTROW " + json.dumps({"arm": "retail", "round": rnd, "tag": b.tag, "crash": (b.error or "no result")[:800]}), flush=True)
            continue
        r.update({"arm": "retail", "tag": b.tag, "round": rnd, "n": n, "os": sys.platform, "close_after_s": close_after,
                  "wall_s": round(time.time() - t0, 1), "log_bytes": b.log_bytes, "log_lines": b.log_lines, "watch_error": b.error})
        # The shutdown log, see shut_report: against the close (t_ms) and against the quit (q_ms).
        shown = []
        if b.t_close:
            fields, shown = shut_report(b.log_matches, b.t_close, getattr(b, "main_gone_wall", None), getattr(b, "gone_wall", None))
            r.update(fields)
            if os.environ.get("HUNT_RS") == "1":
                quit_t = b.t_close + fields["a_close_to_quit_ms"] if fields.get("a_close_to_quit_ms") is not None else None
                r.update(rs_report(b.rs_lines, b.log_matches, b.t_start, b.t_close, quit_t, getattr(b, "main_gone_wall", None)))
            if (r.get("close_main_ms") or 0) > 4900:
                tail = [[t, x[:140]] for t, _q, x in shown if 0 <= t <= r["close_main_ms"] + 1][-8:]
                print("HUNTSLOWCLOSE " + json.dumps({"arm": "retail", "round": rnd, "os": sys.platform, "tag": b.tag,
                                                     "parent_exit_ms": r["close_main_ms"], "last_phase_line": fields.get("last_phase_line"),
                                                     "last_phase_line_ms": fields.get("last_phase_line_ms"),
                                                     "pending_blockers": fields.get("pending_blockers"), "tail": tail}), flush=True)
        mine = [x for x in bg_all if x["attrib"] == b.tag or (x["attrib"] is None and n == 1)]
        bgs = bg_report(mine, b.t_close, "retail", rnd, b.tag) if b.t_close else []
        r["bg_n"] = len(bgs)
        print("HUNTROW " + json.dumps(r), flush=True)
        print_shut("retail", rnd, b.tag, shown)
        print_bg(bgs)
        if not b.log_bytes:
            print("HUNTSHUT " + json.dumps({"arm": "retail", "round": rnd, "tag": b.tag, "os": sys.platform, "t_ms": None,
                                            "line": "(the browser wrote nothing to stdout/stderr)"}), flush=True)
    stray = [x for x in bg_all if x["attrib"] is None and n > 1]
    if stray:
        print_bg(bg_report(stray, min((b.t_close for b in browsers if b.t_close), default=t0 * 1000), "retail", rnd, "unattributed"))
    httpd.shutdown()
    shutil.rmtree(work, ignore_errors=True)


def exitprobe_inner(binary, rnd):
    """HUNT: launch one browser, raise the hunt's own simulated error BEFORE close(), and let the interpreter exit. A daemon thread
    prints what is alive 6 s after the raise (if the interpreter is still there)."""
    from invisible_playwright.async_api import InvisiblePlaywright

    async def go():
        cm = InvisiblePlaywright(seed=42, binary_path=binary, humanize=True, headless=True)
        browser = await cm.__aenter__()
        await browser.new_page()
        OPEN_BROWSERS["probe"] = "open"
        t = threading.Timer(6.0, print_alive, args=("6s-after-the-raise",))
        t.daemon = True
        t.start()
        print("HUNTEXITPROBE raised", flush=True)
        raise RuntimeError("simulated hunt error before close()")
    ARM_CTX.update({"arm": "exitprobe", "round": rnd})
    asyncio.run(go())


def exitprobe_outer(rnd, binary, label):
    """Runs exitprobe_inner as a child and times the interpreter's exit from the raise; 55 s at most, then its tree is killed."""
    t_start = time.time() * 1000
    p = subprocess.Popen([sys.executable, __file__, "exitprobe-inner", binary, rnd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1, env={**os.environ, "HUNT_LABEL": label})
    raised = []

    def pump():
        for line in p.stdout:
            line = line.rstrip()
            if line.startswith("HUNTEXITPROBE raised"):
                raised.append(time.time() * 1000)
            if line.startswith("HUNT"):
                print(line.replace("\"arm\": \"exitprobe\"", "\"arm\": \"exitprobe\", \"label\": \"%s\"" % label, 1), flush=True)
    th = threading.Thread(target=pump, daemon=True)
    th.start()
    timed_out = False
    try:
        rc = p.wait(timeout=55)
    except subprocess.TimeoutExpired:
        timed_out, rc = True, None
    t_exit = time.time() * 1000
    left = []
    for q in psutil.process_iter(["name"]):
        try:
            if "firefox" in (q.info["name"] or "").lower() and q.create_time() * 1000 >= t_start:
                left.append(q.pid)
        except psutil.Error:
            pass
    def ff_left(since):
        found = {}
        for q in psutil.process_iter(["name"]):
            try:
                if "firefox" in (q.info["name"] or "").lower() and q.create_time() * 1000 >= since:
                    found[q.pid] = proc_kind(q)
            except psutil.Error:
                pass
        return found
    kinds_1 = kinds_10 = None
    if not timed_out:
        time.sleep(max(0.0, t_exit + 1000 - time.time() * 1000) / 1000)
        kinds_1 = ff_left(t_start)
        time.sleep(max(0.0, t_exit + 10000 - time.time() * 1000) / 1000)
        kinds_10 = ff_left(t_start)
    if timed_out or kinds_10:
        victims = [psutil.Process(pid) for pid in (kinds_10 or {})]
        if timed_out:
            victims += psutil.Process(p.pid).children(recursive=True) + [psutil.Process(p.pid)]
        for q in victims:
            try:
                q.kill()
            except psutil.Error:
                pass
    th.join(3)
    print("HUNTROW " + json.dumps({"arm": "exitprobe", "label": label, "round": rnd, "os": sys.platform, "returncode": rc, "timed_out": timed_out,
                                   "raised": bool(raised), "exit_after_raise_ms": round(t_exit - raised[0], 1) if raised else None,
                                   "firefox_left_after_exit": len(left), "ff_left_1s": len(kinds_1) if kinds_1 is not None else None,
                                   "ff_left_10s": len(kinds_10) if kinds_10 is not None else None,
                                   "ff_left_kinds_1s": sorted(kinds_1.values()) if kinds_1 else [],
                                   "ff_left_kinds_10s": sorted(kinds_10.values()) if kinds_10 else []}), flush=True)


if __name__ == "__main__":
    # The workflow's per-arm limit kills this process's tree by the pid written here (several processes of an arm append their own).
    if os.environ.get("HUNT_PIDFILE"):
        with open(os.environ["HUNT_PIDFILE"], "a", encoding="utf-8", newline="\n") as _pf:
            _pf.write("%d\n" % os.getpid())
    ARM_CTX.update({"arm": os.environ.get("HUNT_ARM"), "round": sys.argv[3] if len(sys.argv) > 3 else None})
    if sys.argv[1] == "exitprobe-outer":
        exitprobe_outer(sys.argv[2], sys.argv[3], sys.argv[4])
        sys.exit(0)
    if sys.argv[1] == "exitprobe-inner":
        exitprobe_inner(sys.argv[2], sys.argv[3])
        sys.exit(0)
    start_alive_watchdog()
    if sys.argv[1] == "retail":
        retail_main(int(sys.argv[2]), sys.argv[3], sys.argv[4], float(sys.argv[5]) if len(sys.argv) > 5 else 20.0)
        sys.exit(0)
    if sys.argv[1] == "serve":
        serve_one(sys.argv[2])
        sys.exit(0)
    asyncio.run(main(sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None))
    # The interpreter now waits for the non-daemon threads (the core's reaper is one): if it is still here in 5 s, say what holds it.
    _late = threading.Timer(5.0, print_alive, args=("5s-after-main-returned", False))
    _late.daemon = True
    _late.start()
