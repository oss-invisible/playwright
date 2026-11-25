"""The verdict job of a workflow that gates a merge: every job it waits for must have ended in `success`.

    NEEDS='<toJSON(needs)>' EVENT_NAME=<github.event_name> WORKFLOW=<tests|e2e|fakeweb> python scripts/ci_gate.py

Three workflows have one: the job `gate` of tests.yml, `e2e verdict` of e2e.yml and `fakeweb verdict` of fakeweb.yml. WORKFLOW (default `tests`) picks
the tables that belong to the workflow. A pull request runs the fast set (tests.yml); e2e.yml and fakeweb.yml run in the merge queue run
(`merge_group`), and that run decides. `gate` is the one context main requires on a pull request.

Rule ci-13: nothing counts as passed that did not pass, so `skipped`, `cancelled` and `failure` are all red. A job that was skipped ran nothing: when
`unit` was skipped for a core that was not on the index, the old gate said "ok".

The only jobs that may end in something else than `success` are the ones that are conditional BY DESIGN, and each says when:

* CONDITIONAL: the events on which the job runs; on any other event it is expected to be `skipped`, and on those events it must be `success`;
* E2E_BY_OUTPUT: a job that runs only when another job of the workflow says so by an output (the final-seed shards, when the page oracle is touched);
* E2E_NARROWED: what `workflow_dispatch` with `only: windows-control-a` leaves out on purpose (that run is the Windows job of one shortcut alone).

Nothing else is forgiven, and a job the workflow waits for that the table does not know is red as well (the table and the `needs:` of the verdict job
cannot drift apart without tests/test_the_gate_is_red_when_a_needed_job_is_skipped.py and tests/test_the_workflows_are_ready_for_the_merge_queue.py
saying so).

Prints one line per job that is not as expected, `FAIL <verdict job>: job <name> is <result>. Fix: <how>. Rule: ci-13 ...`, and exits 1; prints `ok` and
exits 0 when every job is.
"""
from __future__ import annotations

import json
import os
import sys

RULE = "ci-13 nothing counts as passed that did not pass"

_CORE = "wait for the publish of the pinned invisible-core to finish, or pin a published core, then re-run"

# tests.yml: the jobs `gate` waits for, each with what to do when it is not green.
FIX = {
    "core-on-index": _CORE,
    "unit": "open the job pytest of the failing system and fix its test; when it is skipped, fix the job it needs (core-on-index) first",
    "docs-api": "open the job and fix the guide that names an API that does not exist; when it is skipped, fix core-on-index first",
    "english": "open the job and translate the lines it names; when it is skipped, fix core-on-index first",
    "claims": "open the job and fix the README sentence no measurement supports; when it is skipped, fix core-on-index first",
    "way-in": "open the job and make the install instruction uv; when it is skipped, fix core-on-index first",
    "install-job": "open the job and fix the user-install workflow; when it is skipped, fix core-on-index first",
    "noreply-identity": "open the job and rewrite the commit or tag whose email is not a GitHub noreply address",
    "version": "bump the version in pyproject.toml: the one this change proposes is already published",
}

# Conditional by design: job -> the events on which it runs. On any other event it must be `skipped`.
CONDITIONAL = {
    # `version` runs where a change is proposed: on a pull request and in the merge queue run (it compares the version the change proposes with the
    # published one, so it must run in the queue too). On main straight after a release the version IS the published one until somebody bumps, and a
    # check that is red at rest is one people learn to skip. So a push to main skips it on purpose.
    "version": ("pull_request", "merge_group"),
}

# e2e.yml: the jobs `e2e verdict` waits for.
E2E_FIX = {
    "core-on-index": _CORE,
    "linux-deps-e2e": "open the job linux-deps-e2e and fix the package list of the Linux jobs; when it is skipped, fix core-on-index first",
    "e2e-linux": "open the failing part of `e2e (linux, part N)` and fix its test; when it is skipped, fix core-on-index or linux-deps-e2e first",
    "e2e": "open the job `e2e (linux, xvfb)`: one of the three Linux parts did not pass",
    "e2e-windows-control-a": "open the job `e2e (windows, Control+A)` and fix the shortcut test",
    "e2e-windows": "open the failing part of `e2e (windows, part N)` and fix its test; when it is skipped, fix core-on-index first",
    "e2e-network": "open the job of the network oracle on Linux and fix its test; when it is skipped, fix core-on-index or linux-deps-e2e first",
    "e2e-network-windows": "open the job of the network oracle on Windows and fix its test; when it is skipped, fix core-on-index first",
    "e2e-host": "open the job of the host oracle on Linux and fix its test; when it is skipped, fix core-on-index or linux-deps-e2e first",
    "e2e-host-windows": "open the job of the host oracle on Windows and fix its test; when it is skipped, fix core-on-index first",
    "e2e-page": "open the job of the page oracle on Linux and fix its test; when it is skipped, fix core-on-index or linux-deps-e2e first",
    "e2e-page-windows": "open the job of the page oracle on Windows and fix its test; when it is skipped, fix core-on-index first",
    "e2e-page-final-touched": "open the job and fix scripts/ci_ab_oracle.py touched: in the merge queue run it must print touched=true|false",
    "e2e-page-final": "open the failing shard of the page oracle on the final seeds (linux) and fix its test; when it is skipped, fix the job that decides it",
    "e2e-page-final-windows": "open the failing shard of the page oracle on the final seeds (windows) and fix its test; when it is skipped, fix the job that decides it",
}
E2E_CONDITIONAL = {
    # The base of the change is the one of the merge queue entry (`github.event.merge_group.base_sha`): the job runs there and nowhere else.
    "e2e-page-final-touched": ("merge_group",),
}
# A job that runs only when another job says so: job -> (the job that decides, its output, the value that makes it run). Otherwise it is `skipped`.
E2E_BY_OUTPUT = {
    "e2e-page-final": ("e2e-page-final-touched", "touched", "true"),
    "e2e-page-final-windows": ("e2e-page-final-touched", "touched", "true"),
}
# What `workflow_dispatch` with `only: windows-control-a` skips on purpose: every job but the two that run it.
E2E_NARROWED = tuple(sorted(set(E2E_FIX) - {"core-on-index", "e2e-windows-control-a"}))

