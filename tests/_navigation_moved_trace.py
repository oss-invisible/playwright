"""Reads the engine's own Juggler trace (``INVISIBLE_JUGGLER_TRACE_DIR``, `Trace.js`) for the line the parent writes when it moves a navigation.

The parent decides, synchronously (``DocumentLoadListener::TriggerProcessSwitch``), that a document load leaves the docShell that started it, and
tells the registry (observer topic ``juggler-navigation-moved``). The registry writes one line in the trace:

    {"proc": "parent", "where": "navigations", "what": "onNavigationMoved",
     "detail": {"frameId": "mainframe-<browser id>", "navigationId": "nav-<load id>",
                "origin": {"browsingContextId": <id>, "childId": <id>}}}

``origin`` is the browsing context the load LEFT and the child id of the process that held it (0 is the parent process). The load is found by the
address the client sent: the registry writes ``onDocumentRequest`` with the same ``navigationId`` and the ``url`` of the request. A docShell that tells
the end of a navigation tells it with a ``reporter`` of the same shape (``onNavigationAborted``, ``onStateChange abort``): the docShell the load left is
the one whose reporter is ``origin``.
"""
from __future__ import annotations

import json
import pathlib

WHERE = "navigations"
MOVED = "onNavigationMoved"
REQUESTED = "onDocumentRequest"


def read_trace(trace_dir) -> list:
    """Every line of every file of the engine's trace folder, as dicts. A line that is not whole (the engine may still be writing) is left out."""
    lines = []
    for path in sorted(pathlib.Path(trace_dir).glob("*.jsonl")):
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if isinstance(line, dict):
                lines.append(line)
    return lines


def _detail(line) -> dict:
    detail = line.get("detail")
    return detail if isinstance(detail, dict) else {}


def navigation_ids_of(lines, url) -> set:
    """The ids of the navigations whose document request was for exactly ``url``."""
    return {_detail(l).get("navigationId") for l in lines
            if l.get("where") == WHERE and l.get("what") == REQUESTED and _detail(l).get("url") == url} - {None}


def moved_lines(lines, navigation_ids=None) -> list:
    """The ``onNavigationMoved`` lines, all of them or those of the given navigations."""
    return [l for l in lines if l.get("where") == WHERE and l.get("what") == MOVED
            and (navigation_ids is None or _detail(l).get("navigationId") in navigation_ids)]


def reporters_of(lines, navigation_ids, frame_id) -> list:
    """The ``(browsingContextId, childId)`` of every docShell that told something about these navigations in this frame, in the lines that carry a ``reporter``."""
    out = []
    for l in lines:
        d = _detail(l)
        reporter = d.get("reporter")
        if d.get("navigationId") in navigation_ids and d.get("frameId") == frame_id and isinstance(reporter, dict):
            out.append((reporter.get("browsingContextId"), reporter.get("childId")))
    return out


def moved_origin(lines, url) -> tuple:
    """-> ``(browsingContextId, childId)`` the load of ``url`` left, said by exactly one ``onNavigationMoved`` line of its navigation.

    Raises AssertionError with what the trace holds when the load is not in the trace (nothing proves anything then), when no line or more than one
    is for it, when the line is not of a main frame or its origin is not a browsing context in a child process, or when a docShell that told the end
    of this navigation is not the one the load left."""
    ids = navigation_ids_of(lines, url)
    assert ids, "the trace has no document request for %s: the engine's trace is not on, or the navigation was not seen" % url
    found = moved_lines(lines, ids)
    assert len(found) == 1, "%d onNavigationMoved line(s) for %s (navigations %s): %s" % (len(found), url, sorted(ids), found)
    detail = _detail(found[0])
    origin = detail.get("origin") or {}
    assert str(detail.get("frameId", "")).startswith("mainframe-"), found[0]
    bc, child = origin.get("browsingContextId"), origin.get("childId")
    assert isinstance(bc, int) and bc > 0, "the origin names no browsing context: %s" % found[0]
    assert isinstance(child, int) and child > 0, "the origin is not a content process (0 is the parent process): %s" % found[0]
    told = set(reporters_of(lines, ids, detail["frameId"]))
    assert told <= {(bc, child)}, "a docShell other than the one the load left told its end: %s, the origin is %s" % (sorted(told), (bc, child))
    return bc, child


def assert_not_moved(lines, url) -> None:
    """The load of ``url`` is in the trace and no ``onNavigationMoved`` line is for it."""
    ids = navigation_ids_of(lines, url)
    assert ids, "the trace has no document request for %s: the engine's trace is not on, or the navigation was not seen" % url
    found = moved_lines(lines, ids)
    assert not found, "the engine told that %s moved, and it stayed in its process: %s" % (url, found)
