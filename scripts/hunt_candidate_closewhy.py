"""HUNT ONLY, never merged. Why does the wrapper's Browser.close() return AFTER the core's Connection.close has, when 4 browsers of one
Python process close together on Windows? Probes only: nothing here changes what the product does (every wrapper calls the real function,
records, and returns what it returned), and nothing is raised (the hunt refuses to start with HUNT_CLOSE_TIMEOUT set).

    python hunt_candidate_closewhy.py run inproc N ROUND BINARY     one round: N browsers in this process, closed when the session is 75 s old
                                                                     (env: HUNT_NCLOSE=1 HUNT_NCSHUT=1 HUNT_SESSION_S=75 like cand4close,
                                                                      HUNT_CW_MODE=together|sequential, HUNT_CW_DIR=<where the raw files go>)
    python hunt_candidate_closewhy.py report DIR [ROWS_LOG]         the table of one job (and DIR/readable/: report, stamps, samples)
    python hunt_candidate_closewhy.py compare DIR                   together against sequential; DIR holds both jobs' files at any depth
    python hunt_candidate_closewhy.py selftest                      synthetic data through the report and the compare

It imports `hunt_published_latency.py` and `hunt_candidate.py` (never edited, so the workflows that watch them do not start) and puts its
own probes around the close path of the wrapper (30ee9e59, release/0.53.0) and of the core (8c5e9dbd, fix/stroke-on-own-clock, with #287):

  1. STAMPS. Every step of the close, with `time.perf_counter_ns` (one clock for the process), a wall anchor (wall = anchor + delta) and the
     name of the thread that took it: the client's Browser.close (public and impl), the message it sends, the transport's queue, the server
     worker that picks it up (`Server.handle`), `BrowserDispatcher.op_close`, the core's Connection.close (each call), the events and the
     reply the server emits (`InProcessTransport.emit_message` / `reply`, which end in `call_soon_threadsafe`), the client's Connection.dispatch
     of each of them (the loop's side), the return of Browser.close, then `Playwright.stop` (`Connection.stop_async`, `request_stop`, the
     shutdown thread, the hooks of the server, `SessionRemoval.close` and its hand-off to the reaper, `wait_until_stopped`), every
     `off_the_loop` (call, the thread's begin and end, the return to the awaiter), `final_reap`, and the user's close (`__aexit__`).
  2. SAMPLES. Between the first Connection.close return and the last user close return of the round, every 20 ms, the Python stack
     (12 frames) of EVERY thread (`sys._current_frames`), with what each is doing (R: running Python or in a C call that is not a known wait,
     W: in a known wait, L: waiting for a lock), the time between two samples (a sampler that cannot run is a GIL or CPU starvation), and every
     200 ms the CPU of the machine and of this process. A coroutine on the loop measures its own lateness every 10 ms.
  3. THE REAPERS. Durations and thread of `Reaper.run`, `EngineTree.end / release / _wait / wait_browser_gone`, `EngineJob.wait_empty /
     terminate`, `LifetimeGuard.reap`, `reap_session`, `find_processes`, `terminate`, `SessionFiles.remove`, `remove_profile`; and every
     psutil call (every public method of `psutil.Process`, `process_iter`, `wait_procs`, `pids`, `pid_exists`) counted with its total time per
     thread and per call site, the long ones (5 ms and more) and every `Thread.start` of 2 ms and more stamped with their instant.
  4. TWO ARMS. HUNT_CW_MODE=together closes the 4 browsers at once (gather, as cand4close); `sequential` awaits each close before the next
     starts (the closes of the others wait on a lock, the rest of the hunt's own threads are the same in both).

The raw data of a round is one file of JSON lines, `closewhy-<mode>-<round>-<pid>.jsonl`, written under a lock, whole, when the window closes and
again at the end of the process; it is read back with `raw_decode`, line by line and, within a line, object by object.
"""
from __future__ import annotations

import asyncio
import collections
import contextvars
import functools
import glob
import inspect
import itertools
import json
import linecache
import os
import re
import sys
import tempfile
import threading
import time

PC = time.perf_counter_ns
PC0 = PC()
WALL0_MS = time.time() * 1000.0

#: What a probe sees of the two packages it was written against (the pins of the workflow): the wrapper's and the core's functions are
#: checked against these lines when they are wrapped, and the log says which differ.
WRAPPER_PIN = "30ee9e597c5434e4f1a17020fc2cea82ab303262"
CORE_PIN = "8c5e9dbda105dc2a153085db25f9aaf839245e20"

GAP_S = 0.5            # a gap over this is looked into
LONG_NS = 5_000_000    # a psutil call of this length is stamped with its instant
START_LONG_NS = 2_000_000
SAMPLE_S = 0.02


def wall_ms(t_ns: int) -> float:
    return WALL0_MS + (t_ns - PC0) / 1e6


# ---------------------------------------------------------------------------------------------------------------------------------
# State of the probes (run mode)
# ---------------------------------------------------------------------------------------------------------------------------------
EPOCH_ON = [False]          # the closes have begun: stamps, psutil accounting and Thread.start timing are on
_TL = threading.local()
_UID = itertools.count(1)
_REG_LOCK = threading.Lock()
THREADS = {}                # uid -> {"name", "ident", "tag", "acc", "ps"}
STAMPS = []                 # (pc_ns, label, tag, uid, extra)
LONGS = []                  # (pc_ns, dur_ns, what, site, uid)
LAGS = []                   # (pc_ns, ms) the loop's lateness over 35 ms
TICKS = [0]
CUR_TAG = contextvars.ContextVar("closewhy_tag", default=None)
TAG_BY = {"conn": {}, "server": {}, "cconn": {}, "tree": {}}   # id(object) -> the browser's tag
CALLS = {}                  # id(core connection) -> how many times Connection.close was called
CLOSE_IDS = set()           # (id(client connection), message id) of the Browser.close requests
CLOSE_MSG = {}              # id(server) -> the message id of the close it received
EMITTED = collections.Counter()
WRAPPED = []                # (label, where, status)
WHERE = {}                  # (class or module, name) -> "file:line" of the function BEFORE the hunt's other wrappers went round it
CTX = {"mode": "together", "dir": None, "arm": None, "round": None, "n": 4, "H": None}


def me() -> dict:
    """The record of the calling thread (a unique uid, never reused, unlike the OS ident), made on the first call."""
    r = getattr(_TL, "r", None)
    if r is None:
        th = threading.current_thread()
        r = _TL.r = {"uid": next(_UID), "name": th.name, "ident": th.ident, "tag": None, "acc": {}, "ps": False}
        with _REG_LOCK:
            THREADS[r["uid"]] = r
    return r


def stamp(label: str, tag=None, **extra):
    r = me()
    t = PC()
    if tag is None:
        tag = r["tag"] or CUR_TAG.get()
    STAMPS.append((t, label, tag, r["uid"], extra or None))
    return t


def _acc_add(r: dict, key: tuple, dt: int) -> None:
    acc = r["acc"]
    e = acc.get(key)
    if e is None:
        acc[key] = [1, dt, dt]
    else:
        e[0] += 1
        e[1] += dt
        if dt > e[2]:
            e[2] = dt


def _safe(fn, a, k):
    try:
        return fn(*a, **k)
    except Exception:   # noqa: BLE001 - a probe never changes what the product does
        return None


def callers(depth: int = 5) -> str:
    """The names of the functions that called the wrapped one (called from an `extra_of`: lambda, _safe, begin, call are skipped)."""
    f = sys._getframe(0)
    for _ in range(5):
        f = f.f_back if f is not None else None
    out = []
    while f is not None and len(out) < depth:
        out.append(f.f_code.co_name)
        f = f.f_back
    return "<".join(out)


def rel(path: str) -> str:
    p = path.replace("\\", "/")
    m = re.search(r"(invisible_(?:playwright|core)/.*)$", p)
    return m.group(1) if m else "/".join(p.rsplit("/", 2)[-2:])


def unhunt(fn):
    """The function under the hunt's own wrappers (hunt_published_latency's `_hunt`, hunt_candidate's `_hunt_ncs`, this file's `_cw`): each keeps what it
    wraps in a closure variable named real*, so the line of the PRODUCT's function can be read after the wrappers went round it."""
    for _ in range(12):
        if not (getattr(fn, "_hunt", False) or getattr(fn, "_hunt_ncs", False) or getattr(fn, "_cw", False)):
            break
        inner = getattr(fn, "__wrapped__", None)
        code = getattr(fn, "__code__", None)
        if inner is None and code is not None:
            for n, cell in zip(code.co_freevars, fn.__closure__ or ()):
                if n.startswith(("real", "_real")):
                    try:
                        v = cell.cell_contents
                    except ValueError:
                        continue
                    if callable(v):
                        inner = v
                        break
        if inner is None:
            break
        fn = inner
    return fn


def line_of(fn):
    return getattr(getattr(unhunt(fn), "__code__", None), "co_firstlineno", None)


def where_of(fn) -> str:
    code = getattr(unhunt(fn), "__code__", None)
    return "%s:%d" % (rel(code.co_filename), code.co_firstlineno) if code else "?"


def snapshot_where(owner, name: str) -> None:
    fn = getattr(owner, name, None)
    if fn is not None:
        WHERE[(getattr(owner, "__qualname__", None) or getattr(owner, "__name__", "?"), name)] = (where_of(fn), line_of(fn))


def rebind_everywhere(real, wrapped) -> None:
    """A module that did `from x import f` holds its own name for the function: every such name is pointed at the wrapper."""
    for mod in list(sys.modules.values()):
        try:
            d = mod.__dict__
            for k, v in list(d.items()):
                if v is real:
                    d[k] = wrapped
        except Exception:   # noqa: BLE001
            pass


