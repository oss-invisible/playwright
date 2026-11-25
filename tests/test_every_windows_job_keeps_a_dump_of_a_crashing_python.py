"""Every Windows job of e2e.yml that runs pytest keeps a full dump of a python.exe that crashes, and uploads it.

On Windows the pytest worker (python.exe) died with ``Windows fatal exception: access violation`` in a thread Python has no state for, so faulthandler could print
the Python stacks but not name the native frame. Windows Error Reporting writes a full dump of a process that dies from an unhandled exception if
``HKLM\\SOFTWARE\\Microsoft\\Windows\\Windows Error Reporting\\LocalDumps\\python.exe`` says so; faulthandler's handler on Windows is a vectored one that returns
EXCEPTION_CONTINUE_SEARCH, so the exception goes on to the unhandled-exception path and WER still sees it (both stay on).

For each Windows job that runs pytest (found by ``runs-on: windows-*``, so a new one is covered by itself):

* before its first pytest step a step turns WER on (``Disabled`` 0, ``DontShowUI`` 1) and sets LocalDumps of python.exe: ``DumpType`` 2 (full), ``DumpCount`` 10,
  ``DumpFolder`` an expandable string under the runner's temp folder;
* after it, a step with ``if: always()`` uploads that folder, ``if-no-files-found: ignore``, kept 14 days, under a name no other job uses.

Known-bad inputs: a job without the step, the step after the first pytest step, a mini dump (``DumpType`` 1), a folder that is not the one uploaded, an upload that is
not ``always()``, an upload before the tests, a key for another executable. The green case is the state of the repository.
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
FOLDER = "crash-dumps"


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
    first = runs_pytest[0]
    dumps = [i for i, s in enumerate(steps) if "LocalDumps" in s]
    if len(dumps) != 1:
        return ["%s: %d steps set LocalDumps, expected 1" % (name, len(dumps))]
    step = steps[dumps[0]]
    bad = []
    if dumps[0] > first:
        bad.append("%s: LocalDumps is set after the first pytest step" % name)
    if "shell: pwsh" not in step:
        bad.append("%s: the LocalDumps step is not PowerShell" % name)
    if not re.search(r"LocalDumps\\python\.exe\"", step):
        bad.append("%s: LocalDumps is not the key of python.exe" % name)
    if not re.search(r"-Name DumpType -PropertyType DWord -Value 2\b", step):
        bad.append("%s: DumpType is not 2 (a full dump)" % name)
    if not re.search(r"-Name DumpCount -PropertyType DWord -Value 10\b", step):
        bad.append("%s: DumpCount is not 10" % name)
    if not re.search(r"-Name DumpFolder -PropertyType ExpandString -Value \$dumps\b", step):
        bad.append("%s: DumpFolder is not an expandable string" % name)
    if not re.search(r"\$dumps = Join-Path \$env:RUNNER_TEMP '%s'" % FOLDER, step):
        bad.append("%s: the dump folder is not under the runner's temp folder" % name)
    if not re.search(r"-Name Disabled -PropertyType DWord -Value 0\b", step):
        bad.append("%s: WER is not made enabled (Disabled 0)" % name)
    if not re.search(r"-Name DontShowUI -PropertyType DWord -Value 1\b", step):
        bad.append("%s: WER may show a dialog (DontShowUI 1 missing)" % name)
    uploads = [i for i, s in enumerate(steps) if "actions/upload-artifact" in s and FOLDER in s]
    if len(uploads) != 1:
        return bad + ["%s: %d steps upload the dump folder, expected 1" % (name, len(uploads))]
    up = steps[uploads[0]]
    if uploads[0] < first:
        bad.append("%s: the dump folder is uploaded before the tests ran" % name)
    if not re.search(r"^        if: always\(\)$", up, re.M):
        bad.append("%s: the upload of the dumps is not if: always()" % name)
    if not re.search(r"^          path: \$\{\{ runner\.temp \}\}/%s$" % FOLDER, up, re.M):
        bad.append("%s: the upload does not take the folder of the dumps" % name)
    if not re.search(r"^          if-no-files-found: ignore$", up, re.M):
        bad.append("%s: the upload fails when there is no dump" % name)
    if not re.search(r"^          retention-days: 14$", up, re.M):
        bad.append("%s: the dumps are not kept 14 days" % name)
    return bad


def _artifact(job: str) -> str:
    return re.search(r"^          name: (crash-dumps-.*)$", job, re.M).group(1)


def test_the_windows_jobs_are_the_ones_this_test_knows():
    assert set(_windows_jobs(E2E.read_text(encoding="utf-8"))) == EXPECTED_WINDOWS_JOBS


@pytest.mark.parametrize("name", sorted(EXPECTED_WINDOWS_JOBS))
def test_the_job_keeps_a_full_dump_of_a_crashing_python_and_uploads_it(name):
    job = _windows_jobs(E2E.read_text(encoding="utf-8"))[name]
    assert _problems(name, job) == []


def test_no_two_jobs_upload_their_dumps_under_one_name():
    jobs = _windows_jobs(E2E.read_text(encoding="utf-8"))
    names = [_artifact(j) for j in jobs.values()]
    assert len(names) == len(set(names)) == len(EXPECTED_WINDOWS_JOBS), names
    for n in names:
        assert "${{ github.run_id }}-${{ github.run_attempt }}" in n, n


def _job() -> str:
    return _windows_jobs(E2E.read_text(encoding="utf-8"))["e2e-host-windows"]


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda t: t.replace("-Name DumpType -PropertyType DWord -Value 2", "-Name DumpType -PropertyType DWord -Value 1"), "DumpType is not 2"),
        (lambda t: t.replace("LocalDumps\\python.exe", "LocalDumps\\firefox.exe"), "not the key of python.exe"),
        (lambda t: t.replace("-Name Disabled -PropertyType DWord -Value 0", "-Name Disabled -PropertyType DWord -Value 1"), "Disabled 0"),
        (lambda t: t.replace("-PropertyType ExpandString", "-PropertyType String"), "not an expandable string"),
        (lambda t: t.replace("Join-Path $env:RUNNER_TEMP 'crash-dumps'", "Join-Path $env:RUNNER_TEMP 'elsewhere'"), "not under the runner's temp"),
        (lambda t: t.replace("        if: always()\n        uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4\n        with:\n          name: crash-dumps",
                             "        uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4\n        with:\n          name: crash-dumps"), "not if: always()"),
    ],
)
def test_known_bad_jobs_are_refused(mutate, expected):
    job = _job()
    bad = mutate(job)
    assert bad != job, "the mutation did not apply: this test is stale"
    assert any(expected in p for p in _problems("e2e-host-windows", bad)), _problems("e2e-host-windows", bad)


def test_the_step_after_the_tests_and_the_missing_step_are_refused():
    job = _job()
    # drop the step by name: the job has no LocalDumps left
    name = "      - name: Windows Error Reporting keeps a full dump of a python.exe that crashes\n"
    start = job.index(name)
    end = job.index("\n      - ", start + 1)
    gone = job[:start] + job[end + 1:]
    assert "LocalDumps" not in gone
    assert any("0 steps set LocalDumps" in p for p in _problems("x", gone))
    # moved after the pytest step: put it at the end of the job
    moved = job[:start] + job[end + 1:] + "\n" + job[start:end]
    assert any("after the first pytest step" in p for p in _problems("x", moved)), _problems("x", moved)
