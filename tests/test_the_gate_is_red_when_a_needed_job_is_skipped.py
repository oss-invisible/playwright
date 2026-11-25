"""Rule ci-13, nothing counts as passed that did not pass: the verdict `gate` of tests.yml is red when a job it waits for is skipped.

`gate` is the one context main requires. It used to treat `skipped` as a pass, and `unit` was skipped when `core-on-index` said the pinned
invisible-core was not on the package index yet: a pull request could merge with no test run. Now:

* `core-on-index` FAILS (it does not skip anything) when the pinned core is not on the index, with the message
  `FAIL core-on-index: the pinned invisible-core <version> is not on the index. Fix: ... Rule: ci-13 ...`, and it has no `available` output;
  no `if:` of any workflow reads that output (a missing output reads as '' and would silently skip the job it guards);
* `gate` runs `scripts/ci_gate.py`, read from origin/main and never from the branch it judges, on `toJSON(needs)`: every needed job must be `success`, one `FAIL gate: job <name> is <result>. Fix: ...` line
  for each that is not. The only job conditional by design is `version` (pull requests only): `skipped` on any other event, `success` on a pull request.

Known-bad inputs: a `skipped`, a `cancelled` and a `failure` job, a skipped `version` on a pull request, a needed job that the script does not
know, an unreadable `NEEDS`, the tests.yml with the old generic `case ... failure|cancelled` arms, a workflow with an `if:` that reads the removed
output. The green case is the state of the repository.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
SCRIPT = ROOT / "scripts" / "ci_gate.py"
RULE = "Rule: ci-13 nothing counts as passed that did not pass"

_spec = importlib.util.spec_from_file_location("ci_gate", SCRIPT)
ci_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci_gate)


def _needs(result: str = "success", **overrides: str) -> dict:
    jobs = {name: {"result": result, "outputs": {}} for name in ci_gate.FIX}
    for name, value in overrides.items():
        jobs[name.replace("_", "-")] = {"result": value, "outputs": {}}
    return jobs


# ------------------------------------------------------------------ the script


def test_every_job_green_on_a_pull_request_is_ok():
    assert ci_gate.verdict(_needs(), "pull_request") == []


@pytest.mark.parametrize("result", ["skipped", "cancelled", "failure"])
def test_a_needed_job_that_is_not_success_is_red(result):
    lines = ci_gate.verdict(_needs(unit=result), "pull_request")
    assert len(lines) == 1
    assert lines[0].startswith("FAIL gate: job unit is %s. Fix: " % result)
    assert lines[0].endswith(RULE)


def test_a_skipped_unit_is_red_on_a_push_too():
    assert any("job unit is skipped" in line for line in ci_gate.verdict(_needs(unit="skipped"), "push"))


def test_a_skipped_core_on_index_is_red():
    assert any("job core-on-index is skipped" in line for line in ci_gate.verdict(_needs(core_on_index="skipped"), "pull_request"))


def test_every_job_that_is_not_success_gets_its_own_line():
    lines = ci_gate.verdict(_needs(unit="skipped", docs_api="skipped", english="failure"), "pull_request")
    assert [line.split(" is ")[0] for line in lines] == ["FAIL gate: job docs-api", "FAIL gate: job english", "FAIL gate: job unit"]


def test_version_is_skipped_on_a_push_and_must_succeed_on_a_pull_request():
    assert ci_gate.verdict(_needs(version="skipped"), "push") == []
    assert any("job version is skipped" in line for line in ci_gate.verdict(_needs(version="skipped"), "pull_request"))
    assert any("job version is failure" in line for line in ci_gate.verdict(_needs(version="failure"), "push"))


def test_only_version_is_conditional():
    assert set(ci_gate.CONDITIONAL) == {"version"}


def test_a_needed_job_the_script_does_not_know_is_red():
    needs = _needs()
    needs["brand-new"] = {"result": "success", "outputs": {}}
    assert any("job brand-new is success" in line for line in ci_gate.verdict(needs, "pull_request"))


def test_a_job_that_is_missing_from_needs_is_red():
    needs = _needs()
    del needs["unit"]
    assert any("job unit is missing" in line for line in ci_gate.verdict(needs, "pull_request"))


def _run(needs_text: str, event: str = "pull_request") -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("NEEDS", "EVENT_NAME")}
    env.update(NEEDS=needs_text, EVENT_NAME=event)
    return subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)


def test_the_script_exits_1_with_the_fail_line_when_unit_is_skipped():
    done = _run(json.dumps(_needs(unit="skipped")))
    assert done.returncode == 1
    assert "FAIL gate: job unit is skipped. Fix: " in done.stdout and RULE in done.stdout
    assert "ok" not in done.stdout.splitlines()


def test_the_script_exits_0_when_every_job_is_as_expected():
    done = _run(json.dumps(_needs()))
    assert done.returncode == 0 and done.stdout.splitlines()[-1] == "ok"


@pytest.mark.parametrize("text", ["", "not json", "[]"])
def test_the_script_is_red_when_it_cannot_read_the_results(text):
    done = _run(text)
    assert done.returncode == 1 and done.stdout.startswith("FAIL gate: ")


# ---------------------------------------------------------------- the workflow


def _job_text(text: str, job: str) -> str:
    body = text.split("\njobs:\n", 1)[1] if "\njobs:\n" in text else ""
    found = re.search(r"^  %s:\s*\n(.*?)(?=^  [A-Za-z0-9_-]+:\s*$|\Z)" % re.escape(job), body, re.M | re.S)
    return found.group(1) if found else ""


def problems_gate(tests_yml: str) -> list:
    """What is wrong with the job `gate` of this tests.yml, as the rule ci-13 reads it."""
    out = []
    body = _job_text(tests_yml, "gate")
    if not body:
        return ["tests.yml has no job `gate`"]
    needs = re.search(r"^    needs: \[([^\]]*)\]\s*$", body, re.M)
    names = [n.strip() for n in needs.group(1).split(",")] if needs else []
    if sorted(names) != sorted(ci_gate.FIX):
        out.append("the `needs:` of gate (%s) is not the table of scripts/ci_gate.py (%s)" % (sorted(names), sorted(ci_gate.FIX)))
    if not re.search(r"^ +if: always\(\)\s*$", body, re.M):
        out.append("gate has no `if: always()`: a failed job would leave it skipped")
    if not re.search(r"^ +NEEDS: \$\{\{ toJSON\(needs\) \}\}\s*$", body, re.M):
        out.append("gate does not pass `toJSON(needs)` to the script")
    if 'git show origin/main:scripts/ci_gate.py > "$RUNNER_TEMP/ci_gate.py"' not in body:
        out.append("gate does not read scripts/ci_gate.py from origin/main: the judge would come from the branch it judges")
    if "git fetch" not in body or "refs/heads/main:refs/remotes/origin/main" not in body:
        out.append("gate does not fetch main before it reads the judge")
    if "ci_gate.py is not on origin/main yet: falling back to the copy of this branch" not in body:
        out.append("gate falls back to the branch copy without saying so")
    if not re.search(r'^ +python3 "\$RUNNER_TEMP/ci_gate\.py"\s*$', body, re.M):
        out.append("gate does not run the copy it read from main")
    if re.search(r"python3? +(\./)?scripts/ci_gate\.py", body):
        out.append("gate runs the script of the branch directly")
    for line in body.splitlines():
        code = line.split("#", 1)[0]
        if re.search(r"failure\|cancelled|\bskipped\b|continue-on-error", code):
            out.append("gate names a result or `continue-on-error` outside the script: %s" % line.strip())
    return out


def _tests_yml() -> str:
    return (WORKFLOWS / "tests.yml").read_text(encoding="utf-8")


def test_the_gate_of_tests_yml_runs_the_script_on_every_needed_job():
    assert problems_gate(_tests_yml()) == []


def _gate_run_replaced(text: str, run: str) -> str:
    body = _job_text(text, "gate")
    start = body.index("        # The judge is read from main")
    end = body.index("\n\n", start) if "\n\n" in body[start:] else len(body)
    return text.replace(body, body[:start] + run + body[end:])


def test_a_gate_with_the_old_generic_case_arms_is_refused():
    run = '        run: |\n          case "${{ needs.unit.result }}" in\n            failure|cancelled) exit 1 ;;\n          esac'
    found = problems_gate(_gate_run_replaced(_tests_yml(), run))
    assert any("failure|cancelled" in p for p in found) and any("from origin/main" in p for p in found), found


def test_a_gate_that_runs_the_script_of_the_branch_it_judges_is_refused():
    found = problems_gate(_gate_run_replaced(_tests_yml(), "        run: python3 scripts/ci_gate.py"))
    assert any("from origin/main" in p for p in found) and any("of the branch directly" in p for p in found), found


def test_a_gate_that_falls_back_in_silence_is_refused():
    text = _tests_yml()
    quiet = text.replace("ci_gate.py is not on origin/main yet: falling back to the copy of this branch", "fallback", 1)
    assert quiet != text
    assert any("without saying so" in p for p in problems_gate(quiet))


def test_a_gate_that_does_not_fetch_main_is_refused():
    text = _tests_yml()
    no_fetch = text.replace("git fetch --no-tags --depth=1 origin +refs/heads/main:refs/remotes/origin/main", "true", 1)
    assert no_fetch != text
    assert any("does not fetch main" in p for p in problems_gate(no_fetch))


def test_a_gate_that_forgets_a_job_is_refused():
    text = _tests_yml()
    body = _job_text(text, "gate")
    assert any("table of scripts/ci_gate.py" in p for p in problems_gate(text.replace(body, body.replace(" unit,", "", 1))))


def test_a_gate_that_is_skipped_when_a_job_fails_is_refused():
    text = _tests_yml()
    body = _job_text(text, "gate")
    assert any("no `if: always()`" in p for p in problems_gate(text.replace(body, body.replace("    if: always()\n", "", 1))))


# ------------------------------------------------- the removed `available` output


def problems_available(workflows: dict) -> list:
    """`workflows` maps a file name to its text. Nothing may read, set or declare the `available` answer of core-on-index."""
    out = []
    for name, text in sorted(workflows.items()):
        for number, line in enumerate(text.splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"core-on-index\.outputs\.available|steps\.probe\.outputs\.available|jobs\.probe\.outputs\.available|available=(true|false)", code):
                out.append("%s:%d reads or sets the removed `available` output: %s" % (name, number, line.strip()))
            if name == "core-on-index.yml" and re.match(r"\s+available:", code):
                out.append("%s:%d declares an `available` output" % (name, number))
    return out


def _workflows() -> dict:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(WORKFLOWS.glob("*.yml"))}


def test_no_workflow_reads_or_sets_the_available_output_of_core_on_index():
    assert problems_available(_workflows()) == []


def test_no_job_of_tests_yml_has_an_if_that_reads_core_on_index():
    for number, line in enumerate(_tests_yml().splitlines(), 1):
        code = line.split("#", 1)[0]
        assert not (re.match(r"\s+if:", code) and "core-on-index" in code), "tests.yml:%d: %s" % (number, line.strip())


def test_an_if_that_reads_the_removed_output_is_refused():
    workflows = _workflows()
    workflows["tests.yml"] = workflows["tests.yml"].replace("    needs: core-on-index\n    runs-on: ${{ matrix.os }}", "    needs: core-on-index\n    if: needs.core-on-index.outputs.available == 'true'\n    runs-on: ${{ matrix.os }}", 1)
    assert any("tests.yml" in p for p in problems_available(workflows))


# ------------------------------------------------- core-on-index fails, never skips


def problems_core_on_index(text: str) -> list:
    out = []
    if "FAIL core-on-index: the pinned {DIST} {pin} is not on the index. " not in text:
        out.append("core-on-index does not fail with `FAIL core-on-index: the pinned invisible-core <version> is not on the index`")
    if "Fix: wait for its publish to finish, or pin a published core. " not in text:
        out.append("the failure of core-on-index does not say how to fix it")
    if "Rule: ci-13 nothing counts as passed that did not pass" not in text:
        out.append("the failure of core-on-index does not name ci-13")
    unpublished = text.partition("if unpublished:")[2].partition("versions = data.get(")[0]
    if "sys.exit(0)" in unpublished or "die(" not in unpublished:
        out.append("a core that is not on the index at all does not end in a failure")
    if "sys.exit(0)" in text:
        out.append("core-on-index has a `sys.exit(0)`: a path that ends green without a verdict")
    return out


def test_core_on_index_fails_when_the_pinned_core_is_not_on_the_index():
    assert problems_core_on_index((WORKFLOWS / "core-on-index.yml").read_text(encoding="utf-8")) == []


def test_a_core_on_index_that_skips_during_the_window_is_refused():
    text = (WORKFLOWS / "core-on-index.yml").read_text(encoding="utf-8")
    skipping = text.replace("              die(f\"FAIL core-on-index: the pinned {DIST} {pin} is not on the index. \"\n                  f\"Fix: wait for its publish to finish, or pin a published core. \"\n                  f\"Rule: ci-13 nothing counts as passed that did not pass\")\n",
                            "              write(\"GITHUB_OUTPUT\", \"available=false\")\n              sys.exit(0)\n", 1)
    assert skipping != text
    assert problems_core_on_index(skipping)
    assert problems_available({"core-on-index.yml": skipping})
