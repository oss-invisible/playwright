"""The e2e tests do not wait on clocks, share ports, write the environment or reach the world: an AST check with a reviewed list (no browser).

An e2e test is red for no fault of the product when its verdict depends on the speed of the machine (a fixed pause and then a read, an absolute bound on a duration), on a
resource the workers share (a literal port, ``os.environ``), or on the world (a real site, a service of the internet). The audit of the e2e tests found 342 such places, and
each was found after a red run. This reads the e2e files and the helpers of ``tests/`` with ``ast`` and refuses a new one (``tests/_e2e_clocks.py``: the rules and what each
accepts by its shape), the way ``test_the_consecutive_port_tests_run_serially.py`` refuses a test that asserts consecutive ports and does not carry the marker.

Two lists in ``tests/gates/e2e_clock_exceptions.json``, each item named by ``(file, scope, rule, nth)`` and never by a line:

* ``entries``: occurrences a person reviewed and found deliberate (a window in which nothing must happen, a slow server on purpose, a port nobody binds), with a ``kind`` of a closed
  vocabulary and a ``reason`` that is a sentence;
* ``debt``: every other occurrence, the dangerous ones not yet corrected. It is MADE from the sources (``python scripts/e2e_clock_exceptions.py --write``), never edited, so
  that a branch that corrects tests only has to run it when it merges. It may not grow: the occurrences it names must all be in the ``debt`` of main.

A stale item of either list (it matches no occurrence any more) is red: the list shortens with the code. Known-bad inputs: a fake file with every violation is red, a wait on a
condition with a deadline is green, a list that names an occurrence the sources no longer have is red, a debt that holds an occurrence main does not is red.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import _e2e_clocks as clocks

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
GATE = "tests/gates/e2e_clock_exceptions.json"


def rules_of(source: str, file: str = "test_fake_e2e.py"):
    return sorted({v.rule for v in clocks.analyze(source, file)})


def fake(body: str) -> str:
    return "import pytest\n\npytestmark = pytest.mark.e2e\n\n" + body


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  The check on the real tree
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def test_no_occurrence_is_outside_the_reviewed_lists():
    found = clocks.scan()
    difference = clocks.compare(found, clocks.load())
    lines = {v.key: v for v in found}
    new = ["%s:%d  %s  [%s #%d]  %s" % (v.file, v.line, v.scope, v.rule, v.nth, v.text) for v in (lines[k] for k in difference["new"])]
    assert not new, (
        "an e2e test (or a helper) depends on a clock, a shared resource or the world, and no list names it:\n  " + "\n  ".join(new) +
        "\nWait for the condition with a deadline (tests/_waits.py), bind port 0, use monkeypatch.setenv, serve the page from the test's own server. A deliberate case is "
        "reviewed into `entries` of " + GATE + " with a kind and a reason; `debt` is made by scripts/e2e_clock_exceptions.py --write and may not grow against main.")


def test_a_stale_item_of_either_list_is_refused():
    difference = clocks.compare(clocks.scan(), clocks.load())
    assert difference["stale_entries"] == [], (
        "these `entries` match no occurrence any more (the code changed): drop them with scripts/e2e_clock_exceptions.py --write --prune: %s" % difference["stale_entries"])
    assert difference["stale_debt"] == [], (
        "this `debt` is of a correction that has merged: make it again with scripts/e2e_clock_exceptions.py --write: %s" % difference["stale_debt"])
    assert difference["both"] == [], "an occurrence is both reviewed and debt: %s" % difference["both"]


def test_the_debt_on_disk_is_what_the_script_writes():
    document = clocks.load()
    assert (ROOT / GATE).read_text(encoding="utf-8") == clocks.dump(clocks.regenerate(document, clocks.scan())), (
        "the debt is not what scripts/e2e_clock_exceptions.py --write makes of the sources")


def test_an_entry_is_a_reviewed_sentence_of_a_closed_kind():
    document = clocks.load()
    reasons = {}
    for item in document["entries"]:
        label = "%s %s %s #%s" % (item["file"], item["scope"], item["rule"], item["nth"])
        assert item["kind"] in clocks.KINDS, "%s: kind %r is not one of %s" % (label, item["kind"], clocks.KINDS)
        assert item["rule"] in clocks.RULES, label
        assert isinstance(item["nth"], int) and item["nth"] >= 1, label
        reason = item["reason"]
        assert len(reason) >= 40 and reason.rstrip().endswith("."), "%s: the reason is not a sentence: %r" % (label, reason)
        reasons.setdefault(reason, set()).add(item["rule"])
    shared = [r for r, rules in reasons.items() if len(rules) > 1]
    assert shared == [], "one reason written for entries of different rules says nothing of either: %s" % shared
    for item in document["debt"]:
        assert item["rule"] in clocks.RULES and item["class"] == clocks.CLASS[item["rule"]], item


def _main_document():
    """The list of main, or None when it has none; the reason of the miss otherwise."""
    for ref in ("origin/main", "main"):
        shown = subprocess.run(["git", "show", "%s:%s" % (ref, GATE)], cwd=ROOT, capture_output=True, text=True)
        if shown.returncode == 0:
            return json.loads(shown.stdout), ref
        if subprocess.run(["git", "cat-file", "-e", ref], cwd=ROOT, capture_output=True).returncode == 0:
            return None, ref        # the ref exists and the file does not: main has no list yet (the merge that brings this one)
    return "no ref", None


def test_the_debt_does_not_grow_against_main():
    """THE HONEST LIMIT OF A UNIT TEST: it can be changed together with the list. Reading the list from MAIN is what stops that, and main is only there when the
    checkout has it: the pull-request job fetches it (tests.yml); a developer without it skips, and a CI run without it is red (a comparison that compares nothing is
    not a green)."""
    document, ref = _main_document()
    if document == "no ref":
        if os.environ.get("GITHUB_ACTIONS"):
            pytest.fail("origin/main is not in this checkout, so the debt cannot be compared with it: fetch it (git fetch origin main) before the tests")
        pytest.skip("no origin/main in this checkout: the debt is not compared with main (CI does)")
    if document is None:
        return      # main has no list: this is the change that brings it, there is nothing to grow from
    main_debt = {clocks.key_of(i) for i in document["debt"]}
    main_entries = {clocks.key_of(i) for i in document["entries"]}
    grown = sorted(k for k in (clocks.key_of(i) for i in clocks.load()["debt"]) if k not in main_debt and k not in main_entries)
    assert grown == [], (
        "the debt has occurrences that %s does not have: a test that was clean on main now waits on a clock, shares a port or reaches the world. Correct it; "
        "the debt only shrinks. %s" % (ref, grown))


def test_the_conftest_declares_the_exit_of_the_lab():
    assert clocks.analyze_conftest((ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")) == []


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  Known-bad inputs: each rule refuses its own case
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def test_a_sleep_then_an_assert_is_refused():
    source = fake("import time\n\n@pytest.mark.e2e\ndef test_x(page):\n    page.click('#go')\n    time.sleep(0.5)\n    assert page.title() == 'done'\n")
    assert rules_of(source) == ["sleep"]
    found = clocks.analyze(source, "test_fake_e2e.py")[0]
    assert (found.scope, found.nth, found.line) == ("test_x", 1, 10)


def test_a_wait_for_timeout_then_a_read_is_refused():
    source = fake("def test_x(page):\n    page.wait_for_timeout(300)\n    assert page.inner_text('#a') == 'x'\n    page.wait_for_timeout(300)\n")
    assert rules_of(source) == ["sleep"]
    assert [v.nth for v in clocks.analyze(source, "test_fake_e2e.py")] == [1, 2], "the nth occurrence is how a list tells two pauses of one test apart"


def test_an_asyncio_sleep_is_refused_and_a_yield_is_not():
    assert rules_of(fake("import asyncio\n\nasync def test_x():\n    await asyncio.sleep(1.0)\n")) == ["sleep"]
    assert rules_of(fake("import asyncio\n\nasync def test_x():\n    await asyncio.sleep(0)\n")) == []


def test_a_timer_in_a_page_of_the_test_is_refused():
    assert rules_of(fake("PAGE = '<script>setTimeout(() => { go(); }, 300)</script>'\n")) == ["js-timer"]
    assert rules_of(fake("def test_x(page):\n    page.evaluate('setInterval(tick, 10)')\n")) == ["js-timer"]


def test_a_literal_port_is_refused():
    assert rules_of(fake("import socket\n\ndef helper():\n    s = socket.socket()\n    s.bind(('127.0.0.1', 8080))\n")) == ["port"]
    assert rules_of(fake("from http.server import HTTPServer\n\ndef helper(h):\n    return HTTPServer(('127.0.0.1', 18080), h)\n")) == ["port"]
    assert rules_of(fake("MARIONETTE_PORT = 28431\n")) == ["port"]
    assert rules_of(fake("def prefs():\n    return {'marionette.port': 28431}\n")) == ["port"]
    assert rules_of(fake("def launch(f):\n    return f(port=9222)\n")) == ["port"]
    assert rules_of(fake("import socket\n\ndef helper():\n    s = socket.socket()\n    s.bind(('127.0.0.1', 0))\n    return f(port=0)\n")) == [], "port 0 is the answer"


def test_an_environ_write_is_refused_and_monkeypatch_is_not():
    for body in ("import os\n\ndef test_x():\n    os.environ['A'] = '1'\n", "import os\n\ndef test_x():\n    os.environ.pop('A', None)\n",
                 "import os\n\ndef test_x():\n    del os.environ['A']\n", "import os\n\ndef test_x():\n    os.environ.update({'A': '1'})\n",
                 "import os\n\ndef test_x():\n    os.putenv('A', '1')\n"):
        assert rules_of(fake(body)) == ["env"], body
    assert rules_of(fake("import os\n\ndef test_x(monkeypatch):\n    monkeypatch.setenv('A', '1')\n    assert os.environ.get('A') == '1'\n")) == []


def test_an_absolute_upper_bound_is_refused_and_a_lower_bound_is_not():
    assert rules_of(fake("def test_x(took):\n    assert took < 4.0\n")) == ["bound"]
    assert rules_of(fake("import time\n\ndef test_x(t0):\n    assert time.monotonic() - t0 <= 10\n")) == ["bound"]
    assert rules_of(fake("LIMIT_S = 5\n\ndef test_x(elapsed):\n    assert elapsed < LIMIT_S\n")) == ["bound"]
    assert rules_of(fake("def test_x(took):\n    assert 4.0 > took\n")) == ["bound"]
    assert rules_of(fake("def test_x(took):\n    assert 24.0 < took < 40.0\n")) == ["bound"], "the upper half of a band is an upper bound"
    assert rules_of(fake("def test_x(took):\n    assert took >= 0.25\n")) == [], "a lower bound cannot fail because the machine is slow"
    assert rules_of(fake("def test_x(took, control):\n    assert took <= control * 3\n")) == [], "a bound against a measure of the same run is relative"
    assert rules_of(fake("def test_x(count):\n    assert count < 4\n")) == [], "not a duration"


def test_a_real_url_is_refused_and_a_local_one_is_not():
    assert rules_of(fake("def test_x(page):\n    page.goto('https://example.com/')\n")) == ["net"]
    assert rules_of(fake("import urllib.request\n\ndef test_x():\n    urllib.request.urlopen('http://127.0.0.1:1/')\n")) == ["net"]
    assert rules_of(fake("import requests\n\ndef test_x():\n    requests.get(URL)\n")) == ["net"]
    assert rules_of(fake("import subprocess\n\ndef test_x():\n    subprocess.run(['pip', 'install', 'x'])\n")) == ["net"]
    for url in ("http://127.0.0.1:%d/", "http://localhost:3000/", "http://a.test/x", "http://x/", "http://203.0.113.80/", "http://[::1]:1/", "http://%s/", "http://{host}/"):
        assert rules_of(fake("def test_x(page):\n    page.goto(%r)\n" % url)) == [], url


def test_a_real_url_is_accepted_in_the_files_of_the_round_after_a_publication():
    source = fake("def test_x(page):\n    page.goto('https://example.com/')\n")
    assert rules_of(source, "test_release_e2e.py") == [] and rules_of(source, "test_upgrade_e2e.py") == []


def test_a_count_of_processes_is_refused_outside_the_helpers_that_count():
    source = fake("def test_x(pids):\n    assert len(pids) == 1\n")
    assert rules_of(source) == ["count"]
    assert clocks.analyze(source, "_processes.py") == []


def test_a_polling_step_with_a_deadline_is_accepted():
    for body in (
        "import time\n\ndef test_x(ready):\n    deadline = time.monotonic() + 30\n    while not ready() and time.monotonic() < deadline:\n        time.sleep(0.05)\n",
        "import time\n\ndef test_x(ready):\n    end = time.time() + 30\n    while True:\n        if ready():\n            break\n        assert time.time() < end, 'never'\n        time.sleep(0.05)\n",
        "import time\n\ndef test_x(ready):\n    for _ in range(100):\n        if ready():\n            break\n        time.sleep(0.1)\n",
        "def test_x(page, ready):\n    wait_until(ready, 30, lambda: page.wait_for_timeout(50))\n",
        "def test_x(page, ready):\n    watch(ready, bool, 30, pump=lambda: page.wait_for_timeout(50))\n",
        "def pump(page):\n    page.wait_for_timeout(50)\n",
        "def _wait_for_the_frame(page):\n    page.wait_for_timeout(50)\n",
        "import time\n\nclass SlowHandler(BaseHTTPRequestHandler):\n    def do_GET(self):\n        time.sleep(1.0)\n",
    ):
        assert rules_of(fake(body)) == [], body


def test_a_wait_without_a_deadline_or_a_condition_is_not_a_polling_step():
    assert rules_of(fake("import time\n\ndef test_x(ready):\n    while not ready():\n        time.sleep(0.05)\n")) == ["sleep"]
    assert rules_of(fake("import time\n\ndef test_x(ready):\n    for _ in range(3):\n        time.sleep(0.05)\n")) == ["sleep"]
    assert rules_of(fake("def test_wait_for_the_title(page):\n    page.wait_for_timeout(500)\n")) == ["sleep"], "a test is not a wait helper because of its name"


def test_a_unit_test_and_a_fake_clock_are_not_read():
    assert rules_of("import pytest\nimport time\n\n@pytest.mark.unit\ndef test_x():\n    time.sleep(0.1)\n", "test_unit_fake.py") == []
    assert rules_of("import time\n\ndef test_x():\n    time.sleep(0.1)\n", "test_no_marker.py") == [], "a file with no e2e marker is not an e2e file"
    source = fake("class Clock:\n    def sleep(self, seconds):\n        self.now += seconds\n\ndef test_x():\n    clock = Clock()\n    assert clock\n")
    assert rules_of(source) == [], "a definition of sleep is not a call"
    assert rules_of("import time\n\ndef pause():\n    time.sleep(0.1)\n\ndef helper():\n    time.sleep(0.1)\n", "_helper.py") == ["sleep"], "a helper is always read"


def test_a_word_in_a_docstring_or_a_comment_counts_for_nothing():
    source = fake('def test_x():\n    """Used to call time.sleep(1) and setTimeout(f, 10); port=8080; os.environ["A"] = "1"."""\n    # time.sleep(2)\n    assert True\n')
    assert rules_of(source) == []


def test_the_nth_occurrence_and_the_scope_do_not_depend_on_the_line():
    a = fake("def test_x(page):\n    page.wait_for_timeout(100)\n\n    page.wait_for_timeout(200)\n")
    b = fake("# a comment\n\n\n\ndef test_x(page):\n\n    page.wait_for_timeout(100)\n    page.wait_for_timeout(200)\n")
    key = lambda source: [v.key for v in clocks.analyze(source, "test_fake_e2e.py")]  # noqa: E731
    assert key(a) == key(b) == [("test_fake_e2e.py", "test_x", "sleep", 1), ("test_fake_e2e.py", "test_x", "sleep", 2)]


def test_a_file_with_every_violation_is_red_and_one_that_waits_on_conditions_is_green():
    bad = fake(
        "import os, time, socket, asyncio\n"
        "PAGE = '<script>setTimeout(go, 300)</script>'\n"
        "def test_x(page, took, pids):\n"
        "    os.environ['A'] = '1'\n"
        "    s = socket.socket(); s.bind(('127.0.0.1', 8080))\n"
        "    page.goto('https://example.org/')\n"
        "    time.sleep(1)\n"
        "    assert took < 3\n"
        "    assert len(pids) == 2\n")
    assert rules_of(bad) == sorted(set(clocks.RULES) - {"egress"})
    good = fake(
        "import time\n"
        "def test_x(page, took, control, ready):\n"
        "    page.goto('http://127.0.0.1:%d/' % PORT)\n"
        "    deadline = time.monotonic() + 30\n"
        "    while not ready() and time.monotonic() < deadline:\n"
        "        page.wait_for_timeout(50)\n"
        "    assert took <= control * 3\n")
    assert clocks.analyze(good, "test_fake_e2e.py") == []


def test_a_conftest_without_the_declared_exit_is_refused():
    assert [v.rule for v in clocks.analyze_conftest("import pytest\n\n@pytest.fixture\ndef x():\n    pass\n")] == ["egress"]
    wrong_scope = "import pytest\n\n@pytest.fixture(autouse=True)\ndef _the_lab_declares_the_exit():\n    _lab_egress.install(None, None)\n"
    assert [v.rule for v in clocks.analyze_conftest(wrong_scope)] == ["egress"], "a function-scoped fixture is too late for the module browsers"


def test_a_list_that_names_what_the_sources_no_longer_have_is_red_and_a_new_occurrence_is_not_covered():
    found = clocks.analyze(fake("def test_x(page):\n    page.wait_for_timeout(100)\n"), "test_fake_e2e.py")
    entry = {"file": "test_fake_e2e.py", "scope": "test_x", "rule": "sleep", "nth": 1, "kind": "negative-window", "reason": "x" * 50 + "."}
    assert clocks.compare(found, {"entries": [entry], "debt": []}) == {"new": [], "stale_entries": [], "stale_debt": [], "both": []}
    assert clocks.compare([], {"entries": [entry], "debt": []})["stale_entries"] == [clocks.key_of(entry)]
    assert clocks.compare(found, {"entries": [], "debt": []})["new"] == [clocks.key_of(entry)]
    assert clocks.compare([], {"entries": [], "debt": [dict(entry, **{"class": "A"})]})["stale_debt"] == [clocks.key_of(entry)]


def test_the_debt_is_made_again_from_the_sources_and_an_entry_is_never_made():
    found = clocks.analyze(fake("def test_x(page):\n    page.wait_for_timeout(100)\n    page.wait_for_timeout(200)\n"), "test_fake_e2e.py")
    reviewed = {"file": "test_fake_e2e.py", "scope": "test_x", "rule": "sleep", "nth": 1, "kind": "negative-window", "reason": "x" * 50 + "."}
    stale = {"file": "gone.py", "scope": "test_y", "rule": "sleep", "nth": 1, "kind": "instrument", "reason": "y" * 50 + "."}
    before = {"version": 1, "entries": [reviewed, stale], "debt": [{"file": "gone.py", "scope": "f", "rule": "bound", "nth": 1, "class": "B", "reason": "z"}]}
    after = clocks.regenerate(before, found)
    assert [clocks.key_of(i) for i in after["entries"]] == [clocks.key_of(reviewed), clocks.key_of(stale)], "--write leaves the reviewed entries as they are"
    assert [clocks.key_of(i) for i in after["debt"]] == [("test_fake_e2e.py", "test_x", "sleep", 2)], "the corrected occurrence left, the new one is debt"
    assert clocks.regenerate(before, found, prune_entries=True)["entries"] == [reviewed]
    assert clocks.regenerate(after, found) == after, "made twice, the same"
    assert all(i["class"] == "A" and i["reason"] for i in after["debt"])
    assert clocks.dump(after) == clocks.dump(clocks.regenerate(after, found)) and clocks.dump(after).endswith("}\n")


def test_every_rule_has_a_known_bad_case_in_this_file():
    this = Path(__file__).read_text(encoding="utf-8")
    for rule in clocks.RULES:
        assert ('"%s"' % rule) in this or ("== ['%s']" % rule) in this or ('["%s"]' % rule) in this, "no known-bad input for the rule %s" % rule
