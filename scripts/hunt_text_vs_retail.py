"""HUNT ONLY, never merged: the canvas text of our engine against Firefox 159 RETAIL launched by hand, on the same Windows runner.

    python scripts/hunt_text_vs_retail.py run --arm ours --binary FF --out DIR      (INVISIBLE_SEAL_FILE for an unsealed candidate)
    python scripts/hunt_text_vs_retail.py run --arm retail --retail-from DIR --out DIR
    python scripts/hunt_text_vs_retail.py compare DIR --binary FF

One page runs the text keys of hunt_tv_page_bytes_probe.js (a copy of tests/page_bytes_probe.js) and the 80 single-font draws of
hunt_tv_text_probe.js (a copy of tests/text_bytes_probe.js) and posts both. `compare` prints, per draw, equal or the raster, width
and ink of each side, the text keys equal or not, and the sha256 of the font files each side draws with (retail: the runner's
C:\\Windows\\Fonts; ours: the engine's bundle).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hunt_tv_page_bytes as pb   # noqa: E402

TEXT_KEYS = ("c2d.text.", "c2d.gradtext.", "c2d.offscreen.", "c2d.measure", "dom.span_boxes", "dom.paragraphs")
WAIT_S = 120
RESULT = {}


def page_html() -> bytes:
    a = (HERE / "hunt_tv_page_bytes_probe.js").read_text(encoding="utf-8")
    b = (HERE / "hunt_tv_text_probe.js").read_text(encoding="utf-8")
    f = (HERE / "hunt_tv_fallback_probe.js").read_text(encoding="utf-8")
    js = ("(async () => { let keys = null, draws = null, fallback = null, error = null;\n"
          "try { keys = await (\n" + a + "\n); } catch (e) { error = 'keys: ' + e; }\n"
          "try { draws = await (\n" + b + "\n)(); } catch (e) { error = (error || '') + ' draws: ' + e; }\n"
          "try { fallback = await (\n" + f + "\n)(); } catch (e) { error = (error || '') + ' fallback: ' + e; }\n"
          "await fetch('/result', {method: 'POST', body: JSON.stringify({keys, draws, fallback, error})}); })();")
    return ("<!doctype html><meta charset=utf-8><title>probe</title><body><script>" + js + "</script></body>").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        body = page_html()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        RESULT["body"] = self.rfile.read(n).decode("utf-8")
        self.send_response(204)
        self.end_headers()


def kill_firefox():
    subprocess.run(["taskkill", "/F", "/IM", "firefox.exe", "/T"], capture_output=True)


def wait_result():
    end = time.time() + WAIT_S
    while "body" not in RESULT and time.time() < end:
        time.sleep(0.5)


async def ours(binary: str, url: str):
    from invisible_playwright.async_api import InvisiblePlaywright
    async with InvisiblePlaywright(seed=42, binary_path=binary, headless=True, locale="en-US", timezone="Europe/Rome") as browser:
        page = await browser.new_page()
        await page.goto(url, wait_until="load", timeout=60000)
        for _ in range(WAIT_S * 2):
            if "body" in RESULT:
                break
            await asyncio.sleep(0.5)


def run(args) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/" % httpd.server_address[1]
    kill_firefox()
    note = ""
    try:
        if args.arm == "retail":
            info = json.loads((Path(args.retail_from) / "retail.json").read_text(encoding="utf-8"))
            note = info.get("note", "")
            profile = Path(tempfile.mkdtemp(prefix="hunt-tv-retail-"))
            if args.retail_prefs:
                # The rasterisation parameters our engine declares for this seed, given to retail as the prefs Firefox itself reads
                # (gfxDWriteFonts::UpdateClearTypeVars: prefs over the system): the runner's own ClearType tuning is out of the comparison.
                prefs = json.loads(args.retail_prefs)
                (profile / "user.js").write_text("".join('user_pref("%s", %s);\n' % (k, json.dumps(v)) for k, v in prefs.items()), encoding="utf-8")
                note += " with prefs " + args.retail_prefs
            proc = subprocess.Popen([info["exe"], "-no-remote", "-profile", str(profile), url], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                wait_result()
            finally:
                kill_firefox()
                try:
                    proc.wait(timeout=10)
                except Exception:   # noqa: BLE001
                    pass
        else:
            asyncio.run(ours(args.binary, url))
    finally:
        kill_firefox()
        httpd.shutdown()
    data = json.loads(RESULT["body"]) if "body" in RESULT else {"error": "nothing posted in %d s" % WAIT_S}
    keys = data.get("keys") or {}
    doc = {"arm": args.arm, "note": note, "error": data.get("error"),
           "entries": {k: pb.entry_of(k, v) for k, v in keys.items() if k.startswith(TEXT_KEYS)}, "draws": data.get("draws"),
           "fallback": data.get("fallback")}
    (args.out / ("%s.json" % args.arm)).write_text(json.dumps(doc, indent=1), encoding="utf-8")
    print("arm %s: %s; error %s" % (args.arm, "draws %d" % len(doc["draws"] or []), doc["error"]))
    return 0


FONT_FILES = ["arial.ttf", "arialbd.ttf", "times.ttf", "timesi.ttf", "segoeui.ttf", "segoeuii.ttf", "calibri.ttf", "consola.ttf", "cour.ttf",
              "verdana.ttf", "tahoma.ttf", "georgia.ttf", "seguiemj.ttf", "msyh.ttc", "YuGothR.ttc", "YuGothB.ttc", "simsun.ttc", "msgothic.ttc"]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.is_file() else "absent"


def compare(args) -> int:
    o = json.loads((args.dir / "ours.json").read_text(encoding="utf-8"))
    r = json.loads((args.dir / "retail.json").read_text(encoding="utf-8"))
    print("errors: ours %s; retail %s" % (o.get("error"), r.get("error")))
    od = {(d["font"], d["text"]): d for d in o.get("draws") or []}
    rd = {(d["font"], d["text"]): d for d in r.get("draws") or []}
    same = 0
    rows = []
    for k in sorted(set(od) | set(rd)):
        a, b = od.get(k), rd.get(k)
        if a and b and all(a.get(f) == b.get(f) for f in ("raster", "ink", "width", "asc", "desc", "fasc", "fdesc")):
            same += 1
            continue
        rows.append("HUNTTV draw %-26s %-6s ours w=%s ink=%s asc=%s desc=%s check=%s | retail w=%s ink=%s asc=%s desc=%s check=%s%s"
                    % (k[0], k[1], *(a.get(f) if a else None for f in ("width", "ink", "asc", "desc", "check")),
                       *(b.get(f) if b else None for f in ("width", "ink", "asc", "desc", "check")),
                       "" if (a and b and a.get("raster") == b.get("raster")) else "  RASTER"))
    print("HUNTTV draws equal to retail: %d of %d" % (same, len(set(od) | set(rd))))
    for row in rows:
        print(row)
    for key in sorted(set(o["entries"]) | set(r["entries"])):
        eq = json.dumps(o["entries"].get(key), sort_keys=True) == json.dumps(r["entries"].get(key), sort_keys=True)
        print("HUNTTV key %-28s %s" % (key, "equal" if eq else "DIFF"))
    of = {(x["spec"], x["text"]): x for x in o.get("fallback") or []}
    rf = {(x["spec"], x["text"]): x for x in r.get("fallback") or []}
    for k in sorted(set(of) | set(rf)):
        a, b = of.get(k), rf.get(k)
        print("HUNTTV fallback %-26s %-6s ours face %s | retail face %s | same raster %s"
              % (k[0], k[1], a and a["matches"], b and b["matches"], bool(a and b and a["raster"] == b["raster"])))
    bundle = Path(args.binary).parent / "fonts"
    print("HUNTTV fonts: retail C:\\Windows\\Fonts | ours %s" % bundle)
    found = {p.name.lower(): p for p in bundle.rglob("*") if p.is_file()} if bundle.is_dir() else {}
    for name in FONT_FILES:
        a, b = sha(Path("C:/Windows/Fonts") / name), sha(found.get(name.lower(), Path("/nonexistent")))
        print("HUNTTV font %-14s retail %-16s ours %-16s %s" % (name, a, b, "same" if a == b else "DIFF"))
    print("HUNTTV bundle files: %d (%s)" % (len(found), ", ".join(sorted(found)[:40])))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--arm", choices=["ours", "retail"], required=True)
    p.add_argument("--binary", default="")
    p.add_argument("--retail-from", default="")
    p.add_argument("--retail-prefs", default="")
    p.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("compare")
    c.add_argument("dir", type=Path)
    c.add_argument("--binary", default="")
    args = ap.parse_args()
    return run(args) if args.cmd == "run" else compare(args)


if __name__ == "__main__":
    sys.exit(main())