def wrap(owner, name: str, label: str, *, expect=None, critical=False, tag_of=None, extra_of=None, only=None, adopt=None, rebind=False) -> None:
    """Replace owner.name by a function that stamps `label.enter` and `label.exit` around the real one (enter and exit are taken on the thread that
    runs it) and returns what it returns. `only(*args)` False = not stamped; `adopt(*args)` = the tag the calling thread carries from now on."""
    real = getattr(owner, name, None)
    qual = getattr(owner, "__qualname__", None) or getattr(owner, "__name__", "?")
    if real is None:
        WRAPPED.append((label, "%s.%s" % (qual, name), "MISSING"))
        if critical:
            raise SystemExit("closewhy: %s.%s is not there (%s)" % (qual, name, label))
        return
    if getattr(real, "_cw", False):
        return
    where, line = WHERE.get((qual, name)) or (where_of(real), line_of(real))
    status = "ok" if expect is None or line == expect else "LINE %s, expected %s" % (line, expect)
    WRAPPED.append((label, where, status))
    if status != "ok" and critical:
        raise SystemExit("closewhy: %s is at %s, not %s: not the package this probe was written for" % (label, where, expect))

    def begin(a, k):
        if not EPOCH_ON[0]:
            return None
        if only is not None and not _safe(only, a, k):
            return None
        tag = _safe(tag_of, a, k) if tag_of else None
        if adopt is not None:
            t = _safe(adopt, a, k)
            if t is not None:
                me()["tag"] = t
                tag = tag or t
        ex = _safe(extra_of, a, k) if extra_of else None
        stamp(label + ".enter", tag, **(ex or {}))
        return tag if tag is not None else False

    if inspect.iscoroutinefunction(real):
        async def call(*a, **k):
            tag = begin(a, k)
            if tag is None:
                return await real(*a, **k)
            try:
                return await real(*a, **k)
            finally:
                stamp(label + ".exit", tag or None)
    else:
        def call(*a, **k):
            tag = begin(a, k)
            if tag is None:
                return real(*a, **k)
            try:
                return real(*a, **k)
            finally:
                stamp(label + ".exit", tag or None)
    functools.update_wrapper(call, real)
    call._cw = True
    setattr(owner, name, call)
    if rebind:
        rebind_everywhere(real, call)


def note_custom(owner, name: str, label: str, real, expect, critical: bool = False) -> None:
    """The line of a function the hunt wraps by hand (the wrapper around it is not `wrap`'s)."""
    qual = getattr(owner, "__qualname__", None) or getattr(owner, "__name__", "?")
    where, line = WHERE.get((qual, name)) or (where_of(real), line_of(real))
    status = "ok" if expect is None or line == expect else "LINE %s, expected %s" % (line, expect)
    WRAPPED.append((label, where, status))
    if critical and status != "ok":
        raise SystemExit("closewhy: %s is at %s, not %s: not the package this probe was written for" % (label, where, expect))


def wrap_agg(owner, name: str, label: str, *, expect=None) -> None:
    """For what is called many times a second (EngineJob.active / pids, EngineTree._survives / members): no stamp, a count and a total time
    per thread and call site, as for psutil."""
    real = getattr(owner, name, None)
    qual = getattr(owner, "__qualname__", None) or getattr(owner, "__name__", "?")
    if real is None or getattr(real, "_cw", False):
        if real is None:
            WRAPPED.append((label, "%s.%s" % (qual, name), "MISSING"))
        return
    where, line = WHERE.get((qual, name)) or (where_of(real), line_of(real))
    WRAPPED.append((label, where, "ok" if expect is None or line == expect else "LINE %s, expected %s" % (line, expect)))

    def call(*a, **k):
        if not EPOCH_ON[0]:
            return real(*a, **k)
        t = PC()
        try:
            return real(*a, **k)
        finally:
            f = sys._getframe(1)
            _acc_add(me(), (label, f.f_code.co_filename, f.f_lineno, f.f_code.co_name), PC() - t)
    functools.update_wrapper(call, real)
    call._cw = True
    setattr(owner, name, call)


# ---------------------------------------------------------------------------------------------------------------------------------
# psutil and Thread.start: counted, and the long ones placed in time
# ---------------------------------------------------------------------------------------------------------------------------------
_NOLONG = {"Process.wait", "wait_procs"}     # they wait by design


def _psutil_wrap(real, label: str):
    @functools.wraps(real)
    def call(*a, **k):
        r = me()
        if r["ps"] or not EPOCH_ON[0]:        # a psutil call inside a psutil call is the outer one's time
            return real(*a, **k)
        r["ps"] = True
        t = PC()
        try:
            return real(*a, **k)
        finally:
            dt = PC() - t
            r["ps"] = False
            f = sys._getframe(1)
            c = f.f_code
            _acc_add(r, (label, c.co_filename, f.f_lineno, c.co_name), dt)
            if dt >= LONG_NS and label not in _NOLONG:
                LONGS.append((t, dt, label, "%s:%d %s" % (rel(c.co_filename), f.f_lineno, c.co_name), r["uid"]))
    call._cw = True
    return call


def install_psutil() -> None:
    import psutil
    cls = psutil.Process
    for name, fn in list(vars(cls).items()):
        if not inspect.isfunction(fn) or getattr(fn, "_cw", False):
            continue
        if name.startswith("_") and name != "__init__":
            continue
        if name == "oneshot":
            continue
        setattr(cls, name, _psutil_wrap(fn, "Process." + name))
    for name in ("wait_procs", "pids", "pid_exists"):
        fn = getattr(psutil, name, None)
        if fn is not None and not getattr(fn, "_cw", False):
            setattr(psutil, name, _psutil_wrap(fn, name))
    real_iter = psutil.process_iter
    if not getattr(real_iter, "_cw", False):
        def process_iter(*a, **k):
            f = sys._getframe(1)
            c = f.f_code
            key = ("process_iter", c.co_filename, f.f_lineno, c.co_name)
            it = real_iter(*a, **k)
            r = me()
            t_total, n = 0, 0
            try:
                while True:
                    t = PC()
                    r["ps"] = True
                    try:
                        item = next(it)
                    except StopIteration:
                        return
                    finally:
                        r["ps"] = False
                        d = PC() - t
                        t_total += d
                    n += 1
                    yield item
            finally:
                if EPOCH_ON[0]:
                    _acc_add(r, key, t_total)
                    if t_total >= LONG_NS:
                        LONGS.append((PC() - t_total, t_total, "process_iter (%d items)" % n, "%s:%d %s" % (rel(c.co_filename), f.f_lineno, c.co_name), r["uid"]))
        process_iter._cw = True
        if hasattr(real_iter, "cache_clear"):
            process_iter.cache_clear = real_iter.cache_clear
        psutil.process_iter = process_iter
    WRAPPED.append(("psutil", "psutil.Process methods, process_iter, wait_procs, pids, pid_exists", "ok"))


def install_thread_start() -> None:
    real = threading.Thread.start
    if getattr(real, "_cw", False):
        return

    def start(self):
        if not EPOCH_ON[0]:
            return real(self)
        t = PC()
        try:
            return real(self)
        finally:
            dt = PC() - t
            r = me()
            f = sys._getframe(1)
            _acc_add(r, ("Thread.start", f.f_code.co_filename, f.f_lineno, f.f_code.co_name), dt)
            if dt >= START_LONG_NS:
                LONGS.append((t, dt, "Thread.start", "of %s, from %s:%d %s" % (self.name, rel(f.f_code.co_filename), f.f_lineno, f.f_code.co_name), r["uid"]))
    start._cw = True
    threading.Thread.start = start


# ---------------------------------------------------------------------------------------------------------------------------------
# The window, the sampler, the loop's lateness
# ---------------------------------------------------------------------------------------------------------------------------------
class Window:
    """Opens at the first return of a Connection.close (the first call of any browser) and closes at the last return of the N user closes."""

    def __init__(self) -> None:
        self.opened, self.closed = threading.Event(), threading.Event()
        self.t_open = self.t_close = None
        self.users = 0
        self.lock = threading.Lock()

    def conn_returned(self) -> None:
        with self.lock:
            if self.t_open is None:
                self.t_open = PC()
                self.opened.set()
                print("HUNTCWWIN " + json.dumps({"event": "open", "wall_ms": round(wall_ms(self.t_open), 1)}), flush=True)

    def user_returned(self) -> None:
        with self.lock:
            self.users += 1
            done = self.users >= CTX["n"]
        if done:
            self.t_close = PC()
            self.closed.set()
            print("HUNTCWWIN " + json.dumps({"event": "close", "wall_ms": round(wall_ms(self.t_close), 1),
                                              "ms": round((self.t_close - (self.t_open or self.t_close)) / 1e6, 1)}), flush=True)


W = Window()

_BLOCK = re.compile(r"\.(wait|acquire|select|recv|recv_into|accept|readline|readinto|read|result|communicate|wait_for)\(|(?<!path)(?<![\"'])\.join\(|"
                    r"\bsleep\(|GetQueuedCompletionStatus|WaitForSingleObject|proc_wait\(|real_read\(|\.readline\b|_wait_for_tstate_lock|"
                    r"\.get\(\s*\)|\.get\(.*(timeout|block)|_poll\(")
_LOCK = re.compile(r"^\s*with\s+.*(lock|_cv|cond|mutex)", re.I)


