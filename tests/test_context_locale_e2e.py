"""`new_context(locale=...)` is the context's language, not the launch one.

Measured on 0.25.11 with firefox-35: a session launched with ``locale="it-IT"``
opened ``browser.new_context(locale="de-DE")`` and every page of it reported
it-IT - ``navigator.language``, ``navigator.languages``, ``Intl`` and the
Accept-Language header alike. The option travelled to the engine as
``Browser.setLocaleOverride`` and landed on ``docShell.languageOverride``, which
nothing in Firefox reads, and on the realm that was current at that moment,
usually the initial ``about:blank``.

Two halves, one per side of the protocol:

* the ENGINE puts the context's language into the BrowsingContext's
  LanguageOverride field, the one Firefox reads for ``navigator.languages``,
  for the realm's default ``Intl`` locale and for the Accept-Language header.
  The launch locale already reaches pages through that field (seeded from
  ``juggler.locale.override``), so both arrive the same way;
* the SERVER sends the language LIST the core derives for the locale, not the
  bare tag. A German Firefox reports ``de-DE, de, en-US, en``; a context that
  sent ``"de-DE"`` would report one entry, and the default context - which
  receives the launch locale through the same defaults - would drop from the
  profile's four entries to one.

The header is compared by its tags only: firefox-35 sends the override list
raw, firefox-36 prepares it with q-values the way it prepares
``intl.accept_languages`` (``prfix/firefox-36-extra``).

Workers speak the context's language too. On firefox-35 a dedicated Worker
in the de-DE context still reported the launch locale, because its realm and
its ``navigator.languages`` were built from process-wide sources; from
firefox-36 a worker carries the language override of the document that made
it (a service worker, which has none, takes the one its context's pages
share). Retail 151 under its own ``emulation.setLocaleOverride`` leaves every
worker on the process language, so window and worker disagreeing there is a
gap of that override, not behaviour to copy.
"""
from __future__ import annotations

import http.server
import threading

import pytest

PAGE = b"<!doctype html><html><head><title>t</title></head><body>%s</body></html>"
OPENER = (b"<button id=pop onclick=\"window.open('/echo', '_blank')\" "
          b"style='width:200px;height:40px'>pop</button>")


# One script for the three kinds of worker: it answers with what its own
# realm reports, to a dedicated worker's `message`, a shared worker's port and
# a service worker's client.
WORKER = b"""
const read = () => ({languages: [...navigator.languages],
                     intl: Intl.DateTimeFormat().resolvedOptions().locale});
onmessage = (e) => (e.source || self).postMessage(read());
onconnect = (e) => e.ports[0].postMessage(read());
"""

WORKERS_JS = """async () => {
  const dedicated = await new Promise((ok) => {
    const w = new Worker('/w.js'); w.onmessage = (e) => ok(e.data); w.postMessage(0);
  });
  const shared = await new Promise((ok) => {
    const s = new SharedWorker('/w.js'); s.port.onmessage = (e) => ok(e.data);
  });
  const reg = await navigator.serviceWorker.register('/w.js');
  const sw = reg.active || reg.waiting || reg.installing;
  const service = await new Promise((ok) => {
    navigator.serviceWorker.onmessage = (e) => ok(e.data);
    const send = () => sw.postMessage(0);
    if (sw.state === 'activated') send();
    else sw.addEventListener('statechange', () => sw.state === 'activated' && send());
  });
  return {dedicated, shared, service};
}"""


def _serve():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/opener"):
                body = PAGE % OPENER
            elif self.path.startswith("/w.js"):
                self._send(WORKER, "text/javascript")
                return
            else:
                lang = self.headers.get("Accept-Language", "")
                body = PAGE % (b"<pre id=al>" + lang.encode() + b"</pre>")
            self._send(body, "text/html; charset=utf-8")

        def _send(self, body, kind):
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def _tags(header):
    return [t.split(";")[0].strip() for t in header.split(",") if t.strip()]


def _read(page):
    return {
        "header": _tags(page.inner_text("#al")),
        "language": page.evaluate("navigator.language"),
        "languages": page.evaluate("[...navigator.languages]"),
        "intl": page.evaluate("Intl.DateTimeFormat().resolvedOptions().locale"),
    }


