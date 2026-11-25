"""HUNT, not a test of the suites: the double `mouseover` of `handle.click()`, seen once on Windows.

The e2e `tests/test_element_handle_actions.py::test_the_nine_actions_reach_the_page_through_a_handle` went red once on the Windows leg of an engine
candidate with the page's record `['hover', 'hover', 'click']` where it expects `['hover', 'click']` first. Two readings of that:

* an EXIT AND RE-ENTRY of the pointer (a `mouseout` between the two `mouseover`): the pointer left the button and came back, which a human
  does and a page may count;
* a SPURIOUS second `mouseover` (none between): the engine told the page the pointer entered twice, which no pointer does.

This file runs the same scenario (same page, same launch, same call) with the page listening to everything that tells them apart, and the core's
retry loop hooked (the core logs nothing of its turns), and writes ONE JSON line per run with a verdict:

    single        [mouseover, click]
    exit-reentry  two or more mouseover before the first click, a mouseout between each pair
    spurious      two mouseover before the first click with no mouseout between them
    other         no mouseover before the click, a mouseover then a mouseout and no new one, no click...

It is named `hunt_*` and not `test_*` on purpose: no suite collects it (`ci/required-suites.json` names files, and the e2e accounting collects
`test_*`). The hunt workflow runs it by its path, once with no xdist and once with `-n 4 --dist load` as the suite does:

    HUNT_MODE=sequential|parallel  HUNT_RUNS=60  HUNT_OUT=<dir>  pytest tests/hunt_handle_double_hover_e2e.py ...

A run that finds a defect PASSES (the finding is the point); a run whose scenario errors (a launch that fails, a page that never loads, a click that
raises) writes verdict `error` and FAILS, so that the job says the measurement is not what it was meant to be.
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import threading
import time
import traceback
from pathlib import Path

import pytest

from _servers import LocalServer

pytestmark = pytest.mark.e2e

MODE = os.environ.get("HUNT_MODE", "sequential")
RUNS = int(os.environ.get("HUNT_RUNS", "60"))
OUT = Path(os.environ.get("HUNT_OUT", "hunt-out"))

# The page of tests/test_element_handle_actions.py, as it is there (the button and the record the test reads), and below it the listeners of the hunt.
PAGE = b"""<!doctype html>
<html><head><title>handle actions</title></head><body>
  <input id="testo" type="text">
  <input id="daScrivere" type="text">
  <button id="bottone" type="button">press me</button>
  <input id="casella" type="checkbox">
  <select id="tendina">
    <option value="a">Alpha</option><option value="b">Beta</option>
  </select>
  <div id="passaggio">no</div>
<script>
  window.registro = {click: 0, ordine: [], change: [], tasti: []};
  const b = document.getElementById('bottone');
  b.addEventListener('click', e => {
    window.registro.click++; window.registro.fidato = e.isTrusted;
    window.registro.ordine.push('click');
  });
  b.addEventListener('mouseover', () => window.registro.ordine.push('hover'));
  window.registro.movimenti = 0;
  b.addEventListener('mousemove', () => window.registro.movimenti++);
  for (const id of ['casella','tendina'])
    document.getElementById(id).addEventListener('change',
      () => window.registro.change.push(id));
  document.getElementById('daScrivere').addEventListener('keydown',
    e => window.registro.tasti.push(e.key));
