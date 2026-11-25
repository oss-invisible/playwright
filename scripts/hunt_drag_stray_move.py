"""Evidence for the drag-stray-move hunt (.github/workflows/drag-stray-move-hunt.yml).

    python scripts/hunt_drag_stray_move.py run --ref-dir ref --label <ref> --shard 1 --iterations 25 --out evidence
    python scripts/hunt_drag_stray_move.py summary evidence

The question: in tests/test_drag_motion.py::test_a_real_page_sees_a_drag_that_travelled the page once saw, after the
press, mousemove events with buttons=1 and then ``mousemove 512,299 buttons=0``. Is that stray event born in the
wrapper (the change that merged as 46054486) or in the engine, which during the native drag (OLE DoDragDrop on
Windows) may synthesize a mousemove at the OS cursor position with no button?

What one iteration does, in a fresh browser each time:

* the scenario of that test, byte for byte (same page, same seed 42, same calls), with listeners that record every
  mouse, pointer and drag event with clientX/Y, screenX/Y, buttons, isTrusted, timeStamp and the page's wall clock;
* the window geometry and the screen size the page reads, before and after;
* OUTSIDE the browser, on the runner, the real OS cursor position (GetCursorPos) before and after the drag and,
  from a separate process, every change of it during the drag with its wall time;
* the assertions of the test, with the helper of the ref under test imported from its own tests/ folder, so
  ``instrumented.assertions_failed`` means what a failure of the test means;
* then the ORIGINAL test, run by pytest from the ref's own tests/ folder, so the verdict of the test itself is
  recorded next to the evidence.

One JSON line per iteration goes to ``<out>/iterations-<label>-<shard>.jsonl``. Nothing here fails the job on a
finding: only a scenario that errors is an error (the line says so).
"""
from __future__ import annotations

import argparse
import ctypes
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

NODE = "tests/test_drag_motion.py::test_a_real_page_sees_a_drag_that_travelled"

#: the page of the test, plus passive observers of everything else
HTML = (
    "<style>body{margin:0}"
    "#a{position:absolute;left:40px;top:40px;width:120px;height:80px}"
    "#b{position:absolute;left:520px;top:380px;width:120px;height:80px}"
    "</style>"
    "<div id='a' draggable='true'>source</div><div id='b'>target</div>"
    "<script>window.__e=[];window.__d=[];window.__full=[];"
    # the test's own listeners (these are what the assertions read)
    "for (const t of ['mousemove','mousedown','mouseup'])"
    " document.addEventListener(t,"
    "  e => window.__e.push([t, e.clientX, e.clientY, e.buttons]), true);"
    "for (const t of ['dragstart','dragover','drop','dragend'])"
    " document.addEventListener(t, e => {"
    "  window.__d.push(t);"
    "  if (t === 'dragover')"
    "   window.__e.push([t, e.clientX, e.clientY, e.buttons]);"
    "  if (t !== 'dragstart') e.preventDefault(); },"
    "  true);"
    # the observers: they never stop or change anything
    "for (const t of ['mousemove','mousedown','mouseup','click','pointermove','pointerdown','pointerup',"
    "                 'drag','dragstart','dragenter','dragover','dragleave','drop','dragend'])"
    " document.addEventListener(t, e => window.__full.push({"
    "  type: t, clientX: e.clientX, clientY: e.clientY, screenX: e.screenX, screenY: e.screenY,"
    "  buttons: e.buttons, isTrusted: e.isTrusted, timeStamp: e.timeStamp, wall: Date.now(),"
    "  perf: performance.now(), target: e.target && (e.target.id || e.target.nodeName),"
    "  pointerType: e.pointerType || null}), true);"
    "</script>")

