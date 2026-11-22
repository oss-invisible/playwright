"""The OffscreenCanvas of a worker draws with the language group of its context.

Characterisation of firefox-68 for the hook of fingerprint/locale-time/workers (the language a worker carries) and of the canvas font
choice (nsLanguageAtomService, fed from the worker's language). This test is RED on firefox-68: it states what the engine has to do, not
what the published engine does, and it is not to be merged into the suite of the 68.

The font of text that names no language is chosen by the language group, so a string measured in a canvas tells a site which language the
canvas draws in. The test reads it where a page reads it: a dedicated worker measures a few strings on an OffscreenCanvas, and the page
measures the same strings on canvas ELEMENTS whose ``lang`` names the two groups outright. The control is the other context, the one that
asked for nothing, whose worker has to measure as the other group, and the two elements, which have to differ from each other.
"""
from __future__ import annotations

import _locale_probe as probe
import pytest

#: One page for every tab the test opens: the URL is what tells the tabs apart, so each one carries a query.
SITE = {
    "/page": probe.PAGE,
    "/measure-worker.js": (probe.MEASURE_WORKER, "text/javascript"),
}


def _canvas_widths(page):
    return page.evaluate(probe.MEASURE_PAGE_JS, ["ja", "en"])


@pytest.mark.e2e
def test_an_offscreen_canvas_of_a_worker_draws_with_the_language_group_of_its_context(firefox_binary):
    """⛔ THE FONT OF TEXT THAT NAMES NO LANGUAGE IS CHOSEN BY THE LANGUAGE GROUP, AND A WORKER'S CANVAS HAS NO DOCUMENT TO ASK. The generic
    ``serif`` of a Japanese page is another font than the one of an English page, so a string measured in a canvas tells a site which
    language the canvas draws in. In a worker the engine reads it from the thread's own language slot (nsLanguageAtomService, fed
    from the worker's language), which is the context's, not the process's.

    An en-US session, a context with ``locale="ja-JP"`` and one with nothing asked. In each, a dedicated worker measures a few strings
    on an OffscreenCanvas; the same page measures them on a canvas ELEMENT with ``lang="ja"`` and one with ``lang="en"``, the two groups
    named outright. The ja-JP context's worker measures as the ``ja`` element and not as the ``en`` one; the other context's worker the
    other way round. The two elements differ from each other, which is what makes the comparison say anything.
    """
    from invisible_playwright import InvisiblePlaywright

    srv, base = probe.serve(SITE)
    try:
        with InvisiblePlaywright(seed=5150, binary_path=firefox_binary, headless=True, locale="en-US",
                                 timezone="America/New_York") as browser:
            read = {}
            for name, options in (("ja", {"locale": "ja-JP"}), ("en", {})):
                page = browser.new_context(**options).new_page()
                page.goto("%s/page?%s" % (base, name), wait_until="load", timeout=30_000)
                read[name] = _canvas_widths(page)
                # recorded for the failure message only: a canvas ELEMENT with no ``lang`` in the page of this context
                read[name]["element_without_lang"] = page.evaluate(
                    "() => {%s const c = document.createElement('canvas'); document.body.appendChild(c); return widths(c.getContext('2d'));}"
                    % probe.MEASURE_JS)
        # Everything measured, in every message below, so a failing run says which reader sides with which language: the worker of the
        # ja-JP context and the worker of the default context (the session's own language, en-US), the main-thread offscreen canvas and
        # the canvas element with no lang of each context's page, and the elements named ja and en.
        record = "; ".join(
            "%s context: worker=%r offscreen(main thread)=%r element(no lang)=%r element(lang=ja)=%r element(lang=en)=%r" % (
                name, read[name]["worker"], read[name]["offscreen"], read[name]["element_without_lang"],
                read[name]["element"]["ja"], read[name]["element"]["en"])
            for name in ("ja", "en"))
        for name, other in (("ja", "en"), ("en", "ja")):
            got = read[name]
            assert got["element"]["ja"] != got["element"]["en"], (
                "the two language groups draw these strings alike here, so nothing can tell them apart: %r [%s]" % (got["element"], record))
            assert got["worker"] == got["element"][name], (
                "the worker of the %s context measures %r, a canvas of the %s group %r [%s]" % (name, got["worker"], name, got["element"][name], record))
            assert got["worker"] != got["element"][other], "the worker of the %s context measures as the %s group [%s]" % (name, other, record)
        assert read["ja"]["worker"] != read["en"]["worker"], "the two contexts' workers measure alike: the context's language did not reach the canvas [%s]" % record
    finally:
        probe.stop(srv)
