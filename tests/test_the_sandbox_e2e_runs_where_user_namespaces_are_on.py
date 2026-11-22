"""The e2e that need the browser's sandbox run in a step that turns user namespaces on, and are left out of the normal run.

``tests/test_the_socket_process_is_sandboxed_e2e.py`` (suite ``wrapper.e2e.udp.sandbox``) FAILS by design when the socket and a tab process have no
seccomp filter. On the Ubuntu 24.04 runner AppArmor refuses user namespaces to a normal user (``kernel.apparmor_restrict_unprivileged_userns=1``), so
collected in the normal run of e2e.yml it is red on a runner without the sysctl, and no test of the repository can make it green there. The engine's
gate runs the suite in a leg that turns the restriction off first; e2e.yml does the same, the way it handles the root suite: the Linux e2e runs in
three parts whose rows (``wrapper.e2e.suite.linux.N``) hold no test of the suite, and a step of its own, in ONE part, after the run and the root step,
runs exactly the suite's list (``SANDBOX_FILES`` and ``SANDBOX_NODES``: the file and the three situations of the UDP line) as the runner user after
``sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0``, reads its own junit and refuses anything but exactly the suite's count, all passed.

This test reads the workflow as text (PyYAML is not a dependency of this package). Known-bad inputs: e2e.yml as it was, with no sandbox step ->
``problems`` names it; a row of a part that holds a test of the suite; a step that runs less than the suite or in another part than the root step.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from test_the_root_e2e_run_in_the_root_step_and_nowhere_else import _matrix_parts, _rows, _steps, row_problems, step_if, step_part

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "e2e.yml"
FILE = "tests/test_the_socket_process_is_sandboxed_e2e.py"
SYSCTL = "kernel.apparmor_restrict_unprivileged_userns=0"
SUITE = "wrapper.e2e.udp.sandbox"


def _suite() -> dict:
    return next(s for s in json.loads((ROOT / "ci" / "required-suites.json").read_text(encoding="utf-8")) if s["id"] == SUITE)


def _env(body: str, key: str) -> list:
    found = re.search(r"^\s*%s: \"?([^\"\n]*)\"?\s*$" % key, body, re.M)
    return found.group(1).split() if found else []


def problems(text: str, rows: dict | None = None) -> list:
    suite = _suite()
    out = row_problems(suite["tests"], _rows() if rows is None else rows, SUITE)
    steps = _steps(text)
    sandbox = [body for name, body in steps if "sandbox e2e" in name]
    # the Linux run (xvfb): the Windows parts of e2e.yml leave the linux_only suites out by reading ci/required-suites.json
    runner = [body for name, body in steps if "scripts/run_e2e.py" in body and "xvfb-run" in body]
    if len(sandbox) != 1:
        return ["there is no (or more than one) step for the sandbox e2e: %d" % len(sandbox)]
    if len(runner) != 1:
        return ["the normal run is not one step: %d" % len(runner)]
    sandbox, runner = sandbox[0], runner[0]
    bodies = [body for _name, body in steps]
    head, _, script = sandbox.partition("run: |")
    if bodies.index(sandbox) < bodies.index(runner):
        out.append("the sandbox step comes BEFORE the normal run")
    if "success() || failure()" not in step_if(sandbox):
        out.append("the sandbox step does not run after a red normal run (`if: ... (success() || failure())`)")
    root = [body for name, body in steps if "root e2e" in name]
    if step_part(sandbox) not in _matrix_parts(text) or not root or step_part(sandbox) != step_part(root[0]):
        out.append("the sandbox step is not held to the part of the root step (`matrix.part == N` in its `if:`)")
    elif bodies.index(sandbox) < bodies.index(root[0]):
        out.append("the sandbox step comes BEFORE the root step")
    files = [t for t in suite["tests"] if "::" not in t]
    nodes = [t for t in suite["tests"] if "::" in t]
    if FILE not in files:
        out.append("%s is not a file of %s" % (FILE, SUITE))
    if sorted(_env(sandbox, "SANDBOX_FILES")) != sorted(files) or sorted(_env(sandbox, "SANDBOX_NODES")) != sorted(nodes):
        out.append("the sandbox step does not run the list of %s (SANDBOX_FILES %s, SANDBOX_NODES %s)" % (SUITE, files, nodes))
    if "pytest $SANDBOX_FILES $SANDBOX_NODES " not in script:
        out.append("the sandbox step does not pass SANDBOX_FILES and SANDBOX_NODES to pytest")
    if _env(sandbox, "SANDBOX_TESTS") != [str(suite["min_tests"])] or not suite.get("exact"):
        out.append("the sandbox step does not demand exactly %d test(s), the count of %s" % (suite["min_tests"], SUITE))
    if SYSCTL not in script or script.find(SYSCTL) > script.find("pytest"):
        out.append("the sandbox step does not turn user namespaces on (%s) before pytest" % SYSCTL)
    if "sudo -H" in script or "sudo python" in script or "sudo -E env" in script:
        out.append("the sandbox step runs the test as root: the sandbox leg is the runner user")
    if "-p no:rerunfailures" not in script or "--junitxml" not in script:
        out.append("the sandbox step has no junit or allows a rerun")
    if not all('counts["%s"]' % k in script for k in ("failures", "errors", "skipped")):
        out.append("the sandbox step does not refuse failures, errors and skips")
    for name, body in steps:
        if body is sandbox:
            continue
        # The comment block above a step belongs, by the slicing of `_steps`, to the step before it: only the lines that are not comments count.
        code = " ".join(l for l in body.splitlines() if not l.lstrip().startswith("#"))
        for named in suite["tests"]:
            if named in code:
                out.append("step %r names %s outside the sandbox step" % (name, named))
    return out


def test_the_workflow_runs_the_sandbox_e2e_in_its_own_step_and_leaves_it_out_of_the_normal_run():
    assert problems(WORKFLOW.read_text(encoding="utf-8")) == []


def test_the_file_is_one_of_the_tests_of_the_sandbox_suite_of_the_gate():
    suite = _suite()
    assert FILE in suite["tests"] and suite["os"] == ["linux"] and suite["needs_root"] is False and suite["exact"] is True


BAD_BEFORE = """
    steps:
      - name: Run the full e2e suite under a virtual display, without the root ones
        run: xvfb-run -a python -u scripts/run_e2e.py "$FF"