GEOMETRY_JS = (
    "() => ({screenX: window.screenX, screenY: window.screenY,"
    " outerWidth: window.outerWidth, outerHeight: window.outerHeight,"
    " innerWidth: window.innerWidth, innerHeight: window.innerHeight,"
    " screenWidth: screen.width, screenHeight: screen.height,"
    " availWidth: screen.availWidth, availHeight: screen.availHeight,"
    " dpr: window.devicePixelRatio, wall: Date.now()})")

#: polls the OS cursor from its own process and writes every change, flushed, with the wall time in ms
SAMPLER = (
    "import ctypes, sys, time\n"
    "class P(ctypes.Structure):\n"
    "    _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]\n"
    "out = open(sys.argv[1], 'w')\n"
    "last = None\n"
    "while True:\n"
    "    p = P()\n"
    "    ok = ctypes.windll.user32.GetCursorPos(ctypes.byref(p))\n"
    "    cur = (p.x, p.y) if ok else None\n"
    "    if cur != last:\n"
    "        out.write('%d %s\\n' % (int(time.time() * 1000), 'none' if cur is None else '%d %d' % cur))\n"
    "        out.flush()\n"
    "        last = cur\n"
    "    time.sleep(0.002)\n")


def os_cursor():
    """The real OS cursor position of the runner, read from outside the browser."""
    if sys.platform != "win32":
        return None

    class P(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    p = P()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(p)):
        return None
    return {"x": p.x, "y": p.y, "wall": int(time.time() * 1000)}


def _start_sampler(path):
    if sys.platform != "win32":
        return None
    return subprocess.Popen([sys.executable, "-I", "-c", SAMPLER, str(path)])


def _read_samples(path):
    out = []
    try:
        for line in Path(path).read_text().splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[1] != "none":
                out.append({"wall": int(parts[0]), "x": int(parts[1]), "y": int(parts[2])})
            elif len(parts) == 2:
                out.append({"wall": int(parts[0]), "x": None, "y": None})
    except OSError:
        pass
    return out


