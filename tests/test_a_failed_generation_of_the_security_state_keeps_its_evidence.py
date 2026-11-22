"""TEMPORARY, with tests/_generation_evidence.py and tests/_crash_cores.py: a generation of the security state that fails through the CORE's own launch leaves its
evidence, gdb's text of the core included.

In run 45014369018 the core's `generate` (called once per module by `_ready` of tests/test_security_state_engine_e2e.py, its outcome shared by three tests)
saw the engine exit with -11 and the evidence folder had nothing of it: the core sends stdout and stderr to the null device, deletes the work profile before it
raises, and starts the engine with the test process's core limit (0 on the runners). Here a fake engine (this Python, a script) goes through the same path, the
core's real `generate` under `the_core_s_generation_keeps_its_evidence`, no browser, and then through what tests/conftest.py does after a test that fails
(`_crash_cores.after_the_report`):

* a fake that writes on stdout and stderr, leaves a file in its profile and ends by SIGSEGV (on Windows `os.kill` ends it with exit code 11): the error of the core
  is raised unchanged, and the folder holds the two outputs, the profile's files and the exit code. On Linux the fake's own RLIMIT_CORE is the session's, its soft
  limit equal to the hard one (raised once by tests/conftest.py at the start of pytest, not by the launch: the core's Popen is not given a limit), and where a core
  lands where it is looked for (`$INVPW_CORE_DIR` under an absolute `kernel.core_pattern` there, as on the e2e jobs, or a relative pattern) and gdb exists, the
  evidence holds gdb's TEXT with a backtrace of the SIGSEGV and the core is deleted; where no core can land, the notes say there was none. On Windows the notes say
  that cores are Linux only and nothing of them is written;
* a fake that exits 0 but leaves no result: the core refuses AFTER the exit, the profile is already deleted, and its files are saved from what was noted at exit;
* the control: the same crash through the core WITHOUT the wrapper leaves no evidence folder at all (the state of run 45014369018), and the wrapper puts the core's
  module back as it found it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import _crash_cores
from _generation_evidence import the_core_s_generation_keeps_its_evidence

pytestmark = pytest.mark.unit

NAME = "generation-under-test"
#: The test whose failure `after_the_report` is told about.
NODEID = "tests/test_security_state_engine_e2e.py::the_test_that_failed"
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


def _where_a_core_lands(tmp_path, monkeypatch):
    """Linux: the folder where a crash of the fake leaves its core and `after_the_report` looks for it, set as `$INVPW_CORE_DIR`; or None when no core can land
    there (a piped `kernel.core_pattern`, a hard limit of 0), with `$INVPW_CORE_DIR` an empty folder. Windows: None, and no `$INVPW_CORE_DIR` (as on the runners)."""
    if not sys.platform.startswith("linux"):
        monkeypatch.delenv("INVPW_CORE_DIR", raising=False)
        return None
    import resource

    pattern = _crash_cores.core_pattern()
    given = (os.environ.get("INVPW_CORE_DIR") or "").rstrip("/")
    if resource.getrlimit(resource.RLIMIT_CORE)[1] != 0:
        if given and Path(given).is_dir() and pattern.startswith(given + "/") and "/" not in pattern[len(given) + 1:]:
            return Path(given)
        if not pattern.startswith("|") and "/" not in pattern and not pattern.startswith("unreadable"):
            monkeypatch.setenv("INVPW_CORE_DIR", str(tmp_path / "cwd"))
            return tmp_path / "cwd"
    (tmp_path / "no-core").mkdir()
    monkeypatch.setenv("INVPW_CORE_DIR", str(tmp_path / "no-core"))
    return None


@pytest.fixture
def setting(tmp_path, monkeypatch):
    """The evidence under `tmp_path/runner` (as `$RUNNER_TEMP` would be), the folder a core lands in, the fake and the inputs. At the end the cores the fakes left
    (the control's crash too) are deleted as after a passing test, while `$RUNNER_TEMP` is still `tmp_path/runner`."""
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "runner"))
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    lands = _where_a_core_lands(tmp_path, monkeypatch)
    fake = tmp_path / "fake_engine.py"
    fake.write_text(_FAKE, encoding="utf-8")
    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir()
    yield {"root": tmp_path, "fake": fake, "inputs": _inputs(inputs_dir), "folder": tmp_path / "runner" / "invpw-evidence" / NAME, "lands": lands,
           "cores": tmp_path / "runner" / "invpw-evidence" / "cores"}
    _crash_cores.after_the_report("the fixture of " + NODEID, "teardown", False)


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


def test_a_crash_of_the_core_s_launch_keeps_the_engine_s_output_the_profile_and_gdb_s_text_of_the_core(setting):
    error = _generate(setting, "crash", wrapped=True)
    _crash_cores.after_the_report(NODEID, "call", True)  # what tests/conftest.py does after the test that ran the generation fails
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
    if not sys.platform.startswith("linux"):
        assert "core files: Linux only" in notes, notes
        assert not setting["cores"].exists(), "a folder of cores on %s: %s" % (sys.platform, sorted(p.name for p in setting["cores"].rglob("*")))
        return
    import resource

    session = resource.getrlimit(resource.RLIMIT_CORE)
    limits = [tuple(int(part) for part in line.split()[1:]) for line in stderr.splitlines() if line.startswith("RLIMIT_CORE ")]
    assert session[0] == session[1], "the session's core limit was not raised to its hard limit: %r" % (session,)
    assert limits == [session], "the fake's core limit %r is not the session's %r" % (limits, session)
    assert "read by tests/_crash_cores.py" in notes, notes
    cores = setting["cores"] / _crash_cores.folder_name(NODEID)
    cores_notes = _read(cores / "notes.txt")
    if setting["lands"] is not None and subprocess.run(["sh", "-c", "command -v gdb"], stdout=subprocess.DEVNULL).returncode == 0:
        texts = sorted(cores.glob("core*.gdb.txt"))
        assert len(texts) == 1, cores_notes
        text = _read(texts[0])
        assert "SIGSEGV" in text and re.search(r"^#0\s", text, re.M), "gdb's text has no backtrace of the SIGSEGV:\n%s" % text[:4000]
        assert "gdb exited" in cores_notes, cores_notes
        assert not list(setting["lands"].glob("core*")), "the core was not deleted after gdb read it"
    elif setting["lands"] is not None:
        assert "gdb is not installed" in cores_notes, cores_notes
    else:
        assert "no core file" in cores_notes, cores_notes


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
