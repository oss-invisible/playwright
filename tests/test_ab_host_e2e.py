"""What the engine does to the host over a whole closed run: the processes by kind and the set of host paths it touched, the published engine against itself and, with a
candidate, against the candidate (e2e, Linux and Windows).

scripts/ci_ab_host.py walks the first steps of the fake web's journey (no internet) with the stock Playwright client, closes the browser and waits for its tree to exit, and
compares the TRACE of the run from the launch to the exit (not a photograph of one instant): the kinds of process (the parent, `-contentproc` with its type, `-sandboxingKind`
and `-isForBrowser` / `-notForBrowser`, the helpers by what they ran) with their count, but for the kinds of type `tab` whose count follows the idle time (only their presence
counts), and the set of host paths the tree touched, whatever is `ours` (its package, profile and temp dirs, the run's cache, its own shared memory) being left out as a class.
Linux: strace of the whole tree. Windows: the process-creation audit (event 4688, with the command line) and an ETW file trace (Kernel-File), which need an administrator, as the
hosted runners are. One browser at a time.

Two modes, chosen by the environment as the gate chooses it:

  * no candidate (the PR's e2e: INVPW_BINARY_PATH is the published engine, INVISIBLE_SEAL_FILE is not set): the A/A control, the engine twice, on the first two seeds of the
    candidate set, or on all of them when the pull request changes a file of this oracle (AB_HOST_PR_SEEDS, exported by the step of e2e.yml that runs scripts/ci_ab_pr_seeds.py).
    This is the stability measurement: what the two traces do not agree on is a difference in diff-a-a.json, and red here says "what is compared is not stable". Nothing is
    excused: there is no list of noise.
  * a candidate (the gate's ab_oracle leg: INVPW_BINARY_PATH the candidate's binary, INVISIBLE_SEAL_FILE its seal): the A/A on the published firefox-68 (its seal and archive
    fetched the way the core fetches an engine) on every seed of the set, then the published engine against the candidate, with the declared differences of ci/ab-host-expected.json.

The launches run with a cache folder and a temp root of their own, so they leave nothing for the fontconfig test's cold launch and what the engine makes there is `ours`.

The footprints, the diffs and the log are kept whatever the verdict: in AB_ORACLE_OUT/ab-host (the gate uploads that folder), else in
$RUNNER_TEMP/invpw-evidence/ab-host (the e2e job uploads that one), else in tmp_path.

Not skipped on a machine without a candidate: a skipped test is a red one in the judge, and an oracle that did not run says nothing. On Linux the processes and the files are
seen by strace (installed by e2e.yml): without it, or where it cannot trace, the test fails and says so.
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
sys.path.insert(0, str(ROOT / "scripts"))
import ci_ab_host as host  # noqa: E402
from ci_ab_pr_seeds import pr_seed_count  # noqa: E402

TIMEOUT_S = 25 * 60
#: The seeds of the A/A on a pull request: the first two of the candidate set, or every one when the pull request touches this oracle (AB_HOST_PR_SEEDS).
PR_SEEDS = 2


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
        seeds = host.default_seeds()[:pr_seed_count("AB_HOST_PR_SEEDS", PR_SEEDS)]
        argv = ["--published-binary", binary, "--seeds", ",".join(str(x) for x in seeds)]
    done = subprocess.run([sys.executable, "-u", str(SCRIPT), "ab", *argv, "--leg", leg, "--out", str(out)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_S)
    (out / "ab-host.log").write_text(done.stdout + "\n--- stderr\n" + done.stderr, encoding="utf-8")
    verdict = [line for line in done.stdout.splitlines()
               if line.startswith(("DIFF ", "FAIL ", "DECLARED ", "OURS ", "AB HOST", "BROKEN CONTROL", "seed ", "---"))]
    control = json.loads((out / "diff-a-a.json").read_text(encoding="utf-8")) if (out / "diff-a-a.json").exists() else {}
    assert done.returncode != 2, "the control is broken: the engine differs from itself, what is compared is not stable (nothing is excused: a difference between two traces of one engine is a defect of the comparison)\n" + "\n".join(verdict[:80])
    assert done.returncode == 0, "the host footprint differs:\n" + "\n".join(verdict[:80]) + "\n" + done.stderr[-1500:]
    assert control.get("verdict") == "green" and control.get("differences") == [] and control.get("problems") == []
    names = sorted(p.name for p in out.glob("footprint-*.json"))
    assert names == (["footprint-68-again.json", "footprint-68.json", "footprint-candidate.json"] if candidate
                     else ["footprint-68-again.json", "footprint-68.json"])
    if candidate:
        diff = json.loads((out / "diff.json").read_text(encoding="utf-8"))
        assert diff["verdict"] == "green" and diff["differences"] == [] and diff["problems"] == []
