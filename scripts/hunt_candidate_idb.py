"""HUNT ONLY, never merged. The report of the IndexedDB hunt: from the rows of the arms (rows.log) and the MOZ_LOG files of every process
(MOZ_LOG=IndexedDB:5,sync,timestamp, MOZ_LOG_FILE=<dir>/ff-%PID.log, one file per process, gzipped after each arm), per browser:

  * the transactions of the IndexedDB log: how many, by mode, which were open at the quit (started before it, not completed by it), for how
    long they had been open and when each completed against the quit (the time the blocker "RemoteSettingsClient - finish IDB access."
    holds profile-before-change), and whether the log says they are on the Remote Settings database;
  * the census of the browser's processes at the close (type by command line, `-contentproc ... <type>`), ours against retail: which process
    types differ, with their command lines.

    python hunt_candidate_idb.py ROWS_LOG MOZLOG_DIR

The IndexedDB lines are not assumed to have one shape: a transaction is whatever carries `T[<n>]` in the same file, its mode the first of
readonly / readwrite / readwriteflush / versionchange / cleanup its lines name, its completion the first line that says complete / abort /
commit, and the raw first lines of the transactions that matter, and a sample of the log, are printed so the reading can be checked."""
from __future__ import annotations

import calendar
import collections
import glob
import gzip
import json
import os
import re
import sys

LINE = re.compile(r"^(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)\.(\d+) UTC - \[(\w+) (\d+): ([^\]]*)\]: (\w)/(\S+) (.*)$")
TX = re.compile(r"\bT\[(\d+)\]")
MODE = re.compile(r"\b(readwriteflush|readwrite|readonly|versionchange|cleanup)\b", re.I)
COMPLETE = re.compile(r"\b(complete|completed|aborted|abort|committed|commit)\b", re.I)
DB_END = re.compile(r"(finished database work|DB End)", re.I)
ID_OF = re.compile(r"IndexedDB ([^:\s]+):")
RS = re.compile(r"remote-settings|RemoteSettings", re.I)
FILE_PID = (re.compile(r"ff--(?:main|child)\.(\d+)"), re.compile(r"ff-(\d+)"))


def opened(path):
    return gzip.open(path, "rt", encoding="utf-8", errors="replace") if path.endswith(".gz") else open(path, encoding="utf-8", errors="replace")


def pid_of(name):
    for rx in FILE_PID:
        m = rx.search(name)
        if m:
            return int(m.group(1))
    return None


def parse(path):
    """The IndexedDB lines of one file: (epoch ms, process kind, pid, thread, message)."""
    out = []
    with opened(path) as f:
        for raw in f:
            m = LINE.match(raw.rstrip("\r\n"))
            if not m or "IndexedDB" not in m.group(12):
                continue
            y, mo, d, h, mi, s, us = (int(m.group(k)) for k in range(1, 8))
            t = calendar.timegm((y, mo, d, h, mi, s)) * 1000 + int(str(m.group(7))[:3].ljust(3, "0"))
            out.append((t, m.group(8), int(m.group(9)), m.group(10), m.group(13)))
    return out


def transactions(lines):
    """Group the lines by `T[n]` (and the id string of the factory that logged them)."""
    tx = collections.OrderedDict()
    rs_ids = {ID_OF.search(x).group(1) for _t, _k, _p, _th, x in lines if RS.search(x) and ID_OF.search(x)}
    for t, kind, pid, th, x in lines:
        m = TX.search(x)
        if not m:
            continue
        ident = ID_OF.search(x)
        key = (ident.group(1) if ident else "?", int(m.group(1)))
        d = tx.setdefault(key, {"id": key[0], "serial": key[1], "start": t, "last": t, "mode": None, "complete": None, "db_end": None,
                                "first": x, "rs": bool(ident and ident.group(1) in rs_ids) or bool(RS.search(x)), "n": 0, "kind": kind, "pid": pid})
        d["n"] += 1
        d["last"] = t
        if d["mode"] is None:
            mm = MODE.search(x)
            if mm:
                d["mode"] = mm.group(1).lower()
        if d["complete"] is None and COMPLETE.search(x):
            d["complete"] = t
        if d["db_end"] is None and DB_END.search(x):
            d["db_end"] = t
        if RS.search(x):
            d["rs"] = True
    return list(tx.values())


def template(x):
    x = re.sub(r"0x[0-9a-fA-F]+", "P", x)
    x = re.sub(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F-]+", "ID", x)
    return re.sub(r"\d+", "N", x)[:110]


def q(v, p):
    v = sorted(x for x in v if x is not None)
    return v[min(len(v) - 1, int(p * len(v)))] if v else None


