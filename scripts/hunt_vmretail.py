"""HUNT ONLY (branch hunt/cpu-render-path, never merged): make OUR browser take the retail decisions of this VM, from the hunt only.

    python scripts/hunt_vmretail.py build --binary "$FF" --out DIR      # reads ours and stock, writes DIR/prefs.json, prints the table
    python scripts/hunt_vmretail.py prefs-json DIR/prefs.json           # the prefs as one JSON line (HUNT_EXTRA_PREFS for the pytest plugin)

WHAT STOCK DOES HERE. On the Windows VM runner the adapter is Microsoft Hyper-V Video; stock Firefox's GfxInfo blocks D3D11 compositing and
WebRender with FEATURE_FAILURE_VM_VENDOR and falls back to WebRender (Software) with no D3D11. Ours declares OK for these features through
`zoom.stealth.gfx.feature_status` (the core's declaration of the persona: NAME|STATUS[|FAILURE] for every live feature, answered by the engine's
StealthGfxDeclaredStatus before any blocklist is read), so ours takes the hardware path on the same adapter.

WHAT THIS DOES. `build` launches
  1. ours with Marionette (chrome scope) and reads the DECLARED table: the pref zoom.stealth.gfx.feature_status, as the core wrote it;
  2. stock Playwright's firefox, headed, with Marionette, and asks nsIGfxInfo.getFeatureStatusStr(NAME) for every feature of that table: the
     status and the failure id Mozilla's own decision gives on this machine;
then prints, for EVERY feature, declared against stock, and builds the extra_prefs of the arm `ours_vmretail`:
  - zoom.stealth.gfx.feature_status: the declared table with ONLY the compositing features replaced by stock's decision (DIRECT3D_11_LAYERS, GPU_PROCESS,
    WEBRENDER, WEBRENDER_*, D3D11_KEYED_MUTEX, DX_INTEROP2, CANVAS2D_ACCELERATION, ACCELERATED_CANVAS2D). The WebGL features (WEBGL*, WEBGPU,
    DIRECT3D_11_ANGLE), the media ones and everything else keep what the persona declares: the page-readable WebGL values stay as declared.
  - gfx.webrender.software = true            (WebRender's software backend, explicit)
  - gfx.webrender.software.d3d11 = false     (no D3D11 compositor for it: "WebRender (Software)", not "Software D3D11")
  - webgl.angle.force-warp = true            (WebGL on ANGLE with an explicit WARP device)
Everything is passed through extra_prefs by hunt_cpu_per_browser.arm_prefs; the product is not touched. `support --arm ours_vmretail` then reads the
effective prefs and Troubleshoot back and says what took effect.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hunt_cpu_render_path as rp   # noqa: E402

#: the features that decide where and how the page is composited: replaced by stock's decision
COMPOSITING = re.compile(r"^(DIRECT3D_9_LAYERS|DIRECT3D_10_LAYERS|DIRECT3D_10_1_LAYERS|DIRECT3D_11_LAYERS|GPU_PROCESS|WEBRENDER[A-Z_]*|D3D11_KEYED_MUTEX|"
                         r"DX_INTEROP2|CANVAS2D_ACCELERATION|ACCELERATED_CANVAS2D)$")
STATUS = {"STATUS_OK": 1, "STATUS_UNKNOWN": 2, "BLOCKED_DRIVER_VERSION": 3, "BLOCKED_DEVICE": 4, "DISCOURAGED": 5, "BLOCKED_OS_VERSION": 6,
          "BLOCKED_MISMATCHED_VERSION": 7, "DENIED": 8, "ALLOW_ALWAYS": 9, "ALLOW_QUALIFIED": 10, "BLOCKED_PLATFORM_TEST": 11}
NUMBER_TO_NAME = {v: k for k, v in STATUS.items()}
WEBGL_PREFS = {"gfx.webrender.software": True, "gfx.webrender.software.d3d11": False, "webgl.angle.force-warp": True}

DECLARED_JS = """
let cb = arguments[arguments.length - 1];
let out = {};
for (let n of ["zoom.stealth.gfx.feature_status"]) {
  try { out[n] = Services.prefs.getStringPref(n); } catch (e) { out[n] = null; }
}
out.version = Services.appinfo.version;
cb(JSON.stringify(out));
"""

STOCK_JS = """
let cb = arguments[arguments.length - 1];
let names = arguments[0];
let gi = Cc["@mozilla.org/gfx/info;1"].getService(Ci.nsIGfxInfo);
let out = {version: Services.appinfo.version, features: {}};
for (let n of names) {
  try { let f = {}; let st = gi.getFeatureStatusStr(n, f); out.features[n] = [st, f.value || ""]; }
  catch (e) { out.features[n] = ["ERR", String(e).slice(0, 120)]; }
}
cb(JSON.stringify(out));
"""


def chrome(js: str, args: List[Any]) -> Dict[str, Any]:
    m = rp.Marionette()
    try:
        m.call("WebDriver:NewSession", {"capabilities": {"alwaysMatch": {}}})
        m.call("WebDriver:SetTimeouts", {"script": 120000})
        m.call("Marionette:SetContext", {"value": "chrome"})
        res = rp.marionette_value(m.call("WebDriver:ExecuteAsyncScript", {"script": js, "args": args}))
        return json.loads(res) if isinstance(res, str) else res
    finally:
        m.close()


async def ask(arm: str, binary: str, js: str, args: List[Any]) -> Dict[str, Any]:
    rp.kill_firefox()
    try:
        async with rp.launched(arm, binary, marionette=True) as browser:
            page = await browser.new_page()
            await page.goto("about:blank")
            return await asyncio.get_running_loop().run_in_executor(None, chrome, js, args)
    finally:
        rp.kill_firefox()


def parse_declaration(text: str) -> List[List[str]]:
    return [e.split("|") for e in text.split(",") if e]


def build(binary: str, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    declared_res = asyncio.run(ask("ours", binary, DECLARED_JS, []))
    text = declared_res.get("zoom.stealth.gfx.feature_status")
    print("ours (engine %s): zoom.stealth.gfx.feature_status %s" % (declared_res.get("version"), ("is %d characters" % len(text)) if text else "NOT READABLE"))
    if not text:
        print("cannot build the arm without the declared table")
        return 1
    table = parse_declaration(text)
    names = [e[0] for e in table]
    stock_res = asyncio.run(ask("stockheaded", "", STOCK_JS, [names]))
    stock = stock_res.get("features", {})
    print("stock (Firefox %s) answered for %d of %d features" % (stock_res.get("version"), len([n for n in names if stock.get(n, ["ERR"])[0] != "ERR"]), len(names)))
    new_entries: List[str] = []
    overridden: List[str] = []
    print("=" * 100)
    print("%-36s %-34s %-34s %s" % ("feature", "declared by the persona (ours)", "decided by stock on this runner", "in ours_vmretail"))
    for e in table:
        name = e[0]
        decl = "%s%s" % (NUMBER_TO_NAME.get(int(e[1]), e[1]), ("|" + e[2]) if len(e) > 2 else "")
        st = stock.get(name)
        stock_text = "-" if not st else ("%s%s" % (st[0], ("|" + st[1]) if st[1] else ""))
        keep = "|".join(e)
        verdict = "declared"
        if COMPOSITING.match(name) and st and st[0] in STATUS:
            number = STATUS[st[0]]
            ok_class = number == 1
            failure = st[1] or ("" if ok_class else "FEATURE_FAILURE_VM_VENDOR")
            candidate = "%s|%d%s" % (name, number, ("|" + failure) if failure and not ok_class else "")
            if number in (8, 9, 10):
                verdict = "declared (stock status %s cannot be declared for this feature)" % st[0]
            else:
                if candidate != keep:
                    overridden.append(name)
                    verdict = "STOCK'S"
                keep = candidate
        elif COMPOSITING.match(name):
            verdict = "declared (stock gave no answer)"
        new_entries.append(keep)
        differs = " <-- differs" if st and st[0] in STATUS and (STATUS[st[0]], (st[1] or "")) != (int(e[1]), e[2] if len(e) > 2 else "") else ""
        print("%-36s %-34s %-34s %s%s" % (name, decl, stock_text, verdict, differs))
    prefs: Dict[str, Any] = {"zoom.stealth.gfx.feature_status": ",".join(new_entries)}
    prefs.update(WEBGL_PREFS)
    (out / "prefs.json").write_text(json.dumps({"prefs": prefs, "overridden_features": overridden, "ours_engine": declared_res.get("version"),
                                                "stock_version": stock_res.get("version")}, indent=1), encoding="utf-8")
    print("=" * 100)
    print("ARM ours_vmretail: the extra_prefs passed by the hunt (%s):" % (out / "prefs.json"))
    for k, v in prefs.items():
        if k == "zoom.stealth.gfx.feature_status":
            print("  %s = <the declared table with %d feature(s) replaced by stock's decision: %s>" % (k, len(overridden), ", ".join(overridden) or "none"))
        else:
            print("  %s = %s" % (k, json.dumps(v)))
    if not overridden:
        print("WARNING: stock's decisions are the declared ones for every compositing feature: nothing of the table changed (the three prefs still apply)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--binary", default=os.environ.get("INVPW_BINARY_PATH", ""))
    b.add_argument("--out", type=Path, required=True)
    j = sub.add_parser("prefs-json")
    j.add_argument("file", type=Path)
    args = ap.parse_args()
    if args.cmd == "prefs-json":
        print(json.dumps(json.loads(args.file.read_text(encoding="utf-8"))["prefs"], separators=(",", ":")))
        return 0
    if not args.binary:
        ap.error("--binary (or INVPW_BINARY_PATH)")
    return build(args.binary, args.out)


if __name__ == "__main__":
    sys.exit(main())
