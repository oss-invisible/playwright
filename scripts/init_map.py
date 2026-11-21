#!/usr/bin/env python3
"""A MAP of what a launch does, from the user's call to the first page, with the time of every step.

One question: where does the time of `InvisiblePlaywright(...)` go, from the call to a page that has loaded a first page? It is a measurement and nothing else: no
threshold, no verdict, and nothing of the product is changed. The product is measured from the outside: each function that is a step is wrapped by a timer that
calls the original (`time.perf_counter_ns` before and after, the thread's name), installed on the modules' attributes in the worker process only. A step the product
renames makes a target that is not found: it is said in the report (`unpatched`), never silently skipped.

Two modes of one file.

  driver (default)   starts a worker process per launch (a fresh interpreter each: the import of the packages is a step of a launch), `--runs` times on a warm
                     cache after one warm-up launch that is not counted, for each scenario; then ONE launch on a cold cache (a new cache directory, the engine kept: the
                     engine's download is not this map's, the workflow times `fetch` apart); then ONE launch under cProfile; then writes `init-map-<system>.json` and
                     `init-map-<system>.md` in `--out`.
  worker (--worker)  one launch: imports, the wrapped steps, `InvisiblePlaywright(...)` -> `__enter__` -> `browser.new_page()` -> two `goto` of a page on 127.0.0.1 ->
                     `__exit__`, and writes its spans as JSON.

Scenarios (`--scenarios`):

  direct    `InvisiblePlaywright(seed=...)`: no proxy, the exit asked of the internet (what a user without a proxy pays), the engine on the surface of the system.
  proxy     the same behind a SOCKS5 gateway of this machine (`tests/_socks_servers.py`, the fake web's: no real proxy; it forwards the question of the exit for real): the TCP exit is asked THROUGH the proxy and the
            UDP exit is measured beside it, on a thread (the product's decision: they stay parallel).
  headless  `headless=True`: the hidden surface (an Xvfb on Linux, a Win32 desktop on Windows) is made by the launch.

What a time is. Every time is milliseconds of `time.perf_counter_ns`, relative to the instant before `InvisiblePlaywright(...)` is called. A step's `self` time is its
time minus the time of the steps inside it. A step runs `in parallel` with another when they are not one inside the other and they overlap in half of the runs or more.
The tree is made by containment of the intervals, so a span that a background thread makes while the caller waits sits under whichever step was open (its thread is in
the column `thread`).
"""
from __future__ import annotations

import argparse
import contextlib
import functools
import importlib
import json
import math
import os
import platform
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve()

