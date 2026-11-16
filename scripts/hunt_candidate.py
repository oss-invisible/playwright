"""HUNT ONLY, never merged. The driver of the three candidate hunts (mouse 1b, the close of 4 browsers together, IndexedDB at quit).

It runs `hunt_published_latency.py` (imported, never edited, so the workflow that watches that file does not start) with the
probes of these hunts put around it. Nothing here is inside a test and nothing changes what the product does: every wrapper calls
the real function and only records.

    python hunt_candidate.py ARM N_BROWSERS ROUND [binary]                       (the arguments of hunt_published_latency.py)
    python hunt_candidate.py retail N ROUND FIREFOX_BINARY [close_after_s]

Switches (environment):
  HUNT_CAND_MOUSE=1   the mouse the page saw against the plan the client sent (round 1, criterion 1b). The plan is every
                      Page.dispatchMouseEvent the core's client sends, with its `stroke` and `at`, recorded at the three places a mouse
                      event leaves: `Stroke.send` (a path, a click: each carries `stroke` and `at`), `Actions._mouse_event` outside a
                      stroke (an explicit move, press or release: no stroke, no `at`), and `Actions.walk` (the planned instants handed
                      to the pacer, and how many were delivered); and the pause `Actions._settle_before_press` draws and sleeps.
                      The page's side is what hunt_published_latency.py already records: every mousemove, mousedown, mouseup with
                      event.timeStamp (and the handler's clock). The two are joined by the identity of the browser's core connection.
  HUNT_CAND_CLOSE=1   the close of the core as it is (round 2): the steps stamped as before, NOTHING raised (no deadline of the hunt:
                      `Connection.close` gets what the caller gave, the reaper what it was built with), what the core ended
                      (terminate / kill / job end), and the exit code and the exit instant of every process of each browser's tree.
  HUNT_CAND_IDB=1     (round 3) MOZ_LOG=IndexedDB:5,sync,timestamp and MOZ_LOG_FILE=<dir>/ff-%PID.log for ours and for retail, the
                      census of every process of each browser at its close (command line, process type, birth).
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hunt_published_latency as H  # noqa: E402  (the scripts directory is first on the path when this file is run)

import psutil  # noqa: E402

_TL = threading.local()

# ---------------------------------------------------------------------------------------------------------------------------------
# Round 1: the plan the client sent
# ---------------------------------------------------------------------------------------------------------------------------------
#: One dict per Page.dispatchMouseEvent: conn (id of the core connection), sid / p_stroke / p_at (what went on the wire), at, before,
#: after (client wall ms: before the command left, after send_async returned), type, x, y, via ("stroke" | "single").
M_PLAN = []
#: One dict per Actions.walk: conn, start, end (wall ms), planned (events handed in), delivered (returned), t_ms (their instants).
M_WALKS = []
#: One dict per press settle: conn, start, end (wall ms), pause_ms (what plan_press_settle drew).
M_SETTLES = []


def install_mouse_probe():
    from invisible_core.juggler import _behaviour, _stroke, actions
    real_send = _stroke.Stroke.send
    if not getattr(real_send, "_m1b", False):
        def send(self, method, params, at_ms, *a, **k):
            if method != "Page.dispatchMouseEvent":
                return real_send(self, method, params, at_ms, *a, **k)
            if not k.get("paced"):
                self.hold_until(at_ms)            # the wait for its instant, so `before` is the instant the command leaves
            before = time.time() * 1000
            try:
                return real_send(self, method, params, at_ms, *a, **k)
            finally:
                M_PLAN.append({"conn": id(self._c), "sid": self.id, "type": params.get("type"), "x": params.get("x"), "y": params.get("y"),
                               "at": float(at_ms or 0.0), "p_stroke": params.get("stroke"), "p_at": params.get("at"),
                               "buttons": params.get("buttons"), "click": params.get("clickCount"),
                               "before": before, "after": time.time() * 1000, "via": "stroke"})
        send._m1b = True
        _stroke.Stroke.send = send
    real_event = actions.Actions._mouse_event
    if not getattr(real_event, "_m1b", False):
        def _mouse_event(self, event_type, point, *a, **k):
            if self._stroke is not None:
                return real_event(self, event_type, point, *a, **k)      # leaves through Stroke.send: recorded there
            before = time.time() * 1000
            try:
                return real_event(self, event_type, point, *a, **k)
            finally:
                M_PLAN.append({"conn": id(self.c), "sid": None, "type": event_type, "x": point[0], "y": point[1], "at": None,
                               "p_stroke": None, "p_at": None, "buttons": k.get("buttons"), "click": k.get("click_count"),
                               "before": before, "after": time.time() * 1000, "via": "single"})
        _mouse_event._m1b = True
        actions.Actions._mouse_event = _mouse_event
    real_walk = actions.Actions.walk
    if not getattr(real_walk, "_m1b", False):
        def walk(self, events, *a, **k):
            evs = list(events)
            start, delivered = time.time() * 1000, None
            try:
                delivered = real_walk(self, evs, *a, **k)
                return delivered
            finally:
                M_WALKS.append({"conn": id(self.c), "start": start, "end": time.time() * 1000, "planned": len(evs), "delivered": delivered,
                                "t_ms": [round(float(getattr(e, "t_ms", 0.0)), 1) for e in evs[:400]]})
        walk._m1b = True
        actions.Actions.walk = walk
    real_plan = _behaviour.plan_press_settle
    if not getattr(real_plan, "_m1b", False):
        def plan_press_settle(*a, **k):
            v = real_plan(*a, **k)
            _TL.pause = v
            return v
        plan_press_settle._m1b = True
        _behaviour.plan_press_settle = plan_press_settle
    real_settle = actions.Actions._settle_before_press
    if not getattr(real_settle, "_m1b", False):
        def _settle_before_press(self):
            _TL.pause = None
            start = time.time() * 1000
            try:
                return real_settle(self)
            finally:
                M_SETTLES.append({"conn": id(self.c), "start": start, "end": time.time() * 1000, "pause_ms": getattr(_TL, "pause", None)})
        _settle_before_press._m1b = True
        actions.Actions._settle_before_press = _settle_before_press


def _instants(plan):
    """The planned wall instant of each event: its stroke's start on the client's clock plus its `at`. A stroke's start is the smallest
    `before - at` of its events (an event that left on time gives it exactly, a late one only an upper bound); an event outside a stroke
    has no `at`, it is planned when it is sent."""
    t0 = {}
    for e in plan:
        if e["sid"] is not None and e["at"] is not None:
            d = e["before"] - e["at"]
            t0[e["sid"]] = min(t0.get(e["sid"], d), d)
    return [(t0[e["sid"]] + e["at"]) if e["sid"] is not None and e["at"] is not None else e["before"] for e in plan]


def match_moves(pages, moves):
    """Join each page mousemove to the plan move it is. `pages` = [(page clock, clientX, clientY)], `moves` = [(planned instant, event)].
    By COORDINATES when the page reported them (the hunt's page does): in order, each page move to the plan move of the forward window
    that is nearest in space (within 3 px), ties inside a pixel to the one nearest in time after the clock shift; this does not let a
    late event pass for an on-time one, which a join by nearest time does (a move delayed by 90 ms is nearest to the plan point 90 ms
    later). Without coordinates, or when fewer than 80% join: by nearest time. Returns (the page clock's shift onto the plan's, the
    plan index of each page move or None, how)."""
    import bisect
    import math
    pm = [t for t, _e in moves]
    times = [t for t, _x, _y in pages]
    sh = H._align(times, pm)

    def by_time(shift):
        out = []
        for t in times:
            x = t - shift
            k = bisect.bisect_left(pm, x)
            out.append(min((j for j in (k - 1, k) if 0 <= j < len(pm)), key=lambda j: abs(x - pm[j])))
        return out
    def place(shift):
        js, j0 = [], 0
        for t, x, y in pages:
            win = range(j0, min(len(pm), j0 + 60))
            dist = {j: math.hypot(x - moves[j][1]["x"], y - moves[j][1]["y"]) for j in win}
            if not dist or min(dist.values()) > 3.0:
                js.append(None)
                continue
            best = min(dist.values())
            j = min((j for j in win if dist[j] <= best + 0.75), key=lambda j: abs((t - shift) - pm[j]))
            js.append(j)
            j0 = j + 1
        return js

    def offsets(js):
        return [t - pm[j] for t, j in zip(times, js) if j is not None]
    if all(x is not None and y is not None for _t, x, y in pages):
        js = place(sh)
        ok = offsets(js)
        if len(ok) >= 0.8 * len(pages):
            sh2 = sorted(ok)[len(ok) // 2]
            js2 = place(sh2)
            if len(offsets(js2)) >= 0.8 * len(pages):
                return sh2, js2, "xy"
    return sh, by_time(sh), "time"


def m1b_analyse(ev, plan, settles, i, prefix):
    """The page's mousemove / mousedown / mouseup (`ev`: [type, timeStamp clock, handler clock], `i` picks the clock) against the plan
    the client sent (`plan`: its mouse events in the order sent, `settles`: the presses' settle pauses).

    The page does not see every plan point (the frame coalesces mousemoves), and its clock is not the client's: each page move is
    joined to the plan move it is (by its coordinates, `match_moves`) and the page's clock is shifted onto the plan's by the median
    offset of those joins. Then, over the path up to the first press:
      a  pauses   page gap between two consecutive page moves minus the planned gap between their plan points: over 50 ms (and 100);
      b  bursts   two consecutive page moves less than 2 ms apart whose plan points are 8 ms or more apart;
      c  settle   last move -> mousedown: the pause the core drew (plan_press_settle) and slept, the client's own interval (last move
                  sent -> press sent), the page's interval (last page move -> mousedown, and from the page move joined to the last
                  plan move when the page saw it)."""
    out = {}
    inst = _instants(plan)
    moves = sorted(((t, e) for t, e in zip(inst, plan) if e["type"] == "mousemove"), key=lambda r: r[0])
    downs = sorted(((t, e) for t, e in zip(inst, plan) if e["type"] == "mousedown"), key=lambda r: r[0])
    ups = sorted(((t, e) for t, e in zip(inst, plan) if e["type"] == "mouseup"), key=lambda r: r[0])
    pm = [t for t, _e in moves]
    if downs:
        keep = [(t, e) for t, e in moves if t <= downs[0][0] + 1]
        moves, pm = keep, [t for t, _e in keep]
    pdown = sorted(x[i] for x in ev if x[0] == "mousedown")
    pup = sorted(x[i] for x in ev if x[0] == "mouseup")
    first_down = pdown[0] if pdown else None
    pages = sorted(((x[i], x[3] if len(x) > 4 else None, x[4] if len(x) > 4 else None) for x in ev
                    if x[0] == "mousemove" and (first_down is None or x[i] <= first_down)), key=lambda r: r[0])
    out[prefix + "plan_moves"] = len(pm)
    out[prefix + "page_moves"] = len(pages)
    if len(pages) < 3 or len(pm) < 3:
        out[prefix + "note"] = "too few: page %d, plan %d" % (len(pages), len(pm))
        return out
    sh, js, how = match_moves(pages, moves)
    out[prefix + "match"] = how
    out[prefix + "unmatched"] = sum(1 for j in js if j is None)
    page = [t for (t, _x, _y), j in zip(pages, js) if j is not None]
    js = [j for j in js if j is not None]
    if len(page) < 3:
        out[prefix + "note"] = "too few matched: page %d of %d" % (len(page), len(pages))
        return out
    pg = [b - a for a, b in zip(page, page[1:])]
    plg = [pm[js[k + 1]] - pm[js[k]] for k in range(len(pg))]
    excess = [a - b for a, b in zip(pg, plg)]
    order = sorted(range(len(pg)), key=lambda k: -excess[k])[:3]
    out[prefix + "shift_ms"] = round(sh, 1)
    out[prefix + "pairs"] = len(pg)
    out[prefix + "pauses50"] = sum(1 for e in excess if e > 50)
    out[prefix + "pauses100"] = sum(1 for e in excess if e > 100)
    out[prefix + "pause_max"] = round(max(excess), 1)
    out[prefix + "pause_worst"] = [[round(pg[k], 1), round(plg[k], 1), k] for k in order]
    out[prefix + "pairs8"] = sum(1 for b in plg if b >= 8)
    out[prefix + "bursts"] = sum(1 for a, b in zip(pg, plg) if a < 2 and b >= 8)
    out[prefix + "burst_examples"] = [[round(a, 2), round(b, 1), k] for k, (a, b) in enumerate(zip(pg, plg)) if a < 2 and b >= 8][:4]
    out[prefix + "plan_pairs8"] = sum(1 for a, b in zip(pm, pm[1:]) if b - a >= 8)
    out[prefix + "plan_max_gap"] = round(max(b - a for a, b in zip(pm, pm[1:])), 1)
    if first_down is not None and downs:
        d_t, d_e = downs[0]
        last_e = moves[-1][1]
        drawn = [s for s in settles if s["end"] <= d_e["before"] + 2.0]
        s = drawn[-1] if drawn else None
        out[prefix + "settle_drawn"] = round(s["pause_ms"], 1) if s and s["pause_ms"] is not None else None
        out[prefix + "settle_slept"] = round(s["end"] - s["start"], 1) if s else None
        out[prefix + "settle_client"] = round(d_e["before"] - last_e["before"], 1)
        out[prefix + "settle_page"] = round(first_down - page[-1], 1)
        seen_last = [k for k, j in enumerate(js) if j == len(pm) - 1]
        out[prefix + "settle_page_from_last_planned"] = round(first_down - page[seen_last[-1]], 1) if seen_last else None
        out[prefix + "last_page_move_plan_moves_after"] = len(pm) - 1 - js[-1]
        out[prefix + "press_offset"] = round((first_down - sh) - d_t, 1)
        if ups and pup:
            out[prefix + "dwell_plan"] = round(ups[0][0] - d_t, 1)
            out[prefix + "dwell_page"] = round(pup[0] - first_down, 1) if pup[0] >= first_down else None
    return out


ORIG_MOUSE_REPORT = H.mouse_report


def mouse_report(ev, dwell_plan, settle_plan=None, conn_id=None):
    out = ORIG_MOUSE_REPORT(ev, dwell_plan, settle_plan, conn_id)
    plan = [e for e in M_PLAN if conn_id is not None and e["conn"] == conn_id]
    walks = [w for w in M_WALKS if conn_id is not None and w["conn"] == conn_id]
    settles = [s for s in M_SETTLES if conn_id is not None and s["conn"] == conn_id]
    out["m1b_pairing"] = "exact" if plan else "unpaired"
    out["m1b_plan_events"] = len(plan)
    out["m1b_plan_tagged"] = sum(1 for e in plan if e["p_stroke"] is not None)
    out["m1b_plan_single"] = sum(1 for e in plan if e["via"] == "single")
    out["m1b_walks"] = [[w["planned"], w["delivered"]] for w in walks]
    if not plan:
        return out
    for prefix, i in (("m1b_", 1), ("m1b_h_", 2)):
        out.update(m1b_analyse(ev, plan, settles, i, prefix))
    out["m1b_raw"] = {
        "plan": [[e["sid"], e["type"], e["x"], e["y"], e["p_at"], round(e["before"], 1), round(e["after"], 1), e["via"]] for e in plan],
        "page": [[x[0], round(x[1], 1), round(x[2], 1)] + list(x[3:5]) for x in ev if x[0] in ("mousemove", "mousedown", "mouseup", "click")],
        "walks": walks, "settles": settles}
    return out


# ---------------------------------------------------------------------------------------------------------------------------------
# Round 2: the close as the product does it, only stamped
# ---------------------------------------------------------------------------------------------------------------------------------
#: id(CloseRec) -> what the core did in this close (see x_fields); id(EngineTree) -> its CloseRec.
X = {}
TREE_REC = {}


def _new_x():
    return {"end_calls": [], "conn_terminate": [], "wbg": []}


def _describe(proc):
    """A process the core is about to end: its pid, its type by command line, and whether it is one the browser starts to outlive
    itself (the shutdown-ping sender or its background task: `process.outlives_the_browser`, by command line). sender None = the command
    line could not be read (the process had gone)."""
    from invisible_core import process as pmod
    try:
        cmd = proc.cmdline()
    except Exception:   # noqa: BLE001
        cmd = None
    if not cmd:
        return {"pid": getattr(proc, "pid", None), "kind": "?", "sender": None}
    kind = "ff:" + cmd[-1] if "-contentproc" in cmd else ("ff:backgroundtask" if pmod.outlives_the_browser(cmd) else "ff:parent")
    return {"pid": proc.pid, "kind": kind, "sender": bool(pmod.outlives_the_browser(cmd))}


def install_clean_close_stamps():
    """The steps of a close of the core, stamped, WITHOUT changing what it does. Replaces the hunt's `install_close_stamps`, whose
    wrappers raised the deadlines (`HUNT_CLOSE_TIMEOUT`: Connection.close's timeout and the Reaper's): none is raised here, and the
    hunt refuses to start with that variable set. Wrapped, each calling the real function:
      Connection.__init__ / close, the `os` of connection.py (close of the write end, the empty read), Reaper.run  (the steps, as before)
      Connection.close      the arguments it received and the limit it ends up with (HUNTCLOSEARGS)
      EngineTree.end / _wait / wait_browser_gone, EngineJob.terminate, the launcher's terminate / kill, psutil.Process.terminate / kill
                            inside end: what the core ended, when, and which of it is a sender"""
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
            c = H.FD_WRITE.pop(fd, None)
            if c is not None:
                r = H.CONN_REC.get(id(c))
                if r is not None and r.t_pipe is None:
                    r.t_pipe = pc()
            else:
                c = H.FD_READ.pop(fd, None)
                if c is not None:
                    r = H.CONN_REC.get(id(c))
                    if r is not None and r.t_reader_end is None:
                        r.t_reader_end = pc()
            return real_close(fd)

        def read(fd, n):
            data = real_read(fd, n)
            if not data:
                c = H.FD_READ.get(fd)
                if c is not None:
                    r = H.CONN_REC.get(id(c))
                    if r is not None and r.t_eof is None:
                        r.t_eof = pc()
            return data
        proxy.close, proxy.read = close, read
        cmod.os = proxy
        cmod._hunt_os = True
    real_init = cmod.Connection.__init__
    if not getattr(real_init, "_hunt_ncs", False):
        def __init__(self, *a, **k):
            to_b = k.get("to_browser", a[0] if a else None)
            from_b = k.get("from_browser", a[1] if len(a) > 1 else None)
            H.FD_WRITE[to_b], H.FD_READ[from_b] = self, self       # before the reader thread starts
            H.CONNS.append(self)
            real_init(self, *a, **k)
        __init__._hunt_ncs = True
        cmod.Connection.__init__ = __init__
    real_close_m = cmod.Connection.close
    if not getattr(real_close_m, "_hunt_ncs", False):
        def close_m(self, *a, **k):
            rec = H.CONN_REC.get(id(self))
            mine = rec is not None and rec.t_entry is None
            if mine:
                rec.t_entry = pc()
                X.setdefault(id(rec), _new_x())
                tree = getattr(getattr(self, "_process", None), "tree", None)
                if tree is not None:
                    TREE_REC[id(tree)] = rec
                _TL.rec = rec
            effective = a[0] if a else k.get("timeout")
            print("HUNTCLOSEARGS " + json.dumps({**H.ARM_CTX, "what": "Connection.close", "conn": id(self) % 100000, "mine": mine,
                                                 "asked": [list(a), dict(k)], "effective_timeout": effective,
                                                 "limit_when_none": getattr(pmod, "BROWSER_EXIT_LIMIT", None)}), flush=True)
            try:
                return real_close_m(self, *a, **k)
            finally:
                if mine:
                    rec.t_conn_return = pc()
                    _TL.rec = None
        close_m._hunt_ncs = True
        cmod.Connection.close = close_m
    real_run = pmod.Reaper.run
    if not getattr(real_run, "_hunt_ncs", False):
        def run(self):
            try:
                return real_run(self)
            finally:
                self.t_done = pc()
        run._hunt_ncs = True
        pmod.Reaper.run = run
    real_rinit = pmod.Reaper.__init__
    if not getattr(real_rinit, "_hunt_ncs", False):
        def r_init(self, *a, **k):
            real_rinit(self, *a, **k)
            print("HUNTCLOSEARGS " + json.dumps({**H.ARM_CTX, "what": "Reaper.__init__", "grace": self.grace, "timeout": self.timeout}), flush=True)
        r_init._hunt_ncs = True
        pmod.Reaper.__init__ = r_init
    real_end = pmod.EngineTree.end
    if not getattr(real_end, "_hunt_ncs", False):
        def end(self, *a, **k):
            ctx = {"t0": pc(), "grace": k.get("grace", a[0] if a else 0.0), "timeout": k.get("timeout", a[1] if len(a) > 1 else 5.0),
                   "waits": [], "job_terminate": [], "kills": []}
            _TL.end = (self, ctx)
            try:
                ctx["sent"] = real_end(self, *a, **k)
                return ctx["sent"]
            finally:
                _TL.end = None
                ctx["t1"] = pc()
                rec = TREE_REC.get(id(self))
                if rec is not None:
                    X.setdefault(id(rec), _new_x())["end_calls"].append(ctx)
        end._hunt_ncs = True
        pmod.EngineTree.end = end
    real_wait = pmod.EngineTree._wait
    if not getattr(real_wait, "_hunt_ncs", False):
        def _wait(self, procs, timeout):
            left = real_wait(self, procs, timeout)
            cur = getattr(_TL, "end", None)
            if cur is not None and cur[0] is self:
                cur[1]["waits"].append({"timeout": timeout, "in": len(procs), "t": pc(), "left": [_describe(p) for p in left]})
            return left
        _wait._hunt_ncs = True
        pmod.EngineTree._wait = _wait
    real_wbg = pmod.EngineTree.wait_browser_gone
    if not getattr(real_wbg, "_hunt_ncs", False):
        def wait_browser_gone(self, deadline):
            t = pc()
            ret = real_wbg(self, deadline)
            rec = TREE_REC.get(id(self))
            if rec is not None:
                X.setdefault(id(rec), _new_x())["wbg"].append({"ret": ret, "t0": t, "t1": pc(), "deadline_in_s": round(deadline - time.monotonic(), 2)})
            return ret
        wait_browser_gone._hunt_ncs = True
        pmod.EngineTree.wait_browser_gone = wait_browser_gone
    real_jt = pmod.EngineJob.terminate
    if not getattr(real_jt, "_hunt_ncs", False):
        def job_terminate(self):
            cur = getattr(_TL, "end", None)
            if cur is not None:
                cur[1]["job_terminate"].append(pc())
            return real_jt(self)
        job_terminate._hunt_ncs = True
        pmod.EngineJob.terminate = job_terminate

    def wrap_proc(cls, name, what):
        real = getattr(cls, name, None)
        if real is None or getattr(real, "_hunt_ncs", False):
            return

        def call(self, *a, **k):
            rec = getattr(_TL, "rec", None)
            cur = getattr(_TL, "end", None)
            if rec is not None:
                X.setdefault(id(rec), _new_x())["conn_terminate"].append({"what": what, "t": pc(), "pid": getattr(self, "pid", None)})
            elif cur is not None:
                cur[1]["kills"].append({"what": what, "t": pc(), "pid": getattr(self, "pid", None)})
            return real(self, *a, **k)
        call._hunt_ncs = True
        setattr(cls, name, call)
    for cls, what in ((getattr(cmod, "_WindowsProcess", None), "launcher"), (subprocess.Popen, "popen")):
        if cls is not None:
            wrap_proc(cls, "terminate", what + ".terminate")
            wrap_proc(cls, "kill", what + ".kill")
    wrap_proc(psutil.Process, "terminate", "psutil.terminate")
    wrap_proc(psutil.Process, "kill", "psutil.kill")
    real_fields = H.CloseRec.fields
    if not getattr(real_fields, "_hunt_ncs", False):
        def fields(self):
            d = real_fields(self)
            try:
                d.update(x_fields(self))
            except Exception as exc:   # noqa: BLE001
                d["x_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            return d
        fields._hunt_ncs = True
        H.CloseRec.fields = fields


def x_fields(rec):
    """What the core did in the close of `rec`, as row fields: the ends it made (what was left when it ended, which of it was a sender),
    the launcher's terminate, the waits for the browser's exit and their answers."""
    x = X.get(id(rec)) or _new_x()

    def ms(t):
        return round((t - rec.t_call) * 1000, 1) if rec.t_call is not None else None
    ends, left_nonsender, left_sender, left_unknown = [], set(), set(), set()
    for c in x["end_calls"]:
        waits = []
        for w in c["waits"]:
            waits.append({"timeout": w["timeout"], "in": w["in"], "after_ms": ms(w["t"]), "left": w["left"]})
            for p in w["left"]:
                (left_sender if p["sender"] else left_nonsender if p["sender"] is False else left_unknown).add((p["pid"], p["kind"]))
        ends.append({"after_ms": ms(c["t0"]), "dur_ms": round((c["t1"] - c["t0"]) * 1000, 1), "grace": c["grace"], "timeout": c["timeout"],
                     "sent": c.get("sent"), "waits": waits, "job_terminate_after_ms": [ms(t) for t in c["job_terminate"]],
                     "kills": [{"what": k["what"], "pid": k["pid"], "after_ms": ms(k["t"])} for k in c["kills"]]})
    conn_term = [{"what": c["what"], "pid": c["pid"], "after_ms": ms(c["t"])} for c in x["conn_terminate"]]
    wbg = [{"ret": w["ret"], "after_ms": ms(w["t0"]), "dur_ms": round((w["t1"] - w["t0"]) * 1000, 1)} for w in x["wbg"]]
    return {"x_wall_call": round(rec.wall_call, 1) if rec.wall_call else None, "x_end_calls": ends, "x_conn_terminate": conn_term,
            "x_wait_browser_gone": wbg,
            "x_browser_ended": bool(left_nonsender or conn_term or any(w["ret"] is False for w in wbg)),
            "x_left_nonsender": sorted(left_nonsender), "x_left_sender": sorted(left_sender), "x_left_unknown": sorted(left_unknown)}


class TreeWatch2(H.TreeWatch):
    """The hunt's TreeWatch, and for every process of the tree as it was just before the close: its exit code and the instant it was
    found gone (a thread each; psutil gives the code of any process on Windows), and its census (command line, type, parent, birth)."""

    def __init__(self, main_pid):
        self.census = []
        self.exits_all = {}
        super().__init__(main_pid)
        # The process the core spawned may be gone (a launcher that handed over): the tree is then the core's own list of it (the launch's
        # job on Windows), the same one `Connection.close` waits on.
        try:
            rec = next((r for r in H.CONN_REC.values() if r.pid == main_pid), None)
            tree = getattr(getattr(getattr(rec, "conn", None), "_process", None), "tree", None)
            for p in (tree.members() if tree is not None else []):
                if p.pid not in self.procs:
                    self._add(p)
        except Exception as exc:   # noqa: BLE001
            self.census_error = "%s: %s" % (type(exc).__name__, str(exc)[:160])
        # The waits start now, BEFORE the close is asked for: a wait holds the process's handle, so the exit code of a process that is gone
        # a moment later is still read (a process looked up after its exit has no code to give).
        for pid, v in list(self.procs.items()):
            threading.Thread(target=self._wait_one, args=(pid, v[0], v[1]), daemon=True, name="hunt-exitwait").start()
        self.t_ready = H.now_ms()

    def _add(self, p):
        known = p.pid in self.procs
        super()._add(p)
        if known or p.pid not in self.procs:
            return
        try:
            cmd = p.cmdline()
            self.census.append({"pid": p.pid, "ppid": p.ppid(), "name": p.name(), "kind": self.procs[p.pid][1], "cmd": " ".join(cmd)[:400],
                                "created_ms": round(p.create_time() * 1000), "late": getattr(self, "t_ready", None) is not None})
        except psutil.Error:
            pass

    def _wait_one(self, pid, p, kind):
        try:
            rc = p.wait(timeout=125)
        except psutil.TimeoutExpired:
            rc = "timeout"
        except psutil.Error:
            rc = None
        self.exits_all[pid] = {"kind": kind, "rc": rc, "t": H.now_ms()}

    def fields(self):
        d = super().fields()
        d["x_t_ready"] = round(self.t_ready, 1)
        d["x_exits"] = [[pid, v["kind"], v["rc"], round(v["t"] - self.t_snap, 1)] for pid, v in sorted(self.exits_all.items(), key=lambda kv: kv[1]["t"])]
        d["x_census"] = self.census
        d["x_census_error"] = getattr(self, "census_error", None)
        return d


# ---------------------------------------------------------------------------------------------------------------------------------
# Round 3: MOZ_LOG for IndexedDB, and the census
# ---------------------------------------------------------------------------------------------------------------------------------
def install_moz_log():
    d = os.path.normpath(os.environ.get("HUNT_MOZLOG_DIR") or os.path.join(os.environ.get("RUNNER_TEMP") or os.getcwd(), "mozlog"))
    os.makedirs(d, exist_ok=True)
    # The engine and the retail Firefox both inherit this process's environment (the core lets MOZ_LOG and MOZ_LOG_FILE through:
    # environment.MOZ_DIAGNOSTICS). %PID is Firefox's own: one file per process, named by its pid (the parent's and each child's).
    os.environ["MOZ_LOG"] = "IndexedDB:5,sync,timestamp"
    os.environ["MOZ_LOG_FILE"] = os.path.join(d, "ff-%PID.log")
    print("HUNTCLOSEARGS " + json.dumps({**H.ARM_CTX, "what": "moz_log", "MOZ_LOG": os.environ["MOZ_LOG"], "MOZ_LOG_FILE": os.environ["MOZ_LOG_FILE"]}), flush=True)


# ---------------------------------------------------------------------------------------------------------------------------------
def main():
    if os.environ.get("HUNT_PIDFILE"):
        with open(os.environ["HUNT_PIDFILE"], "a", encoding="utf-8", newline="\n") as f:
            f.write("%d\n" % os.getpid())
    H.ARM_CTX.update({"arm": os.environ.get("HUNT_ARM"), "round": sys.argv[3] if len(sys.argv) > 3 else None})
    if os.environ.get("HUNT_CAND_MOUSE") == "1":
        # The page of the hunt (its own, served by the hunt's server) also reports where each mouse event was: clientX, clientY. The plan's
        # points carry coordinates, so a page event is joined to the point it is, not to the point nearest in time.
        before = H.TYPE_PAGE
        H.TYPE_PAGE = before.replace(b"__ev.push([t,performance.timeOrigin+e.timeStamp,T()])",
                                     b"__ev.push([t,performance.timeOrigin+e.timeStamp,T(),e.clientX,e.clientY])")
        if H.TYPE_PAGE == before:
            sys.exit("the hunt's page changed: its event list no longer matches")
        real_key_probe = H.install_key_probe

        def install_key_probe():
            real_key_probe()
            install_mouse_probe()
        H.install_key_probe = install_key_probe
        H.mouse_report = mouse_report
    if os.environ.get("HUNT_CAND_CLOSE") == "1":
        if os.environ.get("HUNT_CLOSE_TIMEOUT"):
            sys.exit("HUNT_CLOSE_TIMEOUT is set: this hunt raises no deadline")
        H.install_close_stamps = install_clean_close_stamps
        H.TreeWatch = TreeWatch2
    if os.environ.get("HUNT_CAND_IDB") == "1":
        install_moz_log()
    H.start_alive_watchdog()
    if sys.argv[1] == "retail":
        H.retail_main(int(sys.argv[2]), sys.argv[3], sys.argv[4], float(sys.argv[5]) if len(sys.argv) > 5 else 20.0)
        sys.exit(0)
    asyncio.run(H.main(sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None))
    # The interpreter now waits for the non-daemon threads (the core's reaper is one): if it is still here in 5 s, say what holds it.
    late = threading.Timer(5.0, H.print_alive, args=("5s-after-main-returned", False))
    late.daemon = True
    late.start()


if __name__ == "__main__":
    main()