def _expected(locale):
    from invisible_core import decide_session_locale

    langs = list(decide_session_locale(locale).languages)
    return {"header": langs, "language": langs[0], "languages": langs,
            "intl": locale}


@pytest.mark.e2e
def test_a_context_locale_reaches_navigator_intl_and_the_header(firefox_binary):
    from invisible_playwright import InvisiblePlaywright

    srv, base = _serve()
    try:
        with InvisiblePlaywright(seed=5150, binary_path=firefox_binary,
                                 headless=True, locale="it-IT",
                                 timezone="Europe/Rome") as browser:
            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(base + "/echo", wait_until="load", timeout=30_000)
            assert _read(page) == _expected("it-IT"), (
                "the default context must keep the launch locale's full list")
            ctx.close()

            ctx = browser.new_context(locale="de-DE")
            page = ctx.new_page()
            page.goto(base + "/echo", wait_until="load", timeout=30_000)
            assert _read(page) == _expected("de-DE"), (
                "new_context(locale='de-DE') inside an it-IT session")
            want = {"languages": _expected("de-DE")["languages"], "intl": "de-DE"}
            workers = page.evaluate(WORKERS_JS)
            for kind, seen in workers.items():
                assert seen == want, "a %s worker of a de-DE context: %r" % (kind, seen)

            # A page the site opens belongs to the same context and speaks
            # its language from its first request.
            page.goto(base + "/opener", wait_until="load", timeout=30_000)
            with ctx.expect_page(timeout=30_000) as info:
                page.click("#pop")
            popup = info.value
            popup.wait_for_load_state("load", timeout=30_000)
            assert _read(popup) == _expected("de-DE"), "the popup of a de-DE context"
            ctx.close()
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------------------------------------------------------------
# What a worker, a worklet, a canvas and the page's own realm say of the language and the zone of their context.
#
# Characterisation of firefox-68 (the hooks of fingerprint/locale-time/{workers,language,timezone} in the engine series cite these
# tests by name): each one is green on the published engine and has to stay green on the one that replaces its hooks. The language
# and the zone are read where a page reads them (a worker's own `navigator` and `Intl`, a worklet's processor, a canvas's text
# metrics, an inline script of the page). Where a context's value is compared with another value, the other is a control that can
# fail: the context left on the launch language, or a context with a language of its own that the change must not reach.
# ---------------------------------------------------------------------------------------------------------------------------------
import _locale_probe as probe  # noqa: E402

DE_LIST = ["de-DE", "de", "en-US", "en"]

#: One page for every tab the tests below open: the URL is what tells the tabs apart for the chrome scope, so each one carries a query.
SITE = {
    "/page": probe.PAGE,
    "/lang-worker.js": (probe.LANG_WORKER, "text/javascript"),
    "/measure-worker.js": (probe.MEASURE_WORKER, "text/javascript"),
}


def _wait_for(condition, page):
    """True once ``condition()`` holds, polled with the page's event loop as the pump; the deadline is the one of ``wait_until``."""
    from _waits import wait_until

    return wait_until(condition, 30, lambda: page.wait_for_timeout(100))


