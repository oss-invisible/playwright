"""A browser whose parent dies is RED in the host oracle, with its evidence: the recorder's own test (e2e, Linux).

scripts/ci_ab_host.py `crash-probe` launches the engine ONCE under the same stand-in the oracle's launches have (strace of the tree, cores enabled, a crash watcher on the
crash folders), sends SIGSEGV to the browser's parent process with `os.kill` once the browser is up, closes it as the oracle closes it, and reports what the oracle says of
that launch. This test runs it and asserts that the answer is RED, and that what the launch left is in the oracle's artifact:

  * the verdict line names the code the parent ended with (`the parent process exited with code ...: seed N, launch crash`) and the probe exits 1;
  * `strace-signals.txt` and `strace-tail.txt` hold the `--- SIGSEGV {...} ---` line of the strace log (which `-e signal=none` used to drop) and the last lines of the trace;
  * `gdb-core.*.txt` is the core run through `gdb -batch` (`thread apply all bt`: a list of threads), and `build-ids.txt` the build ids;
  * `said.txt` says what the collectors did not find (a crash reporter that wrote no `.extra` is said, never silent).

The red of the probe is the right answer, so it does not make the job red: the test PASSES when the oracle reports red with the evidence, and FAILS when the oracle reports
green for a launch that died (a recorder that does not see it), when the evidence is missing, or when the probe did not run. A probe never changes the outcome it observes:
the engine, its prefs, its environment and its crash reporter are the oracle's own; the one thing added is the signal.

Linux only (`linux_only` in ci/required-suites.json): a signal sent with `os.kill`, a core the kernel writes and gdb reads, and strace's signal line do not exist on Windows.
The job installs strace, gdb and elfutils and points the kernel's core pattern at a folder (INVPW_CORE_DIR). Evidence is kept in $RUNNER_TEMP/invpw-evidence/ab-host-crash.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ci_ab_host.py"
sys.path.insert(0, str(ROOT / "scripts"))
TIMEOUT_S = 15 * 60  # the launch, the signal, the close and gdb over a core of a browser (the job has 65 minutes)


def _evidence_dir(tmp_path: Path) -> Path:
    base = os.environ.get("AB_ORACLE_OUT")
    if base:
        return Path(base) / "ab-host-crash"
    runner = os.environ.get("RUNNER_TEMP")
    if runner:
        return Path(runner) / "invpw-evidence" / "ab-host-crash"
    return tmp_path / "ab-host-crash"


@pytest.mark.timeout(TIMEOUT_S + 120, method="thread")
def test_a_browser_whose_parent_is_sent_sigsegv_is_red_in_the_host_oracle_with_its_gdb_threads_and_its_strace_signal_line(tmp_path):
    assert sys.platform.startswith("linux"), "Linux only: the signal, the core and strace's signal line"
    binary = os.environ.get("INVPW_BINARY_PATH")
    assert binary and Path(binary).is_file(), "INVPW_BINARY_PATH must name the engine's binary, got %r" % binary
    for tool in ("strace", "gdb"):
        if shutil.which(tool) is None:
            pytest.fail("needs %s (apt install strace gdb elfutils): without it the evidence of a crash is not kept" % tool, pytrace=False)
    from _strace_stand_in import _require_strace_can_trace

    _require_strace_can_trace(tmp_path)
    out = _evidence_dir(tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    done = subprocess.run([sys.executable, "-u", str(SCRIPT), "crash-probe", binary, "--leg", "linux-x86_64", "--out", str(out)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_S)
    (out / "crash-probe.log").write_text(done.stdout + "\n--- stderr\n" + done.stderr, encoding="utf-8")
    assert done.returncode != 2, "the probe did not run:\n" + done.stdout[-2000:] + done.stderr[-1500:]
    assert done.returncode == 1, "the oracle did NOT report red for a launch whose parent was killed by SIGSEGV (exit %d):\n%s" % (done.returncode, done.stdout[-2500:])
    fail = [line for line in done.stdout.splitlines() if line.startswith("FAIL the parent process exited with code")]
    assert len(fail) == 1 and re.search(r"seed \d+, launch crash", fail[0]), "the red line names the code, the seed and the launch:\n" + done.stdout[-2500:]
    assert "AB HOST CRASH PROBE FAILED" in done.stdout
    folder = out / "footprint-crash.crash" / "crash"
    names = sorted(p.name for p in folder.glob("*")) if folder.is_dir() else []
    assert {"strace-tail.txt", "strace-signals.txt", "said.txt", "crash.json", "build-ids.txt"} <= set(names), "the evidence of the launch is in the artifact: %s" % names
    signals = (folder / "strace-signals.txt").read_text(encoding="utf-8")
    assert re.search(r"--- SIGSEGV \{si_signo=SIGSEGV, si_code=SI_USER", signals), "the signal line of the strace log (who sent it and what it was):\n" + signals[:800]
    tail = (folder / "strace-tail.txt").read_text(encoding="utf-8")
    assert tail.strip() and "+++ " in tail, "the last lines of the trace, with how the processes ended:\n" + tail[-800:]
    gdb = sorted(folder.glob("gdb-core*.txt"))
    assert gdb, "no core was found and run through gdb; the collectors said: %s\n%s" % ((folder / "said.txt").read_text(encoding="utf-8"), done.stdout[-1500:])
    text = gdb[0].read_text(encoding="utf-8")
    assert re.search(r"Thread \d+ \(", text), "gdb's `thread apply all bt` lists the threads:\n" + text[:1500]
    assert "Build ID" in (folder / "build-ids.txt").read_text(encoding="utf-8")
