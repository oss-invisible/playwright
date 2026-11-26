"""The P2 verdict of the fake web when stock Firefox loses the end of a request the page itself aborted (no browser).

Stock Firefox, on Windows, sometimes never reports the end of a fetch that the page aborts while the context route holds it (page
cancel_among_beacons, a request `/r?kind=json&delay=2000&id=cab<N>`; every other request ended: a request with no end, not a late one).
A stock round that did not idle only because of such a request is "stock unsettled", not a broken control; ours is then judged against
the outcome the page declares. Anything else stock does not idle on, on a page that stock_firefox declares idle, stays a broken control.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from fakeweb import profile_p2, sites  # noqa: E402
import run_fakeweb  # noqa: E402

PAGES = json.loads((ROOT / "tests" / "fakeweb" / "pages.json").read_text(encoding="utf-8"))
DECLARED = PAGES["stock_firefox"]
MAY_LOSE = PAGES["stock_firefox_may_lose"]

CAB6 = {"type": "fetch", "method": "GET", "url": "http://127.0.0.1:59784/r?kind=json&delay=2000&id=cab6",
        "frame": "http://127.0.0.1:59784/p/cancel_among_beacons", "t": 0.09}
BEACON = dict(CAB6, type="beacon", method="POST", url="http://127.0.0.1:59785/beacon?n=3")
KEEPALIVE = dict(CAB6, url="http://127.0.0.1:59785/r?kind=json&id=cak9")


def _stock_round_6(**more):
    """The real stock record of round 6 (Windows, run 45621332479)."""
    rec = {"idle": False, "err": "Timeout 10000ms exceeded.", "seconds": 10.06, "in_flight": [CAB6], "round": 6,
           "pattern": "cancel_among_beacons", "canary": False, "after": None}
    rec.update(more)
    return rec


def _ours(idle, **more):
    rec = {"idle": idle, "err": None if idle else "Timeout 10000ms exceeded.", "seconds": 1.0, "round": 6,
           "pattern": "cancel_among_beacons", "canary": False, "after": None}
    rec.update(more)
    return rec


def test_stock_that_lost_the_end_of_an_aborted_fetch_is_unsettled_not_broken_and_ours_that_idled_does_not_fail():
    fails, broken, unsettled = profile_p2.verdict([_ours(True)], [_stock_round_6()], DECLARED, MAY_LOSE)
    assert broken == [] and fails == []
    assert [key for key, _err in unsettled] == [(6, "cancel_among_beacons", False, None)]


def test_in_that_round_ours_that_did_not_idle_fails_against_the_declared_outcome():
    fails, broken, unsettled = profile_p2.verdict([_ours(False)], [_stock_round_6()], DECLARED, MAY_LOSE)
    assert [key for key, _err in fails] == [(6, "cancel_among_beacons", False, None)]
    assert broken == []
    assert len(unsettled) == 1


@pytest.mark.parametrize("extra", [BEACON, KEEPALIVE], ids=["beacon", "keepalive"])
def test_a_stock_round_with_another_request_in_flight_is_a_broken_control(extra):
    fails, broken, unsettled = profile_p2.verdict([_ours(True)], [_stock_round_6(in_flight=[CAB6, extra])], DECLARED, MAY_LOSE)
    assert [key for key, _err in broken] == [(6, "cancel_among_beacons", False, None)]
    assert unsettled == [] and fails == []


def test_a_stock_round_with_no_request_in_flight_listed_is_a_broken_control():
    empty = _stock_round_6(in_flight=[])
    missing = _stock_round_6()
    del missing["in_flight"]
    for stock in (empty, missing):
        fails, broken, unsettled = profile_p2.verdict([_ours(True)], [stock], DECLARED, MAY_LOSE)
        assert len(broken) == 1 and unsettled == [] and fails == []


def test_the_same_record_on_a_page_with_no_entry_is_a_broken_control():
    stock = _stock_round_6(pattern="leaving_beacons")
    fails, broken, unsettled = profile_p2.verdict([_ours(True, pattern="leaving_beacons")], [stock], DECLARED, MAY_LOSE)
    assert [key for key, _err in broken] == [(6, "leaving_beacons", False, None)]
    assert unsettled == [] and fails == []


def test_without_the_may_lose_argument_the_old_callers_see_a_broken_control():
    fails, broken, unsettled = profile_p2.verdict([_ours(True)], [_stock_round_6()], DECLARED)
    assert len(broken) == 1 and unsettled == []


def test_a_stock_that_idled_and_ours_that_did_not_is_still_a_fail():
    fails, broken, unsettled = profile_p2.verdict([_ours(False)], [_stock_round_6(idle=True, err=None, in_flight=None)], DECLARED, MAY_LOSE)
    assert len(fails) == 1 and broken == [] and unsettled == []


def test_every_may_lose_entry_is_a_page_stock_declares_idle_and_its_url_names_the_aborted_fetch_only():
    assert MAY_LOSE, "the list this test holds is empty"
    for pattern, entry in MAY_LOSE.items():
        assert pattern in sites.PATTERNS, pattern
        assert DECLARED.get(pattern) == "idle", pattern
        assert entry["what"] and entry["measured"]
        regex = re.compile(entry["url"])
        assert regex.search(CAB6["url"])
        assert not regex.search(BEACON["url"]) and not regex.search(KEEPALIVE["url"])


def test_the_run_prints_the_unsettled_cell_writes_the_rate_and_exits_green(tmp_path, capsys):
    out = tmp_path / "report.json"
    args = argparse.Namespace(profile="p2", repetitions=2, out=str(out))
    stock = [_stock_round_6(round=1, idle=True, err=None, in_flight=None), _stock_round_6(),
             {"idle": True, "err": None, "round": 6, "pattern": "quiet", "canary": True, "after": "cancel_among_beacons"}]
    candidate = [_ours(True, round=1), _ours(True), _ours(True, pattern="quiet", canary=True, after="cancel_among_beacons")]
    fails, broken, unsettled = profile_p2.verdict(candidate, stock, DECLARED, MAY_LOSE)
    code = run_fakeweb.finish_p2(args, ["cancel_among_beacons"], candidate, stock, fails, broken, unsettled, MAY_LOSE)
    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert lines[0] == "STOCK UNSETTLED  cancel_among_beacons round 6: %s (pages.json stock_firefox_may_lose)" % CAB6["url"]
    assert lines[-1] == "cells: 3, fails: 0, broken controls: 0, stock unsettled: 1"
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["platform"] == sys.platform
    assert report["stock_unsettled"] == [{"round": 6, "pattern": "cancel_among_beacons", "in_flight": [CAB6["url"]]}]
    assert report["stock_unsettled_rate"] == {"cancel_among_beacons": {"unsettled": 1, "rounds": 2}}


def test_the_run_is_red_on_a_fail_or_a_broken_control_even_with_an_unsettled_cell(tmp_path, capsys):
    args = argparse.Namespace(profile="p2", repetitions=1, out=None)
    stock = [_stock_round_6(), _stock_round_6(pattern="leaving_beacons")]
    candidate = [_ours(True), _ours(True, pattern="leaving_beacons")]
    fails, broken, unsettled = profile_p2.verdict(candidate, stock, DECLARED, MAY_LOSE)
    code = run_fakeweb.finish_p2(args, ["cancel_among_beacons", "leaving_beacons"], candidate, stock, fails, broken, unsettled, MAY_LOSE)
    lines = capsys.readouterr().out.splitlines()
    assert code == 1
    assert lines[-1] == "cells: 2, fails: 0, broken controls: 1, stock unsettled: 1"
