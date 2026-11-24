"""The unit job of tests.yml judges the exact counts the engine series' gate holds, so a PR that adds a test without raising min_tests is red HERE.

``ci/required-suites.json`` holds ``wrapper.unit.all.<os>`` as an exact row: the number of tests the gate collects with the files and the marker of the row.
The series' gate runs that selection on Python 3.11 and judges it with ``scripts/ci_verdict.py judge``. For a while the only thing this repository's pull
requests ran was ``pytest tests/`` with a floor of 380, so PR #590 added one test, did not raise ``min_tests``, was green here, and turned the gate red on
the next series run. The unit job now runs the selection of the row on its Python 3.11 legs (both systems the gate runs on) and judges it with the judge
and the list of main, read from main and never from the branch under judgment.

Known-bad input: the tests.yml of a2dc1b6c, which has no such step (``test_the_unit_job_of_the_old_workflow_is_refused`` runs the checks on it).
The green case is the workflow of the repository.
"""
import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
REQUIRED = ROOT / "ci" / "required-suites.json"

# The workflow of a2dc1b6c, reduced to what these checks read: the steps of the unit job that ran pytest and the floor.
OLD_UNIT_JOB = """
  unit:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest, windows-latest]
        python: ["3.11", "3.12", "3.13", "3.14"]
        exclude:
          - { os: windows-latest, python: "3.12" }
          - { os: windows-latest, python: "3.13" }
    steps:
      - uses: actions/checkout@v4

      - name: Run pytest
        shell: bash
        run: |
          pytest tests/ -v --tb=short -rs | tee tests-report.txt

      - name: Refuse a run in which the suite deselected itself
        shell: bash
        run: |
          python - <<'PY'
          PY

  docs-api:
"""


def _unit_job(text):
    start = re.search(r"^  unit:\s*$", text, re.M)
    assert start, "tests.yml has no job `unit`"
    rest = text[start.end():]
    end = re.search(r"^  [A-Za-z0-9_-]+:\s*$", rest, re.M)
    return rest[:end.start()] if end else rest


def _steps(job):
    """The steps of the job as {name, if, text}: a step starts at a line `      - ` and runs to the next one."""
    steps_at = re.search(r"^    steps:\s*$", job, re.M)
    assert steps_at, "the unit job has no steps"
    parts = re.split(r"^(?=      - )", job[steps_at.end():], flags=re.M)
    steps = []
    for part in parts:
        if not part.startswith("      - "):
            continue
        name = re.search(r"^      - name: (.*)$", part, re.M)
        cond = re.search(r"^        if: (.*)$", part, re.M)
        steps.append({"name": name.group(1) if name else "", "if": cond.group(1) if cond else "", "text": part})
    return steps


def _problems(text):
    """What is wrong with the unit job of this workflow text, one sentence each (empty: it judges the exact counts the way the gate does)."""
    job = _unit_job(text)
    steps = _steps(job)
    problems = []

    exclude = job.split("exclude:", 1)[1].split("steps:", 1)[0] if "exclude:" in job else ""
    if "3.11" in exclude or '"3.11"' not in job or "windows-latest" not in job or "ubuntu-latest" not in job:
        problems.append("the legs of Python 3.11 on ubuntu and on windows are not both in the matrix: the gate's Python and systems")

    on_311 = [s for s in steps if "matrix.python == '3.11'" in s["if"]]
    judge = [s for s in on_311 if re.search(r"\bjudge\b", s["text"]) and "ci_verdict.py" in s["text"]]
    runs = [s for s in on_311 if "--junitxml" in s["text"]]
    if len(judge) != 1:
        problems.append("%d step(s) of the legs of Python 3.11 judge with ci_verdict.py, one is needed" % len(judge))
    else:
        run = judge[0]["text"]
        if "always()" not in judge[0]["if"]:
            problems.append("the judge step does not run `always()`: a red pytest step would hide the verdict")
        if "wrapper.unit.all.$SUITE_OS" not in run or "--os \"$SUITE_OS\"" not in run:
            problems.append("the judge does not judge the row wrapper.unit.all.<os> of the leg's system")
        if "startsWith(matrix.os, 'windows') && 'windows' || 'linux'" not in judge[0]["text"]:
            problems.append("SUITE_OS is not derived from matrix.os as windows or linux")
        if "--required ci/required-suites.json" not in run:
            problems.append("the judge does not read ci/required-suites.json")
        if "git show origin/main:scripts/ci_verdict.py" not in run:
            problems.append("the judge is not read from main (`git show origin/main:scripts/ci_verdict.py`)")
        if re.search(r"python\s+scripts/ci_verdict\.py\s+judge", run):
            problems.append("the judge that runs is the one of the branch under judgment, not the one of main")
        if "git show origin/main:ci/required-suites.json" not in run or "--main-required" not in run:
            problems.append("the list is not compared with the list of main (--main-required of `git show origin/main:ci/required-suites.json`)")
    if len(runs) != 1:
        problems.append("%d step(s) of the legs of Python 3.11 write a junit, one is needed" % len(runs))
    else:
        run = runs[0]["text"]
        if "scripts/ci_verdict.py args" not in run or "wrapper.unit.all.$SUITE_OS" not in run or "--with-marker" not in run:
            problems.append("the junit is not written by the selection of the row (ci_verdict.py args --suite wrapper.unit.all.<os> --with-marker)")
        if "junit_family=xunit2" not in run:
            problems.append("the junit is not xunit2: the judge refuses a testcase with no classname")

    # No test runs twice on the legs of Python 3.11 (the PR stays under 8 minutes): the plain run and its floor are for the other legs.
    for name in ("Run pytest", "Refuse a run in which the suite deselected itself"):
        step = [s for s in steps if s["name"] == name]
        if len(step) != 1:
            problems.append("the step `%s` is not there exactly once" % name)
        elif "matrix.python != '3.11'" not in step[0]["if"]:
            problems.append("the step `%s` also runs on Python 3.11: the suite would run twice there" % name)
    return problems


def test_the_unit_job_judges_the_exact_count_of_the_row_with_the_judge_of_main():
    problems = _problems(WORKFLOW.read_text(encoding="utf-8"))
    assert not problems, "tests.yml, job unit: " + "; ".join(problems)


def test_the_unit_job_of_the_old_workflow_is_refused():
    problems = _problems(OLD_UNIT_JOB)
    assert any("judge with ci_verdict.py" in p for p in problems), problems
    assert any("write a junit" in p for p in problems), problems
    assert any("`Run pytest` also runs on Python 3.11" in p for p in problems), problems


@pytest.mark.parametrize("os_name", ["linux", "windows"])
def test_the_row_the_job_judges_exists_and_is_exact(os_name):
    rows = json.loads(REQUIRED.read_text(encoding="utf-8"))
    rows = rows["suites"] if isinstance(rows, dict) else rows
    row = [r for r in rows if r["id"] == "wrapper.unit.all." + os_name]
    assert len(row) == 1 and row[0]["exact"] is True and os_name in row[0]["os"]