# ── what is wrapped ─────────────────────────────────────────────────────────
# (module, "Class.attr" or "attr", label). The labels are the names of the steps in the map. An entry whose target is not found is reported as `unpatched`.
TARGETS: List[Tuple[str, str, str]] = [
    # the wrapper: the three calls of the user and what `__enter__` does
    ("invisible_playwright.launcher", "InvisiblePlaywright.__init__", "ctor"),
    ("invisible_playwright.launcher", "generate_profile", "ctor.generate_profile"),
    ("invisible_playwright.launcher", "guard_for", "ctor.lifetime_guard"),
    ("invisible_playwright._session", "declared_network", "ctor.declared_network"),
    ("invisible_playwright.launcher", "InvisiblePlaywright.__enter__", "enter"),
    ("invisible_playwright.launcher", "prepare_session_geo", "geo"),
    ("invisible_core._geo", "discover_egress_ip", "geo.tcp_egress_discovery"),
    ("invisible_core._udp_exit", "start_measurement", "geo.udp_measurement_start"),
    ("invisible_core._udp_exit", "PendingMeasurement._run", "geo.udp_probe_thread"),
    ("invisible_core._udp_exit", "PendingMeasurement.result", "geo.udp_join"),
    ("invisible_core._geo", "ensure_geoip_mmdb", "geo.geoip_database"),
    ("invisible_core._geo", "ip_to_timezone", "geo.ip_to_timezone"),
    ("invisible_core._geo", "ip_to_coordinates", "geo.ip_to_coordinates"),
    ("invisible_core._geo", "position_to_declare", "geo.declared_position"),
    ("invisible_core._geo", "_decide", "geo.decide_locale"),
    ("invisible_playwright.launcher", "resolve_executable", "engine_check"),
    ("invisible_playwright._engine", "ensure_binary", "engine_check.ensure_binary"),
    ("invisible_core.download", "read_stamp", "engine_check.read_stamp"),
    ("invisible_core.download", "verify_engine", "engine_check.verify_engine"),
    ("invisible_core.seal", "read_engine_identity", "engine_check.read_engine_identity"),
    ("invisible_core.seal", "engine_problems", "engine_check.engine_problems"),
    ("invisible_playwright._session", "CommonLaunch._resolve_headless", "resolve_headless"),
    ("invisible_playwright._session", "make_virtual_display", "resolve_headless.make_virtual_display"),
    ("invisible_playwright._session", "CommonLaunch._build_prefs", "build_prefs"),
    ("invisible_playwright._session", "compose_session_prefs", "build_prefs.compose_session_prefs"),
    ("invisible_playwright.launcher", "_configure_proxy_shared", "configure_proxy"),
    ("invisible_playwright._session", "CommonLaunch._build_env", "build_env"),
    ("invisible_playwright._session", "resolve_executable", "build_env.engine_check_again"),
    ("invisible_playwright._session", "verify_font_manifest", "build_env.verify_font_manifest"),
    ("invisible_playwright._session", "cached_font_manifest_path", "build_env.font_manifest_file"),
    ("invisible_playwright._session", "build_launch_env", "build_env.build_launch_env"),
    # the client and the in-process server
    ("invisible_playwright._pw.sync_api._context_manager", "PlaywrightContextManager.__enter__", "playwright_start"),
    ("invisible_playwright._pw.sync_api._generated", "BrowserType.launch", "client_launch"),
    ("invisible_playwright._pw.sync_api._generated", "BrowserType.launch_persistent_context", "client_launch_persistent"),
    ("invisible_playwright._juggler.server", "BrowserTypeDispatcher.op_launch", "server_launch"),
    ("invisible_playwright._juggler.server", "session_for_launch", "server_launch.session_for_launch"),
    ("invisible_core.juggler._profile", "SessionFiles.__init__", "server_launch.session_files"),
    ("invisible_playwright._juggler.server", "take_session_motion", "server_launch.take_session_motion"),
    ("invisible_playwright._juggler.server", "write_user_js", "write_profile"),
    ("invisible_core._intermediates", "ensure_intermediates", "write_profile.intermediates_list"),
    ("invisible_playwright._juggler.server", "launch_proxy_command", "server_launch.launch_proxy_command"),
    ("invisible_core.juggler.connection", "launch", "engine_start"),
    ("invisible_core._engine_env", "compose", "engine_start.compose_environment"),
    ("invisible_core.juggler.connection", "_spawn_windows", "engine_start.spawn_process"),
    ("invisible_core.juggler.connection", "_spawn_posix", "engine_start.spawn_process"),
    ("invisible_core.juggler.connection", "ProcessOutput.wait_for_ready", "engine_start.wait_listening"),
    ("invisible_core.juggler.connection", "Connection.__init__", "engine_start.connection_init"),
    ("invisible_core.juggler.connection", "Connection.send_placed", "send"),
    ("invisible_playwright._juggler.server", "read_version", "server_launch.read_version"),
    ("invisible_playwright._juggler.server", "BrowserDispatcher.__init__", "server_launch.browser_dispatcher"),
    ("invisible_playwright.launcher", "assert_wire_version", "assert_wire_version"),
    ("invisible_playwright.launcher", "InvisiblePlaywright._patch_new_context_defaults", "patch_new_context_defaults"),
    ("invisible_playwright._session", "CommonLaunch._arm_cursor_engine", "arm_cursor_engine"),
    # the first page
    ("invisible_playwright._pw.sync_api._generated", "Browser.new_page", "client_new_page"),
    ("invisible_playwright.launcher", "InvisiblePlaywright._assert_uscita_invariata", "egress_recheck"),
    ("invisible_playwright._juggler.server", "BrowserDispatcher.op_new_context", "server_new_context"),
    ("invisible_playwright._juggler.server", "BrowserDispatcher._apply_context_options", "server_new_context.apply_options"),
    ("invisible_playwright._juggler.server", "BrowserContextDispatcher.op_new_page", "server_new_page"),
    ("invisible_playwright._juggler.server", "BrowserDispatcher._wait_for_page", "server_new_page.wait_for_page"),
    # the first goto
    ("invisible_playwright._pw.sync_api._generated", "Page.goto", "client_goto"),
    ("invisible_playwright._juggler.server", "FrameDispatcher.op_goto", "server_goto"),
    # the end (not part of the map: told for the record)
    ("invisible_playwright.launcher", "InvisiblePlaywright.__exit__", "exit"),
]

#: Steps that run on a thread of their own by design; the others are in the line of the caller.
BACKGROUND = {"geo.udp_probe_thread"}

