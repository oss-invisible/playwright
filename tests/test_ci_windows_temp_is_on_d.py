r"""Every Windows job that launches a browser has TEMP and TMP on D:, set on the job.

The profile of every Firefox the tests launch is made in TEMP. On the Windows runners of GitHub the default TEMP is on C:, and four browsers at
once saturate that disk (a queue up to 144, latencies up to 4 s): the parent's main thread stays still 40-60 s and the e2e fail at random
(a `goto` that hangs, a start that never answers). Measured on 20/11 with two hunts: the profiles on C: took 5 of 4 shards, TEMP and TMP on
`D:\a\_temp` took none. A test cannot fail for a reason that is not its own, so the fix is at the source: the job's `env` names D:.

`runner.temp` is not readable in a job's `env` (only `github`, `needs`, `strategy`, `matrix`, `inputs`, `secrets` and `vars` are), so the job
names the runner's temp folder itself: `D:\a\_temp`, which the runner makes before the first step. A job that runs on both systems says
`startsWith(<where it runs>, 'windows') && 'D:\a\_temp' || '/tmp'` (`/tmp` is what Linux uses anyway).

PyYAML is not a dependency of this package, so the workflows are read line by line: a job is the block under `  <id>:` below `jobs:`, its
fields are the ones at four spaces, its `env` the keys at six, where it runs the values of the matrix key (or workflow input) its `runs-on` names.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
BS = chr(92)
D_TEMP = "D:" + BS + "a" + BS + "_temp"

#: A Windows job that is left alone, and why. Nothing else is. (Empty: every Windows job of this repository launches a browser.)
EXEMPT: dict[tuple[str, str], str] = {}

CONDITIONAL = re.compile(
    r"^\$\{\{\s*startsWith\((?P<ctx>(?:matrix|inputs)\.\w+),\s*'windows'\)\s*&&\s*'(?P<win>[^']*)'\s*\|\|\s*'(?P<other>[^']*)'\s*\}\}$")
CONTEXT = re.compile(r"^\$\{\{\s*(?:matrix|inputs)\.(\w+)\s*\}\}$")
JOB = re.compile(r"^  ([\w-]+):\s*$")


def _jobs(text: str) -> dict[str, list[str]]:
    lines = text.split("\n")
    start = next(i for i, line in enumerate(lines) if line == "jobs:")
    jobs, current = {}, None
    for line in lines[start + 1:]:
        m = JOB.match(line)
        if m:
            current = jobs.setdefault(m.group(1), [])
        elif current is not None:
            current.append(line)
    return jobs


def _field(block: list[str], key: str):
    for line in block:
        m = re.match(r"^    %s:\s*(.*?)\s*$" % re.escape(key), line)
        if m:
            return m.group(1)
    return None


def _env(block: list[str]) -> dict[str, str]:
    out, inside = {}, False
    for line in block:
        if re.match(r"^    env:\s*$", line):
            inside = True
        elif inside:
            m = re.match(r"^      ([\w.-]+):\s*(.*?)\s*$", line)
            if m:
                out[m.group(1)] = m.group(2)
            elif line.strip() and not line.strip().startswith("#"):
                inside = False
    return out


def _strip(value: str) -> str:
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"" else value


def _values_of(text: str, block: list[str], context: str, key: str) -> list[str]:
    """The values `key` takes: in the job's matrix (`key: [a, b]` or `{ key: a, ... }` rows), or the options of the workflow input of that name."""
    if context == "inputs":
        options = re.search(r"^      %s:\n(?:        .*\n)*?        options:\n((?:          - .*\n)+)" % re.escape(key), text, re.M)
        return [_strip(x.strip()) for x in re.findall(r"- (.*)", options.group(1))] if options else []
    out = []
    for line in block:
        m = re.match(r"^\s+%s:\s*\[(.*)\]\s*$" % re.escape(key), line)
        if m:
            out += [_strip(x.strip()) for x in m.group(1).split(",") if x.strip()]
        out += [_strip(x) for x in re.findall(r"[{,]\s*%s:\s*([^,}\s]+)" % re.escape(key), line)]
    return out


