"""Same seed, same bytes: the published firefox-68 engine against itself (the control) and, with a candidate, against the candidate, on this machine.

scripts/ci_ab_oracle.py runs the host gate's dump (the page of scripts/host_gate, every API a page reads) over the persona seeds of ci/persona-seeds.json, each at its own
DPR. Like the network and host oracles, it has two modes, told by whether there is a candidate:

* on a pull request (no candidate: INVPW_BINARY_PATH is the published engine and INVISIBLE_SEAL_FILE is not set): the A/A, the published engine dumped twice and the two compared leaf by leaf, on
  the first two seeds of the `candidate` set (the run has a budget per test; the gate's leg runs them all), or on ALL of them when the pull request changes a file of the oracle: the step of e2e.yml
  runs scripts/ci_ab_pr_seeds.py and exports AB_ORACLE_PR_SEEDS (5), which `pr_seed_count` reads; the A/A is zero on every seed before any verdict of an A/B is accepted. A difference is a BROKEN CONTROL
  (exit 2): what is compared is not stable. Both dumps and the diff are saved under $AB_ORACLE_OUT or, on a runner, $RUNNER_TEMP/invpw-evidence/ab-oracle (which the e2e jobs upload);
* with a candidate (INVPW_BINARY_PATH the candidate's binary, INVISIBLE_SEAL_FILE its seal, as the gate's ab_oracle leg sets them): the A/A first, on every seed of the set, then the published engine
  (fetched the way the core fetches an engine: the release's seal.json and the archive checked against it) against the candidate. If the A/A is broken the A/B is not run. AB_ORACLE_SEED_SET=final and
  AB_ORACLE_SHARD=i/n run the 20 seeds that reach every value of every dimension, split over n jobs.

The only differences either comparison lets through are the `noise` entries of scripts/host_gate/exceptions.json and the diag. keys. The verdict is the pytest result; the files (dump-68.json,
dump-68-again.json, diff-a-a.json and, with a candidate, dump-candidate.json and diff.json) and the log are the evidence.

Not skipped on a machine without a candidate: a skipped test is a red one in the judge, and an oracle that did not run says nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ci_ab_oracle.py"
sys.path.insert(0, str(ROOT / "scripts"))
from ci_ab_pr_seeds import pr_seed_count  # noqa: E402
import ci_ab_oracle as ab  # noqa: E402

#: The seeds of the A/A on a pull request: the first two of the candidate set (4 launches of the page; the gate's leg runs the A/A on all of them), or every one
#: when the pull request touches the oracle (AB_ORACLE_PR_SEEDS, from the step of e2e.yml). The final set's A/A (AB_ORACLE_SEED_SET=final) visits every seed of its shard.
PR_SEEDS = 2

#: The hang guard of the test, sized from what it will run (the seeds of its shard, the engine runs, the second launch of seed 0). It is a safety net and never a verdict: a
#: shard of the final set is 5 seeds, the A/A of a pull request is 2, the gate's leg with a candidate is 5 in three engine runs, and one fixed number cannot be right for all of
#: them. These are estimates, not measurements: 120 s to start, then for each engine run 180 s for its first seed (a cold launch, which on Windows is the slowest), 100 s for each
#: next one and 200 s for the two extra launches of seed 0 on one profile; the first A/A of each kind says what they should be and they move with it.
SETUP_S, FIRST_SEED_S, NEXT_SEED_S, RELAUNCH_S = 120, 180, 100, 200


def budget_s(seeds: int, engine_runs: int, relaunch: bool = False) -> int:
    """Seconds the test may take for ``seeds`` seeds in ``engine_runs`` runs of an engine (the A/A is two, with a candidate three), with the second launch of seed 0 when ``relaunch``."""
    return SETUP_S + engine_runs * (FIRST_SEED_S + (seeds - 1) * NEXT_SEED_S + (RELAUNCH_S if relaunch else 0))


def asked(environ=None) -> tuple:
    """(seed set, shard, candidate, visits) this run of the test makes, from the environment as the test reads it. The A/A of a pull request visits the first
    PR_SEEDS seeds of the candidate set (or every one when the pull request touches the oracle); the final set's A/A, and any run with a candidate, visit every
    seed of their shard: that is what they are for."""
    env = os.environ if environ is None else environ
    candidate = bool(env.get("INVISIBLE_SEAL_FILE"))
    seed_set = env.get("AB_ORACLE_SEED_SET") or "candidate"
    shard = env.get("AB_ORACLE_SHARD") or None
    limit = None if candidate or seed_set == "final" else pr_seed_count("AB_ORACLE_PR_SEEDS", PR_SEEDS, env)
    return seed_set, shard, candidate, ab.select_visits(seed_set, shard, limit=limit)


def plan(environ=None) -> tuple:
    """(seeds, engine runs, whether seed 0 is launched twice on a profile) of this run."""
    _seed_set, _shard, candidate, visits = asked(environ)
    return len(visits), 3 if candidate else 2, bool(ab.relaunch_visits(visits))


BUDGET_S = budget_s(*plan())
#: The subprocess ends by itself at BUDGET_S and says so; the guard of the worker comes after it.
TIMEOUT_S = BUDGET_S


def _out(tmp_path: Path) -> Path:
    explicit = os.environ.get("AB_ORACLE_OUT")
    if explicit:
        return Path(explicit)
    runner = os.environ.get("RUNNER_TEMP")
    if runner:
        return Path(runner) / "invpw-evidence" / "ab-oracle"
    return tmp_path / "ab-oracle"


@pytest.mark.timeout(TIMEOUT_S + 120, method="thread")
def test_the_published_engine_repeats_itself_and_the_candidate_gives_the_same_bytes(tmp_path):
    binary = os.environ.get("INVPW_BINARY_PATH")
    seal = os.environ.get("INVISIBLE_SEAL_FILE")
    assert binary and Path(binary).is_file(), "INVPW_BINARY_PATH must name the engine's binary, got %r" % binary
    candidate = bool(seal)
    if candidate:
        assert Path(seal).is_file(), "INVISIBLE_SEAL_FILE names no file: %r" % seal
    seed_set, shard, _candidate, visits = asked()
    out = _out(tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    leg = "win-x86_64" if sys.platform == "win32" else "linux-x86_64"
    command = [sys.executable, "-u", str(SCRIPT), "run", "--leg", leg, "--out", str(out), "--seed-set", seed_set]
    if candidate:
        command[4:4] = [binary, "--candidate-seal", seal]
    else:
        command += ["--published-binary", binary]
        if seed_set != "final":
            command += ["--limit", str(len(visits))]
    if shard:
        command += ["--shard", shard]
    done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_S)
    (out / "oracle.log").write_text(done.stdout + "\n--- stderr\n" + done.stderr, encoding="utf-8")
    verdict = [line for line in done.stdout.splitlines() if line.startswith(("DIFF ", "FAIL ", "AB ORACLE", "BROKEN CONTROL"))]
    assert done.returncode != 2, "BROKEN CONTROL: the published engine differs from itself, what is compared is not stable:\n" + "\n".join(verdict[:60]) + "\n" + done.stderr[-1500:]
    assert done.returncode == 0, "the oracle is red:\n" + "\n".join(verdict[:60]) + "\n" + done.stderr[-1500:]
    control = json.loads((out / "diff-a-a.json").read_text(encoding="utf-8"))
    assert control["verdict"] == "green" and control["differences"] == [] and control["problems"] == [] and len(control["compared"]) == len(control["visits"]) >= 1
    assert len(control["visits"]) == len(visits), "every seed of the shard is visited: %s" % control["visits"]
    assert sorted(control["relaunch"]) == sorted(ab.visit_name(*key) for key in ab.relaunch_visits(visits)), "seed 0 is launched twice on one profile, whichever shard has it"
    names = sorted(p.name for p in out.glob("*.json"))
    if candidate:
        diff = json.loads((out / "diff.json").read_text(encoding="utf-8"))
        assert diff["verdict"] == "green" and diff["differences"] == [] and diff["problems"] == []
        assert len(diff["visits"]) >= 1 and len(diff["compared"]) == len(diff["visits"]), "every visit asked for is compared: %s" % diff["visits"]
        assert names == ["diff-a-a.json", "diff.json", "dump-68-again.json", "dump-68.json", "dump-candidate.json"]
    else:
        assert names == ["diff-a-a.json", "dump-68-again.json", "dump-68.json"]
