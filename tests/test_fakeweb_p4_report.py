"""The reader of the profile P4 report (scripts/fakeweb_p4_report.py) and the shape of the workflow that runs it.

The verdict of the job is made here, so it is tested here: a difference on a sound walk is red, a broken control is red with
another message and is not counted as a difference of ours, a clean run is green, and a sendBeacon reported with another
resource type than `beacon` is red. With a traced stock walk the trace verdict is "same as stock" per kind of request.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fakeweb_p4_report as report  # noqa: E402


def _walk(rounds=2, steps=3):
    cand, stock = [], []
    for r in range(1, rounds + 1):
        for n in range(1, steps + 1):
            rec = {"round": r, "n": n, "name": "step %d" % n, "ok": True, "idle": n != 2}
            cand.append(dict(rec))
            stock.append(dict(rec))
    return {"profile": "p4", "candidate": cand, "stock": stock, "fails": [], "broken_control": []}


def test_a_clean_run_is_green_and_every_step_is_equal():
    text, code = report.journey(_walk())
    assert code == 0
    assert text.count("| equal |") == 6
    assert "Verdict: green" in text and "sound" in text


def test_a_difference_on_a_sound_walk_is_red():
    data = _walk()
    data["candidate"][1]["idle"] = True      # step 2 of walk 1: stock is busy, ours idles
    data["fails"] = [(1, 2, "idle", False, True)]
    text, code = report.journey(data)
    assert code == 1
    assert "different (idle)" in text
    assert "FAIL  round 1 step 2 idle: stock=False candidate=True" in text
    assert "RED, ours differs from stock" in text


def test_a_broken_control_is_red_with_its_own_message_and_is_not_a_difference_of_ours():
    data = _walk()
    data["stock"][0]["idle"] = False
    data["broken_control"] = [(1, 1, "idle", "idle=False, declared idle")]
    data["fails"] = [(1, 1, "idle", False, True)]      # the difference exists only because the control moved
    text, code = report.journey(data)
    assert code == 2
    assert "BROKEN CONTROL" in text and "broken control (not a difference of ours)" in text


def test_a_difference_on_a_sound_walk_and_a_broken_control_on_another_is_both():
    data = _walk()
    data["broken_control"] = [(1, 1, "ok", "boom")]
    data["fails"] = [(2, 3, "title", "a", "b")]
    assert report.journey(data)[1] == 3


def test_a_missing_report_is_red(tmp_path, capsys):
    assert report.main(["journey", str(tmp_path / "nothing.json")]) == 4
    assert "RED" in capsys.readouterr().out


def _client(method, url, typ):
    return {"ev": "request", "method": method, "url": url, "type": typ, "t": 0}


def _server(method, path, ident="", n="", src=""):
    return {"method": method, "path": path, "id": ident, "n": n, "src": src}


def _trace(beacon_type="beacon", server_ka=1, client_ka=1):
    client = [_client("POST", "/beacon?src=load&n=1", beacon_type), _client("POST", "/beacon?src=ad&n=4", beacon_type),
              _client("POST", "/beacon?src=load&n=3", "fetch")]
    client += [_client("GET", "/r?kind=json&id=ka1", "fetch")] * client_ka
    server = [_server("POST", "/beacon", n="1", src="load"), _server("POST", "/beacon", n="3", src="load")]
    server += [_server("GET", "/r", ident="ka1")] * server_ka
    return {"candidate": [{"client_events": client, "server_events": server}]}


def test_trace_is_green_when_every_sendbeacon_is_a_beacon_and_no_keepalive_is_unreported():
    text, code = report.trace(_trace())
    assert code == 0, text
    assert "| sendBeacon | 2 | beacon: 2 | 1 |" in text
    assert "| keepalive POST | 1 | fetch: 1 | 1 |" in text      # told from sendBeacon by n=3 of src=load
    assert "| keepalive fetch | 1 | fetch: 1 | 1 |" in text


def test_trace_is_red_when_a_sendbeacon_has_another_resource_type():
    text, code = report.trace(_trace(beacon_type="ping"))
    assert code == 1
    assert "other than `beacon`" in text


def test_trace_is_red_when_the_server_saw_a_keepalive_the_client_never_reported():
    text, code = report.trace(_trace(server_ka=2, client_ka=1))
    assert code == 1
    assert "keepalive fetch: the server saw 2, the client reported 1" in text


def test_trace_is_red_when_there_is_no_sendbeacon_at_all():
    assert report.trace({"candidate": [{"client_events": [], "server_events": []}]})[1] == 1


def _records(beacon_type="beacon", server_ka=1, client_ka=1, beacons=2):
    """One step record: `beacons` sendBeacon requests of resource type `beacon_type` (all reported and all seen),
    one keepalive POST, and `client_ka` reported / `server_ka` seen keepalive fetches."""
    return _trace(beacon_type, server_ka, client_ka)["candidate"]


def _both(stock=None, ours=None):
    return {"stock": _records(**(stock or {})), "candidate": _records(**(ours or {}))}


def test_trace_with_a_stock_walk_is_green_when_the_types_are_equal():
    text, code = report.trace(_both())
    assert code == 0, text
    assert "| sendBeacon | 2 | beacon: 2 | 1 | 2 | beacon: 2 | 1 |" in text


def test_trace_with_a_stock_walk_does_not_compare_exact_counts():
    ours = _records()
    ours[0]["client_events"] = ours[0]["client_events"] + [_client("POST", "/beacon?src=ad&n=5", "beacon")]
    assert report.trace({"stock": _records(), "candidate": ours})[1] == 0


def test_trace_with_a_stock_walk_is_red_when_ours_reports_other_where_stock_says_beacon():
    text, code = report.trace(_both(ours={"beacon_type": "other"}))
    assert code == 1
    assert "sendBeacon" in text and "other" in text and "beacon" in text and "stock reported {beacon}" in text


def test_trace_with_a_stock_walk_is_green_when_stock_does_the_same_odd_thing():
    assert report.trace(_both(stock={"beacon_type": "ping"}, ours={"beacon_type": "ping"}))[1] == 0


def test_trace_with_a_stock_walk_is_red_when_ours_leaves_more_unreported_than_stock():
    text, code = report.trace(_both(stock={"server_ka": 2, "client_ka": 2}, ours={"server_ka": 3, "client_ka": 1}))
    assert code == 1
    assert "keepalive fetch: the server saw 2 request(s) ours never reported, stock left 0 unreported" in text


def test_trace_with_a_stock_walk_is_green_when_both_leave_the_same_unreported():
    text, code = report.trace(_both(stock={"server_ka": 3, "client_ka": 1}, ours={"server_ka": 3, "client_ka": 1}))
    assert code == 0, text
    assert "| keepalive fetch | 1 | fetch: 1 | 3 | 1 | fetch: 1 | 3 |" in text


def test_trace_with_a_stock_walk_that_reported_no_sendbeacon_is_a_broken_control():
    stock = [{"client_events": [_client("GET", "/r?kind=json&id=ka1", "fetch")], "server_events": []}]
    text, code = report.trace({"stock": stock, "candidate": _records()})
    assert code == 2
    assert "BROKEN CONTROL" in text and "not a difference of ours" in text


def test_a_stock_synthesized_for_no_stock_has_no_events_and_keeps_the_absolute_rules():
    data = _trace()
    data["stock"] = [{"round": 1, "n": 1, "ok": True, "idle": True}]
    assert report.trace(data)[1] == 0
    data = _trace(beacon_type="ping")
    data["stock"] = [{"round": 1, "n": 1, "ok": True, "idle": True}]
    assert report.trace(data)[1] == 1


def test_the_site_tags_events_with_the_walk_and_a_late_event_is_collected_with_its_step():
    """The lost-event case, without a browser: a request fired on pagehide reaches the server after the step's own read
    but before the next step is set. Reading once at the end of the walk, by walk and step, finds it; the walks do not mix."""
    from fakeweb import journey_site

    state = journey_site.State()
    assert state.begin_walk() == 1
    state.set_step(1)
    state.record(0, "GET", "/", {})
    first_read = state.events_of(1, 1)                      # what the old read at the end of step 1 saw
    state.record(1, "POST", "/beacon", {"t": ["pagehide"], "n": ["1"]})      # the pagehide beacon, after that read
    state.set_step(2)
    state.record(0, "GET", "/article/1", {})
    assert [e["path"] for e in first_read] == ["/"]
    assert [e["path"] for e in state.events_of(1, 1)] == ["/", "/beacon"]
    assert [e["path"] for e in state.events_of(1, 2)] == ["/article/1"]
    assert state.begin_walk() == 2
    state.set_step(1)
    state.record(0, "GET", "/", {})
    assert len(state.events_of(2, 1)) == 1 and len(state.events_of(1, 1)) == 2
    assert all(e["walk"] in (1, 2) for e in state.events)


def test_the_site_records_what_the_report_reads_and_the_tracer_keeps_what_the_client_reports():
    """The two ends of --trace, without a browser: the hits the site logs (with the parameters the classifier needs) and the
    request events the tracer keeps; both must be read back by the report as the same kinds."""
    import urllib.request

    from fakeweb import journey_site, profile_p4

    a, _o, stop = journey_site.start((0, 0))
    try:
        journey_site.STATE.set_step(3)
        urllib.request.urlopen(urllib.request.Request(a + "/beacon?src=load&n=3", data=b"x", method="POST")).read()
        urllib.request.urlopen(a + "/r?kind=json&id=ka1").read()
        events = [e for e in journey_site.STATE.events if e["step"] == 3]
    finally:
        stop()
    assert [report._server_kind(e) for e in events] == ["keepalive POST", "keepalive fetch"]

    class Request:
        def __init__(self, method, url, typ):
            self.method, self.url, self.resource_type = method, url, typ

    handlers = {}

    class Context:
        def on(self, name, fn):
            handlers[name] = fn

    tracer = profile_p4.Tracer(Context(), events=True)
    beacon = Request("POST", a + "/beacon?src=ad&n=9", "beacon")
    handlers["request"](beacon)
    assert tracer.snapshot() == ["beacon beacon?src=ad&n=9"]
    handlers["requestfinished"](beacon)
    taken = tracer.take_events()
    assert [e["ev"] for e in taken] == ["request", "finished"] and report._client_kind(taken[0]) == "sendBeacon"
    assert tracer.take_events() == [] and tracer.snapshot() == []
    assert profile_p4.Tracer(Context()).events is None      # without --trace nothing is kept


# the pages the walk left before their noise was done (journey_site.pages_left_before_their_noise) --------------------------------------
A, O = "http://127.0.0.1:1", "http://localhost:2"


def _request(m, site, method, path, referer=None, status=200, ctype=None, step=1):
    if ctype is None:
        ctype = "text/html; charset=utf-8" if not path.startswith("/r?") else "text/css"
    return {"ev": "request", "m_arrive": m, "site": site, "method": method, "path": path, "referer": referer, "status": status,
            "ctype": ctype, "step": step}


def _noise(m, site, path):
    """The last stage of a page, its five woff2 and the release that ends its fetch chain, from moment `m` on."""
    referrer = ([A, O][site]) + path      # a page asks its own origin for the stylesheet: same origin, the whole URL
    return [_request(m, 0, "GET", "/r?kind=css&id=ld3", referrer)] + [
        _request(m + 0.01 * (k + 1), 1, "GET", "/r?kind=woff2&id=font", O + "/r?kind=fontcss&id=fc", ctype="font/woff2") for k in range(5)] + [
        _request(m + 0.07, site, "GET", "/release?id=lpq7x1z2", referrer, ctype="text/plain")]


def test_a_walk_that_leaves_every_page_after_its_noise_has_no_problem():
    from fakeweb import journey_site

    records = ([_request(1, 0, "GET", "/")] + _noise(2, 0, "/") + [_request(3, 0, "GET", "/account", status=302, ctype=None)]
               + [_request(4, 0, "GET", "/signin?x=1")] + _noise(5, 0, "/signin?x=1")
               + [_request(6, 1, "GET", "/partner/1")] + _noise(7, 1, "/partner/1")
               + [_request(7.5, 0, "GET", "/download/1.txt", ctype="text/plain; charset=utf-8")]
               + [_request(8, 0, "POST", "/signin")] + _noise(9, 0, "/signin"))
    assert journey_site.pages_left_before_their_noise(records, [A, O]) == []


def test_a_page_left_before_its_late_stylesheet_and_its_fonts_is_found():
    """Step 15 of the walk as the network oracle saw it: the home page is left (the request of /account) before its last stage."""
    from fakeweb import journey_site

    records = ([_request(1, 0, "GET", "/", step=15), _request(1.1, 0, "GET", "/account", step=15)] + _noise(1.2, 0, "/")
               + _noise(2, 0, "/account"))
    left = journey_site.pages_left_before_their_noise(records, [A, O])
    assert len(left) == 3 and all("GET A/ (step 15)" in line for line in left)
    assert "ld3" in left[0] and "release that ends its fetch chain" in left[1] and "0 woff2" in left[2]


def test_a_late_stylesheet_of_another_page_does_not_count_for_the_page():
    """The referrer tells the pages apart: the stylesheet of the page left behind arrives while the next page is open."""
    from fakeweb import journey_site

    records = ([_request(1, 0, "GET", "/article/2")] + _noise(2, 0, "/article/2")[1:] + [_request(3, 0, "GET", "/article/3")]
               + _noise(4, 0, "/article/3") + [_request(4.5, 0, "GET", "/r?kind=css&id=ld3", A + "/article/2")])
    assert journey_site.pages_left_before_their_noise(records, [A, O]) == [
        "GET A/article/2 (step 1): its stylesheet ld3 (referrer %s/article/2) had not arrived when the walk left the page" % A]


def test_a_page_left_before_the_release_that_ends_its_fetch_chain_is_found():
    """The network oracle of the run 45088980743 (Windows, job 137413984973): the home page of step 15 had sent its last stage and its fonts, but
    its chain of seven fetches had not yet sent `release?id=lp...` when /account was requested; the other run of the same engine had it."""
    from fakeweb import journey_site

    home = _noise(2, 0, "/")
    records = ([_request(1, 0, "GET", "/")] + home[:-1] + [_request(3, 0, "GET", "/account")] + _noise(4, 0, "/account"))
    assert journey_site.pages_left_before_their_noise(records, [A, O]) == [
        "GET A/ (step 1): the release that ends its fetch chain (referrer %s/) had not arrived when the walk left the page" % A]
    late = records + [_request(3.5, 0, "GET", "/release?id=lpq7x1z2", A + "/", ctype="text/plain")]      # after /account was asked for
    assert len(journey_site.pages_left_before_their_noise(late, [A, O])) == 1


def test_the_page_of_the_other_origin_counts_its_fonts_with_the_page_open_beside_it():
    """The partner tab opens while the article is open: the stretch until the next navigation holds both pages, so ten woff2."""
    from fakeweb import journey_site

    records = ([_request(1, 0, "GET", "/article/2")] + _noise(2, 0, "/article/2") + [_request(3, 1, "GET", "/partner/2")]
               + _noise(4, 1, "/partner/2")[:3] + _noise(4, 1, "/partner/2")[-1:] + [_request(5, 0, "GET", "/search")] + _noise(6, 0, "/search"))
    left = journey_site.pages_left_before_their_noise(records, [A, O])
    assert left == ["GET A/article/2 (step 1), GET O/partner/2 (step 1): 7 woff2 had arrived when the walk left, 10 expected "
                    "(5 @font-face per page)"]


def test_the_page_of_the_other_origin_is_found_by_its_own_url_as_the_referrer_of_its_late_stylesheet():
    """The partner page (origin O) loads its stylesheet ld3 from O itself: a same-origin request carries the whole URL of the page, as in
    the run 45074928774 (job 137370233977: `GET /r?kind=css&id=ld3` with the referrer `O/partner/8`, 3 s after `load`). A partner tab
    closed before that stage, and a stylesheet of another partner page, are not the page's."""
    from fakeweb import journey_site

    records = ([_request(1, 0, "GET", "/article/8")] + _noise(2, 0, "/article/8") + [_request(3, 1, "GET", "/partner/8")]
               + _noise(4, 1, "/partner/8") + [_request(5, 0, "GET", "/search")] + _noise(6, 0, "/search"))
    assert journey_site.pages_left_before_their_noise(records, [A, O]) == []
    closed_early = ([_request(1, 0, "GET", "/article/8")] + _noise(2, 0, "/article/8") + [_request(3, 1, "GET", "/partner/8")]
                    + [_request(5, 0, "GET", "/search")] + _noise(6, 0, "/search") + _noise(7, 1, "/partner/9"))
    left = journey_site.pages_left_before_their_noise(closed_early, [A, O])
    assert left[0] == ("GET O/partner/8 (step 1): its stylesheet ld3 (referrer %s/partner/8) had not arrived when the walk left the "
                       "page" % O)


