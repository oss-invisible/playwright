"""A pytest plugin for a HUNT, never for a gate: the protocol trace of every test it runs, one JSON per test.

    python -m pytest -m e2e -p scripts.hunt_trace_plugin tests/test_control_a_selects_all_in_a_field_e2e.py

(run from the root of the repository, or with it on PYTHONPATH; the plugin is loaded with `-p` and is not in tests/conftest.py, so no other
run sees it.) Where the JSON go: `$HUNT_TRACE_DIR` (default `hunt-trace`); `$HUNT_TRACE_TAG` is a label of the run (the round, the copy) that is
written into every file name and every JSON.

Why it exists. `Page.set_content` failed, rarely, with "the caller's world never appeared for frame mainframe-N - did you call install()?
Worlds seen: ['']" (tests/test_control_a_selects_all_in_a_field_e2e.py). The message is raised by `InjectedScript.caller_context` of
invisible_core (juggler/injected.py) after 30 s of waiting for the context named `__ctx_caller__` of that frame, and the one thing that makes
that context exist is `InjectedScript.install()`, which sends `Page.setInitScripts` with a script per world (`__ctx_aux__`, `__ctx_caller__`
with worldKind "caller"): the engine creates the world when a document is created in the frame. `install()` is called once, in
`PageDispatcher._build`, after the events the page missed are replayed. This plugin records what the browser said and what the server said
to it, with times, so that a red run says in which order they happened.

What it records, per test (a time is `time.perf_counter()` in this process, written as ms from the start of the test):

  * events the engine sent (heard by a listener of every `Connection`, registered at its birth): Page.navigationStarted / navigationCommitted /
    navigationAborted / sameDocumentNavigation / frameAttached / frameDetached / eventFired, Runtime.executionContextsCleared /
    executionContextCreated / executionContextDestroyed (frameId, world name, id), Browser.attachedToTarget / detachedFromTarget; the ones
    delivered again to a page built after them (`BrowserDispatcher.replay`) carry `replay: true`;
  * the commands the server sent (`Connection.send_placed`, wrapped on the CLASS, passing everything through): the command, its reduced
    parameters (the worlds of `Page.setInitScripts`, the context of a `Runtime.evaluate`, the start of its expression) and its reply or error
    with the time it took;
  * marks: the phases of the test, `InjectedScript.install` (start, end), `InjectedScript.caller_context` (start, end or error, with the worlds
    known for the frame at that moment), and `FrameDispatcher.op_set_content` (start, end or error), which is where `set_content` goes;
  * the outcome of each phase of the test and the text of its failure.

Both modes of the `browser` fixture are traced: the in-process one and the stock-client one, because in both the server (JugglerServer) runs
in THIS process, on its own threads, and its `Connection` is the one hooked. What the stock client (the driver, a node process) says to
it is not seen; what the server does is. Nothing of src/ is touched: the hooks are installed at run time on the classes, and a hook that cannot
be installed (a name that moved) says so in the JSON (`hook_errors`) and the test goes on.
"""
from __future__ import annotations

import json
import os
import platform
import re
import threading
import time

import pytest

#: Events kept, by method; the rest of what the engine says (network, console, screencast) is not what this hunt asks.
_EVENTS = {
    "Page.navigationStarted": ("frameId", "navigationId"),
    "Page.navigationCommitted": ("frameId", "navigationId", "url"),
    "Page.navigationAborted": ("frameId", "navigationId", "errorText"),
    "Page.sameDocumentNavigation": ("frameId", "url"),
    "Page.eventFired": ("frameId", "name"),
    "Page.frameAttached": ("frameId", "parentFrameId"),
    "Page.frameDetached": ("frameId",),
    "Runtime.executionContextsCleared": (),
    "Runtime.executionContextCreated": ("executionContextId",),
    "Runtime.executionContextDestroyed": ("executionContextId",),
    "Browser.attachedToTarget": (),
    "Browser.detachedFromTarget": ("sessionId",),
}
#: Parameters kept of a command; a long text is cut.
_COMMAND_KEYS = ("frameId", "executionContextId", "url", "worldName", "name", "targetId", "browserContextId")
_CUT = 90
_FAILURE_CUT = 4000

_lock = threading.Lock()
_events: list = []          # [perf_counter, kind, method, detail, connection, session]
_state = {"connections": 0, "test": None, "hook_errors": [], "installed": False}
_replaying = threading.local()


def _now():
    return time.perf_counter()


