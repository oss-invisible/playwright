"""Profile P4 of ci-release-18: a user journey over a small multi-page fake site, driven by the stock Playwright client.

One context, one page, 17 steps a person would make: click, hover, scroll, back, forward, a popstate filter, a
`target=_blank` tab, a download, a keyboard search, a sign-in with a wrong then the right password, sign out, "load more".
After every step the natural wait of the action runs and then `networkidle` is recorded as a boolean (never a failure).
Every page also runs a deterministic ad noise (tests/fakeweb/journey_site.py), on timers after `load`. The walk leaves a page
(a click or a key that navigates, back or forward, closing a tab, the end of the walk) from one point, `Journey.leave`, and only
once the page has declared its noise done (`window.__noiseDone`): a page left halfway sends a different set of requests in every
run, for stock and for us alike. The verdict is "same outcome as stock
Firefox, step by step": ok, url, title, marker, cookie names, history length, open pages, idle and the step's own values,
with stock Firefox run by the same client in the same invocation. Chromium is not part of the verdict. Time is never compared.

Two pages stay "not idle" on purpose (a request held until the walk moves on, one of them a keepalive fetch): the client
must count every request that stock counts. Which steps those are is declared in tests/fakeweb/pages.json.
"""
from __future__ import annotations

import os
import re
import tempfile
import contextlib
import time

from fakeweb import journey_site

BLOCKED = ("image", "media")
#: The guard against a HANG, not a measure of the speed of the machine: the deadline of every wait of the walk that has no time of its own
#: (the default of the context for actions and navigations, `expect_navigation`, hover, click, the wait for the noise, `wait_for_load_state`).
#: Such a wait exists only to find out that the browser has stopped; the walk's verdict never depends on how long a healthy browser takes.
#: Run 45176061220 (gate, Windows, network oracle): the runner was 3 to 4 times slower for about 100 s, and from the click to `load` the walk took
#: 15.7, 18.9 and 22.1 s in that launch (4.5 to 10.2 s on 480 pages in 6 runs), more than the 15 s this guard had: steps 2 and 3 of the
#: second round ended with "Timeout 15000ms exceeded", and a deadline meant for a hang became a leaf of the comparison (`steps/NN/ok`
#: and `error`). 120 s is far above anything a slow runner takes and far below the time the job has, so only a browser that has stopped reaches it.
#: The waits that are part of the behaviour keep their own times: the back and forward keys (3000 ms, `_press`), `idle_timeout_ms` of networkidle, the pauses of the scroll.
HANG_GUARD_MS = 120000
#: What a page of the site says once its noise is done (journey_site.DET_JS; at once on a page without noise).
NOISE_DONE = "window.__noiseDone === true"
FIELDS = ["ok", "url", "title", "marker", "cookies", "history_length", "pages", "idle"]
#: Seconds the walk waits for the requests a closed tab sends as it leaves: a safety net so that a request that never comes does not block it (the lab then sees it missing).
LEAVING_FAILSAFE_S = 15.0


def pagehide_requests():
    """How many requests fired from a `pagehide` handler (`/beacon?t=pagehide`) the site has logged so far."""
    with journey_site.STATE.lock:
        return sum(1 for e in journey_site.STATE.events if e["path"] == "/beacon" and e["id"] == "pagehide")


def wait_for_pagehide_requests(at_least, failsafe=LEAVING_FAILSAFE_S):
    """Returns when the site has logged `at_least` of them (the event the walk waits for), or after the failsafe. True when they came."""
    end = time.monotonic() + failsafe
    while pagehide_requests() < at_least and time.monotonic() < end:
        time.sleep(0.05)
    return pagehide_requests() >= at_least


def install_route(context):
    context.route("**/*", lambda route: route.abort() if route.request.resource_type in BLOCKED else route.continue_())


