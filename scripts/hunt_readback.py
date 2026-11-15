"""HUNT ONLY (branch hunt/cpu-render-path, never merged): why does a 1024x1024 WebGL readPixels take ~250-290 ms in ours against 5-9 ms in stock and retail?

    python scripts/hunt_readback.py run     --arm ours|ours_noproj|stockheaded|retail [--binary B] [--retail-from DIR] --out DIR
    python scripts/hunt_readback.py facts   --arm ... --out DIR                       # the effective zoom.stealth.canvas.* and fpp.hw_seed prefs
    python scripts/hunt_readback.py profile --arm ours|ours_noproj --binary B --out DIR   # Gecko profile of 20 readPixels + 20 toDataURL (every process, every thread)
    python scripts/hunt_readback.py table DIR [DIR ...]                               # one DIR per round

MSAA was not the cause (run 43656857973). The suspect is the canvas noise of the engine (the engine source tree): decision "noise only on read".
  - the code: gfx/2d/StealthCanvasProjection.cpp StealthProjectPixels (lines 133-274), called for WebGL/WebGPU only (a 2D canvas is never projected: StealthCanvasProjection.h:62-71,
    dom/canvas/StealthCanvasNoise.cpp:44-48). Roads: readPixels (dom/canvas/ClientWebGLContext.cpp:5486 -> StealthWebGLReadback.h:89-98 ReadPixels, which reads TWICE: DoReadPixels
    for the page and, inside ProjectReadPixels, a second DoReadPixels of the rectangle grown by one pixel, lines 200-209, then copies the rows back, 237-249), toDataURL/toBlob
    (ClientWebGLContext.cpp:1367 and :1399 -> StealthWebGLReadback.h:60-66 -> CanvasUtils::StealthProjectSnapshot, dom/canvas/StealthCanvasNoise.cpp:50-69, which creates a new data
    surface and copies the whole canvas before projecting, gfx/2d/StealthCanvasProjection.cpp:276-308), PBO reads (StealthWebGLReadback.h:108-171: two more reads and one BufferSubData per row);
  - the seed is zoom.stealth.fpp.hw_seed; the projection is ON when it is > 0 AND zoom.stealth.canvas.substitute_pixels is true (gfx/thebes/StealthDeclarationGate.h:54-62; the pref is
    declared in modules/libpref/init/StealthPrefList.yaml:385-388, set by the core in invisible_core/prefs.py:1015). zoom.stealth.canvas.edge_tolerance (prefs.py:1019, 4) is its tolerance.
  - the cost is DATA-DEPENDENT: a pixel is touched only if one of its four neighbours differs once the low bit of R, G, B is cleared (StealthCanvasProjection.cpp:187-193); an edge pixel
    builds its 3x3 neighbourhood and runs OnOneSegment (lines 89-129: 36 pair distances, each of 4 channels, then up to 9 segment tests) and, if it passes, 8 Mix() calls and the carry
    logic (lines 202-265). A flat canvas is a memcpy, a canvas in which EVERY pixel is an edge costs roughly a thousand operations per pixel. The earlier probes (hunt_compositor_probe.js,
    hunt_msaa_probe.js) drew fract(gl_FragCoord.x/97.): every pixel an edge, the worst case.
THE ARMS: ours; ours_noproj = ours with zoom.stealth.canvas.substitute_pixels=false passed ONLY by the hunt (hunt_cpu_per_browser.ARM_PREFS); stockheaded; retail 159.0 by hand (the page
posts its result to the local server).
`run` (scripts/hunt_readback_probe.js, mode timing): per content (flat, halves, triangle, gradient, noise) WebGL readPixels p50, toDataURL p50, 1-pixel readPixels, canvas 2D getImageData p50 and
toDataURL p50, all 1024x1024, antialias:false; a hash of the bytes; and the 256x256 diagonal-edge scene (antialias:false) with its raw bytes.
`profile` (mode profile): 20 readPixels + 20 toDataURL of the gradient canvas with the Gecko profiler on in EVERY process and thread (MOZ_PROFILER_STARTUP_FILTERS=*); the workflow then runs
hunt_profile_summary.py with symbols for the content ("tab") and GPU processes.
`table`: ms and ms per MB per content and arm; ours against ours_noproj; the 2D control; the pixel diff of the edge scene (is the 1-level difference of ours against retail the projection?).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import gzip
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hunt_cpu_per_browser as hunt   # noqa: E402
import hunt_cpu_render_path as rp   # noqa: E402
import hunt_compositor_visible as cv   # noqa: E402

ARMS = ["ours", "ours_noproj", "stockheaded", "retail"]
CONTENTS = ["flat", "halves", "triangle", "gradient", "noise"]
MB = 1024 * 1024 * 4 / 1048576.0     # a 1024x1024 RGBA canvas is 4 MiB


class ReadbackPage(cv.Page):
    def do_GET(self):   # noqa: N802
        if self.path.startswith("/probe.js"):
            self._send((HERE / "hunt_readback_probe.js").read_bytes(), "text/javascript; charset=utf-8")
        else:
            self._send(b'<!doctype html><meta charset=utf-8><title>readback</title><body style="margin:0"><script src="/probe.js"></script></body>', "text/html; charset=utf-8")


def serve(query: str = "") -> Tuple[ThreadingHTTPServer, str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ReadbackPage)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:%d/%s" % (httpd.server_address[1], query)


def run(args) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    httpd, url = serve()
    cv.Page.result = None
    cv.kill_firefox()
    note, err = "", ""
    try:
        if args.arm == "retail":
            exe, note = cv.retail_exe(args)
            profile = Path(tempfile.mkdtemp(prefix="hunt-readback-retail-"))
            proc = subprocess.Popen([str(exe), "-no-remote", "-profile", str(profile), url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                end = time.time() + cv.WAIT_S
                while cv.Page.result is None and time.time() < end:
                    time.sleep(0.5)
            finally:
                cv.kill_firefox()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=10)
        elif args.arm == "stockheaded":
            asyncio.run(cv.stock_run(url))
        else:
            asyncio.run(cv.ours_run(args.binary, url, hunt.arm_prefs(args.arm)))
    except BaseException as exc:   # noqa: BLE001
        err = "%s: %s" % (type(exc).__name__, str(exc)[:400])
    cv.kill_firefox()
    httpd.shutdown()
    data: Dict[str, Any] = {}
    if cv.Page.result:
        try:
            data = json.loads(cv.Page.result)
        except ValueError as exc:
            err = err or "unreadable result: %s" % exc
    else:
        err = err or "the page posted nothing in %d s" % cv.WAIT_S
    doc = {"arm": args.arm, "note": note, "error": err, "probe": data, "prefs_passed": hunt.arm_prefs(args.arm) if args.arm.startswith("ours") else None}
    (args.out / ("%s.json" % args.arm)).write_text(json.dumps(doc, indent=1), encoding="utf-8")
    print("arm %s: %s%s" % (args.arm, "result received" if data else "NO RESULT", ("; ERROR " + err) if err else ""))
    if data.get("errors"):
        print("  page errors: %s" % data["errors"])
    print_arm(doc)
    return 1 if err else 0


def facts(args) -> int:
    out = args.out / ("%s-facts" % args.arm)
    out.mkdir(parents=True, exist_ok=True)
    cv.kill_firefox()
    report: Dict[str, Any] = {"arm": args.arm}

    async def go():
        async with rp.launched(args.arm, args.binary or "", marionette=True) as browser:
            page = await browser.new_page()
            await page.goto("about:blank")
            await asyncio.get_running_loop().run_in_executor(None, rp.privileged_route, out, report)

    try:
        asyncio.run(go())
    except BaseException as exc:   # noqa: BLE001
        report["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:300])
    cv.kill_firefox()
    print_facts(args.arm, out)
    return 0


def print_facts(arm: str, out: Path) -> None:
    print("-" * 110)
    print("FACTS of %s: the canvas-noise prefs as the browser reads them (the projection is on when hw_seed > 0 and substitute_pixels is true)" % arm)
    pf = out / "effective_prefs.json"
    if not pf.is_file():
        print("  effective_prefs.json missing")
        return
    prefs = json.loads(pf.read_text(encoding="utf-8"))
    for n in sorted(prefs):
        if n.startswith(("zoom.stealth.canvas", "zoom.stealth.fpp")):
            print("  pref %s = %s (user-set: %s)" % (n, prefs[n][1], prefs[n][0]))


# -------------------------------------------------------------------------------------------------------------------- profile

def profile(args) -> int:
    out = args.out / args.arm
    out.mkdir(parents=True, exist_ok=True)
    pattern = str(out / "profile-%p.json")
    os.environ.update({"MOZ_PROFILER_STARTUP": "1", "MOZ_PROFILER_STARTUP_FEATURES": "stackwalk,cpu,threads,processcpu", "MOZ_PROFILER_STARTUP_FILTERS": "*",
                       "MOZ_PROFILER_STARTUP_ENTRIES": "4194304", "MOZ_PROFILER_STARTUP_INTERVAL": "1", "MOZ_PROFILER_SHUTDOWN": pattern})
    print(rp.let_profiler_env_through())
    print("profile arm %s: %s; environment %s" % (args.arm, pattern, ", ".join("%s=%s" % (k, v) for k, v in sorted(os.environ.items()) if k.startswith("MOZ_PROFILER_"))))
    httpd, url = serve("?mode=profile")
    cv.Page.result = None
    cv.kill_firefox()
    err = ""
    try:
        asyncio.run(cv.ours_run(args.binary, url, hunt.arm_prefs(args.arm)))
    except BaseException as exc:   # noqa: BLE001
        err = "%s: %s" % (type(exc).__name__, str(exc)[:300])
    httpd.shutdown()
    print(rp.wait_until_no_firefox(90))
    cv.kill_firefox()
    time.sleep(3)
    page = json.loads(cv.Page.result) if cv.Page.result else {}
    print("the page: %s%s" % (json.dumps(page.get("profile", {}).get("readPixels_ms_p50")) + " ms readPixels p50, " + json.dumps(page.get("profile", {}).get("toDataURL_ms_p50")) + " ms toDataURL p50",
                              ("; ERROR " + err) if err else ""))
    files = sorted(out.glob("profile-*.json"))
    for f in files:
        raw = f.read_bytes()
        try:
            json.loads(raw.decode("utf-8", "replace"))
            state = "valid JSON"
        except ValueError as exc:
            state = "NOT VALID JSON (%s)" % str(exc)[:80]
        print("  %s: %.1f MB, %s" % (f.name, len(raw) / 2 ** 20, state))
        with gzip.open(str(f) + ".gz", "wb", 6) as g:
            g.write(raw)
        f.unlink()
    (out / "page.json").write_text(json.dumps(page, indent=1), encoding="utf-8")
    print("%d profile file(s) in %s" % (len(files), out))
    return 1 if err or not files else 0


# ----------------------------------------------------------------------------------------------------------------------- table

def get(doc: Dict[str, Any], path: str) -> Any:
    cur: Any = doc.get("probe", {})
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def fmt(v: Any) -> str:
    return "-" if v is None else (("%.1f" % v) if isinstance(v, float) else str(v))


def print_arm(doc: Dict[str, Any]) -> None:
    print("-" * 110)
    print("ARM %s%s" % (doc["arm"], ("  prefs passed: %s" % doc["prefs_passed"]) if doc.get("prefs_passed") else ""))
    print("  %-10s %12s %12s %12s %12s %12s %12s" % ("content", "gl read ms", "gl 1px ms", "gl png ms", "2d get ms", "2d png ms", "gl ms/MB"))
    for c in CONTENTS:
        g, d = get(doc, "contents.%s.webgl" % c) or {}, get(doc, "contents.%s.canvas2d" % c) or {}
        rp_ = g.get("readPixels_ms_p50")
        print("  %-10s %12s %12s %12s %12s %12s %12s" % (c, fmt(rp_), fmt(g.get("readPixels_1px_ms_p50")), fmt(g.get("toDataURL_ms_p50")), fmt(d.get("getImageData_ms_p50")),
                                                       fmt(d.get("toDataURL_ms_p50")), fmt(rp_ / MB if rp_ is not None else None)))
    sys.stdout.flush()


def pixel_diff(a: Optional[str], b: Optional[str]) -> Tuple[str, List[Tuple[int, int, int]]]:
    if not a or not b:
        return "missing", []
    x, y = base64.b64decode(a), base64.b64decode(b)
    if len(x) != len(y):
        return "sizes differ", []
    diffs = []
    w = 256
    for i in range(0, len(x), 4):
        d = max(abs(x[i + k] - y[i + k]) for k in range(4))
        if d:
            p = i // 4
            diffs.append((p % w, p // w, d))
    if not diffs:
        return "identical", []
    return "%d of %d pixels differ, largest channel delta %d, first at (%d, %d)" % (len(diffs), len(x) // 4, max(d for _, _, d in diffs), diffs[0][0], diffs[0][1]), diffs


def table(dirs: List[Path]) -> int:
    rounds: List[Dict[str, Dict[str, Any]]] = []
    for d in dirs:
        docs = {a: json.loads((d / ("%s.json" % a)).read_text(encoding="utf-8")) for a in ARMS if (d / ("%s.json" % a)).is_file()}
        if docs:
            rounds.append(docs)
    if not rounds:
        print("no results")
        return 1
    arms = [a for a in ARMS if any(a in r for r in rounds)]
    print("=" * 130)
    print("READBACK COST, 1024x1024, antialias:false: %d round(s), arms %s. ms are p50 of the repeats; one value per round (round 1 / round 2 ...)" % (len(rounds), ", ".join(arms)))
    for label, path_of in (("WebGL readPixels ms", lambda c: "contents.%s.webgl.readPixels_ms_p50" % c), ("WebGL toDataURL ms", lambda c: "contents.%s.webgl.toDataURL_ms_p50" % c),
                           ("WebGL 1-pixel readPixels ms", lambda c: "contents.%s.webgl.readPixels_1px_ms_p50" % c),
                           ("canvas 2D getImageData ms (never projected)", lambda c: "contents.%s.canvas2d.getImageData_ms_p50" % c),
                           ("canvas 2D toDataURL ms (never projected)", lambda c: "contents.%s.canvas2d.toDataURL_ms_p50" % c)):
        print("-" * 130)
        print(label)
        print("  %-10s" % "content" + "".join(" %-22s" % a for a in arms))
        for c in CONTENTS:
            print("  %-10s" % c + "".join(" %-22s" % ("/".join(fmt(get(r[a], path_of(c))) for r in rounds if a in r))[:22] for a in arms))
    print("=" * 130)
    print("COST PER MB of the WebGL readPixels (4 MiB per canvas): ms / 4, round 1; and the cost the projection adds = ours minus ours_noproj")
    r1 = rounds[0]
    print("  %-10s" % "content" + "".join(" %-14s" % a for a in arms) + " %-22s" % "projection ms/MB")
    for c in CONTENTS:
        cells, vals = "", {}
        for a in arms:
            v = get(r1[a], "contents.%s.webgl.readPixels_ms_p50" % c) if a in r1 else None
            vals[a] = v
            cells += " %-14s" % (fmt(v / MB) if v is not None else "-")
        add = (vals["ours"] - vals["ours_noproj"]) / MB if vals.get("ours") is not None and vals.get("ours_noproj") is not None else None
        print("  %-10s" % c + cells + " %-22s" % fmt(add))
    print("  the toDataURL equivalent (ms/MB, projection = ours - ours_noproj):")
    for c in CONTENTS:
        o, n = (get(r1[a], "contents.%s.webgl.toDataURL_ms_p50" % c) if a in r1 else None for a in ("ours", "ours_noproj"))
        print("    %-10s ours %s ms, ours_noproj %s ms -> %s ms/MB" % (c, fmt(o), fmt(n), fmt((o - n) / MB) if o is not None and n is not None else "-"))
    print("=" * 130)
    print("THE VERDICT FOR THE HYPOTHESIS (round 1)")
    for c in ("gradient", "noise", "triangle", "flat"):
        parts = []
        for a in arms:
            if a in r1:
                parts.append("%s %s ms" % (a, fmt(get(r1[a], "contents.%s.webgl.readPixels_ms_p50" % c))))
        print("  WebGL readPixels, %-9s %s" % (c + ":", "; ".join(parts)))
    for c in ("gradient", "noise"):
        parts = ["%s %s ms" % (a, fmt(get(r1[a], "contents.%s.canvas2d.getImageData_ms_p50" % c))) for a in arms if a in r1]
        print("  canvas 2D getImageData, %-9s %s" % (c + ":", "; ".join(parts)))
    print("=" * 130)
    print("WHAT A PAGE READS: hashes of the bytes per content (WebGL readPixels), round 1; a difference is a different answer to the same question")
    print("  %-10s" % "content" + "".join(" %-20s" % a for a in arms))
    for c in CONTENTS:
        print("  %-10s" % c + "".join(" %-20s" % fmt(get(r1[a], "contents.%s.webgl.pixels_hash" % c)) if a in r1 else " %-20s" % "-" for a in arms))
    print("  canvas 2D (never projected):")
    for c in CONTENTS:
        print("  %-10s" % c + "".join(" %-20s" % fmt(get(r1[a], "contents.%s.canvas2d.pixels_hash" % c)) if a in r1 else " %-20s" % "-" for a in arms))
    print("=" * 130)
    print("THE DIAGONAL-EDGE SCENE (antialias:false, 256x256, readPixels), pixel by pixel, round 1: is the 1-level difference of ours against retail the projection?")
    for a, b in (("ours", "retail"), ("ours_noproj", "retail"), ("ours", "ours_noproj"), ("ours", "stockheaded"), ("ours_noproj", "stockheaded"), ("stockheaded", "retail")):
        if a in r1 and b in r1:
            text, diffs = pixel_diff(get(r1[a], "scene.readPixels_b64"), get(r1[b], "scene.readPixels_b64"))
            print("  %-12s against %-12s: %s" % (a, b, text))
    for d in dirs:
        for a in arms:
            fd = d / ("%s-facts" % a)
            if fd.is_dir():
                print_facts(a, fd)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "facts", "profile"):
        p = sub.add_parser(name)
        p.add_argument("--arm", choices=ARMS, required=True)
        p.add_argument("--binary", default=os.environ.get("INVPW_BINARY_PATH", ""))
        p.add_argument("--retail-from", default="")
        p.add_argument("--out", type=Path, required=True)
    t = sub.add_parser("table")
    t.add_argument("dirs", type=Path, nargs="+")
    args = ap.parse_args()
    if args.cmd == "table":
        return table(args.dirs)
    if args.arm.startswith("ours") and not args.binary:
        ap.error("the ours arms need --binary (or INVPW_BINARY_PATH)")
    if args.arm == "retail" and not args.retail_from and args.cmd != "profile":
        ap.error("the arm retail needs --retail-from (hunt_first_run.py fetch-retail)")
    if args.cmd == "profile" and not args.arm.startswith("ours"):
        ap.error("profile is for the ours arms")
    return {"run": run, "facts": facts, "profile": profile}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
