"""The workflows are ready for a GitHub merge queue (rule ci-13, nothing counts as passed that did not pass).

The design: a pull request runs the fast set (tests.yml: unit and the rules jobs). The heavy e2e.yml and fakeweb.yml run in the merge queue run
(`merge_group`), and that run decides. Each workflow that gates a merge has ONE verdict job, red on any needed job that is not `success`, skipped
included (scripts/ci_gate.py, read from origin/main, WORKFLOW=tests|e2e|fakeweb picks its tables):

    tests.yml     the job `gate`
    e2e.yml       the job named `e2e verdict`
    fakeweb.yml   the job named `fakeweb verdict`

What is held here, on the files themselves:

* the three listen to `merge_group` (`checks_requested`); e2e.yml and fakeweb.yml do not listen to `pull_request`;
* no `if:` of the three compares `github.event_name` with 'pull_request' without also allowing `merge_group`, and e2e.yml and fakeweb.yml read
  no field of the pull request: the base of a queue entry is `github.event.merge_group.base_sha`;
* in a merge queue run the concurrency group is the ref of the entry and nothing is cancelled;
* each verdict job waits for EVERY other job of its workflow, is `if: always()`, reads ci_gate.py from origin/main (and says so when it falls back),
  and names its WORKFLOW;
* ci_gate.py, per workflow: `success` passes; `skipped`, `cancelled`, `failure` or a missing job is red; a job the table does not know is red;
  the table has an entry for every job the verdict job needs (and nothing else), parsed from the YAML; the jobs that may be skipped are exactly
  the ones that are conditional by design.
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

_spec = importlib.util.spec_from_file_location("ci_gate_for_the_queue", SCRIPT)
ci_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci_gate)

# workflow file -> (WORKFLOW of ci_gate.py, the id of its verdict job, the name of that job)
VERDICTS = {
    "tests.yml": ("tests", "gate", None),
    "e2e.yml": ("e2e", "verdict", "e2e verdict"),
    "fakeweb.yml": ("fakeweb", "verdict", "fakeweb verdict"),
}
QUEUE_ONLY = ("e2e.yml", "fakeweb.yml")


def _text(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _code(text: str) -> str:
    """The text without its comment lines."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _triggers(text: str) -> list:
    found = re.search(r"^on:\s*\n(.*?)(?=^\S)", text, re.M | re.S)
    assert found, "no `on:` block"
    return re.findall(r"^  ([a-z_]+):", _code(found.group(1)), re.M)


def _top_block(text: str, key: str) -> str:
    found = re.search(r"^%s:\s*\n(.*?)(?=^\S)" % re.escape(key), text, re.M | re.S)
    return found.group(1) if found else ""


def _jobs(text: str) -> dict:
    """job id -> its text, for the jobs of the file."""
    body = text.split("\njobs:\n", 1)[1]
    ids = list(re.finditer(r"^  ([A-Za-z0-9_-]+):\s*$", body, re.M))
    return {m.group(1): body[m.end(): ids[i + 1].start() if i + 1 < len(ids) else len(body)] for i, m in enumerate(ids)}


def _needs(job_text: str) -> list:
    flow = re.search(r"^    needs: \[([^\]]*)\]\s*$", job_text, re.M)
    if flow:
        return [n.strip() for n in flow.group(1).split(",") if n.strip()]
    one = re.search(r"^    needs: ([A-Za-z0-9_-]+)\s*$", job_text, re.M)
    return [one.group(1)] if one else []


def _ifs(text: str) -> list:
    """(line number, condition) of every `if:` of the file, job or step."""
    return [(n, m.group(1)) for n, line in enumerate(text.splitlines(), 1)
            if (m := re.match(r"^\s+(?:- )?if: (.*)$", line)) and not line.lstrip().startswith("#")]


def _needs_of_verdict(name: str) -> list:
    return _needs(_jobs(_text(name))[VERDICTS[name][1]])


# ------------------------------------------------------------------------------ the triggers


@pytest.mark.parametrize("name", sorted(VERDICTS))
def test_every_workflow_with_a_verdict_listens_to_the_merge_queue(name):
    text = _text(name)
    assert "merge_group" in _triggers(text), "%s has a verdict job and does not listen to merge_group" % name
    assert re.search(r"^  merge_group:\s*\n    types: \[checks_requested\]\s*$", text, re.M), "%s: merge_group without types: [checks_requested]" % name


