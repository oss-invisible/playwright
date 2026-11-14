"""HUNT ONLY (branch hunt/cpu-render-path, never merged): "same seed, same bytes" - why are the PNG outputs of the canvas not the same between two launches?

    python scripts/hunt_encoding.py collect --client ours  --label ours          --binary "$FF" --out DIR [--launches 2]
    python scripts/hunt_encoding.py collect --client ours  --label ours_pypi052  [--binary B]   --out DIR      # run by the venv python that has invisible-playwright==0.52.0
    python scripts/hunt_encoding.py collect --client stock --label stockheaded   [--headless]    --out DIR
    python scripts/hunt_encoding.py collect --client retail --label retail --retail-from DIR --out DIR   # Mozilla's retail, launched by hand (hunt_first_run.py fetch-retail)
    python scripts/hunt_encoding.py table DIR [DIR ...]
    python scripts/hunt_encoding.py selftest          # the PNG parser against PNGs whose chunks are known

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
         PNG chunks are read here as the PNG specification lays them out (length, type, data, CRC-32; each CRC checked, IHDR first, IDATs consecutive, IEND last),
         the IDATs inflated and unfiltered, so that nothing is guessed from a hash. For every pair that differs the table prints WHICH BYTES (runs of offsets, and
         the chunk and field each falls in), whether the inflated IDAT data is identical, and the 16-character `deBG` text of every launch.

WHAT THE `deBG` CHUNK IS (found in the run of 2026-11-14): upstream Mozilla's image/encoders/png/nsPNGEncoder.cpp, MaybeAddCustomMetadata, writes it with the
letters 'd','e','B','G' set one by one (so `git grep deBG` finds nothing): 16 hex characters of a hash of the session's fingerprinting randomization key and the
image, after the IDATs, when canvas extraction is in the EfficientRandomize mode (privacy.baselineFingerprintingProtection, which the core turns on to match retail
151; invisible_core/prefs.py). The key is random per browsing session: the pixels are the same, the chunk is not. `collect --client retail` asks whether Mozilla's
own retail 159 writes it too.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import importlib
import json
import re
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


class Retail(BaseHTTPRequestHandler):
    """The page of the client `retail`: runs the probe and posts its result back (nothing drives the browser)."""
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
            self._send((HERE / "hunt_page_bytes_probe.js").read_bytes(), "text/javascript; charset=utf-8")
        else:
            self._send(b"<!doctype html><meta charset=utf-8><title>probe</title><body><script>globalThis.__ENCODING_MODE = true;"
                       b"fetch('/probe.js').then(r => r.text()).then(src => (0, eval)(src))"
                       b".then(o => fetch('/result', {method: 'POST', body: JSON.stringify(o)}))"
                       b".catch(e => fetch('/result', {method: 'POST', body: JSON.stringify({error: String(e)})}));</script></body>", "text/html; charset=utf-8")

    def do_POST(self):   # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        Retail.result = self.rfile.read(n).decode("utf-8")
        self._send(b"ok", "text/plain")

    def log_message(self, *a):   # noqa: D102
        pass


def collect_retail(exe: str) -> Dict[str, Any]:
    import subprocess
    import tempfile
    import time
    Retail.result = None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Retail)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/" % httpd.server_address[1]
    profile = Path(tempfile.mkdtemp(prefix="hunt-retail-profile-"))
    proc = subprocess.Popen([exe, "-no-remote", "-profile", str(profile), url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        end = time.time() + 90
        while Retail.result is None and time.time() < end:
            time.sleep(0.5)
        if Retail.result is None:
            raise RuntimeError("retail answered nothing in 90 s")
        data = json.loads(Retail.result)
        if "error" in data and len(data) == 1:
            raise RuntimeError("the probe failed in the page: %s" % data["error"])
        return data
    finally:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["taskkill", "/F", "/IM", "firefox.exe", "/T"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            proc.kill()
        httpd.shutdown()


def collect_once(client: str, binary: str, headless: bool) -> Dict[str, Any]:
    if client == "retail":
        return collect_retail(binary)
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

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def sha(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()[:12]


def parse_png(data: bytes) -> Dict[str, Any]:
    """Chunks of a PNG, read as the PNG specification (ISO/IEC 15948) lays them out and CHECKED against it.

    Each chunk is: length (4 bytes, big endian, of the data only) + type (4 ASCII letters) + data + CRC-32 (4 bytes, big endian, of type + data).
    Returns {"chunks": [{type, offset, length, crc, crc_ok, data_offset}], "problems": [...], "idat": bytes}. Offsets are absolute, from the first byte of the file.
    A problem is anything the specification forbids: a bad signature, a wrong CRC, a chunk running past the end, IHDR not first or not 13 bytes, no IDAT, IDAT chunks
    that are not consecutive, IEND not last or not empty, a type that is not four letters, bytes after IEND.
    """
    problems: List[str] = []
    chunks: List[Dict[str, Any]] = []
    if data[:8] != SIGNATURE:
        return {"chunks": [], "problems": ["bad signature %r" % data[:8]], "idat": b""}
    pos = 8
    while pos < len(data):
        if pos + 12 > len(data):
            problems.append("%d trailing byte(s) too short for a chunk at offset %d" % (len(data) - pos, pos))
            break
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        if length > 0x7FFFFFFF or pos + 12 + length > len(data):
            problems.append("chunk %r at offset %d claims %d data bytes, the file ends at %d" % (ctype, pos, length, len(data)))
            break
        body = data[pos + 8:pos + 8 + length]
        crc = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])[0]
        ok = (zlib.crc32(ctype + body) & 0xFFFFFFFF) == crc
        if not re.fullmatch(rb"[A-Za-z]{4}", ctype):
            problems.append("chunk type %r at offset %d is not four letters" % (ctype, pos))
        if not ok:
            problems.append("CRC of %r at offset %d is %08x, computed %08x" % (ctype, pos, crc, zlib.crc32(ctype + body) & 0xFFFFFFFF))
        chunks.append({"type": ctype.decode("latin1"), "offset": pos, "length": length, "crc": crc, "crc_ok": ok, "data_offset": pos + 8})
        pos += 12 + length
        if ctype == b"IEND":
            if pos != len(data):
                problems.append("%d byte(s) after IEND" % (len(data) - pos))
            break
    types = [c["type"] for c in chunks]
    if not types or types[0] != "IHDR":
        problems.append("the first chunk is %s, not IHDR" % (types[0] if types else "missing"))
    elif chunks[0]["length"] != 13:
        problems.append("IHDR has %d data bytes, not 13" % chunks[0]["length"])
    if "IDAT" not in types:
        problems.append("no IDAT")
    else:
        idx = [i for i, t in enumerate(types) if t == "IDAT"]
        if idx != list(range(idx[0], idx[-1] + 1)):
            problems.append("the IDAT chunks are not consecutive")
    if not types or types[-1] != "IEND":
        problems.append("the last chunk is %s, not IEND" % (types[-1] if types else "missing"))
    elif chunks[-1]["length"] != 0:
        problems.append("IEND has data")
    idat = b"".join(data[c["data_offset"]:c["data_offset"] + c["length"]] for c in chunks if c["type"] == "IDAT")
    return {"chunks": chunks, "problems": problems, "idat": idat}


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
    f: Dict[str, Any] = {"sha": key, "size": len(data), "chunks": p["chunks"], "problems": p["problems"]}
    if p["chunks"] and p["chunks"][0]["type"] == "IHDR" and p["chunks"][0]["length"] == 13:
        o = p["chunks"][0]["data_offset"]
        w, h, depth, ctype, _comp, _filt, interlace = struct.unpack(">IIBBBBB", data[o:o + 13])
        f["ihdr"] = {"w": w, "h": h, "depth": depth, "color_type": ctype, "interlace": interlace}
        try:
            raw = zlib.decompress(p["idat"])
            f["raw_sha"] = sha(raw)
            channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(ctype)
            if channels and depth == 8 and interlace == 0 and len(raw) == h * (w * channels + 1):
                pixels, filters = unfilter(raw, w, h, channels)
                f["pixels"], f["pixel_sha"], f["channels"] = pixels, sha(pixels), channels
                f["filters"] = {t: filters.count(t) for t in sorted(set(filters))}
            else:
                f["pixel_note"] = "not unfiltered (type %d depth %d interlace %d, inflated %d bytes for %dx%d)" % (ctype, depth, interlace, len(raw), w, h)
        except zlib.error as exc:
            f["problems"] = f["problems"] + ["inflate: %s" % exc]
    _cache[key] = f
    return f


def chunk_summary(data: bytes) -> str:
    """`IHDR 13 crc e319b23c | IDAT 8192 ... | deBG 16 ...` - the real chunks, in order."""
    f = png_facts(data)
    return " | ".join("%s %d crc %08x%s" % (c["type"], c["length"], c["crc"], "" if c["crc_ok"] else " BAD") for c in f["chunks"]) or "(no chunk)"


def field_of(chunks: List[Dict[str, Any]], offset: int) -> str:
    for c in chunks:
        if c["offset"] <= offset < c["offset"] + 12 + c["length"]:
            rel = offset - c["offset"]
            part = "length" if rel < 4 else ("type" if rel < 8 else ("data" if rel < 8 + c["length"] else "CRC"))
            return "%s %s" % (c["type"], part)
    return "outside every chunk"


def byte_report(a: bytes, b: bytes) -> str:
    """WHICH BYTES differ: runs of offsets, and for each the chunk and the field (length, type, data, CRC) it falls in."""
    if a == b:
        return "identical"
    if len(a) != len(b):
        return "lengths differ (%d against %d bytes)" % (len(a), len(b))
    ca = png_facts(a)["chunks"]
    runs: List[Tuple[int, int]] = []
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            if runs and runs[-1][1] == i - 1:
                runs[-1] = (runs[-1][0], i)
            else:
                runs.append((i, i))
    total = sum(e - s + 1 for s, e in runs)
    where = []
    for s, e in runs[:8]:
        fields = {field_of(ca, i) for i in range(s, e + 1)}
        where.append("%d..%d (%d byte%s: %s)" % (s, e, e - s + 1, "" if e == s else "s", ", ".join(sorted(fields))))
    return "%d of %d bytes differ in %d run(s): %s%s" % (total, len(a), len(runs), "; ".join(where), "; ..." if len(runs) > 8 else "")


def compare_png(a: bytes, b: bytes) -> str:
    """Short word for what differs between two PNGs; the chunks are compared one by one, the IDATs inflated and unfiltered."""
    if a == b:
        return "same"
    fa, fb = png_facts(a), png_facts(b)
    if fa["problems"] or fb["problems"]:
        return "INVALID PNG (%s | %s)" % ("; ".join(fa["problems"][:2]) or "ok", "; ".join(fb["problems"][:2]) or "ok")
    ta = [(c["type"], c["length"]) for c in fa["chunks"]]
    tb = [(c["type"], c["length"]) for c in fb["chunks"]]
    differing = []
    if [t for t, _ in ta] == [t for t, _ in tb]:
        # same chunk list: which chunks have different data or CRC
        for i, (ca, cb) in enumerate(zip(fa["chunks"], fb["chunks"])):
            if (ca["length"], ca["crc"]) != (cb["length"], cb["crc"]) or a[ca["data_offset"]:ca["data_offset"] + ca["length"]] != b[cb["data_offset"]:cb["data_offset"] + cb["length"]]:
                differing.append(ca["type"])
    else:
        return "chunk list differs (%s | %s)" % (",".join(t for t, _ in ta), ",".join(t for t, _ in tb))
    non_idat = sorted(set(t for t in differing if t != "IDAT"))
    idat_state = ""
    if "IDAT" in differing:
        if fa.get("raw_sha") == fb.get("raw_sha"):
            idat_state = "IDAT: same inflated data, only the deflate stream differs (%d / %d bytes)" % (len(png_facts(a)["chunks"]), len(png_facts(b)["chunks"]))
            idat_state = "IDAT: same inflated data, only the deflate stream differs"
        elif "pixels" in fa and "pixels" in fb and fa["pixel_sha"] == fb["pixel_sha"]:
            idat_state = "IDAT: same pixels, other filter choice"
        elif "pixels" in fa and "pixels" in fb and len(fa["pixels"]) == len(fb["pixels"]):
            x, y = fa["pixels"], fb["pixels"]
            diff = [i for i in range(len(x)) if x[i] != y[i]]
            px = diff[0] // fa["channels"]
            idat_state = "IDAT: PIXELS differ (%d of %d bytes, largest delta %d, first at (%d, %d))" % (
                len(diff), len(x), max(abs(x[i] - y[i]) for i in diff), px % fa["ihdr"]["w"], px // fa["ihdr"]["w"])
        else:
            idat_state = "IDAT: inflated data differs"
    elif fa.get("raw_sha") == fb.get("raw_sha") and fa.get("pixel_sha") == fb.get("pixel_sha"):
        idat_state = "IDAT identical, pixels identical"
    parts = []
    if non_idat:
        parts.append("only the %s chunk%s differ%s" % ("+".join(non_idat), "" if len(non_idat) == 1 else "s", "s" if len(non_idat) == 1 else "") if not idat_state.startswith("IDAT:") else
                     "the %s chunk%s and IDAT differ" % ("+".join(non_idat), "" if len(non_idat) == 1 else "s"))
    return "; ".join(parts + [idat_state]) if parts else idat_state


def deBG_text(data: bytes) -> str:
    f = png_facts(data)
    for c in f["chunks"]:
        if c["type"] == "deBG":
            return data[c["data_offset"]:c["data_offset"] + c["length"]].decode("latin1")
    return "-"


def selftest() -> int:
    """The parser against PNGs whose chunks are known."""
    def chunk(t: bytes, body: bytes, bad: bool = False) -> bytes:
        crc = zlib.crc32(t + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + t + body + struct.pack(">I", crc ^ (1 if bad else 0))
    # 1. a known 1x1 RGBA PNG (the smallest one browsers produce); its IHDR and IEND CRCs are known.
    one = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    p = parse_png(one)
    got = [(c["type"], c["length"], "%08x" % c["crc"], c["crc_ok"]) for c in p["chunks"]]
    assert p["problems"] == [], p["problems"]
    assert [(t, n, ok) for t, n, _c, ok in got] == [("IHDR", 13, True), ("IDAT", 13, True), ("IEND", 0, True)], got
    assert got[0][2] == "1f15c489" and got[2][2] == "ae426082", got            # the CRCs of this IHDR and of every IEND: known from any PNG tool
    f = png_facts(one)
    assert (f["ihdr"]["w"], f["ihdr"]["h"], f["ihdr"]["depth"], f["ihdr"]["color_type"]) == (1, 1, 8, 6), f["ihdr"]
    assert len(f["pixels"]) == 4, f
    # 2. a PNG built here with an ancillary chunk after IDAT, as nsPNGEncoder writes deBG
    ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes([i, 0, 0, 255, 0, i, 0, 255]) for i in (10, 20))
    img = SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"deBG", b"B78D4FA4CD83BABE") + chunk(b"IEND", b"")
    p2 = parse_png(img)
    assert p2["problems"] == [] and [c["type"] for c in p2["chunks"]] == ["IHDR", "IDAT", "deBG", "IEND"], p2
    assert deBG_text(img) == "B78D4FA4CD83BABE"
    other = SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"deBG", b"0123456789ABCDEF") + chunk(b"IEND", b"")
    assert compare_png(img, other).startswith("only the deBG chunk differs"), compare_png(img, other)
    assert "IDAT identical" in compare_png(img, other)
    assert "12 of" not in byte_report(img, other) and "deBG data" in byte_report(img, other), byte_report(img, other)
    recompressed = SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 0)) + chunk(b"deBG", b"B78D4FA4CD83BABE") + chunk(b"IEND", b"")
    assert "same inflated data" in compare_png(img, recompressed), compare_png(img, recompressed)
    # 3. what the parser must refuse
    bad_crc = SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw), bad=True) + chunk(b"IEND", b"")
    assert any("CRC" in x for x in parse_png(bad_crc)["problems"])
    no_idat = SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")
    assert "no IDAT" in parse_png(no_idat)["problems"]
    assert parse_png(b"not a png")["problems"][0].startswith("bad signature")
    cut = img[:-7]
    assert parse_png(cut)["problems"], "a truncated file must have a problem"
    print("selftest: the PNG parser reads the known 1x1 PNG (IHDR crc 1f15c489, IEND crc ae426082), the deBG layout, tells deBG-only, deflate-only and bad files apart: ok")
    return 0


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
    print("=" * 120)
    print("SAME SEED, SAME BYTES? seed %d; runners (legs): %s; arms: %s" % (SEED, ", ".join(runners), ", ".join(sorted(data))))
    summary: List[str] = []
    for arm in sorted(data):
        legs = data[arm]
        names = sorted(legs)
        print("=" * 120)
        print("ARM %s: %s" % (arm, ", ".join("runner %s %d launch(es)" % (r, len(l)) for r, l in sorted(legs.items()))))
        ua = next(iter(next(iter(legs.values()))), {}).get("env.ua", {}).get("s")
        print("  user agent: %s" % ua)
        first = next(iter(legs.values()))[0]
        scenes = scenes_of(first)
        sample = raw(first, scenes[0] + ".png1") if scenes else None
        if sample:
            print("  the chunks of one PNG (type length crc, in file order; CRCs checked): %s" % chunk_summary(sample))
        for sc in scenes:
            print("  " + "-" * 116)
            print("  %s" % sc)
            px = {r: [raw(run, sc + ".getImageData") for run in l] for r, l in legs.items()}
            all_px = {x for v in px.values() for x in v}
            draw = "same in every launch of every runner" if len(all_px) == 1 else (
                "DIFFERS inside a runner" if any(len(set(v)) > 1 for v in px.values()) else "differs across runners")
            print("    draw (getImageData): %s" % draw)
            valid, png_eq_px, dec_eq = [], [], []
            inpage, blob_eq_png = set(), set()
            for r, l in legs.items():
                for run in l:
                    a1 = raw(run, sc + ".png1")
                    if a1 is None:
                        continue
                    f = png_facts(a1)
                    valid.append(not f["problems"])
                    g = raw(run, sc + ".getImageData")
                    if f.get("pixels") is not None and g is not None and f.get("channels") == 4:
                        png_eq_px.append(f["pixels"] == g)
                    d = raw(run, sc + ".png.decoded")
                    if d is not None and g is not None:
                        dec_eq.append(d == g)
                    p = [raw(run, sc + ".png%d" % i) for i in (1, 2, 3)]
                    inpage.add("identical" if p[0] == p[1] == p[2] else "DIFFER")
                    bl = [raw(run, sc + ".blob%d" % i) for i in (1, 2, 3)]
                    if bl[0] is not None:
                        inpage.add("toBlob x3 " + ("identical" if bl[0] == bl[1] == bl[2] else "DIFFER"))
                        blob_eq_png.add(bl[0] == a1)
            print("    PNG valid against the specification (CRCs, order, IHDR/IDAT/IEND): %s" % ("all %d" % len(valid) if valid and all(valid) else "NO: " + ", ".join(
                "; ".join(png_facts(raw(run, sc + '.png1'))['problems']) for l in legs.values() for run in l if png_facts(raw(run, sc + '.png1'))['problems'])))
            print("    PNG pixels (IDAT inflated and unfiltered) == getImageData: %s; the pixels the browser decodes from the PNG == getImageData: %s" % (
                "always" if png_eq_px and all(png_eq_px) else ("NOT always" if png_eq_px else "n/a"), "always" if dec_eq and all(dec_eq) else ("NOT always" if dec_eq else "n/a")))
            print("    inside one page, three calls: %s; toBlob == toDataURL: %s" % ("toDataURL x3 " + "/".join(sorted(x for x in inpage if not x.startswith("toBlob"))) +
                  ("; " + "/".join(sorted(x for x in inpage if x.startswith("toBlob"))) if any(x.startswith("toBlob") for x in inpage) else ""),
                  "yes" if blob_eq_png == {True} else ("no" if blob_eq_png else "n/a")))
            deb = []
            pair_words: List[str] = []
            for r in names:
                l = legs[r]
                deb.append("%s: %s" % (r, ", ".join(deBG_text(raw(run, sc + ".png1") or b"") for run in l)))
                if len(l) >= 2:
                    a, b = raw(l[0], sc + ".png1"), raw(l[1], sc + ".png1")
                    word = pair_word(a, b)
                    pair_words.append("%s: %s" % (r, word))
                    print("    launch 1 against launch 2, runner %s: %s" % (r, word))
                    if a is not None and b is not None and a != b:
                        print("        bytes: %s" % byte_report(a, b))
            cross = ""
            if len(names) >= 2:
                a, b = raw(legs[names[0]][0], sc + ".png1"), raw(legs[names[1]][0], sc + ".png1")
                cross = pair_word(a, b)
                print("    runner %s launch 1 against runner %s launch 1: %s" % (names[0], names[1], cross))
                if a is not None and b is not None and a != b:
                    print("        bytes: %s" % byte_report(a, b))
            print("    deBG chunk data (16 ASCII hex characters) per launch: %s" % "; ".join(deb))
            summary.append("%-14s %-24s | draw %s | PNG pixels == getImageData %s | launch~launch %s | cross-runner %s" % (
                arm, sc, "same" if len(all_px) == 1 else "DIFF", "yes" if png_eq_px and all(png_eq_px) else "NO", " ; ".join(pair_words) or "-", cross or "-"))
    print("=" * 120)
    print("ONE LINE PER ARM AND KEY")
    for line in summary:
        print("  " + line)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--client", choices=("ours", "stock", "retail"), required=True)
    c.add_argument("--retail-from", default="", help="retail: the --out of hunt_first_run.py fetch-retail (its retail.json names firefox.exe)")
    c.add_argument("--label", required=True)
    c.add_argument("--binary", default="")
    c.add_argument("--headless", action="store_true", help="stock only: headless=True (default headed)")
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--launches", type=int, default=2)
    sub.add_parser("selftest")
    t = sub.add_parser("table")
    t.add_argument("dirs", type=Path, nargs="+")
    args = ap.parse_args()
    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "collect":
        if args.client == "retail":
            if not args.retail_from:
                ap.error("--client retail needs --retail-from")
            args.binary = json.loads((Path(args.retail_from) / "retail.json").read_text(encoding="utf-8"))["exe"]
        return collect(args)
    return table(args.dirs)


if __name__ == "__main__":
    sys.exit(main())