class Sampler(threading.Thread):
    """Every 20 ms, from the window's opening to its closing: the 12 innermost frames of every thread but this one."""

    def __init__(self) -> None:
        super().__init__(name="closewhy-sampler", daemon=True)
        self.samples = []            # (pc_ns, dt_ms, [(tid, state, stack_id)])
        self.cpu = []                # (pc_ns, process_time_ns, busy, total)
        self.fmap, self.flist, self.fstate = {}, [], []
        self.smap, self.slist = {}, []
        self.tmap, self.tlist = {}, []
        self.error = None

    def _state(self, filename: str, lineno: int) -> str:
        line = linecache.getline(filename, lineno)
        if not line.strip():
            return "?"
        if _LOCK.search(line):
            return "L"
        if _BLOCK.search(line):
            return "W"
        return "R"

    def _fid(self, code, lineno: int) -> int:
        key = (code.co_filename, lineno, code.co_name)
        i = self.fmap.get(key)
        if i is None:
            i = self.fmap[key] = len(self.flist)
            self.flist.append(key)
            self.fstate.append(self._state(code.co_filename, lineno))
        return i

    def run(self) -> None:
        try:
            self._run()
        except BaseException as exc:   # noqa: BLE001
            self.error = "%s: %s" % (type(exc).__name__, exc)
            print("HUNTCWERR sampler " + self.error, flush=True)

    def _run(self) -> None:
        import psutil
        W.opened.wait()
        mine = threading.get_ident()
        prev = None
        k = 0
        limit = PC() + 240_000_000_000
        while True:
            t = PC()
            names = {th.ident: th.name for th in threading.enumerate()}
            snap = []
            for ident, frame in sys._current_frames().items():
                if ident == mine:
                    continue
                fids, f = [], frame
                while f is not None and len(fids) < 12:
                    fids.append(self._fid(f.f_code, f.f_lineno))
                    f = f.f_back
                stack = tuple(fids)
                sid = self.smap.get(stack)
                if sid is None:
                    sid = self.smap[stack] = len(self.slist)
                    self.slist.append(stack)
                tkey = (ident, names.get(ident, "?"))
                tid = self.tmap.get(tkey)
                if tid is None:
                    tid = self.tmap[tkey] = len(self.tlist)
                    self.tlist.append(tkey)
                snap.append((tid, self.fstate[fids[0]] if fids else "?", sid))
            self.samples.append((t, round((t - prev) / 1e6, 2) if prev is not None else 0.0, snap))
            prev = t
            if k % 10 == 0:
                ct = psutil.cpu_times()
                total = sum(ct)
                self.cpu.append((t, time.process_time_ns(), total - ct.idle, total))
            k += 1
            if W.closed.is_set() or t > limit:
                break
            time.sleep(SAMPLE_S)
        dump("window closed")


SAMPLER = Sampler()
LOOPTASK = []


async def _looplag() -> None:
    while not W.closed.is_set():
        t = PC()
        await asyncio.sleep(0.01)
        ms = (PC() - t) / 1e6
        TICKS[0] += 1
        if ms > 35:
            LAGS.append((t, round(ms, 2)))


# ---------------------------------------------------------------------------------------------------------------------------------
# The probes on the wrapper and on the core
# ---------------------------------------------------------------------------------------------------------------------------------
def _cc(self, *a, **k):
    return TAG_BY["cconn"].get(id(self))


