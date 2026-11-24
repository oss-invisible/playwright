"""The ab. leaves of the host gate (scripts/ci_host_gate.py): the poison probe of the page and the A/B oracle's extra visit of seeds 0 and 63.

No browser. The site of the visit is served by a real loopback server and asked with plain HTTP requests, the way the browser asks it; the page's
JavaScript is held by what it must contain and by the files it names. What the comparison does with the leaves is shown on synthetic dumps.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import ci_ab_oracle as oracle  # noqa: E402
import ci_host_gate as gate  # noqa: E402
import test_ci_host_gate as host_gate_tests  # noqa: E402

pytestmark = pytest.mark.unit

AB = ROOT / "scripts" / "host_gate" / "ab"


# -
# The prefix: read by the A/B, skipped by the retail comparison.

def test_the_ab_prefix_is_skipped_by_the_retail_comparison_like_diag(tmp_path):
    items = lambda leg, seed: {"surface.gpu.limits": 8, "ab.poison": {"timezone": leg}, "ab.launch": {"top": {"language": "ja-JP", "seed": seed}}}
    assert host_gate_tests.run(tmp_path, items, {"surface.gpu.limits": 8}) == []


def test_only_the_ab_prefix_is_skipped(tmp_path):
    items = lambda leg, seed: {"abc.value": leg}
    failures = host_gate_tests.run(tmp_path, items, {"abc.value": "win-x86_64"})
    assert any(f.startswith("host  abc.value") for f in failures)


def test_the_ab_oracle_compares_the_ab_leaves_which_the_diag_ones_are_left_out():
    assert gate.is_ab("ab.launch.top.language") and not gate.is_ab("diag.ab.launch")
    both = {"ab.launch": {"top": {"language": "ja-JP"}}, "diag.ab.launch": {"bfcache_persisted": True}}
    assert set(oracle.comparable(both)) == {"ab.launch.top.language"}
    published, candidate = {(1.0, 0): both}, {(1.0, 0): {"ab.launch": {"top": {"language": "en-US"}}}}
    report = oracle.compare_visits(published, candidate, [], min_leaves={1.0: 1}, expected=((1.0, 0),))
    assert report["verdict"] == "red" and report["differences"][0]["leaf"] == "ab.launch.top.language"


def test_the_oracle_asks_the_dump_for_the_ab_visit():
    source = (ROOT / "scripts" / "ci_ab_oracle.py").read_text(encoding="utf-8")
    assert '"--no-pin", "--ab"' in source


def test_the_dump_adds_the_ab_visit_to_the_seeds_that_have_one(monkeypatch):
    seen = []
    monkeypatch.setattr(gate, "dump", lambda *a, **k: seen.append(k) or 0)
    assert gate.main(["dump", "ff", "--leg", "linux-x86_64", "--out", "o", "--visit", "0:1.0", "--ab"]) == 0
    assert seen[0]["ab"] is True and seen[0]["visits"] == [(1.0, 0)]
    gate.main(["dump", "ff", "--leg", "linux-x86_64", "--out", "o"])
    assert seen[1]["ab"] is False


# -
# The launch of the visit.

def test_the_ab_visit_launches_in_japanese_and_pins_a_dpr_only_when_asked():
    options = gate.ab_options("ff", 0, None, "Asia/Bangkok")
    assert options["locale"] == "ja-JP" and options["seed"] == 0 and options["timezone"] == "Asia/Bangkok" and "pin" not in options
    assert gate.ab_options("ff", 63, 1.5, "Asia/Bangkok")["pin"] == {"screen.dpr": 1.5}


def test_the_second_context_of_the_visit_overrides_language_and_zone():
    assert dict(gate.AB_CONTEXTS)["launch"] == {}
    assert dict(gate.AB_CONTEXTS)["context"] == {"locale": "de-DE", "timezone_id": "America/Lima"}
    assert gate.AB_SEEDS == (0, 63)


def test_a_visit_keeps_what_the_back_navigation_read_and_puts_whether_it_was_cached_in_diag():
    items = gate.ab_items("launch", {"top": {"language": "ja-JP"}}, {"n": 2, "persisted": True, "data": {"language": "ja-JP"}})
    assert items["ab.launch"] == {"top": {"language": "ja-JP"}, "bfcache": {"language": "ja-JP"}}
    assert items["diag.ab.launch"] == {"bfcache_persisted": True}


# -
# The site.

@pytest.fixture()
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), gate._AbSite)
    server.ab_seen, server.ab_lock = {}, threading.Lock()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % server.server_address[1]
    server.shutdown()
    server.server_close()


def get(url, **headers):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as r:
            return r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read()


def test_every_file_of_the_visit_is_served_with_its_type(site):
    for path in sorted(AB.iterdir()):
        status, kind, body = get("%s/ab/%s?ctx=launch" % (site, path.name))
        assert status == 200 and body == path.read_bytes(), path.name
        assert kind.startswith("text/html" if path.suffix == ".html" else "text/javascript"), path.name


def test_what_is_not_a_file_of_the_visit_is_not_served(site):
    for url in ("/ab/", "/ab/missing.js", "/ab/..%2Fci_host_gate.py", "/ab/%2e%2e/ci_host_gate.py", "/ab/realm.js.map", "/ab/notes.txt", "/etc/passwd", "/ab"):
        assert get(site + url)[0] == 404, url


def test_the_server_says_which_accept_language_it_saw_for_each_kind_of_request(site):
    get(site + "/ab/surfaces.html?ctx=launch", **{"Accept-Language": "ja-JP,ja;q=0.9", "Sec-Fetch-Dest": "document"})
    get(site + "/ab/frame.html?ctx=launch&id=cross", **{"Accept-Language": "ja-JP,ja;q=0.9", "Sec-Fetch-Dest": "iframe"})
    get(site + "/ab/realm.js?ctx=launch", **{"Accept-Language": "ja-JP,ja;q=0.9", "Sec-Fetch-Dest": "script"})
    get(site + "/ab/probe?ctx=launch&k=fetch", **{"Accept-Language": "ja-JP", "Sec-Fetch-Dest": "empty"})
    get(site + "/ab/realm.js?ctx=context", **{"Accept-Language": "de-DE", "Sec-Fetch-Dest": "script"})
    get(site + "/ab/leave.html?ctx=launch")
    status, kind, body = get(site + "/ab/seen?ctx=launch")
    seen = json.loads(body)
    assert status == 200 and kind == "application/json"
    assert seen == {"document:/ab/surfaces.html": ["ja-JP,ja;q=0.9"], "iframe:/ab/frame.html": ["ja-JP,ja;q=0.9"], "script:/ab/realm.js": ["ja-JP,ja;q=0.9"],
                    "empty:/ab/probe": ["ja-JP"], "none:/ab/leave.html": ["<none>"]}
    assert json.loads(get(site + "/ab/seen?ctx=context")[2]) == {"script:/ab/realm.js": ["de-DE"]}
    assert json.loads(get(site + "/ab/seen?ctx=nobody")[2]) == {}


def test_asking_what_the_server_saw_is_not_itself_something_it_saw(site):
    get(site + "/ab/seen?ctx=launch", **{"Accept-Language": "x", "Sec-Fetch-Dest": "empty"})
    assert json.loads(get(site + "/ab/seen?ctx=launch")[2]) == {}


def test_two_values_of_one_kind_are_both_told(site):
    get(site + "/ab/realm.js?ctx=launch", **{"Accept-Language": "ja", "Sec-Fetch-Dest": "script"})
    get(site + "/ab/realm.js?ctx=launch", **{"Accept-Language": "en", "Sec-Fetch-Dest": "script"})
    assert json.loads(get(site + "/ab/seen?ctx=launch")[2]) == {"script:/ab/realm.js": ["en", "ja"]}


# -
# The page and the realms: what they must do, and that they name only files that exist.

def test_every_file_the_visit_names_exists():
    served = {p.name for p in AB.iterdir()}
    for path in AB.iterdir():
        text = path.read_text(encoding="utf-8")
        for name in re.findall(r"/ab/([A-Za-z]+\.(?:js|html))", text):
            assert name in served, "%s names /ab/%s" % (path.name, name)


def test_the_page_reads_every_kind_of_realm_and_window_and_the_required_steps_are_among_them():
    page = (AB / "surfaces.html").read_text(encoding="utf-8")
    steps = re.findall(r"step\(R, '([a-z_]+)'", page)
    assert steps == ["top", "iframe_cross", "iframe_srcdoc", "worker", "shared_worker", "service_worker", "audio_worklet",
                     "popup_plain", "popup_features", "requests", "accept_language"]
    assert set(gate.AB_REQUIRED_STEPS) <= set(steps)
    assert "'http://localhost:' + location.port" in page            # a site that is not the page's
    assert "f.srcdoc" in page and "window.open(url, name)" in page and "window.open(url, name, features)" in page
    assert "'pageshow'" in page and "__abBf" in page and "document.title = 'ab-done'" in page
    assert "new SharedWorker" in page and "serviceWorker.register" in page and "audioWorklet.addModule" in page


def test_the_driver_waits_for_what_the_page_sets():
    source = (ROOT / "scripts" / "ci_host_gate.py").read_text(encoding="utf-8")
    page = (AB / "surfaces.html").read_text(encoding="utf-8")
    for expression in ("document.title === 'ab-done'", "window.__abResult", "window.__abBf && window.__abBf.n >= 2", "window.__abBf"):
        assert expression in source, expression
    assert "window.__abResult =" in page and "window.__abBf = " in page


def test_the_realm_reads_the_subset_and_an_audio_worklet_reports_through_its_processor():
    realm = (AB / "realm.js").read_text(encoding="utf-8")
    for name in ("language", "languages", "hardwareConcurrency", "intl", "dates", "sampleRate", "toDataURL", "measureText", "convertToBlob",
                 "offscreen_webgl", "registerProcessor('ab-probe'"):
        assert name in realm, name
    assert "ab-probe" in (AB / "surfaces.html").read_text(encoding="utf-8")


def test_a_controlled_navigation_is_answered_by_the_worker_not_by_the_server():
    worker = (AB / "sw.js").read_text(encoding="utf-8")
    assert "e.respondWith" in worker and "/ab/controlled.html" in worker and "accept-language" in worker
    assert "the worker did not answer the navigation" in (AB / "surfaces.html").read_text(encoding="utf-8")
    assert "not controlled" in (AB / "controlled.html").read_text(encoding="utf-8")


# -
# The control: another identity must read other values.

def visit(**steps):
    return {"ab.launch": dict({"top": {"hardwareConcurrency": 16, "language": "ja-JP"}}, **steps),
            "ab.context": {"top": {"hardwareConcurrency": 16, "language": "de-DE"}}}


def test_the_control_counts_the_leaves_seed_63_reads_differently_from_seed_0():
    other = {"ab.launch": {"top": {"hardwareConcurrency": 8, "language": "ja-JP"}}, "ab.context": {"top": {"hardwareConcurrency": 8, "language": "de-DE"}}}
    result = gate.ab_control({0: visit(), 63: other}, minimum=2)
    assert result["status"] == "ok" and result["differing"] == 2 and result["compared"] == 4
    assert result["line"].startswith("ab control: seed 63 differs from seed 0 on 2 of 4 ab. leaves (at least 2 are required)")


def test_two_identities_that_read_the_same_fail_the_control():
    result = gate.ab_control({0: visit(), 63: visit()}, minimum=1)
    assert result["status"] == "too_few" and result["differing"] == 0
    assert "an identity that is not seed 0's read the same" in result["line"]


def test_the_control_counts_neither_the_poison_probe_nor_diag():
    a = dict(visit(), **{"ab.poison": {"offsets": [1]}, "diag.ab.launch": {"bfcache_persisted": True}})
    b = dict(visit(), **{"ab.poison": {"offsets": [2]}, "diag.ab.launch": {"bfcache_persisted": False}})
    assert gate.ab_control({0: a, 63: b}, minimum=1)["differing"] == 0


def test_a_run_that_did_not_visit_both_seeds_says_so():
    result = gate.ab_control({0: visit()})
    assert result["status"] == "not_visited" and "seed 63 has no ab. leaves" in result["line"]
    assert gate.ab_control({0: {"surface.x": 1}, 63: visit()})["status"] == "not_visited"


def test_the_minimum_is_written_and_provisional_until_a_run_measures_it():
    assert isinstance(gate.AB_CONTROL_MIN, int) and gate.AB_CONTROL_MIN >= 1
    source = (ROOT / "scripts" / "ci_host_gate.py").read_text(encoding="utf-8")
    assert "PROVISIONAL" in source


# -
# What a run must have read for its A/B to mean anything.

def full(ctx_steps=None):
    steps = {s: {"language": "x"} for s in gate.AB_REQUIRED_STEPS}
    steps.update(ctx_steps or {})
    return {"ab." + name: dict(steps) for name, _ in gate.AB_CONTEXTS}


def test_a_visit_that_read_every_required_step_in_every_context_is_whole():
    assert gate.ab_coverage({0: full(), 63: full(), 2: {"surface.x": 1}}) == []


def test_a_seed_of_the_ab_visit_without_its_leaves_is_a_problem_and_a_seed_not_visited_is_not():
    assert gate.ab_coverage({2: {"surface.x": 1}, 4: {}}) == []
    problems = gate.ab_coverage({0: {"surface.x": 1}})
    assert len(problems) == len(gate.AB_CONTEXTS) and all("seed 0: no ab." in p for p in problems)


@pytest.mark.parametrize("step", gate.AB_REQUIRED_STEPS)
def test_a_required_step_that_timed_out_is_a_problem(step):
    problems = gate.ab_coverage({63: full({step: "timeout: " + step})})
    assert len(problems) == len(gate.AB_CONTEXTS) and all(step in p and "timeout" in p for p in problems)


def test_the_steps_that_a_host_may_not_have_are_not_required():
    assert gate.ab_coverage({0: full({"service_worker": "absent", "audio_worklet": "timeout: audio_worklet", "popup_plain": "blocked"})}) == []


# -
# The visit and the dump, with a fake browser: the order of what is asked, and where the leaves go.

class _Page:
    def __init__(self, log):
        self.log = log
        self.mouse = self

    def goto(self, url, timeout=None):
        self.log.append(("goto", url.split("/ab/")[-1] if "/ab/" in url else url, timeout))

    def go_back(self, timeout=None):
        self.log.append(("back", timeout))

    def wait_for_function(self, expression, timeout=None):
        self.log.append(("wait", expression, timeout))

    def evaluate(self, expression):
        self.log.append(("evaluate", expression))
        if expression == "window.__abResult":
            return {"top": {"language": "ja-JP"}}
        if expression == "window.__abBf":
            return {"n": 2, "persisted": False, "data": {"language": "ja-JP"}}
        if expression == "window.__hostGate":
            return {"format": gate.FORMAT, "items": {"surface.x": 1}}
        raise RuntimeError("not asked by this fake")

    def move(self, *a):
        pass

    def click(self, *a):
        pass


class _Context:
    def __init__(self, log, options):
        self.log, self.options = log, options

    def new_page(self):
        return _Page(self.log)

    def close(self):
        self.log.append(("context closed", self.options))


class _Launch:
    def __init__(self, log, options):
        self.log, self.options = log, options

    def __enter__(self):
        self.log.append(("launch", self.options))
        return self

    def __exit__(self, *exc):
        self.log.append(("quit",))

    def new_context(self, **options):
        self.log.append(("context", options))
        return _Context(self.log, options)


@pytest.fixture()
def fake_browser(monkeypatch):
    import types

    log = []
    module = types.ModuleType("invisible_playwright")
    module.InvisiblePlaywright = lambda **options: _Launch(log, options)
    monkeypatch.setitem(sys.modules, "invisible_playwright", module)
    return log


def test_the_ab_visit_reads_each_context_then_goes_away_and_back_and_closes_it(fake_browser):
    items = gate.ab_visit("ff", 0, "Asia/Bangkok", 1.5, timeout_ms=1234)
    assert set(items) == {"ab.launch", "diag.ab.launch", "ab.context", "diag.ab.context"}
    assert items["ab.launch"]["top"] == {"language": "ja-JP"} and items["ab.launch"]["bfcache"] == {"language": "ja-JP"}
    kinds = [e[0] for e in fake_browser]
    assert kinds[0] == "launch" and kinds[-1] == "quit" and kinds.count("context") == 2 and kinds.count("context closed") == 2
    assert fake_browser[0][1] == gate.ab_options("ff", 0, 1.5, "Asia/Bangkok")
    contexts = [e[1] for e in fake_browser if e[0] == "context"]
    assert contexts == [{}, {"locale": "de-DE", "timezone_id": "America/Lima"}]
    for name in ("launch", "context"):
        steps = [e for e in fake_browser if e[0] in ("goto", "back", "wait") and (e[0] != "goto" or ("ctx=" + name) in e[1])]
        assert [e[1] for e in steps if e[0] == "goto"] == ["surfaces.html?ctx=" + name, "leave.html?ctx=" + name]
    assert all(e[-1] == 1234 for e in fake_browser if e[0] in ("goto", "wait", "back"))
    waits = [e[1] for e in fake_browser if e[0] == "wait"]
    assert waits == ["document.title === 'ab-done'", "window.__abBf && window.__abBf.n >= 2"] * 2


def test_the_dump_adds_the_ab_leaves_to_seeds_0_and_63_only_and_only_when_asked(fake_browser, monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "ab_visit", lambda firefox, seed, zone, dpr=None: {"ab.launch": {"seed": seed}})
    for ab, expected in ((True, {0: True, 2: False, 63: True}), (False, {0: False, 2: False, 63: False})):
        out = tmp_path / str(ab)
        assert gate.dump("ff", "linux-x86_64", out, gate.REFERENCE, visits=[(1.0, 0), (1.0, 2), (1.0, 63)], ab=ab) == 0
        for seed, has in expected.items():
            items = json.loads((out / ("linux-x86_64_dpr1.0_seed%d.json" % seed)).read_text(encoding="utf-8"))["result"]["items"]
            assert ("ab.launch" in items) is has, (ab, seed)
            if has:
                assert items["ab.launch"] == {"seed": seed}