def test_the_site_keeps_the_referrer_and_the_type_of_every_answer_and_the_lab_can_make_the_last_stage_long():
    import urllib.request

    from fakeweb import journey_site

    a, _o, stop = journey_site.start((0, 0), last_stage_ms=3000)
    try:
        page = urllib.request.urlopen(urllib.request.Request(a + "/", headers={"Referer": a + "/article/1"})).read().decode()
        urllib.request.urlopen(a + "/r?kind=css&id=ld3").read()
    finally:
        stop()
    home, css = [r for r in stop.state.records if r["ev"] == "request"]
    assert (home["referer"], home["ctype"], css["referer"], css["ctype"]) == (a + "/article/1", "text/html; charset=utf-8", None, "text/css")
    assert "LAST_MS=3000;" in page and "afterLoad(LAST_MS, () => {" in page and "/r?kind=css&id=ld3" in page
    a, _o, stop = journey_site.start((0, 0))
    try:
        page = urllib.request.urlopen(a + "/").read().decode()
    finally:
        stop()
    assert "LAST_MS=%d;" % journey_site.LAST_STAGE_MS in page and journey_site.LAST_STAGE_MS == 600


# the walk leaves a page only once the page has declared its noise done (profile_p4.Journey.leave) -------------------------------------
class _Page:
    """A page of the walk without a browser: it logs what the walk does to it; `moves` maps an action to the URLs it lands on, in turn."""

    def __init__(self, log, url, moves=None):
        self.log, self.url, self.moves = log, url, {k: list(v) for k, v in (moves or {}).items()}
        page = self

        class Keyboard:
            def press(self, key):
                page.act("press " + key)

            def type(self, text, delay=0):
                page.log.append("type")

        self.keyboard = Keyboard()

    def act(self, what):
        self.log.append(what)
        if self.moves.get(what):
            self.url = self.moves[what].pop(0)

    def wait_for_function(self, expression, timeout=None):
        self.log.append(("wait", expression, timeout))

    def locator(self, selector):
        page = self

        class Locator:
            first = property(lambda self: self)

            def hover(self):
                page.log.append("hover " + selector)

            def click(self):
                page.act("click " + selector)

            def count(self):
                return 3

        return Locator()

    def click(self, selector):
        self.act("click " + selector)

    def expect_navigation(self, **expect):
        page = self

        class Navigation:
            def __enter__(self):
                page.log.append("expect")

            def __exit__(self, *exc):
                page.log.append("navigated")
                return False

        return Navigation()

    def go_back(self, wait_until=None, timeout=None):
        self.act("back")

    def go_forward(self, wait_until=None, timeout=None):
        self.act("forward")

    def wait_for_load_state(self, state, timeout=None):
        self.log.append("load " + state)

    def wait_for_selector(self, selector):
        self.log.append("selector " + selector)

    def inner_text(self, selector):
        return "quiet morning" if selector == "#qshow" else "alice"

    def evaluate(self, expression):
        return 1        # window.__keep: the page did not navigate where the walk says it must not

    def close(self):
        self.log.append("close")


