"""The waits of the user journey (tests/fakeweb/profile_p4.py) that have no time of their own are a guard against a HANG, and a timeout says which wait it was.

Run 45176061220 (gate, Windows, network oracle): the runner was 3 to 4 times slower for about 100 s, the wait for `load` after a click took 15.7, 18.9 and 22.1 s (4.5
to 10.2 s on 480 pages in 6 runs), and steps 2 and 3 of the second round ended with "Timeout 15000ms exceeded", the 15 s of the context: a deadline meant for a hang
became a leaf of the comparison (`steps/NN/ok` and `error`). Here the objects of Playwright are fakes: what is checked is which deadline each wait is given and
what the error of a timeout says; the browser, with a page that really holds `load`, is in tests/test_fakeweb_p4_e2e.py.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from fakeweb import journey_site, profile_p4

A, O = "http://127.0.0.1:8001", "http://localhost:8002"


def timeout_error(api="Page.wait_for_selector", ms=120000):
    """What the Playwright client raises when a wait expires: `<API>: Timeout <N>ms exceeded.` and a call log."""
    return Exception("%s: Timeout %dms exceeded.\nCall log:\n  - waiting for locator(\"#x\")" % (api, ms))


class Page:
    """A page that records the deadline each wait is given, and can raise a timeout in one of them."""

    def __init__(self, url=A + "/article/2", fail=None):
        self.url, self.fail, self.calls = url, fail, []

    def _wait(self, name, *args, **kw):
        self.calls.append((name, args, kw))
        if self.fail == name:
            raise timeout_error()

    def wait_for_function(self, expression, **kw):
        self._wait("wait_for_function", expression, **kw)

    def wait_for_load_state(self, *args, **kw):
        self._wait("wait_for_load_state", *args, **kw)

    def expect_navigation(self, **kw):
        self.calls.append(("expect_navigation", (), kw))
        outer = self

        class Expected:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                if exc is None and outer.fail == "expect_navigation":
                    raise timeout_error("Page.expect_navigation")
                return False

        return Expected()


@pytest.fixture
def site(monkeypatch):
    state = journey_site.State()
    monkeypatch.setattr(journey_site, "STATE", state)
    return state


def journey(page, **kw):
    return profile_p4.Journey(SimpleNamespace(pages=[page]), page, A, O, **kw)


def test_the_guard_is_a_hang_guard_not_the_speed_of_the_machine():
    assert profile_p4.HANG_GUARD_MS == 120000
    assert not hasattr(profile_p4, "TIMEOUT_MS") and not hasattr(profile_p4, "NOISE_DONE_TIMEOUT_MS")
    assert journey(Page()).hang_guard_ms == 120000


def test_the_context_of_a_walk_waits_for_the_guard_for_actions_and_navigations(monkeypatch):
    contexts = []

    class Context:
        def __init__(self):
            self.timeouts = {}
            contexts.append(self)

        def set_default_timeout(self, ms):
            self.timeouts["action"] = ms

        def set_default_navigation_timeout(self, ms):
            self.timeouts["navigation"] = ms

        def route(self, *args):
            pass

        def new_page(self):
            return Page()

        def close(self):
            pass

    browser = SimpleNamespace(new_context=Context)
    monkeypatch.setattr(profile_p4, "Tracer", lambda *a, **k: None)
    monkeypatch.setattr(profile_p4, "run_round", lambda *a, **k: [])
    profile_p4.drive(browser, A, O)
    profile_p4.drive(browser, A, O, hang_guard_ms=3000)
    assert [c.timeouts for c in contexts] == [{"action": 120000, "navigation": 120000}, {"action": 3000, "navigation": 3000}]


def test_navigate_gives_expect_navigation_the_guard_and_keeps_what_the_step_asks():
    page = Page()
    j = journey(page)
    j.navigate(lambda: None)
    j.navigate(lambda: None, what="key press", url="/account")
    expected = [c for c in page.calls if c[0] == "expect_navigation"]
    assert [c[2] for c in expected] == [{"timeout": 120000}, {"timeout": 120000, "url": "/account"}]


def test_leave_gives_the_wait_for_the_noise_the_guard():
    page, tab = Page(), Page(url=O + "/partner/1")
    j = journey(page)
    ran = []
    j.leave(lambda: ran.append("page"))
    j.leave(lambda: ran.append("tab"), page=tab)
    assert page.calls == [("wait_for_function", (profile_p4.NOISE_DONE,), {"timeout": 120000})]
    assert tab.calls == [("wait_for_function", (profile_p4.NOISE_DONE,), {"timeout": 120000})]
    assert ran == ["page", "tab"]


def test_a_timeout_of_the_noise_names_the_wait_the_page_and_the_last_request(site):
    site.record(0, "GET", "/slowload.js", {"id": ["x"]})
    site.record(1, "POST", "/beacon", {"t": ["pagehide"]})
    j = journey(Page(url=A + "/article/2?noise=1", fail="wait_for_function"), hang_guard_ms=3000)
    with pytest.raises(profile_p4.StageTimeout) as caught:
        j.leave(lambda: None)
    first, second = str(caught.value).splitlines()
    assert first == "noise done did not finish in 3 s (hang guard) on A/article/2?noise=1"
    assert second == "last request the site saw: POST O/beacon, +0 s from the start of the wait"
    assert "Timeout" not in str(caught.value)
    assert profile_p4.first_line(caught.value) == first


def test_the_first_line_of_the_error_changes_with_neither_the_moment_nor_the_traffic(site):
    """The verdict compares `first_line`: two rounds that stop at the same wait on the same page say the same words."""
    lines = set()
    for path in ("/a", "/b", "/c"):
        site.record(0, "GET", path, {})
        j = journey(Page(url=A + "/article/2", fail="wait_for_load_state"))
        with pytest.raises(profile_p4.StageTimeout) as caught:
            j.stage("load state", lambda: j.page.wait_for_load_state("load"))
        lines.add(profile_p4.first_line(caught.value))
    assert lines == {"load state did not finish in 120 s (hang guard) on A/article/2"}


def test_a_timeout_of_the_navigation_to_load_names_that_wait_and_not_the_click(site):
    j = journey(Page(url=A + "/slowload?noise=0", fail="expect_navigation"))
    with pytest.raises(profile_p4.StageTimeout) as caught:
        j.navigate(lambda: None)
    assert profile_p4.first_line(caught.value) == "navigation to load did not finish in 120 s (hang guard) on A/slowload?noise=0"
    assert "last request the site saw: none" in str(caught.value)


def test_a_timeout_of_the_click_inside_the_navigation_is_a_click(site):
    j = journey(Page())

    def click():
        raise timeout_error("Locator.click")

    with pytest.raises(profile_p4.StageTimeout) as caught:
        j.navigate(click)
    assert profile_p4.first_line(caught.value).startswith("click did not finish in 120 s (hang guard) on ")
    with pytest.raises(profile_p4.StageTimeout) as caught:
        j.navigate(click, what="key press")
    assert profile_p4.first_line(caught.value).startswith("key press did not finish")


def test_an_error_that_is_not_a_timeout_goes_through_unchanged(site):
    j = journey(Page())
    problem = AssertionError("expected path /article/2, got A/")
    with pytest.raises(AssertionError) as caught:
        j.stage("click", lambda: (_ for _ in ()).throw(problem))
    assert caught.value is problem


def test_a_timeout_that_the_inner_wait_already_named_keeps_its_name(site):
    j = journey(Page())

    def inner():
        raise j.explain(timeout_error("Locator.hover"), "hover", None, 0)

    with pytest.raises(profile_p4.StageTimeout) as caught:
        j.stage("navigation to load", inner)
    assert profile_p4.first_line(caught.value).startswith("hover did not finish")


def test_a_wait_of_a_step_that_has_no_stage_is_named_by_the_call_of_the_client(site, monkeypatch):
    """A selector, a download, a tab: the record of the step says `Page.wait_for_selector did not finish ...`, with the whole text kept apart from the compared `error`."""
    page = Page()
    page.evaluate = lambda expr: None

    class Walk:
        """Steps that succeed, but the second, which waits on a selector the client gives up on."""
        explain, norm, hang_guard_ms, a, o = profile_p4.Journey.explain, profile_p4.Journey.norm, 120000, A, O

        def __init__(self):
            self.page, self.ctx, self.extra = page, SimpleNamespace(pages=[page]), {}

        def __getattr__(self, name):
            def step(*args):
                if name == "s2_hover_click_teaser":
                    raise timeout_error("Page.wait_for_selector")
                return page, None
            return step

    walk = Walk()
    monkeypatch.setattr(profile_p4, "STEPS", profile_p4.STEPS[:3])
    tracer = SimpleNamespace(step=0, events=None, snapshot=lambda: [], take_events=lambda: [])
    records = profile_p4.run_round(walk, tracer, 1, 1000)
    assert [r["ok"] for r in records] == [True, False, True]
    assert records[1]["error"] == "Page.wait_for_selector did not finish in 120 s (hang guard) on A/article/2"
    assert records[1]["error_detail"].splitlines()[1] == "last request the site saw: none"
    assert "error_detail" not in records[0]


# The page of the site that holds `load` (journey_site.page_slowload), the one the e2e tests of the guard use -----------------------------------------------
def _get(url):
    import urllib.request

    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode()


def test_the_site_holds_load_with_a_blocking_script_until_its_time_is_up_or_it_is_released():
    import threading

    a, _o, stop = journey_site.start((0, 0))
    try:
        assert 'href="/slowload?noise=0&amp;ms=20000&amp;id=x"' in _get(a + "/slowlink?noise=0&ms=20000&id=x")
        assert '<script src="/slowload.js?noise=0&amp;ms=20000&amp;id=x"></script>' in _get(a + "/slowload?noise=0&ms=20000&id=x")
        assert _get(a + "/slowload.js?ms=1&id=short") == "/* released */\n"       # its time is up
        body = []
        held = threading.Thread(target=lambda: body.append(_get(a + "/slowload.js?id=never")), daemon=True)
        held.start()
        journey_site.STATE.release("slow-never")                                  # a release that comes first is remembered, as /hold's is
        held.join(timeout=30)
        assert body == ["/* released */\n"]
    finally:
        stop()


def test_no_page_of_the_walk_links_to_the_pages_that_hold_load():
    a, _o, stop = journey_site.start((0, 0))
    try:
        for path in ("/", "/category/world", "/article/1", "/search", "/signin", "/account", "/partner/1"):
            assert "slowl" not in _get(a + path), path
    finally:
        stop()
