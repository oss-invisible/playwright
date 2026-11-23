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
import math
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ci_ab_network.py"
sys.path.insert(0, str(ROOT / "scripts"))
import ci_ab_network as oracle  # noqa: E402
import ci_crash_evidence as crash  # noqa: E402
from ci_ab_pr_seeds import pr_seed_count  # noqa: E402

#: The fewest leaves a comparison holds, per system. A minimal but complete run of ONE seed (one request on each road, tests/test_ab_network.py) holds about 670; a real one holds several times that. A floor of
#: 500 says "the run saw something on every road" and nothing more: it is raised to a fraction of the measured count once the A/A has measured it.
FLOOR = {"linux": 500, "win32": 500}

#: The hang guard of the test, sized from what it will run: the slowest launches MEASURED, plus a declared margin of 25%. It is a guard against a launch that hangs, never a
#: verdict and never a threshold of the product (the pull request's default of 420 s killed the xdist worker of a 5-seed A/A: run 44963364060).
#: Measured per engine run, 5 seeds: run 45121964904 (the gate of the series, Windows, the slowest runner seen: main launches of 139-170 s, idle c1 10 s) and run 45153612447
#: (pull request #578, every launch with a page and the wait for Glean: main launches of 123-135 s on Windows and 110-118 s on Linux, idle c2 4-5 s, exit pings 6 s, and 15 s
#: of the test before the first launch and after the last). An engine run is the idle launches of the first seed and one main launch per seed: 650-659 s on Windows and 590 s
#: on Linux in 45153612447, 925-941 s in 45121964904.
MEASURED_SETUP_S = 15
#: idle c1 (10 s, 45121964904) + idle c2 (5 s, 45153612447) + the exit pings (6 s, 45153612447)
MEASURED_IDLE_S = 21
#: the slowest main launch (45121964904, Windows)
MEASURED_MAIN_S = 170
MARGIN = 1.25


def budget_s(seeds: int, engine_runs: int) -> int:
    """Seconds the test may take for ``seeds`` seeds in ``engine_runs`` runs of an engine (the A/A is two, with a candidate three): the measured time of that run plus ``MARGIN``."""
    return math.ceil(MARGIN * (MEASURED_SETUP_S + engine_runs * (MEASURED_IDLE_S + seeds * MEASURED_MAIN_S)))


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
    # Output in files and the whole tree killed at the limit: `subprocess.run(capture_output=True, timeout=...)` killed the python child alone, the firefox grandchild held the pipe
    # and pytest ended with its own timeout 120 s later and no word about the launch (run 45121964904, Windows, job.log 546-557).
    done = crash.run_tree(command, out / "tree", TIMEOUT_S)
    (out / "oracle.log").write_text(done.stdout + "\n--- stderr\n" + done.stderr, encoding="utf-8")
    assert not done.timed_out, done.timeout_message("the network oracle", TIMEOUT_S) + ":\n" + "\n".join(done.stdout.splitlines()[-30:]) + "\n" + done.stderr[-1500:]
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