#: One line for each step: what it does.
DESC = {
    "ctor": "constructor: seed, profile (fingerprint) and guard",
    "ctor.generate_profile": "the profile generated from the seed",
    "ctor.lifetime_guard": "the process lifetime guard",
    "ctor.declared_network": "the network declared by the seed",
    "enter": "__enter__: everything up to the browser being ready",
    "geo": "prepare_session_geo: exit, zone, language, position",
    "geo.tcp_egress_discovery": "discovery of the exit IP (TCP, an HTTPS request)",
    "geo.udp_measurement_start": "start of the UDP measurement thread",
    "geo.udp_probe_thread": "UDP measurement of the exit (a thread, beside the TCP discovery)",
    "geo.udp_join": "wait for the UDP measurement and its verdict",
    "geo.geoip_database": "GeoIP database (cache, daily check)",
    "geo.ip_to_timezone": "zone from the IP",
    "geo.ip_to_coordinates": "coordinates from the IP",
    "geo.declared_position": "position declared from the seed",
    "geo.decide_locale": "language decided from the exit",
    "engine_check": "choice and verification of the engine (the seal)",
    "engine_check.ensure_binary": "ensure_binary: the engine's cache",
    "engine_check.read_stamp": "read the cache stamp",
    "engine_check.verify_engine": "verify_engine: the engine against the seal",
    "engine_check.read_engine_identity": "application.ini and provenance of the engine",
    "engine_check.engine_problems": "comparison with the seal",
    "resolve_headless": "hidden surface (headless=True)",
    "resolve_headless.make_virtual_display": "makes the Xvfb / Win32 desktop",
    "build_prefs": "composition of the prefs",
    "build_prefs.compose_session_prefs": "the core's compose_session_prefs",
    "configure_proxy": "proxy configuration",
    "build_env": "construction of the environment",
    "build_env.engine_check_again": "second verification of the engine (made by _build_env)",
    "build_env.verify_font_manifest": "the font list against the engine",
    "build_env.font_manifest_file": "the font list file",
    "build_env.build_launch_env": "the process environment",
    "playwright_start": "sync_playwright().start(): in-process server and client",
    "client_launch": "BrowserType.launch, client side",
    "client_launch_persistent": "launch_persistent_context, client side",
    "server_launch": "op_launch of the in-process server",
    "server_launch.session_for_launch": "the launch's session",
    "server_launch.session_files": "profile and temporary directories",
    "server_launch.take_session_motion": "takes the session prefs out",
    "write_profile": "writing user.js",
    "write_profile.intermediates_list": "the intermediates list (cache)",
    "server_launch.launch_proxy_command": "the proxy command",
    "engine_start": "engine start (connection.launch)",
    "engine_start.compose_environment": "the engine's environment (_engine_env.compose)",
    "engine_start.spawn_process": "process creation",
    "engine_start.wait_listening": "wait for the engine to be ready (the listening line)",
    "engine_start.connection_init": "Juggler connection",
    "send": "Juggler message (round trip)",
    "server_launch.read_version": "the engine's version",
    "server_launch.browser_dispatcher": "BrowserDispatcher (Browser.enable)",
    "assert_wire_version": "version on the wire against the seal",
    "patch_new_context_defaults": "context defaults",
    "arm_cursor_engine": "cursor engine",
    "client_new_page": "browser.new_page(), client side",
    "egress_recheck": "check of the exit (with a proxy only)",
    "server_new_context": "first context",
    "server_new_context.apply_options": "context options",
    "server_new_page": "first page",
    "server_new_page.wait_for_page": "wait for Page.ready",
    "client_goto": "page.goto, client side",
    "server_goto": "goto, server side",
    "exit": "closing (outside the map)",
}


# ── the worker ──────────────────────────────────────────────────────────────
class Recorder:
    def __init__(self) -> None:
        self.spans: List[Dict[str, Any]] = []
        self.lock = threading.Lock()

    def add(self, label: str, t0: int, t1: int, extra: Optional[str] = None) -> None:
        span = {"label": label, "t0": t0, "t1": t1, "thread": threading.current_thread().name}
        if extra:
            span["extra"] = extra
        with self.lock:
            self.spans.append(span)


