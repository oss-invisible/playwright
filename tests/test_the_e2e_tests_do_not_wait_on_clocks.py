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


def test_the_conftest_declares_the_exit_of_the_lab():
    assert clocks.analyze_conftest((ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")) == []


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
#  Known-bad inputs: each rule refuses its own case
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def test_a_sleep_then_an_assert_is_refused():
    source = fake("import time\n\n" "@pytest.mark.e2e\ndef test_x(page):\n    page.click('#go')\n    time.sleep(0.5)\n    assert page.title() == 'done'\n")
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
    # the number is put in by `%` so that this line is not itself a fixed port to the guard of test_the_marionette_helpers.py
    assert rules_of(fake("def prefs():\n    return {'marionette.port': %d}\n" % 28431)) == ["port"]
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


def test_a_limit_that_is_a_named_variable_or_has_a_slack_added_is_refused_and_a_relative_one_is_not():
    assert rules_of(fake("def test_x(took, limit_s):\n    assert took < limit_s\n")) == ["bound"], "a limit is a limit when it is not written in capitals"
    assert rules_of(fake("def test_x(elapsed, max_ms):\n    assert elapsed <= max_ms\n")) == ["bound"]
    assert rules_of(fake("def test_x(limit_s, took):\n    assert limit_s > took\n")) == ["bound"]
    assert rules_of(fake("def test_x(wait, rtt_ms):\n    assert wait < rtt_ms + 250\n")) == ["bound"], "an absolute slack added to a measure is an absolute limit"
    assert rules_of(fake("def test_x(took, control):\n    assert took <= control + 0.5\n")) == ["bound"]
    assert rules_of(fake("def test_x(res):\n    assert res['opened'] < 3000\n")) == ["bound"], "the time a connection took to open is a duration"
    assert rules_of(fake("def test_x(took, control):\n    assert took <= control * 3\n")) == [], "a factor of a measure of the same run is relative"
    assert rules_of(fake("def test_x(took, control):\n    assert took <= control\n")) == []
    assert rules_of(fake("def test_x(took, control):\n    assert took >= control - 0.5\n")) == [], "a lower bound with a slack cannot fail because the machine is slow"
    assert rules_of(fake("import time\n\ndef test_x(ready, deadline):\n    assert time.monotonic() < deadline, 'never'\n")) == [], "a point in time that ends a wait is no limit on a duration"


def test_a_real_url_is_refused_and_a_local_one_is_not():
    assert rules_of(fake("def test_x(page):\n    page.goto('https://example.com/')\n")) == ["net"]
    assert rules_of(fake("import urllib.request\n\ndef test_x():\n    urllib.request.urlopen('http://127.0.0.1:1/')\n")) == ["net"]
    assert rules_of(fake("import requests\n\ndef test_x():\n    requests.get(URL)\n")) == ["net"]
    assert rules_of(fake("import subprocess\n\ndef test_x():\n    subprocess.run(['pip', 'install', 'x'])\n")) == ["net"]
    for url in ("http://127.0.0.1:%d/", "http://localhost:3000/", "http://a.test/x", "http://x/", "http://203.0.113.80/", "http://[::1]:1/", "http://%s/", "http://{host}/"):
        assert rules_of(fake("def test_x(page):\n    page.goto(%r)\n" % url)) == [], url


def _page_source(markup):
    """The source of a module whose one constant is `markup`, as a page of an e2e test would hold it."""
    return "PAGE = %r\n" % markup


def test_an_xml_namespace_is_a_name_and_not_a_request():
    """An SVG image or an XHTML island in a page is written with its namespace whole (xmlns="http://..."): nothing is fetched from it."""
    svg, xhtml = "http://www.w3.org/2000/svg", "http://www.w3.org/1999/xhtml"
    for markup in ('<svg xmlns="%s" width="4"><foreignObject><div xmlns="%s"></div></foreignObject></svg>' % (svg, xhtml),
                   "<svg xmlns='%s' xmlns:xlink='http://www.w3.org/1999/xlink'></svg>" % svg,
                   'document.createElementNS("%s", "svg")' % svg,
                   'el.setAttributeNS("http://www.w3.org/2000/xmlns/", "xmlns:x", "v")',
                   'const s = "<svg xmlns=\\"%s\\"></svg>"' % svg):
        assert rules_of(fake(_page_source(markup))) == [], markup


def test_a_request_to_the_host_of_a_namespace_is_still_a_request():
    """What makes a URL a name is where it stands (the value of an xmlns attribute, the argument of a namespace call), never the host it names."""
    for markup in ('<a href="http://www.w3.org/">x</a>', '<img src="http://www.w3.org/2000/svg">', 'fetch("https://example.com/x")',
                   '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com/a.png"/></svg>'):
        assert rules_of(fake(_page_source(markup))) == ["net"], markup
    assert rules_of(fake("def test_x(page):\n    page.goto('http://www.w3.org/1999/xhtml')\n")) == ["net"]


def test_a_real_url_is_accepted_in_the_files_of_the_round_after_a_publication():
    source = fake("def test_x(page):\n    page.goto('https://example.com/')\n")
    assert rules_of(source, "test_release_e2e.py") == [] and rules_of(source, "test_upgrade_e2e.py") == []


def test_a_deadline_under_thirty_seconds_on_a_wait_for_something_that_must_happen_is_refused():
    for call in ("page.goto('http://x.test/', timeout=20000)", "page.wait_for_selector('#a', timeout=10000)", "page.locator('#a').click(timeout=5000)",
                 "page.set_default_timeout(5000)", "page.set_default_navigation_timeout(20000)", "page.wait_for_function('1', timeout=29999)",
                 "expect(page.locator('#a')).to_be_visible(timeout=500)", "thread.join(timeout=5)", "subprocess.run(cmd, timeout=10)",
                 "socket.create_connection(addr, timeout=10)", "actions.click('#a', timeout=15)"):
        assert rules_of(fake("def test_x(page, thread, cmd, addr, actions):\n    %s\n" % call)) == ["timeout"], call


def test_a_deadline_of_thirty_seconds_or_more_or_no_deadline_is_accepted():
    for call in ("page.goto('http://x.test/', timeout=30000)", "page.goto('http://x.test/', timeout=60_000)", "page.wait_for_selector('#a', timeout=30_000)",
                 "page.set_default_timeout(30000)", "thread.join(timeout=30)", "socket.create_connection(addr, timeout=30)", "subprocess.run(cmd, timeout=300)",
                 "lifecycle.goto(url, until='load', timeout=30)", "actions.click('#a', timeout=45)", "page.goto(url, timeout=0)", "page.goto(url, timeout=timeout_ms)",
                 "page.goto(url, timeout=None)", "run(cmd, timeout=1800)"):
        assert rules_of(fake("def test_x(page, thread, cmd, url, addr, actions, lifecycle, timeout_ms):\n    %s\n" % call)) == [], call


def test_a_deadline_that_is_a_named_constant_or_a_default_is_read_as_what_it_is():
    assert rules_of(fake("GOTO_MS = 20000\n\ndef test_x(page):\n    page.goto('http://x.test/', timeout=GOTO_MS)\n")) == ["timeout"]
    assert rules_of(fake("LIMIT_S = 10\n\ndef test_x(page):\n    page.goto('http://x.test/', timeout=int(LIMIT_S * 1000))\n")) == ["timeout"]
    assert rules_of(fake("GOTO_MS = 30000\n\ndef test_x(page):\n    page.goto('http://x.test/', timeout=GOTO_MS)\n")) == []
    assert rules_of(fake("def wait_until_ready(timeout=5):\n    pass\n")) == ["timeout"], "every call that leaves the deadline out waits that long"
    assert rules_of(fake("def wait_until_ready(timeout=30.0):\n    pass\n")) == []


def test_a_wait_for_something_that_must_not_happen_is_accepted_by_its_shape():
    """A window in which the call is EXPECTED to run out of time can only be a weaker check on a slow machine, never a red."""
    raises = fake("def test_x(page):\n    with pytest.raises(Exception):\n        page.click('#a', timeout=500)\n")
    assert rules_of(raises) == []
    assert rules_of(fake("NEGATIVE_S = 0.3\n\ndef test_x(actions):\n    actions.click('#a', timeout=NEGATIVE_S)\n")) == []
    swallowed = fake("def test_x(page):\n    try:\n        page.go_back(timeout=3000)\n    except TimeoutError:\n        pass\n")
    assert rules_of(swallowed) == []
    failing = fake("def test_x(page):\n    try:\n        page.go_back(timeout=3000)\n    except TimeoutError:\n        pytest.fail('late')\n")
    assert rules_of(failing) == ["timeout"], "a timeout that fails the test is a wait for something that must happen"
    assert rules_of(fake("def test_x(page):\n    with pytest.raises(Exception):\n        pass\n    page.click('#a', timeout=500)\n")) == ["timeout"], "after the block it is a positive wait"


def test_a_judge_that_reports_a_duration_over_an_absolute_margin_is_refused_like_an_assert():
    judge = "def judge(walls_ms):\n    problems = []\n    if walls_ms[1] > walls_ms[0] + MARGIN_MS:\n        problems.append('slow')\n    return problems\n"
    assert rules_of(fake("MARGIN_MS = 1500\n\n" + judge), "_judge.py") == ["bound"]
    assert rules_of(fake("def judge(took):\n    if took > 4.0:\n        raise AssertionError('slow')\n"), "_judge.py") == ["bound"]
    assert rules_of(fake("def judge(took):\n    if 4.0 < took:\n        raise AssertionError('slow')\n"), "_judge.py") == ["bound"]
    assert rules_of(fake("def judge(took, control):\n    if took > control * 3:\n        raise AssertionError('slow')\n"), "_judge.py") == [], "relative to the same run"
    assert rules_of(fake("def judge(took):\n    if took > 4.0:\n        print('slow')\n"), "_judge.py") == [], "an if that reports nothing is the logic of the test"
    assert rules_of(fake("def judge(took):\n    if took < 4.0:\n        raise AssertionError('too fast')\n"), "_judge.py") == [], "a lower bound cannot fail because the machine is slow"


def test_a_count_of_processes_is_refused_outside_the_helpers_that_count():
    source = fake("def test_x(pids):\n    assert len(pids) == 1\n")
    assert rules_of(source) == ["count"]
    assert clocks.analyze(source, "_processes.py") == []


def test_a_lab_server_given_a_delay_needs_the_serial_lane_and_one_given_none_does_not():
    """A test that gives a lab gateway or server a delay (the round trip of a far gateway, a DoH that answers late, a slow page) and then waits for an outcome
    the product decides with a time budget (the core's UDP measurement, a DoH timeout) reads the wall clock: the lab's own threads, starved on a loaded
    runner, make the answer late by a margin the budget never allowed for. Such a test runs in the ``wall_clock`` lane (serial, the machine alone)."""
    gateway = "def test_x(monkeypatch):\n    with coherent(user='a', password='b', rtt_ms=250) as gateway:\n        gateway.proxy\n"
    assert rules_of(fake(gateway)) == ["lab-delay"]
    assert rules_of(fake("def test_x():\n    with Chain(rtt_ms=130, exit_host=exit_ip) as chain:\n        chain.proxy\n")) == ["lab-delay"]
    assert rules_of(fake("def test_x(gateway):\n    gw = gateway('gw1', first_connection_delay=1.0)\n")) == ["lab-delay"]
    assert rules_of(fake("def test_x(doh):\n    doh.delay_names['slow-a.test'] = 2.0\n")) == ["lab-delay"]
    assert rules_of(fake("def test_x(sites):\n    sites.route('B', '/late', 'one', delay=0.3)\n")) == ["lab-delay"]
    assert rules_of(fake("def test_x():\n    slow, port = _serve(SLOW, delay=1.5)\n")) == ["lab-delay"]
    assert rules_of(fake("def test_x(monkeypatch, rtt_ms):\n    helper(monkeypatch, rtt_ms)\n")) == ["lab-delay"], "a parameter named rtt_ms is a distance given by a parametrize"
    assert rules_of(fake("def test_x():\n    coherent(rtt_ms=rtt)\n")) == ["lab-delay"], "a value the test computes is a delay until it is shown to be 0"
    found = clocks.analyze(fake("def test_x(monkeypatch):\n    pass\n\ndef test_y():\n    coherent(rtt_ms=250)\n"), "test_fake_e2e.py")
    assert [(v.scope, v.rule, v.nth, v.klass) for v in found] == [("test_y", "lab-delay", 1, "A")]

    fixture = ("@pytest.fixture\ndef gateway():\n    with Chain(rtt_ms=130) as chain:\n        yield chain\n\n"
               "@pytest.fixture\ndef through(gateway):\n    return gateway\n\n")
    assert [v.scope for v in clocks.analyze(fake(fixture + "def test_x(through):\n    pass\n\ndef test_y(page):\n    pass\n"), "test_fake_e2e.py")] == ["test_x"], \
        "a test is as delayed as the fixture it takes, through the fixtures that take that one"
    assert rules_of(fake(fixture + "@pytest.mark.usefixtures('gateway')\ndef test_x():\n    pass\n")) == ["lab-delay"]

    # green: the same test in the serial lane, whichever way the lane is declared
    assert rules_of(fake("@pytest.mark.wall_clock\n" + gateway)) == []
    assert rules_of("import pytest\n\npytestmark = [pytest.mark.e2e, pytest.mark.wall_clock]\n\n" + gateway) == []
    assert rules_of(fake("@pytest.mark.wall_clock\nclass TestFar:\n    def test_x(self):\n        coherent(rtt_ms=250)\n")) == []
    assert rules_of(fake(fixture + "@pytest.mark.wall_clock\ndef test_x(through):\n    pass\n")) == []
    # green: a lab server with no delay, and a delay of nothing
    assert rules_of(fake("def test_x():\n    with coherent(user='a', password='b', relay_host=host) as gateway:\n        gateway.proxy\n")) == []
    assert rules_of(fake("def test_x():\n    coherent(rtt_ms=0)\n    gateway('gw1', first_connection_delay=0.0)\n")) == []
    assert rules_of(fake("def test_x(page):\n    page.keyboard.type('abc', delay=120)\n    helper(delay=0.5)\n")) == [], "only the knobs of the lab servers are delays"
    unit_source = "\n".join(["import pytest", "", "@pytest.mark.unit", "def test_x():", "    coherent(rtt_ms=250)", ""])
    assert rules_of(unit_source, "test_unit_fake.py") == [], "a unit test is not read"


def test_the_far_away_gateway_family_runs_in_the_serial_lane():
    """Run 45058944650: on a runner with no CPU left the core's UDP measurement (a fixed 3 s budget) ran out 0.3 s before a gateway 250 ms away was
    scheduled to answer. The tests that give the lab gateway a distance and wait for what the product decides in time are wall-clock tests."""
    import ast

    names = {"test_a_gateway_that_is_far_away_still_gives_the_page_a_srflx_born_through_its_relay",
             "test_the_first_candidate_of_a_gather_does_not_wait_for_a_gateway_that_is_far_away",
             "test_a_gather_after_the_page_loaded_through_the_gateway_costs_only_the_road_to_the_server"}
    tree = ast.parse((ROOT / "tests" / "test_webrtc_realness.py").read_text(encoding="utf-8"))
    found = {n.name: any("wall_clock" in clocks._marks(d) for d in n.decorator_list)
             for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names}
    assert set(found) == names
    assert [n for n, marked in found.items() if not marked] == []


def test_a_polling_step_with_a_deadline_is_accepted():
    for body in (
        "import time\n\ndef test_x(ready):\n    deadline = time.monotonic() + 30\n    while not ready() and time.monotonic() < deadline:\n        time.sleep(0.05)\n",
        "import time\n\ndef test_x(ready):\n    end = time.time() + 30\n    while True:\n        if ready():\n            break\n        assert time.time() < end, 'never'\n        time.sleep(0.05)\n",
        "import time\n\ndef test_x(ready):\n    for _ in range(100):\n        if ready():\n            break\n        time.sleep(0.1)\n",
        "def test_x(page, ready):\n    wait_until(ready, 30, lambda: page.wait_for_timeout(50))\n",
        "def test_x(page, ready):\n    watch(ready, bool, 30, pump=lambda: page.wait_for_timeout(50))\n",
        "def pump(page):\n    page.wait_for_timeout(50)\n",
        "async def test_x(ready):\n    async def ask():\n        await asyncio.sleep(0.1)\n    await awatch(ready, bool, 30, ask)\n",
        "def _wait_for_the_frame(page):\n    page.wait_for_timeout(50)\n",
        "import time\n\nclass SlowHandler(BaseHTTPRequestHandler):\n    def do_GET(self):\n        time.sleep(1.0)\n",
    ):
        assert rules_of(fake(body)) == [], body


def test_a_wait_without_a_deadline_or_a_condition_is_not_a_polling_step():
    assert rules_of(fake("import time\n\ndef test_x(ready):\n    while not ready():\n        time.sleep(0.05)\n")) == ["sleep"]
    assert rules_of(fake("import time\n\ndef test_x(ready):\n    for _ in range(3):\n        time.sleep(0.05)\n")) == ["sleep"]
    assert rules_of(fake("def test_wait_for_the_title(page):\n    page.wait_for_timeout(500)\n")) == ["sleep"], "a test is not a wait helper because of its name"


def test_a_unit_test_and_a_fake_clock_are_not_read():
    assert rules_of("import pytest\nimport time\n\n" "@pytest.mark.unit\ndef test_x():\n    time.sleep(0.1)\n", "test_unit_fake.py") == []
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
        "    page.click('#go', timeout=5000)\n"
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
    assert [v.rule for v in clocks.analyze_conftest("import pytest\n\n" "@pytest.fixture\ndef x():\n    pass\n")] == ["egress"]
    wrong_scope = "import pytest\n\n" "@pytest.fixture(autouse=True)\ndef _the_lab_declares_the_exit():\n    _lab_egress.install(None, None)\n"
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


def test_a_reason_a_person_wrote_on_a_debt_item_stays_while_the_occurrence_is_there():
    found = clocks.analyze(fake("def test_x(page):\n    page.wait_for_timeout(100)\n"), "test_fake_e2e.py")
    waiting = {"file": "test_fake_e2e.py", "scope": "test_x", "rule": "sleep", "nth": 1, "class": "A",
               "reason": "Waiting for the verdict on the product: the strict form of this test is on its own branch."}
    after = clocks.regenerate({"version": 1, "entries": [], "debt": [waiting]}, found)
    assert after["debt"] == [waiting], "--write kept the written reason"
    assert clocks.regenerate({"version": 1, "entries": [], "debt": [waiting]}, [])["debt"] == [], "and drops it with the occurrence"


def test_every_rule_has_a_known_bad_case_in_this_file():
    this = Path(__file__).read_text(encoding="utf-8")
    for rule in clocks.RULES:
        assert ('"%s"' % rule) in this or ("== ['%s']" % rule) in this or ('["%s"]' % rule) in this, "no known-bad input for the rule %s" % rule
