"""HUNT ONLY, never merged: where the +-1 between our canvas text and retail's falls, with the same font file, and the rendering
settings each browser runs with.

    python scripts/hunt_text_raster_cause.py run --arm ours --binary FF --out DIR      (INVISIBLE_SEAL_FILE for an unsealed engine)
    python scripts/hunt_text_raster_cause.py run --arm retail --retail-from DIR --retail-prefs JSON --out DIR
    python scripts/hunt_text_raster_cause.py compare DIR

The page draws Verdana 16px, Georgia 17px and sans-serif 16px (latin) as the text probe does (#101820 on white, 640x64, at 4,44),
and the same in black; it posts the RGBA. Then, through Marionette in chrome scope: nsIGfxInfo's ClearType parameters and the text and
canvas prefs the browser actually holds.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 28533
WAIT_S = 90
RESULT = {}
DRAWS = [("16px Verdana", "#101820"), ("17px Georgia", "#101820"), ("16px sans-serif", "#101820"),
         ("16px Verdana", "#000000"), ("17px Georgia", "#000000")]
TEXT = "Hamburgefonstiv 0123456789 Ag"

PAGE = """<!doctype html><meta charset=utf-8><title>raster</title><body><script>
(async () => {
  const DRAWS = %s, TEXT = %s, out = [];
  for (const [font, color] of DRAWS) {
    const c = document.createElement("canvas"); c.width = 640; c.height = 64;
    const g = c.getContext("2d"); g.fillStyle = "#fff"; g.fillRect(0, 0, 640, 64);
    g.font = font; g.fillStyle = color; g.textBaseline = "alphabetic"; g.fillText(TEXT, 4, 44);
    const px = g.getImageData(0, 0, 640, 64).data;
    let s = ""; for (let i = 0; i < px.length; i += 8192) s += String.fromCharCode.apply(null, px.subarray(i, i + 8192));
    out.push({font, color, width: g.measureText(TEXT).width, rgba: btoa(s)});
  }
  await fetch("/result", {method: "POST", body: JSON.stringify(out)});
})();
</script></body>""" % (json.dumps(DRAWS), json.dumps(TEXT))

CHROME = r"""
const out = {};
try { const gi = Cc["@mozilla.org/gfx/info;1"].getService(Ci.nsIGfxInfo); out.cleartype = gi.ClearTypeParameters;
      out.adapter = gi.adapterDescription; out.dwrite = gi.DWriteEnabled; out.dwriteVersion = gi.DWriteVersion; } catch (e) { out.gfxinfo_error = String(e); }
const names = Services.prefs.getChildList("gfx.font_rendering").concat(Services.prefs.getChildList("gfx.text"), Services.prefs.getChildList("gfx.canvas"),
  Services.prefs.getChildList("zoom.stealth.text"), ["layout.css.devPixelsPerPx", "gfx.webrender.software", "gfx.font_rendering.directwrite.use_gdi_table_loading"]);
out.prefs = {};
for (const n of names) { try { const t = Services.prefs.getPrefType(n);
  out.prefs[n] = t == 32 ? Services.prefs.getStringPref(n) : t == 64 ? Services.prefs.getIntPref(n) : t == 128 ? Services.prefs.getBoolPref(n) : null; } catch (e) {} }
return JSON.stringify(out);
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        RESULT["draws"] = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        self.send_response(204)
        self.end_headers()


class Marionette:
    def __init__(self, port):
        end = time.time() + 60
        while True:
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
                self.sock.settimeout(60)
                self._read()
                break
            except (OSError, ValueError):
                if time.time() > end:
                    raise
                time.sleep(1)
        self.n = 0

    def _read(self):
        head = b""
        while not head.endswith(b":"):
            c = self.sock.recv(1)
            if not c:
                raise OSError("closed")
            head += c
        size = int(head[:-1])
        data = b""
        while len(data) < size:
            data += self.sock.recv(size - len(data))
        return json.loads(data)

    def call(self, name, params=None):
        self.n += 1
        body = json.dumps([0, self.n, name, params or {}]).encode()
        self.sock.sendall(str(len(body)).encode() + b":" + body)
        msg = self._read()
        if msg[2]:
            raise RuntimeError(str(msg[2])[:300])
        return msg[3]


def chrome_facts():
    m = Marionette(PORT)
    m.call("WebDriver:NewSession", {"capabilities": {"alwaysMatch": {}}})
    m.call("Marionette:SetContext", {"value": "chrome"})
    return json.loads(m.call("WebDriver:ExecuteScript", {"script": CHROME, "args": []})["value"])


def kill_firefox():
    subprocess.run(["taskkill", "/F", "/IM", "firefox.exe", "/T"], capture_output=True)


def wait_draws():
    end = time.time() + WAIT_S
    while "draws" not in RESULT and time.time() < end:
        time.sleep(0.5)