def main(rows_log, mozdir):
    rows = {}
    for line in open(rows_log, encoding="utf-8"):
        m = re.match(r"HUNTROW \[(\w+)\] (.*)", line.strip())
        if m:
            rows.setdefault(m.group(1), []).append(json.loads(m.group(2)))
    files = {}
    for path in glob.glob(os.path.join(mozdir, "**", "ff-*"), recursive=True):
        files.setdefault(pid_of(os.path.basename(path)), []).append(path)
    print("HUNTIDBFILES moz_log files %d for %d pids under %s" % (sum(len(v) for v in files.values()), len(files), mozdir))
    sample_done = set()
    per_arm_held, per_arm_open = collections.defaultdict(list), collections.defaultdict(list)
    census = collections.defaultdict(list)
    for arm, rs in sorted(rows.items()):
        for r in rs:
            if "crash" in r:
                continue
            tag = r.get("tag")
            census_rows = r.get("x_census") or []
            kinds = collections.Counter(c["kind"] for c in census_rows)
            census[arm].append((tag, kinds, census_rows))
            quit_t = r["close_wall_ms"] + r["a_close_to_quit_ms"] if r.get("close_wall_ms") is not None and r.get("a_close_to_quit_ms") is not None else None
            exit_t = r["close_wall_ms"] + r["close_to_main_gone_ms"] if r.get("close_wall_ms") is not None and r.get("close_to_main_gone_ms") is not None else None
            pids = [c["pid"] for c in census_rows]
            mine = [(pid, p) for pid in pids for p in files.get(pid, [])]
            lines_all = []
            per_pid = {}
            for pid, path in mine:
                ls = parse(path)
                per_pid[pid] = per_pid.get(pid, 0) + len(ls)
                lines_all += ls
            if arm not in sample_done and lines_all:
                sample_done.add(arm)
                top = collections.Counter(template(x) for _t, _k, _p, _th, x in lines_all).most_common(15)
                for t, k, p, th, x in lines_all[:12]:
                    print("HUNTIDBSAMPLE %s %s pid %d [%s: %s] %s" % (arm, tag, p, k, th, x[:200]))
                for tpl, n in top:
                    print("HUNTIDBTEMPLATE %s %5d %s" % (arm, n, tpl))
            print("HUNTIDBFILES %s %s | processes %d, with a log %d, IndexedDB lines per pid %s | quit at %s, parent exit at %s (ms after the close: %s, %s)"
                  % (arm, tag, len(pids), sum(1 for p in pids if p in files), dict(sorted(per_pid.items())), "known" if quit_t else "not seen", "known" if exit_t else "not seen",
                     r.get("a_close_to_quit_ms"), r.get("close_to_main_gone_ms")))
            txs = transactions(sorted(lines_all))
            bymode = collections.Counter(t["mode"] or "?" for t in txs)
            open_q = [t for t in txs if quit_t and t["start"] <= quit_t and (t["complete"] is None or t["complete"] > quit_t)]
            rw = [t for t in open_q if (t["mode"] or "").startswith("readwrite")]
            held = [(max(t["complete"], t["db_end"] or 0) - quit_t) if t["complete"] else None for t in rw]
            ended_held = [h for h in held if h is not None]
            if rw:
                per_arm_held[arm].append(max(ended_held) if ended_held else None)
            per_arm_open[arm].append(len(rw))
            print("HUNTIDBTX %s %s | transactions %d by mode %s | open at the quit %d (readwrite %d, on the Remote Settings database per the log %d) | readwrite open at quit: completed after the quit at ms %s, never completed in the log %d | log ends at quit%+.0f ms"
                  % (arm, tag, len(txs), dict(bymode), len(open_q), len(rw), sum(1 for t in rw if t["rs"]), sorted(round(h) for h in ended_held)[:12],
                     sum(1 for h in held if h is None), (max((t[0] for t in lines_all), default=0) - quit_t) if quit_t and lines_all else float("nan")))
            for t in rw[:6]:
                print("HUNTIDBRW %s %s pid %d T[%d] id %s mode %s rs %s | open for %.0f ms before the quit | completed %s ms after the quit | %d lines | first: %s"
                      % (arm, tag, t["pid"], t["serial"], t["id"][:12], t["mode"], t["rs"], quit_t - t["start"],
                         round(t["complete"] - quit_t) if t["complete"] else None, t["n"], t["first"][:160]))
            # The Remote Settings client's own view of the same quit (services.settings.loglevel=debug).
            print("HUNTIDBRS %s %s | writes at close %s | busy for ms after the close %s | finish-IDB blocker done at quit+ms %s (after the parent's exit: %s)"
                  % (arm, tag, r.get("rs_write_kinds_at_close"), r.get("rs_busy_lasts_ms"), r.get("rs_idb_done_q_ms"), r.get("rs_idb_done_after_exit")))
    for arm in sorted(per_arm_held):
        print("HUNTIDBHELD %s browsers with a readwrite transaction open at the quit: %d of %d | the last of them completes after the quit at ms p50 %s max %s"
              % (arm, len(per_arm_held[arm]), len(rows.get(arm, [])), q(per_arm_held[arm], .5), max((h for h in per_arm_held[arm] if h is not None), default=None)))
    # The census: the processes of a browser at its close, by type, ours against retail.
    types = sorted({k for a in census.values() for _tag, c, _r in a for k in c})
    for arm, a in sorted(census.items()):
        n = len(a)
        print("HUNTCENSUS %-12s browsers %d | processes per browser p50 %s | by type (p50 per browser, max): %s"
              % (arm, n, q([sum(c.values()) for _t, c, _r in a], .5), {k: (q([c.get(k, 0) for _t, c, _r in a], .5), max(c.get(k, 0) for _t, c, _r in a)) for k in types}))
    for arm, a in sorted(census.items()):
        for tag, c, rr in a[:1]:
            for p in sorted(rr, key=lambda p: (p["kind"], p["created_ms"])):
                print("HUNTCENSUSPROC %-12s %s pid %d ppid %d %-14s born +%.1fs | %s" % (arm, tag, p["pid"], p["ppid"], p["kind"], (p["created_ms"] - min(x["created_ms"] for x in rr)) / 1000,
                                                                                      re.sub(r"-(?:prefsHandle|prefMapHandle|jsInitHandle|parentBuildID|appDir|[A-Za-z]*Handle)\s+\S+", "", p["cmd"])[:230]))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