"""


def test_the_workflow_as_it_was_is_refused():
    found = problems(BAD_BEFORE)
    assert found and "no (or more than one) step for the sandbox e2e" in found[0]


def test_a_row_of_a_part_that_holds_a_test_of_the_suite_is_refused():
    text = WORKFLOW.read_text(encoding="utf-8")
    rows = _rows()
    rows["1"] = sorted(rows["1"] + [FILE])
    assert any("linux.1 holds %s" % FILE in p for p in problems(text, rows))
    # a file entry of a row holds a node of the suite in that file
    rows = _rows()
    rows["2"] = sorted(rows["2"] + ["tests/test_webrtc_realness.py"])
    assert any("linux.2 holds tests/test_webrtc_realness.py::" in p for p in problems(text, rows))


def test_a_sandbox_step_that_runs_only_the_file_is_refused():
    """The step as it was before the parts: the file alone, one test. The rows leave the other entries of the suite to it."""
    text = WORKFLOW.read_text(encoding="utf-8")
    sandbox = next(body for name, body in _steps(text) if "sandbox e2e" in name)
    alone = sandbox.replace(re.search(r"^ +SANDBOX_NODES: .*\n", sandbox, re.M).group(0), "").replace('SANDBOX_TESTS: "%d"' % _suite()["min_tests"], 'SANDBOX_TESTS: "1"')
    found = problems(text.replace(sandbox, alone))
    assert any("does not run the list of" in p for p in found) and any("does not demand exactly" in p for p in found), found


def test_a_sandbox_step_in_another_part_than_the_root_step_is_refused():
    text = WORKFLOW.read_text(encoding="utf-8")
    sandbox = next(body for name, body in _steps(text) if "sandbox e2e" in name)
    part = step_part(sandbox)
    other = next(p for p in _matrix_parts(text) if p != part)
    assert any("not held to the part of the root step" in p
               for p in problems(text.replace(sandbox, sandbox.replace("matrix.part == %s" % part, "matrix.part == %s" % other, 1))))
    assert any("not held to the part of the root step" in p
               for p in problems(text.replace(sandbox, sandbox.replace("matrix.part == %s && " % part, "", 1))))


def test_a_sandbox_step_without_the_sysctl_is_refused():
    text = WORKFLOW.read_text(encoding="utf-8").replace(SYSCTL, "kernel.apparmor_restrict_unprivileged_userns=1")
    assert any("does not turn user namespaces on" in p for p in problems(text))


def test_a_sandbox_step_that_may_skip_is_refused():
    text = WORKFLOW.read_text(encoding="utf-8")
    sandbox = next(body for name, body in _steps(text) if "sandbox e2e" in name)
    assert any("refuse failures" in p for p in problems(text.replace(sandbox, sandbox.replace('counts["skipped"]', 'counts["nothing"]'))))


def test_the_file_named_in_another_step_is_refused():
    text = WORKFLOW.read_text(encoding="utf-8") + "      - name: Also\n        run: python -m pytest %s\n" % FILE
    assert any("outside the sandbox step" in p for p in problems(text))
