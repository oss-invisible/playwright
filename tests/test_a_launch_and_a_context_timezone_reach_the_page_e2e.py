"""The zone a launch declares, and the one a context asks for, reach everything a page reads the time from.

Characterisation of firefox-68; the hooks of fingerprint/locale-time/timezone in the engine series cite
``test_a_launch_and_a_context_timezone_reach_the_page``. A page learns the zone of a machine from more places than ``Intl``:

* the realm's ``Intl`` and the offset of ``Date`` (``getTimezoneOffset``, ``toLocaleTimeString``), in the page's own realm
  (read by an INLINE script, the way a site reads it) and in a dedicated worker;
* ``document.lastModified``, which formats the ``Last-Modified`` of the response, or the present moment when there is none, as a wall
  clock in the zone (Document.cpp, GetFormattedTimeString);
* EXSLT's ``date:date-time()`` in an XSLT transform (txEXSLTFunctions.cpp), which is the wall clock too.

None of them may be read from the host. A launch declares its zone (``timezone="Pacific/Chatham"``, a zone with a quarter-hour offset
and daylight time, which no runner has) and a context may ask for its own (``timezone_id="Asia/Kolkata"``, +05:30, no daylight time);
every one of those places answers the zone of the context the page is in.

The control is the same reads on a launch whose declaration is EMPTY (``juggler.timezone.override`` empty, and a context that asks for no
zone): the engine then has nothing to give and the values are the host's, which are not Chatham's. A control that agreed with Chatham
would mean the test cannot tell a declared zone from the host's.

Expected values: the offsets of Pacific/Chatham on a date in January (daylight time, +13:45) and in July (+12:45) and of Asia/Kolkata
(+05:30 all year) are the IANA database's; ``Last-Modified: Mon, 15 Jan 2024 10:00:00 GMT`` is 23:45:00 in Chatham and 15:30:00 in
Kolkata. The present moment, with no ``Last-Modified``, is compared with the page's own offset: the wall clock minus the clock of the
machine, rounded to a quarter of an hour, has to be the offset of the zone.
"""
from __future__ import annotations

import pytest

import _locale_probe as probe

pytestmark = pytest.mark.e2e

CHATHAM = "Pacific/Chatham"
KOLKATA = "Asia/Kolkata"

#: Minutes AHEAD of UTC, the sign of ``-getTimezoneOffset()``: what the zone is on 15 January and on 15 July 2024.
EXPECTED = {
    CHATHAM: {"january": 825, "july": 765, "time": "23:45:00", "last_modified": "01/15/2024 23:45:00"},
    KOLKATA: {"january": 330, "july": 330, "time": "15:30:00", "last_modified": "01/15/2024 15:30:00"},
}

LAST_MODIFIED = "Mon, 15 Jan 2024 10:00:00 GMT"

#: An XSLT sheet whose only output is the current date-time of EXSLT.
EXSLT_SHEET = (
    '<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
    'xmlns:date="http://exslt.org/dates-and-times" extension-element-prefixes="date">'
    '<xsl:output method="text"/><xsl:template match="/"><xsl:value-of select="date:date-time()"/></xsl:template></xsl:stylesheet>'
)

#: What an inline script of the page reads. ``wall`` turns "MM/DD/YYYY HH:MM:SS" or an ISO date-time into the minutes it is AHEAD of the
#: clock of the machine, to a quarter of an hour: the zone's offset, whatever the date.
PAGE_READ = """
const o = Intl.DateTimeFormat().resolvedOptions();
const instant = new Date(Date.UTC(2024, 0, 15, 10, 0, 0));
const ahead = (y, mo, d, h, mi, s) => Math.round((Date.UTC(y, mo - 1, d, h, mi, s) - Date.now()) / 60000 / 15) * 15;
const lm = document.lastModified.match(/(\\d\\d)\\/(\\d\\d)\\/(\\d{4}) (\\d\\d):(\\d\\d):(\\d\\d)/);
const processor = new XSLTProcessor();
processor.importStylesheet(new DOMParser().parseFromString(%(sheet)s, 'application/xml'));
const exslt = processor.transformToDocument(document).documentElement.textContent;
const ex = exslt.match(/(\\d{4})-(\\d\\d)-(\\d\\d)T(\\d\\d):(\\d\\d):(\\d\\d)/);
return {
  timeZone: o.timeZone,
  january: -new Date(Date.UTC(2024, 0, 15, 12)).getTimezoneOffset(),
  july: -new Date(Date.UTC(2024, 6, 15, 12)).getTimezoneOffset(),
  now: -new Date().getTimezoneOffset(),
  time: instant.toLocaleTimeString('en-GB'),
  lastModified: document.lastModified,
  lastModifiedAhead: lm ? ahead(+lm[3], +lm[1], +lm[2], +lm[4], +lm[5], +lm[6]) : null,
  exslt: exslt,
  exsltAhead: ex ? ahead(+ex[1], +ex[2], +ex[3], +ex[4], +ex[5], +ex[6]) : null,
};
""" % {"sheet": "`" + EXSLT_SHEET + "`"}

