"""The hunt of the goto that never ends keeps EVERY exception of a round, with the phase it happened in and a kind.

⛔ THE KNOWN-BAD INPUT: run 44764553902 (shard 1, Windows): a hunter died with a Traceback raised outside the block that wrote `caught`
(only the page was inside it), so it wrote neither `caught.json` nor `summary.json`, and its log was not in the artifact. Now a launch that
raises, a connection that raises, a goto that hangs and a close that raises are each in `caught.json` with `phase`, `kind` (`goto-hung` only
for the goto's timeout that says it "neither committed nor been aborted") and the full traceback. No browser here: the pieces of a round are
stand-ins.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "hunt_goto_after_launch.py"
HUNG_TEXT = ("Page.goto: load not reached in 30s (our navigation nav-21 started and has neither committed nor been aborted, "
             "inflight requests: 0)")


@pytest.fixture(scope="module")
def hunt():
    spec = importlib.util.spec_from_file_location("hunt_goto_after_launch", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Closing:
    def __init__(self, error=None):
        self.error = error
        self.closed = 0

    def close(self):
        self.closed += 1
        if self.error:
            raise self.error


class _Page:
    def __init__(self, error):
        self.error = error

    def goto(self, *args, **kwargs):
        if self.error:
            raise self.error


class _Context(_Closing):
    def __init__(self, goto_error=None, close_error=None, page_error=None):
        super().__init__(close_error)
        self.goto_error, self.page_error = goto_error, page_error

    def route(self, *args):
        pass

    def new_page(self):
        if self.page_error:
            raise self.page_error
        return _Page(self.goto_error)


class _Browser(_Closing):
    def __init__(self, context_error=None, **context):
        super().__init__()
        self.context_error, self.context = context_error, context

    def new_context(self, **kwargs):
        if self.context_error:
            raise self.context_error
        return _Context(**self.context)


def _round(hunt, *, launch_error=None, connect_error=None, server_close_error=None, shape="launch", reuse=3, **browser):
    server = _Closing(server_close_error)
    server.ws_endpoint = "ws://x"

    def launch_server(**kwargs):
        if launch_error:
            raise launch_error
        return server

    def connect(endpoint):
        if connect_error:
            raise connect_error
        return _Browser(**browser)

    playwright = SimpleNamespace(firefox=SimpleNamespace(connect=connect))
    a = SimpleNamespace(binary="x", reuse=reuse, timeout_ms=1)
    rnd, answered, _token = hunt._run_round(playwright, launch_server, a, 1234, shape)
    return rnd, answered, server


def test_a_hung_goto_is_told_from_any_other_error(hunt):
    assert hunt.classify(HUNG_TEXT) == "goto-hung"
    assert hunt.classify("TimeoutError: Page.goto: Timeout 30000ms exceeded.") == "other"
    assert hunt.classify("playwright._impl._errors.Error: Browser closed") == "other"


def test_a_round_that_answers_has_nothing_to_keep(hunt):
    rnd, answered, server = _round(hunt, shape="reuse", reuse=3)
    assert rnd.errors == [] and answered == 3 and server.closed == 1
    assert rnd.caught(0, "reuse", "r0000-reuse", 0) is None


@pytest.mark.parametrize("failure, phase", [
    ({"launch_error": RuntimeError("no engine")}, "launch"),
    ({"connect_error": RuntimeError("no socket")}, "connect"),
    ({"context_error": RuntimeError("no context")}, "context"),
    ({"page_error": RuntimeError("no page")}, "page"),
    ({"goto_error": RuntimeError(HUNG_TEXT)}, "goto"),
])
def test_every_phase_of_a_round_is_named_in_what_is_kept(hunt, failure, phase):
    rnd, answered, server = _round(hunt, **failure)
    entry = rnd.caught(7, "launch", "r0007-launch", 0)
    assert entry["phase"] == phase and entry["round"] == 7 and entry["shape"] == "launch" and entry["folder"] == "r0007-launch"
    assert answered == 0
    assert "Traceback (most recent call last)" in entry["traceback"] and entry["error"] in entry["traceback"]
    assert entry["kind"] == ("goto-hung" if phase == "goto" else "other")
    json.dumps(entry)
    if phase != "launch":
        assert server.closed == 1      # what was started is closed, whatever failed


def test_a_close_that_raises_is_kept_and_does_not_hide_the_goto_that_hung(hunt):
    rnd, _, server = _round(hunt, goto_error=RuntimeError(HUNG_TEXT), close_error=RuntimeError("context gone"),
                            server_close_error=RuntimeError("server gone"))
    entry = rnd.caught(0, "launch", "r0000-launch", 0)
    assert (entry["phase"], entry["kind"]) == ("goto", "goto-hung")
    assert [e["phase"] for e in entry["also"]] == ["close", "close"]
    assert server.closed == 1


def test_a_close_that_raises_after_an_answered_goto_is_kept_as_the_close(hunt):
    rnd, answered, _ = _round(hunt, server_close_error=RuntimeError("server gone"))
    entry = rnd.caught(0, "launch", "r0000-launch", 0)
    assert answered == 1 and entry["phase"] == "close" and entry["kind"] == "other"


def test_a_failure_before_the_first_round_is_written_too(hunt, tmp_path, monkeypatch):
    """The stand-in web server that does not start: `caught.json` is written, the hunt ends with 1."""
    def broken():
        raise OSError("address in use")

    for name in ("INVISIBLE_JUGGLER_TRACE_DIR", "MOZ_LOG", "MOZ_LOG_FILE"):
        monkeypatch.setenv(name, "")
    monkeypatch.setattr(hunt, "_serve", broken)
    monkeypatch.setattr(sys, "argv", ["hunt", "firefox", "--evidence", str(tmp_path)])
    assert hunt.main() == 1
    caught = json.loads((tmp_path / "caught.json").read_text(encoding="utf-8"))["caught"]
    assert caught["phase"] == "server" and caught["kind"] == "other" and "address in use" in caught["traceback"]
    assert not os.path.exists(tmp_path / "summary.json")


# ── what the hunt keeps when it catches: the dumps, one log per process, where the profiles live ───────────────────────────────────

@pytest.mark.parametrize("text, drive", [("C", "C"), ("c", "C"), (" d ", "D"), ("D:", "D"), ("d:", "D")])
def test_the_temp_drive_input_is_read_in_any_case(hunt, text, drive):
    assert hunt.parse_drive(text) == drive


@pytest.mark.parametrize("text", ["", "E", "CD", "temp", "C:\\"])
def test_a_temp_drive_that_is_neither_c_nor_d_is_an_error(hunt, text):
    with pytest.raises(ValueError):
        hunt.parse_drive(text)


def test_the_temp_folder_is_the_users_on_c_and_the_runners_work_drive_on_d(hunt):
    assert hunt.temp_dir_for("C") is None
    assert hunt.temp_dir_for("d") == "D:\\a\\_temp\\hunt"


def test_c_leaves_the_environment_alone_and_d_moves_temp_and_tmp(hunt, tmp_path, monkeypatch):
    environ = {"TEMP": "the-users-temp", "TMP": "the-users-temp"}

    class FakeTempfile:
        tempdir = "cached"

        @classmethod
        def gettempdir(cls):
            assert cls.tempdir is None, "tempfile must forget what it cached, or the profiles stay where they were"
            return environ["TEMP"]

    assert hunt.apply_temp_drive("C", environ, FakeTempfile) == "the-users-temp"
    assert environ["TEMP"] == environ["TMP"] == "the-users-temp"
    FakeTempfile.tempdir = "cached"
    monkeypatch.setattr(hunt, "TEMP_ON_D", str(tmp_path / "hunt"))
    assert hunt.apply_temp_drive("D", environ, FakeTempfile) == str(tmp_path / "hunt")
    assert environ["TEMP"] == environ["TMP"] == str(tmp_path / "hunt") and (tmp_path / "hunt").is_dir()


def test_the_profiles_follow_the_temp_drive_through_the_real_tempfile(hunt, tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setattr(hunt, "TEMP_ON_D", str(tmp_path / "on-d"))
    monkeypatch.setattr(tempfile, "tempdir", None)
    for name in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(name, str(tmp_path / "elsewhere"))
    assert hunt.apply_temp_drive("D") == str(tmp_path / "on-d")
    assert os.path.dirname(tempfile.mkdtemp()) == str(tmp_path / "on-d")


def test_every_process_of_a_session_writes_its_own_log(hunt, tmp_path):
    """⛔ KNOWN-BAD: MOZ_LOG_FILE was `<folder>/moz.log`, with no `%PID`: a child that inherited the variable (the closing pings) opened the
    same file and the parent's log was overwritten. The engine expands `%PID` to `-main.<pid>` / `-child.<pid>` (xpcom/base/Logging.cpp)."""
    pattern = hunt.moz_log_file(str(tmp_path / "r0001-launch"))
    assert "%PID" in pattern and pattern.endswith(".moz_log") and os.path.dirname(pattern) == str(tmp_path / "r0001-launch")
    assert "moz.log" not in os.path.basename(pattern)

    def expand(pid, parent):          # what the engine does with the pattern
        head, tail = pattern.split("%PID")
        return "%s%s%d%s" % (head, "-main." if parent else "-child.", pid, tail)
    names = {expand(100, True), expand(100, False), expand(101, False)}
    assert len(names) == 3


