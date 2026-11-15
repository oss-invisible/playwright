"""Hunt only: the bytes a page reads, one seed, on this machine; and each probe font drawn alone, to tell which one differs.

    python scripts/hunt_text_bytes.py <binary> <out.json>
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
import _page_bytes  # noqa: E402

from invisible_playwright import InvisiblePlaywright  # noqa: E402

LAUNCH = dict(seed=42, headless=True, locale="en-US", timezone="Europe/Rome")
PROBE = (Path(__file__).resolve().parent.parent / "tests" / "page_bytes_probe.js").read_text(encoding="utf-8")
EACH_FONT = r"""async () => {
  const FONTS = ["16px sans-serif", "20px serif", "18px monospace", "bold 16px Arial", "italic 15px 'Segoe UI'", "16px 'Times New Roman'",
    "16px Calibri", "14px Consolas", "16px Verdana", "16px Tahoma", "17px Georgia", "16px 'Segoe UI Emoji'", "16px 'Microsoft YaHei'",
    "16px 'Yu Gothic'", "bold 40px sans-serif", "italic 34px serif", "28px monospace", "bold 44px Arial", "36px 'Times New Roman'", "30px 'Segoe UI'"];
  const TEXTS = {latin: "Hamburgefonstiv 0123456789 Ag", cjk: "日本語のテキスト 漢字", emoji: "😀🚀👍🏽", arabic: "مرحبا بالعالم"};
  async function digest(data) {
    const h = await crypto.subtle.digest("SHA-256", data);
    return Array.from(new Uint8Array(h)).slice(0, 8).map((b) => b.toString(16).padStart(2, "0")).join("");
  }
  const out = [];
  for (const f of FONTS) {
    for (const [name, t] of Object.entries(TEXTS)) {
      const c = document.createElement("canvas"); c.width = 640; c.height = 64;
      const g = c.getContext("2d"); g.fillStyle = "#fff"; g.fillRect(0, 0, 640, 64);
      g.font = f; g.fillStyle = "#101820"; g.textBaseline = "alphabetic"; g.fillText(t, 4, 44);
      const m = g.measureText(t);
      const px = g.getImageData(0, 0, 640, 64).data;
      let ink = 0; for (let i = 0; i < px.length; i += 4) if (px[i] < 250) ink++;
      out.push({font: f, text: name, raster: await digest(px), ink, width: m.width, asc: m.actualBoundingBoxAscent,
                desc: m.actualBoundingBoxDescent, fasc: m.fontBoundingBoxAscent, fdesc: m.fontBoundingBoxDescent,
                check: document.fonts.check(f)});
    }
  }
  return out;
}"""


class Page(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b"<!doctype html><meta charset=utf-8><title>probe</title><body></body>"
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)


def main(binary, out):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/" % server.server_address[1]
    try:
        with InvisiblePlaywright(binary_path=binary, **LAUNCH) as browser:
            page = browser.new_page()
            page.goto(url, wait_until="load", timeout=60000)
            measured = page.evaluate(PROBE)
            page.goto(url, wait_until="load", timeout=60000)
            each = page.evaluate(EACH_FONT)
    finally:
        server.shutdown()
    entries = {k: _page_bytes.entry_of(k, v) for k, v in measured.items()}
    raw_strings = {k: v["s"] for k, v in measured.items() if "s" in v}
    Path(out).write_text(json.dumps({"entries": entries, "strings": raw_strings, "each_font": each}, indent=1), encoding="utf-8")
    print("HUNT wrote %d keys and %d single-font draws" % (len(entries), len(each)))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