@pytest.mark.parametrize("name", QUEUE_ONLY)
def test_the_heavy_workflows_do_not_run_on_the_pull_request(name):
    triggers = _triggers(_text(name))
    assert "pull_request" not in triggers and "pull_request_target" not in triggers, triggers
    assert "push" in triggers and "workflow_dispatch" in triggers, "push (main) and workflow_dispatch stay: %s" % triggers


def test_the_fast_set_still_runs_on_the_pull_request_and_on_main():
    triggers = _triggers(_text("tests.yml"))
    assert {"push", "pull_request", "merge_group", "workflow_dispatch"} <= set(triggers), triggers
    assert re.search(r"^  push:\s*\n    branches: \[main\]", _text("tests.yml"), re.M)


# ------------------------------------------------------------------------------ the conditions


def problems_conditions(name: str, text: str) -> list:
    out = []
    for number, cond in _ifs(text):
        if re.search(r"event_name\s*==\s*'pull_request'", cond) and "merge_group" not in cond:
            out.append("%s:%d `if: %s` runs on a pull request and not in the merge queue run" % (name, number, cond))
    if name in QUEUE_ONLY:
        for number, line in enumerate(_code(text).splitlines(), 1):
            if "github.event.pull_request" in line or "github.head_ref" in line or "github.base_ref" in line:
                out.append("%s: %s reads the pull request, which a queue run does not have (use github.event.merge_group.base_sha)" % (name, line.strip()))
    return out


@pytest.mark.parametrize("name", sorted(VERDICTS))
def test_no_condition_runs_on_the_pull_request_and_skips_the_merge_queue(name):
    assert problems_conditions(name, _text(name)) == []


def test_every_step_that_reads_the_base_reads_the_one_of_the_queue_entry():
    for name in QUEUE_ONLY:
        text = _code(_text(name))
        assert text.count("BASE_SHA: ${{ github.event.merge_group.base_sha }}") == text.count("BASE_SHA:"), name
    e2e = _code(_text("e2e.yml"))
    assert e2e.count("BASE_SHA:") == 9 and len(re.findall(r"^\s+if: github\.event_name == 'merge_group'\s*$", e2e, re.M)) == 8
    assert "if: github.event_name == 'merge_group' && inputs.only != 'windows-control-a'" in e2e


def test_the_version_check_runs_in_the_merge_queue_run_too():
    version = _jobs(_text("tests.yml"))["version"]
    assert re.search(r"^    if: github\.event_name == 'pull_request' \|\| github\.event_name == 'merge_group'\s*$", version, re.M)


def test_a_condition_that_skips_the_queue_is_refused():
    text = _text("e2e.yml").replace("if: github.event_name == 'merge_group'\n        shell: bash", "if: github.event_name == 'pull_request'\n        shell: bash", 1)
    assert problems_conditions("e2e.yml", text)
    assert problems_conditions("tests.yml", _text("tests.yml").replace("|| github.event_name == 'merge_group'", "", 1))
    assert problems_conditions("fakeweb.yml", _text("fakeweb.yml") + "\n      - run: echo ${{ github.event.pull_request.base.sha }}\n")


# ------------------------------------------------------------------------------ the concurrency


@pytest.mark.parametrize("name", sorted(VERDICTS))
def test_a_queue_run_is_grouped_on_its_ref_and_never_cancelled(name):
    block = _code(_top_block(_text(name), "concurrency"))
    group = re.search(r"^  group: (.*)$", block, re.M).group(1)
    cancel = re.search(r"^  cancel-in-progress: (.*)$", block, re.M).group(1).strip()
    assert "github.event_name == 'merge_group' && github.ref" in group, group
    assert "github.run_id" in group, "outside a queue run the group is unique to the run: %s" % group
    if name == "tests.yml":
        assert cancel == "${{ github.event_name == 'pull_request' }}", cancel   # false in a queue run; a pull request keeps its behaviour
        assert "format('pr-{0}', github.event.pull_request.number)" in group
    else:
        assert cancel == "false", cancel


# ------------------------------------------------------------------------------ the verdict jobs