def test_a_session_folder_gets_the_per_process_log_in_its_environment(hunt, tmp_path, monkeypatch):
    for name in ("INVISIBLE_JUGGLER_TRACE_DIR", "MOZ_LOG", "MOZ_LOG_FILE"):
        monkeypatch.setenv(name, "")
    folder = hunt._session_dir(str(tmp_path), "r0000-launch")
    assert os.environ["MOZ_LOG_FILE"] == hunt.moz_log_file(folder) and "%PID" in os.environ["MOZ_LOG_FILE"]
    assert os.environ["INVISIBLE_JUGGLER_TRACE_DIR"] == os.path.abspath(folder)


def test_the_dump_files_are_named_by_kind_and_pid(hunt):
    assert hunt.dump_name("parent", 1234) == "dump-parent-1234.dmp"
    assert hunt.dump_name("gpu", 55) == "dump-gpu-55.dmp"


def test_a_session_dump_writes_the_parent_and_its_gpu_process(hunt, tmp_path, monkeypatch):
    monkeypatch.setattr(hunt, "_gpu_children", lambda pid: [777] if pid == 1234 else [])
    written = []

    def writer(pid, path):
        written.append(pid)
        with open(path, "wb") as f:
            f.write(b"MDMP" + bytes(pid % 7))

    out = hunt.dump_session(str(tmp_path), 1234, writer)
    assert written == [1234, 777]
    assert [(e["kind"], e["pid"], e["file"], e["error"]) for e in out] == [
        ("parent", 1234, "dump-parent-1234.dmp", None), ("gpu", 777, "dump-gpu-777.dmp", None)]
    assert (tmp_path / "dump-parent-1234.dmp").read_bytes().startswith(b"MDMP") and out[0]["bytes"] > 0
    json.dumps(out)