def _add(kind, method, detail, conn=None, session=None):
    try:
        row = (_now(), kind, method, detail, conn, session)
        with _lock:
            _events.append(row)
    except Exception:      # a recorder never costs the test
        pass


def _cut(value):
    text = str(value)
    return text if len(text) <= _CUT else text[:_CUT] + "..."


def _event_detail(method, params):
    params = params if isinstance(params, dict) else {}
    out = {k: params[k] for k in _EVENTS[method] if params.get(k) is not None}
    if method == "Runtime.executionContextCreated":
        aux = params.get("auxData") or {}
        out["frameId"] = aux.get("frameId")
        out["world"] = aux.get("name") or ""
    elif method == "Browser.attachedToTarget":
        info = params.get("targetInfo") or {}
        out.update({k: info[k] for k in ("targetId", "type", "url") if info.get(k) is not None})
        if params.get("sessionId"):
            out["sessionId"] = params["sessionId"]
    if getattr(_replaying, "on", False):
        out["replay"] = True
    return out


def _command_detail(method, params):
    params = params if isinstance(params, dict) else {}
    out = {k: _cut(params[k]) for k in _COMMAND_KEYS if params.get(k) is not None}
    if method == "Page.setInitScripts":
        out["worlds"] = [[s.get("worldName"), s.get("worldKind"), len(s.get("script") or "")]
                         for s in params.get("scripts") or [] if isinstance(s, dict)]
    for key in ("expression", "functionDeclaration"):
        if params.get(key):
            out[key] = _cut(params[key])
    return out


# ---------------------------------------------------------------- the hooks on the classes

def _hook_connection():
    """Every Connection hears its events through one listener, registered when it is built (before its reader thread starts, so the first
    events are not missed), and every command goes through `send_placed`, wrapped on the class."""
    from invisible_core.juggler import connection as module

    base, cls = module.EventListeners, module.Connection
    original_init = base.__init__
    original_send = cls.send_placed

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        with _lock:
            _state["connections"] += 1
            label = "c%d" % _state["connections"]
        self._hunt_label = label

        def listen(method, params, session, _label=label):
            if method in _EVENTS:
                _add("event", method, _event_detail(method, params), _label, session)

        self.add_listener(listen)

    def send_placed(self, method, params=None, session=None, timeout=30.0):
        label = getattr(self, "_hunt_label", None)
        _add("send", method, _command_detail(method, params), label, session)
        started = _now()
        try:
            answer = original_send(self, method, params, session, timeout)
        except BaseException as failure:
            _add("error", method, {"error": _cut(failure), "ms": round((_now() - started) * 1000, 1)}, label, session)
            raise
        _add("reply", method, {"ms": round((_now() - started) * 1000, 1)}, label, session)
        return answer

    base.__init__ = init
    cls.send_placed = send_placed


def _worlds_of(injected, frame_id):
    try:
        return sorted("%s=%s" % (n or "''", v) for (f, n), v in list(injected.contexts.items()) if f == frame_id)
    except Exception as failure:
        return ["(unreadable: %s)" % failure]


def _hook_injected():
    from invisible_core.juggler import injected as module

    cls = module.InjectedScript
    install, caller_context = cls.install, cls.caller_context

    def label(self):
        return getattr(self.c, "_hunt_label", None)

    def hooked_install(self):
        _add("mark", "install.start", {}, label(self), self.session)
        try:
            return install(self)
        except BaseException as failure:
            _add("mark", "install.error", {"error": _cut(failure)}, label(self), self.session)
            raise
        finally:
            _add("mark", "install.end", {}, label(self), self.session)

    def hooked_caller_context(self, frame_id, *args, **kwargs):
        _add("mark", "caller_context.start", {"frameId": frame_id, "worlds": _worlds_of(self, frame_id)}, label(self), self.session)
        try:
            context = caller_context(self, frame_id, *args, **kwargs)
        except BaseException as failure:
            _add("mark", "caller_context.error",
                 {"frameId": frame_id, "error": _cut(failure), "worlds": _worlds_of(self, frame_id)}, label(self), self.session)
            raise
        _add("mark", "caller_context.end", {"frameId": frame_id, "executionContextId": context}, label(self), self.session)
        return context

    cls.install = hooked_install
    cls.caller_context = hooked_caller_context


