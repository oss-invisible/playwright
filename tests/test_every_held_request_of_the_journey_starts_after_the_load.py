"""Every request a page of the journey holds open on purpose is started after `load`, never while the page is parsed.

The steps that assert "an open request holds networkidle" (journey_p4.not_idle_steps in tests/fakeweb/pages.json) are the steps
of a page that holds a request until a later step: a fetch (the category page, steps 7 and 8) or a keepalive fetch (the search
results, step 12). The engine announces a request at once, but the commit of the new document waits for the engine's handshake;
a request started while the page is parsed can be announced before the commit and the client drops it there (the reference
Juggler does the same), so the step would test a race, not the rule. The served page must start every such request from a later
task. No browser: the page is read as it is served and its script is read as text.
"""
from __future__ import annotations

import json
import pathlib
import re
import urllib.request

import pytest

from fakeweb import journey_site, sites

pytestmark = pytest.mark.unit

PAGES_JSON = pathlib.Path(__file__).parent / "fakeweb" / "pages.json"

# The page(s) each step that must not reach networkidle shows while it waits (the steps of profile_p4.py: 7 opens the category
# page, 8 sorts it with pushState on the same document, 12 opens the search page and then its results for a query).
PAGES_OF_STEP = {
    7: ["/category/world"],
    8: ["/category/world?sort=new", "/category/world?sort=top"],
    12: ["/search", "/search?q=quiet%20morning"],
}

# The page each parked name of journey_site stands for.
PATH_OF_PARKED = {"category": "/category/world", "search": "/search"}

# Every page of the journey, to show that no other page holds a request open at all.
JOURNEY_PAGES = ["/", "/category/world", "/article/1", "/search", "/search?q=harbour", "/signin", "/partner/1"]

# A held request with an end: `/hold?id=...&until=<step>`; the long poll `lp` has no end, the page releases it itself.
HELD = re.compile(r"/hold\?id=[^;]*until=")


def _scripts(page: str) -> list[str]:
    out, pos = [], 0
    while True:
        start = page.find("<script>", pos)
        if start < 0:
            return out
        end = page.index("</script>", start)
        out.append(page[start + len("<script>"):end])
        pos = end


def _without_deferred(js: str) -> str:
    """The script with every `afterLoad(...)` call removed: what is left runs while the page is parsed."""
    out, i = [], 0
    while True:
        j = js.find("afterLoad(", i)
        if j < 0:
            out.append(js[i:])
            return "".join(out)
        out.append(js[i:j])
        depth, k, quote = 0, j + len("afterLoad"), None
        while k < len(js):
            c = js[k]
            if quote:
                if c == "\\":
                    k += 1
                elif c == quote:
                    quote = None
            elif c in "'\"`":
                quote = c
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        i = k + 1


def held_at_parse_time(page: str) -> list[str]:
    """The statements of the page's scripts that start a held request outside any afterLoad callback."""
    return [m.group(0) for js in _scripts(page) for m in HELD.finditer(_without_deferred(js))]


def held_requests(page: str) -> list[str]:
    """Every held request the page starts, wherever in its scripts."""
    return [m.group(0) for js in _scripts(page) for m in HELD.finditer(js)]


def _serve(path: str) -> str:
    a, _o, stop = journey_site.start((0, 0))
    try:
        with urllib.request.urlopen(a + path) as r:
            return r.read().decode("utf-8")
    finally:
        stop()


def _as_a_statement(page: str, held_id: str) -> str:
    """The page as it was when the held request was a statement of the script, not an afterLoad callback."""
    prefix = "afterLoad(0, () => "
    start = page.rindex(prefix, 0, page.index("/hold?id=" + held_id))
    end = page.index("));", start) + 2          # the closing of the callback, then of afterLoad
    return page[:start] + page[start + len(prefix):end - 1] + ";" + page[end + 1:]


STEP_PAGES = [(n, path) for n, paths in sorted(PAGES_OF_STEP.items()) for path in paths]


def test_the_steps_that_are_not_idle_are_the_steps_this_test_covers():
    declared = json.loads(PAGES_JSON.read_text(encoding="utf-8"))["journey_p4"]["not_idle_steps"]
    assert sorted(PAGES_OF_STEP) == sorted(declared)


def test_every_page_that_parks_a_request_is_a_page_of_a_step_that_is_not_idle():
    parked = {PATH_OF_PARKED[name] for name in {**journey_site.PARK_PAGES, **journey_site.PARK_KEEPALIVE_PAGES}}
    covered = {path.split("?")[0] for _n, path in STEP_PAGES}
    assert parked == covered


@pytest.mark.parametrize("step,path", STEP_PAGES)
def test_a_page_of_a_step_that_is_not_idle_holds_a_request_and_starts_none_while_it_is_parsed(step, path):
    page = _serve(path)
    assert held_requests(page), "the page of step %d (%s) holds nothing any more: the step has nothing to test" % (step, path)
    assert held_at_parse_time(page) == []


@pytest.mark.parametrize("path", JOURNEY_PAGES)
def test_no_page_of_the_journey_starts_a_held_request_while_it_is_parsed(path):
    assert held_at_parse_time(_serve(path)) == []


@pytest.mark.parametrize("name", list(journey_site.PARK_PAGES))
def test_the_parked_fetch_keeps_its_url_its_method_and_its_end(name):
    page = _serve(PATH_OF_PARKED[name])
    parked = [js for js in _scripts(page) if "id=park' + tok" in js]
    assert parked
    assert "afterLoad(0, () => get(A + '/hold?id=park' + tok + '&until=%d'));" % journey_site.PARK_PAGES[name] in parked[0]


@pytest.mark.parametrize("name", list(journey_site.PARK_KEEPALIVE_PAGES))
def test_the_held_keepalive_fetch_is_a_keepalive_fetch_to_the_hold_route(name):
    page = _serve(PATH_OF_PARKED[name])
    deferred = [js for js in _scripts(page) if "id=kapark' + tok" in js]
    assert deferred and "keepalive: true" in deferred[0]
    assert "afterLoad(0, () => fetch(A + '/hold?id=kapark' + tok" in deferred[0]


def test_afterload_runs_its_callback_from_a_task_after_the_load_event():
    prelude = sites.PRELUDE
    assert "addEventListener('load'" in prelude and "setTimeout(f, ms)" in prelude
    assert "document.readyState === 'complete'" in prelude


@pytest.mark.parametrize("path,held_id", [("/search", "kapark"), ("/category/world", "park")])
def test_the_check_sees_a_request_started_while_the_page_is_parsed(path, held_id):
    """On the old page the held request was a statement of the script, not a callback."""
    old = _as_a_statement(_serve(path), held_id)
    found = held_at_parse_time(old)
    assert found != []
    assert "/hold?id=" + held_id in found[0]
