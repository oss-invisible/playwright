"""HUNT ONLY, never merged: why a closed persistent session sometimes did not keep its localStorage.

The red: tests/test_persistent_profile_keeps_state.py::test_cookies_and_local_storage_survive_to_the_next_session (Windows, run
43777526437): in the same run the cookie reached cookies.sqlite and the localStorage did not ("localStorage did not survive").
The hypothesis under test: the loss goes with a browser that the core TERMINATED when its shutdown deadline ran out
(Connection.close(timeout=5.0)), against one that left by itself after the pipe was closed.

    python scripts/hunt_close_keeps_localstorage.py --cycles 120 --arms 0,5,30 --jobs 4 --out result.json [--binary FIREFOX]
    python scripts/hunt_close_keeps_localstorage.py --summarize result-linux.json result-windows.json

One cycle is the test, with a page of this process's own server instead of a site (http on 127.0.0.1: the origin is
http://127.0.0.1:PORT, one server and so one origin for the whole cycle):
  1. InvisiblePlaywright(profile_dir=<new>, seed=11), the page, localStorage.setItem + a cookie, both read back;
  2. wait ARM seconds (the arm: 0 is the test as written, 5 and 30 give LSNG's flush its interval), then close (`with` exit);
  3. read the disk of the closed profile (copies, so reading cannot checkpoint anything): storage/default/<origin>/ls/data.sqlite
     (size, WAL, rows, the `database.usage` row, the usage file) and cookies.sqlite;
  4. the same profile again: localStorage and cookie as the page and the context see them.

Per cycle the row records how the browser left (from the other hunt's probes, hunt_published_latency.py, imported and not copied):
  - the close split in (a) close -> quit (the first "Starting phase" line of the engine's AsyncShutdown log) and (b) quit -> main
    process gone, -> tree gone; the real parent pid; the engine's shutdown lines (filtered); the `--backgroundtask` processes with
    their birth, life and how each ended; the probes of the core's close steps (Connection.close, EngineTree.end/release,
    SessionFiles.remove, reap_session ...);
  - where the CORE DECIDES to terminate (invisible_core/juggler/connection.py, Connection.close, deadline = 5 s after the pipe closed):
    it waits for `p.poll()` and, on Windows, `EngineTree.wait_job_empty(deadline)`; then `p.terminate()` if the spawned process still
    runs, then `EngineTree.release()` which ends the job (`EngineJob.terminate`) when `job.active()` is not zero; later the wrapper's
    `reap(token)` -> `reap_session` -> `EngineTree.end` (POSIX: SIGTERM then SIGKILL to the engine's process group). This script wraps
    those calls (EngineJob.terminate, EngineTree._signal_group / end, the process classes' terminate) and records each with the
    processes alive at that instant, by kind: a kill that found the browser's own process (ff:parent) is `terminated by the core`;
    one that found only --backgroundtask processes (the shutdown-ping sender) ended a leftover and the browser had left by itself;
  - the browser's exit code (Windows: psutil wait of the ff:parent processes; POSIX: the engine's returncode; 1 or a signal = killed).

The summary (per OS and arm): the 2x2 table of localStorage lost against how the browser left, then the cookie losses, and whether a
lost localStorage had a row on disk (read side) or not (never written).

--jobs N runs N workers (processes), each with its own profile and its own server port, as xdist does: the 60 minutes of the job do
not hold 100 serial cycles with a 30 s arm.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SEED = 11
KEY = "kept"
PAGE = b"<!doctype html><title>ls</title><body>ls</body>"
#: What a worker prints for one finished cycle (the orchestrator reads these lines).
ROW = "HUNTLS "
#: A cycle that takes longer than this is hung: the worker says so and ends (the orchestrator starts another with what is left).
CYCLE_LIMIT_S = 300
SHUT_PREFS = {"toolkit.asyncshutdown.log": True, "browser.dom.window.dump.enabled": True}
#: The lines of the engine's shutdown log kept in a row: the phases, the blockers and what names storage.
KEEP_LINE = ("phase", "quota", "storage", "localstorage", "profile-before-change", "completed blocker", "adding blocker", "quit")
#: The probes (label of hunt_published_latency.PROBE) that belong to a close.
CLOSE_PROBES = ("Connection.close", "EngineTree.end", "EngineTree.wait_job_empty", "EngineTree.release", "SessionFiles.remove",
                "process.reap_session", "_profile.terminate", "process.terminate", "remove_profile")

_H = None


def hunt():
    """The other session's hunt module (scripts/hunt_published_latency.py), imported by name."""
    global _H
    if _H is None:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import hunt_published_latency as h
        _H = h
    return _H


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(PAGE)