def install_closewhy(H, HC) -> None:
    """Called by `hunt_published_latency.main` where it installs its close stamps (before a browser exists)."""
    import importlib
    pmod = importlib.import_module("invisible_core.process")
    cmod = importlib.import_module("invisible_core.juggler.connection")
    prof = importlib.import_module("invisible_core.juggler._profile")
    om = importlib.import_module("invisible_core._offloop")
    rmod = importlib.import_module("invisible_playwright._juggler._reaped")
    dmod = importlib.import_module("invisible_playwright._juggler.dispatcher")
    # The lines of the core's functions are read BEFORE the hunt's own wrappers (hunt_candidate) go round them.
    for owner, names in ((pmod.EngineTree, ("end", "release", "_wait", "wait_browser_gone", "members", "remember", "_survives")),
                         (pmod.Reaper, ("run", "then", "start", "join", "_continue")), (pmod.EngineJob, ("wait_empty", "terminate", "active", "pids")),
                         (pmod.LifetimeGuard, ("reap",)), (cmod.Connection, ("close",)), (prof.SessionFiles, ("remove",)),
                         (pmod, ("reap_session", "find_processes", "terminate")), (prof, ("remove_profile",)), (om, ("off_the_loop",)),
                         (rmod, ("after_the_reapers",)), (dmod.Server, ("after_reapers",))):
        for n in names:
            snapshot_where(owner, n)
    HC.install_clean_close_stamps()          # the stamps of the earlier hunt (CloseRec, the exits, the ends), as in cand4close
    install_psutil()
    install_thread_start()

    # ----- the core -----
    real_close = cmod.Connection.close

    def conn_close(self, *a, **k):
        if not EPOCH_ON[0]:
            return real_close(self, *a, **k)
        n = CALLS[id(self)] = CALLS.get(id(self), 0) + 1
        tag = TAG_BY["conn"].get(id(self))
        tree = getattr(getattr(self, "_process", None), "tree", None)
        if tree is not None and tag:
            TAG_BY["tree"][id(tree)] = tag
        me()["tag"] = tag or me()["tag"]
        stamp("conn.close.enter", tag, n=n, args=repr(a)[:40])
        try:
            return real_close(self, *a, **k)
        finally:
            stamp("conn.close.exit", tag, n=n)
            if n == 1:
                W.conn_returned()
    conn_close._cw = True
    functools.update_wrapper(conn_close, real_close)
    cmod.Connection.close = conn_close
    note_custom(cmod.Connection, "close", "conn.close", real_close, 910)

    tree_tag = lambda self, *a, **k: TAG_BY["tree"].get(id(self))     # noqa: E731
    wrap(pmod.EngineTree, "end", "tree.end", expect=1132, critical=True, tag_of=tree_tag,
         extra_of=lambda self, *a, **k: {"grace": k.get("grace", a[0] if a else 0.0), "timeout": k.get("timeout", a[1] if len(a) > 1 else 5.0),
                                        "by": callers(5)})
    wrap(pmod.EngineTree, "release", "tree.release", expect=1175, tag_of=tree_tag, extra_of=lambda self: {"by": callers(4)})
    wrap(pmod.EngineTree, "_wait", "tree._wait", expect=1120, tag_of=tree_tag, extra_of=lambda self, procs, timeout: {"timeout": timeout, "procs": len(procs)})
    wrap(pmod.EngineTree, "wait_browser_gone", "tree.wait_browser_gone", expect=1051, tag_of=tree_tag)
    wrap(pmod.EngineTree, "remember", "tree.remember", expect=969, tag_of=tree_tag)
    wrap_agg(pmod.EngineTree, "members", "tree.members", expect=950)
    wrap_agg(pmod.EngineTree, "_survives", "tree._survives", expect=1096)
    wrap(pmod.EngineJob, "wait_empty", "job.wait_empty", expect=686, extra_of=lambda self, deadline: {"in_s": round(deadline - time.monotonic(), 2)})
    wrap(pmod.EngineJob, "terminate", "job.terminate", expect=725)
    wrap_agg(pmod.EngineJob, "active", "job.active", expect=657)
    wrap_agg(pmod.EngineJob, "pids", "job.pids", expect=694)
    wrap(pmod.Reaper, "run", "reaper.run", expect=1274, critical=True,
         tag_of=lambda self: TAG_BY["tree"].get(id(self.tree)), adopt=lambda self: TAG_BY["tree"].get(id(self.tree)),
         extra_of=lambda self: {"grace": self.grace, "timeout": self.timeout})
    wrap(pmod.Reaper, "_continue", "reaper._continue", expect=1260, critical=True,
         tag_of=lambda self: TAG_BY["tree"].get(id(self.tree)), adopt=lambda self: TAG_BY["tree"].get(id(self.tree)))
    real_then = pmod.Reaper.then

    def reaper_then(self, step):
        """Each step handed to a reaper is stamped when it runs, on whichever thread runs it (the reaper's, or its continuation)."""
        if not EPOCH_ON[0]:
            return real_then(self, step)
        tag = TAG_BY["tree"].get(id(self.tree))
        name = getattr(step, "__qualname__", None) or repr(step)[:60]

        def timed():
            me()["tag"] = tag
            stamp("reaper.step.enter", tag, step=name)
            try:
                return step()
            finally:
                stamp("reaper.step.exit", tag)
        stamp("reaper.then.enter", tag, step=name, finished=getattr(self, "_finished", None))
        try:
            return real_then(self, timed)
        finally:
            stamp("reaper.then.exit", tag)
    reaper_then._cw = True
    functools.update_wrapper(reaper_then, real_then)
    pmod.Reaper.then = reaper_then
    note_custom(pmod.Reaper, "then", "reaper.then", real_then, 1231, critical=True)
    wrap(pmod.Reaper, "start", "reaper.start", expect=1297, tag_of=lambda self: TAG_BY["tree"].get(id(self.tree)))
    wrap(pmod.Reaper, "join", "reaper.join", expect=1305, tag_of=lambda self, *a, **k: TAG_BY["tree"].get(id(self.tree)))
    wrap(pmod.LifetimeGuard, "reap", "guard.reap", expect=299, extra_of=lambda self, *a, **k: {"guard": type(self).__name__, "by": callers(4)})
    wrap(pmod, "reap_session", "reap_session", expect=270, rebind=True, extra_of=lambda *a, **k: {"by": callers(4)})
    wrap(pmod, "find_processes", "find_processes", expect=163, rebind=True)
    wrap(pmod, "terminate", "process.terminate", expect=203, rebind=True)
    wrap(prof.SessionFiles, "remove", "session_files.remove", expect=96)
    wrap(prof, "remove_profile", "remove_profile", expect=116, rebind=True)

    # ----- off_the_loop: the call, the thread's begin and end, the return to the awaiter -----
    real_off = om.off_the_loop
    if not getattr(real_off, "_cw", False):
        async def off_the_loop(step, *args):
            if not EPOCH_ON[0]:
                return await real_off(step, *args)
            name = getattr(step, "__name__", "?")
            tag = CUR_TAG.get() or me()["tag"]
            label = "offloop[%s]" % name

            def timed(*a):
                me()["tag"] = tag
                stamp(label + ".thread.enter", tag)
                try:
                    return step(*a)
                finally:
                    stamp(label + ".thread.exit", tag)
            stamp(label + ".enter", tag)
            try:
                return await real_off(timed, *args)
            finally:
                stamp(label + ".exit", tag)
        off_the_loop._cw = True
        functools.update_wrapper(off_the_loop, real_off)
        om.off_the_loop = off_the_loop
        rebind_everywhere(real_off, off_the_loop)
        note_custom(om, "off_the_loop", "off_the_loop", real_off, 30)

    # ----- the wrapper: client side -----
    bmod = importlib.import_module("invisible_playwright._pw._impl._browser")
    gmod = importlib.import_module("invisible_playwright._pw.async_api._generated")
    cnmod = importlib.import_module("invisible_playwright._pw._impl._connection")
    ctxm = importlib.import_module("invisible_playwright._pw.async_api._context_manager")
    amod = importlib.import_module("invisible_playwright.async_api")
    wrap(gmod.Browser, "close", "api.Browser.close", expect=16290, critical=True, tag_of=lambda self, *a, **k: TAG_BY["cconn"].get(id(self._impl_obj._connection)))
    wrap(bmod.Browser, "close", "impl.Browser.close", expect=254, critical=True, tag_of=lambda self, *a, **k: TAG_BY["cconn"].get(id(self._connection)))
    wrap(cnmod.Connection, "stop_async", "pw.stop_async", expect=322, tag_of=_cc)
    wrap(cnmod.Connection, "cleanup", "client.cleanup", expect=327, tag_of=_cc)
    wrap(ctxm.PlaywrightContextManager, "__aexit__", "pw.__aexit__", expect=58, tag_of=lambda self, *a: TAG_BY["cconn"].get(id(self._connection)))
    wrap(amod.InvisiblePlaywright, "_teardown", "teardown", expect=311)
    wrap(amod.InvisiblePlaywright, "__aexit__", "cm.__aexit__", expect=308)

    real_send = cnmod.Connection._send_message_to_server
    snapshot_where(cnmod.Connection, "_send_message_to_server")

    def send_message(self, obj, method, *a, **k):
        if not EPOCH_ON[0] or method != "close" or not str(getattr(obj, "_guid", "")).startswith("browser@"):
            return real_send(self, obj, method, *a, **k)
        tag = TAG_BY["cconn"].get(id(self))
        stamp("client.send.close.enter", tag)
        try:
            return real_send(self, obj, method, *a, **k)
        finally:
            CLOSE_IDS.add((id(self), self._last_id))
            stamp("client.send.close.exit", tag, id=self._last_id)
    send_message._cw = True
    functools.update_wrapper(send_message, real_send)
    cnmod.Connection._send_message_to_server = send_message
    note_custom(cnmod.Connection, "_send_message_to_server", "client.send.close", real_send, 354)

    real_dispatch = cnmod.Connection.dispatch

    def dispatch(self, msg):
        if not EPOCH_ON[0]:
            return real_dispatch(self, msg)
        tag = TAG_BY["cconn"].get(id(self))
        if tag is None or EMITTED[("d", id(self))] >= 300:
            return real_dispatch(self, msg)
        EMITTED[("d", id(self))] += 1
        mid = msg.get("id") if isinstance(msg, dict) else None
        stamp("client.dispatch.enter", tag, id=mid, method=(msg.get("method") if isinstance(msg, dict) else None),
              close_reply=bool(mid and (id(self), mid) in CLOSE_IDS))
        try:
            return real_dispatch(self, msg)
        finally:
            stamp("client.dispatch.exit", tag, id=mid)
    dispatch._cw = True
    functools.update_wrapper(dispatch, real_dispatch)
    cnmod.Connection.dispatch = dispatch
    note_custom(cnmod.Connection, "dispatch", "client.dispatch", real_dispatch, 408)

    # ----- the wrapper: server side -----
    dmod = importlib.import_module("invisible_playwright._juggler.dispatcher")
    smod = importlib.import_module("invisible_playwright._juggler.server")
    tmod = importlib.import_module("invisible_playwright._juggler.transport")
    rmod = importlib.import_module("invisible_playwright._juggler._reaped")
    server_tag = lambda self, *a, **k: TAG_BY["server"].get(id(self))       # noqa: E731
    transport_tag = lambda self, *a, **k: TAG_BY["server"].get(id(self._server))      # noqa: E731

    real_handle = dmod.Server.handle
    snapshot_where(dmod.Server, "handle")

    def handle(self, message):
        if not EPOCH_ON[0]:
            return real_handle(self, message)
        tag = TAG_BY["server"].get(id(self))
        if tag is not None:
            me()["tag"] = tag
        if not (isinstance(message, dict) and message.get("method") == "close" and str(message.get("guid", "")).startswith("browser@")):
            return real_handle(self, message)
        CLOSE_MSG[id(self)] = message.get("id")
        stamp("server.handle.close.enter", tag, id=message.get("id"))
        try:
            return real_handle(self, message)
        finally:
            stamp("server.handle.close.exit", tag, id=message.get("id"))
    handle._cw = True
    functools.update_wrapper(handle, real_handle)
    dmod.Server.handle = handle
    note_custom(dmod.Server, "handle", "server.handle", real_handle, 487)
    wrap(dmod.Server, "shutdown", "server.shutdown", expect=464, tag_of=server_tag, adopt=server_tag)

    real_on = dmod.Server.on_shutdown
    snapshot_where(dmod.Server, "on_shutdown")

    def on_shutdown(self, hook):
        def timed_hook():
            if not EPOCH_ON[0]:
                return hook()
            tag = TAG_BY["server"].get(id(self))
            stamp("server.hook.enter", tag, hook=getattr(hook, "__qualname__", "?"))
            try:
                return hook()
            finally:
                stamp("server.hook.exit", tag)
        return real_on(self, timed_hook)
    on_shutdown._cw = True
    dmod.Server.on_shutdown = on_shutdown
    note_custom(dmod.Server, "on_shutdown", "server.on_shutdown", real_on, 431)

    real_after = dmod.Server.after_shutdown
    snapshot_where(dmod.Server, "after_shutdown")

    def after_shutdown(self, step):
        def timed_step():
            if not EPOCH_ON[0]:
                return step()
            tag = TAG_BY["server"].get(id(self))
            stamp("server.after_step.enter", tag, step=getattr(step, "__qualname__", "?"))
            try:
                return step()
            finally:
                stamp("server.after_step.exit", tag)
        return real_after(self, timed_step)
    after_shutdown._cw = True
    dmod.Server.after_shutdown = after_shutdown
    note_custom(dmod.Server, "after_shutdown", "server.after_shutdown", real_after, 434)

    is_browser = lambda self, *a, **k: getattr(self, "TYPE", None) == "Browser"     # noqa: E731
    browser_tag = lambda self, *a, **k: TAG_BY["conn"].get(id(getattr(self, "conn", None)))      # noqa: E731
    wrap(dmod.Dispatcher, "emit", "dispatcher.emit", expect=183, only=is_browser, tag_of=browser_tag, extra_of=lambda self, method, *a, **k: {"method": method})
    wrap(dmod.Dispatcher, "dispose", "dispatcher.dispose", expect=192, only=is_browser, tag_of=browser_tag)
    wrap(smod.BrowserDispatcher, "op_close", "server.op_close", expect=6111, critical=True, tag_of=browser_tag, adopt=browser_tag)

    wrap(tmod.InProcessTransport, "send", "transport.send", expect=208, tag_of=transport_tag,
         only=lambda self, message: isinstance(message, dict) and message.get("method") == "close" and str(message.get("guid", "")).startswith("browser@"),
         extra_of=lambda self, message: {"id": message.get("id")})
    real_emit = tmod.InProcessTransport.emit_message
    snapshot_where(tmod.InProcessTransport, "emit_message")

    def emit_message(self, message):
        if not EPOCH_ON[0]:
            return real_emit(self, message)
        key = id(self._server)
        tag = TAG_BY["server"].get(key)
        if tag is None or EMITTED[("e", key)] >= 300:
            return real_emit(self, message)
        EMITTED[("e", key)] += 1
        mid = message.get("id") if isinstance(message, dict) else None
        stamp("transport.emit_message.enter", tag, id=mid, method=(message.get("method") if isinstance(message, dict) else None),
              close_reply=bool(mid is not None and CLOSE_MSG.get(key) == mid))
        try:
            return real_emit(self, message)
        finally:
            stamp("transport.emit_message.exit", tag, id=mid)
    emit_message._cw = True
    functools.update_wrapper(emit_message, real_emit)
    tmod.InProcessTransport.emit_message = emit_message
    note_custom(tmod.InProcessTransport, "emit_message", "transport.emit_message", real_emit, 228)
    wrap(tmod.InProcessTransport, "reply", "transport.reply", expect=244, critical=True, tag_of=transport_tag,
         only=lambda self, msg_id, result: msg_id is not None and CLOSE_MSG.get(id(self._server)) == msg_id, extra_of=lambda self, msg_id, result: {"id": msg_id})
    wrap(tmod.InProcessTransport, "request_stop", "transport.request_stop", expect=139, tag_of=transport_tag)
    wrap(tmod.InProcessTransport, "_begin", "transport._begin", expect=178, tag_of=transport_tag)
    wrap(tmod.InProcessTransport, "_settle", "transport._settle", expect=187, tag_of=transport_tag)
    wrap(tmod.InProcessTransport, "_join_workers", "transport._join_workers", expect=191, tag_of=transport_tag)
    wrap(tmod.InProcessTransport, "wait_until_stopped", "transport.wait_until_stopped", expect=204, tag_of=transport_tag)

    conn_tag = lambda self, conn, *a, **k: TAG_BY["conn"].get(id(conn))     # noqa: E731
    wrap(rmod.SessionRemoval, "close", "removal.close", expect=56, critical=True, tag_of=conn_tag, adopt=conn_tag)
    wrap(rmod.SessionRemoval, "run", "removal.run", expect=38)
    wrap(rmod.SessionRemoval, "after_shutdown", "removal.after_shutdown", expect=45)
    wrap(rmod, "after_the_reapers", "after_the_reapers", expect=97, critical=True, rebind=True,
         extra_of=lambda step, *tokens, **k: {"tokens": len(tokens), "forget": k.get("forget", False), "step": getattr(step, "__qualname__", "?")})
    wrap(rmod, "final_reap", "final_reap", expect=137, critical=True, rebind=True)
    wrap(dmod.Server, "after_reapers", "server.after_reapers", expect=455, tag_of=server_tag)
    wrap(dmod.Server, "note_session_token", "server.note_session_token", expect=449)

    for label, where, status in WRAPPED:
        # Every probe is listed in the first round; the others print only what is not as expected, and the summary.
        if CTX["round"] == "r0" or status != "ok":
            print("HUNTCWWRAP " + json.dumps({"label": label, "where": where, "status": status}), flush=True)
    bad = [w for w in WRAPPED if w[2] != "ok"]
    print("HUNTCWWRAP " + json.dumps({"summary": "%d wrapped, %d not as expected" % (len(WRAPPED), len(bad)), "pins": [WRAPPER_PIN, CORE_PIN]}), flush=True)
    SAMPLER.start()