@pytest.mark.e2e
def test_a_worker_with_its_own_language_ignores_the_accept_languages_pref(firefox_binary, monkeypatch):
    """⛔ ``intl.accept_languages`` IS THE LANGUAGE OF A BROWSER THAT DECLARED NONE. When the pref changes Firefox tells every worker, and
    a worker that has no language of its own takes the new list. A worker whose context declares a language reads that list from its
    owner and has to ignore the broadcast (WorkerPrivate::UpdateLanguagesInternal): a site cannot see two languages move apart in
    one browser because a pref moved. The engine's own refusal is what is pinned here.

    One session with nothing declared at launch (``juggler.locale.override`` empty: no language falls back to the session) and two
    contexts: one asks for de-DE, the other for nothing (``locale=None``). The pref goes to "ja, en-US" from the chrome scope. The worker
    of the context with nothing declared takes it (the control: the broadcast reaches workers, so the other worker's silence is
    its own); the worker with de-DE keeps de-DE and its four entries. The two contexts are in different content processes, so the change of
    the other worker proves nothing about the process of the silent one: before the silent worker is read, the test waits on the
    CONDITION "the content process of each of the two tabs reports ``intl.accept_languages`` = "ja, en-US"" (a process script reads the pref
    in each process). A process that reports it has applied the change and queued the broadcast to its workers before it answered, so the
    answer of the worker with de-DE comes after the broadcast was handled in its own process.
    """
    import webfont_probe
    from invisible_playwright import InvisiblePlaywright

    monkeypatch.setenv("MOZ_REMOTE_ALLOW_SYSTEM_ACCESS", "1")   # the chrome-scope write below
    srv, base = probe.serve(SITE)
    marionette = None
    try:
        with InvisiblePlaywright(seed=5150, binary_path=firefox_binary, headless=True, locale="it-IT", timezone="Europe/Rome",
                                 extra_args=probe.MARIONETTE_ARGS,
                                 extra_prefs={"marionette.port": 0, "juggler.locale.override": ""}) as browser:
            own = browser.new_context(locale="de-DE").new_page()
            none = browser.new_context(locale=None).new_page()
            for name, page in (("own", own), ("none", none)):
                page.goto("%s/page?%s" % (base, name), wait_until="load", timeout=30_000)
                page.evaluate(probe.START_LANG_WORKER_JS)
            kept = {"languages": DE_LIST, "intl": "de-DE"}
            assert own.evaluate(probe.ASK_LANG_WORKER_JS) == kept, "the worker with its own language before the pref moves"
            before = none.evaluate(probe.ASK_LANG_WORKER_JS)["languages"]
            assert before != ["ja", "en-US"], "the control worker already speaks the list the pref is about to take: %r" % (before,)

            marionette = probe.open_chrome(webfont_probe.marionette_active_port())
            assert probe.set_pref(marionette, "intl.accept_languages", "ja, en-US") == "ja, en-US"

            # The barrier: the content process of EACH tab holds the new value (read in that process), so the worker with de-DE is read after its
            # own process handled the change.
            processes = []

            def every_process_has_it():
                processes.append(probe.content_process_prefs(marionette, "intl.accept_languages", ("own", "none")))
                return processes[-1].get("prefs") == {"own": "ja, en-US", "none": "ja, en-US"}

            assert _wait_for(every_process_has_it, none), (
                "a content process never reported the new intl.accept_languages (last answers: %r)" % (processes[-3:],))

            seen = []

            def follows():
                seen.append(none.evaluate(probe.ASK_LANG_WORKER_JS)["languages"])
                return seen[-1] == ["ja", "en-US"]

            assert _wait_for(follows, none), (
                "the worker with no language of its own never took the pref (it answered %r): the broadcast does not reach workers, "
                "so the silence of the other worker proves nothing" % (seen[-3:],))
            assert own.evaluate(probe.ASK_LANG_WORKER_JS) == kept, (
                "the worker with its own language followed intl.accept_languages instead of its context")
    finally:
        if marionette is not None:
            marionette.close()
        probe.stop(srv)


SW_ZONE = b"""
const read = () => ({timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                     january: new Date(Date.UTC(2024, 0, 15, 12)).getTimezoneOffset()});
onmessage = (e) => e.source.postMessage(read());
"""

SW_JS = """async () => {
  const reg = await navigator.serviceWorker.register('/sw-zone.js');
  const sw = reg.active || reg.waiting || reg.installing;
  return await new Promise((ok) => {
    navigator.serviceWorker.onmessage = (e) => ok(e.data);
    const send = () => sw.postMessage(0);
    if (sw.state === 'activated') send();
    else sw.addEventListener('statechange', () => sw.state === 'activated' && send());
  });
}"""