def _timed(recorder: Recorder, label: str, fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = time.perf_counter_ns()
        extra = None
        if label == "send":
            method = args[1] if len(args) >= 2 else kwargs.get("method")
            extra = method if isinstance(method, str) else None
        try:
            return fn(*args, **kwargs)
        finally:
            recorder.add(label if extra is None else "send:" + extra, start, time.perf_counter_ns())
    return wrapper


def install(recorder: Recorder) -> List[str]:
    """Wrap every target; the labels of the targets that were not found."""
    unpatched: List[str] = []
    for module_name, dotted, label in TARGETS:
        try:
            module = importlib.import_module(module_name)
            owner: Any = module
            parts = dotted.split(".")
            for part in parts[:-1]:
                owner = getattr(owner, part)
            original = getattr(owner, parts[-1])
            setattr(owner, parts[-1], _timed(recorder, label, original))
        except Exception as exc:  # noqa: BLE001
            unpatched.append("%s %s -> %s: %s" % (module_name, dotted, label, type(exc).__name__))
    return unpatched


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"<!doctype html><title>init map</title><p>hello</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def worker(args: argparse.Namespace) -> int:
    started = time.perf_counter_ns()
    result: Dict[str, Any] = {"status": "error", "scenario": args.scenario}
    out = Path(args.out)
    try:
        t = time.perf_counter_ns()
        import invisible_core  # noqa: F401
        marks = {"import_core": time.perf_counter_ns() - t}
        t = time.perf_counter_ns()
        from invisible_playwright.sync_api import InvisiblePlaywright
        import invisible_playwright.launcher  # noqa: F401
        import invisible_playwright._juggler.server  # noqa: F401
        marks["import_wrapper"] = time.perf_counter_ns() - t
        result["marks_ns"] = marks
        recorder = Recorder()
        if args.dry:
            _dry()
        result["unpatched"] = install(recorder)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Page)
        threading.Thread(target=httpd.serve_forever, daemon=True, name="init-map-page").start()
        url = "http://127.0.0.1:%d/" % httpd.server_address[1]

        kwargs: Dict[str, Any] = {"seed": args.seed}
        if args.scenario == "headless":
            kwargs["headless"] = True
        if args.scenario == "proxy":
            kwargs["proxy"] = {"server": args.proxy}
            if args.stun:
                kwargs["udp_stun_servers"] = [args.stun]
        profiler = None
        if args.profile:
            import cProfile
            profiler = cProfile.Profile()
        instance = None
        entered = False
        call_start = time.perf_counter_ns()
        try:
            if profiler:
                profiler.enable()
            instance = InvisiblePlaywright(**kwargs)
            browser = instance.__enter__()
            entered = True
            page = browser.new_page()
            call_end = time.perf_counter_ns()
            if profiler:
                profiler.disable()
            t = time.perf_counter_ns()
            page.goto(url + "?1")
            recorder.add("goto1", t, time.perf_counter_ns())
            t = time.perf_counter_ns()
            page.goto(url + "?2")
            recorder.add("goto2", t, time.perf_counter_ns())
            result["status"] = "ok"
            result["call_ns"] = [call_start, call_end]
        finally:
            if profiler:
                with contextlib.suppress(Exception):
                    profiler.disable()
            if instance is not None:
                with contextlib.suppress(Exception):
                    if entered:
                        instance.__exit__(None, None, None)
                    else:
                        instance._teardown()
        if profiler and args.profile_out:
            import io
            import pstats
            text = io.StringIO()
            for key in ("cumulative", "tottime"):
                stats = pstats.Stats(profiler, stream=text)
                text.write("\n=== sorted by %s (main thread only; the first 45) ===\n" % key)
                stats.sort_stats(key).print_stats(45)
            Path(args.profile_out).write_text(text.getvalue(), encoding="utf-8")
        result["spans"] = recorder.spans
        result["call_ns"] = result.get("call_ns") or [call_start, time.perf_counter_ns()]
    except BaseException as exc:  # noqa: BLE001
        import traceback
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
        result["traceback"] = traceback.format_exc()[-3000:]
        if "recorder" in locals():
            result["spans"] = recorder.spans
            result.setdefault("call_ns", [started, time.perf_counter_ns()])
    result["python"] = sys.version.split()[0]
    out.write_text(json.dumps(result), encoding="utf-8")
    return 0


def _dry() -> None:
    """Local check of the wrapping only (never in a workflow): the engine is not started, `launch` raises after the steps before it."""
    import invisible_core.juggler.connection as connection
    import invisible_playwright._engine as engine
    import invisible_playwright._session as session
    import invisible_playwright.launcher as launcher

    class Dry(RuntimeError):
        pass

    def fake_launch(*a, **k):
        raise Dry("dry run: the engine is not started")

    fake = lambda *a, **k: Path(sys.executable)  # noqa: E731
    for owner in (engine, session, launcher):
        setattr(owner, "resolve_executable", fake)
    connection.launch = fake_launch


# ── the driver ──────────────────────────────────────────────────────────────
def kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        with contextlib.suppress(Exception):
            proc.kill()


