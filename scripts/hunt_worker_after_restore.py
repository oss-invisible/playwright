"""Hunt: after a restore from the back-forward cache, does `Page.workerDestroyed` come for the worker, and is
the worker alive or dead when it does?

The scenario is the one of tests/test_a_worker_comes_back_with_its_page_from_the_cache_e2e.py, repeated, with
every protocol event timestamped (monotonic and wall) and, after the restore, the worker asked whether it answers
(postMessage -> reply, 3 s deadline). Nothing is asserted: each repetition writes one record.

    python scripts/hunt_worker_after_restore.py BINARY --out out.json --reps 15 --proc 1 \
        --scenarios top-other-site-location:plain,top-other-site-location:pinger

A scenario is `<where>-<away>-<how>:<arm>`; arm `pinger` makes the page ping the worker every 100 ms and keep the
wall time of each reply, so the replies before and after the `workerDestroyed` tell when the worker stopped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
from _servers import LocalServer  # noqa: E402

STRATEGY_1 = {
    "fission.autostart": True,
    "fission.webContentIsolationStrategy": 1,
    "dom.ipc.processCount.webIsolated": 4,
    "network.dns.localDomains": "a.test,b.test,c.test",
}

OWNER = """<script>
  window.__shown = [];
  window.__werr = [];
  window.__pings = [];
  addEventListener('pageshow', e => window.__shown.push([e.persisted, Date.now()]));
  window.__w = new Worker('/worker.js');
  window.__w.addEventListener('error', e => window.__werr.push(['error', String(e.message), Date.now()]));
  window.__w.addEventListener('messageerror', e => window.__werr.push(['messageerror', Date.now()]));
  window.__probe = () => new Promise(resolve => {
    const t0 = performance.now();
    const timer = setTimeout(() => resolve({timedOut: true, ms: Math.round(performance.now() - t0)}), 3000);
    window.__w.addEventListener('message', function once(e) {
      if (e.data === 'started') return;
      window.__w.removeEventListener('message', once);
      clearTimeout(timer);
      resolve({n: e.data, ms: Math.round(performance.now() - t0)});
    });
    window.__w.postMessage('tick');
  });
  window.__startPinger = () => {
    window.__w.addEventListener('message', e => { if (e.data !== 'started') window.__pings.push([Date.now(), e.data]); });
    window.__pingTimer = setInterval(() => window.__w.postMessage('ping'), 100);
  };
