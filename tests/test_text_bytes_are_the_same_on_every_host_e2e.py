"""The canvas text a page reads is the same bytes on every host: one seed, one set of bytes, whatever Windows build or OS runs it.

With the host's DWrite.dll rasterizing the glyphs, the same seed gave different canvas-text bytes on windows-2022 (DWrite
10.0.20348) and windows-2025 (10.0.26100): 7 keys and 9 of 80 single-font draws, with the measureText boxes identical and
georgia.ttf and verdana.ttf byte-identical on both hosts (oss-invisible/playwright run 43718413937; DWriteCore
captures/host-env/page-bytes-windows-2022-vs-2025). With idwr answering in place of DWrite.dll in the canvas path the two Windows
hosts give the same bytes, recorded in tests/data/text_bytes_reference.json (run 43744178165).

* Windows: every text key of page_bytes_probe.js and every one of the 80 draws of text_bytes_probe.js equals the reference. On
  windows-2022 it fails on today's engine (7 keys, 9 draws) and passes with idwr.
* Linux: the same, except the draws whose glyphs come from a fallback face made bold synthetically (CJK and emoji text in a bold
  font that has no such glyphs) and the composite keys that contain them. That synthetic bold is the one place Linux and Windows
  still differ (Windows 0.1 to 1.7 px wider, other ink), and it has a test of its own that is red until it is fixed:
  tests/test_synthetic_bold_of_a_fallback_face_is_windows_e2e.py, which comes with the port that fixes it.

A difference names its key or its (font, text) draw, so a failure says which font and script moved.
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

TESTS = Path(__file__).resolve().parent
REFERENCE = TESTS / "data" / "text_bytes_reference.json"
#: What a launch is given when the reference was recorded: the same seed, locale and timezone, so the same persona.
LAUNCH = dict(seed=42, headless=True, locale="en-US", timezone="Europe/Rome")
#: The keys of page_bytes_probe.js that are canvas or DOM text.
TEXT_KEYS = ("c2d.text.", "c2d.gradtext.", "c2d.offscreen.", "c2d.measure", "dom.span_boxes", "dom.paragraphs")
#: The composite keys that draw CJK or emoji text in a bold font (page_bytes_probe.js FONTS x TEXTS): on Linux they hold the
#: synthetic bold of a fallback face, which the test of the synthetic bold covers.
HOLDS_SYNTHETIC_BOLD = ("c2d.text.", "c2d.measure", "dom.span_boxes")


def synthetic_bold_of_a_fallback(draw) -> bool:
    """A draw whose glyphs come from a fallback face made bold: CJK or emoji text in a bold font with no such glyphs."""
    return draw["font"].startswith("bold ") and draw["text"] in ("cjk", "emoji")


class _Page(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        body = b"<!doctype html><meta charset=utf-8><title>probe</title><body></body>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def measured(firefox_binary, engine_at_least):
    """The text keys and the 80 draws of this host, once for the module."""
    engine_at_least("firefox-66")
    import _page_bytes
    from invisible_playwright import InvisiblePlaywright

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/" % server.server_address[1]
    try:
        with InvisiblePlaywright(binary_path=firefox_binary, **LAUNCH) as browser:
            page = browser.new_page()
            page.goto(url, wait_until="load", timeout=60000)
            keys = page.evaluate((TESTS / "page_bytes_probe.js").read_text(encoding="utf-8"))
            page.goto(url, wait_until="load", timeout=60000)
            draws = page.evaluate((TESTS / "text_bytes_probe.js").read_text(encoding="utf-8"))
    finally:
        server.shutdown()
    entries = {k: _page_bytes.entry_of(k, v) for k, v in keys.items() if k.startswith(TEXT_KEYS)}
    return entries, draws


@pytest.fixture(scope="module")
def reference():
    return json.loads(REFERENCE.read_text(encoding="utf-8"))


def _same(a, b) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _key_problems(entries, reference, skip=()):
    out = []
    for key, want in sorted(reference["entries"].items()):
        if key.startswith(skip):
            continue
        got = entries.get(key)
        if got is None:
            out.append("%s: missing" % key)
        elif not _same(got, want):
            out.append("%s: %s, the reference %s" % (key, json.dumps(got, sort_keys=True)[:160], json.dumps(want, sort_keys=True)[:160]))
    return out


def _draw_problems(draws, reference, keep=lambda draw: True):
    out = []
    want = {(d["font"], d["text"]): d for d in reference["each_font"]}
    got = {(d["font"], d["text"]): d for d in draws}
    for name, w in sorted(want.items()):
        if not keep(w):
            continue
        g = got.get(name)
        if g is None:
            out.append("%s %s: not drawn" % name)
            continue
        moved = [f for f in ("raster", "ink", "width", "asc", "desc", "fasc", "fdesc", "check") if g.get(f) != w.get(f)]
        if moved:
            out.append("%s %s: %s" % (name[0], name[1], ", ".join("%s %r (reference %r)" % (f, g.get(f), w.get(f)) for f in moved)))
    return out


def test_the_canvas_text_keys_are_the_reference_of_every_host(measured, reference):
    """Windows: every text key. Linux: every text key but the composite ones that hold the synthetic bold of a fallback face."""
    entries, _ = measured
    skip = HOLDS_SYNTHETIC_BOLD if sys.platform.startswith("linux") else ()
    problems = _key_problems(entries, reference, skip)
    assert not problems, "%d text key(s) differ from the reference (%s):\n  %s" % (len(problems), reference["_source"], "\n  ".join(problems))


def test_every_font_and_script_draws_the_reference_bytes(measured, reference):
    """The 80 single-font draws, raster and boxes. Linux leaves out the 6 synthetic-bold fallback draws, tested on their own."""
    _, draws = measured
    keep = (lambda draw: not synthetic_bold_of_a_fallback(draw)) if sys.platform.startswith("linux") else (lambda draw: True)
    problems = _draw_problems(draws, reference, keep)
    assert not problems, "%d draw(s) differ from the reference:\n  %s" % (len(problems), "\n  ".join(problems))


def test_the_reference_was_the_same_on_both_windows_hosts_and_names_its_source(reference):
    """The reference is one set of bytes for every host: recorded on windows-2025, equal on windows-2022, with its engine and run."""
    assert "windows-2025" in reference["_source"] and "windows-2022" in reference["_source"]
    assert len(reference["each_font"]) == 80
    assert sum(1 for d in reference["each_font"] if synthetic_bold_of_a_fallback(d)) == 6
