"""The fake web, profile P4: a user journey over a small multi-page site, driven by the stock Playwright client.

17 steps a person would make (click, hover, scroll, back, forward, a popstate filter, a new tab, a download, a keyboard
search, a sign-in, sign out, "load more") over two origins, with a deterministic ad noise on every page. Every step must
succeed, and `networkidle` must come where stock Firefox has it: on every step except the ones whose page holds a request open
on purpose (declared in tests/fakeweb/pages.json under journey_p4; one of them holds a keepalive fetch, which the client must
count as in flight). Stock Firefox itself is held to that declaration by scripts/run_fakeweb.py --profile p4, which also
compares our engine with it field by field, step by step.

The walk leaves a page (a click or a key that navigates, back or forward, closing a tab, the end of the walk) only once the page says
its noise is done: the noise runs on timers after `load`, and a page left before its last stage sends a different set of requests in
every run. The second test makes that last stage long (LONG_LAST_STAGE_MS after `load`) and reads the site's own record: every page's
late stylesheet, its five woff2 and the release that ends its fetch chain must reach the site before the walk's next navigation.
The last two tests are about the guard of the walk's waits (profile_p4.HANG_GUARD_MS), with a page of the site that holds `load` (journey_site.page_slowload):
a `load` that takes 20 s, more than the 15 s deadline the walk had when run 45176061220 (a slow runner) failed steps with "Timeout 15000ms exceeded", is a step that
succeeds; a `load` that never comes ends in an error that says which wait it was and on which page.
"""
from __future__ import annotations

import importlib
import json
import pathlib

import pytest

from _evidence_on_failure import FailureEvidence
from fakeweb import journey_site, profile_p4

pytestmark = pytest.mark.e2e

HERE = pathlib.Path(__file__).resolve().parent / "fakeweb"
#: The last stage of the noise in the second test, in ms after `load`: far longer than the walk spends on a page it passes through
#: (step 15 clicks Account as soon as the home page has loaded), so a walk that does not wait for the page leaves before that stage.
LONG_LAST_STAGE_MS = 3000
#: How long the page of the third test holds `load`, in ms: more than the 15 s that failed the walk of run 45176061220 (15.7, 18.9 and 22.1 s in that launch), less than the guard.
SLOW_LOAD_MS = 20000
#: The guard of the fourth test: a wait for something that must NOT happen (`load` of a page that never loads) ends at this deadline, and the test wants it to.
NEGATIVE_HANG_GUARD_MS = 3000
#: The engine's own log of where a mouse event goes, kept with the evidence of the two walks (one file per process): ``apz.inputqueue`` and
#: ``apz.child`` say whether the event left the parent for the content process and how the content process took it, ``MouseBoundaryEvents``
#: says whether the content process turned it into the page's mouse events. Run 45668588036 lost the first mousemove to a page whose
#: BrowsingContext had just been replaced (no ack in 5 s, the content process alive), and its evidence could not say where the event died.
#: A diagnostic no page reads.
INPUT_LOG = "timestamp,apz.inputqueue:4,apz.child:4,MouseBoundaryEvents:4"


def _stock_client():
    """The stock Playwright client (a pinned dev dependency), imported by name: the fork's guard forbids a static import."""
    try:
        return importlib.import_module("playwright.sync_api").sync_playwright
    except ImportError as exc:      # a broken environment, not a reason to skip: a skipped gate checks nothing
        pytest.fail("the stock Playwright client is missing (%s): install the wrapper with its dev extra" % exc,
                    pytrace=False)


def test_the_journey_succeeds_on_every_step_and_idles_where_stock_does(firefox_binary):
    """When it fails it saves, under `invpw-evidence/fakeweb-journey-idles-where-stock-does/`, what answers whether the browser sent the
    request a step holds, when the site received and answered it, and what the client heard of it before `networkidle` returned:
    `fakeweb-server-record.jsonl` (the site's own record), `client-request-log.jsonl` (every request event the client heard, with the step),
    `cells.json` (each step's record with `in_flight_at_idle_end`), `juggler-wire.jsonl`, `juggler-engine-trace/` and `moz-log/` (INPUT_LOG)."""
    not_idle = json.loads((HERE / "pages.json").read_text(encoding="utf-8"))["journey_p4"]["not_idle_steps"]
    with FailureEvidence("fakeweb-journey-idles-where-stock-does") as evidence:
        origin_a, origin_o, stop = journey_site.start((0, 0))
        evidence.watch_server(stop)
        step_log = []
        evidence.note_cells(step_log)
        try:
            evidence.arm(moz_log=INPUT_LOG)
            steps = profile_p4.run_on_candidate(_stock_client(), firefox_binary, origin_a, origin_o, 1,
                                                client_log=evidence.client_log, step_log=step_log)
        finally:
            stop()
        failed = ["step %d (%s): %s" % (s["n"], s["name"], s["error"]) for s in steps if not s["ok"]]
        assert not failed, "steps that did not succeed:\n  " + "\n  ".join(failed)
        wrong = ["step %d (%s): %s" % (s["n"], s["name"], "idle, stock holds a request here" if s["idle"] else
                 "not idle, open: %s" % s["open_at_idle_end"]) for s in steps if s["idle"] != (s["n"] not in not_idle)]
        assert not wrong, "networkidle differs from the declared outcome of stock Firefox:\n  " + "\n  ".join(wrong)


