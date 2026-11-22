"""``measureText`` answers the vertical metrics of the bundled font as whole numbers, as retail Windows Firefox does (e2e, every OS, firefox-68).

What the engine fixes. With the engine declared, ``CanvasRenderingContext2D::DrawOrMeasureText`` hands the run to ``StealthHostIndepTextMetrics``
(``fingerprint/canvas/text-metrics``, ``dom/canvas/StealthCanvasTextMetrics.h`` of the series), which takes the font's vertical metrics from the
bundled font's own OS/2 table (usWinAscent and usWinDescent, from the manifest the core hands to the engine) instead of the host's rasteriser, and
ROUNDS them the way a Windows Firefox does (``gfxDWriteFont::ComputeMetrics`` rounds the pair, then derives the em box from the rounded pair). On
retail, ``fontBoundingBoxAscent`` and ``fontBoundingBoxDescent`` are therefore whole numbers at every size, and so are the four
``actualBoundingBox*`` of the empty string (it has no ink); a public fingerprinting page measures exactly that
string and flags any value that is not whole. Fractional values (65.1796875 for 72px Arial) are what a FreeType host computes from the same table.

What the test reads. One page measures ``Hamburgefonstiv`` and the empty string through ``Arial`` at 10, 12, 16, 20 and 72 px
(``ctx.font = "<n>px Arial"``) and returns the twelve fields of every TextMetrics. The expected font-box values are the rounded
``usWin`` metrics of the Arial face in the bundle manifest (``tests/_font_names.py``: ascent 1854, descent 434, unit 2048): round(1854 x px / 2048)
and round(434 x px / 2048); four of them are also written as the retail measurements recorded with the engine source (72px ascent 65, 20px
ascent 18, 12px ascent 11, 10px descent 2).

The negative control, in the same test. The page runs again with the engine not declared (``zoom.stealth.fpp.hw_seed`` = 0), which keeps the
stock code. The control is adapted to the OS because the stock code is a different one on each:

  * Linux: the stock path is FreeType's, which does not round like DirectWrite: the twelve fields over the five sizes DIFFER from the engine's.
    The page is the one that tells the host's values from the declared ones.
  * Windows: the stock path IS DirectWrite's (the retail path the engine reproduces), so the font box with the engine off is the same whole numbers
    the engine declares: the test asserts that they are equal, which is the engine matching the Windows rasteriser and not an invented rounding.

Why a page reads it: ``CanvasRenderingContext2D.measureText`` is plain web API; ``fontBoundingBoxAscent`` is in the probe of every canvas
fingerprint.

Run: pytest tests/test_canvas_measure_text_metrics_e2e.py -m e2e -v
"""
from __future__ import annotations

import math
import sys

import pytest

import _font_names as fn

pytestmark = pytest.mark.e2e

SIZES = [10, 12, 16, 20, 72]
TEXT = "Hamburgefonstiv"

FIELDS = ["width", "actualBoundingBoxLeft", "actualBoundingBoxRight", "actualBoundingBoxAscent", "actualBoundingBoxDescent",
          "fontBoundingBoxAscent", "fontBoundingBoxDescent", "emHeightAscent", "emHeightDescent", "hangingBaseline", "alphabeticBaseline",
          "ideographicBaseline"]

#: Retail Windows Firefox, Arial, recorded with the engine source: (size, field) -> value.
RETAIL = {(72, "fontBoundingBoxAscent"): 65, (20, "fontBoundingBoxAscent"): 18, (12, "fontBoundingBoxAscent"): 11,
          (10, "fontBoundingBoxDescent"): 2}

MEASURE = """(args) => {
  const [sizes, text, fields] = args;
  const ctx = document.createElement('canvas').getContext('2d');
  const read = (m) => { const o = {}; for (const f of fields) o[f] = m[f]; return o; };
  const out = {};
  for (const size of sizes) {
    ctx.font = size + 'px Arial';
    out[size] = {text: read(ctx.measureText(text)), empty: read(ctx.measureText(''))};
  }
  return out;
}"""


def _read(firefox_binary, engine_declared):
    from invisible_playwright import InvisiblePlaywright

    prefs = {} if engine_declared else {"zoom.stealth.fpp.hw_seed": 0}
    with InvisiblePlaywright(seed=21, binary_path=firefox_binary, headless=True, extra_prefs=prefs) as browser:
        page = browser.new_context().new_page()
        page.goto("about:blank", timeout=30_000)
        raw = page.evaluate(MEASURE, [SIZES, TEXT, FIELDS])
    return {int(size): rows for size, rows in raw.items()}


def _whole(value):
    return float(value).is_integer()


def _round(value):
    """std::round: halves away from zero (the values here are positive)."""
    return int(math.floor(value + 0.5))


def test_measure_text_vertical_metrics_are_whole_and_retail(firefox_binary):
    arial = fn.bundle_manifest().face_with_psname("ArialMT")
    upem, ascent, descent = arial["upem"], arial["ascent"], arial["descent"]
    expected = {size: {"fontBoundingBoxAscent": _round(ascent * size / upem), "fontBoundingBoxDescent": _round(descent * size / upem)}
                for size in SIZES}
    for (size, field), value in RETAIL.items():
        assert expected[size][field] == value, (
            "the rounded OS/2 metric of the manifest's Arial (%s at %dpx = %r) is not the retail value %r the engine source records: the manifest "
            "is not the face this test was written for" % (field, size, expected[size][field], value))

    declared = _read(firefox_binary, True)
    for size in SIZES:
        for kind in ("text", "empty"):
            row = declared[size][kind]
            for field in ("fontBoundingBoxAscent", "fontBoundingBoxDescent"):
                assert _whole(row[field]), "engine declared: %dpx Arial %s of %r is %r, not a whole number" % (size, field, kind, row[field])
                assert row[field] == expected[size][field], (
                    "engine declared: %dpx Arial %s of %r is %r, the rounded OS/2 metric of the bundled face is %r"
                    % (size, field, kind, row[field], expected[size][field]))
        for field in ("actualBoundingBoxLeft", "actualBoundingBoxRight", "actualBoundingBoxAscent", "actualBoundingBoxDescent"):
            assert _whole(declared[size]["empty"][field]), (
                "engine declared: %dpx Arial %s of the empty string is %r, not a whole number: a page that measures the empty string reads a "
                "host rasteriser" % (size, field, declared[size]["empty"][field]))

    stock = _read(firefox_binary, False)
    if sys.platform == "win32":
        for size in SIZES:
            for kind in ("text", "empty"):
                for field in ("fontBoundingBoxAscent", "fontBoundingBoxDescent"):
                    assert stock[size][kind][field] == declared[size][kind][field], (
                        "engine not declared: %dpx Arial %s of %r is %r on the DirectWrite path and %r with the engine: the engine's rounding is "
                        "not the Windows rasteriser's" % (size, field, kind, stock[size][kind][field], declared[size][kind][field]))
    else:
        different = [(size, kind, field) for size in SIZES for kind in ("text", "empty") for field in FIELDS
                     if stock[size][kind][field] != declared[size][kind][field]]
        assert different, (
            "engine not declared: the twelve fields over five sizes are the same as with the engine declared: the page cannot tell the host's "
            "values from the declared ones, so the control proves nothing")
