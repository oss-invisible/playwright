"""One CI run per commit, and a new push replaces the run it supersedes.

A pull request is covered by `pull_request`; `push` stays only for main and for
the tags `publish.yml` starts on. What the workflows promise, checked here on
the files themselves:

  1. A workflow with a `pull_request` trigger has no `push` trigger that also
     matches feature branches (an unfiltered push, `**`, or a wildcard).
  2. The push triggers the release reads are still there. `publish.yml` asks the
     checks API for the `e2e` check-run of the commit being tagged
     (scripts/e2e_is_green.py), so `e2e.yml` must run on every push to main with
     the job name `e2e (linux, xvfb)`, and the required `gate` context of
     `tests.yml` keeps its name.
  3. A workflow that runs on pull requests has a concurrency group that cancels
     on pull requests and on nothing else. The group used to be keyed on the
     ref, which is `refs/heads/main` for every push to main: the next push
     cancelled the run of the commit before it, and a cancelled e2e refuses a
     release (`cancelled` is not in e2e_is_green.FINE). Publishing and scheduled
     workflows never cancel.
  4. A hunt workflow has its own group and caps every matrix at 8 parallel
     jobs.
  5. The check `e2e (linux, xvfb)` is green only when every part of the Linux
     e2e is. The Linux e2e runs in three parts (the job `e2e-linux`, a matrix
     over the rows `wrapper.e2e.suite.linux.1` to `.3`), and the name of 2. is
     a job of its own that waits for them. A job that `needs` another is
     SKIPPED when that one fails unless its `if:` says `always()`, and a
     skipped check is not red. So: the job `e2e` needs the parts and
     `core-on-index`; its `if:` is `always()` and the gate of the parts (the
     core on the index, not the Windows-only dispatch) joined by `&&`, so that
     it is skipped exactly when the parts are skipped for the core, as the
     Linux job was before the split, and runs in every other case; its one
     step binds `needs.e2e-linux.result` (`success` only when every part
     succeeded) and fails unless it is `success`, in exactly the form
     `if [ "$PARTS" != "success" ]; then ... exit 1 ... fi`, with no other
     result named in its code (a cancelled part measured nothing, a skipped
     one ran nothing); no `continue-on-error`; no other job has its name.
     Known-bad: without `always()`, accepting `skipped`, not needing the
     parts, exiting 0, reading another job, dropping the gate of the core,
     `success() || failure()`, the single job before the split.

The checkers are tested on known-bad inputs first, because a check that has
only ever said "fine" is not known to be able to say anything else.

PyYAML is not a dependency of this package, so the workflows are read with a
small parser for the subset these files use (mappings, block and flow lists,
block scalars skipped, steps skipped); the script of the step of 5. is read
as text.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

#: The one expression a cancelling group may use, whitespace aside.
CANCEL_ONLY_PULL_REQUESTS = "${{github.event_name=='pull_request'}}"
#: Publishing and scheduled workflows: an upload or a snapshot that has started
#: is allowed to finish.
NEVER_CANCEL = {"publish.yml", "publish-wiki.yml", "counters.yml"}
MAX_HUNT_PARALLEL = 8


# --------------------------------------------------------------- the reader

_KEY = re.compile(r"""^(?P<k>"[^"]*"|'[^']*'|[^\s:#'"\[{-][^:]*?)\s*:(?:\s+(?P<v>.*))?$""")
_BLOCK_SCALAR = re.compile(r"^[|>][+-]?\d*$")


def _significant(text: str) -> list[tuple[int, str]]:
    out = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append((len(raw) - len(raw.lstrip(" ")), stripped))
    return out


def _scalar(v: str):
    v = v.strip()
    if " #" in v and not (v[:1] in "'\"" or "${{" in v):
        v = v.split(" #", 1)[0].strip()
    if v.startswith("[") and v.endswith("]"):
        return [_scalar(x) for x in v[1:-1].split(",") if x.strip()]
    if v in ("{}", ""):
        return {} if v == "{}" else None
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    if v in ("true", "false"):
        return v == "true"
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def _parse(lines, i, indent):
    """A mapping or a list starting at lines[i], all at `indent`."""
    if lines[i][1].startswith("- ") or lines[i][1] == "-":
        items = []
        while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("-"):
            body = lines[i][1][1:].strip()
            if body and _KEY.match(body) and not body.startswith(("'", '"')):
                # "- key: value": the item is a mapping whose first line sits at indent + 2
                lines[i] = (indent + 2, body)
                item, i = _parse(lines, i, indent + 2)
                items.append(item)
            else:
                items.append(_scalar(body))
                i += 1
        return items, i
    out = {}
    while i < len(lines) and lines[i][0] == indent:
        m = _KEY.match(lines[i][1])
        if not m:
            i += 1
            continue
        key, value = m.group("k").strip("'\""), m.group("v")
        i += 1
        if value is not None and (_BLOCK_SCALAR.match(value.split(" #")[0].strip())):
            while i < len(lines) and lines[i][0] > indent:
                i += 1
            out[key] = "<block>"
        elif value:
            out[key] = _scalar(value)
        elif i < len(lines) and (lines[i][0] > indent or
                                 (lines[i][0] == indent and lines[i][1].startswith("-"))):
            if key == "steps":
                while i < len(lines) and (lines[i][0] > indent or lines[i][1].startswith("-")):
                    if lines[i][0] < indent:
                        break
                    i += 1
                out[key] = "<steps>"
            else:
                out[key], i = _parse(lines, i, lines[i][0])
        else:
            out[key] = None
    return out, i


def load(text: str) -> dict:
    lines = _significant(text)
    spec, _ = _parse(lines, 0, lines[0][0])
    return spec


def triggers(spec: dict) -> dict:
    on = spec.get("on")
    if isinstance(on, str):
        return {on: {}}
    if isinstance(on, list):
        return {e: {} for e in on}
    return {e: (c if isinstance(c, dict) else {}) for e, c in (on or {}).items()}


def _flat(expr) -> str:
    return re.sub(r"\s+", "", str(expr))


# ------------------------------------------------------------- the checkers

def problems_double_run(name: str, spec: dict) -> list[str]:
    ev = triggers(spec)
    if "pull_request" not in ev or "push" not in ev:
        return []
    push = ev["push"]
    branches = push.get("branches")
    if not branches:
        if push.get("tags") and not push.get("branches-ignore"):
            return []  # tags only
        return [f"{name}: `push` without a branch list also runs on feature branches"]
    bad = [b for b in branches if b != "main"]
    if bad:
        return [f"{name}: `push` matches {bad}, which a pull request already covers"]
    return []


def problems_concurrency(name: str, spec: dict) -> list[str]:
    ev = triggers(spec)
    conc = spec.get("concurrency")
    cancel = conc.get("cancel-in-progress") if isinstance(conc, dict) else None
    out = []
    if name in NEVER_CANCEL:
        if conc is not None and cancel is not False:
            out.append(f"{name}: publishing/scheduled workflow may cancel ({cancel!r})")
        return out
    if "hunt" in name:
        return out  # judged by problems_hunt
    if "pull_request" not in ev:
        if cancel not in (None, False) and _flat(cancel) != CANCEL_ONLY_PULL_REQUESTS:
            out.append(f"{name}: cancels without a pull request ({cancel!r})")
        return out
    if not isinstance(conc, dict):
        return [f"{name}: runs on pull requests and has no concurrency group"]
    group = str(conc.get("group", ""))
    if _flat(cancel) != CANCEL_ONLY_PULL_REQUESTS:
        out.append(f"{name}: cancel-in-progress is {cancel!r}, not "
                   f"`github.event_name == 'pull_request'`")
    if "github.event.pull_request.number" not in group:
        out.append(f"{name}: the group is not keyed on the pull request number")
    if "github.run_id" not in group:
        out.append(f"{name}: outside a pull request the group is not unique to the run, "
                   f"so a newer pending run of main replaces an older one")
    if "github.ref" in group or "head_ref" in group:
        out.append(f"{name}: the group is keyed on the ref, which is main for every push to main")
    return out


def problems_hunt(name: str, spec: dict) -> list[str]:
    if "hunt" not in name:
        return []
    out = []
    conc = spec.get("concurrency")
    if not isinstance(conc, dict) or "hunt" not in (
            str(conc.get("group", "")) + str(spec.get("name", ""))).lower():
        out.append(f"{name}: a hunt needs a concurrency group of its own")
    for job, body in (spec.get("jobs") or {}).items():
        strategy = (body or {}).get("strategy") if isinstance(body, dict) else None
        if not isinstance(strategy, dict) or "matrix" not in strategy:
            continue
        cap = strategy.get("max-parallel")
        if not isinstance(cap, int) or cap > MAX_HUNT_PARALLEL:
            out.append(f"{name}: matrix job `{job}` has max-parallel {cap!r} "
                       f"(needs an integer <= {MAX_HUNT_PARALLEL})")
    return out


def all_workflows():
    return sorted(WORKFLOWS.glob("*.yml"))


def _load(path: Path) -> dict:
    return load(path.read_text(encoding="utf-8"))


# ----------------------------------------------------- the checkers can fail

OLD_CI = """
name: ci
on:
  push:
    branches: ["**"]
  pull_request:
"""
BAD_CONCURRENCY = """
name: x
on:
  pull_request:
concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
"""
GOOD_CONCURRENCY = """
name: x
on:
  push:
    branches: [main]
  pull_request:
concurrency:
  group: ${{ github.workflow }}-${{ github.event_name == 'pull_request' && format('pr-{0}', github.event.pull_request.number) || github.run_id }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
"""
HUNT_WITHOUT_CAP = """
name: e2e hunt
on:
  workflow_dispatch:
concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
jobs:
  suite:
    strategy:
      matrix:
        include: ${{ fromJSON(needs.plan.outputs.m) }}
"""


def test_the_reader_reads_what_the_checkers_need():
    spec = load(GOOD_CONCURRENCY)
    assert triggers(spec)["push"]["branches"] == ["main"]
    assert "pull_request" in triggers(spec)
    assert spec["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert load(HUNT_WITHOUT_CAP)["jobs"]["suite"]["strategy"]["matrix"]["include"].startswith("${{")


def test_the_old_trigger_is_caught():
    assert problems_double_run("ci.yml", load(OLD_CI))
    assert problems_double_run("ci.yml", load(GOOD_CONCURRENCY)) == []


def test_a_group_keyed_on_the_ref_that_cancels_everywhere_is_caught():
    assert len(problems_concurrency("e2e.yml", load(BAD_CONCURRENCY))) >= 3
    assert problems_concurrency("e2e.yml", load(GOOD_CONCURRENCY)) == []


def test_a_hunt_matrix_without_a_cap_is_caught():
    assert problems_hunt("e2e-hunt.yml", load(HUNT_WITHOUT_CAP))
    capped = HUNT_WITHOUT_CAP.replace("    strategy:\n", "    strategy:\n      max-parallel: 8\n")
    assert problems_hunt("e2e-hunt.yml", load(capped)) == []
    assert problems_hunt("e2e-hunt.yml", load(capped.replace("max-parallel: 8", "max-parallel: 9")))


# ------------------------------------------------------ the workflows here

def test_no_workflow_runs_twice_for_a_commit_of_a_pull_request():
    problems = []
    for path in all_workflows():
        problems += problems_double_run(path.name, _load(path))
    assert not problems, "\n".join(problems)


def test_the_push_triggers_the_release_reads_are_still_there():
    e2e = triggers(_load(WORKFLOWS / "e2e.yml"))
    assert e2e["push"]["branches"] == ["main"], \
        "scripts/e2e_is_green.py needs the e2e of the push to main on the tagged commit"
    assert "pull_request" in e2e
    tests = triggers(_load(WORKFLOWS / "tests.yml"))
    assert tests["push"]["branches"] == ["main"] and "pull_request" in tests
    publish = triggers(_load(WORKFLOWS / "publish.yml"))
    assert publish["push"]["tags"] == ["v*"], "publish must start on a release tag"


def test_the_names_the_release_and_the_branch_protection_read_are_unchanged():
    e2e = _load(WORKFLOWS / "e2e.yml")
    assert e2e["name"] == "e2e" and e2e["jobs"]["e2e"]["name"] == "e2e (linux, xvfb)"
    tests = _load(WORKFLOWS / "tests.yml")
    assert tests["name"] == "tests" and "gate" in tests["jobs"]
    assert "name" not in tests["jobs"]["gate"]


def test_every_workflow_cancels_on_pull_requests_and_nowhere_else():
    problems = []
    for path in all_workflows():
        problems += problems_concurrency(path.name, _load(path))
    assert not problems, "\n".join(problems)


def test_a_hunt_has_its_own_group_and_caps_its_matrices():
    hunts = [p for p in all_workflows() if "hunt" in p.name]
    assert hunts, "e2e-hunt.yml is gone: this check would pass on nothing"
    problems = []
    for path in hunts:
        problems += problems_hunt(path.name, _load(path))
    assert not problems, "\n".join(problems)


# ------------------------------------- 5. the Linux check waits for every part

CHECK_JOB = "e2e"
CHECK_NAME = "e2e (linux, xvfb)"
PARTS_JOB = "e2e-linux"
INDEX_GATE = "core-on-index"
PARTS_VAR = "PARTS"
PARTS_TEST = 'if [ "$%s" != "success" ]; then' % PARTS_VAR


def _conjuncts(expr) -> list:
    return [c.strip() for c in str(expr or "").split("&&")]


def _job_text(text: str, job: str) -> str:
    """The text of one job under `jobs:` (two-space indentation), comments of the next job included."""
    body = text.split("\njobs:\n", 1)[1] if "\njobs:\n" in text else ""
    found = re.search(r"^  %s:\s*\n(.*?)(?=^  [A-Za-z0-9_-]+:\s*$|\Z)" % re.escape(job), body, re.M | re.S)
    return found.group(1) if found else ""


def problems_linux_check(text: str) -> list:
    jobs = load(text).get("jobs", {})
    check, parts = jobs.get(CHECK_JOB), jobs.get(PARTS_JOB)
    if not isinstance(check, dict) or not isinstance(parts, dict):
        return ["e2e.yml has no job %r (the check) or no job %r (the parts)" % (CHECK_JOB, PARTS_JOB)]
    out = []
    if check.get("name") != CHECK_NAME:
        out.append("the job %r is named %r, not %r" % (CHECK_JOB, check.get("name"), CHECK_NAME))
    others = sorted(k for k, j in jobs.items() if k != CHECK_JOB and isinstance(j, dict) and j.get("name") == CHECK_NAME)
    if others:
        out.append("another job is named %r too: %s" % (CHECK_NAME, others))
    if "matrix.part" not in str(parts.get("name", "")):
        out.append("the parts are not named by their part (%r)" % parts.get("name"))
    needs = check.get("needs")
    needs = [needs] if isinstance(needs, str) else list(needs or [])
    if PARTS_JOB not in needs:
        out.append("the check does not need the parts (%s): it would not wait for them, nor read their result" % PARTS_JOB)
    if INDEX_GATE not in needs:
        out.append("the check does not need %s: its `if:` cannot read whether the core is on the index" % INDEX_GATE)
    if "||" in str(check.get("if", "")):
        out.append("the `if:` of the check has a `||`: it must be `always()` and the gate of the parts")
    cond = _conjuncts(check.get("if"))
    if "always()" not in cond:
        out.append("the `if:` of the check has no `always()`: a red or cancelled part leaves it skipped, and skipped is not red")
    gate = [c for c in _conjuncts(parts.get("if")) if c]
    if not gate or sorted(c for c in cond if c != "always()") != sorted(gate):
        out.append("the `if:` of the check is not `always()` and the gate of the parts (%s): %r" % (" && ".join(gate), check.get("if")))
    if "continue-on-error" in check:
        out.append("the check has continue-on-error: a red step would leave it green")
    body = _job_text(text, CHECK_JOB)
    steps = re.findall(r"^      - name: ", body, re.M)
    if len(steps) != 1:
        out.append("the check is not one step: %d" % len(steps))
    if not re.search(r"^ +%s: \$\{\{ needs\.%s\.result \}\}\s*$" % (PARTS_VAR, re.escape(PARTS_JOB)), body, re.M):
        out.append("the step does not bind %s to `${{ needs.%s.result }}`" % (PARTS_VAR, PARTS_JOB))
    script = body.partition("run: |")[2]
    code = [line.strip() for line in script.splitlines() if line.strip() and not line.strip().startswith(("#", "echo "))]
    if code != [PARTS_TEST, "exit 1", "fi"]:
        out.append("the script is not `%s exit 1 fi` (apart from echo lines): %s" % (PARTS_TEST, code))
    for line in code:
        for word in ("skipped", "cancelled", "failure", "neutral", "&&", "||"):
            if word in line:
                out.append("the script names %r outside an echo: %s" % (word, line))
    return out


def _e2e_with_check(new_body: str) -> str:
    text = (WORKFLOWS / "e2e.yml").read_text(encoding="utf-8")
    old = _job_text(text, CHECK_JOB)
    assert old and text.count(old) == 1
    return text.replace(old, new_body)


def _check_body() -> str:
    return _job_text((WORKFLOWS / "e2e.yml").read_text(encoding="utf-8"), CHECK_JOB)


def test_the_linux_check_is_green_only_when_every_part_is():
    assert problems_linux_check((WORKFLOWS / "e2e.yml").read_text(encoding="utf-8")) == []


def test_a_linux_check_without_always_is_refused():
    body = _check_body().replace("if: always() && ", "if: ", 1)
    assert any("no `always()`" in p for p in problems_linux_check(_e2e_with_check(body)))


def test_a_linux_check_that_accepts_skipped_is_refused():
    body = _check_body().replace(PARTS_TEST, 'if [ "$PARTS" != "success" ] && [ "$PARTS" != "skipped" ]; then', 1)
    found = problems_linux_check(_e2e_with_check(body))
    assert any("names 'skipped'" in p for p in found) and any("is not `if" in p for p in found), found


def test_a_linux_check_that_does_not_need_the_parts_is_refused():
    body = _check_body().replace("needs: [core-on-index, e2e-linux]", "needs: [core-on-index]", 1)
    assert body != _check_body()
    assert any("does not need the parts" in p for p in problems_linux_check(_e2e_with_check(body)))


def test_a_linux_check_that_exits_0_is_refused():
    body = _check_body().replace("exit 1", "exit 0", 1)
    assert any("is not `if" in p for p in problems_linux_check(_e2e_with_check(body)))


def test_a_linux_check_that_reads_another_job_is_refused():
    body = _check_body().replace("needs.e2e-linux.result", "needs.core-on-index.result", 1)
    assert any("does not bind PARTS" in p for p in problems_linux_check(_e2e_with_check(body)))


def test_a_linux_check_that_drops_the_gate_of_the_core_is_refused():
    body = _check_body().replace(" && needs.core-on-index.outputs.available == 'true'", "", 1)
    assert body != _check_body()
    assert any("not `always()` and the gate of the parts" in p for p in problems_linux_check(_e2e_with_check(body)))


def test_a_linux_check_that_says_success_or_failure_is_refused():
    body = _check_body().replace("if: always() && ", "if: (success() || failure()) && ", 1)
    found = problems_linux_check(_e2e_with_check(body))
    assert any("has a `||`" in p for p in found) and any("no `always()`" in p for p in found), found


def test_the_single_linux_job_before_the_split_is_refused():
    """e2e.yml before the parts: the job `e2e` ran the tests itself, and there was no `e2e-linux`."""
    text = (WORKFLOWS / "e2e.yml").read_text(encoding="utf-8").replace("\n  e2e-linux:\n", "\n  e2e-linux-old:\n", 1)
    assert any("no job 'e2e-linux'" in p for p in problems_linux_check(text))
