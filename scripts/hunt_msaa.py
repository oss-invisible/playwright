"""HUNT ONLY (branch hunt/cpu-render-path, never merged): does our forced multisample make WebGL readPixels slow, and does MSAA change pixels a page can read?

    python scripts/hunt_msaa.py run   --arm ours|ours_nomsaa|stockheaded|retail [--binary B] [--retail-from DIR] --out DIR
    python scripts/hunt_msaa.py facts --arm ... --out DIR                 # effective webgl.msaa-* prefs, Troubleshoot's WebGL lines
    python scripts/hunt_msaa.py table DIR [DIR ...]                       # one DIR per round

MEASURED: WebGL readPixels of 1024x1024 takes ~400 ms (p50) in ours and in ours_vmretail, ~89 ms in stock and in retail 159.
HYPOTHESIS: the multisample we force. GLContext.cpp:783-787 marks framebuffer_multisample unsupported on the Microsoft Basic Render Driver (bug 978966); our hook at
GLContext.cpp:799 (StealthDeclaredMultisample, gfx/gl/StealthGLMultisample.cpp:24-26) turns it back on whenever webgl.msaa-samples > 0; the core sets webgl.msaa-samples=4 and
webgl.msaa-force=true on Windows (invisible_core/prefs.py:1263-1264). The default framebuffer is then multisampled (WebGLContext.cpp:461-465, MozFramebuffer::Create with
mMsaaSamples) and every readPixels resolves it, in software on WARP.

THE ARMS (one browser each; ours, ours_nomsaa and stockheaded through the clients of hunt_cpu_baseline.py, retail by hand: Mozilla's 159.0, subprocess, -no-remote, a fresh profile,
the page posts its result to the local server):
  ours          the declared persona (webgl.msaa-samples=4, webgl.msaa-force=true)
  ours_nomsaa   ours with webgl.msaa-samples=0 and webgl.msaa-force=false, passed ONLY by the hunt (hunt_cpu_per_browser.ARM_PREFS). With samples 0 Mozilla turns antialias off
                for every context (dom/canvas/ClientWebGLContext.cpp:1091-1094): a page reads antialias:false and SAMPLES 0
  stockheaded   stock Playwright's firefox, headless=False
  retail        Mozilla's Firefox 159.0
WHAT THE PAGE MEASURES (scripts/hunt_msaa_probe.js), in each browser: readPixels / toDataURL timing of a 1024x1024 context with antialias:true and with antialias:false in the SAME
browser (if the multisample resolve is the cost, the antialias:false context is fast where the antialias:true one is slow); a 256x256 scene with diagonal edges (a rotated triangle,
thin rotated lines and slivers) in both, read back with readPixels AND toDataURL, edge pixels hashed apart from interior pixels (an interior pixel equals its eight neighbours in the
antialias:false image), antialiased pixels counted; getContextAttributes().antialias, SAMPLES, SAMPLE_BUFFERS, MAX_SAMPLES and the RGBA8 sample counts. `table` also compares the
raw readPixels of the antialias:true scene between arms pixel by pixel.

WHAT RETAIL DOES, from the sources (this tree): the default of the attribute is webgl.default-antialias (StaticPrefList.yaml:20451, true off Android); webgl.msaa-samples defaults to 4
(StaticPrefList.yaml:20650); samples 0 forces antialias false (ClientWebGLContext.cpp:1091-1094); the default framebuffer is created with those samples (WebGLContext.cpp:461-465)
and MozFramebuffer::Create returns null when the GL has no framebuffer_multisample or MAX_SAMPLES is lower (MozFramebuffer.cpp:30-37), in which case the context falls back to no
samples (WebGLContext.cpp:467-472) and `mOptions.antialias &= bool(mDefaultFB->mSamples)` (WebGLContext.cpp:670) makes the attribute false. So: on a real GPU through ANGLE/D3D11,
framebuffer_multisample is supported (ANGLE_framebuffer_multisample, GLContext.cpp:83), the renderer is not the Basic Render Driver, and retail has MSAA on (antialias true, SAMPLES
4 or the GPU's MAX_SAMPLES if lower); on the Basic Render Driver (a VM, this runner) retail marks it unsupported (GLContext.cpp:783-787) and a page reads antialias:false, SAMPLES 0.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
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

ARMS = ["ours", "ours_nomsaa", "stockheaded", "retail"]


class MsaaPage(cv.Page):
    """The page of this hunt: same server as hunt_compositor_visible, the probe of this file."""

    def do_GET(self):   # noqa: N802
        if self.path.startswith("/probe.js"):
            self._send((HERE / "hunt_msaa_probe.js").read_bytes(), "text/javascript; charset=utf-8")
        else:
            self._send(b'<!doctype html><meta charset=utf-8><title>msaa</title><body style="margin:0"><script src="/probe.js"></script></body>', "text/html; charset=utf-8")


def serve() -> Tuple[ThreadingHTTPServer, str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), MsaaPage)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:%d/" % httpd.server_address[1]


def run(args) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    httpd, url = serve()
    cv.Page.result = None
    cv.kill_firefox()
    note, err = "", ""
    t0 = time.time()
    try:
        if args.arm == "retail":
            exe, note = cv.retail_exe(args)
            profile = Path(tempfile.mkdtemp(prefix="hunt-msaa-retail-"))
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
    doc = {"arm": args.arm, "note": note, "error": err, "wall_s": time.time() - t0, "probe": data,
           "prefs_passed": hunt.arm_prefs(args.arm) if args.arm.startswith("ours") and args.arm != "ours_vmretail" else None}
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

    async def ours_like():
        async with rp.launched(args.arm, args.binary or "", marionette=True) as browser:
            page = await browser.new_page()
            await page.goto("about:blank")
            await asyncio.get_running_loop().run_in_executor(None, rp.privileged_route, out, report)

    try:
        if args.arm == "retail":
            exe, note = cv.retail_exe(args)
            profile = Path(tempfile.mkdtemp(prefix="hunt-msaa-facts-"))
            proc = subprocess.Popen([str(exe), "-no-remote", "-marionette", "-remote-allow-system-access", "-profile", str(profile), "about:blank"],
                                    env=dict(os.environ, MOZ_REMOTE_ALLOW_SYSTEM_ACCESS="1"), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                rp.privileged_route(out, report)
            finally:
                cv.kill_firefox()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=10)
        else:
            asyncio.run(ours_like())
    except BaseException as exc:   # noqa: BLE001
        report["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:300])
    cv.kill_firefox()
    print_facts(args.arm, out)
    return 0


def print_facts(arm: str, out: Path) -> None:
    print("-" * 110)
    print("FACTS of %s: the multisample prefs as the browser reads them, and what Troubleshoot says of WebGL" % arm)
    pf = out / "effective_prefs.json"
    if pf.is_file():
        prefs = json.loads(pf.read_text(encoding="utf-8"))
        for n in sorted(prefs):
            if n.startswith(("webgl.msaa", "webgl.default-antialias", "webgl.angle", "webgl.disable", "webgl.force", "gfx.webrender.software", "webgl.out-of-process")):
                print("  pref %s = %s (user-set: %s)" % (n, prefs[n][1], prefs[n][0]))
    else:
        print("  effective_prefs.json missing")
    sf = out / "troubleshoot.json"
    if sf.is_file():
        g = json.loads(sf.read_text(encoding="utf-8")).get("graphics", {})
        for k in ("webgl1Renderer", "webgl1Version", "webgl1DriverRenderer", "webgl2Renderer", "webgl2Version", "webgl1Extensions", "windowLayerManagerType", "webgl1WSIInfo"):
            if k in g:
                print("  Troubleshoot %s: %s" % (k, str(g[k])[:200]))
    else:
        print("  troubleshoot.json missing")


# ------------------------------------------------------------------------------------------------------------------------ table

def get(doc: Dict[str, Any], path: str) -> Any:
    cur: Any = doc.get("probe", {})
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


TIMING = [
    ("readPixels 1024 first ms, antialias:true", "timing.antialias_true.first_readPixels_ms"),
    ("readPixels 1024 repeat p50 ms, antialias:true", "timing.antialias_true.repeat_readPixels_ms_p50"),
    ("draw then 1px readPixels p50 ms, antialias:true", "timing.antialias_true.draw_then_1px_readPixels_ms_p50"),
    ("toDataURL png 1024 ms, antialias:true", "timing.antialias_true.toDataURL_png_ms"),
    ("readPixels 1024 first ms, antialias:false", "timing.antialias_false.first_readPixels_ms"),
    ("readPixels 1024 repeat p50 ms, antialias:false", "timing.antialias_false.repeat_readPixels_ms_p50"),
    ("draw then 1px readPixels p50 ms, antialias:false", "timing.antialias_false.draw_then_1px_readPixels_ms_p50"),
    ("toDataURL png 1024 ms, antialias:false", "timing.antialias_false.toDataURL_png_ms"),
]
VALUES = [
    ("getContextAttributes().antialias, requested true (webgl)", "attributes.webgl.antialias_true.attributes_antialias"),
    ("getContextAttributes().antialias, requested false (webgl)", "attributes.webgl.antialias_false.attributes_antialias"),
    ("SAMPLES, requested true (webgl)", "attributes.webgl.antialias_true.SAMPLES"),
    ("SAMPLE_BUFFERS, requested true (webgl)", "attributes.webgl.antialias_true.SAMPLE_BUFFERS"),
    ("getContextAttributes().antialias, requested true (webgl2)", "attributes.webgl2.antialias_true.attributes_antialias"),
    ("SAMPLES, requested true (webgl2)", "attributes.webgl2.antialias_true.SAMPLES"),
    ("MAX_SAMPLES (webgl2)", "attributes.webgl2.antialias_true.MAX_SAMPLES"),
    ("RGBA8 renderbuffer sample counts (webgl2)", "attributes.webgl2.antialias_true.RGBA8_samples"),
    ("unmasked renderer (webgl)", "attributes.webgl.antialias_true.UNMASKED_RENDERER"),
    ("scene: edge pixels (from the antialias:false image)", "scene.edge_pixels"),
    ("scene, antialias:true, antialiased pixels (readPixels)", "scene.aa_true.readPixels.antialiased_pixels"),
    ("scene, antialias:true, edge hash (readPixels)", "scene.aa_true.readPixels.edge_hash"),
    ("scene, antialias:true, interior hash (readPixels)", "scene.aa_true.readPixels.interior_hash"),
    ("scene, antialias:true, edge hash (toDataURL decoded)", "scene.aa_true.toDataURL_decoded.edge_hash"),
    ("scene, antialias:true, interior hash (toDataURL decoded)", "scene.aa_true.toDataURL_decoded.interior_hash"),
    ("scene, antialias:true, toDataURL pixels == readPixels", "scene.aa_true.png_equals_readPixels"),
    ("scene, antialias:false, antialiased pixels (readPixels)", "scene.aa_false.readPixels.antialiased_pixels"),
    ("scene, antialias:false, edge hash (readPixels)", "scene.aa_false.readPixels.edge_hash"),
    ("scene, antialias:false, interior hash (readPixels)", "scene.aa_false.readPixels.interior_hash"),
    ("scene, antialias:true against antialias:false: pixels that differ", "scene.aa_true_against_aa_false_readPixels.differing_pixels"),
    ("  ... of them at edges", "scene.aa_true_against_aa_false_readPixels.at_edges"),
    ("  ... of them in the interior", "scene.aa_true_against_aa_false_readPixels.in_interior"),
    ("  ... largest channel delta", "scene.aa_true_against_aa_false_readPixels.largest_delta"),
]


def pixel_diff(a: Optional[str], b: Optional[str]) -> str:
    if not a or not b:
        return "missing"
    x, y = base64.b64decode(a), base64.b64decode(b)
    if len(x) != len(y):
        return "sizes differ"
    n, mx = 0, 0
    for i in range(0, len(x), 4):
        d = max(abs(x[i + k] - y[i + k]) for k in range(4))
        if d:
            n += 1
            mx = max(mx, d)
    return "identical" if n == 0 else "%d of %d pixels differ, largest channel delta %d" % (n, len(x) // 4, mx)


def fmt(v: Any) -> str:
    return "-" if v is None else (json.dumps(v) if isinstance(v, (list, dict)) else str(v))


def print_arm(doc: Dict[str, Any]) -> None:
    print("-" * 110)
    print("ARM %s%s%s" % (doc["arm"], (" (" + doc["note"][:80] + ")") if doc.get("note") else "", ("  prefs passed: %s" % doc["prefs_passed"]) if doc.get("prefs_passed") else ""))
    for label, path in TIMING + VALUES:
        print("  %-70s %s" % (label, fmt(get(doc, path))))
    sys.stdout.flush()


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
    print("MSAA AND readPixels: %d round(s), arms %s" % (len(rounds), ", ".join(arms)))
    print("=" * 130)
    print("TIMING (ms; one value per round, round 1 / round 2 ...)")
    print("%-62s" % "" + "".join(" %-24s" % a for a in arms))
    for label, path in TIMING:
        print("%-62s" % label[:62] + "".join(" %-24s" % ("/".join(fmt(get(r[a], path)) for r in rounds if a in r))[:24] for a in arms))
    print("=" * 130)
    print("VALUES (what the page reads; round 1)")
    r1 = rounds[0]
    print("%-62s" % "" + "".join(" %-22s" % a for a in arms))
    for label, path in VALUES:
        print("%-62s" % label[:62] + "".join(" %-22s" % fmt(get(r1[a], path))[:22] if a in r1 else " %-22s" % "-" for a in arms))
    print("=" * 130)
    print("THE ANTIALIASED SCENE, pixel by pixel (readPixels of the antialias:true context, round 1)")
    pairs = [("ours", "retail"), ("ours_nomsaa", "retail"), ("ours", "ours_nomsaa"), ("ours", "stockheaded"), ("stockheaded", "retail")]
    for a, b in pairs:
        if a in r1 and b in r1:
            print("  %-12s against %-12s: antialias:true image: %s; antialias:false image: %s" % (a, b,
                  pixel_diff(get(r1[a], "scene.aa_true_readPixels_b64"), get(r1[b], "scene.aa_true_readPixels_b64")),
                  pixel_diff(get(r1[a], "scene.aa_false_readPixels_b64"), get(r1[b], "scene.aa_false_readPixels_b64"))))
    print("=" * 130)
    print("WHAT DIFFERS (VALUES read by a page), against retail and ours against ours_nomsaa")
    for a, b in (("ours", "retail"), ("ours_nomsaa", "retail"), ("ours", "ours_nomsaa")):
        if a in r1 and b in r1:
            diff = [(l, get(r1[a], p), get(r1[b], p)) for l, p in VALUES if get(r1[a], p) != get(r1[b], p)]
            print("  %s AGAINST %s: %d value(s) differ" % (a.upper(), b.upper(), len(diff)))
            for l, x, y in diff:
                print("      %-70s %s | %s" % (l[:70], fmt(x)[:30], fmt(y)[:30]))
    print("=" * 130)
    print("THE HYPOTHESIS, per arm and round: readPixels 1024 repeat p50, antialias:true against antialias:false in the SAME browser")
    for a in arms:
        cells = []
        for r in rounds:
            if a in r:
                t, f = get(r[a], "timing.antialias_true.repeat_readPixels_ms_p50"), get(r[a], "timing.antialias_false.repeat_readPixels_ms_p50")
                cells.append("%s / %s ms%s" % (fmt(t), fmt(f), (" = x%.1f" % (t / f)) if t and f else ""))
        print("  %-12s %s" % (a, "; ".join(cells)))
    for d in dirs:
        for a in arms:
            fd = d / ("%s-facts" % a)
            if fd.is_dir():
                print_facts(a, fd)
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
    t.add_argument("dirs", type=Path, nargs="+")
    args = ap.parse_args()
    if args.cmd == "table":
        return table(args.dirs)
    if args.arm.startswith("ours") and not args.binary:
        ap.error("the ours arms need --binary (or INVPW_BINARY_PATH)")
    if args.arm == "retail" and not args.retail_from:
        ap.error("the arm retail needs --retail-from (hunt_first_run.py fetch-retail)")
    return run(args) if args.cmd == "run" else facts(args)


if __name__ == "__main__":
    sys.exit(main())
