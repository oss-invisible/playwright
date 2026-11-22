"""The wait of the install job for the index, and the upgrade test's two versions (no network, no browser).

Ledger #476 of 0.50.1: the Linux job of `user-install` failed in ``test_upgrade_e2e.py`` with "the upgrade did not
move the wrapper" ('0.49.1' != '0.49.1') three minutes after the upload: the step that waited for the index had asked the
JSON endpoint of the version, which answered, while the ``pip install --upgrade`` that followed was served an older page.
The wait now asks pip itself and ends in an error that says what is missing; the upgrade installs the version the same
listing named. The installers are two - the upgrade test installs with pip, the release test with uv (the README's way in) - so
each round asks both, and the job goes on after three rounds in a row in which both said yes (one yes proves that one request saw the
version, not that the next is served by the same node). The script of the step is run here as it stands in the workflow, with a
``pip`` and a ``uv`` that answer as a lagging index does.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "user-install.yml"


def _wait_script() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.index("- name: wait for the index to serve this version")
    block = text[start:]
    m = re.search(r"python - <<'PY'\n(.*?)\n\s*PY\n", block, re.S)
    assert m, "the wait step has no python script"
    return textwrap.dedent(m.group(1)) + "\n"


# How a fake installer counts its calls. ATOMIC: the wait kills a call that does not answer and reads the count while calls run, and
# `write_text` truncates the file before it writes, so a call killed or read in between left an empty counter (`int('')`, a ValueError
# in the release gate of 14/11). The new count goes to a file of its own and replaces the old one in one step.
_COUNT_CALL = """
import os, pathlib
p = pathlib.Path(%r)
n = int(p.read_text()) if p.exists() else 0
tmp = p.with_name(p.name + ".%%d.tmp" %% os.getpid())
tmp.write_text(str(n + 1))
os.replace(tmp, p)
"""


# The clock of the wait. The script reads `time.monotonic` and sleeps with `time.sleep` (it has no other seam), so it runs behind this preamble:
# the clock moves only by what the script sleeps and by what a call is said to cost, never by the real time a call took. How many rounds the
# wait makes is then decided by the answers of the installers and by its own arithmetic, whatever the load of the machine running the test.
# A call is free in fake time; one that hangs (exit code HUNG) costs the whole limit it was given, as a real one would. `subprocess` is imported
# before the clock is replaced, so its own timeouts stay on the real one.
_HUNG = 99
_FAKE_CLOCK = """
import subprocess, time
_now = [1000.0]
_run = subprocess.run


def _run_in_fake_time(*args, **kwargs):
    done = _run(*args, **dict(kwargs, timeout=60))
    if done.returncode == %d:
        _now[0] += kwargs["timeout"]
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    return done


def _sleep(seconds):
    _now[0] += seconds