def _waited_before(log, what):
    """Every `what` of the log comes right after a wait for the page's declaration, with a deadline of at least 30 s."""
    from fakeweb import profile_p4

    at = [i for i, entry in enumerate(log) if entry == what]
    assert at, "the walk did not %s" % what
    return all(i > 0 and log[i - 1][:2] == ("wait", profile_p4.NOISE_DONE) and log[i - 1][2] >= 30000 for i in at)


def test_step_15_leaves_the_home_page_only_once_its_noise_is_done():
    """The case the network oracle saw: from the account page the logo opens the home page, and Account is clicked on it at once."""
    from fakeweb import profile_p4

    log = []
    page = _Page(log, A + "/account", {"click #logo": [A + "/"], "click #nav-account": [A + "/account"]})
    profile_p4.Journey(None, page, A, O).s15_logo_account()
    assert [e if isinstance(e, str) else e[0] for e in log] == ["wait", "expect", "click #logo", "navigated",
                                                               "wait", "expect", "click #nav-account", "navigated"]
    assert _waited_before(log, "expect")


def test_the_keys_that_send_a_form_leave_the_page_only_once_its_noise_is_done():
    """Step 14: Sign in, then Enter twice (the wrong password, then the right one); step 12: Enter on the search page."""
    from fakeweb import profile_p4

    log = []
    page = _Page(log, A + "/", {"click #nav-signin": [A + "/signin"], "press Enter": [A + "/signin", A + "/account"]})
    profile_p4.Journey(None, page, A, O).s14_signin()
    assert [e for e in log if e == "press Enter"] == ["press Enter"] * 2
    assert _waited_before(log, "expect")
    log = []
    page = _Page(log, A + "/", {"click #nav-search": [A + "/search"], "press Enter": [A + "/search?q=quiet+morning"]})
    profile_p4.Journey(None, page, A, O).s12_search()
    assert log.count("press Enter") == 1 and _waited_before(log, "expect")


