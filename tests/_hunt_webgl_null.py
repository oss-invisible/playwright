"""TEMPORARY, with .github/workflows/webgl-null-in-the-row-hunt.yml (deleted with its branch hunt/webgl-null-in-the-row, never merged to main).

What a WebGL null at engine off has beside it in the real row. The hunt files (tests/hunt_webgl_null_in_the_row_{1..4}_e2e.py) run the real test
``test_a_non_premultiplied_webgl_canvas_reads_the_same_through_todataurl_and_toblob`` of tests/test_canvas_reads_as_drawn.py itself (its body, its assertions,
its FailureEvidence), several times, while the files of the row ``wrapper.e2e.suite.linux.2`` run beside them on the other workers of one pytest run.

This module only LISTENS, and changes no verdict: it replaces three names of the test's module by wrappers that call the original and note what it returned
(``InvisiblePlaywright`` by a subclass whose ``__exit__`` writes a record first, ``split_probe``, ``unavailable_message``). ``install()`` is idempotent and is
called by the hunt cases and by the plugin ``_hunt_timeline`` once the run is collected, so that the launches of the real test inside the row are recorded too.

One record per launch (``launches-<pid>.jsonl`` in ``$HUNT_DIR``), written while the browser is still open (the page has been asked, the second attempt of a
null has been made), whatever the engine (hw_seed 12345 or 0) and whatever the verdict, so that a null can be compared with the passes:
the GL libraries the parent maps (own = inside the engine's folder, host = anywhere else), the parent's open descriptors, the number of clients of its X display
(``xlsclients`` and the descriptors of the Xvfb that serves it), how many Xvfb and engine processes the machine holds, the load and the free memory. A null adds a
second line (``ev: null``) with the page's first attempt and the second one made 2 s later.
"""
from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

import psutil

import test_canvas_reads_as_drawn as real
from _cpu_by_type import classify, is_engine

HW_SEED_PREF = "zoom.stealth.fpp.hw_seed"
ENGINE_ON, ENGINE_OFF = 12345, 0
CASES_PER_FILE = 15
# The mapped files that say which GL a process holds (case-sensitive, fnmatch on the basename).
GL_PATTERNS = ("libEGL*", "libGL*", "libGLX*", "libGLES*", "libgallium*", "*dri*", "libvulkan*", "libllvmpipe*", "swrast*", "mesa*", "libLLVM*", "libglapi*")

_lock = threading.Lock()
_installed = False


class _State:
    current: Optional[dict] = None     # the launch that is open (one at a time in a test process)
    last_id: Optional[str] = None
    last_hw_seed: Any = None
    seq = 0


def hunt_dir() -> Optional[str]:
    return os.environ.get("HUNT_DIR") or None


def write(record: dict) -> None:
    """One line of this process's file; never raises."""
    folder = hunt_dir()
    if not folder:
        return
    try:
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "launches-%d.jsonl" % os.getpid()), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass


def _gl_libs(pid: int, own_roots: tuple) -> List[dict]:
    seen, out = set(), []
    try:
        with open("/proc/%d/maps" % pid, errors="replace") as handle:
            for line in handle:
                parts = line.split(None, 5)
                if len(parts) < 6:
                    continue
                path = parts[5].strip()
                if path.endswith(" (deleted)"):
                    path = path[: -len(" (deleted)")]
                if not path.startswith("/") or path in seen:
                    continue
                if not any(fnmatch.fnmatchcase(os.path.basename(path), pat) for pat in GL_PATTERNS):
                    continue
                seen.add(path)
                out.append({"path": path, "origin": "own" if any(path.startswith(r + "/") for r in own_roots) else "host"})
    except OSError as problem:
        return [{"error": "%s: %s" % (type(problem).__name__, problem)}]
    return out


def _engine_parent() -> Optional[psutil.Process]:
    try:
        below = psutil.Process().children(recursive=True)
    except psutil.Error:
        return None
    for proc in below:
        try:
            if is_engine(proc) and classify(proc.cmdline()) == "parent":
                return proc
        except psutil.Error:
            continue
    return None


def _x_side(parent: psutil.Process) -> dict:
    """The clients of the X display of the parent: what xlsclients lists (top-level clients), and the descriptors of the Xvfb that serves it (one per connection)."""
    out: Dict[str, Any] = {"display": None, "x_clients": None, "xvfb_fds": None, "xvfb_count": 0}
    try:
        environ = parent.environ()
    except psutil.Error:
        environ = {}
    display = environ.get("DISPLAY") or os.environ.get("DISPLAY")
    out["display"] = display
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if proc.info["name"] != "Xvfb":
                continue
            out["xvfb_count"] += 1
            if display and display in (proc.info["cmdline"] or []):
                out["xvfb_fds"] = proc.num_fds()
        except (psutil.Error, OSError):
            continue
    if display:
        env = dict(os.environ, DISPLAY=display)
        if environ.get("XAUTHORITY"):
            env["XAUTHORITY"] = environ["XAUTHORITY"]
        try:
            done = subprocess.run(["xlsclients", "-display", display], capture_output=True, text=True, timeout=30, env=env)
            out["x_clients"] = len([l for l in done.stdout.splitlines() if l.strip()]) if done.returncode == 0 else None
            if done.returncode != 0:
                out["x_clients_error"] = (done.stderr or "").strip()[:200]
        except FileNotFoundError:
            out["x_clients_error"] = "xlsclients is not installed"
        except Exception as problem:
            out["x_clients_error"] = "%s: %s" % (type(problem).__name__, problem)
    return out


