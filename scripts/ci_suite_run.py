#!/usr/bin/env python3
"""Run ONE row of the whole-e2e suite against a binary, for the candidate's gate, and write ONE junit for the judge.

    ci_suite_run.py --required ci/required-suites.json --suite wrapper.e2e.suite.linux.1 --os linux --binary FF --junit result/x.xml

The run is the run of ``scripts/run_e2e.py`` (``invisible_core.testing.e2e``: the two passes, ``-m "e2e and not wall_clock"`` with 4 browsers at once and then
``-m "e2e and wall_clock"`` alone, ``--dist loadfile``, the deadline per test, no rerun) on the files and node ids of the row; this script does not copy it, it calls it.
What it adds is the end: that run writes one junit per pass, and the judge (``scripts/ci_verdict.py judge``) reads one. The two are merged into ONE xunit2 file, every
testcase as pytest wrote it (``classname`` kept, no ``file``/``line``).

The exit code is 0 when the passes reached their end, red tests included: the verdict is the judge's, and a failed test is a line of its list, not a reason to lose the
junit. It is not 0 when a pass did not run to its end (interrupted, an internal error, a usage error: the code of that pass) or when no junit was written at all.
"""
from __future__ import annotations

import argparse
import copy
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_verdict  # noqa: E402  (the judge: the row is read the way it reads it)

#: pytest's exit codes that mean "the pass reached its end": all passed, some failed, nothing collected (a client with no `wall_clock` test has an empty pass).
_REACHED_THE_END = (0, 1, 5)


def row_arguments(required: str, suite: str, os_name: str) -> list:
    """The files and node ids of the row, in its order (what ``ci_verdict.py args`` prints, without the marker: the passes bring their own)."""
    return list(ci_verdict.find_suite(ci_verdict.load_required(required), suite, os_name)["tests"])


def pass_junits(directory: Path, procs: str = "4") -> list:
    """The junit file of each pass of the run, in the order of the passes (``invisible_core.testing.e2e`` names them)."""
    from invisible_core.testing import e2e

    return [e2e.junit_file(Path(directory), selection) for _width, selection in e2e.passes(procs)]


def merge_junits(paths: Sequence, out: Path) -> int:
    """Write ONE xunit2 junit with every testcase of the junit files that exist, in the order given. -> the number of testcases.

    The counts of the testsuite are those of the testcases written, not the sum of the headers read: a header that disagrees with its own cases is not copied.
    A path that does not exist is a pass that wrote nothing (it is not an error here: the judge counts what is in the file)."""
    cases = []
    for path in paths:
        if not Path(path).is_file():
            continue
        cases += [copy.deepcopy(c) for c in ET.parse(path).getroot().iter("testcase")]
    tags = [{child.tag for child in c} for c in cases]
    failures = sum(1 for t in tags if "failure" in t and "error" not in t)
    errors = sum(1 for t in tags if "error" in t)
    skipped = sum(1 for t in tags if "skipped" in t and not ({"failure", "error"} & t))
    seconds = sum(float(c.get("time") or 0) for c in cases)
    suite = ET.Element("testsuite", {"name": "pytest", "errors": str(errors), "failures": str(failures), "skipped": str(skipped),
                                     "tests": str(len(cases)), "time": "%.3f" % seconds})
    suite.extend(cases)
    root = ET.Element("testsuites", {"name": "pytest tests"})
    root.append(suite)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(out, encoding="utf-8", xml_declaration=True)
    return len(cases)


def run(required: str, suite: str, os_name: str, binary: str, junit: str, *, repo: Path, procs: Optional[str] = None,
        run_e2e: Optional[Callable] = None, junit_directory: Optional[Path] = None) -> int:
    """Run the row and write the merged junit. ``run_e2e`` is ``invisible_core.testing.e2e.main`` (a fake in the unit tests)."""
    from invisible_core.testing import e2e

    run_e2e = run_e2e or e2e.main
    arguments = row_arguments(required, suite, os_name)
    directory = Path(junit_directory or tempfile.mkdtemp(prefix="invpw-suite-junit-"))
    # the core reads the directory of the junit from the environment (its own order: this variable, the watchdog's folder, the checkout): one name, set for this run
    before = os.environ.get(e2e.JUNIT_DIR_ENV)
    os.environ[e2e.JUNIT_DIR_ENV] = str(directory)
    try:
        code = run_e2e([binary] + (["-n", procs] if procs else []) + arguments, repo)
    finally:
        if before is None:
            os.environ.pop(e2e.JUNIT_DIR_ENV, None)
        else:
            os.environ[e2e.JUNIT_DIR_ENV] = before
    written = merge_junits(pass_junits(directory, procs or "4"), Path(junit))
    print("[ci_suite_run] %s on %s: %d test(s) in %s" % (suite, os_name, written, junit), flush=True)
    if code not in _REACHED_THE_END:
        print("[ci_suite_run] a pass did not run to its end (exit %s)" % code, file=sys.stderr)
        return code
    if not written:
        print("[ci_suite_run] no pass wrote a junit", file=sys.stderr)
        return 3
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--required", required=True, help="ci/required-suites.json")
    parser.add_argument("--suite", required=True, help="the id of the row")
    parser.add_argument("--os", required=True, choices=ci_verdict.OSES)
    parser.add_argument("--binary", required=True, help="the engine under test")
    parser.add_argument("--junit", required=True, help="the one junit written for the judge")
    parser.add_argument("--procs", default=None, help="browsers at once, for the first pass (default: the run's own, 4)")
    ns = parser.parse_args(argv)
    try:
        return run(ns.required, ns.suite, ns.os, ns.binary, ns.junit, repo=Path(__file__).resolve().parent.parent, procs=ns.procs)
    except ci_verdict.Usage as exc:
        print("ci_suite_run.py: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