def test_the_journey_leaves_every_page_only_once_its_noise_is_done(firefox_binary):
    """The last stage of every page's noise starts LONG_LAST_STAGE_MS after `load`. The site's own record must show, for every page of
    the walk, its stylesheet `ld3` and the `release?id=lp...` that ends its fetch chain (with the page as their referrer) and the woff2 of its five @font-face rules arriving before the
    walk's next navigation (journey_site.pages_left_before_their_noise). When it fails it saves, under
    `invpw-evidence/fakeweb-journey-leaves-a-page-when-its-noise-is-done/`, `fakeweb-server-record.jsonl` (every request with its
    referrer and the moment it arrived), `client-request-log.jsonl`, `cells.json` (each step's moments) and `moz-log/` (INPUT_LOG)."""
    with FailureEvidence("fakeweb-journey-leaves-a-page-when-its-noise-is-done") as evidence:
        origin_a, origin_o, stop = journey_site.start((0, 0), last_stage_ms=LONG_LAST_STAGE_MS)
        evidence.watch_server(stop)
        step_log = []
        evidence.note_cells(step_log)
        try:
            evidence.arm(moz_log=INPUT_LOG)
            steps = profile_p4.run_on_candidate(_stock_client(), firefox_binary, origin_a, origin_o, 1,
                                                client_log=evidence.client_log, step_log=step_log)
        finally:
            stop()
        failed = ["step %d (%s): %s" % (s["n"], s["name"], s["error"]) for s in steps if not s["ok"]]
        assert not failed, "steps that did not succeed:\n  " + "\n  ".join(failed)
        with stop.state.lock:
            records = list(stop.state.records)
        assert any(r.get("ev") == "request" and r["path"] == "/" for r in records), "the site saw no walk"
        left = journey_site.pages_left_before_their_noise(records, stop.state.origins)
        assert not left, "pages the walk left before their noise was done:\n  " + "\n  ".join(left)


def _on_candidate(firefox_binary, origin_a, origin_o, hang_guard_ms, run):
    """`run(journey, page)` with the walk's own Journey on a fresh context of our engine, served over the Playwright WebSocket and driven by the stock client,
    with the context configured as `profile_p4.drive` does (the guard as the default of actions and navigations, the route that blocks images and media)."""
    from invisible_playwright._juggler.remote import launch_server

    server = launch_server(headless=True, binary_path=firefox_binary, locale="en-US", timezone="Europe/Rome")
    try:
        with _stock_client()() as p:
            browser = p.firefox.connect(server.ws_endpoint)
            try:
                context = browser.new_context()
                context.set_default_timeout(hang_guard_ms)
                context.set_default_navigation_timeout(hang_guard_ms)
                profile_p4.install_route(context)
                page = context.new_page()
                return run(profile_p4.Journey(context, page, origin_a, origin_o, hang_guard_ms), page)
            finally:
                browser.close()
    finally:
        server.close()


def test_a_click_to_a_page_that_holds_load_for_20_s_is_a_step_that_succeeds(firefox_binary):
    """Run 45176061220: from the click to `load` the walk took 15.7, 18.9 and 22.1 s on a runner that was 3 to 4 times slower, and the 15 s of the context failed the
    step. With the guard of the walk (profile_p4.HANG_GUARD_MS, 120 s) a `load` held SLOW_LOAD_MS by the site (a script the server answers late) is a navigation
    that arrives: the click is `nav_click`, the one of the walk's steps."""
    origin_a, origin_o, stop = journey_site.start((0, 0))
    try:
        def run(journey, page):
            page.goto(origin_a + "/slowlink?noise=0&id=late&ms=%d" % SLOW_LOAD_MS)
            journey.nav_click("#go")
            return journey.norm(page.url), page.evaluate("document.readyState")

        arrived, state = _on_candidate(firefox_binary, origin_a, origin_o, profile_p4.HANG_GUARD_MS, run)
    finally:
        stop()
    assert arrived.startswith("A/slowload?") and state == "complete"


def test_a_page_that_never_loads_ends_in_an_error_that_names_the_wait_and_the_page(firefox_binary):
    """The page holds `load` until the test releases it, the guard is NEGATIVE_HANG_GUARD_MS: the click's navigation ends with the walk's own error, which says `navigation to
    load` and the page (the first line, the one the verdict compares), and, on the second, the last request the site saw; never the client's "Timeout Nms exceeded"."""
    origin_a, origin_o, stop = journey_site.start((0, 0))
    try:
        def run(journey, page):
            page.goto(origin_a + "/slowlink?noise=0&id=never")
            try:
                journey.nav_click("#go")
            except profile_p4.StageTimeout as exc:
                return str(exc)
            return None

        try:
            message = _on_candidate(firefox_binary, origin_a, origin_o, NEGATIVE_HANG_GUARD_MS, run)
        finally:
            stop.state.release("slow-never")
    finally:
        stop()
    assert message is not None, "the click's navigation ended, but the page never loads"
    first, second = message.splitlines()
    assert first.startswith("navigation to load did not finish in 3 s (hang guard) on A/slowload?"), first
    assert second.startswith("last request the site saw: GET A/"), second
    assert "Timeout" not in message