class Tracer:
    """The requests the client has heard of and not yet heard the end of (kept for the report; never compared). With
    `events` (the runner's --trace) it also keeps every request event the client reports, per step, for the request-type check.
    With `log` (a list; a test that saves evidence when it fails gives it) it keeps, apart from that, one entry per event the client
    hears (request, response, requestfinished, requestfailed, and the page's own framenavigated / domcontentloaded / load), each with
    the step the walk is in (`self.step`, set by the walk), its wall time `t` and `time.perf_counter()` as `m`; it only listens."""

    def __init__(self, context, events=False, log=None):
        self.open = {}
        self.events = [] if events else None
        self.t0 = time.monotonic()
        self.log = log
        self.step = 0
        self.detail = {}
        context.on("request", self._started)
        context.on("requestfinished", lambda r: self._ended(r, "finished"))
        context.on("requestfailed", lambda r: self._ended(r, "failed"))
        if log is not None:
            context.on("response", self._answered)
            context.on("page", self._page_opened)

    def _started(self, r):
        self.open[r] = "%s %s" % (r.resource_type, r.url.split("/", 3)[-1][:70])
        self._event(r, "request")
        entry = self._logged(r, "request")
        if entry is not None:
            self.detail[r] = {k: entry[k] for k in ("step", "m", "type", "method", "url", "frame")}

    def _ended(self, r, kind):
        self.open.pop(r, None)
        self.detail.pop(r, None)
        self._event(r, kind)
        more = {}
        if kind == "failed" and self.log is not None:
            try:
                more["failure"] = str(r.failure)[:200]
            except Exception:
                more["failure"] = None
        self._logged(r, "requestfinished" if kind == "finished" else "requestfailed", **more)

    def _event(self, r, kind):
        if self.events is not None:
            self.events.append({"ev": kind, "type": r.resource_type, "method": r.method,
                                "url": "/" + r.url.split("/", 3)[-1][:90], "t": round(time.monotonic() - self.t0, 3)})

    def _logged(self, r, kind, **more):
        """One entry of the log; a failure of the recording is never the client's (the walk goes on, the entry is missing)."""
        if self.log is None:
            return None
        try:
            try:
                frame = r.frame.url[:160]
            except Exception:
                frame = None
            entry = {"ev": kind, "step": self.step, "t": round(time.time(), 4), "m": round(time.perf_counter(), 6), "req": id(r),
                     "type": r.resource_type, "method": r.method, "url": r.url[:200], "frame": frame}
            entry.update(more)
            self.log.append(entry)
            return entry
        except Exception:
            return None

    def _answered(self, response):
        try:
            self._logged(response.request, "response", status=response.status)
        except Exception:
            pass

    def _page_opened(self, page):
        try:
            for name in ("framenavigated", "domcontentloaded", "load"):
                page.on(name, lambda arg, name=name: self._page_event(page, name, arg))
            self._page_event(page, "page", None)
        except Exception:
            pass

    def _page_event(self, page, name, arg):
        try:
            url = (arg.url if name == "framenavigated" else page.url)[:200]
        except Exception:
            url = None
        try:
            self.log.append({"ev": name, "step": self.step, "t": round(time.time(), 4), "m": round(time.perf_counter(), 6), "page": id(page), "url": url})
        except Exception:
            pass

    def snapshot(self):
        return sorted(self.open.values())[:12]

    def in_flight(self):
        """What the client counts as in flight now, with how long (`age_s`) and from which step (`step`); `log` only."""
        now = time.perf_counter()
        return [dict(d, age_s=round(now - d["m"], 3)) for d in sorted(self.detail.values(), key=lambda d: d["m"])]

    def take_events(self):
        taken, self.events = self.events, []
        return taken


class StageTimeout(Exception):
    """A wait of the walk reached the hang guard. Its first line (the one the verdict compares, `first_line`) names the wait and the page and
    the guard, and nothing that changes from run to run: two rounds that stop at the same place say the same words. The second line has
    the last request the site saw and when (seconds from the start of the wait, rounded): for the report, never compared."""


def is_playwright_timeout(exc):
    """A timeout of the Playwright client: its own class, or an error whose message says `Timeout Nms exceeded` (the fork's guard forbids importing the class)."""
    if type(exc).__name__ == "TimeoutError" and type(exc).__module__.startswith("playwright"):
        return True
    return re.search(r"\bTimeout \d+ms exceeded", str(exc)[:300]) is not None


