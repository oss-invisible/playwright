"""The network A/B oracle on a runner: the same journey on the local lab with the published firefox-68 engine twice (the A/A, the control), and, with a candidate, against the candidate.

scripts/ci_ab_network.py walks the P4 journey (17 steps), one HTTPS site (HTTP/1.1 and HTTP/2), one HTTP/3 site, one WebRTC page and two idle launches through a SOCKS5 gateway in coherent mode, with the fake DoH,
a witness and the watcher of direct connections, and writes what the lab saw as a transcript: the hosts contacted in order, the DNS questions, the TLS ClientHello, the HTTP/2 frames, every request head in
wire order, the QUIC parameters, the WebRTC candidates and the UDP path. Two transcripts are compared leaf by leaf; a leaf that differs is red with its path and both values. The leaves that differ between two
runs of ONE engine are the noise (ci/ab-network-volatile.json), and they are born from the A/A: this test is where it runs.

* On a pull request (no candidate: ``INVPW_BINARY_PATH`` is the published engine and no ``INVISIBLE_SEAL_FILE``): the A/A, on the first seed. It saves both transcripts, the diff and a proposal of the volatile
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

#: The fewest leaves a comparison holds, per system. A minimal but complete run of ONE seed (one request on each road, tests/test_ab_network.py) holds about 670; a real one holds several times that. A floor of
#: 500 says "the run saw something on every road" and nothing more: it is raised to a fraction of the measured count once the A/A has measured it.
FLOOR = {"linux": 500, "win32": 500}
TIMEOUT_S = 40 * 60


def _out(tmp_path: Path) -> Path:
    explicit = os.environ.get("AB_ORACLE_OUT")
    if explicit:
        return Path(explicit) / "network"
    runner = os.environ.get("RUNNER_TEMP")
    if runner:
        return Path(runner) / "invpw-evidence" / "ab-network"
    return tmp_path / "ab-network"


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
        command += ["--a-a", "--published-binary", firefox_binary, "--seeds", str(oracle.default_seeds()[0])]
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
