"""The verdict of the `gate` job of tests.yml: every job it waits for must have ended in `success`.

    NEEDS='<toJSON(needs)>' EVENT_NAME=<github.event_name> python scripts/ci_gate.py

`gate` is the one context main requires. Rule ci-13: nothing counts as passed that did not pass, so `skipped`, `cancelled` and `failure` are all
red. A job that was skipped ran nothing: when `unit` was skipped for a core that was not on the index, the old gate said "ok".

The only job that may end in something else than `success` is one that is conditional BY DESIGN and is named in CONDITIONAL with the events on
which it runs: on any other event it is expected to be `skipped`, and on those events it must be `success`. Nothing else is forgiven, and a job
the workflow waits for that this table does not know is red as well (the table and the `needs:` of the job `gate` cannot drift apart without
tests/test_the_gate_is_red_when_a_needed_job_is_skipped.py saying so).

Prints one line per job that is not as expected, `FAIL gate: job <name> is <result>. Fix: <how>. Rule: ci-13 ...`, and exits 1; prints `ok` and
exits 0 when every job is.
"""
from __future__ import annotations

import json
import os
import sys

RULE = "ci-13 nothing counts as passed that did not pass"

# The jobs `gate` waits for, each with what to do when it is not green.
FIX = {
    "core-on-index": "wait for the publish of the pinned invisible-core to finish, or pin a published core, then re-run",
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
    # `version` has `if: github.event_name == 'pull_request'`: on main straight after a release the version IS the published one until somebody
    # bumps, and a check that is red at rest is one people learn to skip. So a push to main skips it on purpose.
    "version": ("pull_request",),
}


def verdict(needs: dict, event: str) -> list[str]:
    """The FAIL lines for the results of `needs` (the object `toJSON(needs)` gives) on `event`; empty when every job is as expected."""
    out = []
    for name in sorted(set(FIX) | set(needs)):
        entry = needs.get(name)
        result = entry.get("result") if isinstance(entry, dict) else None
        expected = "success"
        if name in CONDITIONAL and event not in CONDITIONAL[name]:
            expected = "skipped"
        if name not in FIX:
            out.append(
                "FAIL gate: job %s is %s. Fix: add it to FIX in scripts/ci_gate.py, or take it out of `needs:` of the job gate. Rule: %s"
                % (name, result, RULE))
        elif result != expected:
            out.append("FAIL gate: job %s is %s. Fix: %s. Rule: %s" % (name, result or "missing", FIX[name], RULE))
    return out


def main() -> int:
    try:
        needs = json.loads(os.environ.get("NEEDS", ""))
        if not isinstance(needs, dict):
            raise ValueError("not an object")
    except ValueError as exc:
        print("FAIL gate: the results of the needed jobs cannot be read (%s). Fix: pass NEEDS=${{ toJSON(needs) }}. Rule: %s" % (exc, RULE))
        return 1
    event = os.environ.get("EVENT_NAME", "")
    for name in sorted(needs):
        entry = needs[name]
        print("%-18s %s" % (name + ":", entry.get("result") if isinstance(entry, dict) else entry))
    lines = verdict(needs, event)
    for line in lines:
        print(line)
    if lines:
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
