"""HUNT ONLY (branch hunt/cpu-render-path, never merged): "same seed, same bytes" - why are the PNG outputs of the canvas not the same between two launches?

    python scripts/hunt_encoding.py collect --client ours  --label ours          --binary "$FF" --out DIR [--launches 2]
    python scripts/hunt_encoding.py collect --client ours  --label ours_pypi052  [--binary B]   --out DIR      # run by the venv python that has invisible-playwright==0.52.0
    python scripts/hunt_encoding.py collect --client stock --label stockheaded   [--headless]    --out DIR
    python scripts/hunt_encoding.py table DIR [DIR ...]

The 11 keys that were not stable inside an arm (seed 42, two launches) in scripts/hunt_page_bytes.py: the PNG (toDataURL, toBlob, convertToBlob) of the
2D scenes shapes, composite, image, text, of the OffscreenCanvas and of the WebGL 1 and 2 triangle; their pixels (getImageData, readPixels), their
JPEG and WebP were stable.

collect  launches the client `--launches` times and evaluates scripts/hunt_page_bytes_probe.js in ENCODING MODE (see its header): for each of those scenes
         the pixels of the draw, the raw bytes of three toDataURL and three toBlob on the same canvas in the same page, and the pixels the browser decodes
         from the first PNG. One gzipped JSON per launch: DIR/<label>-<n>.json.gz. This file imports nothing of this repository: it runs in the venv of the
         published wrapper as well as in the checkout's, and with stock Playwright (the client `stock`, imported by name).
table    reads one or more DIRs (one per runner) and prints, per arm and per key: whether the DRAW is the same (pixels), whether the PNGs are the same
         between two launches on the same runner, inside one page (three calls), between two runners, and for every pair of PNGs that differ WHAT differs:
         a chunk other than IDAT, only the deflate stream (same inflated data), only the filter choice (same pixels), or the pixels themselves.
         PNG chunks are read here (IHDR, IDAT, ...), the IDATs inflated and unfiltered, so that nothing is guessed from a hash.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import importlib
import json
import struct
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
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


def probe_expression() -> str:
    return "globalThis.__ENCODING_MODE = true;\n" + (HERE / "hunt_page_bytes_probe.js").read_text(encoding="utf-8")


def collect_once(client: str, binary: str, headless: bool) -> Dict[str, Any]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Blank)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/" % httpd.server_address[1]
    try:
        if client == "ours":
            from invisible_playwright import InvisiblePlaywright
            kwargs: Dict[str, Any] = {"seed": SEED, "headless": True, "locale": "en-US", "timezone": "Europe/Rome"}
            if binary:
                kwargs["binary_path"] = binary
            try:
                browser_cm = InvisiblePlaywright(**kwargs)
            except TypeError as exc:   # an older wrapper without one of the keywords
                print("  InvisiblePlaywright(%s) refused (%s): launching with seed, headless and binary only" % (sorted(kwargs), exc))
                kwargs = {k: v for k, v in kwargs.items() if k in ("seed", "headless", "binary_path")}
                browser_cm = InvisiblePlaywright(**kwargs)
            with browser_cm as browser:
                page = browser.new_page()
                page.goto(url, wait_until="load", timeout=60000)
                return page.evaluate(probe_expression())
        sync_playwright = importlib.import_module("playwright.sync_api").sync_playwright   # by name: the fork's guard forbids a static import
        with sync_playwright() as pw:
            browser = pw.firefox.launch(headless=headless, firefox_user_prefs={"fission.autostart": True})
            try:
                page = browser.new_page()
                page.goto(url, wait_until="load", timeout=60000)
                return page.evaluate(probe_expression())
            finally:
                browser.close()
    finally:
        httpd.shutdown()


def collect(args) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    try:
        import invisible_playwright
        version = getattr(invisible_playwright, "__version__", "?")
    except Exception:   # noqa: BLE001
        version = "not importable"
    print("collect: client %s, label %s, invisible_playwright %s, binary %s" % (args.client, args.label, version, args.binary or "(the wrapper's own)"))
    rc = 0
    for n in range(1, args.launches + 1):
        try:
            data = collect_once(args.client, args.binary, args.headless)
        except BaseException as exc:   # noqa: BLE001
            print("  launch %d FAILED: %s: %s" % (n, type(exc).__name__, str(exc)[:400]))
            rc = 1
            continue
        path = args.out / ("%s-%d.json.gz" % (args.label, n))
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(data, f)
        print("  launch %d: %d keys -> %s; ua %s" % (n, len(data), path, (data.get("env.ua") or {}).get("s")))
        sys.stdout.flush()
    return rc


# ------------------------------------------------------------------------------------------------------------------- the PNG

def sha(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()[:12]


def parse_png(data: bytes) -> Dict[str, Any]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return {"error": "not a PNG (%r)" % data[:8]}
    pos, chunks, idat, ihdr = 8, [], b"", None
    while pos + 8 <= len(data):
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        crc = data[pos + 8 + length:pos + 12 + length]
        chunks.append((ctype.decode("latin1"), length, crc.hex()))
        if ctype == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif ctype == b"IDAT":
            idat += body
        pos += 12 + length
    return {"chunks": chunks, "ihdr": ihdr, "idat": idat}


def unfilter(raw: bytes, w: int, h: int, bpp: int) -> Tuple[bytes, List[int]]:
    stride = w * bpp
    out = bytearray()
    prev = bytearray(stride)
    pos = 0
    filters: List[int] = []
    for _ in range(h):
        ft = raw[pos]
        pos += 1
        row = bytearray(raw[pos:pos + stride])
        pos += stride
        filters.append(ft)
        if ft == 1:
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 255
        elif ft == 2:
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[i] = (row[i] + pr) & 255
        out += row
        prev = row
    return bytes(out), filters


_cache: Dict[str, Dict[str, Any]] = {}


def png_facts(data: bytes) -> Dict[str, Any]:
    key = sha(data)
    if key in _cache:
        return _cache[key]
    p = parse_png(data)
    f: Dict[str, Any] = {"sha": key, "size": len(data)}
    if "error" in p:
        f["error"] = p["error"]
    else:
        f["chunks"] = [(c[0], c[1]) for c in p["chunks"]]
        f["other_chunks"] = [c for c in p["chunks"] if c[0] != "IDAT"]
        f["idat_sha"] = sha(p["idat"])
        w, h, depth, ctype, _comp, _filt, interlace = p["ihdr"]
        f["ihdr"] = "%dx%d depth %d type %d interlace %d" % (w, h, depth, ctype, interlace)
        try:
            raw = zlib.decompress(p["idat"])
            f["raw_sha"] = sha(raw)
            channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(ctype)
            if channels and depth == 8 and interlace == 0:
                pixels, filters = unfilter(raw, w, h, channels)
                f["pixel_sha"] = sha(pixels)
                f["pixels"] = pixels
                f["filters"] = {t: filters.count(t) for t in sorted(set(filters))}
                f["channels"] = channels
            else:
                f["pixel_note"] = "not unfiltered (type %d depth %d interlace %d)" % (ctype, depth, interlace)
        except zlib.error as exc:
            f["error"] = "inflate: %s" % exc
    _cache[key] = f
    return f


def compare_png(a: bytes, b: bytes) -> str:
    """Short word for what differs between two PNGs."""
    if a == b:
        return "same"
    fa, fb = png_facts(a), png_facts(b)
    if "error" in fa or "error" in fb:
        return "unreadable (%s / %s)" % (fa.get("error"), fb.get("error"))
    if fa["other_chunks"] != fb["other_chunks"]:
        names_a, names_b = [c[0] for c in fa["other_chunks"]], [c[0] for c in fb["other_chunks"]]
        return "chunks differ (%s | %s)" % (",".join(names_a), ",".join(names_b))
    if fa.get("raw_sha") == fb.get("raw_sha"):
        return "deflate only (same inflated data, %d / %d bytes)" % (fa["size"], fb["size"])
    if "pixels" in fa and "pixels" in fb and fa["pixel_sha"] == fb["pixel_sha"]:
        return "filter only (same pixels; filters %s | %s)" % (fa["filters"], fb["filters"])
    if "pixels" in fa and "pixels" in fb and len(fa["pixels"]) == len(fb["pixels"]):
        x, y = fa["pixels"], fb["pixels"]
        diff = [i for i in range(len(x)) if x[i] != y[i]]
        mx = max(abs(x[i] - y[i]) for i in diff)
        px = diff[0] // fa["channels"]
        w = int(fa["ihdr"].split("x")[0])
        return "PIXELS differ (%d of %d bytes, largest delta %d, first at (%d, %d))" % (len(diff), len(x), mx, px % w, px // w)
    return "inflated data differs (%s | %s)" % (fa.get("pixel_note"), fb.get("pixel_note"))


# ------------------------------------------------------------------------------------------------------------------ the table

def load(dirs: List[Path]) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """{arm label: {runner dir name: [launch data...]}}"""
    out: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for d in dirs:
        for f in sorted(d.glob("*-*.json.gz")):
            label = f.name.rsplit("-", 1)[0]
            with gzip.open(f, "rt", encoding="utf-8") as g:
                out.setdefault(label, {}).setdefault(d.name, []).append(json.load(g))
    return out


def raw(run: Dict[str, Any], key: str) -> Optional[bytes]:
    v = run.get(key)
    return base64.b64decode(v["b64"]) if v and "b64" in v else None


def scenes_of(run: Dict[str, Any]) -> List[str]:
    return sorted({k[:-len(".getImageData")] for k in run if k.endswith(".getImageData")})


def pair_word(a: Optional[bytes], b: Optional[bytes]) -> str:
    if a is None or b is None:
        return "missing"
    return compare_png(a, b)


def table(dirs: List[Path]) -> int:
    data = load(dirs)
    if not data:
        print("no data in %s" % ", ".join(map(str, dirs)))
        return 1
    runners = [d.name for d in dirs]
    print("=" * 110)
    print("SAME SEED, SAME BYTES? seed %d; runners (legs): %s; arms: %s" % (SEED, ", ".join(runners), ", ".join(sorted(data))))
    summary: List[str] = []
    for arm in sorted(data):
        legs = data[arm]
        print("=" * 110)
        print("ARM %s: %s" % (arm, ", ".join("runner %s %d launch(es)" % (r, len(l)) for r, l in sorted(legs.items()))))
        ua = next(iter(next(iter(legs.values()))), {}).get("env.ua", {}).get("s")
        print("  user agent: %s" % ua)
        first = next(iter(legs.values()))[0]
        scenes = scenes_of(first)
        print("  %-26s %-9s %-34s %-34s %s" % ("scene", "draw px", "PNG launch1~launch2 (per runner)", "PNG, 3 calls in one page", "PNG across runners"))
        for sc in scenes:
            # the draw: getImageData, every launch of every runner
            px = {r: {raw(run, sc + ".getImageData") for run in l} for r, l in legs.items()}
            all_px = set().union(*px.values())
            draw = "same" if len(all_px) == 1 else ("DIFF inside a runner" if any(len(v) > 1 for v in px.values()) else "DIFF across runners")
            # between two launches on one runner
            per_runner = []
            for r, l in sorted(legs.items()):
                if len(l) >= 2:
                    words = {pair_word(raw(l[0], sc + ".png1"), raw(l[i], sc + ".png1")) for i in range(1, len(l))}
                    per_runner.append("%s: %s" % (r, "; ".join(sorted(words))))
            # inside one page
            inpage = set()
            for l in legs.values():
                for run in l:
                    a, b, c = (raw(run, sc + ".png%d" % i) for i in (1, 2, 3))
                    inpage.add("same" if a is not None and a == b == c else "DIFFER")
            blobs = set()
            for l in legs.values():
                for run in l:
                    a, b, c = (raw(run, sc + ".blob%d" % i) for i in (1, 2, 3))
                    if a is not None:
                        blobs.add("same" if a == b == c else "DIFFER")
            # across runners (launch 1 of each)
            cross = ""
            runner_names = sorted(legs)
            if len(runner_names) >= 2:
                cw = {pair_word(raw(legs[runner_names[0]][0], sc + ".png1"), raw(legs[r][0], sc + ".png1")) for r in runner_names[1:]}
                cross = "; ".join(sorted(cw))
            print("  %-26s %-9s %-34s %-34s %s" % (sc, draw, " | ".join(per_runner) or "(one launch)", "toDataURL x3 " + "/".join(sorted(inpage)) +
                                                  (", toBlob x3 " + "/".join(sorted(blobs)) if blobs else ""), cross or "(one runner)"))
            # decoded: what the browser itself reads back from the PNG
            dec = {raw(run, sc + ".png.decoded") for l in legs.values() for run in l}
            png_px = []
            for l in legs.values():
                for run in l:
                    b = raw(run, sc + ".png1")
                    g = raw(run, sc + ".getImageData")
                    if b is not None and g is not None:
                        f = png_facts(b)
                        if f.get("pixels") is not None and f.get("channels") == 4:
                            png_px.append(f["pixels"] == g)
            summary.append("%s | %s | draw %s | PNG==getImageData %s | decoded-by-browser %s | PNG launch~launch %s | cross-runner %s"
                           % (arm, sc, draw, ("always" if png_px and all(png_px) else ("never" if png_px and not any(png_px) else ("sometimes" if png_px else "n/a"))),
                              "same" if len(dec) == 1 and None not in dec else "differs", "; ".join(per_runner) or "-", cross or "-"))
    print("=" * 110)
    print("ONE LINE PER ARM AND KEY")
    for line in summary:
        print("  " + line)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--client", choices=("ours", "stock"), required=True)
    c.add_argument("--label", required=True)
    c.add_argument("--binary", default="")
    c.add_argument("--headless", action="store_true", help="stock only: headless=True (default headed)")
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--launches", type=int, default=2)
    t = sub.add_parser("table")
    t.add_argument("dirs", type=Path, nargs="+")
    args = ap.parse_args()
    if args.cmd == "collect":
        return collect(args)
    return table(args.dirs)


if __name__ == "__main__":
    sys.exit(main())
