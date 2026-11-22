"""TEMPORARY, with tests/_generation_evidence.py: a generation of the security state that fails through the CORE's own launch leaves its evidence.

In run 45014369018 the core's `generate` (called once per module by `_ready` of tests/test_security_state_engine_e2e.py, its outcome shared by three tests)
saw the engine exit with -11 and the evidence folder had nothing of it: the core sends stdout and stderr to the null device, deletes the work profile before it
raises, and starts the engine with the test process's core limit (0 on the runners). Here a fake engine (this Python, a script) goes through the same path, the
core's real `generate` under `the_core_s_generation_keeps_its_evidence`, no browser:

* a fake that writes on stdout and stderr, leaves a file in its profile and ends by SIGSEGV (on Windows `os.kill` ends it with exit code 11): the error of the core
  is raised unchanged, and the folder holds the two outputs, the profile's files and the exit code; on Linux the fake's own RLIMIT_CORE says the soft limit was
  raised to the hard one, and where a core can land where it is looked for (a relative `kernel.core_pattern`, or `$INVPW_CORE_DIR`) and gdb exists, gdb's
  backtrace is there; elsewhere the notes say which of the two is missing;
* a fake that exits 0 but leaves no result: the core refuses AFTER the exit, the profile is already deleted, and its files are saved from what was noted at exit;
* the control: the same crash through the core WITHOUT the wrapper leaves no evidence folder at all (the state of run 45014369018), and the wrapper puts the core's
  module back as it found it.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from _generation_evidence import core_pattern, the_core_s_generation_keeps_its_evidence

pytestmark = pytest.mark.unit

NAME = "generation-under-test"
LEFT = "left-by-the-fake-engine.txt"

_FAKE = r'''
import os, signal, sys
profile = sys.argv[sys.argv.index("-profile") + 1]
with open(os.path.join(profile, "left-by-the-fake-engine.txt"), "w") as sink:
    sink.write("x")
sys.stdout.write("FAKE-ENGINE-STDOUT\n")
sys.stdout.flush()
if sys.platform.startswith("linux"):
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
    sys.stderr.write("RLIMIT_CORE %d %d\n" % (soft, hard))
sys.stderr.write("FAKE-ENGINE-STDERR\n")
sys.stderr.flush()
if sys.argv[1] == "crash":
    os.kill(os.getpid(), signal.SIGSEGV)
sys.exit(0)
'''


def _inputs(directory):
    from invisible_core._security_state_generate import Inputs

    files = {"intermediates.json": {"collection_timestamp": 1}, "onecrl.json": {"collection_timestamp": 2},
             "crlite.json": {"collection_timestamp": 3, "full": {"sha256": "0" * 64}, "deltas": []}}
    for name, document in files.items():
        (directory / name).write_text(json.dumps(document), encoding="utf-8")
    return Inputs(directory / "intermediates.json", directory / "onecrl.json", directory / "crlite.json")


@pytest.fixture
def setting(tmp_path, monkeypatch):
    """The evidence under `tmp_path/runner` (as `$RUNNER_TEMP` would be), cores of a relative pattern in `tmp_path/cwd`, the fake and the inputs."""
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "runner"))
    monkeypatch.delenv("INVPW_CORE_DIR", raising=False)
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    fake = tmp_path / "fake_engine.py"
    fake.write_text(_FAKE, encoding="utf-8")
    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir()
    return {"root": tmp_path, "fake": fake, "inputs": _inputs(inputs_dir), "folder": tmp_path / "runner" / "invpw-evidence" / NAME}


def _generate(setting, mode, *, wrapped):
    from invisible_core._security_state_generate import EngineId, GenerationError, generate

    argv = [sys.executable, "-I", str(setting["fake"]), mode]
    out = setting["root"] / ("out-" + mode)
    with pytest.raises(GenerationError) as raised:
        if wrapped:
            with the_core_s_generation_keeps_its_evidence(setting["root"] / "work", NAME):
                generate(argv, setting["inputs"], EngineId(version="1", build_id="1"), out)
        else:
            generate(argv, setting["inputs"], EngineId(version="1", build_id="1"), out)
    return str(raised.value)


def _read(path):
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else "<%s is not there>" % path.name


def test_a_crash_of_the_core_s_launch_keeps_the_engine_s_output_the_profile_and_the_core(setting):
    error = _generate(setting, "crash", wrapped=True)
    expected_code = -11 if sys.platform != "win32" else 11
    assert "exited with %d" % expected_code in error, error
    folder = setting["folder"]
    notes = _read(folder / "notes.txt")
    assert "exit code %d" % expected_code in notes and error in notes, notes
    assert "FAKE-ENGINE-STDOUT" in _read(folder / "engine.stdout"), notes
    stderr = _read(folder / "engine.stderr")
    assert "FAKE-ENGINE-STDERR" in stderr, notes
    listing = _read(folder / "profile-files.tsv")
    assert LEFT in listing and "user.js" in listing, listing
    assert (folder / "user.js").is_file(), sorted(p.name for p in folder.iterdir())
    if sys.platform.startswith("linux"):
        limits = [line.split()[1:] for line in stderr.splitlines() if line.startswith("RLIMIT_CORE ")]
        assert len(limits) == 1 and limits[0][0] == limits[0][1], "the fake's core limit was not raised to its hard limit: %s" % stderr
        pattern = core_pattern()
        lands_here = not pattern.startswith("|") and "/" not in pattern and not pattern.startswith("unreadable")
        if lands_here and subprocess.run(["sh", "-c", "command -v gdb"], stdout=subprocess.DEVNULL).returncode == 0:
            assert "gdb exited" in notes and (folder / "gdb-backtrace.txt").stat().st_size > 0, notes
            assert not list(Path.cwd().glob("core*")), "the core was not deleted after gdb read it"
        else:
            assert "no core file" in notes or "gdb is not installed" in notes, notes


def test_a_refusal_after_a_clean_exit_keeps_the_profile_the_core_deleted(setting):
    error = _generate(setting, "exit0", wrapped=True)
    assert "securitystate-result.json" in error, error
    folder = setting["folder"]
    notes = _read(folder / "notes.txt")
    assert "exit code 0" in notes and "the core deleted it afterwards" in notes, notes
    assert "FAKE-ENGINE-STDERR" in _read(folder / "engine.stderr"), notes
    assert LEFT in _read(folder / "profile-files.tsv"), notes


def test_without_the_wrapper_the_same_crash_leaves_nothing_and_the_core_is_put_back(setting):
    import invisible_core._security_state_generate as module

    before = (module.subprocess, module._wait_for_exit)
    assert before[0] is subprocess
    _generate(setting, "crash", wrapped=False)
    assert not setting["folder"].exists(), "the core alone saved something: %s" % sorted(p.name for p in setting["folder"].iterdir())
    _generate(setting, "crash", wrapped=True)
    assert (module.subprocess, module._wait_for_exit) == before, "the wrapper did not put the core's module back"
    assert (setting["folder"] / "engine.stderr").is_file()