def last_request_seen(began):
    """The last request the site logged, as `GET A/path, +2 s from the start of the wait`: the seconds from `began` (`time.time()` of the start of the wait), rounded; `none` before any."""
    state = journey_site.STATE
    if state is None:
        return "none"
    with state.lock:
        last = state.events[-1] if state.events else None
        t0 = state.t0
    if last is None:
        return "none"
    return "%s %s%s, %+d s from the start of the wait" % (last["method"], last["origin"], last["path"], round(t0 + last["t"] - began))


class Journey:
    #: The deadline of every wait without a time of its own (HANG_GUARD_MS); a test that makes a hang cheap gives a smaller one.
    hang_guard_ms = HANG_GUARD_MS

    def __init__(self, context, page, a, o, hang_guard_ms=HANG_GUARD_MS):
        self.ctx, self.page, self.a, self.o = context, page, a, o
        self.hang_guard_ms = hang_guard_ms
        self.extra = {}

    def norm(self, url):
        return (url or "").replace(self.a, "A").replace(self.o, "O")

    # helpers -----------------------------------------------------------------
    def stage(self, name, fn, page=None):
        """Runs `fn` (a wait of the walk) and, if the client times out, raises a StageTimeout that says which wait (`name`), on which page
        (`page`, default the walk's page; normalized with `norm`) and the last request the site saw, in place of the client's "Timeout Nms exceeded".
        Any other error, and the StageTimeout of a wait inside this one, goes through unchanged."""
        began = time.time()
        try:
            return fn()
        except Exception as exc:
            explained = self.explain(exc, name, page, began)
            if explained is exc:
                raise
            raise explained from exc

    def explain(self, exc, name, page, began):
        """`exc` itself, or the StageTimeout that replaces it when it is a timeout of the client (see `stage`)."""
        if isinstance(exc, StageTimeout) or not is_playwright_timeout(exc):
            return exc
        try:
            url = (page or self.page).url
        except Exception:
            url = None
        return StageTimeout("%s did not finish in %g s (hang guard) on %s\nlast request the site saw: %s"
                            % (name, self.hang_guard_ms / 1000, self.norm(url) or "a page that is gone", last_request_seen(began)))

    def leave(self, act, page=None):
        """The one point from which the walk leaves a page (`page`, default the walk's page): it waits until the page has declared its
        noise done (NOISE_DONE), then runs `act`, the click, the key, the back or forward, the close that leaves it."""
        pg = page or self.page
        self.stage("noise done", lambda: pg.wait_for_function(NOISE_DONE, timeout=self.hang_guard_ms), page=pg)
        return act()

    def navigate(self, act, what="click", **expect):
        """Leave the page with `act` (a click, a key: `what` names it in a timeout) and wait for the navigation it starts (`expect`: of expect_navigation)."""
        def go():
            with self.page.expect_navigation(**{"timeout": self.hang_guard_ms, **expect}):
                self.stage(what, act)
        self.leave(lambda: self.stage("navigation to load", go))

    def nav_click(self, selector, hover_first=False):
        loc = self.page.locator(selector).first      # hover and click wait for the context's default: the hang guard (`drive`)
        if hover_first:
            self.stage("hover", loc.hover)
        self.navigate(loc.click, what="click")

    def expect_path(self, pattern):
        path = re.sub(r"^[A-Z]", "", self.norm(self.page.url))
        if not re.search(pattern, path):
            raise AssertionError("expected path %s, got %s" % (pattern, self.norm(self.page.url)))

    def keep_mark(self):
        self.page.evaluate("window.__keep = 1")

    def keep_check(self):
        if self.page.evaluate("window.__keep") != 1:
            raise AssertionError("the page navigated but should not have")

    def history_step(self, direction, max_presses=4, required=True):
        """Press back or forward like a person; if the URL did not change, press again (up to 4 times)."""
        p = self.page
        before = p.url
        presses = 0
        for _ in range(max_presses):
            presses += 1
            self.leave(lambda: self._press(direction))
            if p.url != before:
                break
        self.extra["presses"] = presses
        if p.url == before:
            if required:
                raise AssertionError("%s did not change the URL after %d presses" % (direction, presses))
            return False
        self.stage("load state", lambda: p.wait_for_load_state("load", timeout=self.hang_guard_ms))
        return True

    def _press(self, direction):
        try:
            (self.page.go_back if direction == "back" else self.page.go_forward)(wait_until="commit", timeout=3000)
        except Exception:
            pass  # nothing committed in the main frame

    def type_in(self, selector, text):
        self.page.locator(selector).click()
        self.page.keyboard.type(text, delay=30)

    # steps: each returns (page_to_observe, cleanup or None) -----------------
    def s1_open_home(self):
        self.page.goto(self.a + "/")
        self.expect_path(r"^/")
        return self.page, None

    def s2_hover_click_teaser(self):
        self.nav_click("a.teaser >> nth=1", hover_first=True)
        self.expect_path(r"^/article/2")
        return self.page, None

    def s3_scroll_next(self):
        p = self.page
        p.mouse.move(640, 400)
        for _ in range(80):
            if p.evaluate("window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2"):
                break
            p.mouse.wheel(0, 600)
            p.wait_for_timeout(60)
        self.extra["scrolled"] = bool(p.evaluate("window.scrollY > 0"))
        self.nav_click("#next")
        self.expect_path(r"^/article/3")
        return p, None

    def s4_back(self):
        self.history_step("back")
        self.expect_path(r"^/article/2")
        return self.page, None

    def s5_back_home(self):
        self.history_step("back")
        self.expect_path(r"^/($|\?)")
        return self.page, None

    def s6_forward(self):
        if not self.history_step("forward", max_presses=2, required=False):
            self.extra["forward_available"] = False
            self.nav_click("a.teaser >> nth=1")
        self.expect_path(r"^/article/2")
        return self.page, None

    def s7_nav_category(self):
        self.nav_click("#nav-category")
        self.expect_path(r"^/category/world")
        return self.page, None

    def s8_filter_popstate(self):
        p = self.page
        self.keep_mark()
        p.click("#sort-new")
        p.wait_for_selector("#list[data-sort=new]")
        p.click("#sort-top")
        p.wait_for_selector("#list[data-sort=top]")
        self.extra["top_first"] = p.inner_text("#list a.item >> nth=0")
        p.go_back()
        p.wait_for_selector("#list[data-sort=new]")
        self.keep_check()
        self.extra["new_first"] = p.inner_text("#list a.item >> nth=0")
        if "sort=new" not in p.url:
            raise AssertionError("popstate did not restore the URL: " + self.norm(p.url))
        return p, None

    def s9_category_article(self):
        self.nav_click("#list a.item >> nth=0")
        self.expect_path(r"^/article/")
        return self.page, None

    def s10_blank_tab(self):
        with self.ctx.expect_page() as info:
            self.page.click("#partner")
        tab = info.value
        self.stage("load state", lambda: tab.wait_for_load_state("load", timeout=self.hang_guard_ms), page=tab)
        tab.wait_for_selector("#marker")
        self.extra["pages_when_open"] = len(self.ctx.pages)
        if "Partner" not in tab.inner_text("#marker"):
            raise AssertionError("unexpected marker in the new tab")

        noisy = "noise=0" not in tab.url

        def cleanup():
            before = pagehide_requests()
            self.leave(tab.close, page=tab)
            if noisy:
                # The closed tab's pagehide sends two requests (a beacon and a keep-alive POST) that reach the server after close() has returned. The walk goes on to the
                # next step when the server has both, so they are the requests of THIS step: with no wait they landed in the next step in most walks and in this one in
                # some (A/A run 44982522362, Windows seed 6). An event, not a delay.
                wait_for_pagehide_requests(before + 2)
            self.extra["pages_after_close"] = len(self.ctx.pages)

        return tab, cleanup

    def s11_download(self):
        with self.page.expect_download() as info:
            self.page.click("#download")
        dl = info.value
        fd, path = tempfile.mkstemp(suffix=".txt", prefix="journey_dl_")
        os.close(fd)
        try:
            dl.save_as(path)
            size = os.path.getsize(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        self.extra["download_name"] = dl.suggested_filename
        self.extra["download_size"] = size
        if size <= 0:
            raise AssertionError("empty download")
        return self.page, None

    def s12_search(self):
        p = self.page
        self.nav_click("#nav-search")
        self.expect_path(r"^/search")
        self.keep_mark()
        p.click("#search-submit")
        p.wait_for_selector("#msg")
        self.keep_check()
        self.type_in("#q", "quiet morning")
        self.navigate(lambda: p.keyboard.press("Enter"), what="key press")
        p.wait_for_selector("#qshow")
        self.extra["qshow"] = p.inner_text("#qshow")
        self.extra["results"] = p.locator("a.result").count()
        if self.extra["qshow"] != "quiet morning" or self.extra["results"] < 1:
            raise AssertionError("bad search results page")
        return p, None

    def s13_click_result(self):
        self.nav_click("a.result >> nth=0")
        self.expect_path(r"^/article/")
        return self.page, None

    def s14_signin(self):
        p = self.page
        self.nav_click("#nav-signin")
        self.expect_path(r"^/signin")
        self.type_in("#user", journey_site.USER)
        self.type_in("#pass", "not-the-password")
        self.navigate(lambda: p.keyboard.press("Enter"), what="key press")
        p.wait_for_selector("#error")
        self.extra["error"] = p.inner_text("#error")
        self.type_in("#pass", journey_site.PASSWORD)
        self.navigate(lambda: p.keyboard.press("Enter"), what="key press", url=re.compile(r"/account"))
        self.expect_path(r"^/account")
        self.extra["user"] = p.inner_text("#user")
        return p, None

    def s15_logo_account(self):
        self.nav_click("#logo")
        self.expect_path(r"^/($|\?)")
        self.nav_click("#nav-account")
        self.expect_path(r"^/account")
        self.extra["user"] = self.page.inner_text("#user")
        return self.page, None

    def s16_signout(self):
        self.nav_click("#signout")
        self.expect_path(r"^/($|\?)")
        self.nav_click("#nav-account")
        self.expect_path(r"^/signin")
        return self.page, None

    def s17_load_more(self):
        p = self.page
        self.nav_click("#logo")
        self.expect_path(r"^/($|\?)")
        before = p.locator("a.teaser").count()
        self.keep_mark()
        p.click("#loadmore")
        p.wait_for_function("document.querySelectorAll('a.teaser').length >= %d" % (before + 3))
        self.keep_check()
        self.extra["teasers"] = [before, p.locator("a.teaser").count()]
        return p, None


STEPS = [
    ("open home", "s1_open_home"),
    ("hover and click a teaser", "s2_hover_click_teaser"),
    ("scroll to the bottom, next article", "s3_scroll_next"),
    ("back", "s4_back"),
    ("back to home", "s5_back_home"),
    ("forward", "s6_forward"),
    ("nav Category", "s7_nav_category"),
    ("filter twice, back (popstate)", "s8_filter_popstate"),
    ("click an article from the category", "s9_category_article"),
    ("target=_blank link, new tab", "s10_blank_tab"),
    ("download summary", "s11_download"),
    ("search: empty, then keyboard + Enter", "s12_search"),
    ("click a result", "s13_click_result"),
    ("sign in: wrong then right password", "s14_signin"),
    ("logo, then Account (cookie kept)", "s15_logo_account"),
    ("sign out, then Account lands on sign in", "s16_signout"),
    ("load more on home", "s17_load_more"),
]


def first_line(exc):
    text = str(exc).strip()
    return (text.splitlines()[0] if text else repr(exc))[:300]


def snapshot(j, pg, rec):
    for key, expr in (
        ("title", "document.title"),
        ("marker", "(document.getElementById('marker') || {}).textContent || null"),
        ("cookies", "document.cookie.split(';').map((s) => s.trim().split('=')[0]).filter(Boolean).sort()"),
        ("history_length", "history.length"),
    ):
        try:
            rec[key] = pg.evaluate(expr)
        except Exception as exc:
            rec[key] = None
            rec.setdefault("snapshot_error", first_line(exc))
    rec["url"] = j.norm(pg.url)


def run_round(j, tracer, round_no, idle_timeout_ms, step_log=None):
    """`step_log`, a list (default None: nothing kept), receives each step's record as soon as it is done, with the moments of the step
    (`m_*`: time.perf_counter(), the scale of the client log and of the site's record) and what the client counted as in flight when
    `networkidle` returned or gave up: for a test that saves evidence when it fails. Never compared."""
    out = []
    walk = journey_site.STATE.begin_walk()      # the site tags what it sees with the walk, stock and ours both use steps 1..N
    for n, (name, method) in enumerate(STEPS, 1):
        j.extra = {}
        tracer.step = n
        m_before_set_step = time.perf_counter()
        journey_site.STATE.set_step(n)      # a held response of the site is released when the walk reaches its step
        rec = {"round": round_no, "n": n, "name": name, "ok": False, "error": None}
        if step_log is not None:
            rec["m_step_start"], rec["t_step_start"] = round(m_before_set_step, 6), round(time.time(), 4)
        t0 = time.monotonic()
        pg, cleanup = j.page, None
        began = time.time()
        try:
            pg, cleanup = getattr(j, method)()
            rec["ok"] = True
        except Exception as exc:
            # a wait that has no stage of its own (a selector, a download, a tab) is named by the client's call: `Page.wait_for_selector`
            exc = j.explain(exc, first_line(exc).split(": Timeout", 1)[0][:60], None, began)
            rec["error"] = first_line(exc)
            if isinstance(exc, StageTimeout):
                rec["error_detail"] = str(exc)
        if step_log is not None:
            rec["m_action_end"] = round(time.perf_counter(), 6)
        try:
            pg.wait_for_load_state("networkidle", timeout=idle_timeout_ms)
            rec["idle"] = True
        except Exception as exc:
            rec["idle"] = False
            if step_log is not None:
                rec["idle_error"] = first_line(exc)
        rec["open_at_idle_end"] = tracer.snapshot()      # for the report, not compared
        if step_log is not None:
            rec["m_idle_end"], rec["t_idle_end"] = round(time.perf_counter(), 6), round(time.time(), 4)
            rec["in_flight_at_idle_end"] = tracer.in_flight()
        if tracer.events is not None:
            rec["client_events"] = tracer.take_events()
        snapshot(j, pg, rec)
        rec["pages"] = len(j.ctx.pages)
        rec["extra"] = dict(j.extra)
        if cleanup:
            try:
                cleanup()
            except Exception as exc:
                rec["ok"] = False
                rec["error"] = rec["error"] or ("cleanup: " + first_line(exc))
        rec["seconds"] = round(time.monotonic() - t0, 2)
        out.append(rec)
        if step_log is not None:
            step_log.append(rec)
    # The walk ends: the context then closes its last page, which is left like every other one, once its noise is done.
    if out and out[-1]["ok"]:
        try:
            j.leave(lambda: None)
        except Exception as exc:
            out[-1]["ok"] = False
            out[-1]["error"] = "end of the walk: " + first_line(exc)
    if tracer.events is not None:
        # read once, at the end: a request fired on pagehide reaches the server after its step's own work is done
        time.sleep(0.5)
        for rec in out:
            rec["server_events"] = journey_site.STATE.events_of(walk, rec["n"])
    return out


def drive(browser, origin_a, origin_o, rounds=1, idle_timeout_ms=15000, trace=False, client_log=None, step_log=None, hang_guard_ms=HANG_GUARD_MS):
    """`rounds` walks, each in a fresh context and a fresh page. Returns the step records. `trace`: keep, per step, the request
    events the client reported and the hits the server saw (for the report; never compared). `client_log` and `step_log`, two lists
    (default None: nothing kept), are filled as the walk goes with what the client heard (`Tracer`) and with each step's record
    (`run_round`): for a test that saves evidence when it fails. `hang_guard_ms`: HANG_GUARD_MS; only a test of the guard gives another."""
    records = []
    for r in range(1, rounds + 1):
        context = browser.new_context()
        try:
            context.set_default_timeout(hang_guard_ms)
            context.set_default_navigation_timeout(hang_guard_ms)
            install_route(context)
            tracer = Tracer(context, events=trace, log=client_log)
            page = context.new_page()
            records += run_round(Journey(context, page, origin_a, origin_o, hang_guard_ms), tracer, r, idle_timeout_ms, step_log)
        finally:
            try:
                context.close()
            except Exception:
                pass
    return records


#: The engine's own log of where a mouse event goes, the same for stock Firefox and for ours (one file per process): `apz.inputqueue` and
#: `apz.child` say whether the event left the parent for the content process and how the content process took it, `MouseBoundaryEvents`
#: says whether the content process turned it into the page's mouse events. Run 45668588036 lost the first mousemove to a page whose
#: BrowsingContext had just been replaced (no ack in 5 s, the content process alive): with both legs logged, "differs from stock" is read
#: from the two logs side by side. A diagnostic no page reads.
INPUT_LOG = "timestamp,apz.inputqueue:4,apz.child:4,MouseBoundaryEvents:4"


@contextlib.contextmanager
def _input_log(moz_log_dir, leg):
    """While the browser of `leg` is launched: MOZ_LOG = INPUT_LOG into `<moz_log_dir>/<leg>/engine%PID` (Firefox writes one file per process);
    nothing when `moz_log_dir` is None. The environment is put back as it was."""
    if moz_log_dir is None:
        yield None
        return
    folder = os.path.join(str(moz_log_dir), leg)
    os.makedirs(folder, exist_ok=True)
    saved = {name: os.environ.get(name) for name in ("MOZ_LOG", "MOZ_LOG_FILE")}
    os.environ["MOZ_LOG"], os.environ["MOZ_LOG_FILE"] = INPUT_LOG, os.path.join(folder, "engine%PID")
    try:
        yield folder
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def run_on_candidate(sync_playwright, binary, origin_a, origin_o, rounds=1, moz_log_dir=None, **kw):
    """Our engine, served over the Playwright WebSocket and driven by the stock client. `moz_log_dir`: the input log of the engine goes to
    `<moz_log_dir>/candidate/` (INPUT_LOG)."""
    from invisible_playwright._juggler.remote import launch_server

    with _input_log(moz_log_dir, "candidate"):
        server = launch_server(headless=True, binary_path=binary, locale="en-US", timezone="Europe/Rome")
    try:
        with sync_playwright() as p:
            browser = p.firefox.connect(server.ws_endpoint)
            try:
                return drive(browser, origin_a, origin_o, rounds, **kw)
            finally:
                browser.close()
    finally:
        server.close()


def run_on_stock(sync_playwright, origin_a, origin_o, rounds=1, moz_log_dir=None, **kw):
    """Stock Firefox (the client's own build) driven by the stock client. `moz_log_dir`: its input log goes to `<moz_log_dir>/stock/` (INPUT_LOG)."""
    with sync_playwright() as p, _input_log(moz_log_dir, "stock") as folder:
        browser = p.firefox.launch(env=dict(os.environ)) if folder else p.firefox.launch()
        try:
            return drive(browser, origin_a, origin_o, rounds, **kw)
        finally:
            browser.close()


def _flat(rec):
    flat = {f: rec.get(f) for f in FIELDS}
    for k, v in (rec.get("extra") or {}).items():
        flat["extra." + k] = v
    return flat


def synthesize_stock(candidate, not_idle_steps):
    """Stock as pages.json declares it: every step ok, idle everywhere except the declared steps. Only these two fields
    are then compared (the other fields are copied from the candidate)."""
    return [dict(c, ok=True, idle=c["n"] not in not_idle_steps) for c in candidate]


def verdict(candidate, stock, not_idle_steps):
    """Returns (fails, broken). fails: (round, step, field, stock value, candidate value) where the candidate differs from
    stock Firefox. broken: where stock is not what pages.json declares (a step failed, or idle is not as declared)."""
    stock_by = {(s["round"], s["n"]): s for s in stock}
    fails, broken = [], []
    for c in candidate:
        s = stock_by.get((c["round"], c["n"]))
        if s is None:
            fails.append((c["round"], c["n"], "step", "missing in stock", None))
            continue
        cf, sf = _flat(c), _flat(s)
        for k in sorted(set(cf) | set(sf)):
            if cf.get(k) != sf.get(k):
                fails.append((c["round"], c["n"], k, sf.get(k), cf.get(k)))
    for s in stock:
        if not s["ok"]:
            broken.append((s["round"], s["n"], "ok", s["error"]))
        elif s["idle"] != (s["n"] not in not_idle_steps):
            broken.append((s["round"], s["n"], "idle", "idle=%s, declared %s" % (
                s["idle"], "not idle" if s["n"] in not_idle_steps else "idle")))
    return fails, broken
