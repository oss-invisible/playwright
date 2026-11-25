"""Evidence for the cursor-over-chrome hunt (.github/workflows/cursor-over-chrome-hunt.yml).

    python scripts/hunt_cursor_over_chrome.py run --cells chrome-drag,chrome-hover --iterations 25 --out evidence
    python scripts/hunt_cursor_over_chrome.py summary evidence

The question: a native mouse move of the OS cursor (WM_MOUSEMOVE for the real cursor, which SetCursorPos makes the system send) goes to the top-level
widget of the browser window. Over the CONTENT area it is forwarded to the content page and the page records a mousemove with buttons=0 at the cursor's
place (the stray event of tests/test_drag_motion.py::test_a_real_page_sees_a_drag_that_travelled). Over the CHROME (the tab strip and the toolbar above the
content) it should reach the chrome document and never the content page. If so, a test that parks the OS cursor inside the chrome strip, a point inside the
screen, needs no point outside the window.

One iteration is a fresh headed browser (seed 42, as the drag test), a page that records every mouse/pointer/drag event with clientX/Y, screenX/Y, buttons,
isTrusted and timeStamp, the geometry the page reads (mozInnerScreenX/Y, innerWidth/Height, outerWidth/Height, screenX/Y, devicePixelRatio, screen size),
the window rectangle the system gives (GetWindowRect, GetClientRect with ClientToScreen), and one gesture with a helper thread that moves the real cursor
with SetCursorPos while the gesture runs. Cells:

* ``chrome-drag`` / ``chrome-hover``: the jiggle points are inside the chrome strip (between the window's visible top and the content's top).
* ``content-drag`` / ``content-hover``: the control, the jiggle points are inside the content area.
* ``edge-drag`` / ``edge-hover``: no jiggle; the cursor was parked in the top rows of the screen before the browser started (what the fixture would do).
  Shows the pre-launch park is over the chrome once the window opens and that no event reaches the page.
* ``baseline-drag``: no park and no jiggle: the runner's cursor stays where it is (screen centre), the arrangement of the red run.
* ``boundary-hover``: the jiggle alternates between 3 px above the content's top edge (chrome) and 3 px below (content): the boundary read from the page
  is where the page starts to see native moves.

Every cell but ``baseline-drag`` parks the cursor in the top rows before launching. A "native event" is an event the page recorded whose screen point is
within 2 px of a jiggle point (the page's screenX/Y, and the same times devicePixelRatio). GetCursorPos is recorded before and after each jiggle.
One JSON line per iteration goes to ``<out>/iterations-<cell>.jsonl``. Nothing here fails the job on a finding: only a scenario that errors is an error.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import threading
import time
import urllib.parse
from pathlib import Path

CELLS = ("chrome-drag", "chrome-hover", "content-drag", "content-hover", "edge-drag", "edge-hover", "baseline-drag", "boundary-hover")

#: the page of the drag test, with a button for the hover gesture, plus passive observers of every mouse, pointer and drag event
HTML = (
    "<style>body{margin:0}"
    "#a{position:absolute;left:40px;top:40px;width:120px;height:80px}"
    "#b{position:absolute;left:520px;top:380px;width:120px;height:80px}"
    "#c{position:absolute;left:280px;top:260px;width:100px;height:60px}"
    "</style>"
    "<div id='a' draggable='true'>source</div><div id='b'>target</div><button id='c'>hover</button>"
    "<script>window.__full=[];"
    "for (const t of ['mousemove','mousedown','mouseup','click','mouseover','mouseout','mouseenter','mouseleave',"
    "                 'pointermove','pointerdown','pointerup','pointerover','pointerout','pointerenter','pointerleave',"
    "                 'drag','dragstart','dragenter','dragover','dragleave','drop','dragend'])"
    " document.addEventListener(t, e => { window.__full.push({"
    "  type: t, clientX: e.clientX, clientY: e.clientY, screenX: e.screenX, screenY: e.screenY,"
    "  buttons: e.buttons, isTrusted: e.isTrusted, timeStamp: e.timeStamp, wall: Date.now(),"
    "  target: e.target && (e.target.id || e.target.nodeName), pointerType: e.pointerType || null});"
    "  if (t.startsWith('drag') && t !== 'dragstart') e.preventDefault(); }, true);"
    "</script>")

GEOMETRY_JS = (
    "() => ({mozInnerScreenX: window.mozInnerScreenX, mozInnerScreenY: window.mozInnerScreenY,"
    " innerWidth: window.innerWidth, innerHeight: window.innerHeight,"
    " outerWidth: window.outerWidth, outerHeight: window.outerHeight,"
    " screenX: window.screenX, screenY: window.screenY,"
    " screenWidth: screen.width, screenHeight: screen.height,"
    " availWidth: screen.availWidth, availHeight: screen.availHeight,"
    " dpr: window.devicePixelRatio, wall: Date.now()})")


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
# the desktop (Windows only; every function answers None or does nothing elsewhere: the hunt only runs on a Windows runner)
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
class Desk:
    def __init__(self):
        from ctypes import wintypes

        self.wt = wintypes
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        u = self.u
        u.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
        u.GetCursorPos.restype = wintypes.BOOL
        u.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
        u.SetCursorPos.restype = wintypes.BOOL
        u.GetSystemMetrics.argtypes = (ctypes.c_int,)
        u.GetSystemMetrics.restype = ctypes.c_int
        u.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        u.GetWindowRect.restype = wintypes.BOOL
        u.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        u.GetClientRect.restype = wintypes.BOOL
        u.ClientToScreen.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.POINT))
        u.ClientToScreen.restype = wintypes.BOOL
        u.IsWindowVisible.argtypes = (wintypes.HWND,)
        u.IsWindowVisible.restype = wintypes.BOOL
        u.IsIconic.argtypes = (wintypes.HWND,)
        u.IsIconic.restype = wintypes.BOOL
        u.IsZoomed.argtypes = (wintypes.HWND,)
        u.IsZoomed.restype = wintypes.BOOL
        u.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        u.GetClassNameW.restype = ctypes.c_int
        self.enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u.EnumWindows.argtypes = (self.enum_proc, wintypes.LPARAM)
        u.EnumWindows.restype = wintypes.BOOL

    def cursor(self):
        p = self.wt.POINT()
        if not self.u.GetCursorPos(ctypes.byref(p)):
            return None
        return (p.x, p.y)

    def set_cursor(self, x, y):
        return bool(self.u.SetCursorPos(int(x), int(y)))

    def screen(self):
        """The primary screen in physical pixels, as this process sees it, and the virtual screen."""
        g = self.u.GetSystemMetrics
        return {"primary": (g(0), g(1)), "virtual": (g(76), g(77), g(78), g(79))}

    def dpi(self):
        out = {}
        try:
            out["system_dpi"] = int(self.u.GetDpiForSystem())
        except Exception as exc:  # older systems
            out["system_dpi"] = "n/a %s" % type(exc).__name__
        try:
            awareness = ctypes.c_int(-1)
            ctypes.WinDLL("shcore").GetProcessDpiAwareness(0, ctypes.byref(awareness))
            out["process_dpi_awareness"] = awareness.value  # 0 unaware, 1 system, 2 per-monitor
        except Exception as exc:
            out["process_dpi_awareness"] = "n/a %s" % type(exc).__name__
        return out

    def windows_below(self, root_pid):
        """Every visible top-level window of the processes below root_pid, with the window and client rectangles in screen pixels."""
        import psutil

        try:
            pids = {p.pid for p in psutil.Process(root_pid).children(recursive=True)}
        except psutil.Error:
            return []
        found = []

        def each(hwnd, _):
            owner = self.wt.DWORD()
            self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value in pids and self.u.IsWindowVisible(hwnd):
                rect, client, origin = self.wt.RECT(), self.wt.RECT(), self.wt.POINT(0, 0)
                if self.u.GetWindowRect(hwnd, ctypes.byref(rect)) and rect.right > rect.left and rect.bottom > rect.top:
                    self.u.GetClientRect(hwnd, ctypes.byref(client))
                    self.u.ClientToScreen(hwnd, ctypes.byref(origin))
                    name = ctypes.create_unicode_buffer(128)
                    self.u.GetClassNameW(hwnd, name, 128)
                    found.append({"class": name.value, "iconic": bool(self.u.IsIconic(hwnd)), "zoomed": bool(self.u.IsZoomed(hwnd)),
                                  "window": [rect.left, rect.top, rect.right, rect.bottom],
                                  "client": [origin.x, origin.y, origin.x + client.right, origin.y + client.bottom]})
            return True

        self.u.EnumWindows(self.enum_proc(each), 0)
        return found


def main_window(windows):
    """The biggest visible window below the test process (the browser's top-level window)."""
    best = None
    for w in windows:
        area = (w["window"][2] - w["window"][0]) * (w["window"][3] - w["window"][1])
        if best is None or area > best[0]:
            best = (area, w)
    return best[1] if best else None


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
# the points of a cell, from the geometry that was read back (never fixed numbers)
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def content_rect(geo):
    """The content area in screen pixels, from the page's own read: mozInnerScreen* (CSS px of the screen) and innerWidth/Height times the ratio."""
    d = geo["dpr"] or 1
    left = geo["mozInnerScreenX"] * d
    top = geo["mozInnerScreenY"] * d
    return (round(left), round(top), round(left + geo["innerWidth"] * d), round(top + geo["innerHeight"] * d))


def chrome_points(win, content, screen_wh):
    """Points inside the screen and inside the chrome strip: between the window's visible top and the content's top, x away from the window buttons
    (right) and the left edge: 25 % to 45 % of the window width, y at 35 %, 50 % and 65 % of the strip."""
    left, top, right, _ = win
    vis_top = max(top, 0)
    strip = content[1] - vis_top
    xs = [left + (right - left) * f for f in (0.25, 0.30, 0.35, 0.40, 0.45)]
    ys = [vis_top + strip * f for f in (0.35, 0.50, 0.65)]
    return _inside(screen_wh, [(round(x), round(y)) for y in ys for x in xs])


def content_points(content, screen_wh):
    """Points inside the content area and inside the screen: 20 % to 80 % across, 25 %, 50 % and 75 % down."""
    left, top, right, bottom = content
    xs = [left + (right - left) * f for f in (0.20, 0.35, 0.50, 0.65, 0.80)]
    ys = [top + (bottom - top) * f for f in (0.25, 0.50, 0.75)]
    return _inside(screen_wh, [(round(x), round(y)) for y in ys for x in xs])


def boundary_points(content, screen_wh):
    """Alternating 3 px above (chrome) and 3 px below (content) the content's top edge, at three x's."""
    left, top, right, _ = content
    pts = []
    for f in (0.30, 0.50, 0.70):
        x = round(left + (right - left) * f)
        pts += [(x, top - 3), (x + 7, top + 3)]
    return _inside(screen_wh, pts)


def _inside(screen_wh, pts):
    w, h = screen_wh
    return [p for p in pts if 0 <= p[0] < w and 0 <= p[1] < h]


def top_rows_point(screen_wh):
    """Before the browser exists: a point in the top rows of the screen, a third across."""
    return (screen_wh[0] // 3, 10)


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
# the jiggle
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
class Jiggle(threading.Thread):
    """Moves the real cursor through ``points`` in turn, every ``gap`` seconds, until stopped; records GetCursorPos around each move."""

    def __init__(self, desk, points, gap=0.012):
        super().__init__(daemon=True)
        self.desk, self.points, self.gap = desk, points, gap
        self.stop_flag = threading.Event()
        self.log = []
        self.error = None

    def run(self):
        try:
            i = 0
            while not self.stop_flag.is_set():
                x, y = self.points[i % len(self.points)]
                before = self.desk.cursor()
                t0 = int(time.time() * 1000)
                ok = self.desk.set_cursor(x, y)
                after = self.desk.cursor()
                self.log.append({"wall": t0, "target": [x, y], "ok": ok, "before": before, "after": after})
                i += 1
                time.sleep(self.gap)
        except Exception as exc:
            self.error = "%s: %s" % (type(exc).__name__, exc)

    def finish(self):
        self.stop_flag.set()
        self.join(timeout=5)


def natives(events, jiggle_points, dpr):
    """The events whose screen point is within 2 px of a jiggle point (the page's screenX/Y as they are, or times the ratio)."""
    out = []
    for e in events:
        sx, sy = e.get("screenX"), e.get("screenY")
        if sx is None:
            continue
        for jx, jy in jiggle_points:
            if (abs(sx - jx) <= 2 and abs(sy - jy) <= 2) or (abs(sx * dpr - jx) <= 2 and abs(sy * dpr - jy) <= 2):
                out.append(e)
                break
    return out


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
# the gestures
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def gesture_drag(page):
    page.mouse.move(300, 300)
    page.evaluate("() => { window.__full = []; }")
    page.drag_and_drop("#a", "#b")
    page.mouse.move(700, 120)


def gesture_hover(page):
    page.mouse.move(100, 100)
    page.evaluate("() => { window.__full = []; }")
    for _ in range(3):
        page.hover("#a")
        page.mouse.move(330, 290)
        page.click("#c")
        page.mouse.move(600, 200)
        page.mouse.move(150, 400)


def one_iteration(cell, binary, desk):
    from invisible_playwright import InvisiblePlaywright

    kind, gesture = cell.split("-")
    rec = {"cell": cell, "error": None, "seed": 42, "dpi": desk.dpi(), "screen_metrics": desk.screen()}
    jig = None
    screen_wh = desk.screen()["primary"]
    rec["parked_before_launch"] = None
    if kind != "baseline":
        target = top_rows_point(screen_wh)
        desk.set_cursor(*target)
        rec["parked_before_launch"] = {"target": list(target), "read_back": desk.cursor()}
    rec["cursor_before_launch"] = desk.cursor()
    try:
        with InvisiblePlaywright(seed=42, binary_path=binary) as browser:
            page = browser.new_page()
            page.goto("data:text/html," + urllib.parse.quote(HTML))
            time.sleep(0.5)
            geo = page.evaluate(GEOMETRY_JS)
            rec["geometry"] = geo
            wins = desk.windows_below(os.getpid())
            rec["windows"] = wins
            mw = main_window(wins)
            rec["main_window"] = mw
            rec["content_rect"] = list(content_rect(geo))
            rec["cursor_after_launch"] = desk.cursor()
            cur = rec["cursor_after_launch"]
            cr = rec["content_rect"]
            rec["cursor_inside_content_after_launch"] = bool(cur and cr[0] <= cur[0] < cr[2] and cr[1] <= cur[1] < cr[3])
            rec["cursor_inside_window_after_launch"] = bool(
                cur and mw and mw["window"][0] <= cur[0] < mw["window"][2] and mw["window"][1] <= cur[1] < mw["window"][3])
            win = mw["window"] if mw else [geo["screenX"], geo["screenY"], geo["screenX"] + geo["outerWidth"], geo["screenY"] + geo["outerHeight"]]
            if kind == "chrome":
                points = chrome_points(win, cr, screen_wh)
            elif kind == "content":
                points = content_points(cr, screen_wh)
            elif kind == "boundary":
                points = boundary_points(cr, screen_wh)
            else:
                points = []
            rec["jiggle_points"] = points
            page.evaluate("() => { window.__full = []; }")
            if points:
                jig = Jiggle(desk, points)
                jig.start()
                time.sleep(0.25)
            rec["wall_before_gesture"] = int(time.time() * 1000)
            (gesture_drag if gesture == "drag" else gesture_hover)(page)
            rec["wall_after_gesture"] = int(time.time() * 1000)
            if jig is not None:
                time.sleep(0.25)
                jig.finish()
            events = page.evaluate("window.__full")
            rec["events"] = events
            rec["geometry_after"] = page.evaluate(GEOMETRY_JS)
    except Exception as exc:  # the scenario itself broke: recorded, not a finding
        rec["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:500])
    finally:
        if jig is not None:
            jig.finish()
            rec["jiggle_log"] = jig.log
            rec["jiggle_error"] = jig.error
    events = rec.get("events") or []
    pts = rec.get("jiggle_points") or []
    dpr = (rec.get("geometry") or {}).get("dpr") or 1
    nat = natives(events, pts, dpr) if pts else []
    rec["native_events"] = nat
    rec["native_count"] = len(nat)
    rec["jiggles"] = len(rec.get("jiggle_log") or [])
    rec["jiggles_read_back_ok"] = sum(1 for j in rec.get("jiggle_log") or [] if j["after"] == tuple(j["target"]) or j["after"] == j["target"])
    # events with no button after the press of a drag: the stray the drag test names (whatever its screen point)
    press = next((e for e in events if e["type"] == "mousedown"), None)
    rec["moves_buttons0_after_press"] = (
        [e for e in events if press and e["wall"] >= press["wall"] and e["type"] in ("mousemove", "pointermove") and e["buttons"] == 0]
        if gesture == "drag" else [])
    rec["event_count"] = len(events)
    return rec


def cmd_run(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    binary = os.environ["INVPW_BINARY_PATH"]
    desk = Desk()
    cells = [c.strip() for c in args.cells.split(",") if c.strip()]
    for c in cells:
        if c not in CELLS:
            raise SystemExit("unknown cell %r (known: %s)" % (c, ", ".join(CELLS)))
    for cell in cells:
        jsonl = out / ("iterations-%s.jsonl" % cell)
        for n in range(1, args.iterations + 1):
            try:
                line = one_iteration(cell, binary, desk)
            except Exception as exc:  # the harness around the scenario broke: recorded, the loop goes on
                line = {"cell": cell, "error": "harness: %s: %s" % (type(exc).__name__, str(exc)[:500])}
            line["iteration"] = n
            with open(jsonl, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line) + "\n")
            print("%s %02d: error=%s events=%s jiggles=%s native=%s stray0=%s inside_content_after_launch=%s" % (
                cell, n, line.get("error"), line.get("event_count"), line.get("jiggles"), line.get("native_count"),
                len(line.get("moves_buttons0_after_press") or []), line.get("cursor_inside_content_after_launch")), flush=True)
    return 0


def _lines(directory):
    for p in sorted(Path(directory).rglob("iterations-*.jsonl")):
        for raw in p.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                yield json.loads(raw)


def cmd_summary(args):
    rows = {}
    examples = {}
    geo = None
    for line in _lines(args.directory):
        r = rows.setdefault(line["cell"], {"iterations": 0, "errors": 0, "jiggles": 0, "native_events": 0, "iters_with_native": 0, "stray0": 0,
                                           "iters_with_stray0": 0, "events": 0, "inside_content": 0})
        r["iterations"] += 1
        if line.get("error"):
            r["errors"] += 1
            continue
        geo = geo or line
        r["jiggles"] += line.get("jiggles") or 0
        r["native_events"] += line.get("native_count") or 0
        r["iters_with_native"] += 1 if line.get("native_count") else 0
        r["stray0"] += len(line.get("moves_buttons0_after_press") or [])
        r["iters_with_stray0"] += 1 if line.get("moves_buttons0_after_press") else 0
        r["events"] += line.get("event_count") or 0
        r["inside_content"] += 1 if line.get("cursor_inside_content_after_launch") else 0
        for e in (line.get("native_events") or [])[:1]:
            examples.setdefault(line["cell"], []).append((line, e))
    head = ("cell", "iterations", "errors", "jiggles", "native_events", "iters_with_native", "stray0", "iters_with_stray0", "events", "inside_content")
    print("| " + " | ".join(head) + " |")
    print("|" + "---|" * len(head))
    for cell, r in sorted(rows.items()):
        print("| %s | " % cell + " | ".join(str(r[k]) for k in head[1:]) + " |")
    print()
    print("(native_events: page events whose screen point is within 2 px of a jiggle point; stray0: mousemove/pointermove with buttons=0 after the press;")
    print(" inside_content: iterations in which the OS cursor was inside the content area right after the launch.)")
    print()
    if geo:
        print("geometry read on the runner (first clean iteration, cell %s):" % geo["cell"])
        print("  page:", json.dumps(geo.get("geometry")))
        print("  main window:", json.dumps(geo.get("main_window")))
        print("  content rect (screen px, from the page):", geo.get("content_rect"), " dpi:", json.dumps(geo.get("dpi")),
              " metrics:", json.dumps(geo.get("screen_metrics")))
        print("  cursor before launch:", geo.get("cursor_before_launch"), " parked:", json.dumps(geo.get("parked_before_launch")),
              " after launch:", geo.get("cursor_after_launch"))
    print()
    print("examples of native events (page screen point vs jiggle point):")
    if not examples:
        print("  none")
    for cell, items in sorted(examples.items()):
        for line, e in items[:3]:
            print("  cell=%s it=%02d %s client=(%s,%s) screen=(%s,%s) buttons=%s trusted=%s; mozInnerScreen=(%s,%s) -> screen-client=(%s,%s)" % (
                cell, line["iteration"], e["type"], e["clientX"], e["clientY"], e["screenX"], e["screenY"], e["buttons"], e["isTrusted"],
                line["geometry"]["mozInnerScreenX"], line["geometry"]["mozInnerScreenY"], e["screenX"] - e["clientX"], e["screenY"] - e["clientY"]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--cells", required=True)
    r.add_argument("--iterations", type=int, default=25)
    r.add_argument("--out", required=True)
    s = sub.add_parser("summary")
    s.add_argument("directory")
    args = ap.parse_args(argv)
    return cmd_run(args) if args.cmd == "run" else cmd_summary(args)


if __name__ == "__main__":
    sys.exit(main())