WORKER = b"""
onmessage = () => postMessage({
  timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  january: -new Date(Date.UTC(2024, 0, 15, 12)).getTimezoneOffset(),
  july: -new Date(Date.UTC(2024, 6, 15, 12)).getTimezoneOffset(),
  time: new Date(Date.UTC(2024, 0, 15, 10, 0, 0)).toLocaleTimeString('en-GB'),
});
"""

WORKER_JS = """() => new Promise((ok) => {
  const w = new Worker('/zone-worker.js');
  w.onmessage = (e) => { ok(e.data); w.terminate(); };
  w.postMessage(0);
})"""

SITE = {
    "/with": (probe.PAGE, "text/html; charset=utf-8", {"Last-Modified": LAST_MODIFIED}),
    "/without": probe.PAGE,
    "/zone-worker.js": (WORKER, "text/javascript"),
}


def _read(page, base, with_last_modified):
    """The page's reads of the zone, from an inline script of its own realm, and a dedicated worker's."""
    page.goto("%s/%s" % (base, "with" if with_last_modified else "without"), wait_until="load", timeout=30_000)
    return {"page": probe.read_in_page_realm(page, PAGE_READ), "worker": page.evaluate(WORKER_JS)}


def _assert_zone(read, zone, label):
    want = EXPECTED[zone]
    page, worker = read["with"]["page"], read["with"]["worker"]
    assert page["timeZone"] == zone, "%s: Intl timeZone in the page realm" % label
    assert (page["january"], page["july"]) == (want["january"], want["july"]), "%s: getTimezoneOffset in the page realm: %r" % (label, page)
    assert page["time"] == want["time"], "%s: a date's wall clock in the page realm" % label
    assert page["lastModified"] == want["last_modified"], "%s: document.lastModified with a Last-Modified header" % label
    assert worker == {"timeZone": zone, "january": want["january"], "july": want["july"], "time": want["time"]}, (
        "%s: a dedicated worker" % label)
    # With no Last-Modified the page, the clock of the document and EXSLT's date-time agree on the zone's present offset.
    now = read["without"]["page"]
    assert now["timeZone"] == zone, "%s: Intl timeZone on a page with no Last-Modified" % label
    assert now["lastModifiedAhead"] == now["now"], (
        "%s: document.lastModified with no Last-Modified is %r, %d minutes ahead of the machine, the offset says %d"
        % (label, now["lastModified"], now["lastModifiedAhead"] or 0, now["now"]))
    assert now["exsltAhead"] == now["now"], (
        "%s: EXSLT date:date-time() is %r, %r minutes ahead of the machine, the offset says %d" % (label, now["exslt"], now["exsltAhead"], now["now"]))


def test_a_launch_and_a_context_timezone_reach_the_page(firefox_binary):
    """⛔ THE ZONE OF A MACHINE LEAKS FROM MANY PLACES, AND ONE OF THEM IS ALWAYS THE LAST TO BE CLOSED. ``Intl`` and ``Date`` were
    declared first; ``document.lastModified`` and EXSLT's ``date:date-time()`` format a time in the C library's local zone, which is the
    host's, and a worker's realm took the process's zone. A page reads all of them in one breath.

    A launch with ``timezone="Pacific/Chatham"``: its default context reads Chatham in every place above. A second context with
    ``timezone_id="Asia/Kolkata"`` of the same browser reads Kolkata in every place, with and without a ``Last-Modified`` header. The
    control is a launch whose declaration is empty (``juggler.timezone.override`` empty, a context that asks for no zone): there the
    values are the host's, and none of them is Chatham's, so the declared zone is what the first reads had.
    """
    from invisible_playwright import InvisiblePlaywright

    srv, base = probe.serve(SITE)
    try:
        with InvisiblePlaywright(seed=5150, binary_path=firefox_binary, headless=True, locale="en-US", timezone=CHATHAM) as browser:
            for label, zone, options in (("the launch's context", CHATHAM, {}), ("a context with timezone_id", KOLKATA, {"timezone_id": KOLKATA})):
                page = browser.new_context(**options).new_page()
                read = {"with": _read(page, base, True), "without": _read(page, base, False)}
                _assert_zone(read, zone, label)

        with InvisiblePlaywright(seed=5150, binary_path=firefox_binary, headless=True, locale="en-US", timezone=CHATHAM,
                                 extra_prefs={"juggler.timezone.override": ""}) as browser:
            page = browser.new_context(timezone_id=None).new_page()
            host = {"with": _read(page, base, True), "without": _read(page, base, False)}
        chatham = EXPECTED[CHATHAM]
        seen = host["with"]["page"]
        assert seen["timeZone"] != CHATHAM, "the launch with no declared zone reads Chatham: the control cannot tell a declaration from the host"
        assert (seen["january"], seen["july"]) != (chatham["january"], chatham["july"]), seen
        assert seen["lastModified"] != chatham["last_modified"], seen
        assert host["with"]["worker"]["timeZone"] != CHATHAM, host["with"]["worker"]
        # What the host reads is consistent with itself: the same clock in every place (the offset the page has now is the wall clock's).
        now = host["without"]["page"]
        assert now["lastModifiedAhead"] == now["now"] and now["exsltAhead"] == now["now"], now
    finally:
        probe.stop(srv)