# fakeweb.yml: every job runs on every event the workflow listens to.
FAKEWEB_FIX = {
    "core-on-index": _CORE,
    "linux-deps-fakeweb": "open the job linux-deps-fakeweb and fix the package list; when it is skipped, fix core-on-index first",
    "walk": "open the failing leg (`fakeweb p2` or `p4`, on linux or windows) and fix what differs from stock; when it is skipped, fix core-on-index first",
}

# WORKFLOW -> (the name of the verdict job, its FIX table, its CONDITIONAL table).
TABLES = {
    "tests": ("gate", FIX, CONDITIONAL),
    "e2e": ("e2e verdict", E2E_FIX, E2E_CONDITIONAL),
    "fakeweb": ("fakeweb verdict", FAKEWEB_FIX, {}),
}


def expected_result(name: str, needs: dict, event: str, workflow: str = "tests", only: str = "") -> str:
    """`success`, or `skipped` for a job that is conditional by design on this event (or on this input, or on the output of another job)."""
    conditional = TABLES[workflow][2]
    if workflow == "e2e":
        if only == "windows-control-a" and name in E2E_NARROWED:
            return "skipped"
        if name in E2E_BY_OUTPUT:
            decider, output, value = E2E_BY_OUTPUT[name]
            entry = needs.get(decider)
            outputs = entry.get("outputs") if isinstance(entry, dict) else None
            ran = isinstance(outputs, dict) and outputs.get(output) == value
            return "success" if ran else "skipped"
    if name in conditional and event not in conditional[name]:
        return "skipped"
    return "success"


def verdict(needs: dict, event: str, workflow: str = "tests", only: str = "") -> list[str]:
    """The FAIL lines for the results of `needs` (the object `toJSON(needs)` gives) of `workflow` on `event`; empty when every job is as expected."""
    label, fix, _conditional = TABLES[workflow]
    out = []
    for name in sorted(set(fix) | set(needs)):
        entry = needs.get(name)
        result = entry.get("result") if isinstance(entry, dict) else None
        if name not in fix:
            out.append(
                "FAIL %s: job %s is %s. Fix: add it to the table of %s in scripts/ci_gate.py, or take it out of `needs:` of the job %s. Rule: %s"
                % (label, name, result, workflow, label, RULE))
        elif result != expected_result(name, needs, event, workflow, only):
            out.append("FAIL %s: job %s is %s. Fix: %s. Rule: %s" % (label, name, result or "missing", fix[name], RULE))
    return out


def main() -> int:
    workflow = os.environ.get("WORKFLOW", "") or "tests"
    if workflow not in TABLES:
        print("FAIL gate: WORKFLOW is %r, not one of %s. Fix: pass WORKFLOW=tests, e2e or fakeweb. Rule: %s" % (workflow, "|".join(sorted(TABLES)), RULE))
        return 1
    label = TABLES[workflow][0]
    try:
        needs = json.loads(os.environ.get("NEEDS", ""))
        if not isinstance(needs, dict):
            raise ValueError("not an object")
    except ValueError as exc:
        print("FAIL %s: the results of the needed jobs cannot be read (%s). Fix: pass NEEDS=${{ toJSON(needs) }}. Rule: %s" % (label, exc, RULE))
        return 1
    event = os.environ.get("EVENT_NAME", "")
    for name in sorted(needs):
        entry = needs[name]
        print("%-24s %s" % (name + ":", entry.get("result") if isinstance(entry, dict) else entry))
    lines = verdict(needs, event, workflow, os.environ.get("INPUT_ONLY", ""))
    for line in lines:
        print(line)
    if lines:
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