subprocess.run = _run_in_fake_time
time.monotonic = lambda: _now[0]
time.sleep = _sleep
""" % _HUNG


def _tool(tmp_path, name, plan, load=0.0):
    """A regular package `name` on the shim path whose `__main__` answers call number i as plan[i] says (the last letter repeats):
    y = resolves it, n = no such version, h = does not answer (exits with HUNG, which the fake clock below turns into a call that outlived its limit).
    It counts its calls in `<name>-calls`. `load` is real time every call spends before it answers: what a machine busy with other work takes."""
    shim = tmp_path / "shim" / name
    shim.mkdir(parents=True)
    (shim / "__init__.py").write_text("", encoding="utf-8")   # a REGULAR package: a namespace one loses to the real one of the environment
    (shim / "__main__.py").write_text(_COUNT_CALL % str(tmp_path / (name + "-calls")) + textwrap.dedent("""
        import sys, time
        sys.stdin.read() if %r == "uv" else None
        plan = %r
        what = plan[min(n, len(plan) - 1)]
        time.sleep(%r)
        if what == "h":
            sys.exit(%d)
        if what == "y":
            sys.exit(0)
        sys.stderr.write("ERROR: No matching distribution found for invisible-playwright==9.9.9" + chr(10))
        sys.exit(1)
    """) % (name, plan, load, _HUNG), encoding="utf-8")


def _run_the_wait(tmp_path, pip="y", uv="y", *, seconds=3.0, poll=0.05, call_seconds=None, load=0.0):
    """Run the script of the step in a project named ``invisible-playwright`` 9.9.9, with a ``pip`` and a ``uv`` that answer as their plans say.
    Returns (completed process, pip calls, uv calls)."""
    # The tree declares 0.99.0 and the ledger pull request records 9.9.9 (what the step before the wait computes): the wait is about the latter.
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "invisible-playwright"\nversion = "0.99.0"\n', encoding="utf-8")
    _tool(tmp_path, "pip", pip, load)
    _tool(tmp_path, "uv", uv, load)
    env = dict(os.environ, PYTHONPATH=str(tmp_path / "shim"), INDEX_WAIT_SECONDS=str(seconds), INDEX_WAIT_POLL=str(poll), INVPW_E2E_VERSION="9.9.9")
    if call_seconds is not None:
        env["INDEX_WAIT_CALL_SECONDS"] = str(call_seconds)
    try:
        r = subprocess.run([sys.executable, "-c", _FAKE_CLOCK + _wait_script()], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        pytest.fail("the wait did not end within 60 s with INDEX_WAIT_SECONDS=%s: it does not read its deadline" % seconds)

    def calls(name):
        f = tmp_path / (name + "-calls")
        return int(f.read_text()) if f.exists() else 0
    return r, calls("pip"), calls("uv")


def test_the_wait_asks_both_installers_three_rounds_in_a_row(tmp_path):
    r, pip_calls, uv_calls = _run_the_wait(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (pip_calls, uv_calls) == (3, 3), "one yes is not enough: pip %d, uv %d" % (pip_calls, uv_calls)
    assert "pip and uv resolve invisible-playwright 9.9.9 from the index, 3 rounds in a row" in r.stdout


def test_uv_alone_saying_no_keeps_the_wait_going(tmp_path):
    """The release test installs with uv: pip seeing the version is not enough."""
    r, pip_calls, uv_calls = _run_the_wait(tmp_path, pip="y", uv="nyyy")
    assert r.returncode == 0, r.stdout + r.stderr
    assert uv_calls == 4 and pip_calls == 4, "the round with uv's no counted: pip %d, uv %d" % (pip_calls, uv_calls)


def test_a_no_between_the_yes_starts_the_count_again(tmp_path):
    r, pip_calls, _ = _run_the_wait(tmp_path, pip="yynyyy")
    assert r.returncode == 0 and pip_calls == 6, r.stdout + r.stderr


def test_calls_that_take_long_on_a_busy_machine_do_not_cut_the_rounds_short(tmp_path):
    """The rounds are decided by what the installers answer, not by how long the machine took to run them: each call spends 0.3 s of real
    time here (a busy PC), 2.4 s over the 8 calls of four rounds against a budget of 1 s, and the wait still goes on to its third yes."""
    r, pip_calls, uv_calls = _run_the_wait(tmp_path, pip="y", uv="nyyy", seconds=1.0, load=0.3)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (pip_calls, uv_calls) == (4, 4), "pip %d, uv %d" % (pip_calls, uv_calls)


def test_the_wait_ends_in_an_error_that_names_the_missing_version_when_the_index_never_has_it(tmp_path):
    r, pip_calls, _ = _run_the_wait(tmp_path, pip="n", uv="n", seconds=1.0)
    assert r.returncode == 1, "a forgotten publish must fail the job here: " + r.stdout + r.stderr
    assert "::error::the index does not have invisible-playwright 9.9.9 yet" in r.stdout
    assert "minutes" in r.stdout and "pip said: ERROR: No matching distribution" in r.stdout and "0 of 3 rounds" in r.stdout
    assert pip_calls >= 2


def test_pip_yes_and_uv_never_is_an_error_naming_uv(tmp_path):
    r, _, uv_calls = _run_the_wait(tmp_path, pip="y", uv="n", seconds=1.0)
    assert r.returncode == 1 and "uv said:" in r.stdout, r.stdout + r.stderr
    assert uv_calls >= 2


def test_a_call_that_does_not_answer_is_a_no_and_does_not_stop_the_wait(tmp_path):
    r, pip_calls, _ = _run_the_wait(tmp_path, pip="hyyy", seconds=20.0, call_seconds=0.5)
    assert r.returncode == 0 and pip_calls == 4, r.stdout + r.stderr


def test_a_call_that_never_answers_ends_in_the_error_with_what_happened(tmp_path):
    r, _, _ = _run_the_wait(tmp_path, pip="h", seconds=1.5, call_seconds=0.3)
    assert r.returncode == 1 and "pip did not answer within" in r.stdout, r.stdout + r.stderr


def test_a_call_killed_while_it_counts_leaves_the_count_it_found(tmp_path):
    """The kill lands at the worst moment: a file opened for writing (so a truncating open has already emptied it) and nothing written
    yet. The counter must still read as the number it was, never as an empty file."""
    counter = tmp_path / "pip-calls"
    counter.write_text("2")
    kill_at_the_first_write = textwrap.dedent("""
        import io, os
        _open = io.open

        class _KilledAtTheWrite:
            def __init__(self, f):
                self.f = f
            def write(self, data):
                os._exit(9)
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                self.f.close()

        def _opening(file, mode="r", *args, **kwargs):
            f = _open(file, mode, *args, **kwargs)
            if any(c in mode for c in "wax+") and str(file).startswith(%r):
                return _KilledAtTheWrite(f)
            return f
        io.open = _opening
    """) % str(tmp_path)
    r = subprocess.run([sys.executable, "-c", kill_at_the_first_write + _COUNT_CALL % str(counter)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 9, "the kill did not land: " + r.stdout + r.stderr
    assert counter.read_text() == "2", "a call killed while counting left %r" % counter.read_text()


def test_the_harness_has_uv_for_the_wait_to_ask():
    """Read as text: PyYAML is not a dependency of this package (nor of [dev]), as tests/test_ci_runs_once_per_commit.py says."""
    found = re.search(r"^[ \t]*-[ \t]*name:[ \t]*Test harness only[ \t]*\n[ \t]*run:[ \t]*(?P<run>.+)$", WORKFLOW.read_text(encoding="utf-8"), re.M)
    assert found, "the harness step is gone"
    assert "pip" in found.group("run") and found.group("run").split()[-1] == "uv", found.group("run")


def test_the_wait_step_still_passes_the_gate_of_the_core():
    from invisible_core import install_job
    assert install_job.problems(WORKFLOW.read_text(encoding="utf-8")) == []


def test_the_upgrade_goes_from_the_second_newest_to_the_newest_of_one_listing():
    import test_upgrade_e2e as upgrade
    assert upgrade._previous_and_latest(["0.48.0", "0.49.1", "0.50.1"]) == ("0.49.1", "0.50.1")


# ---- the version of a ledger pull request is the one it adds to PUBLISHED.json, not the tree's ------------------------------------------------
# Ledger #534 recorded 0.56.0 while main already declared 0.57.0 (not published until that ledger is merged): the wait asked the index for the
# version of pyproject.toml, which does not exist, and the pull request could never turn green.

def _ledger(*versions):
    import json
    return json.dumps({"comment": "x", "schema": 1, "released": [{"version": v} for v in versions]})


def _added_versions_script():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ci_ledger_added_versions_under_test", ROOT / "scripts" / "ci_ledger_added_versions.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_version_of_the_pull_request_is_the_one_it_adds_not_the_one_of_the_tree(tmp_path):
    """The case of #534: base [..0.55.0], head [..0.55.0, 0.56.0], tree 0.57.0 -> 0.56.0 (through the command the step runs)."""
    (tmp_path / "base.json").write_text(_ledger("0.54.0", "0.55.0"), encoding="utf-8")
    (tmp_path / "head.json").write_text(_ledger("0.54.0", "0.55.0", "0.56.0"), encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "invisible-playwright"\nversion = "0.57.0"\n', encoding="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "ci_ledger_added_versions.py"), "--base", "base.json", "--head", "head.json",
                        "--tree-pyproject", "pyproject.toml"], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip() == "0.56.0", r.stdout + r.stderr
    assert "the tree declares 0.57.0 (not used)" in r.stderr