def _machine() -> dict:
    out: Dict[str, Any] = {}
    try:
        out["loadavg"] = [round(x, 2) for x in os.getloadavg()]
        out["mem_available_mb"] = int(psutil.virtual_memory().available / 1048576)
    except Exception:
        pass
    engines = 0
    for proc in psutil.process_iter(["exe"]):
        try:
            if (proc.info["exe"] or "").rsplit("/", 1)[-1].lower().startswith("firefox"):
                engines += 1
        except (psutil.Error, OSError):
            continue
    out["engine_processes_on_the_machine"] = engines
    return out


def _record_launch(launch: dict) -> None:
    _State.seq += 1
    launch_id = "%d-%d" % (os.getpid(), _State.seq)
    record = {
        "ev": "launch", "id": launch_id,
        "shard": os.environ.get("HUNT_SHARD"), "iteration": os.environ.get("HUNT_ITERATION"),
        "worker": os.environ.get("PYTEST_XDIST_WORKER"), "pid": os.getpid(),
        "test": (os.environ.get("PYTEST_CURRENT_TEST") or "").rsplit(" ", 1)[0],
        "hw_seed": launch["hw_seed"], "context": launch["context"], "first": launch["first"],
        "t_start": launch["t_start"], "t_ready": launch.get("t_ready"), "t_exit": time.time(),
    }
    parent = _engine_parent()
    if parent is not None:
        binary = launch.get("binary") or os.environ.get("INVPW_BINARY_PATH") or ""
        roots = tuple({os.path.dirname(os.path.realpath(binary)), os.path.dirname(os.path.abspath(binary))}) if binary else ()
        record["parent_pid"] = parent.pid
        record["gl_libs"] = _gl_libs(parent.pid, roots)
        try:
            record["parent_fds"] = parent.num_fds()
            record["parent_threads"] = parent.num_threads()
        except psutil.Error:
            pass
        record.update(_x_side(parent))
    else:
        record["parent_pid"] = None
    record.update(_machine())
    record["t_recorded"] = time.time()
    write(record)
    _State.last_id, _State.last_hw_seed = launch_id, launch["hw_seed"]


def install() -> None:
    """Replace the three names of the test's module by listening wrappers. Idempotent, for the life of the process."""
    global _installed
    with _lock:
        if _installed:
            return
        original_ip = real.InvisiblePlaywright
        original_split, original_message = real.split_probe, real.unavailable_message

        class Recording(original_ip):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._hunt_launch = {"hw_seed": (kwargs.get("extra_prefs") or {}).get(HW_SEED_PREF), "binary": kwargs.get("binary_path"),
                                     "context": None, "first": None, "t_start": time.time()}

            def __enter__(self):
                self._hunt_launch["t_start"] = time.time()
                _State.current = self._hunt_launch
                result = super().__enter__()
                self._hunt_launch["t_ready"] = time.time()
                return result

            def __exit__(self, *exc):
                try:
                    _record_launch(self._hunt_launch)
                except Exception:
                    pass
                _State.current = None
                return super().__exit__(*exc)

        def split_probe(result):
            got, first = original_split(result)
            if _State.current is not None:
                _State.current["context"] = got is not None
                _State.current["first"] = first
            return got, first

        def unavailable_message(first, second):
            write({"ev": "null", "launch": _State.last_id, "hw_seed": _State.last_hw_seed, "shard": os.environ.get("HUNT_SHARD"),
                   "iteration": os.environ.get("HUNT_ITERATION"), "worker": os.environ.get("PYTEST_XDIST_WORKER"),
                   "test": (os.environ.get("PYTEST_CURRENT_TEST") or "").rsplit(" ", 1)[0], "t": time.time(), "first": first, "second": second})
            return original_message(first, second)

        real.InvisiblePlaywright, real.split_probe, real.unavailable_message = Recording, split_probe, unavailable_message
        _installed = True


def run_case(firefox_binary: str, shard: int, case: int) -> None:
    """One case of a hunt file: the real test itself (engine on with hw_seed 12345, then engine off with 0, FailureEvidence, the same assertions), called as a
    function: a mark is only an attribute, so the body that runs is the test's, not a copy. A null raises the test's own AssertionError."""
    install()
    real.test_a_non_premultiplied_webgl_canvas_reads_the_same_through_todataurl_and_toblob(firefox_binary)