def test_back_and_forward_leave_the_page_only_once_its_noise_is_done():
    from fakeweb import profile_p4

    log = []
    page = _Page(log, A + "/article/3", {"back": [A + "/article/2"], "forward": [A + "/article/3"]})
    journey = profile_p4.Journey(None, page, A, O)
    journey.s4_back()
    assert _waited_before(log, "back")
    assert journey.history_step("forward") and _waited_before(log, "forward")


def test_the_tab_and_the_last_page_are_left_only_once_their_noise_is_done(monkeypatch):
    """Step 10 closes the partner tab; when the walk ends the context closes the page it is on."""
    from fakeweb import journey_site, profile_p4

    log = []
    tab = _Page(log, O + "/partner/2")
    journey = profile_p4.Journey(None, _Page([], A + "/article/2"), A, O)
    journey.leave(tab.close, page=tab)
    assert _waited_before(log, "close")

    class Walk(profile_p4.Journey):
        def s_only(self):
            self.page.log.append("step")
            return self.page, None

    class Tracer:
        events, step = None, 0

        def snapshot(self):
            return []

    log = []
    page = _Page(log, A + "/")
    walk = Walk(None, page, A, O)
    walk.ctx = type("Context", (), {"pages": [page]})()
    monkeypatch.setattr(profile_p4, "STEPS", [("the only step", "s_only")])
    monkeypatch.setattr(journey_site, "STATE", journey_site.State())
    records = profile_p4.run_round(walk, Tracer(), 1, 30000)
    assert records[0]["ok"] and log[0] == "step" and log[-1][:2] == ("wait", profile_p4.NOISE_DONE)