# ---- what the core does at the end of a close, as events -------------------------------------------------------------------------
EVENTS = []
PROCS = []        # the process objects the core spawned (Connection.__init__'s `process`), newest last
_WRAPPED = {"done": False}


def _live(pids):
    """[(pid, kind)] of the given pids that are alive, by the other hunt's kind (ff:parent, ff:backgroundtask, ff:gpu ...)."""
    import psutil
    out = []
    for pid in pids:
        try:
            p = psutil.Process(pid)
            if p.is_running():
                out.append((pid, hunt().proc_kind(p)))
        except Exception:   # noqa: BLE001
            pass
    return out


def _tree_pids(tree):
    """The pids of an EngineTree right now: its job's list on Windows, the members of its process group on POSIX."""
    pids = set()
    try:
        job = getattr(tree, "job", None)
        if job is not None and getattr(job, "handle", None):
            pids |= set(job.pids())
        elif getattr(tree, "group", False):
            pids |= {p.pid for p in tree._group_members()}
        else:
            pids |= {p.pid for p in tree.members()}
    except Exception:   # noqa: BLE001
        pass
    return pids


def _event(what, live, **extra):
    kinds = sorted({k for _p, k in live})
    EVENTS.append({"what": what, "wall_ms": time.time() * 1000, "caller": sys._getframe(3).f_code.co_name, "live": kinds,
                   "browser_alive": "ff:parent" in kinds, **extra})


def install_end_probes():
    """Wrap, without changing what they do, the calls with which the core ends a browser: a record of each, with who was alive."""
    if _WRAPPED["done"]:
        return
    _WRAPPED["done"] = True
    import functools
    import importlib
    cmod = importlib.import_module("invisible_core.juggler.connection")
    pmod = importlib.import_module("invisible_core.process")

    def wrap(owner, name, before):
        real = getattr(owner, name, None)
        if real is None:
            return

        @functools.wraps(real)
        def call(self, *a, **k):
            try:
                before(self, *a, **k)
            except Exception:   # noqa: BLE001
                pass
            return real(self, *a, **k)
        setattr(owner, name, call)

    # Windows: the spawned process (the launcher stub or the engine) asked to end while it still runs.
    for cls_name in ("_WindowsProcess", "_PosixProcess"):
        cls = getattr(cmod, cls_name, None)
        if cls is not None:
            def before_terminate(self, *a, _n=cls_name, **k):
                if self.poll() is None:
                    live = _live([self.pid])
                    tree = getattr(self, "tree", None)
                    live = live or [(self.pid, "ff:parent")]
                    _event("process.terminate", live, cls=_n, pid=self.pid, tree_alive=sorted(_live(_tree_pids(tree))) if tree else None)
            wrap(cls, "terminate", before_terminate)
            wrap(cls, "kill", before_terminate)
    # Windows: the job ended with processes in it (Connection.close -> EngineTree.release, or reap -> EngineTree.end).
    job_cls = getattr(pmod, "EngineJob", None)
    if job_cls is not None:
        def before_job_terminate(self, *a, **k):
            pids = self.pids() if self.handle else []
            if pids:
                _event("job.terminate", _live(pids), active=len(pids))
        wrap(job_cls, "terminate", before_job_terminate)
        real_wait = job_cls.wait_empty

        @functools.wraps(real_wait)
        def wait_empty(self, deadline):
            ok = real_wait(self, deadline)
            if not ok:
                _event("job.wait_empty_timeout", _live(self.pids()) if self.handle else [], ok=False)
            return ok
        job_cls.wait_empty = wait_empty
    # POSIX: the engine's process group signalled (SIGTERM, then SIGKILL).
    tree_cls = getattr(pmod, "EngineTree", None)
    if tree_cls is not None and hasattr(tree_cls, "_signal_group"):
        def before_signal(self, number, *a, **k):
            if self._group_alive():
                _event("signal_group", _live(_tree_pids(self)), signal=int(number))
        wrap(tree_cls, "_signal_group", before_signal)
    # Every browser the core spawns: its process object, for the exit code.
    real_init = cmod.Connection.__init__

    @functools.wraps(real_init)
    def init(self, *a, **k):
        real_init(self, *a, **k)
        proc = k.get("process") if "process" in k else (a[2] if len(a) > 2 else None)
        if proc is not None:
            PROCS.append(proc)
    cmod.Connection.__init__ = init