def run_worker(scenario: str, seed: int, workdir: Path, tag: str, *, proxy: str = "", stun: str = "", env_extra: Optional[Dict[str, str]] = None,
               profile: bool = False, timeout: int = 300) -> Dict[str, Any]:
    out = workdir / ("run-%s.json" % tag)
    cmd = [sys.executable, "-u", str(HERE), "--worker", "--scenario", scenario, "--seed", str(seed), "--out", str(out)]
    if proxy:
        cmd += ["--proxy", proxy]
    if stun:
        cmd += ["--stun", stun]
    if profile:
        cmd += ["--profile", "--profile-out", str(workdir / ("profile-%s.txt" % tag))]
    env = dict(os.environ, **(env_extra or {}))
    kwargs: Dict[str, Any] = {"env": env, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT}
    if os.name != "nt":
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    try:
        text, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        text, _ = proc.communicate()
        return {"status": "timeout", "scenario": scenario, "log": (text or b"").decode("utf-8", "replace")[-2000:]}
    if proc.poll() is None:
        kill_tree(proc)
    if not out.exists():
        return {"status": "no-result", "scenario": scenario, "log": (text or b"").decode("utf-8", "replace")[-2000:]}
    data = json.loads(out.read_text(encoding="utf-8"))
    if data.get("status") != "ok":
        data["log"] = (text or b"").decode("utf-8", "replace")[-1500:]
    return data


def build_run(data: Dict[str, Any]) -> Dict[str, Any]:
    """One run's spans as a tree, in ms relative to the call."""
    call0, call1 = data["call_ns"]
    spans = []
    for s in data.get("spans", []):
        if s["t1"] < call0:
            continue
        spans.append({"label": s["label"], "t0": (s["t0"] - call0) / 1e6, "t1": (s["t1"] - call0) / 1e6, "thread": s["thread"]})
    spans.append({"label": "call", "t0": 0.0, "t1": (call1 - call0) / 1e6, "thread": "MainThread"})
    spans.sort(key=lambda s: (s["t0"], -s["t1"]))
    stack: List[Dict[str, Any]] = []
    for s in spans:
        if s["label"] in BACKGROUND:
            # not in the line of the caller: it hangs under the step it started inside, and never holds another open
            holder = [p for p in stack if p["t0"] <= s["t0"] and s["t1"] <= p["t1"] + 1e-9]
            s["parent"] = holder[-1] if holder else None
            s["bg"] = True
            continue
        while stack and not (stack[-1]["t0"] <= s["t0"] and s["t1"] <= stack[-1]["t1"] + 1e-9):
            stack.pop()
        s["parent"] = stack[-1] if stack else None
        s["bg"] = False
        stack.append(s)
    seen: Dict[Tuple[int, str], int] = {}
    for s in spans:
        parent = s["parent"]
        key = (id(parent), s["label"])
        seen[key] = seen.get(key, 0) + 1
        s["path"] = ((parent["path"] + "/") if parent else "") + s["label"] + ("" if seen[key] == 1 else "#%d" % seen[key])
        s["depth"] = (parent["depth"] + 1) if parent else 0
    for s in spans:
        kids = [k["t1"] - k["t0"] for k in spans if k["parent"] is s and not k["bg"]]
        s["self"] = max(0.0, (s["t1"] - s["t0"]) - sum(kids))
    marks = data.get("marks_ns", {})
    return {"spans": spans, "call_ms": (call1 - call0) / 1e6,
            "import_core_ms": marks.get("import_core", 0) / 1e6, "import_wrapper_ms": marks.get("import_wrapper", 0) / 1e6}


def pct(values: List[float], q: float) -> float:
    v = sorted(values)
    return v[max(0, math.ceil(q * len(v)) - 1)]


def ancestors(s: Dict[str, Any]) -> set:
    out = set()
    while s["parent"] is not None:
        s = s["parent"]
        out.add(s["path"])
    return out