def test_step_10_closes_the_partner_tab_only_once_its_noise_is_done():
    """The cleanup step 10 returns closes the tab the click opened, and only after the wait for the tab's own declaration (the last stage of
    the partner page comes seconds after `load`: a tab closed at once never sends its late stylesheet)."""
    from fakeweb import profile_p4

    log = []

    class Tab(_Page):
        def inner_text(self, selector):
            return "Partner 8 (other origin)"

        def wait_for_selector(self, selector):
            self.log.append("selector " + selector)

    tab = Tab(log, O + "/partner/8")

    class Context:
        pages = [object(), tab]

        def expect_page(self):
            class Opened:
                value = tab

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            return Opened()

    journey = profile_p4.Journey(Context(), _Page([], A + "/article/8"), A, O)
    seen, cleanup = journey.s10_blank_tab()
    assert seen is tab and "close" not in log
    cleanup()
    assert log[-1] == "close" and _waited_before(log, "close")


def test_every_navigation_back_forward_and_close_of_the_walk_goes_through_its_one_point_of_leaving():
    """Read from the source: the walk waits for a navigation only in Journey.navigate, presses back or forward only in Journey._press
    (and in the popstate step, which stays on its document), opens an address only to open the first page, never reloads, and closes
    a page only through Journey.leave."""
    import ast

    from fakeweb import profile_p4

    tree = ast.parse(Path(profile_p4.__file__).read_text(encoding="utf-8"))
    journey = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Journey")
    allowed = {"expect_navigation": {"navigate"}, "go_back": {"_press", "s8_filter_popstate"}, "go_forward": {"_press"},
               "goto": {"s1_open_home"}, "reload": set()}      # goto: the first page, from a blank one
    wrong = []
    for method in (n for n in journey.body if isinstance(n, ast.FunctionDef)):
        passed_to_leave = {id(a) for c in ast.walk(method) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                           and c.func.attr == "leave" for a in c.args}
        for node in ast.walk(method):
            if isinstance(node, ast.Attribute) and node.attr in allowed and method.name not in allowed[node.attr]:
                wrong.append("%s: %s" % (method.name, node.attr))
            if (isinstance(node, ast.Attribute) and node.attr == "close" and id(node) not in passed_to_leave
                    and not (isinstance(node.value, ast.Name) and node.value.id == "os")):      # os.close: a file, not a page
                wrong.append("%s: close not through leave" % method.name)
    assert wrong == []


