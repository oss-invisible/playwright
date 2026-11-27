#!/usr/bin/env python3
"""TEMPORARY, with .github/workflows/webgl-null-in-the-row-hunt.yml (deleted with its branch hunt/webgl-null-in-the-row, never merged to main).

Two subcommands, both read by the workflow:

  args     the files of the row wrapper.e2e.suite.linux.2 (one per line, as `ci_verdict.py args` prints them) with the four hunt files put in among them: after the
           1/5, 2/5, 3/5 and 4/5 of the row's entries. xdist's `--dist loadfile` hands the files to the workers in command-line order, so a hunt file is started while the
           other workers are in the middle of the row, not all four at the start or all four at the end.
  summary  what one job found, from $HUNT_DIR: the launches (launches-<pid>.jsonl), the timeline of the tests of every worker (timeline-<pid>.jsonl), the iterations of the
           row (iterations.tsv), the junit of each iteration (junit-<n>/) and the evidence the tests kept ($RUNNER_TEMP/invpw-evidence). Writes summary.md and summary.json,
           appends the markdown to $GITHUB_STEP_SUMMARY, and exits 1 when any launch had a null WebGL context (or a hunt case failed for another reason).

Usage:
  python scripts/hunt_webgl_null.py args < row.txt > args.txt
  python scripts/hunt_webgl_null.py summary --dir "$HUNT_DIR" --shard 1
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
import xml.etree.ElementTree as ET

HUNT_FILES = ["tests/hunt_webgl_null_in_the_row_%d_e2e.py" % i for i in (1, 2, 3, 4)]
HUNT_MARK = "hunt_webgl_null_in_the_row"
WINDOW_S = 30.0          # the tests of the other workers "in that minute": 30 s before the launch starts to 30 s after it ends
MAX_OVERLAP = 60
MAX_GFX_LINES = 60
ENGINE = {12345: "on", 0: "off"}
COMPARED = ("parent_fds", "parent_threads", "x_clients", "xvfb_fds", "xvfb_count", "engine_processes_on_the_machine", "mem_available_mb", "load1")


def _lines(path: str):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
    except OSError:
        return


def interleave(row: list) -> list:
    n = len(row)
    out, at = [], 0
    for k, hunt in enumerate(HUNT_FILES, start=1):
        cut = round(k * n / (len(HUNT_FILES) + 1))
        out += row[at:cut] + [hunt]
        at = cut
    return out + row[at:]


def cmd_args(_args) -> int:
    row = [l.strip() for l in sys.stdin.read().splitlines() if l.strip()]
    if not row:
        print("hunt_webgl_null.py args: the row is empty", file=sys.stderr)
        return 2
    sys.stdout.write("\n".join(interleave(row)) + "\n")
    return 0


# ---------------------------------------------------------------- summary

def timeline(folder: str) -> list:
    """[{nodeid, worker, pid, t0, t1}]: a test from its start to its end (a start with no end runs to the last time seen)."""
    open_, done, last = {}, [], 0.0
    events = [e for path in sorted(glob.glob(os.path.join(folder, "timeline-*.jsonl"))) for e in _lines(path) if e.get("ev") in ("start", "end")]
    events.sort(key=lambda e: e["t"])
    for e in events:
        last = max(last, e["t"])
        key = (e["pid"], e["nodeid"], e.get("iteration"))
        if e["ev"] == "start":
            open_[key] = e
        elif key in open_:
            s = open_.pop(key)
            done.append({"nodeid": e["nodeid"], "worker": e.get("worker"), "pid": e["pid"], "t0": s["t"], "t1": e["t"], "iteration": e.get("iteration")})
    for s in open_.values():
        done.append({"nodeid": s["nodeid"], "worker": s.get("worker"), "pid": s["pid"], "t0": s["t"], "t1": last, "iteration": s.get("iteration"), "unfinished": True})
    return done


def overlapping(tests: list, launch: dict) -> list:
    lo, hi = launch["t_start"] - WINDOW_S, launch["t_exit"] + WINDOW_S
    out = []
    for t in tests:
        if t["pid"] == launch.get("pid") or t["t1"] < lo or t["t0"] > hi:
            continue
        out.append({"worker": t["worker"], "test": t["nodeid"], "from_s": round(t["t0"] - launch["t_start"], 1), "to_s": round(t["t1"] - launch["t_start"], 1)})
    return sorted(out, key=lambda x: (x["worker"] or "", x["from_s"]))


def evidence_for(null: dict, launch: dict) -> dict:
    """The folder the test kept for this null (RUNNER_TEMP/invpw-evidence/canvas-webgl-non-premultiplied/<worker pid>-<ms>) and the gfx2d lines of the engine's parent."""
    root = os.path.join(os.environ.get("RUNNER_TEMP", ""), "invpw-evidence", "canvas-webgl-non-premultiplied")
    pid = (launch or {}).get("pid") or null.get("pid")
    best, best_gap = None, None
    for folder in glob.glob(os.path.join(root, "%s-*" % pid)) if pid else []:
        try:
            saved = float(folder.rsplit("-", 1)[1]) / 1000.0
        except ValueError:
            continue
        gap = saved - null["t"]
        if gap >= -1 and (best_gap is None or gap < best_gap):
            best, best_gap = folder, gap
    out = {"folder": os.path.relpath(best, os.environ.get("RUNNER_TEMP", "")) if best else None, "gfx2d": [], "moz_log_files": []}
    if not best:
        return out
    logs = sorted(glob.glob(os.path.join(best, "moz-log", "*")))
    out["moz_log_files"] = [os.path.basename(p) for p in logs]
    parent = (launch or {}).get("parent_pid")
    mine = [p for p in logs if parent and (".%s." % parent in os.path.basename(p) or os.path.basename(p).endswith(".%s" % parent))] or [p for p in logs if "main" in os.path.basename(p)]
    for path in mine:
        try:
            for line in open(path, encoding="utf-8", errors="replace"):
                if "gfx2d" in line or "gfxCriticalNote" in line or "[GFX" in line:
                    if not out["gfx2d"] or out["gfx2d"][-1] != line.rstrip():
                        out["gfx2d"].append(line.rstrip()[:300])
        except OSError:
            continue
    out["gfx2d"] = out["gfx2d"][:MAX_GFX_LINES]
    return out


