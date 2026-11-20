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