</script>"""
WORKER = "let n = 0; postMessage('started'); onmessage = () => postMessage(++n);"


def make_sites():
    routes = {}

    class Handler(BaseHTTPRequestHandler):
        timeout = 5

        def log_message(self, *args):
            pass

        def do_GET(self):
            path = self.path.split("?")[0]
            if path not in routes:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            ctype, body = routes[path]
            body = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = LocalServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def at(host, path):
        return "http://%s:%d%s" % (host, port, path)

    routes["/worker.js"] = ("text/javascript", WORKER)
    routes["/top-worker"] = ("text/html", "<!doctype html><title>top</title>" + OWNER)
    routes["/frame-worker"] = ("text/html", "<!doctype html><title>frame</title>" + OWNER)
    routes["/top-frame"] = ("text/html", "<!doctype html><title>top</title>"
                            "<script>window.__shown = []; addEventListener('pageshow', e => window.__shown.push(e.persisted));"
                            "</script><iframe id=f src='%s' width=300 height=150></iframe>" % at("b.test", "/frame-worker"))
    routes["/other"] = ("text/html", "<!doctype html><title>other</title><p>other")
    sites = {"top": at("a.test", "/top-worker"), "oop-frame": at("a.test", "/top-frame"),
             "same-site": at("a.test", "/other"), "other-site": at("c.test", "/other")}
    return server, sites


EVENTS = []   # (monotonic, wall, method, brief)
T0 = time.monotonic()


def brief(method, params):
    if method.startswith("Page.worker"):
        return {"workerId": params.get("workerId"), "frameId": params.get("frameId"), "url": params.get("url")}
    if method in ("Page.navigationCommitted", "Page.navigationStarted"):
        return {"navigationId": params.get("navigationId"), "url": params.get("url"), "frameId": params.get("frameId")}
    if method in ("Page.eventFired", "Page.frameDocumentRestored", "Page.frameAttached", "Page.frameDetached"):
        return {k: params.get(k) for k in ("frameId", "name", "parentFrameId") if k in params}
    if method.startswith("Runtime.executionContext"):
        return {"id": params.get("executionContextId")}
    return None


def install_recorder():
    from invisible_core.juggler.connection import EventListeners

    dispatch = EventListeners.dispatch_event

    def record(self, method, params, session):
        if method.startswith(("Page.", "Runtime.executionContext")):
            EVENTS.append((time.monotonic(), time.time(), method, brief(method, params)))
        return dispatch(self, method, params, session)

    EventListeners.dispatch_event = record


def mark(name, extra=None):
    EVENTS.append((time.monotonic(), time.time(), "MARK " + name, extra))


def wait(check, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.05)
    return bool(check())


def find(start, method, pred=lambda b: True):
    for i in range(start, len(EVENTS)):
        if EVENTS[i][2] == method and pred(EVENTS[i][3] or {}):
            return i
    return None


def target_of(page, where):
    return page if where == "top" else next(f for f in page.frames if f.url.endswith("/frame-worker"))


def snapshot(target):
    try:
        return target.evaluate(
            "() => ({shown: window.__shown, werr: window.__werr, readyState: document.readyState,"
            " visibility: document.visibilityState, hasWorker: !!window.__w,"
            " nav: (performance.getEntriesByType('navigation')[0] || {}).type})")
    except Exception as exc:  # noqa: BLE001
        return {"error": repr(exc)[:200]}


def probe(target):
    try:
        return target.evaluate("() => window.__probe()")
    except Exception as exc:  # noqa: BLE001
        return {"error": repr(exc)[:200]}


def one(binary, scenario, arm, rep, proc):
    from invisible_playwright import InvisiblePlaywright

    parts = scenario.split("-")
    if parts[0] == "oop":
        where, rest = "oop-frame", parts[2:]
    else:
        where, rest = parts[0], parts[1:]
    away = "-".join(rest[:-1])
    how = rest[-1]

    EVENTS.clear()
    rec = {"scenario": scenario, "arm": arm, "rep": rep, "proc": proc, "where": where, "away": away, "how": how}
    server, sites = make_sites()
    try:
        with InvisiblePlaywright(seed=42, binary_path=binary, humanize=False, headless=True,
                                 extra_prefs=STRATEGY_1) as browser:
            page = browser.new_context().new_page()
            page.set_default_timeout(10000)
            mark("goto-first")
            page.goto(sites[where], wait_until="load")
            if not wait(lambda: find(0, "Page.workerCreated", lambda b: (b.get("url") or "").endswith("/worker.js")) is not None):
                rec["error"] = "the worker was never announced"
                return rec
            wid = EVENTS[find(0, "Page.workerCreated", lambda b: (b.get("url") or "").endswith("/worker.js"))][3]["workerId"]
            rec["worker_id"] = wid
            if where == "oop-frame":
                wait(lambda: any(f.url.endswith("/frame-worker") for f in page.frames))
            tgt = target_of(page, where)
            first = probe(tgt), probe(tgt)
            rec["first_probes"] = first
            if arm == "pinger":
                tgt.evaluate("() => window.__startPinger()")
                time.sleep(0.5)

            fwd = len(EVENTS)
            mark("forward-start")
            if how == "goto":
                page.goto(sites[away], wait_until="load")
            else:
                with page.expect_navigation(wait_until="load"):
                    page.evaluate("url => { location.href = url; }", sites[away])
            mark("forward-done")
            committed = find(fwd, "Page.navigationCommitted", lambda b: (b.get("url") or "").endswith("/other"))
            ended = find(fwd, "Page.workerDestroyed", lambda b: b.get("workerId") == wid)
            rec["forward"] = {"committed": committed is not None, "ended": ended is not None,
                              "ended_before_commit": (ended is not None and committed is not None and ended < committed)}

            back = len(EVENTS)
            mark("go_back-start")
            page.go_back(wait_until="load")
            mark("go_back-return")
            rec["back_index"] = back
            tgt_back = page
            shown = page.evaluate("window.__shown")
            rec["shown_top"] = shown
            rec["restored_top"] = ([s[0] if isinstance(s, list) else s for s in shown] == [False, True]) if shown else None
            announced = wait(lambda: find(back, "Page.workerCreated", lambda b: b.get("workerId") == wid) is not None)
            rec["announced_again"] = announced
            # what comes in excess comes now (the test sleeps 1.0 s)
            time.sleep(1.0)
            mark("after-1s")
            destroyed_i = find(back, "Page.workerDestroyed", lambda b: b.get("workerId") == wid)
            rec["destroyed_before_probe"] = destroyed_i is not None
            if where == "oop-frame":
                try:
                    tgt_back = target_of(page, where)
                except StopIteration:
                    tgt_back = None
            if tgt_back is not None:
                rec["snapshot_1"] = snapshot(tgt_back)
                mark("probe1-start")
                rec["probe_1"] = probe(tgt_back)
                mark("probe1-end")
                time.sleep(1.5)
                rec["snapshot_2"] = snapshot(tgt_back)
                mark("probe2-start")
                rec["probe_2"] = probe(tgt_back)
                mark("probe2-end")
                if arm == "pinger":
                    try:
                        rec["pings"] = tgt_back.evaluate("() => window.__pings")
                    except Exception as exc:  # noqa: BLE001
                        rec["pings_error"] = repr(exc)[:200]
            time.sleep(0.5)
            mark("end")
            rec["destroyed_total_after_back"] = len([1 for e in EVENTS[back:] if e[2] == "Page.workerDestroyed"
                                                      and (e[3] or {}).get("workerId") == wid])
            rec["created_total_after_back"] = len([1 for e in EVENTS[back:] if e[2] == "Page.workerCreated"
                                                    and (e[3] or {}).get("workerId") == wid])
            rec["other_worker_ids_after_back"] = sorted({(e[3] or {}).get("workerId") for e in EVENTS[back:]
                                                          if e[2] == "Page.workerCreated"} - {wid})
            rec["destroyed_after_restore"] = rec["destroyed_total_after_back"] > 0
    except Exception as exc:  # noqa: BLE001
        rec["error"] = repr(exc)[:300]
        rec["trace"] = traceback.format_exc()[-1500:]
    finally:
        try:
            server.shutdown()
            server.server_close()
        except Exception:  # noqa: BLE001
            pass
    base = EVENTS[0][0] if EVENTS else 0
    rec["events"] = [[round((t - base) * 1000, 1), round(w * 1000), m, b] for t, w, m, b in EVENTS]
    return rec


def verdict_of(r):
    """alive / dead / unknown for a repetition in which the worker was told ended after the restore."""
    p1 = r.get("probe_1") or {}
    destroyed = [e for e in r.get("events", []) if e[2] == "Page.workerDestroyed"
                 and (e[3] or {}).get("workerId") == r.get("worker_id") and e[0] >= 0]
    back = [e for e in r.get("events", []) if e[2] == "MARK go_back-start"]
    if back:
        destroyed = [e for e in destroyed if e[0] >= back[0][0]]
    if "n" in p1:
        alive_by_probe = True
    elif p1.get("timedOut"):
        alive_by_probe = False
    else:
        alive_by_probe = None
    pings_after = None
    if r.get("pings") is not None and destroyed:
        wall = destroyed[0][1]
        pings_after = len([1 for t, _ in r["pings"] if t > wall + 50])
    return {"probe": alive_by_probe, "pings_after_destroyed": pings_after}


def summarize(files, out):
    rows, metas = [], []
    for f in files:
        d = json.loads(Path(f).read_text())
        metas.append(d["meta"])
        rows.extend(d["results"])
    groups = {}
    for r in rows:
        groups.setdefault("%s:%s" % (r["scenario"], r["arm"]), []).append(r)
    summary = {}
    for key, rs in sorted(groups.items()):
        ok = [r for r in rs if not r.get("error")]
        restored = [r for r in ok if r.get("restored_top") in (True, None)]
        destroyed = [r for r in ok if r.get("destroyed_after_restore")]
        verdicts = [verdict_of(r) for r in destroyed]
        entry = {"reps": len(rs), "errors": len(rs) - len(ok), "not_announced_again": len([r for r in ok if not r.get("announced_again")]),
                 "restored_or_frame": len(restored), "destroyed_after_restore": len(destroyed),
                 "destroyed_worker_answers": len([v for v in verdicts if v["probe"] is True]),
                 "destroyed_worker_silent": len([v for v in verdicts if v["probe"] is False]),
                 "destroyed_with_pings_after": len([v for v in verdicts if v["pings_after_destroyed"]]),
                 "all_probes_ok": len([r for r in ok if "n" in (r.get("probe_1") or {})]),
                 "destroyed_before_forward_commit_missing": len([r for r in ok if not (r.get("forward") or {}).get("ended_before_commit")]),
                 "error_samples": sorted({(r.get("error") or "")[:100] for r in rs if r.get("error")})[:3]}
        summary[key] = entry
        print("HUNTSUMMARY %s %s" % (key, json.dumps(entry)))
    # The question of the hunt, over all the scenarios: of the pages restored from the back-forward cache after an away navigation to another
    # SITE (with site isolation on, the page's process changes: a.test -> c.test), in how many was the restored worker told ended.
    ok = [r for r in rows if not r.get("error")]
    switch = [r for r in ok if r.get("away") == "other-site" and r.get("restored_top") is True]
    switch_destroyed = [r for r in switch if r.get("destroyed_after_restore")]
    totals = {"launches": len(rows), "errors": len(rows) - len(ok),
              "restores_with_process_switch": len(switch), "workerDestroyed_after_restore_with_process_switch": len(switch_destroyed),
              "restores_all": len([r for r in ok if r.get("restored_top") is True]),
              "workerDestroyed_after_restore_all": len([r for r in ok if r.get("restored_top") is True and r.get("destroyed_after_restore")])}
    print("HUNTTOTAL %s (expected workerDestroyed 0)" % json.dumps(totals))
    if out:
        Path(out).write_text(json.dumps({"metas": metas, "summary": summary, "totals": totals, "results": rows}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("binary", nargs="?", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--reps", type=int, default=15)
    ap.add_argument("--proc", type=int, default=1)
    ap.add_argument("--scenarios", default="")
    ap.add_argument("--deadline-minutes", type=float, default=0, help="start no new repetition after this long (0: no limit)")
    ap.add_argument("--summarize", nargs="*", help="part files to merge and summarise (no browser)")
    ap.add_argument("--summary-out", default="")
    args = ap.parse_args()
    if args.summarize is not None:
        return summarize(args.summarize, args.summary_out)

    install_recorder()
    from invisible_core.seal import active_seal
    meta = {"os": platform.platform(), "python": sys.version, "proc": args.proc, "seal_tag": active_seal().tag,
            "binary": args.binary, "started_wall": time.time()}
    try:
        meta["binary_sha256"] = hashlib.sha256(Path(args.binary).read_bytes()).hexdigest()
    except OSError as exc:
        meta["binary_sha256_error"] = repr(exc)
    results = []
    plan = [tuple(s.split(":")) for s in args.scenarios.split(",")]
    started = time.monotonic()
    for rep in range(1, args.reps + 1):
        if args.deadline_minutes and (time.monotonic() - started) > args.deadline_minutes * 60:
            print("HUNTROW proc=%d stops before rep %d: the deadline" % (args.proc, rep), flush=True)
            break
        for scenario, arm in plan:
            r = one(args.binary, scenario, arm, rep, args.proc)
            results.append(r)
            print("HUNTROW proc=%d rep=%d %s:%s destroyed_after_restore=%s probe1=%s probe2=%s err=%s" % (
                args.proc, rep, scenario, arm, r.get("destroyed_after_restore"),
                json.dumps(r.get("probe_1")), json.dumps(r.get("probe_2")), r.get("error")), flush=True)
            Path(args.out).write_text(json.dumps({"meta": meta, "results": results}))
    meta["finished_wall"] = time.time()
    Path(args.out).write_text(json.dumps({"meta": meta, "results": results}))


if __name__ == "__main__":
    main()
