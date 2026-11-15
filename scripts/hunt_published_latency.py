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
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import psutil

ARRIVED = {}
EVALS = 100
GOTOS = 10
SEED = 42
TEXT = "someone@example.com"
#: The page stamps every keydown, keyup, mousemove and click with performance.timeOrigin + performance.now() in the handler.
TYPE_PAGE = (b"<!doctype html><html><body style='margin:0'><div style='height:300px'></div><input id=f style='margin-left:300px'>"
             b"<script>window.__k=[];window.__u=[];window.__m=[];window.__c=[];const T=()=>performance.timeOrigin+performance.now();"
             b"document.addEventListener('keydown',e=>__k.push([e.key,T()]),true);document.addEventListener('keyup',e=>__u.push([e.key,T()]),true);"
             b"document.addEventListener('mousemove',e=>__m.push(T()),true);document.addEventListener('click',e=>__c.push(T()),true);</script></body></html>")


def typing_plan():
    """The core's plan for the session's first field: (dwell, gap) per character, for the persona of SEED and typing nonce 1."""
    from invisible_core.juggler._behaviour import TypingPersona, plan_typing
    return plan_typing(TEXT, TypingPersona.from_seed(SEED), nonce=1)


def typing_report(arm, keys, ups, moves, clicks, plan):
    """What the page saw against the plan: per key the slip of its keydown (page time from the first keydown, against the planned
    time); keys over 100 ms from the plan; compressions (a gap between two keydowns under half the planned gap, the catch-up after a
    stall); the mouse: the longest gap between two mousemoves and the bursts (gaps under 2 ms)."""
    downs = [t for k, t in keys if k != "Shift"]
    planned, t = [], 0.0
    for dwell, gap in plan:
        planned.append(t)
        t += dwell + gap
    out = {"keys_seen": len(downs), "keys_planned": len(plan)}
    if downs and len(downs) == len(plan):
        page = [x - downs[0] for x in downs]
        slip = [p - q for p, q in zip(page, planned)]
        rel = [abs(s) for s in slip]
        gaps_p = [page[i + 1] - page[i] for i in range(len(page) - 1)]
        gaps_q = [planned[i + 1] - planned[i] for i in range(len(planned) - 1)]
        out.update({"slip_abs_p50": round(sorted(rel)[len(rel) // 2], 1), "slip_abs_max": round(max(rel), 1),
                    "keys_over_100ms": sum(1 for s in rel if s > 100), "slip_last": round(slip[-1], 1),
                    "compressions": sum(1 for a, b in zip(gaps_p, gaps_q) if b >= 40 and a < 0.5 * b)})
    mg = [moves[i + 1] - moves[i] for i in range(len(moves) - 1)]
    out.update({"mousemoves": len(moves), "mouse_gap_max": round(max(mg), 1) if mg else None,
                "mouse_bursts_under_2ms": sum(1 for g in mg if g < 2), "clicks": len(clicks)})
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/type"):
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


SLOW = []          # (kind, tag, t0, t1) in time.monotonic(): every event over SLOW_MS
SLOW_MS = 1000


def proc_kind(p):
    try:
        name = p.name().lower()
        if "firefox" in name or "plugin-container" in name:
            cmd = p.cmdline()
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
            self.cpu.append((now, row))
            time.sleep(0.2)

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
        return {"samples": n, "top_stacks": [[k, v] for k, v in top], "cpu_s_by_kind": by_kind}


SAMPLER = StackSampler()


async def work(page, port, tag):
    goto = []
    for i in range(GOTOS):
        t0, m0 = time.time() * 1000, time.monotonic()
        await page.goto("http://127.0.0.1:%d/p?b=%s&i=%d" % (port, tag, i), wait_until="load", timeout=30000)
        arrived = ARRIVED.get((tag, str(i)))
        goto.append(round(arrived - t0, 1) if arrived else None)
        if arrived and arrived - t0 > SLOW_MS:
            SLOW.append(("goto->request", tag, m0, m0 + (arrived - t0) / 1000))
    ev = []
    for _ in range(EVALS):
        t0 = time.monotonic()
        await page.evaluate("1 + 1")
        ev.append(round((time.monotonic() - t0) * 1000, 2))
        if ev[-1] > SLOW_MS:
            SLOW.append(("evaluate", tag, t0, time.monotonic()))
    return goto, ev


async def typing(page, port, arm):
    """A click into a field (with the cursor's path when humanised) and the fixed text: ours through press_sequentially, which
    follows the core's plan; stock through the same plan driven by hand (it has none of its own: only the runner's delay)."""
    plan = typing_plan()
    await page.goto("http://127.0.0.1:%d/type" % port, wait_until="load", timeout=30000)
    m0 = time.monotonic()
    if arm == "stock":
        await page.mouse.move(10, 10)
        await page.click("#f")
        for (dwell, gap), ch in zip(plan, TEXT):
            if ch == "@":
                await page.keyboard.down("Shift")
            await page.keyboard.down(ch)
            await asyncio.sleep(dwell / 1000)
            await page.keyboard.up(ch)
            if ch == "@":
                await page.keyboard.up("Shift")
            await asyncio.sleep(gap / 1000)
    else:
        await page.locator("#f").click()
        await page.locator("#f").press_sequentially(TEXT)
    took = time.monotonic() - m0
    keys, ups, moves, clicks = await page.evaluate("[window.__k, window.__u, window.__m, window.__c]")
    rep = typing_report(arm, keys, ups, moves, clicks, plan)
    rep["typing_wall_ms"] = round(took * 1000)
    return rep


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


async def one_browser(arm, binary, port, tag):
    row = {"arm": arm, "tag": tag}
    t0 = time.monotonic()
    if arm == "inproc":
        from invisible_playwright.async_api import InvisiblePlaywright
        extra = json.loads(os.environ.get("HUNT_EXTRA_PREFS") or "{}") or None
        cm = InvisiblePlaywright(seed=42, binary_path=binary, humanize=True, headless=True, extra_prefs=extra)
        browser = await cm.__aenter__()
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

        async def close(browser=browser, pw=pw):
            await browser.close()
            await pw.stop()
    page = await browser.new_page()
    row["launch_ms"] = round((time.monotonic() - t0) * 1000)
    if row["launch_ms"] > SLOW_MS:
        SLOW.append(("launch", tag, t0, time.monotonic()))
    row["goto_req_ms"], row["eval_ms"] = await work(page, port, tag)
    try:
        row["typing"] = await typing(page, port, arm)
    except Exception as exc:   # noqa: BLE001
        row["typing"] = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
    t1 = time.monotonic()
    await close()
    row["close_ms"] = round((time.monotonic() - t1) * 1000)
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
    for mod, attrs in (("invisible_core._geoip_db", ["ensure_geoip_mmdb"]),
                       ("invisible_core.juggler._profile", ["remove_profile"]),
                       ("invisible_core.process", ["reap_session"])):
        try:
            m = importlib.import_module(mod)
            for a in attrs:
                timed(m, a, mod.rsplit(".", 1)[-1] + "." + a)
        except Exception as exc:   # noqa: BLE001
            PROBE.append(("probe-error " + mod, "", 0, 0, repr(exc)[:200]))
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
    sampler = CpuSampler()
    sampler.t.start()
    SAMPLER.start()
    if arm in ("inproc", "remote") and os.environ.get("HUNT_PROBES") == "1":
        install_probes()
    t0, t_mono0 = time.time(), time.monotonic()
    rows = await asyncio.gather(*(one_browser(arm, binary, port, "%s-%s-%d" % (arm, rnd, i)) for i in range(n)), return_exceptions=True)
    cpu = sampler.total()
    for r in rows:
        if isinstance(r, Exception):
            print("HUNTROW " + json.dumps({"arm": arm, "round": rnd, "crash": repr(r)[:800]}), flush=True)
            continue
        r.update({"round": rnd, "n": n, "os": sys.platform, "wall_s": round(time.time() - t0, 1), "cpu_s_per_browser": round(cpu / n, 2)})
        print("HUNTROW " + json.dumps(r), flush=True)
    SAMPLER.stop.set()
    for name, thread, start, ms, extra in PROBE:
        print("HUNTPROBE " + json.dumps({"arm": arm, "round": rnd, "os": sys.platform, "name": name, "thread": thread,
                                         "t": round(start - t_mono0, 3), "ms": ms, "extra": extra}), flush=True)
    for kind, tag, a, b in SLOW:
        print("HUNTSLOW " + json.dumps({"arm": arm, "round": rnd, "os": sys.platform, "kind": kind, "tag": tag, "ms": round((b - a) * 1000),
                                        **SAMPLER.explain(a, b)}), flush=True)
    httpd.shutdown()


if __name__ == "__main__":
    if sys.argv[1] == "serve":
        serve_one(sys.argv[2])
        sys.exit(0)
    asyncio.run(main(sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None))