# ---------------------------------------------------------------------------------------------------------------------------------
# The raw file
# ---------------------------------------------------------------------------------------------------------------------------------
DUMP_LOCK = threading.Lock()


def build_records() -> list:
    H = CTX["H"]
    recs = [{"k": "meta", "mode": CTX["mode"], "arm": CTX["arm"], "round": CTX["round"], "n": CTX["n"], "pc0": PC0, "wall0_ms": WALL0_MS,
             "python": sys.version.split()[0], "platform": sys.platform, "cpus": os.cpu_count(), "pid": os.getpid(),
             "wrapped": WRAPPED, "pins": [WRAPPER_PIN, CORE_PIN], "ticks": TICKS[0]}]
    recs.append({"k": "win", "open": W.t_open, "close": W.t_close, "users": W.users})
    with _REG_LOCK:
        threads = {uid: {"name": r["name"], "ident": r["ident"], "tag": r["tag"]} for uid, r in THREADS.items()}
        accs = [(uid, dict(r["acc"])) for uid, r in THREADS.items()]
    recs.append({"k": "threads", "t": threads})
    for t, label, tag, uid, extra in list(STAMPS):
        o = {"k": "s", "pc": t, "w": round(wall_ms(t), 3), "l": label, "g": tag, "u": uid}
        if extra:
            o["x"] = extra
        recs.append(o)
    for t, dt, what, site, uid in list(LONGS):
        recs.append({"k": "long", "pc": t, "ms": round(dt / 1e6, 3), "what": what, "site": site, "u": uid})
    for t, ms in list(LAGS):
        recs.append({"k": "lag", "pc": t, "ms": ms})
    for uid, acc in accs:
        for (fn, filename, line, func), (n, total, mx) in acc.items():
            recs.append({"k": "acc", "u": uid, "fn": fn, "file": rel(filename), "line": line, "func": func, "n": n, "ms": round(total / 1e6, 3), "max": round(mx / 1e6, 3)})
    s = SAMPLER
    recs.append({"k": "frames", "t": [[rel(f), ln, fn] for f, ln, fn in s.flist], "states": list(s.fstate)})
    recs.append({"k": "stacks", "t": [list(x) for x in s.slist]})
    recs.append({"k": "tids", "t": [list(x) for x in s.tlist]})
    for t, dt, snap in list(s.samples):
        recs.append({"k": "p", "pc": t, "dt": dt, "s": [list(x) for x in snap]})
    for t, proc, busy, total in list(s.cpu):
        recs.append({"k": "cpu", "pc": t, "proc": proc, "busy": round(busy, 3), "total": round(total, 3)})
    if H is not None:
        for rec in list(H.CONN_REC.values()):
            def ns(v):
                return int(v * 1e9) if v is not None else None
            recs.append({"k": "rec", "g": TAG_BY["conn"].get(id(rec.conn)), "t_call": ns(rec.t_call), "t_entry": ns(rec.t_entry), "t_conn_return": ns(rec.t_conn_return),
                         "t_return": ns(rec.t_return), "t_parent": ns(rec.t_parent), "t_reaper": ns(rec.t_reaper), "reaper_finished": rec.reaper_finished})
    return recs


def write_records(path: str, recs: list) -> int:
    """The whole file or none of it: written to a temporary file, one line per record in a single write, under a lock, then renamed."""
    with DUMP_LOCK:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            for r in recs:
                f.write(json.dumps(r, separators=(",", ":"), default=str) + "\n")
        os.replace(tmp, path)
    return os.path.getsize(path)


def dump(why: str) -> None:
    try:
        d = CTX["dir"] or os.path.join(tempfile.gettempdir(), "closewhy")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "closewhy-%s-%s-%d.jsonl" % (CTX["mode"], CTX["round"], os.getpid()))
        recs = build_records()
        size = write_records(path, recs)
        print("HUNTCWDUMP " + json.dumps({"why": why, "path": path, "records": len(recs), "bytes": size, "stamps": len(STAMPS), "samples": len(SAMPLER.samples)}), flush=True)
    except BaseException as exc:   # noqa: BLE001
        print("HUNTCWERR dump %s: %s" % (type(exc).__name__, exc), flush=True)


