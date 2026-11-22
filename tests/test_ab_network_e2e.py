"""The network A/B oracle on a runner: the same journey on the local lab with the published firefox-68 engine twice (the A/A, the control), and, with a candidate, against the candidate.

scripts/ci_ab_network.py walks the P4 journey (17 steps), one HTTPS site (HTTP/1.1 and HTTP/2), one HTTP/3 site, one WebRTC page and two idle launches through a SOCKS5 gateway in coherent mode, with the fake DoH,
a witness and the watcher of direct connections, and writes what the lab saw as a transcript: the hosts contacted in order, the DNS questions, the TLS ClientHello, the HTTP/2 frames, every request head in
wire order, the QUIC parameters, the WebRTC candidates and the UDP path. Two transcripts are compared leaf by leaf; a leaf that differs is red with its path and both values. The leaves that differ between two
runs of ONE engine are the noise (ci/ab-network-volatile.json), and they are born from the A/A: this test is where it runs.

* On a pull request (no candidate: ``INVPW_BINARY_PATH`` is the published engine and no ``INVISIBLE_SEAL_FILE``): the A/A, on the first seed, or on every seed of the candidate set when the pull request
  changes a file of this oracle (``AB_NETWORK_PR_SEEDS``, exported by the step of e2e.yml that runs scripts/ci_ab_pr_seeds.py). It saves both transcripts, the diff and a proposal of the volatile
  entries under ``$AB_ORACLE_OUT/network`` or, on a runner, ``$RUNNER_TEMP/invpw-evidence/ab-network`` (which the e2e jobs upload), on Linux and on Windows. A difference is a BROKEN CONTROL and red,
  until the list holds it: the first run is red on purpose, and its evidence (uploaded whatever the verdict) is what the list is filled from, with the run id.
* With a candidate (``INVPW_BINARY_PATH`` and ``INVISIBLE_SEAL_FILE`` as the gate's ab_oracle leg sets them): the A/A first, then the published engine against the candidate on every seed of
  ci/persona-seeds.json (key ``candidate``) or, without the file, the seeds of the A/B oracle of the page. The A/A is the control: if it is broken the A/B is not run.

Not skipped on a machine without the engine's own network: a skipped test is a red one in the judge, and an oracle that did not run says nothing. Nothing here reads a wall clock: the lab drives the journey by
events, and the idle windows end at an event (see IDLE in scripts/ci_ab_network.py).
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
SCRIPT = ROOT / "scripts" / "ci_ab_network.py"
sys.path.insert(0, str(ROOT / "scripts"))
import ci_ab_network as oracle  # noqa: E402
from ci_ab_pr_seeds import pr_seed_count  # noqa: E402

#: The fewest leaves a comparison holds, per system. A minimal but complete run of ONE seed (one request on each road, tests/test_ab_network.py) holds about 670; a real one holds several times that. A floor of
#: 500 says "the run saw something on every road" and nothing more: it is raised to a fraction of the measured count once the A/A has measured it.
FLOOR = {"linux": 500, "win32": 500}

#: The hang guard of the test, sized from what it will run. It is a safety net and never a verdict: the pull request's default timeout of 420 s (`--timeout 420 --timeout-method thread`
#: of run_e2e.py) killed the xdist worker of the A/A on 5 seeds, on Linux and on Windows (run 44963364060), because the run takes minutes per seed.
#: Measured (runs 44946411675 and 44960767715, A/A of one seed, two engine runs): 4:00 on Linux and 4:50 on Windows, so 120 s and 145 s per engine run for the first seed, which also
#: has the two idle launches. The budget allows 240 s for the first seed of an engine run, 150 s for each next one (as slow as a whole first seed on Windows), and 120 s to start: 5 seeds
#: in an A/A are 30 minutes at most, expected about 15 on Linux and 19 on Windows; with a candidate (three engine runs on every seed) 44 minutes at most.
SETUP_S, FIRST_SEED_S, NEXT_SEED_S = 120, 240, 150


def budget_s(seeds: int, engine_runs: int) -> int:
    """Seconds the test may take for ``seeds`` seeds in ``engine_runs`` runs of an engine (the A/A is two, with a candidate three)."""
    return SETUP_S + engine_runs * (FIRST_SEED_S + (seeds - 1) * NEXT_SEED_S)


def plan() -> tuple:
    """(seeds, engine runs) this run of the test will make, from the environment as the test reads it."""
    seal = os.environ.get("INVISIBLE_SEAL_FILE")
    if seal and Path(seal).is_file():
        return len(oracle.default_seeds()), 3
    return min(len(oracle.default_seeds()), pr_seed_count("AB_NETWORK_PR_SEEDS", 1)), 2


BUDGET_S = budget_s(*plan())
#: The subprocess ends by itself at BUDGET_S and says so; the guard of the worker comes after it.
TIMEOUT_S = BUDGET_S


def _out(tmp_path: Path) -> Path:
    explicit = os.environ.get("AB_ORACLE_OUT")
    if explicit:
        return Path(explicit) / "network"
    runner = os.environ.get("RUNNER_TEMP")
    if runner:
        return Path(runner) / "invpw-evidence" / "ab-network"
    return tmp_path / "ab-network"


@pytest.mark.timeout(BUDGET_S + 120, method="thread")
def test_the_network_a_a_is_the_control_and_the_candidate_puts_on_the_wire_what_the_published_engine_puts(firefox_binary, tmp_path):
    seal = os.environ.get("INVISIBLE_SEAL_FILE")
    candidate = bool(seal and Path(seal).is_file())
    out = _out(tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    floor = FLOOR["win32" if sys.platform == "win32" else "linux"]
    command = [sys.executable, "-u", str(SCRIPT), "ab", "--out", str(out), "--floor", str(floor)]
    if candidate:
        command += ["--candidate", firefox_binary, "--candidate-seal", seal]
    else:
        command += ["--a-a", "--published-binary", firefox_binary, "--seeds", ",".join(str(s) for s in oracle.default_seeds()[:plan()[0]])]
    done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_S)
    (out / "oracle.log").write_text(done.stdout + "\n--- stderr\n" + done.stderr, encoding="utf-8")
    verdict = [line for line in done.stdout.splitlines() if line.startswith(("DIFF ", "FAIL ", "AB NETWORK", "ERROR "))]
    control = out / "diff-a-a.json"
    assert control.is_file(), "the A/A did not run to its end (exit %s):\n%s\n%s" % (done.returncode, "\n".join(verdict[:40]), done.stderr[-1500:])
    aa = json.loads(control.read_text(encoding="utf-8"))
    assert done.returncode != 2 or aa["verdict"] == "green", "BROKEN CONTROL: the published engine differs from itself on %d leaves (the list is ci/ab-network-volatile.json):\n%s" % (
        len(aa["differences"]), "\n".join(verdict[:60]))
    assert done.returncode == 0, "the network oracle is red:\n" + "\n".join(verdict[:60]) + "\n" + done.stderr[-1500:]
    assert aa["verdict"] == "green" and aa["differences"] == [] and aa["leaves"] >= floor
    if candidate:
        diff = json.loads((out / "diff.json").read_text(encoding="utf-8"))
        assert diff["verdict"] == "green" and diff["differences"] == [] and diff["direct"] == [] and diff["leaves"] >= floor
