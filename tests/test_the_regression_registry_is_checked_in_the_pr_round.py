"""Rule ci-14: the regression registry (ci/regressions.json) is checked in the required verdict, with the checker of the core's main.

The registry starts empty. The full check (every test of the registry is collected, covered by a suite, and the list only grows against main) runs in
CI, in the step of `tests.yml` this file reads; the checker is never copied here. What is checked here: the step exists in the job the verdict `gate`
needs, on the Linux leg only, the checker and its sibling come from the core at main (never from this branch) and the step fails when the core has
none, the step passes the suites and the registry of main; and the REAL checker, run on registries this file writes, is red on a row naming a test
this repository does not collect and green on the empty registry. The checker is the core's: its path is in INVISIBLE_CORE_MAIN_CHECKER (the CI step
that checks out the core's main sets it); a test that cannot find it FAILS, it never skips.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
REGISTRY = ROOT / "ci" / "regressions.json"
SUITES = ROOT / "ci" / "required-suites.json"
CHECKER_VAR = "INVISIBLE_CORE_MAIN_CHECKER"
THIS = "tests/test_the_regression_registry_is_checked_in_the_pr_round.py"


def _job(name):
    start = WORKFLOW.index("\n  %s:\n" % name)
    following = re.search(r"\n  [A-Za-z0-9_-]+:[ ]*\n", WORKFLOW[start + 1:])
    return WORKFLOW[start:start + 1 + following.start()] if following else WORKFLOW[start:]


def _step(job_text, name):
    start = job_text.index("      - name: %s\n" % name)
    following = re.search(r"\n      - (name|uses): ", job_text[start + 1:])
    return job_text[start:start + 1 + following.start()] if following else job_text[start:]


def test_the_verdict_waits_for_the_job_that_holds_the_check():
    needs = re.search(r"\n  gate:\n    needs: \[([^\]]*)\]", WORKFLOW).group(1).replace(" ", "").split(",")
    assert "unit" in needs, "the verdict does not wait for the job that runs the registry check"


def test_the_core_main_is_checked_out_with_the_checker_and_its_sibling_on_every_leg_before_the_tests():
    job = _job("unit")
    step = _step(job, "The core's main (the regression checker)")
    assert re.search(r"uses: actions/checkout@[0-9a-f]{40} # v4 \(v4\.4\.0\)", step)
    assert "repository: oss-invisible/core\n" in step
    assert re.search(r"\n\s+ref: main\n", step), "the checker is read from the core's main"
    assert "scripts/ci_regressions.py\n" in step and "scripts/ci_skips.py\n" in step
    assert "\n        if:" not in step, "the tests of this repository run the checker on every leg"
    assert job.index("The core's main (the regression checker)") < job.index("      - name: Run pytest\n")
    assert job.index("The core's main (the regression checker)") < job.index("Run pytest (the selection of the gate")


def test_a_core_main_without_the_checker_is_red_and_the_path_reaches_the_later_steps():
    job = _job("unit")
    step = _step(job, "The regression checker of the core's main (a core main without it is red)")
    assert "[ ! -f .core-main/scripts/ci_regressions.py ]" in step and "[ ! -f .core-main/scripts/ci_skips.py ]" in step
    missing = step[step.index("[ ! -f .core-main/scripts/ci_regressions.py ]"):step.index("mkdir -p")]
    assert "exit 1" in missing and "exit 0" not in missing
    assert "continue-on-error" not in step and "\n        if:" not in step
    assert "cp .core-main/scripts/ci_regressions.py .core-main/scripts/ci_skips.py" in step, "the checker and its sibling travel together"
    assert 'echo "%s=' % CHECKER_VAR in step and '>> "$GITHUB_ENV"' in step


def test_the_check_runs_the_core_checker_with_the_suites_and_the_registry_of_main():
    job = _job("unit")
    step = _step(job, "Check the regression registry")
    assert "matrix.python == '3.11'" in step and "startsWith(matrix.os, 'ubuntu')" in step, "the check runs on the Linux leg of 3.11 only"
    assert "always()" in step
    assert re.search(r'python "\$%s" check --registry ci/regressions\.json "\$\{main_args\[@\]\}" --root \. --suites ci/required-suites\.json' % CHECKER_VAR, step)
    assert "git show origin/main:ci/regressions.json" in step and "--main-registry" in step
    assert "continue-on-error" not in step
    assert ".core-main" not in step and "ci_regressions.py" not in step, "the step reads the checker only through the variable the core's main step sets"
    # the order: main is fetched before, and the core is checked out before the check
    check = job.index("      - name: Check the regression registry\n")
    assert job.index("git fetch --no-tags --depth=1 origin main:refs/remotes/origin/main") < check
    assert job.index("The regression checker of the core's main (a core main without it is red)") < check


def _checker():
    path = os.environ.get(CHECKER_VAR, "")
    assert path and Path(path).is_file(), (
        "%s names no file (%r): the CI step that checks out the core's main sets it; locally point it at scripts/ci_regressions.py of a clone of "
        "the core at origin/main (this test fails without it, it never skips)" % (CHECKER_VAR, path))
    assert (Path(path).parent / "ci_skips.py").is_file(), "the checker's sibling ci_skips.py is not beside it"
    return Path(path)


def _row(test):
    return {"test": test, "defect": "a defect seen once", "fix": "oss-invisible/playwright@" + "a" * 40,
            "red_on_old": "oss-invisible/playwright/actions/runs/1", "red_kind": "test-on-base",
            "pr": "oss-invisible/playwright#1", "added": "2026-11-27"}


def _run_checker(registry):
    run = subprocess.run(
        [sys.executable, str(_checker()), "check", "--registry", str(registry), "--root", str(ROOT), "--suites", str(SUITES), "--python", sys.executable],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=240)
    return run.returncode, run.stdout + run.stderr


def _written(tmp_path, rows):
    path = tmp_path / "regressions.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def test_the_empty_registry_is_green(tmp_path):
    code, out = _run_checker(_written(tmp_path, []))
    assert code == 0 and out.startswith("OK ci_regressions check: 0 row(s)"), out


def test_a_row_naming_a_collected_test_of_this_repository_is_green(tmp_path):
    code, out = _run_checker(_written(tmp_path, [_row(THIS + "::test_the_empty_registry_is_green")]))
    assert code == 0 and "OK ci_regressions check: 1 row(s)" in out, out


def test_a_row_naming_a_test_in_a_file_the_wrapper_does_not_have_is_red(tmp_path):
    code, out = _run_checker(_written(tmp_path, [_row("tests/test_that_does_not_exist_anywhere.py::test_x")]))
    assert code == 1 and "the test is not collected by pytest" in out, out


def test_a_row_naming_a_test_that_its_file_does_not_hold_is_red(tmp_path):
    code, out = _run_checker(_written(tmp_path, [_row(THIS + "::test_that_this_file_does_not_hold")]))
    assert code == 1 and "the test is not collected by pytest" in out, out
    assert out.count("FAIL") == 1, "one problem, one line: " + out


def test_the_registry_of_the_repository_is_green_with_the_real_checker():
    code, out = _run_checker(REGISTRY)
    assert code == 0, out


def test_the_registry_is_a_json_array_and_every_row_names_a_test_file_that_exists():
    rows = json.loads(REGISTRY.read_text(encoding="utf-8"))
    assert isinstance(rows, list), "ci/regressions.json is not a JSON array"
    for row in rows:
        assert isinstance(row, dict) and isinstance(row.get("test"), str), "a row without a test id: %r" % (row,)
        if row["test"].startswith(("tree:", "gtest:", "xpcshell:")):
            continue  # a test of another tree or of C++: the checker collects it where it lives
        path = row["test"].split("::")[0]
        assert (ROOT / path).is_file(), "the test file of the row %s does not exist" % row["test"]