def test_a_page_of_the_site_declares_when_its_noise_is_done():
    """With the noise, once the font stage and the last stage have loaded and the fetch chain and the three streams have sent their release
    (window.__noiseDone); without it, at once. A held request of a parked page is not part of it."""
    import urllib.request

    from fakeweb import journey_site

    a, o, stop = journey_site.start((0, 0))
    try:
        noisy, partner = (urllib.request.urlopen(u).read().decode() for u in (a + "/", o + "/partner/2"))
        quiet = urllib.request.urlopen(a + "/?noise=0").read().decode()
    finally:
        stop()
    for page in (noisy, partner):
        assert "const noiseDone = () => { if (fontsDone && lastDone && chainDone && streamsOpen === 0) window.__noiseDone = true; };" in page
        assert "rel('lp' + tok).then(() => { chainDone = true; noiseDone(); })" in page
        assert "return rel('stream-ab' + i); }).catch(noop)" in page and "streamsOpen--; noiseDone();" in page
        assert page.count("l.addEventListener('load', ") == 2 and page.count("l.addEventListener('error', ") == 2
        assert "document.fonts.ready.then(() => { fontsDone = true; noiseDone(); })" in page
        assert "const last = () => { lastDone = true; noiseDone(); };" in page
    assert "<script>window.__noiseDone = true;</script>" in quiet and "fontsDone" not in quiet


