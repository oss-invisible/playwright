"""EXSLT's ``date:date-time()`` called from an IMPORTED stylesheet is the wall clock of the zone the context declares.

``test_a_launch_and_a_context_timezone_reach_the_page_e2e`` reads ``date:date-time()`` from a stylesheet that stands alone, built by ``DOMParser``.
A site more often has two sheets: a main one that ``xsl:import``s another, loaded by address, and the call to ``date:date-time()`` is in the imported
one. The imported sheet is compiled by the stylesheet compiler's own load (a real fetch made while ``importStylesheet`` runs, not a document the page
parsed), and the function finds the zone through the SOURCE document of the transform (``getSourceDocument`` in txEXSLTFunctions.cpp), never through the
sheet that holds the call. A source document that an ``XMLHttpRequest`` loaded has no browsing context of its own; the engine takes the one of the window
that made it. This is the claim that the zone still comes out the declared one on that road.

A launch declares ``Europe/Rome`` and a context asks for ``Pacific/Kiritimati`` (+14:00 all year, no daylight time: a zone no runner has, and one whose
offset is the same whatever day the test runs). The page loads ``/doc.xml`` and ``/main.xsl`` by address with ``XMLHttpRequest``, ``/main.xsl`` imports
``/imported.xsl``, and the imported sheet writes ``date:date-time()`` into the output: its ``±hh:mm`` suffix must be ``+14:00``.

The control is a launch whose declaration is EMPTY (``juggler.timezone.override`` empty, a context that asks for no zone): the engine then has nothing to
give and the zone is the host's, which is not +14:00. The suffix must be what ``new Date().getTimezoneOffset()`` says in that same page, written as
``±hh:mm``. A control that read +14:00 would mean the test cannot tell a declared zone from the host's.

Only the offset is judged, never the clock: the digits of the date and the time are read for their shape and nothing else.
"""
from __future__ import annotations

import re

import pytest

import _locale_probe as probe

pytestmark = pytest.mark.e2e

KIRITIMATI = "Pacific/Kiritimati"
KIRITIMATI_OFFSET = "+14:00"
SESSION_ZONE = "Europe/Rome"

XML_KIND = "application/xml; charset=utf-8"

#: The document the transform reads: its source document is the one whose context gives the zone.
DOC = b"<?xml version='1.0'?><doc><item>a</item></doc>"

#: The main sheet owns the root template and hands the work to the imported one with ``xsl:apply-imports``.
MAIN = (
    "<?xml version='1.0'?>"
    "<xsl:stylesheet version='1.0' xmlns:xsl='http://www.w3.org/1999/XSL/Transform'>"
    "<xsl:import href='imported.xsl'/>"
    "<xsl:output method='xml'/>"
    "<xsl:template match='/'><out><xsl:apply-imports/></out></xsl:template>"
    "</xsl:stylesheet>"
).encode()

#: The imported sheet is the only one that names the function.
IMPORTED = (
    "<?xml version='1.0'?>"
    "<xsl:stylesheet version='1.0' xmlns:xsl='http://www.w3.org/1999/XSL/Transform' xmlns:date='http://exslt.org/dates-and-times' "
    "extension-element-prefixes='date'>"
    "<xsl:template match='/'><stamp><xsl:value-of select='date:date-time()'/></stamp></xsl:template>"
    "</xsl:stylesheet>"
).encode()

SITE = {
    "/page": probe.PAGE,
    "/doc.xml": (DOC, XML_KIND),
    "/main.xsl": (MAIN, XML_KIND),
    "/imported.xsl": (IMPORTED, XML_KIND),
}