@pytest.mark.e2e
def test_a_service_worker_of_a_context_with_its_own_zone_has_it(firefox_binary):
    """⛔ A SERVICE WORKER IS BORN FAR FROM ITS PAGE. It has no window; the engine hands it the language and the zone of its context in
    the data it is started with (ServiceWorkerPrivate::RefreshRemoteWorkerData), and a worker that missed them told a site the
    SESSION's zone while the page in the same context told another: the one clock of a context, read twice. A context asks for its own
    zone with ``timezone_id``; the service worker it registers reads it back in ``Intl`` and in the offset of a date.

    An Europe/Rome session, a context with ``timezone_id="Asia/Tokyo"``: its service worker answers Asia/Tokyo and -540 (no daylight
    time, one answer all year). The control is the session's own context: its service worker answers Europe/Rome and -60 in January.
    """
    from invisible_playwright import InvisiblePlaywright

    srv, base = probe.serve(dict(SITE, **{"/sw-zone.js": (SW_ZONE, "text/javascript")}))
    try:
        with InvisiblePlaywright(seed=5150, binary_path=firefox_binary, headless=True, locale="it-IT",
                                 timezone="Europe/Rome") as browser:
            answers = {}
            for name, options in (("session", {}), ("tokyo", {"timezone_id": "Asia/Tokyo"})):
                page = browser.new_context(**options).new_page()
                page.goto("%s/page?%s" % (base, name), wait_until="load", timeout=30_000)
                answers[name] = page.evaluate(SW_JS)
            assert answers["tokyo"] == {"timeZone": "Asia/Tokyo", "january": -540}, "the service worker of the Asia/Tokyo context"
            assert answers["session"] == {"timeZone": "Europe/Rome", "january": -60}, (
                "the service worker of the context that asked for no zone (the session's)")
    finally:
        probe.stop(srv)


WORKLET = b"""
class Probe extends AudioWorkletProcessor {
  constructor() {
    super();
    const options = Intl.DateTimeFormat().resolvedOptions();
    this.port.postMessage({locale: options.locale, timeZone: options.timeZone,
                           january: new Date(Date.UTC(2024, 0, 15, 12)).getTimezoneOffset()});
  }
  process() { return false; }
}
registerProcessor('probe', Probe);
"""

# An OfflineAudioContext: a worklet needs no sound device, and the processor is built when its node is.
WORKLET_JS = """async () => {
  const ctx = new OfflineAudioContext(1, 128, 44100);
  await ctx.audioWorklet.addModule('/worklet.js');
  const node = new AudioWorkletNode(ctx, 'probe');
  const heard = new Promise((ok) => { node.port.onmessage = (e) => ok(e.data); });
  node.connect(ctx.destination);
  ctx.startRendering();
  return await heard;
}"""


@pytest.mark.e2e
def test_an_audio_worklet_of_a_context_speaks_its_language_and_zone(firefox_binary):
    """⛔ A WORKLET IS A GLOBAL OF ITS OWN AND ASKS ITS OWN QUESTIONS. An AudioWorkletProcessor runs on the audio thread in a realm that is
    not the page's and not a worker's (WorkletGlobalScope::CreateRealmOptions); a realm built from the process's sources told a site that
    reads ``Intl`` in a processor the host's language and zone. The engine reads the language and the zone from the window that added the
    module (WorkletLoadInfo) and gives them to the realm it builds.

    An it-IT, Europe/Rome session; a context with ``locale="de-DE"`` and ``timezone_id="Asia/Tokyo"``; the processor posts what its own
    ``Intl`` says. The context's worklet answers de-DE, Asia/Tokyo and -540 for a January date. The control is the context that asked
    for neither: its worklet answers it-IT, Europe/Rome and -60. The two answers differ in each field.
    """
    from invisible_playwright import InvisiblePlaywright

    srv, base = probe.serve(dict(SITE, **{"/worklet.js": (WORKLET, "text/javascript")}))
    try:
        with InvisiblePlaywright(seed=5150, binary_path=firefox_binary, headless=True, locale="it-IT",
                                 timezone="Europe/Rome") as browser:
            answers = {}
            for name, options in (("session", {}), ("own", {"locale": "de-DE", "timezone_id": "Asia/Tokyo"})):
                page = browser.new_context(**options).new_page()
                page.goto("%s/page?%s" % (base, name), wait_until="load", timeout=30_000)
                answers[name] = page.evaluate(WORKLET_JS)
            assert answers["own"] == {"locale": "de-DE", "timeZone": "Asia/Tokyo", "january": -540}, (
                "the worklet of the de-DE, Asia/Tokyo context")
            assert answers["session"] == {"locale": "it-IT", "timeZone": "Europe/Rome", "january": -60}, (
                "the worklet of the context that asked for neither (the session's)")
            assert all(answers["own"][key] != answers["session"][key] for key in answers["own"]), answers
    finally:
        probe.stop(srv)


def _canvas_widths(page):
    return page.evaluate(probe.MEASURE_PAGE_JS, ["ja", "en"])