def install_all():
    h = hunt()
    h.install_output_capture()
    h.install_probes()
    install_end_probes()


# ---- the disk ------------------------------------------------------------------------------------------------------------------
def _copy_db(path, scratch):
    """A copy of a sqlite file with its WAL (what a reader sees), in `scratch`; None when the file is not there."""
    if not os.path.exists(path):
        return None
    dest = os.path.join(scratch, os.path.basename(os.path.dirname(os.path.dirname(path))) + "-" + os.path.basename(path))
    shutil.copy2(path, dest)
    for suffix in ("-wal",):
        if os.path.exists(path + suffix):
            shutil.copy2(path + suffix, dest + suffix)
    return dest


def _file(path):
    try:
        return {"bytes": os.path.getsize(path)}
    except OSError:
        return None


def read_disk(profile):
    """The profile as the next session will find it, read from copies: localStorage's data.sqlite per origin directory, and the cookie."""
    out = {"ls": {}, "cookie": None, "storage_dirs": []}
    scratch = tempfile.mkdtemp(prefix="hunt_ls_disk_")
    try:
        base = os.path.join(profile, "storage", "default")
        try:
            out["storage_dirs"] = sorted(os.listdir(base))
        except OSError as exc:
            out["storage_dirs_error"] = "%s: %s" % (type(exc).__name__, exc)
        for origin in out["storage_dirs"]:
            ls_dir = os.path.join(base, origin, "ls")
            data = os.path.join(ls_dir, "data.sqlite")
            if not os.path.exists(data):
                continue
            rec = {"data": _file(data), "wal": _file(data + "-wal"), "shm": _file(data + "-shm"), "usage_file": None, "rows": None,
                   "key_row": None, "usage": None, "error": None}
            try:
                with open(os.path.join(ls_dir, "usage"), "rb") as fh:
                    raw = fh.read()
                rec["usage_file"] = {"hex": raw.hex(), "int_be": int.from_bytes(raw, "big") if raw else None}
            except OSError:
                pass
            try:
                copy = _copy_db(data, scratch)
                con = sqlite3.connect(copy)
                try:
                    rec["rows"] = con.execute("select count(*) from data").fetchone()[0]
                    row = con.execute("select key, value from data where key = ?", (KEY,)).fetchone()
                    if row:
                        raw = row[1]
                        rec["key_row"] = {"key": row[0], "value_hex": bytes(raw).hex() if isinstance(raw, (bytes, bytearray, memoryview)) else None,
                                          "value": raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw}
                    try:
                        rec["usage"] = [list(r) for r in con.execute("select origin, usage from database")]
                    except sqlite3.Error as exc:
                        rec["usage_error"] = str(exc)[:120]
                finally:
                    con.close()
            except Exception as exc:   # noqa: BLE001
                rec["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            out["ls"][origin] = rec
        cookies = os.path.join(profile, "cookies.sqlite")
        rec = {"file": _file(cookies), "wal": _file(cookies + "-wal"), "rows": None, "row": None, "error": None}
        try:
            copy = _copy_db(cookies, scratch)
            if copy:
                con = sqlite3.connect(copy)
                try:
                    rec["rows"] = con.execute("select count(*) from moz_cookies").fetchone()[0]
                    row = con.execute("select name, value, host, expiry from moz_cookies where name = ?", (KEY,)).fetchone()
                    rec["row"] = list(row) if row else None
                finally:
                    con.close()
        except Exception as exc:   # noqa: BLE001
            rec["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
        out["cookie"] = rec
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return out


def ls_row_on_disk(disk):
    return any((rec.get("key_row") or {}).get("value") == "yes" for rec in disk["ls"].values())


def cookie_row_on_disk(disk):
    row = (disk.get("cookie") or {}).get("row")
    return bool(row) and row[1] == "yes"


# ---- one cycle -----------------------------------------------------------------------------------------------------------------
def kill_leftovers():
    """Every process under this one, with the tree (a browser left by a failed cycle)."""
    import psutil
    try:
        kids = psutil.Process().children(recursive=True)
    except psutil.Error:
        return 0
    for p in kids:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(kids, timeout=10)
    return len(kids)


def browser_exit_codes(h):
    """Windows: threads waiting on the browser's own processes (ff:parent in the job or under this process), started before the close.
    Returns (codes dict filled by the threads, the threads). POSIX: nothing (the core reaps its child; the code is the process'
    returncode, read after the close)."""
    import psutil
    codes, threads = {}, []
    if sys.platform != "win32":
        return codes, threads
    pids = set()
    if PROCS:
        pids |= _tree_pids(getattr(PROCS[-1], "tree", None))
    try:
        pids |= {c.pid for c in psutil.Process().children(recursive=True)}
    except psutil.Error:
        pass
    for pid in pids:
        try:
            p = psutil.Process(pid)
            if h.proc_kind(p) != "ff:parent":
                continue
        except psutil.Error:
            continue

        def wait(p=p):
            try:
                codes[p.pid] = p.wait(timeout=95)
            except Exception as exc:   # noqa: BLE001
                codes[p.pid] = "%s" % type(exc).__name__
        t = threading.Thread(target=wait, daemon=True, name="hunt-rc")
        t.start()
        threads.append(t)
    return codes, threads


def reset_capture():
    h = hunt()
    del h.ENGINE_LINES[:]
    del h.ENGINE_PIDS[:]
    del h.TREE_PIDS[:]
    del h.PROBE[:]
    del EVENTS[:]
    del PROCS[:]


def session(binary, profile, url, hold_s, write, arm, tag):
    """One session on `profile`. write=True: write both stores, hold `hold_s`, close and record how the browser left. write=False: read."""
    h = hunt()
    from invisible_playwright import InvisiblePlaywright
    reset_capture()
    row = {}
    sv = h.SurvivorWatch(time.time() * 1000, lambda: {pid: tag for pid in set(h.ENGINE_PIDS) | set(h.TREE_PIDS)}) if write else None
    if sv:
        sv.start()
    t0 = time.monotonic()
    cm = InvisiblePlaywright(headless=True, seed=SEED, binary_path=binary, profile_dir=profile,
                             extra_prefs=dict(SHUT_PREFS) if os.environ.get("HUNT_LS_SHUTLOG", "1") == "1" else None)
    ctx = cm.__enter__()
    try:
        row["launch_ms"] = round((time.monotonic() - t0) * 1000)
        page = ctx.new_page()
        page.goto(url, timeout=60000)
        if write:
            page.evaluate("() => { localStorage.setItem('%s', 'yes'); document.cookie = '%s=yes; max-age=86400; SameSite=Lax'; }" % (KEY, KEY))
            row["written_ls"] = page.evaluate("localStorage.getItem('%s')" % KEY)
            row["written_cookie_in_context"] = ("kept", "yes") in [(c["name"], c["value"]) for c in ctx.cookies()]
            row["write_wall_ms"] = time.time() * 1000
            if hold_s:
                page.wait_for_timeout(hold_s * 1000)
        else:
            row["ls"] = page.evaluate("localStorage.getItem('%s')" % KEY)
            row["ls_length"] = page.evaluate("localStorage.length")
            row["document_cookie"] = page.evaluate("document.cookie")
            row["context_cookies"] = [[c["name"], c["value"]] for c in ctx.cookies()]
    except BaseException:
        try:
            cm.__exit__(None, None, None)
        finally:
            raise
    row["session_s_at_close"] = round(time.monotonic() - t0, 2)
    watch = rc_codes = rc_threads = None
    if write:
        watch = h.ExitWatch()
        watch.start()
        sv.fast.set()
        row["parent_pids"] = [p.pid for p in watch.main]
        rc_codes, rc_threads = browser_exit_codes(h)
    t_close_wall = time.time() * 1000
    t_close_mono = time.monotonic()
    cm.__exit__(None, None, None)
    row["close_ms"] = round((time.monotonic() - t_close_mono) * 1000)
    if not write:
        return row
    watch.join(95)
    bg_recs = sv.finish()
    for t in rc_threads:
        t.join(2)
    main_gone, tree_gone = watch.main_gone, watch.tree_gone
    fields, shown = h.shut_report(h.ENGINE_LINES, t_close_wall, main_gone, tree_gone)
    row.update(fields)
    kept = [[t, q, x[:140]] for t, q, x in shown if any(k in x.lower() for k in KEEP_LINE)]
    row["shutdown_lines"] = [k for k in kept if k[0] < 0][:20] + [k for k in kept if k[0] >= 0][:70]
    bgs = h.bg_report(bg_recs, t_close_wall, "ls", arm, tag)
    row["bg_tasks"] = [{k: b[k] for k in ("pid", "born_ms", "life_ms", "end_ms", "rc", "ended_by", "ended_in", "attrib")} for b in bgs]
    row["probes_ms"] = {}
    for label, _thread, start, ms, _extra in h.PROBE:
        if label in CLOSE_PROBES and start >= t_close_mono - 0.05:
            row["probes_ms"][label] = round(row["probes_ms"].get(label, 0) + ms, 1)
    deadline_hit = row["probes_ms"].get("Connection.close", 0) >= 4900
    # How it left: the kills of the core, with who was alive at each.
    kills = [{"what": e["what"], "t_ms": round(e["wall_ms"] - t_close_wall, 1), "caller": e["caller"], "live": e["live"],
              "browser_alive": e["browser_alive"], **{k: e[k] for k in ("signal", "active", "cls", "ok") if k in e}} for e in EVENTS]
    row["core_events"] = kills
    browser_killed = any(e["browser_alive"] for e in kills if e["what"] != "job.wait_empty_timeout")
    leftover_ended = any(not e["browser_alive"] for e in kills if e["what"] in ("job.terminate", "signal_group"))
    if tree_gone is None:
        how = "unknown"
    elif browser_killed:
        how = "terminated"
    else:
        how = "self"
    row["exit_how"] = how
    row["leftover_ended_by_core"] = leftover_ended
    row["deadline_hit"] = bool(deadline_hit or any(e["what"] == "job.wait_empty_timeout" for e in kills))
    code = None
    if PROCS:
        try:
            code = PROCS[-1].poll()
        except Exception:   # noqa: BLE001
            code = None
    row["spawned_exit_code"] = code
    row["browser_exit_codes"] = {str(k): v for k, v in (rc_codes or {}).items()}
    row["close_to_tree_gone_ms"] = round(tree_gone - t_close_wall, 1) if tree_gone else None
    row["close_wall_ms"] = round(t_close_wall, 1)
    return row


def cycle(binary, idx, arm_s, worker):
    """One full cycle; returns the row."""
    h = hunt()
    tag = "c%03d" % idx
    hold = int(arm_s)
    row = {"cycle": idx, "arm": arm_s, "hold_s": hold, "worker": worker, "os": sys.platform, "started_wall_s": round(time.time(), 1)}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    url = "http://127.0.0.1:%d/" % port
    row["origin"] = "http://127.0.0.1:%d" % port
    profile = tempfile.mkdtemp(prefix="hunt_ls_profile_")
    t_start = time.monotonic()
    try:
        try:
            row["first"] = session(binary, profile, url, hold, True, arm_s, tag)
        except BaseException as exc:   # noqa: BLE001
            row["error"] = "first session: %s: %s" % (type(exc).__name__, str(exc)[:300])
        row["leftover_after_first"] = kill_leftovers()
        row["disk"] = read_disk(profile)
        row["disk_ls_row"] = ls_row_on_disk(row["disk"])
        row["disk_cookie_row"] = cookie_row_on_disk(row["disk"])
        if "error" not in row:
            try:
                row["second"] = session(binary, profile, url, 0, False, arm_s, tag)
                s = row["second"]
                row["ls_lost"] = s.get("ls") != "yes"
                row["cookie_lost"] = not ("kept=yes" in (s.get("document_cookie") or "") and ["kept", "yes"] in (s.get("context_cookies") or []))
            except BaseException as exc:   # noqa: BLE001
                row["error"] = "second session: %s: %s" % (type(exc).__name__, str(exc)[:300])
        row["leftover_after_second"] = kill_leftovers()
    finally:
        httpd.shutdown()
        shutil.rmtree(profile, ignore_errors=True)
    row["cycle_s"] = round(time.monotonic() - t_start, 1)
    return row


# ---- the worker ----------------------------------------------------------------------------------------------------------------
def worker_main(args):
    plan = json.loads(args.plan)
    install_all()
    state = {"t": time.monotonic(), "idx": None}

    def watchdog():
        while True:
            time.sleep(5)
            if state["idx"] is not None and time.monotonic() - state["t"] > CYCLE_LIMIT_S:
                import faulthandler
                faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
                print(ROW + json.dumps({"cycle": state["idx"], "worker": args.worker, "os": sys.platform,
                                        "error": "cycle over %d s: hung" % CYCLE_LIMIT_S}), flush=True)
                kill_leftovers()
                os._exit(4)
    threading.Thread(target=watchdog, daemon=True, name="hunt-watchdog").start()
    for idx, arm_s in plan:
        if time.time() > args.stop_at:
            print("worker %s: the budget is spent, no new cycle" % args.worker, flush=True)
            break
        state["t"], state["idx"] = time.monotonic(), idx
        row = cycle(args.binary, idx, arm_s, args.worker)
        state["idx"] = None
        print(ROW + json.dumps(row), flush=True)
    kill_leftovers()


# ---- the orchestrator ----------------------------------------------------------------------------------------------------------
def make_plan(cycles, arms, jobs):
    """{worker: [(cycle, arm)]}: cycle i goes to worker i % jobs, and the arm of a worker's k-th cycle is shifted by the worker, so the
    arms run side by side and each worker has every arm."""
    plan = {w: [] for w in range(jobs)}
    for i in range(cycles):
        w, k = i % jobs, i // jobs
        plan[w].append((i, arms[(k + w) % len(arms)]))
    return plan


def run_worker_slot(w, todo, args, rows, lock, stop_at):
    """Runs `todo` on a worker process; when one ends before it is done (a hung cycle, a crash), starts another with what is left."""
    for attempt in range(1, 6):
        if not todo or time.time() > stop_at:
            return
        cmd = [sys.executable, "-u", os.path.abspath(__file__), "worker", "--worker", str(w), "--plan", json.dumps(todo),
               "--stop-at", str(stop_at)] + (["--binary", args.binary] if args.binary else [])
        print("slot %d: worker process %d with %d cycles" % (w, attempt, len(todo)), flush=True)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        done = set()
        for line in proc.stdout:
            if line.startswith(ROW):
                row = json.loads(line[len(ROW):])
                with lock:
                    rows.append(row)
                done.add(row.get("cycle"))
                print("slot %d cycle %s arm %s: ls_lost=%s cookie_lost=%s exit=%s close_ms=%s %s" % (
                    w, row.get("cycle"), row.get("arm"), row.get("ls_lost"), row.get("cookie_lost"),
                    (row.get("first") or {}).get("exit_how"), (row.get("first") or {}).get("close_ms"),
                    ("ERROR " + row["error"]) if row.get("error") else ""), flush=True)
            else:
                sys.stdout.write("[w%d] %s" % (w, line))
                sys.stdout.flush()
        proc.wait()
        todo = [(i, a) for i, a in todo if i not in done]
        if proc.returncode == 0:
            return


def orchestrate(args):
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    for a in arms:
        int(a)
    plan = make_plan(args.cycles, arms, args.jobs)
    stop_at = time.time() + args.budget_min * 60
    rows, lock, threads = [], threading.Lock(), []
    t0 = time.time()
    for w, todo in plan.items():
        t = threading.Thread(target=run_worker_slot, args=(w, todo, args, rows, lock, stop_at), name="slot-%d" % w)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    rows.sort(key=lambda r: r.get("cycle", -1))
    meta = {"os": sys.platform, "cycles_planned": args.cycles, "cycles_done": len(rows), "arms": arms, "jobs": args.jobs,
            "wall_s": round(time.time() - t0), "budget_min": args.budget_min, "python": sys.version.split()[0],
            "core": core_version(), "wrapper": wrapper_version()}
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"meta": meta, "cycles": rows}, fh)
    print(summary([{"meta": meta, "cycles": rows}]))
    return 0