def run(args) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/" % httpd.server_address[1]
    os.environ["MOZ_REMOTE_ALLOW_SYSTEM_ACCESS"] = "1"
    kill_firefox()
    facts, err = {}, None
    try:
        if args.arm == "retail":
            info = json.loads((Path(args.retail_from) / "retail.json").read_text(encoding="utf-8"))
            profile = Path(tempfile.mkdtemp(prefix="hunt-trc-retail-"))
            prefs = dict(json.loads(args.retail_prefs or "{}"), **{"marionette.port": PORT})
            (profile / "user.js").write_text("".join('user_pref("%s", %s);\n' % (k, json.dumps(v)) for k, v in prefs.items()), encoding="utf-8")
            proc = subprocess.Popen([info["exe"], "-no-remote", "-marionette", "-remote-allow-system-access", "-profile", str(profile), url],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                wait_draws()
                facts = chrome_facts()
            finally:
                kill_firefox()
                try:
                    proc.wait(timeout=10)
                except Exception:   # noqa: BLE001
                    pass
        else:
            async def go():
                from invisible_playwright.async_api import InvisiblePlaywright
                async with InvisiblePlaywright(seed=42, binary_path=args.binary, headless=True, locale="en-US", timezone="Europe/Rome",
                                               extra_args=["-marionette", "-remote-allow-system-access"], extra_prefs={"marionette.port": PORT}) as browser:
                    page = await browser.new_page()
                    await page.goto(url, wait_until="load", timeout=60000)
                    for _ in range(WAIT_S * 2):
                        if "draws" in RESULT:
                            break
                        await asyncio.sleep(0.5)
                    return await asyncio.to_thread(chrome_facts)
            facts = asyncio.run(go())
    except Exception as exc:   # noqa: BLE001
        err = "%s: %s" % (type(exc).__name__, str(exc)[:300])
    finally:
        kill_firefox()
        httpd.shutdown()
    doc = {"arm": args.arm, "error": err, "facts": facts, "draws": json.loads(RESULT["draws"]) if "draws" in RESULT else None}
    (args.out / ("%s.json" % args.arm)).write_text(json.dumps(doc, indent=1), encoding="utf-8")
    print("arm %s: draws %s, facts %s, error %s" % (args.arm, len(doc["draws"] or []), bool(facts), err))
    return 0


def compare(args) -> int:
    o = json.loads((args.dir / "ours.json").read_text(encoding="utf-8"))
    r = json.loads((args.dir / "retail.json").read_text(encoding="utf-8"))
    print("errors: ours %s; retail %s" % (o["error"], r["error"]))
    for arm, doc in (("ours", o), ("retail", r)):
        f = doc.get("facts") or {}
        print("HUNTRC facts %-6s cleartype=%s dwrite=%s %s adapter=%s" % (arm, f.get("cleartype"), f.get("dwrite"), f.get("dwriteVersion"), f.get("adapter")))
        print("HUNTRC prefs %-6s %s" % (arm, json.dumps(f.get("prefs"), sort_keys=True)))
    for a, b in zip(o["draws"] or [], r["draws"] or []):
        pa, pb = base64.b64decode(a["rgba"]), base64.b64decode(b["rgba"])
        n = len(pa) // 4
        diff = edge = interior = background = same_rgb = 0
        darker = lighter = 0
        chan = [0, 0, 0]
        fringe = {"ours": 0, "retail": 0}
        maxd = 0
        for i in range(n):
            ra, ga, ba = pa[4 * i], pa[4 * i + 1], pa[4 * i + 2]
            rb, gb, bb = pb[4 * i], pb[4 * i + 1], pb[4 * i + 2]
            for who, (x, y, z) in (("ours", (ra, ga, ba)), ("retail", (rb, gb, bb))):
                if max(x, y, z) < 250 and (abs(x - y) > 24 or abs(y - z) > 24):
                    fringe[who] += 1
            d = (ra - rb, ga - gb, ba - bb)
            if d != (0, 0, 0):
                diff += 1
                maxd = max(maxd, max(abs(v) for v in d))
                for k in range(3):
                    chan[k] += d[k] != 0
                same_rgb += d[0] == d[1] == d[2]
                darker += sum(d) < 0
                lighter += sum(d) > 0
                cov = (255 - gb) / 255.0   # retail's coverage on the green channel
                if cov <= 0.02:
                    background += 1
                elif cov >= 0.9:
                    interior += 1
                else:
                    edge += 1
        print("HUNTRC draw %-15s %s width ours %.3f retail %.3f | pixels differing %d (edge %d, interior %d, background %d) | per channel R/G/B %s | "
              "same delta on R,G,B %d | ours darker %d lighter %d | max |delta| %d | colour fringes ours %d retail %d"
              % (a["font"], a["color"], a["width"], b["width"], diff, edge, interior, background, chan, same_rgb, darker, lighter, maxd, fringe["ours"], fringe["retail"]))
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
    args = ap.parse_args()
    return run(args) if args.cmd == "run" else compare(args)


if __name__ == "__main__":
    sys.exit(main())
