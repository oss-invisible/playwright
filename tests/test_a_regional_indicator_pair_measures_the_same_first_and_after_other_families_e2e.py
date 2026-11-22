"""``measureText`` of a regional-indicator pair does not depend on what the page measured before it (e2e, every OS, firefox-68).

What the engine fixes. A flag (two regional-indicator code points, U+1F1EE U+1F1F9) set in Arial is a run that needs a FALLBACK font. The fallback
search may skip a family whose character maps are not loaded yet, start an asynchronous load and go on (``gfx.font_rendering.fallback.async``);
then the answer depends on which maps this process happens to have loaded already, and a page sees it in the width. Measured on the engine when
the site was missed: the pair through "Arial" gave 52.63 on both platforms when it was the first thing a fresh page measured, and 63.53 (Windows)
against 80.07 (Linux) once other families had been measured first: same binary, same page, another answer by load order. The font list is the
bundle (always bundle-only), so there is nothing to defer: ``gfxPlatformFontList::MayDeferCmapLoading`` answers false and both sites that ask it
(``GlobalFontFallback`` and ``gfxFontGroup::FindFallbackFaceForChar``) search synchronously (folders ``fingerprint/fonts/bundle`` and
``fingerprint/fonts/font-list`` of the series; ``StealthFallbackMayDefer`` in ``gfx/thebes/StealthBundleFontList.cpp``).

What the test reads. A browser is launched and its first page is opened on ``about:blank``; the pair of three flags is measured through
``16px Arial`` and ``40px Arial`` as the FIRST text the page measures. Then the page measures a string of Latin, Han, Arabic and an emoji through
EVERY family of the bundle manifest (the manifest the core hands the engine, ``tests/_font_names.py``), which loads the character maps of the
bundle, and measures the pairs again: the widths must be the same numbers as the first time. A second context's first page of the same browser
measures them as its first text too, and must read the same. Nothing is a timer: each read is the answer of the page to one call.

The negative control, in the same test. The assertion is about what a changed load order does; the same test runs again with the engine not declared
(``zoom.stealth.fpp.hw_seed`` = 0). On 68 bundle-only does not depend on the declaration, so the relation holds in both runs; the widths themselves
are written in the failure message and the test does not fix them (they are measurements of the first CI round: see the report of the filone).

Why a page reads it: ``CanvasRenderingContext2D.measureText().width`` is plain web API, and a flag emoji is in every fingerprinting canvas probe.

Run: pytest tests/test_a_regional_indicator_pair_measures_the_same_first_and_after_other_families_e2e.py -m e2e -v
"""
from __future__ import annotations

import pytest

import _font_names as fn

pytestmark = pytest.mark.e2e

#: Flags of Italy, the United States and Japan: pairs of regional indicators.
PAIRS = ["\U0001F1EE\U0001F1F9", "\U0001F1FA\U0001F1F8", "\U0001F1EF\U0001F1F5"]
SIZES = [16, 40]

#: Latin, Han, Arabic and an emoji: what makes the engine look for a fallback face in every family.
SAMPLE = "Hamburgefonstiv 日本語 中文 مرحبا \U0001F600"

MEASURE_PAIRS = """(args) => {
  const [pairs, sizes] = args;
  const ctx = document.createElement('canvas').getContext('2d');
  const out = {};
  for (const size of sizes) {
    ctx.font = size + 'px Arial';
    for (const pair of pairs) out[size + 'px ' + Array.from(pair).map(c => c.codePointAt(0).toString(16)).join('+')] = ctx.measureText(pair).width;
  }
  return out;
}"""

MEASURE_FAMILIES = """(args) => {
  const [families, sample] = args;
  const ctx = document.createElement('canvas').getContext('2d');
  const out = {};
  for (const family of families) {
    ctx.font = '24px "' + family + '"';
    out[family] = ctx.measureText(sample).width;
  }
  return out;
}"""


def _readings(firefox_binary, families, engine_declared):
    from invisible_playwright import InvisiblePlaywright

    prefs = {} if engine_declared else {"zoom.stealth.fpp.hw_seed": 0}
    with InvisiblePlaywright(seed=21, binary_path=firefox_binary, headless=True, extra_prefs=prefs) as browser:
        page = browser.new_context().new_page()
        page.goto("about:blank", timeout=30_000)
        first = page.evaluate(MEASURE_PAIRS, [PAIRS, SIZES])
        measured = page.evaluate(MEASURE_FAMILIES, [families, SAMPLE])
        after = page.evaluate(MEASURE_PAIRS, [PAIRS, SIZES])
        other = browser.new_context().new_page()
        other.goto("about:blank", timeout=30_000)
        in_a_fresh_page = other.evaluate(MEASURE_PAIRS, [PAIRS, SIZES])
    return first, after, in_a_fresh_page, len(measured)


def test_a_regional_indicator_pair_measures_the_same_first_and_after_other_families(firefox_binary):
    families = fn.bundle_manifest().families
    for declared in (True, False):
        mode = "engine declared" if declared else "engine not declared (hw_seed 0)"
        first, after, fresh, measured = _readings(firefox_binary, families, declared)
        assert measured == len(families), "%s: the page measured %d families, not the %d of the bundle" % (mode, measured, len(families))
        assert set(first) == {"%dpx %s" % (size, "+".join(format(ord(c), "x") for c in pair)) for size in SIZES for pair in PAIRS}
        assert all(width > 0 for width in first.values()), "%s: a flag measures no width at all: %r" % (mode, first)
        assert first == after, (
            "%s: the regional-indicator pairs measure %r as the first thing on a fresh page and %r once every family of the bundle was measured: "
            "the answer depends on the load order of the character maps" % (mode, first, after))
        assert first == fresh, (
            "%s: the pairs measure %r in the first page and %r as the first text of another context's page: the answer depends on what the "
            "process measured before" % (mode, first, fresh))
