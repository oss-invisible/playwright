"""HUNT ONLY (branch hunt/cpu-render-path, never merged): does ANY byte a page reads change when our browser runs software WebRender?

The arm `ours_swwr` is `ours` with gfx.webrender.software=true, passed only here through extra_prefs (scripts/hunt_cpu_per_browser.ARM_PREFS).
The wrapper's own byte suites cannot take a pref, so two instruments:

    python scripts/hunt_page_bytes.py collect --arm ours      --binary "$FF" --out DIR [--launches 2]
    python scripts/hunt_page_bytes.py collect --arm ours_swwr --binary "$FF" --out DIR [--launches 2]
    python scripts/hunt_page_bytes.py collect --arm ours_vmretail --binary "$FF" --out DIR     # needs HUNT_VMRETAIL_PREFS (scripts/hunt_vmretail.py)
    python scripts/hunt_page_bytes.py diff DIR [--a ours --b ours_swwr]
    python scripts/hunt_page_bytes.py suites A.xml B.xml [--a ours --b ours_swwr]

collect  launches the arm `--launches` times (same seed 42 every time), loads a local page, and evaluates scripts/hunt_page_bytes_probe.js: canvas 2D
         (getImageData, toDataURL png/jpeg/webp, toBlob, OffscreenCanvas, measureText), WebGL 1 and 2 (VENDOR, RENDERER, unmasked strings, the
         extensions and the objects they give, limits, precision formats, readPixels of 10+ scenes, toDataURL, drawImage of the canvas, MSAA resolve,
         float targets), an OffscreenCanvas WebGL in a worker, WebGPU adapter facts, the boxes of text in 14 fonts, wrapped paragraphs, system colours.
         One gzipped JSON per launch: DIR/<arm>-<n>.json.gz.
diff     compares the arms key by key. Launching the same arm twice says which keys are NOT stable by themselves (the noise of the seed, a font
         that loads late): those are listed apart and do not count. For every pixel key that differs it says how many bytes, the largest delta and
         the first pixel. The last line is the answer: "ANY byte differs: YES/NO".
suites   reads the two JUnit files of the wrapper's byte suites run under both arms (pytest -p hunt_extra_prefs_plugin, HUNT_EXTRA_PREFS) and
         prints each test's outcome in both. A test that passes in one arm and not in the other is the finding; both passing means both arms read
         the bytes of the retail references those tests hold.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import sys
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from hunt_cpu_per_browser import arm_prefs   # noqa: E402

SEED = 42


class Blank(BaseHTTPRequestHandler):
    def do_GET(self):   # noqa: N802
        body = b"<!doctype html><meta charset=utf-8><title>probe</title><body></body>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):   # noqa: D102
        pass


def collect_once(arm: str, binary: str) -> Dict[str, Any]:
    from invisible_playwright import InvisiblePlaywright
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Blank)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/" % httpd.server_address[1]
    expression = (HERE / "hunt_page_bytes_probe.js").read_text(encoding="utf-8")
    try:
        with InvisiblePlaywright(seed=SEED, binary_path=binary, headless=True, locale="en-US", timezone="Europe/Rome",
                                 extra_prefs=arm_prefs(arm)) as browser:
            page = browser.new_page()
            page.goto(url, wait_until="load", timeout=60000)
            return page.evaluate(expression)
    finally:
        httpd.shutdown()


def collect(arm: str, binary: str, out: Path, launches: int) -> int:
    out.mkdir(parents=True, exist_ok=True)
    rc = 0
    for n in range(1, launches + 1):
        try:
            data = collect_once(arm, binary)
        except BaseException as exc:   # noqa: BLE001
            print("arm %s launch %d: FAILED %s: %s" % (arm, n, type(exc).__name__, str(exc)[:400]))
            rc = 1
            continue
        path = out / ("%s-%d.json.gz" % (arm, n))
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(data, f)
        errors = sorted(k for k in data if k.endswith(".error"))
        pixel = [k for k, v in data.items() if "b64" in v or "w" in v]
        print("arm %s launch %d: %d keys, %d pixel/byte keys, %d scene error(s)%s -> %s (%.1f MB)"
              % (arm, n, len(data), len(pixel), len(errors), (": " + ", ".join("%s=%s" % (k, data[k].get("s")) for k in errors)) if errors else "",
                 path, path.stat().st_size / 2 ** 20))
        for k in ("gl1.unmasked", "gl2.unmasked", "gl1.context", "gl2.context", "gpu.webgpu.info", "env.dpr"):
            if k in data:
                print("  %s = %s" % (k, str(data[k].get("s", data[k]))[:200]))
        sys.stdout.flush()
    return rc


# -------------------------------------------------------------------------------------------------------------------- diff

def load_runs(root: Path, arm: str) -> List[Dict[str, Any]]:
    runs = []
    for p in sorted(root.glob("%s-*.json.gz" % arm)):
        with gzip.open(p, "rt", encoding="utf-8") as f:
            runs.append(json.load(f))
    return runs


def ident(v: Dict[str, Any]) -> str:
    """What identifies a value: its string, or the hash of its bytes."""
    return ("s:" + v["s"]) if "s" in v else ("h:%s/%s" % (v.get("h"), v.get("n")))


def short(v: Dict[str, Any], limit: int = 160) -> str:
    return (v["s"] if "s" in v else "%s bytes, hash %s%s" % (v.get("n"), v.get("h"), (", starts " + v["s0"][:60]) if v.get("s0") else ""))[:limit]


def byte_diff(a: Dict[str, Any], b: Dict[str, Any]) -> str:
    if "b64" not in a or "b64" not in b:
        return "bytes not kept (larger than 400 KiB)"
    x, y = base64.b64decode(a["b64"]), base64.b64decode(b["b64"])
    if len(x) != len(y):
        return "different lengths %d / %d" % (len(x), len(y))
    n, mx, first = 0, 0, None
    for i, (p, q) in enumerate(zip(x, y)):
        if p != q:
            n += 1
            mx = max(mx, abs(p - q))
            if first is None:
                first = i
    bpp, w = a.get("bpp", 4), a.get("w")
    where = ""
    if first is not None and w:
        px = first // bpp
        where = ", first at pixel (%d, %d)" % (px % w, px // w)
    return "%d of %d bytes differ, largest delta %d%s" % (n, len(x), mx, where)


def diff(root: Path, arm_a: str, arm_b: str) -> int:
    ra, rb = load_runs(root, arm_a), load_runs(root, arm_b)
    print("=" * 100)
    print("PAGE-READABLE BYTES, %s (%d launch(es)) against %s (%d launch(es))" % (arm_a, len(ra), arm_b, len(rb)))
    if not ra or not rb:
        print("no data for %s" % (arm_a if not ra else arm_b))
        return 1
    keys = sorted(set().union(*[set(r) for r in ra + rb]))
    unstable, differ, same, missing = [], [], [], []
    for k in keys:
        va = [r[k] for r in ra if k in r]
        vb = [r[k] for r in rb if k in r]
        if len(va) != len(ra) or len(vb) != len(rb):
            missing.append((k, len(va), len(vb)))
            continue
        sa, sb = {ident(v) for v in va}, {ident(v) for v in vb}
        if len(sa) > 1 or len(sb) > 1:
            unstable.append((k, len(sa), len(sb), bool(sa & sb)))
        elif sa != sb:
            differ.append(k)
        else:
            same.append(k)
    print("%d keys: %d identical in both arms, %d DIFFER, %d not stable inside an arm (listed apart), %d missing in a launch"
          % (len(keys), len(same), len(differ), len(unstable), len(missing)))
    for prefix in ("env", "c2d", "gl1", "gl2", "worker", "gpu", "dom"):
        ks = [k for k in keys if k.split(".")[0] == prefix]
        if ks:
            print("  %-7s %3d keys, %3d identical, %3d differ" % (prefix, len(ks), len([k for k in ks if k in same]), len([k for k in ks if k in differ])))
    if differ:
        print("-" * 100)
        print("KEYS THAT DIFFER between %s and %s (launch 1 of each shown):" % (arm_a, arm_b))
        for k in differ:
            va, vb = ra[0][k], rb[0][k]
            print("  %s" % k)
            if "b64" in va or "w" in va:
                print("      %s" % byte_diff(va, vb))
            print("      %s: %s" % (arm_a, short(va)))
            print("      %s: %s" % (arm_b, short(vb)))
    if unstable:
        print("-" * 100)
        print("NOT STABLE inside an arm (the same arm launched again gave another value; not counted): key (values in %s, in %s, overlap)" % (arm_a, arm_b))
        for k, na, nb, overlap in unstable:
            print("  %s (%d, %d, %s)" % (k, na, nb, "overlap" if overlap else "no overlap"))
    if missing:
        print("-" * 100)
        print("MISSING in some launch: " + ", ".join("%s (%d/%d)" % m for m in missing))
    errs = [k for k in keys if k.endswith(".error")]
    if errs:
        print("-" * 100)
        print("scenes that threw (the message is compared like any value): " + ", ".join(errs))
    print("-" * 100)
    print("ANY byte a page reads differs between %s and %s: %s (%d key(s) differ of %d compared; %d unstable keys left out)"
          % (arm_a, arm_b, "YES" if differ else "NO", len(differ), len(same) + len(differ), len(unstable)))
    return 0


# ------------------------------------------------------------------------------------------------------------------ suites

def junit(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for tc in ET.parse(path).getroot().iter("testcase"):
        name = "%s::%s" % (tc.get("classname"), tc.get("name"))
        state = "passed"
        for child in tc:
            if child.tag in ("failure", "error"):
                state = "FAILED: " + (child.get("message") or "")[:160].replace("\n", " ")
            elif child.tag == "skipped":
                state = "skipped: " + (child.get("message") or "")[:100].replace("\n", " ")
        out[name] = state
    return out


def suites(xa: Path, xb: Path, arm_a: str, arm_b: str) -> int:
    print("=" * 100)
    print("THE WRAPPER'S BYTE SUITES under %s and under %s (JUnit)" % (arm_a, arm_b))
    try:
        a, b = junit(xa), junit(xb)
    except (OSError, ET.ParseError) as exc:
        print("cannot read the JUnit files: %s: %s" % (type(exc).__name__, exc))
        return 1
    names = sorted(set(a) | set(b))

    def kind(s: Optional[str]) -> str:
        return "missing" if s is None else s.split(":")[0].lower()
    counts = {}
    for n in names:
        key = (kind(a.get(n)), kind(b.get(n)))
        counts[key] = counts.get(key, 0) + 1
    print("%d tests; (outcome in %s, outcome in %s): count" % (len(names), arm_a, arm_b))
    for key, c in sorted(counts.items()):
        print("  %-9s %-9s %d" % (key[0], key[1], c))
    print("-" * 100)
    for n in names:
        sa, sb = a.get(n), b.get(n)
        flag = "" if kind(sa) == kind(sb) else "   <-- DIFFERS"
        print("%-9s %-9s %s%s" % (kind(sa), kind(sb), n, flag))
        if kind(sa) in ("failed", "error") or kind(sb) in ("failed", "error"):
            print("      %s: %s" % (arm_a, sa))
            print("      %s: %s" % (arm_b, sb))
    differs = [n for n in names if kind(a.get(n)) != kind(b.get(n))]
    print("-" * 100)
    print("tests whose outcome differs between the arms: %d%s" % (len(differs), "" if not differs else " -> " + ", ".join(differs[:20])))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--arm", choices=("ours", "ours_swwr", "ours_vmretail"), required=True)
    c.add_argument("--binary", default=os.environ.get("INVPW_BINARY_PATH", ""))
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--launches", type=int, default=2)
    d = sub.add_parser("diff")
    d.add_argument("root", type=Path)
    d.add_argument("--a", default="ours")
    d.add_argument("--b", default="ours_swwr", help="ours_swwr or ours_vmretail")
    s = sub.add_parser("suites")
    s.add_argument("a", type=Path)
    s.add_argument("b", type=Path)
    s.add_argument("--a-name", default="ours")
    s.add_argument("--b-name", default="ours_swwr", help="ours_swwr or ours_vmretail")
    args = ap.parse_args()
    if args.cmd == "collect":
        if not args.binary:
            ap.error("--binary (or INVPW_BINARY_PATH)")
        return collect(args.arm, args.binary, args.out, args.launches)
    if args.cmd == "diff":
        return diff(args.root, args.a, args.b)
    return suites(args.a, args.b, args.a_name, args.b_name)


if __name__ == "__main__":
    sys.exit(main())
