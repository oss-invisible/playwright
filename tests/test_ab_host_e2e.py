"""What the engine leaves on the host after one short journey: the process tree and the modules per kind, the published engine against itself and, with a
candidate, against the candidate (e2e, Linux and Windows).

scripts/ci_ab_host.py walks the first steps of the fake web's journey (no internet) with the stock Playwright client, waits for the tree to be quiet, and
before closing the browser takes: the multiset of process kinds (the parent, or the last argument of `-contentproc` with `-sandboxingKind` and
`-isForBrowser` / `-notForBrowser`), the modules each kind has mapped (normalized), and the children that were born and gone before the snapshot (Linux:
strace; Windows: the job object's TotalProcesses and a WMI trace). Three seeds, one browser at a time.

Two modes, chosen by the environment as the gate chooses it:

  * no candidate (the PR's e2e: INVPW_BINARY_PATH is the published engine, INVISIBLE_SEAL_FILE is not set): the A/A control, the engine twice. This is the
    stability measurement: what the footprint holds that two launches of the SAME engine do not agree on is a difference in diff-a-a.json, and red here
    says "what is compared is not stable".
  * a candidate (the gate's ab_oracle leg: INVPW_BINARY_PATH the candidate's binary, INVISIBLE_SEAL_FILE its seal): the A/A on the published firefox-68
    (its seal and archive fetched the way the core fetches an engine), then the published engine against the candidate, with the declared differences of
    ci/ab-host-expected.json.

What two launches of the SAME engine disagree on is listed in ci/ab-host-volatile.json (with the run that proved it, per system); it applies in the A/A and in the A/B, and an
entry that matches no leaf is red as stale. The launches run with a cache folder of their own (XDG_CACHE_HOME on Linux), so they leave nothing for the fontconfig test's cold launch.

The footprints, the diffs and the log are kept whatever the verdict: in AB_ORACLE_OUT/ab-host (the gate uploads that folder), else in
$RUNNER_TEMP/invpw-evidence/ab-host (the e2e job uploads that one), else in tmp_path.

Not skipped on a machine without a candidate: a skipped test is a red one in the judge, and an oracle that did not run says nothing. On Linux the
children are seen by strace (installed by e2e.yml): without it, or where it cannot trace, the test fails and says so.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ci_ab_host.py"
TIMEOUT_S = 25 * 60


def _evidence_dir(tmp_path: Path) -> Path:
    base = os.environ.get("AB_ORACLE_OUT")
    if base:
        return Path(base) / "ab-host"
    runner = os.environ.get("RUNNER_TEMP")
    if runner:
        return Path(runner) / "invpw-evidence" / "ab-host"
    return tmp_path / "ab-host"


@pytest.mark.timeout(TIMEOUT_S + 120, method="thread")
def test_the_engine_leaves_the_same_footprint_on_the_host_after_the_same_journey(tmp_path):
    binary = os.environ.get("INVPW_BINARY_PATH")
    seal = os.environ.get("INVISIBLE_SEAL_FILE")
    assert binary and Path(binary).is_file(), "INVPW_BINARY_PATH must name the engine's binary, got %r" % binary
    candidate = bool(seal)
    if candidate:
        assert Path(seal).is_file(), "INVISIBLE_SEAL_FILE names no file: %r" % seal
    if sys.platform.startswith("linux"):
        from _strace_stand_in import _require_strace_can_trace

        if shutil.which("strace") is None:
            pytest.fail("needs strace (apt install strace): without it the short-lived children are not seen", pytrace=False)
        _require_strace_can_trace(tmp_path)
    out = _evidence_dir(tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    leg = "win-x86_64" if sys.platform == "win32" else "linux-x86_64"
    if candidate:
        argv = [binary, "--candidate-seal", seal]
    else:
        argv = ["--published-binary", binary]
    done = subprocess.run([sys.executable, "-u", str(SCRIPT), "ab", *argv, "--leg", leg, "--out", str(out)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_S)
    (out / "ab-host.log").write_text(done.stdout + "\n--- stderr\n" + done.stderr, encoding="utf-8")
    verdict = [line for line in done.stdout.splitlines()
               if line.startswith(("DIFF ", "FAIL ", "DECLARED ", "VOLATILE ", "AB HOST", "BROKEN CONTROL", "seed ", "---"))]
    control = json.loads((out / "diff-a-a.json").read_text(encoding="utf-8")) if (out / "diff-a-a.json").exists() else {}
    assert done.returncode != 2, "the control is broken: the engine differs from itself, what is compared is not stable (what may differ is listed in ci/ab-host-volatile.json)\n" + "\n".join(verdict[:80])
    assert done.returncode == 0, "the host footprint differs:\n" + "\n".join(verdict[:80]) + "\n" + done.stderr[-1500:]
    assert control.get("verdict") == "green" and control.get("differences") == [] and control.get("problems") == []
    names = sorted(p.name for p in out.glob("footprint-*.json"))
    assert names == (["footprint-68-again.json", "footprint-68.json", "footprint-candidate.json"] if candidate
                     else ["footprint-68-again.json", "footprint-68.json"])
    if candidate:
        diff = json.loads((out / "diff.json").read_text(encoding="utf-8"))
        assert diff["verdict"] == "green" and diff["differences"] == [] and diff["problems"] == []