def problems_verdict(name: str, text: str) -> list:
    workflow, job_id, job_name = VERDICTS[name]
    out = []
    jobs = _jobs(text)
    if job_id not in jobs:
        return ["%s has no job `%s`" % (name, job_id)]
    body = jobs[job_id]
    if job_name is None:
        if re.search(r"^    name:", body, re.M):
            out.append("%s: the verdict job `gate` has a name, and the branch protection reads the context `gate`" % name)
    elif not re.search(r"^    name: %s\s*$" % re.escape(job_name), body, re.M):
        out.append("%s: the verdict job is not named exactly `%s`" % (name, job_name))
    named = [i for i, b in jobs.items() if job_name and re.search(r"^    name: %s\s*$" % re.escape(job_name), b, re.M)]
    if job_name and named != [job_id]:
        out.append("%s: more than one job is named `%s`: %s" % (name, job_name, named))
    others = sorted(set(jobs) - {job_id})
    needs = _needs(body)
    if sorted(needs) != others:
        out.append("%s: the verdict job needs %s, not every other job of the workflow %s" % (name, sorted(needs), others))
    if not re.search(r"^    if: always\(\)\s*$", body, re.M):
        out.append("%s: the verdict job has no `if: always()`: a failed job would leave it skipped" % name)
    if "actions/checkout@" not in body:
        out.append("%s: the verdict job does not check out the repository" % name)
    if not re.search(r"^ +NEEDS: \$\{\{ toJSON\(needs\) \}\}\s*$", body, re.M):
        out.append("%s: the verdict job does not pass `toJSON(needs)`" % name)
    if not re.search(r"^ +EVENT_NAME: \$\{\{ github\.event_name \}\}\s*$", body, re.M):
        out.append("%s: the verdict job does not pass the event" % name)
    if not re.search(r"^ +WORKFLOW: %s\s*$" % workflow, body, re.M):
        out.append("%s: the verdict job does not set WORKFLOW: %s" % (name, workflow))
    if 'git show origin/main:scripts/ci_gate.py > "$RUNNER_TEMP/ci_gate.py"' not in body:
        out.append("%s: the verdict job does not read scripts/ci_gate.py from origin/main" % name)
    if "git fetch" not in body or "refs/heads/main:refs/remotes/origin/main" not in body:
        out.append("%s: the verdict job does not fetch main before it reads the judge" % name)
    if "ci_gate.py is not on origin/main yet: falling back to the copy of this branch" not in body:
        out.append("%s: the verdict job falls back to the branch copy without saying so" % name)
    if not re.search(r'^ +python3 "\$RUNNER_TEMP/ci_gate\.py"\s*$', body, re.M):
        out.append("%s: the verdict job does not run the copy it read from main" % name)
    if re.search(r"python3? +(\./)?scripts/ci_gate\.py", body):
        out.append("%s: the verdict job runs the script of the branch directly" % name)
    for line in body.splitlines():
        code = line.split("#", 1)[0]
        if re.search(r"failure\|cancelled|\bskipped\b|continue-on-error|needs\.[A-Za-z0-9_-]+\.result", code):
            out.append("%s: the verdict job names a result outside the script: %s" % (name, line.strip()))
    return out


@pytest.mark.parametrize("name", sorted(VERDICTS))
def test_each_verdict_job_waits_for_every_job_and_reads_the_judge_from_main(name):
    assert problems_verdict(name, _text(name)) == []


def test_a_verdict_that_forgets_a_job_or_is_not_always_or_reads_the_branch_is_refused():
    text = _text("e2e.yml")
    assert any("not every other job" in p for p in problems_verdict("e2e.yml", text.replace("e2e-page-windows, ", "", 1)))
    assert any("no `if: always()`" in p for p in problems_verdict("fakeweb.yml", _text("fakeweb.yml").replace("    name: fakeweb verdict\n    needs: [core-on-index, linux-deps-fakeweb, walk]\n    if: always()\n", "    name: fakeweb verdict\n    needs: [core-on-index, linux-deps-fakeweb, walk]\n", 1)))
    assert any("origin/main" in p for p in problems_verdict("e2e.yml", text.replace('git show origin/main:scripts/ci_gate.py > "$RUNNER_TEMP/ci_gate.py"', "true", 1)))
    assert any("WORKFLOW" in p for p in problems_verdict("e2e.yml", text.replace("WORKFLOW: e2e\n", "", 1)))
    assert any("not named exactly" in p for p in problems_verdict("fakeweb.yml", _text("fakeweb.yml").replace("name: fakeweb verdict", "name: verdict", 1)))
    assert any("names a result" in p for p in problems_verdict("fakeweb.yml", _text("fakeweb.yml").replace('python3 "$RUNNER_TEMP/ci_gate.py"', 'python3 "$RUNNER_TEMP/ci_gate.py" || [ "${{ needs.walk.result }}" = skipped ]', 1)))


def test_the_names_the_release_and_the_branch_protection_read_are_kept():
    e2e = _jobs(_text("e2e.yml"))["e2e"]
    assert re.search(r"^    name: e2e \(linux, xvfb\)\s*$", e2e, re.M), "scripts/e2e_is_green.py reads the Linux check of the push to main"
    assert "gate" in _jobs(_text("tests.yml"))