def test_a_dump_that_fails_is_said_and_never_raises(hunt, tmp_path, monkeypatch):
    monkeypatch.setattr(hunt, "_gpu_children", lambda pid: [])

    def writer(pid, path):
        raise OSError("access denied")

    out = hunt.dump_session(str(tmp_path), 1234, writer)
    assert out[0]["file"] is None and "access denied" in out[0]["error"]
    assert hunt.dump_session(str(tmp_path), 1234, lambda pid, path: "OpenProcess(1234) failed: 5")[0]["error"].startswith("OpenProcess")


def test_with_no_pid_the_session_is_looked_for_by_its_folder_and_a_miss_is_said(hunt, tmp_path, monkeypatch):
    monkeypatch.setattr(hunt, "_firefox_by_trace_dir", lambda folder: None)
    out = hunt.dump_session(str(tmp_path), None, lambda pid, path: pytest.fail("no process to dump"))
    assert out[0]["pid"] is None and "no Firefox parent" in out[0]["error"]
    monkeypatch.setattr(hunt, "_firefox_by_trace_dir", lambda folder: 4321)
    monkeypatch.setattr(hunt, "_gpu_children", lambda pid: [])
    out = hunt.dump_session(str(tmp_path), None, lambda pid, path: None)
    assert out[0]["pid"] == 4321