# ---------------------------------------------------------------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------------------------------------------------------------
def run_main(argv: list) -> None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import hunt_candidate as HC
    import hunt_published_latency as H

    if os.environ.get("HUNT_CLOSE_TIMEOUT"):
        sys.exit("HUNT_CLOSE_TIMEOUT is set: this hunt raises no deadline")
    if os.environ.get("HUNT_NCLOSE") != "1":
        sys.exit("HUNT_NCLOSE=1 is the close stamps' switch: without it nothing is installed")
    arm, n, rnd = argv[0], int(argv[1]), argv[2]
    binary = argv[3] if len(argv) > 3 else None
    mode = os.environ.get("HUNT_CW_MODE") or "together"
    if mode not in ("together", "sequential") or arm != "inproc":
        sys.exit("closewhy: arm inproc, HUNT_CW_MODE together|sequential")
    CTX.update({"mode": mode, "dir": os.environ.get("HUNT_CW_DIR"), "arm": os.environ.get("HUNT_ARM"), "round": rnd, "n": n, "H": H})
    if os.environ.get("HUNT_PIDFILE"):
        with open(os.environ["HUNT_PIDFILE"], "a", encoding="utf-8", newline="\n") as f:
            f.write("%d\n" % os.getpid())
    H.ARM_CTX.update({"arm": os.environ.get("HUNT_ARM"), "round": rnd})

    H.install_close_stamps = lambda: install_closewhy(H, HC)
    H.TreeWatch = HC.TreeWatch2
    real_close_browser = H._close_browser
    lock = {}

    def registered(tag, browser):
        """The identities the probes know a browser by: its core connection, the wrapper's server and transport, the client's connection, its tree."""
        impl = getattr(browser, "_impl_obj", browser)
        cconn = impl._connection
        server = cconn._transport._server
        TAG_BY["cconn"][id(cconn)] = tag
        TAG_BY["server"][id(server)] = tag
        conn, _how = H.find_conn(browser)
        if conn is not None:
            TAG_BY["conn"][id(conn)] = tag
            tree = getattr(getattr(conn, "_process", None), "tree", None)
            if tree is not None:
                TAG_BY["tree"][id(tree)] = tag

    async def close_browser(arm_, port, tag, row, browser, close, t0):
        if not EPOCH_ON[0]:
            EPOCH_ON[0] = True
            stamp("epoch.start", tag)
            LOOPTASK.append(asyncio.get_running_loop().create_task(_looplag()))     # kept: a task nobody holds can be collected
        try:
            registered(tag, browser)
        except Exception as exc:   # noqa: BLE001
            row["closewhy_register_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])

        async def user_close():
            CUR_TAG.set(tag)
            stamp("user.close.enter", tag)
            try:
                return await close()
            finally:
                stamp("user.close.exit", tag)
                W.user_returned()

        async def instrumented():
            if CTX["mode"] == "sequential":
                if "l" not in lock:
                    lock["l"] = asyncio.Lock()
                async with lock["l"]:
                    stamp("sequential.turn", tag)
                    return await user_close()
            return await user_close()
        return await real_close_browser(arm_, port, tag, row, browser, instrumented, t0)
    H._close_browser = close_browser
    H.start_alive_watchdog()

    async def main():
        try:
            await H.main(arm, n, rnd, binary)
        finally:
            dump("end of the process")
    asyncio.run(main())
    late = threading.Timer(5.0, H.print_alive, args=("5s-after-main-returned", False))
    late.daemon = True
    late.start()


# ---------------------------------------------------------------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------------------------------------------------------------
_DEC = json.JSONDecoder()


def iter_json(text: str):
    """Every JSON object in `text`, wherever it starts: a line broken by an interleaved write loses that object, never the ones around it."""
    i, n = 0, len(text)
    while True:
        j = text.find("{", i)
        if j < 0:
            return
        try:
            obj, end = _DEC.raw_decode(text, j)
        except ValueError:
            i = j + 1
            continue
        yield obj
        i = end


def pct(v, p):
    v = sorted(x for x in v if x is not None)
    return v[min(len(v) - 1, int(p * len(v)))] if v else None


def short(tag) -> str:
    return "b" + str(tag).rsplit("-", 1)[-1] if tag else "-"


def base(label: str) -> str:
    return label.rsplit(".", 1)[0]


class Run:
    def __init__(self, path: str) -> None:
        self.path = path
        self.meta, self.win = {}, {}
        self.stamps, self.longs, self.lags, self.accs, self.samples, self.cpus, self.recs = [], [], [], [], [], [], []
        self.threads, self.frames, self.states, self.stacks, self.tids = {}, [], [], [], []
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                for o in iter_json(line):
                    self.add(o)
        self.stamps.sort(key=lambda s: s["pc"])
        self.samples.sort(key=lambda s: s["pc"])
        for r in self.recs:
            if r.get("g") and r.get("t_parent"):
                self.stamps.append({"k": "s", "pc": r["t_parent"], "w": None, "l": "hunt.parent_gone", "g": r["g"], "u": -1})
        self.stamps.sort(key=lambda s: s["pc"])
        self._fs = {}

    def add(self, o: dict) -> None:
        k = o.get("k")
        if k == "meta":
            self.meta = o
        elif k == "win":
            self.win = o
        elif k == "s":
            self.stamps.append(o)
        elif k == "long":
            self.longs.append(o)
        elif k == "lag":
            self.lags.append(o)
        elif k == "acc":
            self.accs.append(o)
        elif k == "p":
            self.samples.append(o)
        elif k == "cpu":
            self.cpus.append(o)
        elif k == "rec":
            self.recs.append(o)
        elif k == "threads":
            self.threads = {int(u): v for u, v in o["t"].items()}
        elif k == "frames":
            self.frames, self.states = o["t"], o.get("states") or []
        elif k == "stacks":
            self.stacks = o["t"]
        elif k == "tids":
            self.tids = o["t"]

    # names
    def tname(self, uid: int) -> str:
        t = self.threads.get(uid)
        if uid == -1:
            return "hunt-parentwatch"
        return t["name"] if t else "?"

    def frame(self, fid: int) -> str:
        s = self._fs.get(fid)
        if s is None:
            f, ln, fn = self.frames[fid]
            s = self._fs[fid] = "%s:%s %s" % (f.rsplit("/", 1)[-1], ln, fn)
        return s

    def stack(self, sid: int, n: int = 12) -> str:
        return " < ".join(self.frame(f) for f in self.stacks[sid][:n])

    def ms(self, t: int) -> float:
        return (t - self.meta.get("pc0", 0)) / 1e6

    def mode(self) -> str:
        return self.meta.get("mode") or "?"

    def rnd(self) -> str:
        return str(self.meta.get("round"))


def thread_class(name: str) -> str:
    return re.sub(r"[-_]\d+(_\d+)?$", "-N", name)


def first(st: list, label: str, **match):
    for s in st:
        if s["l"] == label and all((s.get("x") or {}).get(k) == v for k, v in match.items()):
            return s
    return None


def last(st: list, label: str):
    for s in reversed(st):
        if s["l"] == label:
            return s
    return None


def spans_of(run: Run) -> list:
    """enter/exit pairs by thread: {label, t0, t1 (None = never exited), tag, uid, x}."""
    open_, out = {}, []
    for s in run.stamps:
        lab = s["l"]
        if lab.endswith(".enter"):
            open_.setdefault((base(lab), s["u"]), []).append(s)
        elif lab.endswith(".exit"):
            stack = open_.get((base(lab), s["u"]))
            if stack:
                e = stack.pop()
                out.append({"label": base(lab), "t0": e["pc"], "t1": s["pc"], "tag": e.get("g") or s.get("g"), "uid": s["u"], "x": e.get("x") or {}})
    for (lab, uid), stack in open_.items():
        for e in stack:
            out.append({"label": lab, "t0": e["pc"], "t1": None, "tag": e.get("g"), "uid": uid, "x": e.get("x") or {}})
    out.sort(key=lambda s: s["t0"])
    return out


def closes_of(run: Run) -> list:
    """One entry per browser that was closed: the instants and the gaps (ms)."""
    by = collections.defaultdict(list)
    for s in run.stamps:
        if s.get("g"):
            by[s["g"]].append(s)
    out = []
    for tag, st in sorted(by.items()):
        enter, exit_ = first(st, "user.close.enter"), last(st, "user.close.exit")
        if not enter:
            continue
        conn = first(st, "conn.close.exit", n=1)
        api, impl = first(st, "api.Browser.close.exit"), first(st, "impl.Browser.close.exit")
        rp = first(st, "transport.reply.enter")
        c = {"tag": tag, "reply_uid": rp["u"] if rp else None, "enter": enter["pc"], "exit": exit_["pc"] if exit_ else None, "conn": conn["pc"] if conn else None,
             "api": api["pc"] if api else None, "impl": impl["pc"] if impl else None}

        def d(a, b):
            return round((c[b] - c[a]) / 1e6, 1) if c[a] is not None and c[b] is not None else None
        c["close_ms"] = d("enter", "exit")
        c["conn_ms"] = d("enter", "conn")
        c["gap_api"] = d("conn", "api")
        c["gap_user"] = d("conn", "exit")
        tl = [s for s in st if c["conn"] is not None and c["exit"] is not None and c["conn"] <= s["pc"] <= c["exit"]]
        c["timeline"] = tl
        iv = [(tl[i + 1]["pc"] - tl[i]["pc"], i) for i in range(len(tl) - 1)]
        iv.sort(reverse=True)
        c["intervals"] = [(round(dt / 1e6, 1), tl[i], tl[i + 1]) for dt, i in iv]
        out.append(c)
    return out


def _ident_tag(run: Run) -> dict:
    out = {}
    for uid, t in run.threads.items():
        if t.get("tag"):
            out[t["ident"]] = t["tag"]
    return out


def sample_report(run: Run, t0: int, t1: int, uids: list, L, F, reply_uid=None) -> None:
    """What the threads were doing between two stamps: MainThread, the threads of the two stamps, the other threads that ran Python."""
    S = [p for p in run.samples if t0 <= p["pc"] <= t1]
    L("      samples %d in the interval; sampler lateness: max dt %s ms, %d samples over 60 ms (a sampler that cannot run is GIL or CPU starvation)"
      % (len(S), max((p["dt"] for p in S), default=None), sum(1 for p in S if p["dt"] > 60)))
    cp = [c for c in run.cpus if t0 <= c["pc"] <= t1]
    if len(cp) >= 2:
        db, dt_ = cp[-1]["busy"] - cp[0]["busy"], cp[-1]["total"] - cp[0]["total"]
        L("      machine CPU busy %s%% (%d cpus), this process %.2f cores" % (round(100 * db / dt_) if dt_ > 0 else None, run.meta.get("cpus") or 0,
                                                                         (cp[-1]["proc"] - cp[0]["proc"]) / max(1, cp[-1]["pc"] - cp[0]["pc"])))
    lg = [x for x in run.lags if t0 <= x["pc"] <= t1]
    L("      the loop's own lateness over 35 ms: %d times, max %s ms" % (len(lg), max((x["ms"] for x in lg), default=None)))
    lo = sorted((x for x in run.longs if t0 <= x["pc"] <= t1 or (x["pc"] <= t0 <= x["pc"] + x["ms"] * 1e6)), key=lambda x: -x["ms"])[:5]
    for x in lo:
        L("      long call: %s %.1f ms at +%.0f ms [%s] %s" % (x["what"], x["ms"], (x["pc"] - t0) / 1e6, run.tname(x["u"]), x["site"]))
    if not S:
        return
    tid_ident = {i: (t[0], t[1]) for i, t in enumerate(run.tids)}
    want = {}
    for uid in uids:
        t = run.threads.get(uid)
        if t:
            want[t["ident"]] = t["name"] + (" (the thread that sent the close's reply)" if uid == reply_uid else "")
    main_c = collections.Counter()
    want_c = {i: collections.Counter() for i in want}
    others = collections.Counter()
    others_top = collections.Counter()
    n_other = 0
    n_run_total = 0
    itag = _ident_tag(run)
    for p in S:
        any_other = False
        for tid, state, sid in p["s"]:
            ident, name = tid_ident[tid]
            if name == "MainThread":
                main_c[(sid, state)] += 1
            if ident in want_c:
                want_c[ident][(sid, state)] += 1
            if state == "R" and name != "MainThread":
                any_other = True
                n_run_total += 1
                cls = thread_class(name)
                if name in ("invisible-reaper", "invisible-reaper-then"):
                    cls += "[%s]" % short(itag.get(ident))
                others[cls] += 1
                top = run.frame(run.stacks[sid][0]) if run.stacks[sid] else "?"
                others_top[(cls, top)] += 1
        n_other += any_other
    L("      MainThread, most frequent stacks (innermost first; state R running, W in a known wait, L on a lock):")
    for (sid, state), n in main_c.most_common(3):
        L("        %3dx [%s] %s" % (n, state, run.stack(sid, 6)))
    for (sid, state), n in main_c.most_common(8)[3:]:
        F("        %3dx [%s] %s" % (n, state, run.stack(sid, 12)))
    for (sid, state), n in main_c.most_common(3):
        F("          full: %s" % run.stack(sid, 12))
    for ident, name in want.items():
        if name == "MainThread":
            continue
        c = want_c[ident]
        if not c:
            L("      thread of a stamp: %s (ident %s): not in the samples of the interval (it had ended or had not begun)" % (name, ident))
            continue
        L("      thread of a stamp: %s (ident %s), most frequent stacks:" % (name, ident))
        for (sid, state), n in c.most_common(3):
            L("        %3dx [%s] %s" % (n, state, run.stack(sid, 6)))
        for (sid, state), n in c.most_common(6)[3:]:
            F("        %3dx [%s] %s" % (n, state, run.stack(sid, 12)))
    L("      other threads running Python (state R): in %d of %d samples at least one (%.1f per sample); by thread, samples and the commonest top frame:"
      % (n_other, len(S), n_run_total / len(S)))
    for cls, n in others.most_common(6):
        top = max(((t, c) for (k, t), c in others_top.items() if k == cls), key=lambda x: x[1])
        L("        %-34s %4d  e.g. %s" % (cls, n, top[0]))
    for cls, n in others.most_common(20)[6:]:
        top = max(((t, c) for (k, t), c in others_top.items() if k == cls), key=lambda x: x[1])
        F("        %-34s %4d  e.g. %s" % (cls, n, top[0]))


def label_of(s: dict, run: Run) -> str:
    return "%s [%s]" % (s["l"], run.tname(s["u"]))


def report_run(run: Run, L, F, rounds_seen: list) -> list:
    """The table of one round. Returns its closes."""
    cl = closes_of(run)
    sp = spans_of(run)
    r = run.rnd()
    w = run.win or {}
    L("HUNTCW1 round %s (%s): window %s ms, %d stamps, %d samples (%s), loop probe %d ticks, %d lateness events" % (
        r, run.mode(), round((w["close"] - w["open"]) / 1e6, 1) if w.get("open") and w.get("close") else None, len(run.stamps), len(run.samples),
        "sampler lateness max %s ms" % max((p["dt"] for p in run.samples), default=None), run.meta.get("ticks") or 0, len(run.lags)))
    for c in cl:
        iv = c["intervals"][0] if c["intervals"] else None
        L("HUNTCW1 %s %s %s | close() %s ms | Connection.close returned at +%s ms | Connection.close -> Browser.close (api) %s ms, -> user close() %s ms | largest interval %s"
          % (r, run.mode(), short(c["tag"]), c["close_ms"], c["conn_ms"], c["gap_api"], c["gap_user"],
             "%s ms between %s -> %s" % (iv[0], label_of(iv[1], run), label_of(iv[2], run)) if iv else None))
    for c in cl:
        gu = c["gap_user"]
        if gu is None or gu <= GAP_S * 1000:
            continue
        L("HUNTCW2 GAP %s %s %s: Connection.close returned, then %s ms to the return of Browser.close (api %s ms) / of the user's close() (%s ms)"
          % (r, run.mode(), short(c["tag"]), gu, c["gap_api"], gu))
        tl = c["timeline"]
        notable = set()
        for i in range(len(tl) - 1):
            if tl[i + 1]["pc"] - tl[i]["pc"] >= 50e6:
                notable.update((i, i + 1))
        for i, s in enumerate(tl):
            line = "      %+9.1f ms  %-34s %-26s %s%s" % ((s["pc"] - c["conn"]) / 1e6, s["l"], run.tname(s["u"]), "" if not s.get("x") else json.dumps(s["x"], default=str)[:120],
                                                        "" if i == 0 else "   (+%.1f)" % ((s["pc"] - tl[i - 1]["pc"]) / 1e6))
            (L if i in notable or i in (0, len(tl) - 1) else F)(line)
        F("      (the lines above that are not shown in the job log are all in report.txt)")
        # The intervals of 250 ms and more: what the threads were doing
        for dt, a, b in [x for x in c["intervals"] if x[0] >= 250][:3]:
            L("    interval %s ms: %s -> %s" % (dt, label_of(a, run), label_of(b, run)))
            sample_report(run, a["pc"], b["pc"], [a["u"], b["u"]] + ([c["reply_uid"]] if c.get("reply_uid") else []), L, F, c.get("reply_uid"))
            ov = [s for s in sp if s["t1"] is not None and s["t0"] < b["pc"] and s["t1"] > a["pc"] and (s["t1"] - s["t0"]) >= 100e6 and
                  not (s["t0"] <= a["pc"] and s["t1"] >= b["pc"] and s["tag"] == c["tag"])]      # not what trivially contains the interval
            ov.sort(key=lambda s: -(s["t1"] - s["t0"]))
            L("      operations of 100 ms and more overlapping it (any browser, any thread):")
            for s in ov[:8]:
                L("        %-26s %-4s %-22s %+.0f ms for %.1f ms %s" % (s["label"], short(s["tag"]), run.tname(s["uid"]), (s["t0"] - a["pc"]) / 1e6,
                                                                     (s["t1"] - s["t0"]) / 1e6, json.dumps(s["x"], default=str)[:80] if s["x"] else ""))
    rounds_seen.append(r)
    return cl


def reapers_report(run: Run, L, F) -> None:
    sp = spans_of(run)
    r, t_epoch = run.rnd(), (first(run.stamps, "epoch.start") or {}).get("pc")
    names = ("reaper.run", "reaper._continue", "reaper.step", "reaper.then", "reaper.join", "after_the_reapers", "server.after_reapers", "reaper.start", "tree.end", "tree.release", "tree._wait", "tree.wait_browser_gone", "job.wait_empty", "job.terminate", "guard.reap",
             "reap_session", "find_processes", "process.terminate", "session_files.remove", "remove_profile", "removal.close", "removal.run",
             "offloop[final_reap]", "final_reap", "offloop[_join_workers]", "server.shutdown", "server.hook", "server.after_step", "transport.request_stop",
             "transport.wait_until_stopped", "transport._settle")
    L("HUNTCW3 round %s (%s): the reapers and the final reap, each operation from the first close (start +ms, duration, thread, tag)" % (r, run.mode()))
    for s in sp:
        if s["label"] in names and t_epoch is not None:
            dur = (s["t1"] - s["t0"]) / 1e6 if s["t1"] else None
            line = "HUNTCW3   %s %-4s %-28s +%8.1f ms for %s ms [%s] %s" % (r, short(s["tag"]), s["label"], (s["t0"] - t_epoch) / 1e6,
                                                                              "%.1f" % dur if dur is not None else "NOT RETURNED", run.tname(s["uid"]),
                                                                              json.dumps(s["x"], default=str)[:110] if s["x"] else "")
            (L if (dur is None or dur >= 100 or s["label"] in ("tree.end", "reaper.run", "final_reap", "guard.reap", "reap_session", "tree.release")) else F)(line)
    # psutil and the other counted calls, by thread
    L("HUNTCW3 round %s: counted calls (psutil, Thread.start, EngineJob/EngineTree polls), per thread: calls, total ms, the call sites that took most" % r)
    groups = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0.0, 0.0]))
    for a in run.accs:
        t = run.threads.get(a["u"]) or {}
        name = t.get("name", "?")
        key = "%s[%s]" % (name, short(t.get("tag"))) if name in ("invisible-reaper", "invisible-reaper-then", "invisible-teardown") or (t.get("tag") and name.startswith("juggler")) else thread_class(name)
        site = "%s %s:%s %s" % (a["fn"], a["file"].rsplit("/", 1)[-1], a["line"], a["func"])
        g = groups[key][site]
        g[0] += a["n"]
        g[1] += a["ms"]
        g[2] = max(g[2], a["max"])
    rows = []
    for key, sites in groups.items():
        rows.append((sum(v[1] for v in sites.values()), key, sites))
    rows.sort(key=lambda x: -x[0])
    for total, key, sites in rows:
        n = sum(v[0] for v in sites.values())
        top = sorted(sites.items(), key=lambda kv: -kv[1][1])
        show = L if key.startswith(("invisible-reaper", "invisible-teardown", "MainThread", "juggler", "hunt-")) or total >= 50 else F
        show("HUNTCW3   %s %-34s %7d calls %9.1f ms | %s" % (r, key, n, total,
                                                              "; ".join("%s x%d %.1f ms (max %.1f)" % (s, v[0], v[1], v[2]) for s, v in top[:3])))
        for s, v in top[3:10]:
            F("             %-34s %s x%d %.1f ms (max %.1f)" % ("", s, v[0], v[1], v[2]))