# ------------------------------------------------------------------------------ ci_gate.py, per workflow


def _table(workflow: str) -> dict:
    return ci_gate.TABLES[workflow][1]


def _all_green(workflow: str, touched: str = "true") -> dict:
    needs = {name: {"result": "success", "outputs": {}} for name in _table(workflow)}
    if workflow == "e2e":
        needs["e2e-page-final-touched"]["outputs"] = {"touched": touched}
        if touched != "true":
            for name in ci_gate.E2E_BY_OUTPUT:
                needs[name]["result"] = "skipped"
    return needs


def _lines(workflow: str, needs: dict, event: str, only: str = "") -> list:
    return ci_gate.verdict(needs, event, workflow, only)


def _event_for(workflow: str) -> str:
    return "pull_request" if workflow == "tests" else "merge_group"


@pytest.mark.parametrize("workflow", ["tests", "e2e", "fakeweb"])
def test_every_job_green_passes(workflow):
    assert _lines(workflow, _all_green(workflow), _event_for(workflow)) == []


@pytest.mark.parametrize("workflow", ["tests", "e2e", "fakeweb"])
@pytest.mark.parametrize("result", ["skipped", "cancelled", "failure"])
def test_a_needed_job_that_is_not_success_is_red(workflow, result):
    event = _event_for(workflow)
    label = ci_gate.TABLES[workflow][0]
    for job in sorted(_table(workflow)):
        if job in ci_gate.CONDITIONAL and workflow == "tests" or job in ci_gate.E2E_BY_OUTPUT or job in ci_gate.E2E_CONDITIONAL:
            continue                                        # the conditional ones are tested below
        needs = _all_green(workflow)
        needs[job]["result"] = result
        lines = _lines(workflow, needs, event)
        assert any(line.startswith("FAIL %s: job %s is %s. Fix: " % (label, job, result)) and line.endswith(RULE) for line in lines), (job, lines)


@pytest.mark.parametrize("workflow", ["tests", "e2e", "fakeweb"])
def test_a_job_that_is_missing_is_red(workflow):
    for job in sorted(_table(workflow)):
        needs = _all_green(workflow)
        del needs[job]
        assert any("job %s is missing" % job in line for line in _lines(workflow, needs, _event_for(workflow))), job


@pytest.mark.parametrize("workflow", ["tests", "e2e", "fakeweb"])
def test_a_job_the_table_does_not_know_is_red(workflow):
    needs = _all_green(workflow)
    needs["brand-new"] = {"result": "success", "outputs": {}}
    label = ci_gate.TABLES[workflow][0]
    assert any(line.startswith("FAIL %s: job brand-new is success" % label) for line in _lines(workflow, needs, _event_for(workflow)))


@pytest.mark.parametrize("name", sorted(VERDICTS))
def test_the_table_has_an_entry_for_every_job_the_verdict_needs_and_nothing_else(name):
    workflow = VERDICTS[name][0]
    needed = _needs_of_verdict(name)
    assert sorted(needed) == sorted(_table(workflow)), "%s: needs %s, table %s" % (name, sorted(needed), sorted(_table(workflow)))
    assert all(_table(workflow)[job].strip() for job in needed), "every entry says what to do"


def test_version_runs_in_the_queue_and_on_a_pull_request_and_is_skipped_on_a_push():
    for event in ("pull_request", "merge_group"):
        assert ci_gate.CONDITIONAL["version"] and event in ci_gate.CONDITIONAL["version"]
        needs = _all_green("tests")
        needs["version"]["result"] = "skipped"
        assert any("job version is skipped" in line for line in _lines("tests", needs, event)), event
    needs = _all_green("tests")
    needs["version"]["result"] = "skipped"
    assert _lines("tests", needs, "push") == []
    needs["version"]["result"] = "failure"
    assert any("job version is failure" in line for line in _lines("tests", needs, "push"))


def test_the_final_seed_shards_run_exactly_when_the_page_oracle_is_touched():
    for name in ci_gate.E2E_BY_OUTPUT:
        needs = _all_green("e2e", touched="true")
        needs[name]["result"] = "skipped"
        assert any("job %s is skipped" % name in line for line in _lines("e2e", needs, "merge_group")), "touched, and a shard did not run"
        needs = _all_green("e2e", touched="false")
        assert _lines("e2e", needs, "merge_group") == [], "not touched: the shards are skipped on purpose"
        needs[name]["result"] = "failure"
        assert any("job %s is failure" % name in line for line in _lines("e2e", needs, "merge_group"))
    needs = _all_green("e2e", touched="true")
    needs["e2e-page-final-touched"]["result"] = "skipped"
    assert any("job e2e-page-final-touched is skipped" in line for line in _lines("e2e", needs, "merge_group")), "the job that decides must run in the queue run"


