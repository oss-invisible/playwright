"""The e2e of the suite `wrapper.e2e.udp.exit` need root, and e2e.yml runs them as root, in their own step, and nowhere else (no browser here).

Run 43262636367 of PR 483: e2e.yml collected every `@pytest.mark.e2e` test as the runner user, and the seven tests whose lab exit is a documentation
address added to `lo` (`ip addr add`) ERRORED: "cannot hold 198.51.100.9 on lo (needs root and iproute2 on Linux)". The gate runs them as root (`sudo -H -E
env -u XDG_RUNTIME_DIR INVPW_REQUIRE_TRAP=1 ...`, the engine copied to /opt/ff). e2e.yml now does the same in a step of its own and leaves them out of the
run of the runner user - not by a skip, which would be silent. The Linux e2e runs in three parts (the job `e2e-linux`, a matrix over the rows
`wrapper.e2e.suite.linux.1` to `.3`), and the root tests run in ONE of them. This test holds them together, reading the suite's list and the rows from
ci/required-suites.json and the workflow as text (PyYAML is not a dependency of this package):

  * the run of the runner user takes its files from the row of its part (`ci_verdict.py args --suite "wrapper.e2e.suite.linux.${{ matrix.part }}"`),
    and no row of the parts holds a file or a node of the suite (a file entry that would hold a node of it counts);
  * the root step runs exactly those, under sudo with the gate's environment, from /opt/ff, with no rerun plugin and no `|| true` after the judge,
    in one part of the matrix only (`matrix.part == N` in its `if:`, N one of the parts);
  * no other step names them.

Known-bad inputs: e2e.yml as it was (no root step, no exclusion) -> `problems` names both; the root step BEFORE the run of the runner user
(a5606dd, run 43271443150: every launch after it waited for the browser until its timeout) -> `problems` names the order; a row of a part that holds
a root test; a run that does not read the row of its part; a root step with no part, or with a part the matrix does not have.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "e2e.yml"
SUITE = "wrapper.e2e.udp.exit"
#: The job of the Linux parts and the prefix of the rows its matrix runs, one per part.
PARTS_JOB = "e2e-linux"
PART_ROW = "wrapper.e2e.suite.linux."
#: How the run of a Linux part reads its files: the row of its part, as the Windows parts read theirs.
ROW_ARGS = 'python scripts/ci_verdict.py args --required ci/required-suites.json --suite "wrapper.e2e.suite.linux.${{ matrix.part }}" --os linux'


def _suite(name: str = SUITE) -> dict:
    return next(s for s in json.loads((ROOT / "ci" / "required-suites.json").read_text(encoding="utf-8")) if s["id"] == name)


def _rows() -> dict:
    """{part: entries} of the rows the Linux parts run (`wrapper.e2e.suite.linux.N`)."""
    rows = json.loads((ROOT / "ci" / "required-suites.json").read_text(encoding="utf-8"))
    return {r["id"][len(PART_ROW):]: r["tests"] for r in rows if r["id"].startswith(PART_ROW)}


def _holds(entry: str, test: str) -> bool:
    """Does the row entry `entry` (a file or a `file::test`) share a test with the suite's entry `test` (a file or a `file::test`)?"""
    if entry.split("::", 1)[0] != test.split("::", 1)[0]:
        return False
    return "::" not in entry or "::" not in test or entry == test


def row_problems(suite_tests: list, rows: dict, suite_id: str) -> list:
    """-> each row of a Linux part that holds a test of the suite (empty if none does)."""
    return ["the row %s%s holds %s, a test of %s: it would run in the part, outside its own step" % (PART_ROW, part, t, suite_id)
            for part, entries in sorted(rows.items()) for e in entries for t in suite_tests if _holds(e, t)]


def _matrix_parts(text: str) -> list:
    """The values of `part:` in the matrix of the job of the Linux parts."""
    job = re.search(r"^  %s:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)" % re.escape(PARTS_JOB), text, re.M | re.S)
    found = re.search(r"^ +part: \[([^\]]*)\]", job.group(1), re.M) if job else None
    return [p.strip() for p in found.group(1).split(",")] if found else []


def step_if(body: str) -> str:
    """The `if:` of a step (empty if it has none), read from the lines before its `run: |`."""
    found = re.search(r"^ +if: (.+)$", body.partition("run: |")[0], re.M)
    return found.group(1).strip() if found else ""


def step_part(body: str) -> str:
    """The part of the matrix a step is held to by its `if:` (`matrix.part == N`), or empty."""
    found = re.search(r"\bmatrix\.part == (\d+)\b", step_if(body))
    return found.group(1) if found else ""