def stats_line(mode: str, closes: list) -> str:
    gu = [c["gap_user"] for c in closes if c["gap_user"] is not None]
    ga = [c["gap_api"] for c in closes if c["gap_api"] is not None]
    cm = [c["close_ms"] for c in closes if c["close_ms"] is not None]
    return ("HUNTCWCMP %-10s | closes %2d | gaps over %.1f s: user close() %2d, Browser.close (api) %2d | gap user close() p50 %s p95 %s max %s ms | gap api p50 %s p95 %s max %s ms"
            " | close() p50 %s max %s ms") % (mode, len(closes), GAP_S, sum(1 for g in gu if g > GAP_S * 1000), sum(1 for g in ga if g > GAP_S * 1000),
                                              pct(gu, .5), pct(gu, .95), max(gu, default=None), pct(ga, .5), pct(ga, .95), max(ga, default=None),
                                              pct(cm, .5), max(cm, default=None))


def verdict_line(mode: str, closes: list, files: int) -> str:
    """HUNTCWVERDICT: the closes where (the user's close() return - Connection.close return) is over 0.5 s. The criterion is 0 in `together`;
    `sequential` is the control (printed, never judged). A close that could not be measured (no Connection.close return or no close() return)
    or fewer closes than 4 per round make the verdict INCOMPLETE: a missing close is not a pass."""
    gu = [c["gap_user"] for c in closes]
    measured = [g for g in gu if g is not None]
    over = sum(1 for g in measured if g > GAP_S * 1000)
    expected = 4 * files
    head = "HUNTCWVERDICT mode=%s closes=%d (expected %d) unmeasured=%d closes_with_gap_over_%.1fs=%d max_gap_ms=%s" % (
        mode, len(closes), expected, len(gu) - len(measured), GAP_S, over, max(measured, default=None))
    if mode != "together":
        return head + " CONTROL (not judged)"
    if len(closes) < expected or len(measured) < len(closes):
        return head + " INCOMPLETE"
    return head + (" PASS" if over == 0 else " FAIL")


def write_readable(runs: list, outdir: str, lines: list) -> None:
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "report.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(outdir, "stamps.tsv"), "w", encoding="utf-8", newline="\n") as f:
        f.write("round\tmode\tms_since_epoch_start\twall_ms\ttag\tthread\tlabel\textra\n")
        for run in runs:
            t0 = (first(run.stamps, "epoch.start") or run.stamps[0] if run.stamps else {"pc": 0})["pc"]
            for s in run.stamps:
                f.write("%s\t%s\t%.3f\t%s\t%s\t%s\t%s\t%s\n" % (run.rnd(), run.mode(), (s["pc"] - t0) / 1e6, s.get("w"), short(s.get("g")), run.tname(s["u"]), s["l"],
                                                              json.dumps(s["x"], default=str) if s.get("x") else ""))
    with open(os.path.join(outdir, "samples.txt"), "w", encoding="utf-8", newline="\n") as f:
        for run in runs:
            t_open = (run.win or {}).get("open") or 0
            itag = _ident_tag(run)
            f.write("##### round %s (%s): every sample, MainThread and every thread in state R or L (the others are idle), 12 frames innermost first\n" % (run.rnd(), run.mode()))
            for p in run.samples:
                f.write("--- +%.1f ms since the window opened, %.1f ms since the previous sample\n" % ((p["pc"] - t_open) / 1e6, p["dt"]))
                for tid, state, sid in p["s"]:
                    ident, name = run.tids[tid]
                    if name == "MainThread" or state in ("R", "L"):
                        f.write("  %s%s [%s] %s\n" % (name, "[%s]" % short(itag.get(ident)) if name.startswith("invisible-reaper") else "", state, run.stack(sid, 12)))


def load_runs(d: str) -> list:
    runs = []
    for path in sorted(glob.glob(os.path.join(d, "**", "closewhy-*.jsonl"), recursive=True)):
        try:
            run = Run(path)
        except OSError:
            continue
        if run.meta:
            runs.append(run)
    return runs


