"""TEMPORARY, with .github/workflows/webgl-null-in-the-row-hunt.yml (deleted with its branch, never merged to main).

A pytest plugin, passed with ``-p _hunt_timeline`` (and ``PYTHONPATH=tests``) to the run of the row, and so loaded by every xdist worker too: it writes, for every test
of the run, when it started and when it ended (``time.time()``), with the worker and the process, one line per event in ``$HUNT_DIR/timeline-<pid>.jsonl``, so that the
summary can list which tests the other workers were running while a launch of the hunt went null. (The xunit2 junit has one time per test and no start.) It also
installs the listeners of tests/_hunt_webgl_null.py once the run is collected, so that the launches of the real test inside the row are recorded like the hunt's own.
It writes nothing outside the folder and changes no outcome.
"""
from __future__ import annotations

import json
import os
import time

import pytest

_handle = None


def _write(record: dict) -> None:
    global _handle
    folder = os.environ.get("HUNT_DIR")
    if not folder:
        return
    try:
        if _handle is None:
            os.makedirs(folder, exist_ok=True)
            _handle = open(os.path.join(folder, "timeline-%d.jsonl" % os.getpid()), "a", encoding="utf-8")
        _handle.write(json.dumps(record) + "\n")
        _handle.flush()
    except Exception:
        pass


def pytest_collection_finish(session):
    import sys

    if "test_canvas_reads_as_drawn" in sys.modules:
        try:
            import _hunt_webgl_null
            _hunt_webgl_null.install()
        except Exception as problem:
            _write({"ev": "note", "t": time.time(), "what": "the listeners were not installed: %r" % (problem,)})


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    common = {"nodeid": item.nodeid, "worker": os.environ.get("PYTEST_XDIST_WORKER"), "pid": os.getpid(), "iteration": os.environ.get("HUNT_ITERATION")}
    _write(dict(common, ev="start", t=time.time()))
    yield
    _write(dict(common, ev="end", t=time.time()))