def windows_jobs_without_temp_on_d(name: str, text: str) -> list[str]:
    """One line per Windows leg of the workflow whose TEMP or TMP is not on D:."""
    problems = []
    for job_id, block in _jobs(text).items():
        runs_on = _field(block, "runs-on")
        if runs_on is None or (name, job_id) in EXEMPT:
            continue
        m = CONTEXT.match(runs_on)
        if m:
            context = re.search(r"(matrix|inputs)", runs_on).group(1)
            legs = [(v, {context + "." + m.group(1): v}) for v in _values_of(text, block, context, m.group(1))]
        else:
            legs = [(runs_on, {})]
        env = _env(block)
        for runner, leg in legs:
            if "windows" not in runner:
                continue
            for var in ("TEMP", "TMP"):
                value = None
                if var in env:
                    raw = env[var]
                    c = CONDITIONAL.match(_strip(raw)) or CONDITIONAL.match(raw)
                    if c:
                        value = _strip(c.group("win")) if leg.get(c.group("ctx"), "").startswith("windows") else c.group("other")
                    else:
                        value = _strip(raw)
                if value is None or not value.upper().startswith("D:" + BS):
                    problems.append("%s: job `%s` on %s has %s=%r, not a folder on D:" % (name, job_id, runner, var, value))
    return problems


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_every_windows_job_keeps_the_profiles_on_d(path):
    assert windows_jobs_without_temp_on_d(path.name, path.read_text(encoding="utf-8")) == []


def test_the_windows_jobs_that_launch_a_browser_are_all_seen():
    """The check must find the legs it is meant to judge: a parser that sees none says fine to everything."""
    seen = {}
    for path in WORKFLOWS.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        for job_id, block in _jobs(text).items():
            runs_on = _field(block, "runs-on") or ""
            m = CONTEXT.match(runs_on)
            values = _values_of(text, block, re.search(r"(matrix|inputs)", runs_on).group(1), m.group(1)) if m else [runs_on]
            if any("windows" in v for v in values):
                seen[(path.name, job_id)] = True
    expected = {("e2e.yml", "e2e-windows"), ("e2e.yml", "e2e-windows-control-a"), ("fakeweb.yml", "walk"), ("tests.yml", "unit"),
                ("firefox-launch-matrix.yml", "smoke"), ("real-sites.yml", "round"), ("real-sites.yml", "admit"),
                ("user-install.yml", "install"), ("e2e-hunt.yml", "hunt")}
    assert expected <= set(seen), "the check does not see %s" % sorted(expected - set(seen))


def _wf(runs_on, env="", matrix=""):
    text = "jobs:\n  a:\n    runs-on: %s\n" % runs_on
    if env:
        text += "    env:\n" + "".join("      %s: %s\n" % kv for kv in env)
    return text + matrix + "    steps:\n      - run: x\n"


def test_the_check_sees_what_it_is_meant_to_see():
    """A check that has only ever said fine is not known to say anything else."""
    check = windows_jobs_without_temp_on_d
    assert check("x.yml", _wf("windows-2022")), "a Windows job with no TEMP passed"
    assert len(check("x.yml", _wf("windows-2022", [("TEMP", "C:" + BS + "t"), ("TMP", "'" + D_TEMP + "'")]))) == 1, "TEMP on C: passed"
    assert len(check("x.yml", _wf("windows-2022", [("TEMP", "'" + D_TEMP + "'")]))) == 1, "a job with TEMP and no TMP passed"
    assert check("x.yml", _wf("windows-2022", [("TEMP", "'" + D_TEMP + "'"), ("TMP", "'" + D_TEMP + "'")])) == []
    cond = "${{ startsWith(matrix.os, 'windows') && '" + D_TEMP + "' || '/tmp' }}"
    matrix = "    strategy:\n      matrix:\n        os: [ubuntu-24.04, windows-latest]\n"
    assert check("x.yml", _wf("${{ matrix.os }}", [("TEMP", cond), ("TMP", cond)], matrix)) == []
    wrong_way = cond.replace("'" + D_TEMP + "' || '/tmp'", "'/tmp' || '" + D_TEMP + "'")
    assert len(check("x.yml", _wf("${{ matrix.os }}", [("TEMP", wrong_way), ("TMP", wrong_way)], matrix))) == 2, "a matrix job on C: on Windows passed"
    assert len(check("x.yml", _wf("${{ matrix.os }}", None, matrix))) == 2, "a matrix job with no TEMP passed"
    include = "    strategy:\n      matrix:\n        include:\n          - { os: linux, runner: ubuntu-24.04 }\n          - { os: windows, runner: windows-2022 }\n"
    assert len(check("x.yml", _wf("${{ matrix.runner }}", None, include))) == 2, "a Windows leg of an `include` matrix was not seen"
    assert check("x.yml", _wf("ubuntu-24.04")) == [], "a Linux job was judged"
    inputs = "on:\n  workflow_dispatch:\n    inputs:\n      os:\n        type: choice\n        options:\n          - ubuntu-24.04\n          - windows-latest\n"
    assert len(check("x.yml", inputs + _wf("${{ inputs.os }}"))) == 2, "a Windows option of a workflow input was not seen"
