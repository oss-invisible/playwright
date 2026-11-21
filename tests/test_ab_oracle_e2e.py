"""Same seed, same bytes: the published firefox-68 engine against the candidate, on this machine.

scripts/ci_ab_oracle.py runs the host gate's dump (the page of scripts/host_gate, every API a page reads) over the persona seeds of ci/persona-seeds.json, each at its own
DPR, with the engine published as
firefox-68 (its seal and archive fetched the way the core fetches an engine) and again with the candidate binary of the gate (INVPW_BINARY_PATH, under
INVISIBLE_SEAL_FILE), with the same core and the same wrapper, and compares every leaf of every visit. The only differences it lets through are the `noise`
entries of scripts/host_gate/exceptions.json and the diag. keys. The seeds are the `candidate` set (5 seeds that reach every webgl renderer and every DPR) unless the
job says otherwise: AB_ORACLE_SEED_SET=final and AB_ORACLE_SHARD=i/n run the 20 seeds that reach every value of every dimension, split over n jobs. This test is
the gate's leg of that oracle: the verdict is the pytest result, the three files
(dump-68.json, dump-candidate.json, diff.json) and the log are kept in AB_ORACLE_OUT (the gate uploads them), or in tmp_path when it is not set.

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


def test_the_published_engine_and_the_candidate_give_the_same_bytes(tmp_path):
    binary = os.environ.get("INVPW_BINARY_PATH")
    seal = os.environ.get("INVISIBLE_SEAL_FILE")
    assert binary and Path(binary).is_file(), "INVPW_BINARY_PATH must name the candidate's binary, got %r" % binary
    assert seal and Path(seal).is_file(), "INVISIBLE_SEAL_FILE must name the candidate's seal, got %r" % seal
    seed_set = os.environ.get("AB_ORACLE_SEED_SET") or "candidate"
    shard = os.environ.get("AB_ORACLE_SHARD") or None
    out = Path(os.environ.get("AB_ORACLE_OUT") or tmp_path / "ab-oracle")
    out.mkdir(parents=True, exist_ok=True)
    leg = "win-x86_64" if sys.platform == "win32" else "linux-x86_64"
    command = [sys.executable, "-u", str(SCRIPT), "run", binary, "--candidate-seal", seal, "--leg", leg, "--out", str(out), "--seed-set", seed_set]
    if shard:
        command += ["--shard", shard]
    done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_S)
    (out / "oracle.log").write_text(done.stdout + "\n--- stderr\n" + done.stderr, encoding="utf-8")
    verdict = [line for line in done.stdout.splitlines() if line.startswith(("DIFF ", "FAIL ", "AB ORACLE"))]
    assert done.returncode == 0, "the oracle is red:\n" + "\n".join(verdict[:60]) + "\n" + done.stderr[-1500:]
    diff = json.loads((out / "diff.json").read_text(encoding="utf-8"))
    assert diff["verdict"] == "green" and diff["differences"] == [] and diff["problems"] == []
    assert len(diff["visits"]) >= 1 and len(diff["compared"]) == len(diff["visits"]), "every visit asked for is compared: %s" % diff["visits"]
    assert sorted(p.name for p in out.glob("dump-*.json")) == ["dump-68.json", "dump-candidate.json"]