def aggregate(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(runs)
    by_path: Dict[str, Dict[str, Any]] = {}
    for r in runs:
        for s in r["spans"]:
            e = by_path.setdefault(s["path"], {"path": s["path"], "label": s["label"], "depth": s["depth"], "dur": [], "self": [], "start": [],
                                                "threads": set(), "bg": s["bg"], "parent": s["parent"]["path"] if s["parent"] else None})
            e["dur"].append(s["t1"] - s["t0"])
            e["self"].append(s["self"])
            e["start"].append(s["t0"])
            e["threads"].add(s["thread"])
    # overlaps: two steps neither of which holds the other, overlapping in half of the runs or more
    overlap_runs: Dict[Tuple[str, str], int] = {}
    for r in runs:
        spans = [s for s in r["spans"] if s["t1"] - s["t0"] >= 2.0]
        up = {s["path"]: ancestors(s) for s in spans}
        for i, a in enumerate(spans):
            for b in spans[i + 1:]:
                if a["path"] in up[b["path"]] or b["path"] in up[a["path"]]:
                    continue
                common = min(a["t1"], b["t1"]) - max(a["t0"], b["t0"])
                if common >= max(1.0, 0.5 * min(a["t1"] - a["t0"], b["t1"] - b["t0"])):
                    key = tuple(sorted((a["path"], b["path"])))
                    overlap_runs[key] = overlap_runs.get(key, 0) + 1
    parallel: Dict[str, List[str]] = {}
    for (a, b), count in overlap_runs.items():
        if count * 2 >= n:
            parallel.setdefault(a, []).append(b)
            parallel.setdefault(b, []).append(a)
    total = [r["import_core_ms"] + r["import_wrapper_ms"] + r["call_ms"] + _goto1(r) for r in runs]
    total_median = statistics.median(total)
    steps = []
    for e in by_path.values():
        steps.append({
            "path": e["path"], "label": e["label"], "depth": e["depth"], "parent": e["parent"], "count": len(e["dur"]),
            "median_ms": round(statistics.median(e["dur"]), 3), "p90_ms": round(pct(e["dur"], 0.9), 3),
            "min_ms": round(min(e["dur"]), 3), "max_ms": round(max(e["dur"]), 3),
            "self_median_ms": round(statistics.median(e["self"]), 3),
            "start_median_ms": round(statistics.median(e["start"]), 3),
            "pct_total": round(100.0 * statistics.median(e["dur"]) / total_median, 2) if total_median else 0.0,
            "thread": ",".join(sorted(e["threads"])), "background": e["bg"],
            "in_parallel_with": sorted(parallel.get(e["path"], [])),
        })
    steps.sort(key=lambda x: (x["start_median_ms"], x["depth"]))
    return {
        "runs": n,
        "total_ms": {"median": round(total_median, 1), "p90": round(pct(total, 0.9), 1), "min": round(min(total), 1), "max": round(max(total), 1)},
        "import_core_ms": _stat([r["import_core_ms"] for r in runs]),
        "import_wrapper_ms": _stat([r["import_wrapper_ms"] for r in runs]),
        "call_ms": _stat([r["call_ms"] for r in runs]),
        "steps": _in_tree_order(steps),
    }


def _goto1(r: Dict[str, Any]) -> float:
    for s in r["spans"]:
        if s["label"] == "goto1":
            return s["t1"] - s["t0"]
    return 0.0


def _stat(values: List[float]) -> Dict[str, float]:
    return {"median": round(statistics.median(values), 2), "p90": round(pct(values, 0.9), 2), "min": round(min(values), 2), "max": round(max(values), 2)}


def _in_tree_order(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    children: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for s in steps:
        children.setdefault(s["parent"], []).append(s)
    out: List[Dict[str, Any]] = []

    def walk(parent: Optional[str]) -> None:
        for s in sorted(children.get(parent, []), key=lambda x: x["start_median_ms"]):
            out.append(s)
            walk(s["path"])

    walk(None)
    return out


def describe(label: str) -> str:
    if label.startswith("send:"):
        return "Juggler: " + label[5:]
    return DESC.get(label, "")


def markdown(report: Dict[str, Any]) -> str:
    meta = report["meta"]
    lines = ["# Map of the launch of invisible (%s)" % meta["system"], ""]
    lines.append("A measurement, not a verdict: no threshold. Times in milliseconds, median and p90 of %s launches on a warm cache (one warm-up launch that is not counted; every launch in a new interpreter)." % report["config"]["runs"])
    lines.append("")
    lines.append("- system: %s, Python %s, %s CPUs" % (meta["platform"], meta["python"], meta["cpus"]))
    lines.append("- core %s, wrapper %s, engine %s (%s)" % (meta["core"], meta["wrapper"], meta["engine_tag"], meta["engine_upstream"]))
    lines.append("- run: %s" % meta.get("run_url", "local"))
    if meta.get("fetch_seconds") is not None:
        lines.append("- download of the engine on a cold cache (`fetch`, outside the map): %.1f s" % meta["fetch_seconds"])
    lines.append("- time 0 is the instant before `InvisiblePlaywright(...)`; the total is the import + the call up to the first page + the first goto")
    if meta.get("unpatched"):
        lines.append("- steps that could not be measured (targets not found): %s" % "; ".join(sorted(set(meta["unpatched"]))))
    lines.append("")
    for name, sc in report["scenarios"].items():
        lines += _scenario_md("Scenario `%s` (warm cache)" % name, sc)
    cold = report.get("cold")
    if cold:
        lines += _scenario_md("Cold cache, scenario `direct`: ONE launch (a new cache directory: GeoIP database, intermediates list, font files; the engine was there)", cold)
    for name, why in report.get("failed", {}).items():
        lines += ["## %s: not measured" % name, "", why, ""]
    return "\n".join(lines) + "\n"


def _scenario_md(title: str, sc: Dict[str, Any]) -> List[str]:
    out = ["## " + title, ""]
    t = sc["total_ms"]
    out.append("Total (import to first goto): median %.0f ms, p90 %.0f ms (min %.0f, max %.0f). Import of the core %.0f ms, of the wrapper %.0f ms; the call up to the first page %.0f ms (p90 %.0f)." % (
        t["median"], t["p90"], t["min"], t["max"], sc["import_core_ms"]["median"], sc["import_wrapper_ms"]["median"], sc["call_ms"]["median"], sc["call_ms"]["p90"]))
    out.append("")
    out.append("| step | what | median ms | p90 ms | % of total | own ms | in parallel with | thread |")
    out.append("|---|---|---:|---:|---:|---:|---|---|")
    total = t["median"]
    out.append("| import of the core and the wrapper | the packages are imported | %.0f | %.0f | %.1f | %.0f | | MainThread |" % (
        sc["import_core_ms"]["median"] + sc["import_wrapper_ms"]["median"],
        sc["import_core_ms"]["p90"] + sc["import_wrapper_ms"]["p90"],
        100.0 * (sc["import_core_ms"]["median"] + sc["import_wrapper_ms"]["median"]) / total if total else 0,
        sc["import_core_ms"]["median"] + sc["import_wrapper_ms"]["median"]))
    hidden: Dict[Optional[str], List[float]] = {}
    visible = []
    for s in sc["steps"]:
        if s["median_ms"] < 0.3 and s["label"] != "call":
            hidden.setdefault(s["parent"], []).append(s["median_ms"])
        else:
            visible.append(s)
    for s in visible:
        parallel = ", ".join(p.split("/")[-1] for p in s["in_parallel_with"])
        name = ("&nbsp;&nbsp;" * s["depth"]) + "`" + s["path"].split("/")[-1] + "`"
        names = s["thread"].split(",")
        thread = "" if names == ["MainThread"] else ", ".join(names[:2]) + (" +%d" % (len(names) - 2) if len(names) > 2 else "")
        out.append("| %s | %s | %.1f | %.1f | %.1f | %.1f | %s | %s |" % (
            name, describe(s["label"]), s["median_ms"], s["p90_ms"], s["pct_total"], s["self_median_ms"], parallel, thread))
        extra = hidden.get(s["path"])
        if extra:
            out.append("| %s+ %d steps under 0.3 ms | | %.1f | | | | | |" % ("&nbsp;&nbsp;" * (s["depth"] + 1), len(extra), sum(extra)))
    out.append("")
    return out


def top_three(sc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The three steps with the most time of their own in the line of the caller (not the ones that wait for what is under them)."""
    cands = [s for s in sc["steps"] if not s["background"] and s["label"] not in ("call",)]
    return sorted(cands, key=lambda s: -s["self_median_ms"])[:3]


def meta_of(unpatched: List[str]) -> Dict[str, Any]:
    from invisible_core.seal import active_seal
    import invisible_core
    import invisible_playwright
    seal = active_seal()
    return {
        "system": "Windows" if os.name == "nt" else "Linux", "platform": platform.platform(), "python": platform.python_version(),
        "cpus": os.cpu_count(), "core": getattr(invisible_core, "__version__", "?"), "wrapper": getattr(invisible_playwright, "__version__", "?"),
        "engine_tag": seal.tag, "engine_upstream": seal.upstream_version,
        "run_url": os.environ.get("INIT_MAP_RUN_URL") or "locale", "unpatched": unpatched,
        "date_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "temp": tempfile.gettempdir(),
    }


def count_browsers() -> int:
    try:
        if os.name == "nt":
            text = subprocess.run(["tasklist", "/FI", "IMAGENAME eq firefox.exe"], capture_output=True, text=True, timeout=30).stdout
            return text.lower().count("firefox.exe")
        text = subprocess.run(["pgrep", "-c", "-f", "firefox"], capture_output=True, text=True, timeout=30).stdout
        return int(text.strip() or 0)
    except Exception:  # noqa: BLE001
        return -1


def driver(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="init-map-"))
    scenarios = [s for s in args.scenarios.split(",") if s]
    report: Dict[str, Any] = {"config": {"runs": args.runs, "seed": args.seed, "scenarios": scenarios}, "scenarios": {}, "failed": {}}
    gateway = None
    proxy = stun = ""
    exit_ip = ""
    if "proxy" in scenarios:
        sys.path.insert(0, str(ROOT / "tests"))
        import _socks_servers
        # The gateway of the tests refuses the question "what is my exit" (the lab declares it instead of asking the internet). Here the question is the step that
        # is measured, so the gateway forwards it for real: the TCP discovery goes through it to the services, as it does through a real proxy.
        _socks_servers.asks_the_world = lambda server, host, port: False
        try:
            from invisible_core._geo import discover_egress_ip
            exit_ip = discover_egress_ip(None)
        except Exception as exc:  # noqa: BLE001
            print("the exit address of this machine could not be asked (%s): the fake STUN answers its default" % exc)
        gateway = _socks_servers.coherent(exit_ip=exit_ip) if exit_ip else _socks_servers.coherent()
        proxy, stun = gateway.server, gateway.measure_servers[0]
        report["config"]["proxy_gateway"] = "coherent SOCKS5 on loopback (tests/_socks_servers.py), TCP forwarded for real, UDP to a fake STUN that answers the machine's exit"
    unpatched: List[str] = []
    try:
        for name in scenarios:
            kw = {"proxy": proxy, "stun": stun} if name == "proxy" else {}
            print("== %s: warm-up" % name, flush=True)
            warm = run_worker(name, args.seed, work, name + "-warm", **kw)
            if warm.get("status") != "ok":
                report["failed"][name] = "the warm-up launch failed: %s %s" % (warm.get("error") or warm.get("status"), (warm.get("log") or warm.get("traceback") or "")[-600:])
                print(report["failed"][name], flush=True)
                continue
            unpatched += warm.get("unpatched", [])
            good: List[Dict[str, Any]] = []
            failures = []
            for i in range(args.runs):
                data = run_worker(name, args.seed, work, "%s-%02d" % (name, i), **kw)
                if data.get("status") == "ok":
                    good.append(build_run(data))
                    print("== %s run %d/%d: call %.0f ms" % (name, i + 1, args.runs, good[-1]["call_ms"]), flush=True)
                else:
                    failures.append(data.get("error") or data.get("status"))
                    print("== %s run %d/%d FAILED: %s" % (name, i + 1, args.runs, failures[-1]), flush=True)
            if good:
                report["scenarios"][name] = aggregate(good)
                report["scenarios"][name]["raw"] = [{"call_ms": r["call_ms"], "spans": [{k: s[k] for k in ("label", "path", "t0", "t1", "thread")} for s in r["spans"]]} for r in good]
                if failures:
                    report["scenarios"][name]["failed_runs"] = failures
            else:
                report["failed"][name] = "no launch succeeded: %s" % failures[:3]
        if "direct" in scenarios and "direct" in report["scenarios"]:
            from invisible_core.download import engine_root
            cold_cache = work / "cold-cache"
            print("== cold cache (one launch, a new cache directory, the engine kept)", flush=True)
            data = run_worker("direct", args.seed, work, "cold", env_extra={
                "INVISIBLE_PLAYWRIGHT_CACHE_DIR": str(cold_cache), "INVISIBLE_PLAYWRIGHT_ENGINE_DIR": str(engine_root())})
            if data.get("status") == "ok":
                report["cold"] = aggregate([build_run(data)])
            else:
                report["failed"]["cold"] = "the cold-cache launch failed: %s %s" % (data.get("error") or data.get("status"), (data.get("log") or "")[-600:])
            print("== profile (one launch under cProfile)", flush=True)
            data = run_worker("direct", args.seed, work, "profile", profile=True)
            if data.get("status") == "ok" and (work / "profile-profile.txt").exists():
                shutil.copy(work / "profile-profile.txt", out / ("init-map-profile-%s.txt" % ("windows" if os.name == "nt" else "linux")))
    finally:
        if gateway is not None:
            with contextlib.suppress(Exception):
                gateway.close()
    report["meta"] = meta_of(unpatched)
    report["meta"]["browsers_left"] = count_browsers()
    for name, sc in list(report["scenarios"].items()) + ([("cold", report["cold"])] if report.get("cold") else []):
        sc["top_three_by_own_time"] = [{"path": s["path"], "self_median_ms": s["self_median_ms"], "median_ms": s["median_ms"]} for s in top_three(sc)]
    if os.environ.get("INIT_MAP_FETCH_SECONDS"):
        report["meta"]["fetch_seconds"] = float(os.environ["INIT_MAP_FETCH_SECONDS"])
    system = "windows" if os.name == "nt" else "linux"
    (out / ("init-map-%s.json" % system)).write_text(json.dumps(report, indent=1), encoding="utf-8")
    text = markdown(report)
    (out / ("init-map-%s.md" % system)).write_text(text, encoding="utf-8")
    print(text)
    shutil.rmtree(work, ignore_errors=True)
    return 0 if report["scenarios"] else 1


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--worker", action="store_true")
    ap.add_argument("--dry", action="store_true", help="local check of the wrapping: the engine is not started (worker only)")
    ap.add_argument("--scenario", default="direct")
    ap.add_argument("--scenarios", default="direct,proxy,headless")
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--seed", type=int, default=4242)
    ap.add_argument("--proxy", default="")
    ap.add_argument("--stun", default="")
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--profile-out", default="")
    ap.add_argument("--out", default="init-map")
    args = ap.parse_args()
    return worker(args) if args.worker else driver(args)


if __name__ == "__main__":
    sys.exit(main())