#: What an inline script of the page runs. Each document is loaded by address with a synchronous ``XMLHttpRequest`` (no response type: a window may not
#: ask a synchronous request for one), ``importStylesheet`` fetches ``imported.xsl`` itself, and the result is the text of the output.
PAGE_READ = """
try {
  const load = (path) => {
    const request = new XMLHttpRequest();
    request.open('GET', path, false);
    request.send();
    if (request.status !== 200 || !request.responseXML) { throw new Error(path + ': ' + request.status); }
    return request.responseXML;
  };
  const processor = new XSLTProcessor();
  processor.importStylesheet(load('/main.xsl'));
  const fragment = processor.transformToFragment(load('/doc.xml'), document);
  return {
    stamp: fragment.textContent,
    marked: fragment.firstChild ? fragment.firstChild.localName : null,
    behind: -new Date().getTimezoneOffset(),
  };
} catch (error) {
  return {error: String(error)};
}
"""

STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?([+-]\d\d:\d\d)$")


def _suffix(minutes_ahead):
    """The ``±hh:mm`` EXSLT writes for a zone that is ``minutes_ahead`` minutes ahead of UTC (``-getTimezoneOffset()``)."""
    sign = "-" if minutes_ahead < 0 else "+"
    whole = abs(minutes_ahead)
    return "%s%02d:%02d" % (sign, whole // 60, whole % 60)


def _read(page, base):
    page.goto(base + "/page", wait_until="load", timeout=30_000)
    answer = probe.read_in_page_realm(page, PAGE_READ)
    assert "error" not in answer, "the transform with an imported stylesheet failed in the page: %r" % (answer,)
    assert answer["marked"] == "out", "the output is the main sheet's <out> element: %r" % (answer,)
    found = STAMP.match(answer["stamp"])
    assert found, "the imported sheet's date:date-time() is not a date-time with an offset: %r" % (answer,)
    return {"offset": found.group(1), "behind": answer["behind"], "stamp": answer["stamp"]}


def test_exslt_date_time_through_an_imported_stylesheet_is_in_the_declared_zone(firefox_binary):
    """⛔ A SHEET THAT IS LOADED, NOT PARSED BY THE PAGE, IS STILL READ IN THE PAGE'S ZONE. ``date:date-time()`` formats a time in the zone of the
    context the SOURCE document of the transform belongs to; the call is in a stylesheet the compiler fetched for ``xsl:import``, and the source
    document came from an ``XMLHttpRequest``, so no part of the road holds a browsing context of its own and a read that asked the sheet, or the
    process, found the host's zone.

    A launch with ``timezone="Europe/Rome"`` and a context with ``timezone_id="Pacific/Kiritimati"``: the page's ``getTimezoneOffset()`` says -840
    and the imported sheet's ``date:date-time()`` ends in ``+14:00``, which is neither Rome's nor the host's. The control is a launch with an EMPTY
    declaration and a context that asks for no zone: the date-time ends in the offset ``getTimezoneOffset()`` gives in that page, and that offset is
    not +14:00.
    """
    from invisible_playwright import InvisiblePlaywright

    srv, base = probe.serve(SITE)
    try:
        with InvisiblePlaywright(seed=5150, binary_path=firefox_binary, headless=True, locale="en-US", timezone=SESSION_ZONE) as browser:
            declared = _read(browser.new_context(timezone_id=KIRITIMATI).new_page(), base)

        with InvisiblePlaywright(seed=5150, binary_path=firefox_binary, headless=True, locale="en-US", timezone=SESSION_ZONE,
                                 extra_prefs={"juggler.timezone.override": ""}) as browser:
            control = _read(browser.new_context(timezone_id=None).new_page(), base)

        assert declared["behind"] == 14 * 60, "the context's zone did not reach the page: getTimezoneOffset says %r" % (declared,)
        assert declared["offset"] == KIRITIMATI_OFFSET, (
            "date:date-time() of an imported stylesheet is %r, not the declared zone's %s" % (declared["stamp"], KIRITIMATI_OFFSET))

        assert control["offset"] != KIRITIMATI_OFFSET, (
            "the control reads %r: the launch with no declared zone gives the declared zone's offset, so the test cannot tell the two apart" % (control,))
        assert control["offset"] == _suffix(control["behind"]), (
            "with no declared zone date:date-time() is %r, the page's own offset says %s" % (control["stamp"], _suffix(control["behind"])))
    finally:
        probe.stop(srv)