def test_a_goto_that_hangs_is_dumped_before_anything_is_closed_and_the_dumps_are_in_caught_json(hunt, monkeypatch):
    events = []
    monkeypatch.setattr(_Closing, "close", lambda self: events.append("close"))
    monkeypatch.setattr(hunt, "dump_session", lambda folder, pid: events.append("dump") or [{"kind": "parent", "pid": pid, "file": "x.dmp"}])
    server = _Closing()
    server.ws_endpoint, server.process = "ws://x", SimpleNamespace(pid=4242)
    playwright = SimpleNamespace(firefox=SimpleNamespace(connect=lambda endpoint: _Browser(goto_error=RuntimeError(HUNG_TEXT))))
    a = SimpleNamespace(binary="x", reuse=3, timeout_ms=1)
    rnd, _, _ = hunt._run_round(playwright, lambda **kw: server, a, 1234, "launch", "r0000-launch")
    assert events[0] == "dump" and events.count("dump") == 1 and "close" in events
    entry = rnd.caught(0, "launch", "r0000-launch", 0)
    assert entry["dumps"] == [{"kind": "parent", "pid": 4242, "file": "x.dmp"}] and entry["kind"] == "goto-hung"


def test_a_launch_that_raises_is_dumped_from_the_session_folder_and_a_close_is_not_dumped(hunt, monkeypatch):
    seen = []
    monkeypatch.setattr(hunt, "dump_session", lambda folder, pid: seen.append((folder, pid)) or [])
    playwright = SimpleNamespace(firefox=SimpleNamespace(connect=lambda endpoint: _Browser()))
    a = SimpleNamespace(binary="x", reuse=3, timeout_ms=1)

    def launch_server(**kwargs):
        raise RuntimeError("the browser did not answer")
    hunt._run_round(playwright, launch_server, a, 1234, "launch", "r0003-launch")
    assert seen == [("r0003-launch", None)]          # no handle to the browser: found by its folder
    seen.clear()
    server = _Closing(RuntimeError("server gone"))
    server.ws_endpoint = "ws://x"
    hunt._run_round(playwright, lambda **kw: server, a, 1234, "launch", "r0004-launch")
    assert seen == []                                # the only exception was a close: nothing to dump


def test_a_round_that_answers_takes_no_dump(hunt, monkeypatch):
    monkeypatch.setattr(hunt, "dump_session", lambda folder, pid: pytest.fail("nothing was caught"))
    server = _Closing()
    server.ws_endpoint = "ws://x"
    playwright = SimpleNamespace(firefox=SimpleNamespace(connect=lambda endpoint: _Browser()))
    rnd, answered, _ = hunt._run_round(playwright, lambda **kw: server, SimpleNamespace(binary="x", reuse=3, timeout_ms=1), 1, "reuse", "r0000-reuse")
    assert answered == 3 and rnd.dumps == [] and "dumps" not in (rnd.caught(0, "reuse", "x", 0) or {})