def test_the_dispatch_of_the_windows_job_alone_skips_what_it_leaves_out_and_nothing_else():
    needs = {name: {"result": "skipped", "outputs": {}} for name in ci_gate.E2E_FIX}
    for name in ("core-on-index", "e2e-windows-control-a"):
        needs[name]["result"] = "success"
    assert _lines("e2e", needs, "workflow_dispatch", "windows-control-a") == []
    assert any("job e2e-windows is skipped" in line for line in _lines("e2e", needs, "workflow_dispatch", "")), "a full run does not forgive a skipped job"
    needs["e2e-windows-control-a"]["result"] = "skipped"
    assert any("job e2e-windows-control-a is skipped" in line for line in _lines("e2e", needs, "workflow_dispatch", "windows-control-a"))


def test_the_jobs_that_may_be_skipped_are_the_ones_the_workflow_says_it_skips():
    text = _text("e2e.yml")
    jobs = _jobs(text)
    only = {j for j, body in jobs.items() if re.search(r"^    if: .*inputs\.only != 'windows-control-a'", body, re.M)}
    by_output = {j for j, body in jobs.items() if re.search(r"^    if: needs\.[A-Za-z0-9_-]+\.outputs\.[a-z]+ == 'true'\s*$", body, re.M)}
    assert by_output == set(ci_gate.E2E_BY_OUTPUT), by_output
    assert set(ci_gate.E2E_NARROWED) - set(ci_gate.E2E_BY_OUTPUT) == only, (sorted(only), ci_gate.E2E_NARROWED)
    for job, (decider, output, value) in ci_gate.E2E_BY_OUTPUT.items():
        assert "needs.%s.outputs.%s == '%s'" % (decider, output, value) in jobs[job], job
        assert decider in _needs(jobs[job]) and "%s: ${{ steps.%s.outputs.%s }}" % (output, output, output) in jobs[decider]
    conditional = {j for j, body in jobs.items() if re.search(r"^    if: .*event_name", body, re.M)}
    assert conditional == set(ci_gate.E2E_CONDITIONAL), conditional
    assert set(_triggers(text)) >= {"merge_group"} and all("merge_group" in ci_gate.E2E_CONDITIONAL[j] for j in conditional)
    fake = _jobs(_text("fakeweb.yml"))
    assert not [j for j, body in fake.items() if j != "verdict" and re.search(r"^    if:", body, re.M)], "no job of fakeweb.yml is conditional"


def test_the_conditional_jobs_of_tests_yml_are_the_ones_whose_if_reads_the_event():
    jobs = _jobs(_text("tests.yml"))
    conditional = {j for j, body in jobs.items() if re.search(r"^    if: .*event_name", body, re.M)}
    assert conditional == set(ci_gate.CONDITIONAL) == {"version"}, conditional


def _run(workflow: str, needs: dict, event: str, only: str = "") -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("NEEDS", "EVENT_NAME", "WORKFLOW", "INPUT_ONLY")}
    env.update(NEEDS=json.dumps(needs), EVENT_NAME=event, INPUT_ONLY=only)
    if workflow:
        env["WORKFLOW"] = workflow
    return subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("workflow", ["tests", "e2e", "fakeweb"])
def test_the_script_exits_0_on_green_and_1_with_the_verdict_job_named_on_a_skipped_job(workflow):
    done = _run(workflow, _all_green(workflow), _event_for(workflow))
    assert done.returncode == 0 and done.stdout.splitlines()[-1] == "ok", done.stdout
    needs = _all_green(workflow)
    first = sorted(j for j in _table(workflow) if j not in ci_gate.E2E_BY_OUTPUT and j not in ci_gate.E2E_CONDITIONAL and j != "version")[0]
    needs[first]["result"] = "skipped"
    done = _run(workflow, needs, _event_for(workflow))
    assert done.returncode == 1 and "FAIL %s: job %s is skipped. Fix: " % (ci_gate.TABLES[workflow][0], first) in done.stdout, done.stdout


def test_the_default_workflow_is_tests_and_an_unknown_one_is_red():
    assert _run("", _all_green("tests"), "pull_request").returncode == 0
    done = _run("nope", _all_green("tests"), "pull_request")
    assert done.returncode == 1 and done.stdout.startswith("FAIL gate: WORKFLOW is 'nope'")
    done = _run("e2e", _all_green("tests"), "merge_group")
    assert done.returncode == 1, "the tables of another workflow are not forgiven"
