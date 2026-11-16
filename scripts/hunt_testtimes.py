"""HUNT ONLY: a pytest plugin (`-p hunt_testtimes`, with this directory on PYTHONPATH) that writes, for every test of a run, when it started and
when each phase ended (wall clock, ms), one JSON line per event, to the file named by HUNT_TESTTIMES. It touches no test and no fixture:
the lab's own file is run as it is, and the instants say which test was running when a process of a session did something."""
import json
import os
import time

_OUT = os.environ.get("HUNT_TESTTIMES")


def _emit(rec):
    if _OUT:
        with open(_OUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")


def pytest_runtest_logstart(nodeid, location):
    _emit({"event": "start", "nodeid": nodeid, "t": round(time.time() * 1000, 1)})


def pytest_runtest_logreport(report):
    _emit({"event": "phase", "nodeid": report.nodeid, "when": report.when, "outcome": report.outcome, "start": round(report.start * 1000, 1),
           "stop": round(report.stop * 1000, 1)})


def pytest_runtest_logfinish(nodeid, location):
    _emit({"event": "finish", "nodeid": nodeid, "t": round(time.time() * 1000, 1)})
