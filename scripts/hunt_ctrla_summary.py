#!/usr/bin/env python3
"""The summary of a Control+A hunt: the JSON of scripts/hunt_trace_plugin.py, read and put in two piles, red and green.

    python scripts/hunt_ctrla_summary.py DIR [DIR ...] [--around MS] [--top N] [--max-reds N]

DIR is a folder (or several: the artefact of each job) holding the files the plugin wrote, however deep. It prints

  * the count per system and mode of the browser fixture (in-process, stock-client): how many passed and how many were red;
  * for each red (up to --max-reds): the test, the first line of its failure and the sequence of events of the world, the context and the
    navigation of ITS page around `set_content`: what the engine said (event), what the server sent (send / reply / error) and the marks of
    the plugin (install, caller_context, set_content), with the ms counted from the start of `set_content` (negative: before it). A context is
    named by its world: main (the page's own, empty name), aux (`__ctx_aux__`, the utility world) and caller (`__ctx_caller__`, the one
    `set_content` waits for); an id is resolved to its world from the creation event;
  * the sequence that is typical of the greens, per mode (the most frequent ones, --top), in the same words and with the times of the first
    test that had it, so that a red is read against what a green does.

Nothing here decides: the exit code is 1 only when no JSON is found. The red ones are the ones whose outcome is `failed` or `error`.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

#: What the sequence is made of: the events of the worlds, the contexts, the frames and the navigations; the commands that make a world or run
#: in one; the marks of the plugin.
_EVENT = {
    "Page.navigationStarted", "Page.navigationCommitted", "Page.navigationAborted", "Page.sameDocumentNavigation", "Page.eventFired",
    "Page.frameAttached", "Page.frameDetached", "Runtime.executionContextsCleared", "Runtime.executionContextCreated",
    "Runtime.executionContextDestroyed", "Browser.attachedToTarget", "Browser.detachedFromTarget",
}
_COMMAND = {"Page.setInitScripts", "Runtime.evaluate", "Runtime.callFunction", "Page.navigate", "Page.close", "Browser.newPage"}
_WORLD = {"": "main", "__ctx_aux__": "aux", "__ctx_caller__": "caller"}
_RED = ("failed", "error")


def load(directories):
    tests = []
    for directory in directories:
        for path in sorted(Path(directory).rglob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and "events" in data and "nodeid" in data:
                data["_file"] = path.name
                tests.append(data)
    return tests


def _session_of(row):
    """The session a row belongs to: its own, or for a target that attaches the one it announces."""
    _, _, method, detail, _, session = row
    if method in ("Browser.attachedToTarget", "Browser.detachedFromTarget"):
        return (detail or {}).get("sessionId") or session
    return session


def _world_names(rows):
    """executionContextId -> world, from the creation events of this test (every connection: the ids are unique to a session)."""
    out = {}
    for _, kind, method, detail, _, _ in rows:
        if kind == "event" and method == "Runtime.executionContextCreated":
            out[str(detail.get("executionContextId"))] = _WORLD.get(detail.get("world"), detail.get("world"))
    return out


def _word(row, worlds):
    """One row as a short word: what it is and what tells it apart (the world, the frame is left out: the session is already one page)."""
    _, kind, method, detail, _, _ = row
    detail = detail or {}
    short = method.replace("Page.", "").replace("Runtime.", "").replace("Browser.", "")
    parts = []
    if method == "Runtime.executionContextCreated":
        parts.append(_WORLD.get(detail.get("world"), detail.get("world")))
    elif method == "Runtime.executionContextDestroyed":
        parts.append(worlds.get(str(detail.get("executionContextId")), "unknown ctx"))
    elif method in ("Runtime.evaluate", "Runtime.callFunction"):
        parts.append("in " + worlds.get(str(detail.get("executionContextId")), "?"))
        if detail.get("expression"):
            parts.append(detail["expression"][:40])
    elif method == "Page.setInitScripts" and kind == "send":
        parts.append("+".join(_WORLD.get(w[0], str(w[0])) for w in detail.get("worlds") or []) or "none")
    elif method.startswith("Page.navigation") and detail.get("url"):
        parts.append(detail["url"][:40])
    elif method == "Page.eventFired":
        parts.append(detail.get("name"))
    elif method in ("caller_context.start", "caller_context.error", "set_content.start", "install.start"):
        if detail.get("worlds") is not None:
            parts.append("worlds known: [%s]" % ", ".join(w.split("=")[0] for w in detail["worlds"]))
        if detail.get("error"):
            parts.append(detail["error"][:60])
    elif method == "set_content.error" and detail.get("error"):
        parts.append(detail["error"][:60])
    prefix = {"send": "-> ", "reply": "<- ", "error": "<-ERR ", "event": "", "mark": "## "}[kind]
    word = prefix + short
    if kind == "event" and detail.get("replay"):
        word += " (replayed)"
    if kind == "error":
        parts.append(str(detail.get("error", ""))[:60])
    elif kind == "reply" and detail.get("ms") is not None:
        parts.append("%s ms" % detail["ms"])
    return "%s(%s)" % (word, " ".join(str(p) for p in parts if p is not None and p != "")) if parts else word


def _interesting(row):
    _, kind, method, _, _, _ = row
    if kind == "event":
        return method in _EVENT
    if kind in ("send", "reply", "error"):
        return method in _COMMAND
    return kind == "mark" and (method.startswith(("install.", "caller_context.", "set_content.")))


def _sequence(test):
    """(session, rows of that page that matter, anchor ms): the page is the one of the `set_content` mark; the anchor is its start."""
    rows = test["events"]
    anchor_row = next((r for r in rows if r[1] == "mark" and r[2] == "set_content.start"), None) or         next((r for r in rows if r[1] == "mark" and r[2] == "caller_context.start"), None)    # the world is asked for by others too
    if anchor_row is None:
        return None, [], None, {}
    session = anchor_row[5]
    worlds = _world_names(rows)
    mine = [r for r in rows if _interesting(r) and (_session_of(r) == session or r[1] == "mark" and r[5] == session)]
    return session, mine, anchor_row[0], worlds


def _fold(words):
    """Consecutive repeats collapse: [word, ms of the first, count]."""
    folded = []
    for word, ms in words:
        if folded and folded[-1][0] == word:
            folded[-1][2] += 1
        else:
            folded.append([word, ms, 1])
    return folded


def _words(test):
    got = _sequence(test)
    session, rows, anchor, worlds = got
    if session is None and not rows:
        return None
    return [(_word(r, worlds), round(r[0] - anchor, 1)) for r in rows]


def _print_sequence(folded, indent="      "):
    for word, ms, count in folded:
        print("%s%+10.1f ms  %s%s" % (indent, ms, word, " x%d" % count if count > 1 else ""))


def _first_line(test):
    for text in (test.get("failure") or {}).values():
        for line in str(text).splitlines():
            if line.startswith("E ") and ("Error" in line or "error" in line):
                return line[1:].strip()[:200]
        return str(text).strip().splitlines()[0][:200] if str(text).strip() else ""
    return ""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directories", nargs="+", help="folders with the JSON of the plugin (searched however deep)")
    parser.add_argument("--around", type=float, default=None, metavar="MS",
                        help="show only the rows from this many ms before set_content (default: the whole life of the page)")
    parser.add_argument("--top", type=int, default=3, help="how many green sequences to show per mode (default 3)")
    parser.add_argument("--max-reds", type=int, default=10, help="how many red tests to show in detail (default 10)")
    args = parser.parse_args(argv)

    tests = load(args.directories)
    if not tests:
        print("no JSON of the plugin under %s" % ", ".join(args.directories))
        return 1
    counts = Counter((t.get("system"), t.get("mode"), "red" if t["outcome"] in _RED else t["outcome"]) for t in tests)
    print("== %d test runs" % len(tests))
    for system, mode in sorted({(s, m) for s, m, _ in counts}, key=str):
        row = {k: v for (s, m, k), v in counts.items() if (s, m) == (system, mode)}
        print("   %-8s %-13s %s" % (system, mode, ", ".join("%s %d" % (k, v) for k, v in sorted(row.items()))))
    hooks = sorted({e for t in tests for e in t.get("hook_errors") or []})
    if hooks:
        print("== hooks that could not be installed (the trace is partial):")
        for e in hooks:
            print("   " + e)

    reds = [t for t in tests if t["outcome"] in _RED]
    print("\n== %d red" % len(reds))
    for test in reds[:args.max_reds]:
        print("\n-- %s [%s, %s, pid %s, #%s]\n   %s" % (test["nodeid"], test.get("system"), test.get("mode"), test.get("pid"),
                                                     test.get("sequence"), _first_line(test)))
        words = _words(test)
        if words is None:
            print("   (no set_content and no caller_context in this test: the failure is not where the hunt looks)")
            continue
        if args.around is not None:
            words = [w for w in words if w[1] >= -args.around]
        print("   page session %s; ms from the start of set_content (events of THIS page; ## = a mark of the plugin):" % _sequence(test)[0])
        _print_sequence(_fold(words))
    if len(reds) > args.max_reds:
        print("\n(%d more red not shown: --max-reds)" % (len(reds) - args.max_reds))

    greens = [t for t in tests if t["outcome"] == "passed"]
    print("\n== typical sequence of the %d green, per mode" % len(greens))
    for mode in sorted({t.get("mode") for t in greens}, key=str):
        groups = {}
        for test in (t for t in greens if t.get("mode") == mode):
            words = _words(test)
            if words is None:
                continue
            if args.around is not None:
                words = [w for w in words if w[1] >= -args.around]
            folded = _fold(words)
            slot = groups.setdefault(tuple((w, c) for w, _, c in folded), [0, folded])
            slot[0] += 1
        ranked = sorted(groups.values(), key=lambda item: -item[0])
        print("\n-- mode %s: %d distinct sequence(s)" % (mode, len(ranked)))
        for n, folded in ranked[:args.top]:
            print("   x%d (times of the first test with it):" % n)
            _print_sequence(folded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