def _load_ref_helper(ref_dir):
    """`_no_event_carried_the_journey` of the ref under test, from its own tests/ folder."""
    path = Path(ref_dir) / "tests" / "test_drag_motion.py"
    spec = importlib.util.spec_from_file_location("ref_test_drag_motion", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._no_event_carried_the_journey


def _assertions(events, dragged, afterwards, journey_check):
    """The assertions of the test, in its order. Returns the list of messages that failed."""
    failed = []
    if "dragend" not in dragged:
        failed.append("no dragend, the gesture did not complete: %s" % (dragged or "nothing"))
    if not (afterwards and all(e[3] == 0 for e in afterwards)):
        failed.append("the button is still down after the drag: %s" % afterwards[:3])
    kinds = [e[0] for e in events]
    if "mousedown" not in kinds:
        failed.append("the press never reached the page")
        return failed
    press = kinds.index("mousedown")
    journey = [e for e in events[press + 1:] if e[0] in ("mousemove", "dragover")]
    if len(journey) <= 5:
        failed.append("the whole journey took %d events: %s" % (len(journey), kinds))
    else:
        try:
            journey_check([(e[1], e[2]) for e in journey])
        except AssertionError as exc:
            failed.append("journey: %s" % exc)
    after_press = [e for e in events[press + 1:] if e[0] == "mousemove"]
    if not (after_press and all(e[3] == 1 for e in after_press)):
        failed.append("a movement after the press reports the button up: %s" % after_press[:3])
    return failed


def instrumented(binary, journey_check, tmp):
    from invisible_playwright import InvisiblePlaywright

    rec = {"error": None}
    sampler_file = Path(tmp) / "cursor-samples.txt"
    sampler = _start_sampler(sampler_file)
    try:
        time.sleep(0.3)  # let the sampler write its first position
        rec["os_cursor_start"] = os_cursor()
        with InvisiblePlaywright(seed=42, binary_path=binary) as browser:
            page = browser.new_page()
            page.goto("data:text/html," + urllib.parse.quote(HTML))
            rec["geometry_before"] = page.evaluate(GEOMETRY_JS)
            page.mouse.move(300, 300)
            page.evaluate("() => { window.__e = []; window.__d = []; window.__full = []; }")
            rec["os_cursor_before"] = os_cursor()
            rec["wall_before_drag"] = int(time.time() * 1000)
            page.drag_and_drop("#a", "#b")
            rec["wall_after_drag"] = int(time.time() * 1000)
            rec["os_cursor_after"] = os_cursor()
            events = page.evaluate("window.__e")
            dragged = page.evaluate("window.__d")
            full = page.evaluate("window.__full")
            rec["geometry_after"] = page.evaluate(GEOMETRY_JS)
            page.evaluate("() => { window.__e = []; }")
            page.mouse.move(700, 120)
            afterwards = page.evaluate("window.__e")
        rec["events"] = full
        rec["test_events"] = events
        rec["dragged"] = dragged
        rec["afterwards"] = afterwards
        rec["assertions_failed"] = _assertions(events, dragged, afterwards, journey_check)
    except Exception as exc:  # the scenario itself broke: recorded, not a finding
        rec["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:500])
        rec.setdefault("assertions_failed", None)
    finally:
        time.sleep(0.1)
        if sampler is not None:
            sampler.kill()
            sampler.wait()
        rec["os_cursor_samples"] = _read_samples(sampler_file)
    rec["stray"] = find_stray(rec)
    return rec


def find_stray(rec):
    """The `mousemove` events after the press that report no button (what the test calls a lost press), and
    every trusted mouse/pointer move with buttons=0 after the press, whatever the test thinks of it."""
    full = rec.get("events") or []
    press = next((e for e in full if e["type"] == "mousedown"), None)
    if press is None:
        return []
    cur = rec.get("os_cursor_before") or {}
    out = []
    for e in full:
        if e["wall"] < press["wall"] or e is press:
            continue
        if e["type"] in ("mousemove", "pointermove") and e["buttons"] == 0:
            item = dict(e)
            item["ms_after_press"] = e["wall"] - press["wall"]
            item["os_cursor_before"] = cur
            item["screen_equals_os_cursor_before"] = (
                cur.get("x") is not None
                and abs(e["screenX"] - cur["x"]) <= 1 and abs(e["screenY"] - cur["y"]) <= 1)
            out.append(item)
    return out


def original(ref_dir, binary, out_dir, label, shard, n):
    """The original test, pytest from the ref's own tests/ folder, as the e2e jobs run it."""
    env = dict(os.environ, INVPW_BINARY_PATH=binary, STEALTHFOX_E2E_BINARY=binary, PYTHONUNBUFFERED="1")
    env.pop("PYTEST_ADDOPTS", None)
    log = Path(out_dir) / "original-%s-%s-%02d.log" % (label, shard, n)
    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", NODE, "-m", "e2e", "-o", "addopts=", "-p", "no:rerunfailures",
             "-p", "no:cacheprovider", "-q", "-rs", "--no-header"],
            cwd=str(ref_dir), env=env, capture_output=True, text=True, timeout=240)
        text, rc = (proc.stdout or "") + (proc.stderr or ""), proc.returncode
    except subprocess.TimeoutExpired as exc:
        text, rc = "TIMEOUT after 240 s\n%s" % (exc.stdout or b"", ), 124
    log.write_text(text, encoding="utf-8", errors="replace")
    outcome = "passed" if rc == 0 and " passed" in text else ("skipped" if " skipped" in text and rc == 0 else "failed")
    return {"rc": rc, "outcome": outcome, "seconds": round(time.time() - t0, 1),
            "tail": "\n".join(text.splitlines()[-40:]) if outcome != "passed" else ""}


