#!/usr/bin/env python3
"""How many persona seeds the A/A of each oracle visits on a pull request.

The page, network and host oracles run their A/A (the published engine against itself) on a pull request, where a test has a time budget: a few seeds. The A/A is
the control that every A/B verdict rests on, so when the pull request changes the code or the data of an oracle, its A/A runs on every seed of the `candidate` set
of ci/persona-seeds.json (the seeds the gate's leg runs), not on the first few: a change that makes the control unstable on one seed is found on the pull request.

`e2e.yml` runs this once per job, on a pull request, with the base commit of the pull request:

    python scripts/ci_ab_pr_seeds.py "$BASE_SHA" >> "$GITHUB_ENV"

It fetches that commit (the checkout is shallow), lists the files the pull request changed (`git diff --name-only <base> HEAD`: HEAD is the merge commit the
runner checks out, so this is exactly what the pull request changes), and prints one `VARIABLE=N` line per oracle: N is every seed of the set when a file of the
oracle (ORACLES below, with what it shares with the others) is in the list, else the oracle's default. The tests read the variable with `pr_seed_count`; without it
(a push to main, a manual run) they use the default.
"""
from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: What every oracle depends on: the page oracle's driver (the others use its fetch of the published engine and its cache folder), this file and the seeds.
SHARED = ("scripts/ci_ab_oracle.py", "scripts/ci_ab_pr_seeds.py", "scripts/persona_cover.py", "ci/persona-seeds.json")
#: variable -> (oracle, seeds by default on a pull request that does not touch it, the files of the oracle: exact paths or fnmatch patterns).
ORACLES = {
    "AB_ORACLE_PR_SEEDS": ("page", 2, SHARED + ("scripts/ci_host_gate.py", "scripts/host_gate/*", "tests/test_ab_oracle*")),
    "AB_NETWORK_PR_SEEDS": ("network", 1, SHARED + ("scripts/ci_ab_network.py", "ci/ab-network-volatile.json", "tests/test_ab_network*")),
    "AB_HOST_PR_SEEDS": ("host", 2, SHARED + ("scripts/ci_ab_host.py", "tests/_strace_stand_in.py", "ci/ab-host-expected.json", "tests/test_ab_host*")),
}
FALLBACK_SEEDS = 5


def candidate_seed_count() -> int:
    """How many seeds the `candidate` set of ci/persona-seeds.json has (5 when the file cannot be read)."""
    try:
        seeds = json.loads((ROOT / "ci" / "persona-seeds.json").read_text(encoding="utf-8")).get("candidate")
        return len(seeds) if seeds else FALLBACK_SEEDS
    except (OSError, ValueError, AttributeError):
        return FALLBACK_SEEDS


def touches(changed, patterns) -> list:
    """The changed files that are one of `patterns` (exact paths or fnmatch patterns, `/` separated)."""
    return sorted(p for p in changed if any(fnmatch.fnmatchcase(p, pattern) for pattern in patterns))


def pr_seed_counts(changed, every: int | None = None) -> dict:
    """{variable: how many seeds} for a pull request that changed `changed`: every seed for an oracle whose own files are in it, else its default."""
    every = candidate_seed_count() if every is None else every
    return {variable: (every if touches(changed, files) else default) for variable, (_name, default, files) in ORACLES.items()}


def pr_seed_count(variable: str, default: int, environ=None) -> int:
    """What a test reads: the number in `variable` (set by the step of e2e.yml), else `default`. A value that is not a whole number of at least 1 is an error."""
    raw = (os.environ if environ is None else environ).get(variable, "").strip()
    if not raw:
        return default
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError("%s=%r: the number of seeds is a whole number of at least 1" % (variable, raw))
    return int(raw)


def changed_files(base: str, run=subprocess.run) -> list:
    """The files that differ between the base commit of the pull request and HEAD (the merge commit of the checkout)."""
    run(["git", "fetch", "--no-tags", "--depth=1", "origin", base], check=True, capture_output=True, text=True)
    done = run(["git", "diff", "--name-only", base, "HEAD"], check=True, capture_output=True, text=True)
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: ci_ab_pr_seeds.py BASE_SHA", file=sys.stderr)
        return 2
    sys.stdout.reconfigure(newline="\n")  # the lines go to $GITHUB_ENV: a carriage return would be part of the value
    changed = changed_files(args[0])
    counts = pr_seed_counts(changed)
    for variable, (name, default, files) in ORACLES.items():
        hit = touches(changed, files)
        print("%s oracle: %d seeds (%s)" % (name, counts[variable], ("its files changed: " + ", ".join(hit[:6])) if hit else "default %d" % default),
              file=sys.stderr)
        print("%s=%d" % (variable, counts[variable]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
