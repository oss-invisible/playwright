"""A docs-only change runs nothing heavy in the merge queue, and "nothing to test" is an explicit success the verdict knows.

GitHub applies no `paths` filter to a merge queue run (`merge_group`), so e2e.yml and fakeweb.yml would run in full for a change to a Markdown file. Both
workflows now start with the job `changes` (scripts/ci_changes.py): in a queue run it lists the files between `github.event.merge_group.base_sha` and
`head_sha` and applies the path rules the `push` trigger of the same workflow has (e2e: `paths-ignore` of `**.md`, `docs/**`, `LICENSE`; fakeweb: its
`paths`); on push and workflow_dispatch it always says run=true. Every other job needs `changes` and runs only on `needs.changes.outputs.run == 'true'`.

That is NOT a skip the verdict waves through: scripts/ci_gate.py gets the output as CHANGES_RUN, and

* run=false: `changes` is success, every other job is `skipped` (one that ran anyway is red), and the verdict ends with
  `ok: nothing to test (<n> files changed, none under the paths of <workflow>)`;
* run=true: every job is `success`, a skipped one is red;
* a CHANGES_RUN that is missing or is not true or false is red, and so is a `changes` job that did not succeed.

Known-bad inputs: a heavy job that ran when there was nothing to test, a heavy job skipped when there was something to test, no output at all.
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


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name + "_for_changes", ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci_changes = _load("ci_changes")
ci_gate = _load("ci_gate")
CHANGES = ROOT / "scripts" / "ci_changes.py"
GATE = ROOT / "scripts" / "ci_gate.py"
HEAVY = ("e2e", "fakeweb")
RULE = "Rule: ci-13 nothing counts as passed that did not pass"

# ------------------------------------------------------------------------------ the path rules


@pytest.mark.parametrize("files", [
    ["README.md"],
    ["docs/ARCHITECTURE.md"],
    ["docs/diagram.png"],
    ["docs/deep/er/file.txt"],
    ["LICENSE"],
    ["src/invisible_playwright/NOTES.md"],
    ["README.md", "docs/a.png", "LICENSE"],
])
def test_a_docs_only_change_gives_run_false_for_both_workflows(files):
    assert ci_changes.should_run("e2e", files) is False
    assert ci_changes.should_run("fakeweb", files) is False


@pytest.mark.parametrize("files", [
    ["src/invisible_playwright/launcher.py"],
    ["tests/test_a_launch_has_one_deadline.py"],
    ["README.md", "src/invisible_playwright/launcher.py"],
    ["docs/a.md", "tests/test_something_e2e.py"],
    ["pyproject.toml"],
    ["scripts/ci_gate.py"],
    [".github/workflows/e2e.yml"],
    ["LICENSE.txt"],
])
def test_a_change_to_a_test_or_to_src_gives_run_true_for_e2e(files):
    assert ci_changes.should_run("e2e", files) is True


@pytest.mark.parametrize("path", [
    "tests/fakeweb/pages.json",
    "tests/fakeweb/deep/er/site.py",
    "tests/test_fakeweb_p4_report.py",
    "tests/test_fakeweb_p2_e2e.py",
    "scripts/run_fakeweb.py",
    "scripts/fakeweb_p4_report.py",
    "scripts/playwright_pin.txt",
    ".github/workflows/fakeweb.yml",
])
def test_a_fake_web_file_gives_run_true_for_fakeweb(path):
    assert ci_changes.should_run("fakeweb", [path]) is True
    assert ci_changes.should_run("fakeweb", ["docs/a.md", path]) is True


@pytest.mark.parametrize("path", [
    "src/invisible_playwright/launcher.py",
    "tests/test_a_launch_has_one_deadline.py",
    "tests/fakewebs/other.py",
    "tests/sub/test_fakeweb_x.py",
    "scripts/run_fakeweb.pyc",
    "README.md",
])
def test_a_file_that_is_not_the_fake_web_gives_run_false_for_fakeweb(path):
    assert ci_changes.should_run("fakeweb", [path]) is False


def test_a_change_that_lists_no_file_runs_because_it_cannot_be_told_from_a_broken_diff():
    assert ci_changes.should_run("e2e", []) is True
    assert ci_changes.should_run("fakeweb", []) is True


def test_an_unknown_workflow_is_an_error():
    with pytest.raises(ValueError):
        ci_changes.should_run("tests", ["README.md"])


def _push_block(workflow_file: str) -> str:
    text = (WORKFLOWS / workflow_file).read_text(encoding="utf-8")
    on = re.search(r"^on:\s*\n(.*?)(?=^\S)", text, re.M | re.S).group(1)
    code = "\n".join(line for line in on.splitlines() if not line.lstrip().startswith("#"))
    return re.search(r"^  push:\s*\n(.*?)(?=^  \S|\Z)", code + "\n", re.M | re.S).group(1)


def test_the_tables_are_the_path_filters_of_the_push_triggers():
    ignore = re.findall(r"- '([^']+)'", _push_block("e2e.yml").split("paths-ignore:", 1)[1])
    assert tuple(ignore) == ci_changes.IGNORE["e2e"]
    only = re.findall(r"- '([^']+)'", _push_block("fakeweb.yml").split("paths:", 1)[1])
    assert tuple(only) == ci_changes.ONLY["fakeweb"]


# ------------------------------------------------------------------------------ the script on events and on a real diff


def _changes(*args: str, cwd=ROOT) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CHANGES), *args], cwd=str(cwd), capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "schedule", ""])
@pytest.mark.parametrize("workflow", HEAVY)
def test_a_push_and_a_dispatch_always_run(workflow, event):
    done = _changes(workflow, event)
    assert done.returncode == 0 and done.stdout.splitlines() == ["run=true", "files=0"], (done.stdout, done.stderr)


def test_a_queue_run_without_its_commits_is_an_error():
    assert _changes("e2e", "merge_group").returncode == 2
    assert _changes("e2e", "merge_group", "", "").returncode == 2
    assert _changes("nope", "push").returncode == 2


def _git(cwd, *args: str) -> str:
    done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@users.noreply.github.com", "-c", "commit.gpgsign=false", *args],
                          cwd=str(cwd), capture_output=True, text=True, timeout=60, check=True)
    return done.stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "src" / "a.py").write_text("a = 1\n")
    (tmp_path / "README.md").write_text("one\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _entry(repo, files: dict) -> tuple:
    base = _git(repo, "rev-parse", "HEAD")
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    return base, _git(repo, "rev-parse", "HEAD")


def test_a_queue_entry_that_changes_only_markdown_says_run_false_and_the_count(repo):
    base, head = _entry(repo, {"README.md": "two\n", "docs/new.md": "x\n", "docs/pic.png": "y\n"})
    for workflow in HEAVY:
        done = _changes(workflow, "merge_group", base, head, cwd=repo)
        assert done.returncode == 0 and done.stdout.splitlines() == ["run=false", "files=3"], (workflow, done.stdout, done.stderr)


def test_a_queue_entry_that_changes_src_says_run_true_for_e2e_and_false_for_fakeweb(repo):
    base, head = _entry(repo, {"src/a.py": "a = 2\n"})
    assert _changes("e2e", "merge_group", base, head, cwd=repo).stdout.splitlines() == ["run=true", "files=1"]
    assert _changes("fakeweb", "merge_group", base, head, cwd=repo).stdout.splitlines() == ["run=false", "files=1"]


def test_a_queue_entry_that_changes_a_fake_web_file_says_run_true_for_fakeweb(repo):
    base, head = _entry(repo, {"tests/fakeweb/pages.json": "{}\n"})
    assert _changes("fakeweb", "merge_group", base, head, cwd=repo).stdout.splitlines() == ["run=true", "files=1"]


def test_a_commit_that_cannot_be_listed_fails_the_job_instead_of_guessing(repo):
    done = _changes("e2e", "merge_group", "0" * 40, "1" * 40, cwd=repo)
    assert done.returncode != 0 and "run=" not in done.stdout


# ------------------------------------------------------------------------------ the workflows


def _text(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _jobs(text: str) -> dict:
    body = text.split("\njobs:\n", 1)[1]
    ids = list(re.finditer(r"^  ([A-Za-z0-9_-]+):\s*$", body, re.M))
    return {m.group(1): body[m.end(): ids[i + 1].start() if i + 1 < len(ids) else len(body)] for i, m in enumerate(ids)}


def _needs(job_text: str) -> list:
    flow = re.search(r"^    needs: \[([^\]]*)\]\s*$", job_text, re.M)
    if flow:
        return [n.strip() for n in flow.group(1).split(",") if n.strip()]
    one = re.search(r"^    needs: ([A-Za-z0-9_-]+)\s*$", job_text, re.M)
    return [one.group(1)] if one else []


def problems_changes(name: str, text: str) -> list:
    workflow = name.split(".")[0]
    out = []
    jobs = _jobs(text)
    if list(jobs)[0:1] != ["changes"]:
        return ["%s: the first job is not `changes`: %s" % (name, list(jobs)[:1])]
    body = jobs["changes"]
    if _needs(body):
        out.append("%s: `changes` needs %s: it must be the first job and wait for nothing" % (name, _needs(body)))
    if not re.search(r"^      run: \$\{\{ steps\.changes\.outputs\.run \}\}\s*$", body, re.M):
        out.append("%s: `changes` has no output `run`" % name)
    if not re.search(r"^      files: \$\{\{ steps\.changes\.outputs\.files \}\}\s*$", body, re.M):
        out.append("%s: `changes` has no output `files`" % name)
    if 'git show origin/main:scripts/ci_changes.py > "$RUNNER_TEMP/ci_changes.py"' not in body or "refs/heads/main:refs/remotes/origin/main" not in body:
        out.append("%s: `changes` does not read scripts/ci_changes.py from origin/main: a branch could decide that it needs no test" % name)
    if "ci_changes.py is not on origin/main yet: falling back to the copy of this branch" not in body:
        out.append("%s: `changes` falls back to the branch copy without saying so" % name)
    if not re.search(r'^ +python3 "\$RUNNER_TEMP/ci_changes\.py" %s "\$EVENT_NAME" "\$BASE_SHA" "\$HEAD_SHA" \| tee -a "\$GITHUB_OUTPUT"\s*$' % workflow, body, re.M):
        out.append("%s: `changes` does not run the copy it read from main with the workflow, the event and the two commits" % name)
    if "BASE_SHA: ${{ github.event.merge_group.base_sha }}" not in body or "HEAD_SHA: ${{ github.event.merge_group.head_sha }}" not in body:
        out.append("%s: `changes` does not read the base and the head of the queue entry" % name)
    if not re.search(r"^        shell: bash\s*$", body, re.M):
        out.append("%s: the step of `changes` has no `shell: bash` (the pipe to tee would hide a failure of the script: no pipefail)" % name)
    for job, text_ in jobs.items():
        if job in ("changes", "verdict"):
            continue
        if "changes" not in _needs(text_):
            out.append("%s: job %s does not need `changes`" % (name, job))
        cond = re.search(r"^    if: (.*)$", text_, re.M)
        if not cond or "needs.changes.outputs.run == 'true'" not in cond.group(1):
            out.append("%s: job %s does not run only on `needs.changes.outputs.run == 'true'`" % (name, job))
    verdict = jobs["verdict"]
    if "changes" not in _needs(verdict):
        out.append("%s: the verdict does not need `changes`" % name)
    if not re.search(r"^ +CHANGES_RUN: \$\{\{ needs\.changes\.outputs\.run \}\}\s*$", verdict, re.M):
        out.append("%s: the verdict does not pass CHANGES_RUN" % name)
    return out


@pytest.mark.parametrize("name", ["e2e.yml", "fakeweb.yml"])
def test_the_workflow_starts_with_changes_and_every_other_job_waits_for_its_answer(name):
    assert problems_changes(name, _text(name)) == []


def test_a_workflow_that_forgets_changes_somewhere_is_refused():
    e2e = _text("e2e.yml")
    host = _jobs(e2e)["e2e-host"]
    assert any("job e2e-host does not need" in p for p in problems_changes("e2e.yml", e2e.replace(host, host.replace("needs: [changes, ", "needs: [", 1), 1)))
    no_if = e2e.replace("    if: needs.changes.outputs.run == 'true'\n    uses: ./.github/workflows/core-on-index.yml", "    uses: ./.github/workflows/core-on-index.yml", 1)
    assert no_if != e2e and any("job core-on-index does not run only on" in p for p in problems_changes("e2e.yml", no_if))
    fake = _text("fakeweb.yml")
    assert any("CHANGES_RUN" in p for p in problems_changes("fakeweb.yml", fake.replace("CHANGES_RUN: ${{ needs.changes.outputs.run }}", "X: 1", 1)))
    assert any("from origin/main" in p for p in problems_changes("fakeweb.yml", fake.replace('git show origin/main:scripts/ci_changes.py > "$RUNNER_TEMP/ci_changes.py"', "true", 1)))
    assert any("pipefail" in p for p in problems_changes("fakeweb.yml", fake.replace("        shell: bash\n        env:\n          EVENT_NAME", "        env:\n          EVENT_NAME", 1)))


# ------------------------------------------------------------------------------ the verdict


def _all_run(workflow: str) -> dict:
    """Every job of the workflow as it ends when there was something to test."""
    needs = {name: {"result": "success", "outputs": {}} for name in ci_gate.TABLES[workflow][1]}
    needs["changes"]["outputs"] = {"run": "true", "files": "4"}
    if workflow == "e2e":
        needs["e2e-page-final-touched"]["outputs"] = {"touched": "true"}
    return needs


def _nothing_to_test(workflow: str, files: str = "3") -> dict:
    """Every job as it ends when `changes` said there was nothing to test."""
    needs = {name: {"result": "skipped", "outputs": {}} for name in ci_gate.TABLES[workflow][1]}
    needs["changes"] = {"result": "success", "outputs": {"run": "false", "files": files}}
    return needs


def _heavy(workflow: str) -> list:
    return sorted(n for n in ci_gate.TABLES[workflow][1] if n != "changes")


def _run(workflow: str, needs: dict, changes_run, event: str = "merge_group") -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("NEEDS", "EVENT_NAME", "WORKFLOW", "INPUT_ONLY", "CHANGES_RUN")}
    env.update(NEEDS=json.dumps(needs), EVENT_NAME=event, WORKFLOW=workflow)
    if changes_run is not None:
        env["CHANGES_RUN"] = changes_run
    return subprocess.run([sys.executable, str(GATE)], env=env, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("workflow", HEAVY)
def test_every_job_ran_and_passed_is_ok_when_there_was_something_to_test(workflow):
    done = _run(workflow, _all_run(workflow), "true")
    assert done.returncode == 0 and done.stdout.splitlines()[-1] == "ok", done.stdout


@pytest.mark.parametrize("workflow", HEAVY)
def test_nothing_to_test_with_every_heavy_job_skipped_is_green_and_says_so(workflow):
    needs = _nothing_to_test(workflow)
    assert ci_gate.verdict(needs, "merge_group", workflow, "", "false") == []
    done = _run(workflow, needs, "false")
    assert done.returncode == 0, done.stdout
    assert done.stdout.splitlines()[-1] == "ok: nothing to test (3 files changed, none under the paths of %s)" % workflow


@pytest.mark.parametrize("workflow", HEAVY)
def test_nothing_to_test_but_a_heavy_job_ran_and_failed_is_red(workflow):
    for job in _heavy(workflow):
        needs = _nothing_to_test(workflow)
        needs[job]["result"] = "failure"
        done = _run(workflow, needs, "false")
        assert done.returncode == 1 and "job %s is failure" % job in done.stdout, (job, done.stdout)
        assert not done.stdout.splitlines()[-1].startswith("ok"), job


@pytest.mark.parametrize("workflow", HEAVY)
def test_nothing_to_test_but_a_heavy_job_ran_is_red_even_when_it_passed(workflow):
    for job in _heavy(workflow):
        needs = _nothing_to_test(workflow)
        needs[job]["result"] = "success"
        assert any("job %s is success" % job in line for line in ci_gate.verdict(needs, "merge_group", workflow, "", "false")), job


@pytest.mark.parametrize("workflow", HEAVY)
def test_something_to_test_but_a_heavy_job_skipped_is_red(workflow):
    for job in _heavy(workflow):
        if workflow == "e2e" and job in ci_gate.E2E_BY_OUTPUT:
            continue                                         # skipped on purpose when the page oracle is not touched (run=true and touched=false)
        needs = _all_run(workflow)
        needs[job]["result"] = "skipped"
        done = _run(workflow, needs, "true")
        assert done.returncode == 1 and "FAIL %s: job %s is skipped. Fix: " % (ci_gate.TABLES[workflow][0], job) in done.stdout, (job, done.stdout)


@pytest.mark.parametrize("workflow", HEAVY)
@pytest.mark.parametrize("value", [None, "", "maybe", "True", "1"])
def test_a_missing_or_unreadable_answer_is_red_whatever_the_jobs_say(workflow, value):
    for needs in (_all_run(workflow), _nothing_to_test(workflow)):
        done = _run(workflow, needs, value)
        assert done.returncode == 1, (value, done.stdout)
        assert "FAIL %s: the output run of the job changes is" % ci_gate.TABLES[workflow][0] in done.stdout and RULE in done.stdout
        assert "ok" not in done.stdout.splitlines()
    assert ci_gate.verdict(_all_run(workflow), "merge_group", workflow)[0].startswith("FAIL "), "no answer given at all"


@pytest.mark.parametrize("workflow", HEAVY)
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
@pytest.mark.parametrize("run", ["true", "false"])
def test_a_changes_job_that_did_not_succeed_is_red_either_way(workflow, result, run):
    needs = _all_run(workflow) if run == "true" else _nothing_to_test(workflow)
    needs["changes"]["result"] = result
    assert any("job changes is %s" % result in line for line in ci_gate.verdict(needs, "merge_group", workflow, "", run))


def test_a_push_and_a_dispatch_run_everything_so_nothing_there_may_be_skipped():
    for event in ("push", "workflow_dispatch"):
        needs = _all_run("fakeweb")
        needs["walk"]["result"] = "skipped"
        assert any("job walk is skipped" in line for line in ci_gate.verdict(needs, event, "fakeweb", "", "true"))


def test_the_tests_workflow_has_no_changes_job_and_is_judged_as_before():
    assert "changes" not in ci_gate.FIX and "tests" not in ci_gate.HAS_CHANGES
    assert ci_gate.verdict({n: {"result": "success", "outputs": {}} for n in ci_gate.FIX}, "pull_request") == []