# the workflow ------------------------------------------------------------------------------------------------------------
WORKFLOW = (ROOT / ".github" / "workflows" / "fakeweb.yml").read_text(encoding="utf-8")


def test_the_workflow_has_four_legs_and_one_verdict_job():
    assert len(re.findall(r"^  [a-z0-9_-]+:\n    (?:name|uses):", WORKFLOW, re.MULTILINE)) == 3     # core-on-index, walk, verdict
    assert WORKFLOW.count("{ profile: ") == 4
    for leg in ("p2, os: linux", "p2, os: windows", "p4, os: linux", "p4, os: windows"):
        assert "profile: " + leg in WORKFLOW
    assert "name: fakeweb verdict" in WORKFLOW and "if: always()" in WORKFLOW.split("name: fakeweb verdict", 1)[1]
    assert '"${{ needs.walk.result }}" != "success"' in WORKFLOW        # a leg that did not run is not green


def test_every_action_is_pinned_to_a_commit():
    uses = re.findall(r"uses: (\S+)", WORKFLOW)
    assert uses and all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", u) or u.startswith("./.github/workflows/") for u in uses), uses


def test_the_workflow_runs_stock_and_ours_and_never_reruns_or_forgives():
    assert "--no-stock" not in WORKFLOW and "rerun" not in WORKFLOW.lower() and "continue-on-error" not in WORKFLOW
    assert "|| true" not in WORKFLOW and "|| echo" not in WORKFLOW
    assert "fetch | tail -1" in WORKFLOW                      # the published engine, as e2e.yml
    assert "gh run download" not in WORKFLOW                 # no engine of a named run: the candidate runs the entry itself
    # the lab database and the security state are made by the repository before the first launch (the scripts launch the core as the tests do)
    first_run = WORKFLOW.index("      - name: Profile P2")
    assert WORKFLOW.index("scripts/ci_lab_geoip.py build") < first_run and WORKFLOW.index("scripts/ci_lab_security_state.py set") < WORKFLOW.index("fetch | tail -1")


def test_the_workflow_starts_on_the_fake_web_files_of_a_pull_request_and_of_main_and_by_hand():
    on = WORKFLOW.split("\npermissions:", 1)[0]
    assert "  push:\n    branches: [main]\n    paths:" in on and "  pull_request:\n    paths:" in on and "  workflow_dispatch:" in on
    assert on.count("'tests/fakeweb/**'") == 2 and on.count("'.github/workflows/fakeweb.yml'") == 2