def report_main(d: str, rows_log: str = None) -> int:
    runs = load_runs(d)
    out = []

    def L(line: str = "") -> None:
        print(line)
        out.append(line)

    def F(line: str = "") -> None:
        out.append(line)
    if not runs:
        L("HUNTCW0 no closewhy file under %s" % d)
        return 1
    meta = runs[0].meta
    bad = [w for w in meta.get("wrapped", []) if w[2] != "ok"]
    L("HUNTCW0 %d rounds, mode %s, python %s on %s, %s cpus; probes %d, not as expected %d %s; pins wrapper %s core %s"
      % (len(runs), meta.get("mode"), meta.get("python"), meta.get("platform"), meta.get("cpus"), len(meta.get("wrapped", [])), len(bad), bad or "",
         (meta.get("pins") or ["?", "?"])[0][:8], (meta.get("pins") or ["?", "?"])[1][:7]))
    allc, seen = [], []
    for run in runs:
        allc += report_run(run, L, F, seen)
    for run in runs:
        reapers_report(run, L, F)
    L("HUNTCW4 the arm, all rounds:")
    L(stats_line(runs[0].mode(), allc))
    L(verdict_line(runs[0].mode(), allc, len(runs)))
    if rows_log and os.path.exists(rows_log):
        text = open(rows_log, encoding="utf-8", errors="replace").read()
        rows = []
        for m in re.finditer(r"HUNTROW \[([\w-]+)\] ", text):
            try:
                rows.append(_DEC.raw_decode(text, m.end())[0])
            except ValueError:
                pass            # a row broken by an interleaved write: the others are read
        ok = [x for x in rows if "crash" not in x]
        L("HUNTCW0 rows of the hunt: %d browsers closed, %d crash rows, %d close errors, %d errors before the close; close() ms p50 %s max %s"
          % (len(ok), len(rows) - len(ok), sum(1 for x in ok if x.get("close_error")), sum(1 for x in ok if x.get("error_before_close")),
             pct([x.get("close_ms") for x in ok], .5), max((x.get("close_ms") or 0 for x in ok), default=None)))
    write_readable(runs, os.path.join(d, "readable"), out)
    print("HUNTCW0 the readable files are in %s: report.txt (everything the job log shortens), stamps.tsv, samples.txt" % os.path.join(d, "readable"))
    return 0


def compare_main(d: str) -> int:
    runs = load_runs(d)
    by = collections.defaultdict(list)
    for run in runs:
        by[run.mode()] += closes_of(run)
    if not by:
        print("HUNTCW0 no closewhy file under %s" % d)
        return 1
    print("HUNTCWCMP together against sequential (gaps over %.1f s; gap = Connection.close returned -> the call returned; one entry per close)" % GAP_S)
    for mode in ("together", "sequential"):
        if mode in by:
            print(stats_line(mode, by[mode]))
        else:
            print("HUNTCWCMP %-10s | no data (the job did not leave a file)" % mode)
    files = collections.Counter(run.mode() for run in runs)
    for mode in ("together", "sequential"):
        if mode in by:
            print(verdict_line(mode, by[mode], files[mode]))
    for mode in ("together", "sequential"):
        gaps = sorted(((c["gap_user"], c["tag"]) for c in by.get(mode, []) if c["gap_user"] is not None), reverse=True)[:8]
        print("HUNTCWCMP %-10s | the largest gaps (ms, browser): %s" % (mode, gaps))
    return 0


# ---------------------------------------------------------------------------------------------------------------------------------
# A synthetic round, through the report
# ---------------------------------------------------------------------------------------------------------------------------------
def synthetic(d: str, mode: str, rnd: str, gap_ms: float) -> None:
    """Records in the schema `build_records` writes: 4 browsers, one of which has the reply of its close delayed by `gap_ms` while the main
    thread sat in a psutil call; a reaper of another browser runs `tree.end` for as long."""
    pc0 = 10_000_000_000
    ms = lambda v: int(pc0 + v * 1e6)       # noqa: E731
    recs = [{"k": "meta", "mode": mode, "arm": "cw-" + mode, "round": rnd, "n": 4, "pc0": pc0, "wall0_ms": 1.7e12, "python": "3.12.0", "platform": "win32", "cpus": 4,
             "pid": 1, "wrapped": [["a", "x.py:1", "ok"], ["b", "y.py:2", "LINE 3, expected 2"]], "pins": ["e" * 40, "c" * 40], "ticks": 400},
            {"k": "win", "open": ms(100), "close": ms(100 + 3000 + gap_ms), "users": 4}]
    threads = {1: {"name": "MainThread", "ident": 11, "tag": None}, 2: {"name": "juggler-0", "ident": 22, "tag": "inproc-%s-0" % rnd},
               3: {"name": "invisible-reaper", "ident": 33, "tag": "inproc-%s-1" % rnd}, 4: {"name": "hunt-treewatch", "ident": 44, "tag": None},
               5: {"name": "juggler-shutdown", "ident": 55, "tag": "inproc-%s-0" % rnd}}
    recs.append({"k": "threads", "t": threads})
    st = []

    def s(t, label, tag, uid, **x):
        o = {"k": "s", "pc": ms(t), "w": 1.7e12 + t, "l": label, "g": tag, "u": uid}
        if x:
            o["x"] = x
        st.append(o)
    t0 = 100
    s(0, "epoch.start", "inproc-%s-0" % rnd, 1)
    for i in range(4):
        tag = "inproc-%s-%d" % (rnd, i)
        o = i * 5
        if mode == "sequential":
            o = i * 1500
        s(t0 + o, "user.close.enter", tag, 1)
        s(t0 + o + 1, "api.Browser.close.enter", tag, 1)
        s(t0 + o + 2, "client.send.close.exit", tag, 1, id=7)
        s(t0 + o + 3, "server.handle.close.enter", tag, 2, id=7)
        s(t0 + o + 4, "conn.close.enter", tag, 2, n=1)
        s(t0 + o + 1400, "conn.close.exit", tag, 2, n=1)
        s(t0 + o + 1401, "transport.reply.enter", tag, 2, id=7)
        s(t0 + o + 1402, "transport.reply.exit", tag, 2, id=7)
        late = gap_ms if i == 0 else 0
        s(t0 + o + 1403 + late, "client.dispatch.enter", tag, 1, id=7, close_reply=True)
        s(t0 + o + 1404 + late, "api.Browser.close.exit", tag, 1)
        s(t0 + o + 1500 + late, "user.close.exit", tag, 1)
        if i == 1:
            s(t0 + o + 1405, "tree.end.enter", tag, 3, grace=1.0)
            s(t0 + o + 1405 + gap_ms, "tree.end.exit", tag, 3)
    recs += st
    recs.append({"k": "long", "pc": ms(t0 + 1500), "ms": 900.0, "what": "Process.cmdline", "site": "process.py:1112 _survives", "u": 3})
    recs.append({"k": "acc", "u": 3, "fn": "Process.cmdline", "file": "invisible_core/process.py", "line": 1112, "func": "_survives", "n": 40, "ms": 1200.0, "max": 900.0})
    recs.append({"k": "acc", "u": 4, "fn": "Process.cpu_times", "file": "scripts/hunt_published_latency.py", "line": 1236, "func": "run", "n": 9000, "ms": 800.0, "max": 3.0})
    recs.append({"k": "frames", "t": [["a/threading.py", 1, "wait"], ["a/windows_events.py", 2, "_poll"], ["a/process.py", 3, "_survives"], ["a/x.py", 4, "run"]],
                 "states": ["W", "W", "R", "R"]})
    recs.append({"k": "stacks", "t": [[0, 3], [1], [2, 3]]})
    recs.append({"k": "tids", "t": [[11, "MainThread"], [33, "invisible-reaper"], [44, "hunt-treewatch"], [22, "juggler-0"]]})
    n = int((3000 + gap_ms) / 20)
    for k in range(n):
        recs.append({"k": "p", "pc": ms(100 + k * 20), "dt": 20.0 + (400.0 if k == 40 else 0), "s": [[0, "W", 1], [1, "R", 2], [2, "R", 2], [3, "W", 0]]})
    for k in range(0, n, 10):
        recs.append({"k": "cpu", "pc": ms(100 + k * 20), "proc": 1_000_000_000 + k * 40_000_000, "busy": 10 + k * 0.15, "total": 10 + k * 0.2})
    recs.append({"k": "lag", "pc": ms(t0 + 1500), "ms": 880.0})
    write_records(os.path.join(d, "closewhy-%s-%s-1.jsonl" % (mode, rnd)), recs)


def selftest() -> int:
    d = tempfile.mkdtemp(prefix="closewhy-selftest-")
    for mode, gap in (("together", 2300.0), ("sequential", 0.0)):
        for rnd in ("r0", "r1"):
            os.makedirs(os.path.join(d, "art-" + mode), exist_ok=True)
            synthetic(os.path.join(d, "art-" + mode), mode, rnd, gap if rnd == "r0" else 0.0)
    # A file with an interleaved write inside (two objects on a line, a broken one between): everything else is still read.
    with open(os.path.join(d, "art-together", "closewhy-together-r1-1.jsonl"), "a", encoding="utf-8") as f:
        f.write('{"k":"lag","pc":1,"ms":1.0}{"k":"lag","pc":2,"ms":2.0}\n{"k":"lag","pc":3,"m\n{"k":"lag","pc":4,"ms":4.0}\n')
    assert [o["pc"] for o in iter_json('x{"pc":1}{"pc":\n{"pc":2} tail')] == [1, 2]
    rc = report_main(os.path.join(d, "art-together"))
    rc |= report_main(os.path.join(d, "art-sequential"))
    rc |= compare_main(d)
    runs = load_runs(os.path.join(d, "art-together"))
    gaps = [c["gap_user"] for run in runs for c in closes_of(run)]
    assert any(g and g > 2000 for g in gaps), gaps
    assert os.path.exists(os.path.join(d, "art-together", "readable", "report.txt"))
    print("selftest ok, files in " + d)
    return rc


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "run":
        run_main(sys.argv[2:])
    elif cmd == "report":
        sys.exit(report_main(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
    elif cmd == "compare":
        sys.exit(compare_main(sys.argv[2]))
    elif cmd == "selftest":
        sys.exit(selftest())
    else:
        sys.exit(__doc__)
