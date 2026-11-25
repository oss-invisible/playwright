#!/usr/bin/env python3
"""The summary of tests/hunt_handle_double_hover_e2e.py: reads every run-<mode>-<index>.json of a directory, writes runs.jsonl (one line per run)
and summary.txt, and prints the table: verdicts per mode, the retry turns against the verdict, and the sequence of every run that is not `single`.

    python scripts/hunt_handle_double_hover_summary.py DIR

It judges nothing: the exit code is 0 whatever it finds (the pytest steps say whether the scenario itself errored).
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

VERDICTS = ("single", "exit-reentry", "spurious", "other", "error")


def _brief(record: dict) -> str:
    """The page's events up to the first click on one line: `over(412,310 rel=body t=..) move... out(..) over(..) click(..)`."""
    parts = []
    for e in record.get("events", []):
        name = e["type"].replace("mouse", "")
        extra = ""
        if e["type"] in ("mouseover", "mouseout", "mouseenter", "mouseleave"):
            rel = e.get("rel") or {}
            extra = "(%s,%s rel=%s t=%.1f)" % (e["x"], e["y"], rel.get("id") or rel.get("tag"), e["ts"])
        elif e["type"] == "click":
            extra = "(%s,%s t=%.1f)" % (e["x"], e["y"], e["ts"])
        elif e["type"].startswith("doc-"):
            extra = "(%s)" % e.get("target")
        parts.append(name + extra)
        if e["type"] == "click":
            break
    return " ".join(parts)


def main(argv=None) -> int:
    root = Path((argv or sys.argv[1:] or ["."])[0])
    records = []
    for path in sorted(root.glob("run-*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            records.append({"mode": "?", "index": path.name, "verdict": "error", "error": "unreadable: %s" % exc})
    with open(root / "runs.jsonl", "w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    lines = []
    out = lines.append
    modes = sorted({r.get("mode", "?") for r in records}) or ["-"]
    out("== VERDICTS per mode (run = one fresh browser, one first button.click() of a handle)")
    out("%-12s %6s " % ("mode", "runs") + " ".join("%12s" % v for v in VERDICTS) + "   test-rule-red")
    for mode in modes:
        rows = [r for r in records if r.get("mode") == mode]
        count = collections.Counter(r.get("verdict") for r in rows)
        red = sum(1 for r in rows if r.get("ok") and not r.get("test_rule_passes"))
        out("%-12s %6d " % (mode, len(rows)) + " ".join("%12d" % count.get(v, 0) for v in VERDICTS) + "   %d" % red)
    total = collections.Counter(r.get("verdict") for r in records)
    out("HUNTTOTAL runs=%d %s" % (len(records), " ".join("%s=%d" % (v, total.get(v, 0)) for v in VERDICTS)))
    live = sum(1 for r in records if r.get("hooks_live"))
    out("the hooks on the core's retry loop saw the click in %d of %d runs" % (live, len(records)))

    out("")
    out("== RETRY TURNS (turns = center-point computations of the click's _retry) against the verdict")
    by = collections.defaultdict(list)
    for r in records:
        if r.get("ok"):
            by[r["verdict"]].append(r)
    for verdict in VERDICTS:
        rows = by.get(verdict, [])
        if not rows:
            continue
        turns = collections.Counter(r.get("turns") for r in rows)
        wrong = sum(1 for r in rows if r.get("wrong_hit_target"))
        out("%-13s n=%-4d turns: %s   runs with a WrongHitTarget: %d" % (
            verdict, len(rows), ", ".join("%s turn(s) x%d" % (k, v) for k, v in sorted(turns.items(), key=lambda kv: str(kv[0]))), wrong))

    out("")
    out("== RUNS THAT ARE NOT single")
    odd = [r for r in records if r.get("verdict") != "single"]
    if not odd:
        out("none")
    for r in odd:
        out("%s #%s [%s] verdict=%s word=%s turns=%s ordine=%s" % (r.get("mode"), r.get("index"), r.get("worker"), r.get("verdict"), r.get("word"),
                                                                   r.get("turns"), r.get("ordine")))
        if r.get("verdict") == "error":
            out("    %s" % r.get("error"))
            continue
        out("    page: " + _brief(r))
        out("    core sent: " + " ".join("%s(%.0f,%.0f)" % (m["type"].replace("mouse", ""), m["x"], m["y"]) for m in r.get("core_mouse", [])))
        out("    rect changes: %s   resize: %s   layout-shifts: %s" % (r.get("rect_changes"), r.get("resize"), r.get("layout_shifts")))
        for t in r.get("retries", []):
            out("    retry: turns=%s runs=%s act=%s hit=%s exc=%s" % (t["center_point_calls"], t["run_calls"], t["act_on_target_calls"],
                                                                      [v for _, v in t["hit_verdicts"]], [e[1] for e in t["exceptions"]]))
    text = "\n".join(lines) + "\n"
    (root / "summary.txt").write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
