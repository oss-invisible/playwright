#!/usr/bin/env python3
"""A MEASUREMENT, not a gate: the Control+A file, run again and again with the protocol trace, until there are enough runs.

    python scripts/hunt_ctrla_rounds.py --binary "$FF" --out-dir out [--min-runs 300] [--copies 4] [--budget-min 28]

One ROUND starts COPIES pytest processes at once, each on the whole file tests/test_control_a_selects_all_in_a_field_e2e.py (20 tests: 10 for
the in-process browser, 10 for the stock client), each with its own junit, its own log and its own tag (`r<round>c<copy>`), loaded with
`-p scripts.hunt_trace_plugin` (one JSON per test in `<out-dir>/trace`). Rounds follow each other until `--min-runs` test runs have been
recorded or the time budget is spent (no round is started after it). No round ever stops the others: a copy that is red, hangs or dies is
only a copy that wrote what it wrote, and the exit code is non-zero only when the hunt could not start (no binary) or recorded nothing.

Why COPIES in parallel and not `-n 4 --dist loadfile` or `-n 4 --dist load`: under `loadfile` a file goes to ONE worker, so a single file
would run on one worker and the other three would be idle, which is not four at once; under `load` the 20 tests of the file are scattered
over four workers and each builds the module-scoped `browser` fixture for itself, so every browser sees about 5 tests instead of the 10 it
sees in the gate, in another order: the question of this hunt (what a browser that has served pages before does with the next one) would
be asked of a different browser. A copy is what a `loadfile` worker is in the gate: the whole file, in file order, one browser per mode;
four copies are the four browsers at once of the gate (the cap of this project is four browsers).

Layout of `--out-dir`: `trace/` (the JSON of the plugin), `junit/` (one XML per copy), `logs/` (the output of each copy).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_FILE = "tests/test_control_a_selects_all_in_a_field_e2e.py"
#: A test that hangs ends its copy at this many seconds (pytest-timeout, thread method as in the gate), not the job.
TEST_DEADLINE_S = 300
#: A copy that is still alive after this long is killed: a whole file takes a couple of minutes.
COPY_DEADLINE_S = 15 * 60


def command(junit):
    return [
        sys.executable, "-u", "-m", "pytest",
        "-m", "e2e", "-o", "addopts=",
        "-p", "no:rerunfailures", "-p", "no:cacheprovider",
        "-p", "scripts.hunt_trace_plugin",
        "-o", "junit_family=xunit2", "--junitxml=%s" % junit,
        "--timeout", str(TEST_DEADLINE_S), "--timeout-method", "thread",
        "-v", "--tb=short",
        TEST_FILE,
    ]


def environment(binary, trace_dir, tag):
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    env.update({
        "PYTHONUNBUFFERED": "1",
        "INVPW_BINARY_PATH": binary,
        "STEALTHFOX_E2E_BINARY": binary,
        "HUNT_TRACE_DIR": str(trace_dir),
        "HUNT_TRACE_TAG": tag,
    })
    return env


def recorded(trace_dir):
    return sum(1 for _ in Path(trace_dir).glob("*.json"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", default=os.environ.get("INVPW_BINARY_PATH") or os.environ.get("FF"),
                        help="the engine of invisible_playwright (default: $INVPW_BINARY_PATH, then $FF)")
    parser.add_argument("--out-dir", default="ctrla-hunt", help="where trace/, junit/ and logs/ are written")
    parser.add_argument("--min-runs", type=int, default=300, help="stop after the round that reaches this many test runs (default 300)")
    parser.add_argument("--copies", type=int, default=4, help="pytest processes at once in a round (default 4: the browsers of the gate)")
    parser.add_argument("--budget-min", type=float, default=28.0, help="no new round after this many minutes (default 28)")
    args = parser.parse_args(argv)
    if not args.binary or not Path(args.binary).exists():
        print("::error::the engine is needed: --binary, or $INVPW_BINARY_PATH / $FF (%r)" % args.binary, flush=True)
        return 2

    out = Path(args.out_dir).resolve()
    trace, junit, logs = out / "trace", out / "junit", out / "logs"
    for directory in (trace, junit, logs):
        directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    number = 0
    while recorded(trace) < args.min_runs:
        if number and time.monotonic() - started > args.budget_min * 60:
            print("budget of %.0f min spent: no new round" % args.budget_min, flush=True)
            break
        number += 1
        round_started = time.monotonic()
        procs = []
        for copy in range(1, args.copies + 1):
            tag = "r%02dc%d" % (number, copy)
            log = open(logs / (tag + ".log"), "w", encoding="utf-8", newline="\n")
            proc = subprocess.Popen(command(junit / (tag + ".xml")), cwd=str(ROOT), env=environment(args.binary, trace, tag),
                                    stdout=log, stderr=subprocess.STDOUT)
            procs.append((tag, proc, log))
        codes = {}
        for tag, proc, log in procs:
            try:
                codes[tag] = proc.wait(timeout=max(1.0, COPY_DEADLINE_S - (time.monotonic() - round_started)))
            except subprocess.TimeoutExpired:
                proc.kill()
                codes[tag] = "killed"
            log.close()
        print("round %d: %.0f s, %d run(s) recorded so far, exit codes %s" % (
            number, time.monotonic() - round_started, recorded(trace), codes), flush=True)
    total = recorded(trace)
    print("%d round(s), %d test run(s) recorded in %s" % (number, total, trace), flush=True)
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