def test_a_ledger_pull_request_that_adds_nothing_is_an_error_that_says_so():
    module = _added_versions_script()
    with pytest.raises(module.Refused, match="adds no version to PUBLISHED.json"):
        module.the_version_of_the_pr(_ledger("0.54.0", "0.55.0"), _ledger("0.54.0", "0.55.0"))


def test_a_ledger_pull_request_that_adds_two_versions_is_an_error_naming_both():
    module = _added_versions_script()
    with pytest.raises(module.Refused, match=r"adds 2 versions .*0\.56\.0, 0\.57\.0"):
        module.the_version_of_the_pr(_ledger("0.55.0"), _ledger("0.55.0", "0.56.0", "0.57.0"))


def test_a_version_the_head_dropped_is_not_an_addition():
    module = _added_versions_script()
    assert module.added_versions(_ledger("0.54.0", "0.55.0"), _ledger("0.55.0", "0.56.0")) == ["0.56.0"]


def test_the_install_job_takes_the_version_of_a_pull_request_from_the_ledger_and_not_from_the_tree():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert '["project"]["version"]' not in text, "a step reads the version of the tree: a ledger pull request is not about it"
    step = text.index("- name: the version this ledger pull request records")
    wait = text.index("- name: wait for the index to serve this version")
    assert step < wait, "the version is computed after the wait that needs it"
    block = text[step:wait]
    assert "if: github.event_name == 'pull_request'" in block
    assert "github.event.pull_request.base.sha" in block and "scripts/ci_ledger_added_versions.py" in block
    assert 'INVPW_E2E_VERSION=$version" >> "$GITHUB_ENV"' in block
    assert 'os.environ["INVPW_E2E_VERSION"]' in text[wait:]


def test_the_upgrade_goes_to_the_version_the_pull_request_records_from_the_one_before_it():
    import test_upgrade_e2e as upgrade
    listing = ["0.54.0", "0.55.0", "0.56.0", "0.57.0"]
    assert upgrade._previous_and_latest(listing, "0.56.0") == ("0.55.0", "0.56.0")
    assert upgrade._previous_and_latest(listing) == ("0.56.0", "0.57.0")
    with pytest.raises(AssertionError, match="lists no 0.58.0"):
        upgrade._previous_and_latest(listing, "0.58.0")