def _steps(text: str) -> list:
    """(name, body) of each step, by the dash at the indentation of the first `- name:` (comments and bodies included)."""
    marks = [m for m in re.finditer(r"^( +)- name: (.+)$", text, re.M)]
    return [(m.group(2).strip(), text[m.start():(marks[i + 1].start() if i + 1 < len(marks) else len(text))]) for i, m in enumerate(marks)]


def problems(text: str, suite: dict, rows: dict | None = None) -> list:
    rows = _rows() if rows is None else rows
    out = row_problems(suite["tests"], rows, suite["id"])
    files = [t for t in suite["tests"] if "::" not in t]
    nodes = [t for t in suite["tests"] if "::" in t]
    steps = _steps(text)
    root = [body for name, body in steps if "root e2e" in name]
    # the Linux run (xvfb): the Windows parts of e2e.yml leave the linux_only suites out by reading ci/required-suites.json
    runner = [body for name, body in steps if "scripts/run_e2e.py" in body and "xvfb-run" in body]
    if len(root) != 1:
        return ["there is no (or more than one) step for the root e2e: %d" % len(root)]
    if len(runner) != 1:
        return ["the run of the runner user is not one step: %d" % len(runner)]
    root, runner = root[0], runner[0]
    bodies = [body for _name, body in steps]
    if bodies.index(root) < bodies.index(runner):
        out.append("the root step comes BEFORE the run of the runner user: what root leaves behind reaches every launch after it")
    if "success() || failure()" not in step_if(root):
        out.append("the root step does not run after a red run of the runner user (`if: ... (success() || failure())`)")
    parts = _matrix_parts(text)
    if not parts or sorted(parts) != sorted(rows):
        out.append("the matrix of %s (%s) is not the rows %s* (%s)" % (PARTS_JOB, ", ".join(parts) or "none", PART_ROW, ", ".join(sorted(rows))))
    if step_part(root) not in parts:
        out.append("the root step is not held to one part of the matrix (`matrix.part == N` in its `if:`): it would run in every part, or in none")
    listed = {key: (re.search(r"%s: (.*)" % key, root).group(1).split() if re.search(r"%s: (.*)" % key, root) else []) for key in ("ROOT_FILES", "ROOT_NODES")}
    if sorted(listed["ROOT_FILES"]) != sorted(files):
        out.append("ROOT_FILES of the root step is not the files of the suite: %s" % files)
    if sorted(listed["ROOT_NODES"]) != sorted(nodes):
        out.append("ROOT_NODES of the root step is not the nodes of the suite: %s" % nodes)
    # the step that writes the files of the part comes right before the run
    reads = [body for _name, body in steps if ROW_ARGS in body]
    if len(reads) != 1 or bodies.index(reads[0]) + 1 != bodies.index(runner):
        out.append("the files of the run of the runner user are not read from the row of its part, in the step before it (%s)" % ROW_ARGS)
    if 'scripts/run_e2e.py "$FF" "${ARGS[@]}"' not in runner:
        out.append("the run of the runner user does not pass the files of its row to run_e2e.py")
    if 'ROOT_TESTS: "%d"' % suite["min_tests"] not in root:
        out.append("the root step does not demand exactly %d tests" % suite["min_tests"])
    for need in ("sudo -H -E env -u PYTEST_ADDOPTS -u XDG_RUNTIME_DIR INVPW_REQUIRE_TRAP=1", "sudo cp -a", "/opt/ff", "-p no:rerunfailures", "--junitxml"):
        if need not in root:
            out.append("the root step lacks `%s`" % need)
    if not all('counts["%s"]' % k in root for k in ("failures", "errors", "skipped")):
        out.append("the root step does not refuse failures, errors and skips")
    for name, body in steps:
        if body is root or body is runner:
            continue
        for t in files + nodes:
            if t in body.replace("\n", " ") and "ROOT_" not in body:
                out.append("step %r names %s outside the root step" % (name, t))
    return out


def test_the_workflow_runs_the_root_e2e_in_the_root_step_and_leaves_them_out_of_the_other_run():
    assert problems(WORKFLOW.read_text(encoding="utf-8"), _suite()) == []


def test_the_suite_is_the_one_the_gate_runs_as_root_on_linux_only():
    suite = _suite()
    assert suite["os"] == ["linux"] and suite["needs_root"] == ["linux"] and suite["min_tests"] == 7 and suite["exact"] is True


