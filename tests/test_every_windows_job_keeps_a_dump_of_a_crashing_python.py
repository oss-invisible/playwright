"""Every Windows job of e2e.yml that runs pytest keeps a full dump of a python.exe that crashes, inside the evidence it uploads.

The pytest-xdist worker (python.exe) died on the Windows e2e jobs with ``node down: Not properly terminated`` and ``Windows fatal exception: access violation``
(jobs 138402013609 and 138966622085), in a thread Python has no state for, so faulthandler named no native frame. Windows Error Reporting writes a full user-mode
dump of a process that dies from an unhandled exception when ``HKLM\\SOFTWARE\\Microsoft\\Windows\\Windows Error Reporting\\LocalDumps\\python.exe`` says so
(DumpFolder, DumpType 2 = full, DumpCount; https://learn.microsoft.com/windows/win32/wer/collecting-user-mode-dumps). faulthandler's handler on Windows is a vectored
one that returns EXCEPTION_CONTINUE_SEARCH, so the exception goes on and WER still sees it.

For each Windows job that runs pytest (found by ``runs-on: windows-*``, so a new one is covered by itself):

* before its first pytest step a PowerShell step turns WER on (``Disabled`` 0, ``DontShowUI`` 1) and sets LocalDumps of python.exe: ``DumpType`` 2, ``DumpCount`` 3,
  ``DumpFolder`` an expandable string, ``$RUNNER_TEMP/invpw-evidence/dumps/python``: inside the folder the job's evidence upload takes;
* after the tests a step with ``if: always()`` runs ``scripts/ci_wer_dumps.py`` on that folder (sizes, faulting module), before the upload;
* the upload of ``$RUNNER_TEMP/invpw-evidence`` is ``if: always()``, after the tests.

Known-bad inputs: a job without the step, the step after the first pytest step, a mini dump (DumpType 1), a key of another executable, a folder outside the evidence, a
summary missing or not ``always()``, an upload that is not ``always()``. The green case is the state of the repository.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

E2E = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "e2e.yml"
EXPECTED_WINDOWS_JOBS = {
    "e2e-windows-control-a", "e2e-windows", "e2e-network-windows", "e2e-host-windows", "e2e-page-windows", "e2e-page-final-windows",
}
SUMMARY_STEP = "      - name: What Windows Error Reporting kept of a crashing python.exe (size, faulting module, in the evidence)\n"
WER_STEP = "      - name: Windows Error Reporting keeps a full dump of a python.exe that crashes (into the evidence)\n"
ALWAYS = "        if: always()\n"


def _jobs(text: str) -> dict[str, str]:
    """job id -> its text, for the jobs of the file (a job starts at two spaces of indent)."""
    body = "\n" + text.split("\njobs:\n", 1)[1]
    starts = [(m.group(1), m.start()) for m in re.finditer(r"\n  ([A-Za-z0-9_-]+):[ ]*(?=\n)", body)]
    out = {}
    for i, (name, at) in enumerate(starts):
        out[name] = body[at:starts[i + 1][1] if i + 1 < len(starts) else len(body)]
    return out


def _windows_jobs(text: str) -> dict[str, str]:
    return {k: v for k, v in _jobs(text).items() if re.search(r"^    runs-on: windows-", v, re.M)}


def _steps(job: str) -> list[str]:
    """The steps of a job, each without its comment lines."""
    body = job.split("\n    steps:\n", 1)[1]
    chunks = re.split(r"\n(?=      - )", "\n" + body)
    steps = []
    for c in chunks:
        lines = [ln for ln in c.split("\n") if not ln.strip().startswith("#")]
        if any(ln.startswith("      - ") for ln in lines):
            steps.append("\n".join(lines))
    return steps


def _problems(name: str, job: str) -> list[str]:
    steps = _steps(job)
    runs_pytest = [i for i, s in enumerate(steps) if re.search(r"pytest|run_e2e\.py", s)]
    if not runs_pytest:
        return ["%s: no step runs pytest (this test finds them by name: update it with the job)" % name]
    first, last = runs_pytest[0], runs_pytest[-1]
    dumps = [i for i, s in enumerate(steps) if "LocalDumps" in s]
    if len(dumps) != 1:
        return ["%s: %d steps set LocalDumps, expected 1" % (name, len(dumps))]
    step = steps[dumps[0]]
    bad = []
    if dumps[0] > first:
        bad.append("%s: LocalDumps is set after the first pytest step" % name)
    if "shell: pwsh" not in step:
        bad.append("%s: the LocalDumps step is not PowerShell" % name)
    if not re.search(r'LocalDumps\\python\.exe"', step):
        bad.append("%s: LocalDumps is not the key of python.exe" % name)
    if not re.search(r"-Name DumpType -PropertyType DWord -Value 2\b", step):
        bad.append("%s: DumpType is not 2 (a full dump)" % name)
    if not re.search(r"-Name DumpCount -PropertyType DWord -Value 3\b", step):
        bad.append("%s: DumpCount is not 3" % name)
    if not re.search(r"-Name DumpFolder -PropertyType ExpandString -Value \$dumps\b", step):
        bad.append("%s: DumpFolder is not an expandable string" % name)
    if not re.search(r"\$dumps = Join-Path \$env:RUNNER_TEMP 'invpw-evidence\\dumps\\python'", step):
        bad.append("%s: the dump folder is not RUNNER_TEMP/invpw-evidence/dumps/python (the folder the evidence upload takes)" % name)
    if not re.search(r"-Name Disabled -PropertyType DWord -Value 0\b", step):
        bad.append("%s: WER is not made enabled (Disabled 0)" % name)
    if not re.search(r"-Name DontShowUI -PropertyType DWord -Value 1\b", step):
        bad.append("%s: WER may show a dialog (DontShowUI 1 missing)" % name)
    uploads = [i for i, s in enumerate(steps) if "actions/upload-artifact" in s and re.search(r"^\s+(path: )?\$\{\{ runner\.temp \}\}/invpw-evidence$", s, re.M)]
    if len(uploads) != 1:
        return bad + ["%s: %d steps upload the evidence folder, expected 1" % (name, len(uploads))]
    up = steps[uploads[0]]
    if uploads[0] < last:
        bad.append("%s: the evidence is uploaded before the last pytest step" % name)
    if not re.search(r"^        if: always\(\)$", up, re.M):
        bad.append("%s: the upload of the evidence is not if: always()" % name)
    summaries = [i for i, s in enumerate(steps) if "scripts/ci_wer_dumps.py" in s]
    if len(summaries) != 1:
        return bad + ["%s: %d steps run scripts/ci_wer_dumps.py, expected 1" % (name, len(summaries))]
    s = steps[summaries[0]]
    if not (last < summaries[0] < uploads[0]):
        bad.append("%s: the dump summary is not between the last pytest step and the upload" % name)
    if not re.search(r"^        if: always\(\)$", s, re.M):
        bad.append("%s: the dump summary is not if: always()" % name)
    if 'invpw-evidence/dumps/python"' not in s:
        bad.append("%s: the dump summary does not read the dump folder" % name)
    return bad


def _job() -> str:
    return _windows_jobs(E2E.read_text(encoding="utf-8"))["e2e-host-windows"]


def test_the_windows_jobs_are_the_ones_this_test_knows():
    assert set(_windows_jobs(E2E.read_text(encoding="utf-8"))) == EXPECTED_WINDOWS_JOBS


@pytest.mark.parametrize("name", sorted(EXPECTED_WINDOWS_JOBS))
def test_the_job_keeps_a_full_dump_of_a_crashing_python_in_its_evidence(name):
    job = _windows_jobs(E2E.read_text(encoding="utf-8"))[name]
    assert _problems(name, job) == []


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda t: t.replace("-Name DumpType -PropertyType DWord -Value 2", "-Name DumpType -PropertyType DWord -Value 1"), "DumpType is not 2"),
        (lambda t: t.replace("LocalDumps\\python.exe", "LocalDumps\\firefox.exe"), "not the key of python.exe"),
        (lambda t: t.replace("-Name Disabled -PropertyType DWord -Value 0", "-Name Disabled -PropertyType DWord -Value 1"), "Disabled 0"),
        (lambda t: t.replace("-PropertyType ExpandString", "-PropertyType String"), "not an expandable string"),
        (lambda t: t.replace("'invpw-evidence\\dumps\\python'", "'crash-dumps'"), "not RUNNER_TEMP/invpw-evidence/dumps/python"),
        (lambda t: t.replace("scripts/ci_wer_dumps.py", "scripts/nothing.py"), "0 steps run scripts/ci_wer_dumps.py"),
        (lambda t: t.replace(SUMMARY_STEP + ALWAYS, SUMMARY_STEP), "dump summary is not if: always()"),
        (lambda t: t.replace("      - name: Upload what the test saved\n" + ALWAYS, "      - name: Upload what the test saved\n"), "upload of the evidence is not if: always()"),
    ],
)
def test_known_bad_jobs_are_refused(mutate, expected):
    job = _job()
    bad = mutate(job)
    assert bad != job, "the mutation did not apply: this test is stale"
    assert any(expected in p for p in _problems("e2e-host-windows", bad)), _problems("e2e-host-windows", bad)


def test_the_step_after_the_tests_and_the_missing_step_are_refused():
    job = _job()
    start = job.index(WER_STEP)
    end = job.index("\n      - ", start + 1)
    gone = job[:start] + job[end + 1:]
    assert not any("LocalDumps" in s for s in _steps(gone))
    assert any("0 steps set LocalDumps" in p for p in _problems("x", gone))
    moved = job[:start] + job[end + 1:] + "\n" + job[start:end]
    assert any("after the first pytest step" in p for p in _problems("x", moved)), _problems("x", moved)