def cmd_run(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    binary = os.environ["INVPW_BINARY_PATH"]
    journey_check = _load_ref_helper(args.ref_dir)
    jsonl = out / ("iterations-%s-%s.jsonl" % (args.label, args.shard))
    for n in range(1, args.iterations + 1):
        with tempfile.TemporaryDirectory() as tmp:
            line = {"ref": args.label, "shard": args.shard, "iteration": n, "seed": 42}
            line["instrumented"] = instrumented(binary, journey_check, tmp)
        line["original"] = original(args.ref_dir, binary, out, args.label, args.shard, n)
        with open(jsonl, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
        ins = line["instrumented"]
        print("iteration %02d: instrumented=%s original=%s stray=%d error=%s" % (
            n, "ERROR" if ins["error"] else ("fail" if ins["assertions_failed"] else "pass"),
            line["original"]["outcome"], len(ins["stray"]), ins["error"]), flush=True)
    return 0


def _lines(directory):
    for p in sorted(Path(directory).rglob("iterations-*.jsonl")):
        for raw in p.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                yield json.loads(raw)


def cmd_summary(args):
    rows = {}
    strays = []
    for line in _lines(args.directory):
        ins, orig = line["instrumented"], line["original"]
        r = rows.setdefault(line["ref"], {"iterations": 0, "ins_pass": 0, "ins_fail": 0, "ins_error": 0,
                                          "orig_pass": 0, "orig_fail": 0, "stray_events": 0, "iters_with_stray": 0})
        r["iterations"] += 1
        if ins["error"]:
            r["ins_error"] += 1
        elif ins["assertions_failed"]:
            r["ins_fail"] += 1
        else:
            r["ins_pass"] += 1
        r["orig_pass" if orig["outcome"] == "passed" else "orig_fail"] += 1
        r["stray_events"] += len(ins["stray"])
        r["iters_with_stray"] += 1 if ins["stray"] else 0
        for s in ins["stray"]:
            strays.append((line, s))
    head = ("ref", "iterations", "ins_pass", "ins_fail", "ins_error", "orig_pass", "orig_fail", "stray_events",
            "iters_with_stray")
    print("| " + " | ".join(head) + " |")
    print("|" + "---|" * len(head))
    for ref, r in sorted(rows.items()):
        print("| %s | " % ref + " | ".join(str(r[k]) for k in head[1:]) + " |")
    print()
    print("stray events (a mousemove/pointermove with buttons=0 after the press):")
    if not strays:
        print("  none")
    for line, s in strays:
        ins = line["instrumented"]
        print("  ref=%s shard=%s it=%02d %s client=(%s,%s) screen=(%s,%s) buttons=%s trusted=%s +%d ms after press; "
              "os cursor before=%s after=%s; screen==os_before: %s; window=%s inner=%sx%s screen=%sx%s" % (
                  line["ref"], line["shard"], line["iteration"], s["type"], s["clientX"], s["clientY"],
                  s["screenX"], s["screenY"], s["buttons"], s["isTrusted"], s["ms_after_press"],
                  _xy(ins.get("os_cursor_before")), _xy(ins.get("os_cursor_after")),
                  s["screen_equals_os_cursor_before"],
                  (ins.get("geometry_before") or {}).get("screenX"),
                  (ins.get("geometry_before") or {}).get("innerWidth"),
                  (ins.get("geometry_before") or {}).get("innerHeight"),
                  (ins.get("geometry_before") or {}).get("screenWidth"),
                  (ins.get("geometry_before") or {}).get("screenHeight")))
    return 0


def _xy(c):
    return "none" if not c or c.get("x") is None else "(%s,%s)" % (c["x"], c["y"])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--ref-dir", required=True)
    r.add_argument("--label", required=True)
    r.add_argument("--shard", required=True)
    r.add_argument("--iterations", type=int, default=25)
    r.add_argument("--out", required=True)
    s = sub.add_parser("summary")
    s.add_argument("directory")
    args = ap.parse_args(argv)
    return cmd_run(args) if args.cmd == "run" else cmd_summary(args)


if __name__ == "__main__":
    sys.exit(main())