BAD_BEFORE = """
    steps:
      - name: Fetch
        run: echo hi
      - name: Run the full e2e suite under a virtual display
        run: xvfb-run -a python -u scripts/run_e2e.py "$FF"
"""


def test_the_workflow_as_it_was_is_refused():
    found = problems(BAD_BEFORE, _suite())
    assert found and "no (or more than one) step for the root e2e" in found[0]


def test_a_row_of_a_part_that_holds_a_root_test_is_refused():
    text = WORKFLOW.read_text(encoding="utf-8")
    rows = _rows()
    rows["2"] = sorted(rows["2"] + ["tests/test_a_proxy_that_cannot_carry_udp_leaks_nothing_direct_e2e.py"])
    assert any("linux.2 holds tests/test_a_proxy" in p for p in problems(text, _suite(), rows))
    # a file entry holds the node of the suite in it
    rows = _rows()
    rows["3"] = sorted(rows["3"] + ["tests/test_the_whole_udp_chain_by_name_e2e.py"])
    assert any("linux.3 holds tests/test_the_whole_udp_chain_by_name_e2e.py::" in p for p in problems(text, _suite(), rows))


def test_the_rows_of_the_parts_are_the_matrix_of_the_job():
    assert sorted(_rows()) == ["1", "2", "3"] and _matrix_parts(WORKFLOW.read_text(encoding="utf-8")) == ["1", "2", "3"]
    text = WORKFLOW.read_text(encoding="utf-8").replace("        part: [1, 2, 3]\n", "        part: [1, 2]\n", 1)
    assert any("is not the rows" in p for p in problems(text, _suite()))


def test_a_run_that_does_not_read_the_row_of_its_part_is_refused():
    text = WORKFLOW.read_text(encoding="utf-8").replace('--suite "wrapper.e2e.suite.linux.${{ matrix.part }}"', '--suite "wrapper.e2e.suite.linux.1"')
    assert any("not read from the row of its part" in p for p in problems(text, _suite()))
    text = WORKFLOW.read_text(encoding="utf-8").replace('scripts/run_e2e.py "$FF" "${ARGS[@]}"', 'scripts/run_e2e.py "$FF"')
    assert any("does not pass the files of its row" in p for p in problems(text, _suite()))


def test_a_root_step_in_every_part_or_in_a_part_that_does_not_exist_is_refused():
    text = WORKFLOW.read_text(encoding="utf-8")
    root = next(body for name, body in _steps(text) if "root e2e" in name)
    part = step_part(root)
    assert part in _matrix_parts(text)
    everywhere = text.replace(root, root.replace("matrix.part == %s && " % part, "", 1))
    assert any("not held to one part" in p for p in problems(everywhere, _suite()))
    nowhere = text.replace(root, root.replace("matrix.part == %s" % part, "matrix.part == 4", 1))
    assert any("not held to one part" in p for p in problems(nowhere, _suite()))


def test_a_root_step_that_may_skip_is_refused():
    text = WORKFLOW.read_text(encoding="utf-8").replace('counts["skipped"]', 'counts["nothing"]')
    assert any("refuse failures" in p for p in problems(text, _suite()))


def test_a_root_step_before_the_run_of_the_runner_user_is_refused():
    """Run 43271443150 of PR 483: with the root step first, every launch of the run that followed waited for the browser until its timeout."""
    text = WORKFLOW.read_text(encoding="utf-8")
    steps = _steps(text)
    root = next(body for name, body in steps if "root e2e" in name)
    runner = next(body for name, body in steps if "scripts/run_e2e.py" in body)
    assert runner + root in text, "the two steps are expected next to each other, the run of the runner user first"
    swapped = text.replace(runner + root, root + runner)
    assert any("BEFORE the run of the runner user" in p for p in problems(swapped, _suite()))


def test_a_root_step_that_does_not_run_after_a_red_run_is_refused():
    root = next(body for name, body in _steps(WORKFLOW.read_text(encoding="utf-8")) if "root e2e" in name)
    text = WORKFLOW.read_text(encoding="utf-8").replace(root, root.replace("(success() || failure())", "success()", 1))
    assert any("success() || failure()" in p for p in problems(text, _suite()))


def test_a_root_test_named_in_another_step_is_refused():
    text = WORKFLOW.read_text(encoding="utf-8") + (
        "      - name: Also\n        run: python -m pytest tests/test_a_proxy_that_cannot_carry_udp_leaks_nothing_direct_e2e.py\n")
    assert any("outside the root step" in p for p in problems(text, _suite()))
