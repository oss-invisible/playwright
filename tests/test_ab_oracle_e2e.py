"""Same seed, same bytes: the published firefox-68 engine against itself (the control) and, with a candidate, against the candidate, on this machine.

scripts/ci_ab_oracle.py runs the host gate's dump (the page of scripts/host_gate, every API a page reads) over the persona seeds of ci/persona-seeds.json, each at its own
DPR. Like the network and host oracles, it has two modes, told by whether there is a candidate:

* on a pull request (no candidate: INVPW_BINARY_PATH is the published engine and INVISIBLE_SEAL_FILE is not set): the A/A, the published engine dumped twice and the two compared leaf by leaf, on
  the first two seeds of the `candidate` set (the run has a budget per test; the gate's leg runs them all). A difference is a BROKEN CONTROL (exit 2): what is compared is not stable. Both dumps and
  the diff are saved under $AB_ORACLE_OUT or, on a runner, $RUNNER_TEMP/invpw-evidence/ab-oracle (which the e2e jobs upload);
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
TIMEOUT_S = 40 * 60
#: The seeds of the A/A on a pull request: the first two of the candidate set (4 launches of the page; the gate's leg runs the A/A on all of them).
PR_SEEDS = 2


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
    seed_set = os.environ.get("AB_ORACLE_SEED_SET") or "candidate"
    shard = os.environ.get("AB_ORACLE_SHARD") or None
    out = _out(tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    leg = "win-x86_64" if sys.platform == "win32" else "linux-x86_64"
    command = [sys.executable, "-u", str(SCRIPT), "run", "--leg", leg, "--out", str(out), "--seed-set", seed_set]
    if candidate:
        command[4:4] = [binary, "--candidate-seal", seal]
    else:
        command += ["--published-binary", binary, "--limit", str(PR_SEEDS)]
    if shard:
        command += ["--shard", shard]
    done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_S)
    (out / "oracle.log").write_text(done.stdout + "\n--- stderr\n" + done.stderr, encoding="utf-8")
    verdict = [line for line in done.stdout.splitlines() if line.startswith(("DIFF ", "FAIL ", "AB ORACLE", "BROKEN CONTROL"))]
    assert done.returncode != 2, "BROKEN CONTROL: the published engine differs from itself, what is compared is not stable:\n" + "\n".join(verdict[:60]) + "\n" + done.stderr[-1500:]
    assert done.returncode == 0, "the oracle is red:\n" + "\n".join(verdict[:60]) + "\n" + done.stderr[-1500:]
    control = json.loads((out / "diff-a-a.json").read_text(encoding="utf-8"))
    assert control["verdict"] == "green" and control["differences"] == [] and control["problems"] == [] and len(control["compared"]) == len(control["visits"]) >= 1
    names = sorted(p.name for p in out.glob("*.json"))
    if candidate:
        diff = json.loads((out / "diff.json").read_text(encoding="utf-8"))
        assert diff["verdict"] == "green" and diff["differences"] == [] and diff["problems"] == []
        assert len(diff["visits"]) >= 1 and len(diff["compared"]) == len(diff["visits"]), "every visit asked for is compared: %s" % diff["visits"]
        assert names == ["diff-a-a.json", "diff.json", "dump-68-again.json", "dump-68.json", "dump-candidate.json"]
    else:
        assert names == ["diff-a-a.json", "dump-68-again.json", "dump-68.json"]