</script>
<script>
  // THE HUNT: everything a page can see of the pointer around the button, with the rect of the button at each moment.
  (() => {
    const b = document.getElementById('bottone');
    const H = window.hunt = {ev: [], moves: [], dmoves: [], rects: [], ro: [], shifts: [], frames: 0};
    const epoch = e => performance.timeOrigin + e.timeStamp;
    const rect = () => { const r = b.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; };
    const rel = e => e.relatedTarget ? {id: e.relatedTarget.id || null, tag: e.relatedTarget.tagName} : null;
    for (const type of ['mouseover', 'mouseout', 'mouseenter', 'mouseleave', 'click'])
      b.addEventListener(type, e => H.ev.push({type, rel: rel(e), x: e.clientX, y: e.clientY, ts: e.timeStamp, t: epoch(e),
                                               trusted: e.isTrusted, rect: rect()}));
    b.addEventListener('mousemove', e => H.moves.push([e.clientX, e.clientY, e.timeStamp, epoch(e)]));
    document.addEventListener('mousemove', e => {
      if (H.dmoves.length < 3000) H.dmoves.push([e.clientX, e.clientY, e.target.id || e.target.tagName, e.timeStamp]);
    }, true);
    // what happens to the pointer OFF the button (the body it leaves to, the field next to it)
    for (const type of ['mouseover', 'mouseout'])
      document.addEventListener(type, e => {
        if (e.target !== b) H.ev.push({type: 'doc-' + type, target: e.target.id || e.target.tagName, rel: rel(e),
                                       x: e.clientX, y: e.clientY, ts: e.timeStamp, t: epoch(e)});
      }, true);
    try {
      new ResizeObserver(es => es.forEach(en => H.ro.push([en.contentRect.width, en.contentRect.height,
                                                          performance.timeOrigin + performance.now()]))).observe(b);
    } catch (e) { H.roError = String(e); }
    try {
      new PerformanceObserver(l => l.getEntries().forEach(en => H.shifts.push([en.value, en.startTime]))).observe({type: 'layout-shift', buffered: true});
    } catch (e) { H.shiftsError = String(e); }
    let last = rect().join();
    const tick = () => {
      H.frames++;
      const now = rect().join();
      if (now !== last) { H.rects.push({t: performance.timeOrigin + performance.now(), now, was: last}); last = now; }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
    H.snap = () => ({rect: rect(), vw: innerWidth, vh: innerHeight, scrollY: scrollY, dpr: devicePixelRatio,
                     focus: document.hasFocus(), visibility: document.visibilityState,
                     t: performance.timeOrigin + performance.now()});
  })();
</script>
</body></html>"""


def judge(events):
    """The verdict of one run, from the page's events in order (a pure function: tests/unit would be where it is tested, it is here with the hunt).

    Only what precedes the FIRST click counts: `mouseover`, `mouseout`, `click` of the button. See the module docstring for the four words."""
    short = {"mouseover": "o", "mouseout": "x", "click": "c"}
    seq = []
    for e in events:
        if e["type"] in short:
            seq.append(short[e["type"]])
            if e["type"] == "click":
                break
    word = "".join(seq)
    if not word.endswith("c"):
        return "other", word
    before = word[:-1]
    if before == "o":
        return "single", word
    if before.count("o") >= 2:
        overs = [i for i, ch in enumerate(before) if ch == "o"]
        for first, second in zip(overs, overs[1:]):
            if "x" not in before[first:second]:
                return "spurious", word
        return "exit-reentry", word
    return "other", word


# ── the core's retry loop, hooked (it logs nothing of its turns) ────────────────────────────────────────────────────────
class _Recorder:
    def __init__(self):
        self.mouse = []        # every pointer event the core sent: the client side of the sequence the page records
        self.retries = []      # one per `_retry`: turns, runs, hit-target verdicts, what each `_act_on_target` ended with


REC = _Recorder()
_hooked = False


def _now_ms():
    return time.time() * 1000.0


def _install_hooks():
    """Class-level wrappers on `invisible_core.juggler.actions.Actions`, once per process. They record and call the original with the same
    arguments: nothing is changed. A run where `mouse` stays empty says the hooks never saw the call (`hooks_live` false in its line)."""
    global _hooked
    if _hooked:
        return
    from invisible_core.juggler import actions as A

    cls = A.Actions
    mouse, retry, center, hit, act = (cls._mouse_event, cls._retry, cls._center_point, cls.hit_target, cls._act_on_target)

    def _mouse_event(self, event_type, point, *a, **kw):
        REC.mouse.append({"t": _now_ms(), "type": event_type, "x": point[0], "y": point[1], "buttons": kw.get("buttons"),
                          "at_ms": kw.get("at_ms"), "retry": len(REC.retries)})
        return mouse(self, event_type, point, *a, **kw)

    def _retry(self, selector, run, *a, **kw):
        turn = {"selector": str(selector)[:60], "start": _now_ms(), "center_point_calls": 0, "run_calls": 0, "act_on_target_calls": 0,
                "hit_verdicts": [], "exceptions": [], "end": None}
        REC.retries.append(turn)

        def counted(f, el, pt):
            turn["run_calls"] += 1
            return run(f, el, pt)
        try:
            return retry(self, selector, counted, *a, **kw)
        finally:
            turn["end"] = _now_ms()

    def _center_point(self, *a, **kw):
        if REC.retries:
            REC.retries[-1]["center_point_calls"] += 1
        return center(self, *a, **kw)

    def hit_target(self, f, element, point):
        verdict = hit(self, f, element, point)
        if REC.retries:
            REC.retries[-1]["hit_verdicts"].append([_now_ms(), verdict])
        return verdict

    def _act_on_target(self, *a, **kw):
        if REC.retries:
            REC.retries[-1]["act_on_target_calls"] += 1
        try:
            return act(self, *a, **kw)
        except BaseException as exc:
            if REC.retries:
                REC.retries[-1]["exceptions"].append([_now_ms(), type(exc).__name__, str(exc)[:200]])
            raise

    cls._mouse_event, cls._retry, cls._center_point, cls.hit_target, cls._act_on_target = (
        _mouse_event, _retry, _center_point, hit_target, _act_on_target)
    _hooked = True


def _serve():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)

        def log_message(self, *a):
            pass

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = LocalServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/" % port


def _write(index, record):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / ("run-%s-%03d.json" % (MODE, index))
    path.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")


@pytest.mark.parametrize("index", range(RUNS))
def test_hunt_the_first_click_of_a_handle(index, firefox_binary):
    """One fresh browser, the first `button.click()` of the test, and the page's whole account of it."""
    from invisible_playwright import InvisiblePlaywright

    _install_hooks()
    REC.mouse.clear()
    REC.retries.clear()
    record = {"mode": MODE, "index": index, "pid": os.getpid(), "worker": os.environ.get("PYTEST_XDIST_WORKER", "main")}
    srv, url = _serve()
    try:
        with InvisiblePlaywright(seed=4242, binary_path=firefox_binary, headless=True) as browser:
            page = browser.new_context().new_page()
            page.goto(url, wait_until="load", timeout=30_000)
            button = page.query_selector("#bottone")
            t0 = _now_ms()
            button.click()
            t1 = _now_ms()
            # nothing is asked of the page before the click: the scenario is the test's (goto, query_selector, click)
            after = page.evaluate("() => { const h = window.hunt; return {hunt: {ev: h.ev, moves: h.moves, dmoves: h.dmoves, rects: h.rects, "
                                  "ro: h.ro, shifts: h.shifts, frames: h.frames}, registro: window.registro, snap: h.snap()}; }")
        hunt = after["hunt"]
        verdict, word = judge(hunt["ev"])
        turns = [r["center_point_calls"] for r in REC.retries]
        record.update({
            "ok": True, "verdict": verdict, "word": word,
            "ordine": after["registro"]["ordine"], "clicks": after["registro"]["click"], "trusted": after["registro"].get("fidato"),
            "test_rule_passes": after["registro"]["ordine"][:2] == ["hover", "click"],
            "click_ms": round(t1 - t0, 1),
            "events": hunt["ev"], "moves": hunt["moves"], "doc_moves": hunt["dmoves"],
            "rect_changes": hunt["rects"], "resize": hunt["ro"], "layout_shifts": hunt["shifts"], "frames": hunt["frames"],
            "snap_after_click": after["snap"],
            "hooks_live": bool(REC.mouse), "core_mouse": REC.mouse, "retries": REC.retries,
            "turns": turns[0] if turns else None,
            "run_calls": REC.retries[0]["run_calls"] if REC.retries else None,
            "wrong_hit_target": sum(1 for r in REC.retries for x in r["exceptions"] if x[1] == "WrongHitTarget"),
        })
        _write(index, record)
    except BaseException as exc:
        record.update({"ok": False, "verdict": "error", "error": "%s: %s" % (type(exc).__name__, exc),
                       "traceback": traceback.format_exc()[-3000:], "core_mouse": REC.mouse, "retries": REC.retries})
        _write(index, record)
        raise
    finally:
        srv.shutdown()