@pytest.mark.e2e
def test_text_without_lang_takes_the_language_group_of_the_session_language(firefox_binary):
    """⛔ TEXT WITH NO ``lang`` TAKES THE LANGUAGE OF THE MACHINE, AND THE MACHINE IS THE SESSION. An OffscreenCanvas on the main thread has
    no element and no document: its font comes from nsLanguageAtomService::GetLocaleLanguage, which asked the host's regional settings
    and now asks the language the session declares. Two sessions launched with two languages measure the same strings differently, each
    as the canvas of its own language group.

    A ja-JP launch and an en-US launch (one browser at a time). In each, a main-thread OffscreenCanvas with no ``lang`` measures as the
    canvas element with the session's language and not as the one with the other language, and as a worker of the session's own context
    (another reader of the same language). The control is the pair of sessions: their OffscreenCanvases do not measure alike.
    """
    from invisible_playwright import InvisiblePlaywright

    srv, base = probe.serve(SITE)
    try:
        read = {}
        for name, locale, zone in (("ja", "ja-JP", "Asia/Tokyo"), ("en", "en-US", "America/New_York")):
            with InvisiblePlaywright(seed=5150, binary_path=firefox_binary, headless=True, locale=locale, timezone=zone) as browser:
                page = browser.new_context().new_page()
                page.goto("%s/page?%s" % (base, name), wait_until="load", timeout=30_000)
                read[name] = _canvas_widths(page)
        for name, other in (("ja", "en"), ("en", "ja")):
            got = read[name]
            assert got["element"]["ja"] != got["element"]["en"], (
                "the two language groups draw these strings alike here, so nothing can tell them apart: %r" % (got["element"],))
            assert got["offscreen"] == got["element"][name], (
                "the %s session's OffscreenCanvas measures %r, a canvas of the %s group %r" % (name, got["offscreen"], name, got["element"][name]))
            assert got["offscreen"] != got["element"][other], "the %s session's OffscreenCanvas measures as the %s group" % (name, other)
            assert got["offscreen"] == got["worker"], "the %s session: the main thread and a worker of the session's context disagree" % name
        assert read["ja"]["offscreen"] != read["en"]["offscreen"], "the two sessions' OffscreenCanvases measure alike"
    finally:
        probe.stop(srv)


INTL_JS = "const o = Intl.DateTimeFormat().resolvedOptions(); return {locale: o.locale, timeZone: o.timeZone};"


@pytest.mark.e2e
def test_intl_in_the_page_realm_follows_the_language_and_zone_of_its_context(firefox_binary):
    """The page's own global is built from the language and the zone of its context (xpc::InitGlobalObjectOptions, fed by
    nsGlobalWindowOuter::CreateNativeGlobalForInner), and that realm is the one a site's inline script runs in. It is read here the way a
    site reads it: an INLINE script writes ``Intl.DateTimeFormat().resolvedOptions()`` into the DOM (``page.evaluate`` is not necessarily
    that realm on this engine, so a read through it would say nothing about it).

    An it-IT, Europe/Rome launch: the default context's page realm says it-IT and Europe/Rome. A context with ``locale="de-DE"`` and
    ``timezone_id="Asia/Tokyo"``: de-DE and Asia/Tokyo. The control is the pair: the two contexts' answers differ in both fields.
    """
    from invisible_playwright import InvisiblePlaywright

    srv, base = probe.serve(SITE)
    try:
        with InvisiblePlaywright(seed=5150, binary_path=firefox_binary, headless=True, locale="it-IT",
                                 timezone="Europe/Rome") as browser:
            read = {}
            for name, options in (("session", {}), ("own", {"locale": "de-DE", "timezone_id": "Asia/Tokyo"})):
                page = browser.new_context(**options).new_page()
                page.goto("%s/page?%s" % (base, name), wait_until="load", timeout=30_000)
                read[name] = probe.read_in_page_realm(page, INTL_JS)
        assert read["session"] == {"locale": "it-IT", "timeZone": "Europe/Rome"}, "the page realm of the default context"
        assert read["own"] == {"locale": "de-DE", "timeZone": "Asia/Tokyo"}, "the page realm of the de-DE, Asia/Tokyo context"
        assert all(read["own"][key] != read["session"][key] for key in read["own"]), read
    finally:
        probe.stop(srv)
