"""HUNT ONLY (branch hunt/cpu-render-path, never merged): can a PAGE tell software compositing ("WebRender (Software)") from our hardware path?

    python scripts/hunt_compositor_visible.py run   --arm ours|ours_vmretail|stockheaded|retail [--binary B] [--retail-from DIR] --out DIR
    python scripts/hunt_compositor_visible.py facts --arm ... (same)  --out DIR        # Troubleshoot, effective prefs, the OS's own refresh rate
    python scripts/hunt_compositor_visible.py table DIR

Earlier rAF data showed 60 fps in one arm and 50-53 in another. What decides the rate of requestAnimationFrame on Windows (code of this tree):
  - the vsync source is gfx/thebes/gfxWindowsPlatform.cpp D3DVsyncSource. Its RATE is read once from DwmGetCompositionTimingInfo (lines 1466-1480: the DWM's
    rateRefresh, or 60 Hz if the call fails). Its LOOP (lines 1605-1646) waits on IDXGIOutput::WaitForVBlank; if that fails, or returns in under 1 ms ("vblank
    might return instantly when running headless, monitor powering off"), it schedules a SOFTWARE vsync with that same rate (ScheduleSoftwareVsync, line 1520);
  - layout.frame_rate > 0 forces SoftwareVsyncSource at that rate; -1 (default) is the hardware source, with 60 Hz only when the hardware source fails
    (gfx/thebes/gfxPlatform.cpp:3462-3516, GetDefaultFrameRate() == 60);
  - privacy.resistFingerprinting with RFPTarget::FrameRate makes layout.frame_rate read as 60 (gfxPlatform.cpp LayoutFrameRateFromPrefs).
So a browser whose vsync is the DWM/WaitForVBlank of a virtual display runs at that display's refresh rate and jitter, and one on the software fallback runs on an exact
60.00 Hz timer. `run` measures what the page sees; `facts` reads what the OS and Troubleshoot say (graphics.monitors[].refreshRate is Screen::GetRefreshRate, the OS rate;
the effective layout.frame_rate and gfx.vsync.* prefs; the layer manager) and PowerShell's Win32_VideoController.CurrentRefreshRate. Which vsync source Gecko picked is not exposed to
a page or to Troubleshoot: it is inferred from the interval distribution (an exact 16.667 ms with a tight p5..p95 is a timer; a hardware wait has the display's period and jitter).

`run` launches ONE browser of the arm, opens a page of this script's local server and lets it measure for about 20 s (scripts/hunt_compositor_probe.js): requestAnimationFrame
alone (5 s: interval p5/p50/p95, fps, dropped frames, the interval buckets, now - timestamp), with a CSS animation (4 s), with a WebGL animation (2 s free + 2 s with a synchronous
readback every frame), a smooth scrollTo of a long page (scroll events per second, time to finish, scrollend), canvas 2D getImageData on 1024x1024 after drawing, WebGL
readPixels of 1024x1024, matchMedia (update: fast), screen refresh hints, the resolution of performance.now(). The arms:
  ours           invisible_playwright, headless=True (on Windows a headed Firefox on a hidden desktop), the declared persona
  ours_vmretail  ours with the prefs of hunt_vmretail.py build (HUNT_VMRETAIL_PREFS): retail's VM decisions, WebRender software without D3D11, WebGL on WARP
  stockheaded    stock Playwright's firefox, headless=False
  retail         Mozilla's Firefox 159.0 launched by hand (hunt_first_run.py fetch-retail): subprocess, -no-remote, a fresh profile, no automation, the page posts its result
`table` prints, per arm, every measurement, then ours and ours_vmretail against retail (and the other pairs) with a DIFFERS mark. Each row says whether it is TIMING (a measurement:
a difference is a rate or a latency a page can time) or VALUE (a declared or fixed property: a difference is a different answer to the same question).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hunt_cpu_per_browser as hunt   # noqa: E402
import hunt_cpu_render_path as rp   # noqa: E402

ARMS = ["ours", "ours_vmretail", "stockheaded", "retail"]
WAIT_S = 90


class Page(BaseHTTPRequestHandler):
    result: Optional[str] = None

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):   # noqa: N802
        if self.path.startswith("/probe.js"):
            self._send((HERE / "hunt_compositor_probe.js").read_bytes(), "text/javascript; charset=utf-8")
        else:
            self._send(b'<!doctype html><meta charset=utf-8><title>compositor</title><body style="margin:0"><script src="/probe.js"></script></body>',
                       "text/html; charset=utf-8")

    def do_POST(self):   # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        Page.result = self.rfile.read(n).decode("utf-8", "replace")
        self._send(b"ok", "text/plain")

    def log_message(self, *a):   # noqa: D102
        pass


def serve() -> Tuple[ThreadingHTTPServer, str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:%d/" % httpd.server_address[1]


def kill_firefox() -> None:
    rp.kill_firefox()


def retail_exe(args) -> Tuple[Path, str]:
    info = json.loads((Path(args.retail_from) / "retail.json").read_text(encoding="utf-8"))
    return Path(info["exe"]), info["note"]


def os_facts() -> Dict[str, Any]:
    """What the machine itself says about its display (not through Firefox)."""
    out: Dict[str, Any] = {}
    if sys.platform == "win32":
        cmd = ("Get-CimInstance Win32_VideoController | Select-Object Name,CurrentRefreshRate,MaxRefreshRate,MinRefreshRate,VideoModeDescription,"
               "AdapterCompatibility,DriverVersion | ConvertTo-Json -Compress")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=60)
            out["Win32_VideoController"] = json.loads(r.stdout) if r.stdout.strip() else r.stderr[:300]
        except Exception as exc:   # noqa: BLE001
            out["Win32_VideoController"] = "%s: %s" % (type(exc).__name__, exc)
        try:
            import ctypes
            out["session_is_remote"] = bool(ctypes.windll.user32.GetSystemMetrics(0x1000))   # SM_REMOTESESSION
        except Exception:   # noqa: BLE001
            pass
    return out


# ----------------------------------------------------------------------------------------------------------------------------- run

async def ours_run(binary: str, url: str, prefs: Optional[dict]) -> None:
    from invisible_playwright.async_api import InvisiblePlaywright
    async with InvisiblePlaywright(seed=100, binary_path=binary, headless=True, locale="en-US", timezone="Europe/Rome", extra_prefs=prefs) as browser:
        page = await browser.new_page()
        await page.goto(url, wait_until="load", timeout=60000)
        await wait_result()


async def stock_run(url: str) -> None:
    import importlib
    pw = await importlib.import_module("playwright.async_api").async_playwright().start()   # by name: the fork's guard forbids a static import
    browser = None
    try:
        browser = await pw.firefox.launch(headless=False, firefox_user_prefs={"fission.autostart": True})
        page = await browser.new_page()
        await page.goto(url, wait_until="load", timeout=60000)
        await wait_result()
    finally:
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.close()
        with contextlib.suppress(Exception):
            await pw.stop()


async def wait_result() -> None:
    end = time.time() + WAIT_S
    while Page.result is None and time.time() < end:
        await asyncio.sleep(0.5)


def run(args) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    httpd, url = serve()
    Page.result = None
    kill_firefox()
    note, err = "", ""
    t0 = time.time()
    try:
        if args.arm == "retail":
            exe, note = retail_exe(args)
            profile = Path(tempfile.mkdtemp(prefix="hunt-compositor-retail-"))
            proc = subprocess.Popen([str(exe), "-no-remote", "-profile", str(profile), url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                end = time.time() + WAIT_S
                while Page.result is None and time.time() < end:
                    time.sleep(0.5)
            finally:
                kill_firefox()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=10)
        elif args.arm == "stockheaded":
            asyncio.run(stock_run(url))
        else:
            asyncio.run(ours_run(args.binary, url, hunt.arm_prefs(args.arm)))
    except BaseException as exc:   # noqa: BLE001
        err = "%s: %s" % (type(exc).__name__, str(exc)[:400])
    kill_firefox()
    httpd.shutdown()
    data: Dict[str, Any] = {}
    if Page.result:
        try:
            data = json.loads(Page.result)
        except ValueError as exc:
            err = err or "unreadable result: %s" % exc
    else:
        err = err or "the page posted nothing in %d s" % WAIT_S
    doc = {"arm": args.arm, "note": note, "error": err, "wall_s": time.time() - t0, "probe": data, "os": os_facts()}
    (args.out / ("%s.json" % args.arm)).write_text(json.dumps(doc, indent=1), encoding="utf-8")
    print("arm %s: %s; probe took %.1f s of wall time%s" % (args.arm, "result received" if data else "NO RESULT", doc["wall_s"], ("; ERROR " + err) if err else ""))
    if data.get("errors"):
        print("  page errors: %s" % data["errors"])
    print_arm(doc)
    return 1 if err else 0


# -------------------------------------------------------------------------------------------------------------------------- facts

def facts(args) -> int:
    out = args.out / ("%s-facts" % args.arm)
    out.mkdir(parents=True, exist_ok=True)
    kill_firefox()
    report: Dict[str, Any] = {"arm": args.arm}

    async def ours_like():
        async with rp.launched(args.arm, args.binary or "", marionette=True) as browser:
            page = await browser.new_page()
            await page.goto("about:blank")
            await asyncio.get_running_loop().run_in_executor(None, rp.privileged_route, out, report)

    try:
        if args.arm == "retail":
            exe, note = retail_exe(args)
            report["note"] = note
            profile = Path(tempfile.mkdtemp(prefix="hunt-compositor-facts-"))
            env = dict(os.environ, MOZ_REMOTE_ALLOW_SYSTEM_ACCESS="1")
            proc = subprocess.Popen([str(exe), "-no-remote", "-marionette", "-remote-allow-system-access", "-profile", str(profile), "about:blank"], env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                rp.privileged_route(out, report)
            finally:
                kill_firefox()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=10)
        else:
            asyncio.run(ours_like())
    except BaseException as exc:   # noqa: BLE001
        report["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:300])
    kill_firefox()
    (out / "report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print_facts(args.arm, out)
    return 0


def read_facts(out: Path) -> Dict[str, Any]:
    d: Dict[str, Any] = {}
    sf, pf = out / "troubleshoot.json", out / "effective_prefs.json"
    if sf.is_file():
        data = json.loads(sf.read_text(encoding="utf-8"))
        g = data.get("graphics", {})
        d["monitors"] = g.get("monitors")
        d["layer_manager"] = (g.get("windowLayerManagerType"), g.get("windowLayerManagerRemote"))
        d["window_protocol"] = g.get("windowProtocol")
        d["adapter"] = g.get("adapterDescription")
        d["features"] = {f.get("name"): f.get("status") for f in (g.get("features") or []) if isinstance(f, dict)}
    if pf.is_file():
        prefs = json.loads(pf.read_text(encoding="utf-8"))
        d["prefs"] = {n: prefs[n][1] for n in prefs if n.startswith(("layout.frame_rate", "gfx.vsync", "gfx.webrender.software", "gfx.canvas", "layout.throttled", "dom.animations", "general.smoothScroll",
                                                                    "apz.", "privacy.resistFingerprinting", "privacy.baselineFingerprintingProtection"))}
    return d


def print_facts(arm: str, out: Path) -> None:
    d = read_facts(out)
    print("-" * 110)
    print("FACTS of %s (Troubleshoot, effective prefs)" % arm)
    if not d:
        print("  nothing read (see report.json)")
        return
    print("  monitors (graphics.monitors, Screen::GetRefreshRate = the OS rate): %s" % json.dumps(d.get("monitors")))
    print("  layer manager type / remote: %s; window protocol: %s; adapter: %s" % (d.get("layer_manager"), d.get("window_protocol"), d.get("adapter")))
    for name in ("Compositing", "WebRender", "WebRender (Software)", "Direct3D 11 Compositing", "GPU Process"):
        if name in (d.get("features") or {}):
            print("  feature %-28s %s" % (name, d["features"][name]))
    for n, v in sorted((d.get("prefs") or {}).items()):
        print("  pref %s = %s" % (n, v))


# --------------------------------------------------------------------------------------------------------------------------- table

# (label, path, kind, comparison): comparison is ("rel", ratio) | ("abs", amount) | ("fps", ...) | ("eq",)
METRICS: List[Tuple[str, str, str, Tuple]] = [
    ("rAF idle: fps", "phases.raf_idle_5s.fps", "timing", ("fps",)),
    ("rAF idle: interval p5 ms", "phases.raf_idle_5s.interval_ms_p5", "timing", ("ms", 1.0, 0.15)),
    ("rAF idle: interval p50 ms", "phases.raf_idle_5s.interval_ms_p50", "timing", ("ms", 1.0, 0.15)),
    ("rAF idle: interval p95 ms", "phases.raf_idle_5s.interval_ms_p95", "timing", ("ms", 2.0, 0.25)),
    ("rAF idle: dropped frames", "phases.raf_idle_5s.dropped_frames", "timing", ("count",)),
    ("rAF idle: now - timestamp p50 ms", "phases.raf_idle_5s.now_minus_timestamp_ms_p50", "timing", ("ms", 1.0, 0.5)),
    ("rAF idle: intervals <14 / 14-15.5 / 15.5-18 / 18-20 / 20-25 / >25 ms", "phases.raf_idle_5s.buckets", "timing", ("buckets",)),
    ("rAF + CSS animation: fps", "phases.raf_with_css_animation_4s.fps", "timing", ("fps",)),
    ("rAF + CSS animation: interval p95 ms", "phases.raf_with_css_animation_4s.interval_ms_p95", "timing", ("ms", 2.0, 0.25)),
    ("rAF + CSS animation: dropped frames", "phases.raf_with_css_animation_4s.dropped_frames", "timing", ("count",)),
    ("rAF + CSS animation: animations running (value)", "phases.raf_with_css_animation_4s.running_animations", "value", ("eq",)),
    ("rAF + WebGL animation: fps", "phases.raf_with_webgl_animation_4s.free.fps", "timing", ("fps",)),
    ("rAF + WebGL animation: interval p95 ms", "phases.raf_with_webgl_animation_4s.free.interval_ms_p95", "timing", ("ms", 2.0, 0.25)),
    ("rAF + WebGL animation: dropped frames", "phases.raf_with_webgl_animation_4s.free.dropped_frames", "timing", ("count",)),
    ("WebGL animation + readback each frame: fps", "phases.raf_with_webgl_animation_4s.with_sync_readback_every_frame.fps", "timing", ("fps",)),
    ("WebGL 1-pixel sync readback per frame p50 ms", "phases.raf_with_webgl_animation_4s.sync_readback_ms_p50", "timing", ("ms", 2.0, 0.5)),
    ("smooth scroll: reached target (value)", "phases.smooth_scroll.reached_target", "value", ("eq",)),
    ("smooth scroll: scrollend fired (value)", "phases.smooth_scroll.scrollend_fired", "value", ("eq",)),
    ("smooth scroll: scroll events", "phases.smooth_scroll.scroll_events", "timing", ("count",)),
    ("smooth scroll: scroll events per second", "phases.smooth_scroll.scroll_events_per_second", "timing", ("rel", 0.3)),
    ("smooth scroll: time to finish ms", "phases.smooth_scroll.time_to_finish_ms", "timing", ("ms", 100.0, 0.3)),
    ("smooth scroll: rAF fps while it runs", "phases.smooth_scroll.frames_during.fps", "timing", ("fps",)),
    ("canvas2d 1024: first getImageData ms", "phases.canvas2d_getImageData_1024.first_getImageData_ms", "timing", ("ms", 3.0, 0.5)),
    ("canvas2d 1024: repeat getImageData p50 ms", "phases.canvas2d_getImageData_1024.repeat_getImageData_ms_p50", "timing", ("ms", 3.0, 0.5)),
    ("canvas2d 1024: draw then 1x1 getImageData p50 ms", "phases.canvas2d_getImageData_1024.draw_then_1x1_getImageData_ms_p50", "timing", ("ms", 3.0, 0.5)),
    ("canvas2d 1024: toDataURL png ms", "phases.canvas2d_getImageData_1024.toDataURL_png_ms", "timing", ("ms", 5.0, 0.5)),
    ("canvas2d 1024: pixels hash (value)", "phases.canvas2d_getImageData_1024.bytes_hash_value", "value", ("eq",)),
    ("webgl 1024: first readPixels ms", "phases.webgl_readPixels_1024.first_readPixels_ms", "timing", ("ms", 3.0, 0.5)),
    ("webgl 1024: repeat readPixels p50 ms", "phases.webgl_readPixels_1024.repeat_readPixels_ms_p50", "timing", ("ms", 3.0, 0.5)),
    ("webgl 1024: draw then 1px readPixels p50 ms", "phases.webgl_readPixels_1024.draw_then_1px_readPixels_ms_p50", "timing", ("ms", 2.0, 0.5)),
    ("webgl 1024: pixels hash (value)", "phases.webgl_readPixels_1024.bytes_hash_value", "value", ("eq",)),
    ("webgl: unmasked renderer (value)", "phases.webgl_readPixels_1024.renderer_value", "value", ("eq",)),
]
VALUE_KEYS = ["update_fast", "update_slow", "update_none", "prefers_reduced_motion", "hover", "any_pointer_fine", "dynamic_range_high", "color_gamut_p3", "display_mode_browser", "screen", "dpr",
              "visibility", "hasFocus", "hidden", "scroll_behavior_supported", "scrollend_supported", "screen_refresh_property", "screen_keys_with_rate", "performance_now_min_step_ms",
              "performance_now_decimals_seen", "date_now_min_step_ms", "hardwareConcurrency"]


def dig(doc: Dict[str, Any], path: str) -> Any:
    cur: Any = doc.get("probe", {})
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def differs(spec: Tuple, a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a != b
    kind = spec[0]
    if kind == "eq":
        return a != b
    if kind == "buckets":
        ta, tb = sum(a.values()) or 1, sum(b.values()) or 1
        return any(abs(a[k] / ta - b[k] / tb) > 0.25 for k in a)
    if kind == "fps":
        return abs(a - b) > max(2.0, 0.05 * max(a, b))
    if kind == "count":
        return abs(a - b) > max(3.0, 0.3 * max(a, b))
    if kind == "rel":
        lo, hi = min(a, b), max(a, b)
        return hi > 0 and (lo == 0 or hi / lo > 1 + spec[1])
    if kind == "ms":
        floor, rel = spec[1], spec[2]
        return abs(a - b) > max(floor, rel * max(abs(a), abs(b)))      # above `floor` ms AND above `rel` of the larger value
    return a != b


def fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, dict):
        return "/".join(str(x) for x in v.values())
    if isinstance(v, list):
        return json.dumps(v)
    return str(v)


def print_arm(doc: Dict[str, Any]) -> None:
    print("-" * 110)
    print("ARM %s%s" % (doc["arm"], (" (" + doc["note"][:100] + ")") if doc.get("note") else ""))
    for label, path, kind, _spec in METRICS:
        print("  %-9s %-64s %s" % (kind.upper(), label, fmt(dig(doc, path))))
    vals = doc.get("probe", {}).get("values", {})
    for k in VALUE_KEYS:
        print("  %-9s %-64s %s" % ("VALUE", k, fmt(vals.get(k))))
    print("  OS: %s" % json.dumps(doc.get("os"))[:400])
    sys.stdout.flush()


def table(root: Path) -> int:
    docs: Dict[str, Dict[str, Any]] = {}
    for a in ARMS:
        p = root / ("%s.json" % a)
        if p.is_file():
            docs[a] = json.loads(p.read_text(encoding="utf-8"))
    if not docs:
        print("no results in %s" % root)
        return 1
    arms = [a for a in ARMS if a in docs]
    print("=" * 140)
    print("WHAT A PAGE READS OF THE COMPOSITOR: %s" % ", ".join("%s%s" % (a, " (ERROR %s)" % docs[a]["error"][:60] if docs[a]["error"] else "") for a in arms))
    for a in arms:
        if docs[a].get("note"):
            print("  %s: %s" % (a, docs[a]["note"][:200]))
    print("=" * 140)
    print("%-9s %-62s" % ("kind", "measurement") + "".join(" %-15s" % a for a in arms))
    for label, path, kind, _spec in METRICS:
        print("%-9s %-62s" % (kind.upper(), label[:62]) + "".join(" %-15s" % fmt(dig(docs[a], path))[:15] for a in arms))
    for k in VALUE_KEYS:
        print("%-9s %-62s" % ("VALUE", k) + "".join(" %-15s" % fmt(docs[a].get("probe", {}).get("values", {}).get(k))[:15] for a in arms))
    for pair in (("ours", "retail"), ("ours_vmretail", "retail"), ("ours", "ours_vmretail"), ("stockheaded", "retail")):
        if pair[0] not in docs or pair[1] not in docs:
            continue
        a, b = docs[pair[0]], docs[pair[1]]
        print("=" * 140)
        print("%s AGAINST %s: what differs" % (pair[0].upper(), pair[1].upper()))
        n_t = n_v = 0
        for label, path, kind, spec in METRICS:
            va, vb = dig(a, path), dig(b, path)
            if differs(spec, va, vb):
                n_t += kind == "timing"
                n_v += kind == "value"
                print("  DIFFERS  %-8s %-62s %s: %s | %s: %s" % ("TIMING-ONLY" if kind == "timing" else "VALUE", label[:62], pair[0], fmt(va), pair[1], fmt(vb)))
        for k in VALUE_KEYS:
            va, vb = a.get("probe", {}).get("values", {}).get(k), b.get("probe", {}).get("values", {}).get(k)
            if va != vb:
                n_v += 1
                print("  DIFFERS  %-8s %-62s %s: %s | %s: %s" % ("VALUE", k, pair[0], fmt(va), pair[1], fmt(vb)))
        print("  -> %d timing measurement(s) differ (timing-only), %d value(s) differ" % (n_t, n_v))
    # the facts, if collected
    for a in arms:
        f = root / ("%s-facts" % a)
        if f.is_dir():
            print_facts(a, f)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "facts"):
        p = sub.add_parser(name)
        p.add_argument("--arm", choices=ARMS, required=True)
        p.add_argument("--binary", default=os.environ.get("INVPW_BINARY_PATH", ""))
        p.add_argument("--retail-from", default="")
        p.add_argument("--out", type=Path, required=True)
    t = sub.add_parser("table")
    t.add_argument("root", type=Path)
    args = ap.parse_args()
    if args.cmd == "table":
        return table(args.root)
    if args.arm.startswith("ours") and not args.binary:
        ap.error("the ours arms need --binary (or INVPW_BINARY_PATH)")
    if args.arm == "retail" and not args.retail_from:
        ap.error("the arm retail needs --retail-from (hunt_first_run.py fetch-retail)")
    return run(args) if args.cmd == "run" else facts(args)


if __name__ == "__main__":
    sys.exit(main())