def junit_counts(folder: str) -> dict:
    counts = {"cases": 0, "passed": 0, "failed": 0, "skipped": 0}
    failed = []
    for path in glob.glob(os.path.join(folder, "junit-*", "*.xml")):
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        for case in root.iter("testcase"):
            if HUNT_MARK not in (case.get("classname") or "") + (case.get("file") or ""):
                continue
            counts["cases"] += 1
            if case.find("skipped") is not None:
                counts["skipped"] += 1
            elif case.find("failure") is not None or case.find("error") is not None:
                counts["failed"] += 1
                node = case.find("failure") if case.find("failure") is not None else case.find("error")
                failed.append({"case": case.get("name"), "message": (node.get("message") or "")[:300]})
            else:
                counts["passed"] += 1
    counts["failures"] = failed
    return counts


def stat(values: list) -> str:
    values = [v for v in values if isinstance(v, (int, float))]
    if not values:
        return "-"
    return "%s / %s / %s (n=%d)" % (min(values), statistics.median(values), max(values), len(values))


def cmd_summary(args) -> int:
    folder, shard = args.dir, args.shard
    launches, nulls = [], []
    for path in sorted(glob.glob(os.path.join(folder, "launches-*.jsonl"))):
        for record in _lines(path):
            (launches if record.get("ev") == "launch" else nulls).append(record)
    for launch in launches:
        launch["load1"] = (launch.get("loadavg") or [None])[0]
    by_id = {l["id"]: l for l in launches}
    tests = timeline(folder)
    iterations = [l.rstrip("\n").split("\t") for l in open(os.path.join(folder, "iterations.tsv"), encoding="utf-8")] if os.path.exists(os.path.join(folder, "iterations.tsv")) else []
    hunt_ended = [t for t in tests if HUNT_MARK in t["nodeid"] and not t.get("unfinished")]
    junit = junit_counts(folder)
    on = [l for l in launches if l["hw_seed"] == 12345]
    off = [l for l in launches if l["hw_seed"] == 0]
    null_launches = [l for l in launches if l["context"] is False]
    null_off = [l for l in null_launches if l["hw_seed"] == 0]
    null_on = [l for l in null_launches if l["hw_seed"] == 12345]
    other_null = [l for l in null_launches if l["hw_seed"] not in (0, 12345)]
    passes_off = [l for l in off if l["context"] is True]
    red = bool(null_launches) or bool(junit["failed"]) or not launches
    out = ["## WebGL null in the row, shard %s" % shard, "",
           "- row iterations ended: %d (pytest exit, seconds: %s)" % (len(iterations), "; ".join("%s: %s, %ss" % (r[0], r[1], r[2]) for r in iterations if len(r) > 2) or "-"),
           "- hunt cases run (ended in the timeline): %d; junit: %d cases, %d passed, %d failed, %d skipped" % (len(hunt_ended), junit["cases"], junit["passed"], junit["failed"], junit["skipped"]),
           "- launches recorded: engine on %d, engine off %d (the real test of the row included: %d launches outside the hunt files)" % (
               len(on), len(off), sum(1 for l in launches if HUNT_MARK not in (l.get("test") or ""))),
           "- NULLS: engine off %d, engine on %d%s" % (len(null_off), len(null_on), (", other hw_seed %d" % len(other_null)) if other_null else ""),
           "- tests of the timeline: %d, workers: %s" % (len(tests), sorted({t["worker"] for t in tests if t["worker"]})), ""]
    no_parent = [l for l in launches if not l.get("parent_pid")]
    if no_parent:
        out += ["- launches with no engine parent found below the test process (no GL libraries, no fds, no X clients): %d" % len(no_parent)]
    if not launches:
        out += ["- NO LAUNCH WAS RECORDED: the listeners did not run (the hunt measured nothing)", ""]
    for f in junit["failures"]:
        out += ["- hunt case failed in the junit: %s: %s" % (f["case"], f["message"])]
    # what the passes look like, to compare with each null
    out += ["", "### engine-off launches that got a context (min / median / max)", "", "| field | passes |", "|---|---|"]
    for key in COMPARED:
        out.append("| %s | %s |" % (key, stat([l.get(key) for l in passes_off])))
    out += [""]
    summary = {"shard": shard, "iterations": len(iterations), "hunt_cases_run": len(hunt_ended), "launches_on": len(on), "launches_off": len(off),
               "nulls_off": len(null_off), "nulls_on": len(null_on), "junit": junit, "nulls": []}
    for launch in null_launches:
        note = next((n for n in nulls if n.get("launch") == launch["id"]), {})
        ev = evidence_for(dict(note, t=note.get("t", launch["t_exit"]), pid=launch.get("pid")), launch)
        first, second = launch.get("first") or note.get("first"), note.get("second")
        overlap = overlapping(tests, launch)
        libs = launch.get("gl_libs") or []
        item = {"launch": launch["id"], "engine": ENGINE.get(launch["hw_seed"], launch["hw_seed"]), "test": launch.get("test"), "iteration": launch.get("iteration"),
                "worker": launch.get("worker"), "statusMessages": (first or {}).get("statusMessages"), "first": first, "second_after_2s": second,
                "gl_libs": libs, "x_clients": launch.get("x_clients"), "xvfb_fds": launch.get("xvfb_fds"), "parent_fds": launch.get("parent_fds"),
                "machine": {k: launch.get(k) for k in ("loadavg", "mem_available_mb", "engine_processes_on_the_machine", "xvfb_count")},
                "evidence": ev, "overlap": overlap}
        summary["nulls"].append(item)
        out += ["### NULL: engine %s, %s (iteration %s, worker %s)" % (item["engine"], item["test"], item["iteration"], item["worker"]), "",
                "- statusMessage(s) of the first attempt: %s" % json.dumps(item["statusMessages"]),
                "- first attempt: %s" % json.dumps(first), "- second attempt, 2 s later: %s" % json.dumps(second),
                "- GL libraries the parent maps: %s" % (", ".join("%s [%s]" % (l.get("path") or l.get("error"), l.get("origin", "?")) for l in libs) or "none"),
                "- X clients on its display %s: xlsclients %s, Xvfb descriptors %s; parent open fds %s, threads %s" % (
                    launch.get("display"), launch.get("x_clients"), launch.get("xvfb_fds"), launch.get("parent_fds"), launch.get("parent_threads")),
                "- machine: load %s, free memory %s MB, engine processes %s, Xvfb %s" % (launch.get("loadavg"), launch.get("mem_available_mb"),
                                                                                      launch.get("engine_processes_on_the_machine"), launch.get("xvfb_count")),
                "- kept evidence folder: %s (moz log files: %s)" % (ev["folder"], ", ".join(ev["moz_log_files"]) or "none"),
                "- gfx2d lines of the engine's parent:", "", "```"] + (ev["gfx2d"] or ["(none)"]) + ["```", "",
                "- tests of the other workers within %d s of the launch (%d%s):" % (WINDOW_S, len(overlap), ", first %d shown" % MAX_OVERLAP if len(overlap) > MAX_OVERLAP else ""), ""]
        out += ["  - w%s `%s` (%+.1f s .. %+.1f s)" % (o["worker"], o["test"], o["from_s"], o["to_s"]) for o in overlap[:MAX_OVERLAP]] or ["  - none"]
        out += [""]
    text = "\n".join(out) + "\n"
    with open(os.path.join(folder, "summary.md"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    with open(os.path.join(folder, "summary.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=1, default=str)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(text)
    print(text)
    if red:
        print("::error::the hunt is red: %d null context(s) (engine off %d, engine on %d), %d hunt case(s) failed in the junit, %d launches recorded" % (
            len(null_launches), len(null_off), len(null_on), junit["failed"], len(launches)))
        return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("args")
    s = sub.add_parser("summary")
    s.add_argument("--dir", default=os.environ.get("HUNT_DIR"), required=not os.environ.get("HUNT_DIR"))
    s.add_argument("--shard", default=os.environ.get("HUNT_SHARD", "?"))
    args = parser.parse_args(argv)
    return cmd_args(args) if args.cmd == "args" else cmd_summary(args)


if __name__ == "__main__":
    sys.exit(main())