def core_version():
    try:
        import importlib.metadata as m
        return m.version("invisible-core")
    except Exception:   # noqa: BLE001
        return None


def wrapper_version():
    try:
        import importlib.metadata as m
        return m.version("invisible-playwright")
    except Exception:   # noqa: BLE001
        return None


# ---- the summary ---------------------------------------------------------------------------------------------------------------
def summary(docs):
    """Per OS and arm: the 2x2 of localStorage lost against how the browser left, the cookie losses, the disk's part in them."""
    lines = []
    for doc in docs:
        meta, rows = doc["meta"], doc["cycles"]
        lines.append("== %s: %s of %s cycles (jobs %s, %ss), core %s, wrapper %s" % (
            meta.get("os"), meta.get("cycles_done"), meta.get("cycles_planned"), meta.get("jobs"), meta.get("wall_s"), meta.get("core"),
            meta.get("wrapper")))
        arms = sorted({r.get("arm") for r in rows if r.get("arm") is not None}, key=int) + [None]
        for arm in arms:
            sel = [r for r in rows if arm is None or r.get("arm") == arm]
            if not sel:
                continue
            good = [r for r in sel if "error" not in r]
            errs = [r for r in sel if "error" in r]
            title = "arm %ss before the close" % arm if arm is not None else "all arms"
            lines.append("-- %s: %d cycles, %d with an error" % (title, len(sel), len(errs)))
            for e in errs[:3]:
                lines.append("   error: %s" % e["error"][:160])

            def how(r):
                return (r.get("first") or {}).get("exit_how", "unknown")
            lines.append("   localStorage lost vs how the browser left       kept  lost")
            for label, key in (("exited by itself", "self"), ("terminated by the core", "terminated"), ("not known", "unknown")):
                cell = [r for r in good if how(r) == key]
                if not cell and key == "unknown":
                    continue
                lines.append("   %-44s %4d  %4d" % (label, sum(1 for r in cell if not r.get("ls_lost")), sum(1 for r in cell if r.get("ls_lost"))))
            lost = [r for r in good if r.get("ls_lost")]
            lines.append("   localStorage lost: %d of %d | with its row on disk after the close: %d, without: %d" % (
                len(lost), len(good), sum(1 for r in lost if r.get("disk_ls_row")), sum(1 for r in lost if not r.get("disk_ls_row"))))
            clost = [r for r in good if r.get("cookie_lost")]
            lines.append("   cookie lost: %d of %d | row on disk: %d, without: %d | cookie lost by how it left: self %d, terminated %d" % (
                len(clost), len(good), sum(1 for r in clost if r.get("disk_cookie_row")), sum(1 for r in clost if not r.get("disk_cookie_row")),
                sum(1 for r in clost if how(r) == "self"), sum(1 for r in clost if how(r) == "terminated")))
            closes = sorted(r["first"]["close_ms"] for r in good if (r.get("first") or {}).get("close_ms") is not None)
            if closes:
                lines.append("   close ms p50 %d p95 %d max %d | deadline hit (5 s): %d | background tasks ended by the core: %d" % (
                    closes[len(closes) // 2], closes[min(len(closes) - 1, int(.95 * len(closes)))], closes[-1],
                    sum(1 for r in good if (r.get("first") or {}).get("deadline_hit")),
                    sum(1 for r in good if (r.get("first") or {}).get("leftover_ended_by_core"))))
        lost_rows = [r for r in rows if r.get("ls_lost")][:6]
        for r in lost_rows:
            f = r.get("first") or {}
            lines.append("   LOST cycle %s arm %ss: exit %s, close %s ms (a %s, b %s), disk ls row %s, usage %s, events %s" % (
                r["cycle"], r["arm"], f.get("exit_how"), f.get("close_ms"), f.get("a_close_to_quit_ms"), f.get("b_quit_to_tree_gone_ms"),
                r.get("disk_ls_row"), [(o[:30], v.get("usage")) for o, v in (r.get("disk") or {}).get("ls", {}).items()],
                [(e["what"], e["live"]) for e in f.get("core_events", [])][:4]))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", choices=["worker"], help=argparse.SUPPRESS)
    ap.add_argument("--cycles", type=int, default=100, help="cycles in all, split over the arms (default 100)")
    ap.add_argument("--arms", default="5,30", help="seconds between the write and the close, comma separated (0 = the test as written)")
    ap.add_argument("--jobs", type=int, default=1, help="worker processes at once, each with its own profile and port (default 1)")
    ap.add_argument("--budget-min", type=float, default=45.0, help="no new cycle starts after this many minutes (default 45)")
    ap.add_argument("--binary", default=os.environ.get("INVPW_BINARY_PATH") or os.environ.get("STEALTHFOX_E2E_BINARY"),
                    help="the engine's executable (default: INVPW_BINARY_PATH, else the wrapper's own choice)")
    ap.add_argument("--out", default="close-keeps-localstorage.json", help="where the JSON of every cycle is written")
    ap.add_argument("--summarize", nargs="+", metavar="JSON", help="print the summary of result files and exit")
    ap.add_argument("--worker", default="0", help=argparse.SUPPRESS)
    ap.add_argument("--plan", help=argparse.SUPPRESS)
    ap.add_argument("--stop-at", type=float, default=float("inf"), help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.summarize:
        docs = []
        for path in args.summarize:
            with open(path, encoding="utf-8") as fh:
                docs.append(json.load(fh))
        print(summary(docs))
        return 0
    if args.mode == "worker":
        worker_main(args)
        return 0
    return orchestrate(args)


if __name__ == "__main__":
    sys.exit(main())