def _hook_server():
    from invisible_playwright._juggler import server as module

    frame_cls = module.FrameDispatcher
    set_content = frame_cls.op_set_content
    browser_cls = module.BrowserDispatcher
    replay = browser_cls.replay

    def hooked_set_content(self, params):
        injected = getattr(self, "injected", None)
        session = getattr(injected, "session", None)
        conn = getattr(getattr(injected, "c", None), "_hunt_label", None)
        frame_id = getattr(self, "frame_id", None)
        detail = {"frameId": frame_id, "worlds": _worlds_of(injected, frame_id) if injected is not None else None,
                  "delivered": getattr(getattr(injected, "c", None), "delivered", None)}
        _add("mark", "set_content.start", detail, conn, session)
        try:
            result = set_content(self, params)
        except BaseException as failure:
            _add("mark", "set_content.error", {"frameId": frame_id, "error": _cut(failure)}, conn, session)
            raise
        _add("mark", "set_content.end", {"frameId": frame_id}, conn, session)
        return result

    def hooked_replay(self, session, deliver):
        _replaying.on = True
        try:
            return replay(self, session, deliver)
        finally:
            _replaying.on = False

    frame_cls.op_set_content = hooked_set_content
    browser_cls.replay = hooked_replay


def _install_hooks():
    if _state["installed"]:
        return
    _state["installed"] = True
    for hook in (_hook_connection, _hook_injected, _hook_server):
        try:
            hook()
        except Exception as failure:       # the hunt goes on without that part, and says so
            _state["hook_errors"].append("%s: %s: %s" % (hook.__name__, type(failure).__name__, failure))


# ---------------------------------------------------------------- the tests

def pytest_configure(config):
    _install_hooks()


def _safe(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[-120:]


class _Current:
    def __init__(self, item):
        self.item = item
        self.start = _now()
        with _lock:
            self.first = len(_events)
        self.phases = {}
        self.failure = {}


def _mode(item):
    callspec = getattr(item, "callspec", None)
    params = getattr(callspec, "params", {}) if callspec else {}
    return params.get("browser")      # "in-process" or "stock-client" (the fixture's param)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    current = _state["test"] = _Current(item)
    _add("mark", "test.start", {"nodeid": item.nodeid})
    try:
        yield
    finally:
        _add("mark", "test.end", {})
        try:
            _write(current)
        except Exception as failure:
            _state["hook_errors"].append("write: %s: %s" % (type(failure).__name__, failure))
        _state["test"] = None


def _phase_marks(phase):
    _add("mark", "test.%s.start" % phase, {})
    outcome = yield
    _add("mark", "test.%s.end" % phase, {"raised": outcome.excinfo is not None})


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_setup(item):
    yield from _phase_marks("setup")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    yield from _phase_marks("call")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_teardown(item):
    yield from _phase_marks("teardown")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    current = _state["test"]
    if current is None:
        return
    report = outcome.get_result()
    current.phases[call.when] = report.outcome
    if report.outcome in ("failed",) or (report.outcome == "skipped" and call.when == "setup"):
        text = str(report.longrepr)
        current.failure[call.when] = text if len(text) <= _FAILURE_CUT else text[:_FAILURE_CUT] + "\n..."


def _verdict(phases):
    if phases.get("setup") == "failed" or phases.get("teardown") == "failed":
        return "error"
    if phases.get("call") == "failed":
        return "failed"
    if phases.get("setup") == "skipped" or phases.get("call") == "skipped":
        return "skipped"
    return "passed" if phases.get("call") == "passed" else "unknown"


_sequence = {"n": 0}


def _write(current):
    with _lock:
        rows = list(_events[current.first:])
        _sequence["n"] += 1
        number = _sequence["n"]
    base = current.start
    events = [[round((t - base) * 1000, 2), kind, method, detail, conn, session]
              for t, kind, method, detail, conn, session in rows]
    item = current.item
    tag = os.environ.get("HUNT_TRACE_TAG", "")
    payload = {
        "nodeid": item.nodeid,
        "mode": _mode(item),
        "outcome": _verdict(current.phases),
        "phases": current.phases,
        "failure": current.failure,
        "tag": tag,
        "system": platform.system(),
        "pid": os.getpid(),
        "sequence": number,
        "hook_errors": list(_state["hook_errors"]),
        "columns": ["ms_from_test_start", "kind", "method", "detail", "connection", "session"],
        "events": events,
    }
    out = os.environ.get("HUNT_TRACE_DIR", "hunt-trace")
    os.makedirs(out, exist_ok=True)
    name = "%s%s-%04d-%s.json" % (_safe(tag) + "-" if tag else "", os.getpid(), number, _safe(item.nodeid.split("::", 1)[-1]))
    with open(os.path.join(out, name), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=1, default=str)
        handle.write("\n")
