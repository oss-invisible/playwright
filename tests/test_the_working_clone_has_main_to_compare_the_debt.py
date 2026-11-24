"""A test of the clone it runs in: ``origin/main`` is a ref only in a clone that fetched it.

``test_the_debt_does_not_grow_against_main`` reads the list of clock exceptions from ``origin/main``; the checkout of the candidate job has no such ref
("origin/main is not in this checkout"), so it is red there for a reason that is not the code. The pull-request job fetches main and runs it
(``tests.yml``). It lives in its own file, out of ``tests/test_the_e2e_tests_do_not_wait_on_clocks.py`` (the rules, true in any checkout), and is a file of the
environment of ``tests/test_the_unit_suite_is_accounted_for.py``.
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
WIRE_GATE = "tests/gates/wire_method_debt.json"


def _main_document(gate: str = GATE):
    """The list of main, or None when it has none; the reason of the miss otherwise."""
    for ref in ("origin/main", "main"):
        shown = subprocess.run(["git", "show", "%s:%s" % (ref, gate)], cwd=ROOT, capture_output=True, text=True)
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


def test_the_wire_method_debt_does_not_grow_against_main():
    """The budget of the wire methods that the client sends and the server neither serves nor refuses (``tests/gates/wire_method_debt.json``, compared with the
    code by ``test_every_wire_method_is_served_or_refused.py``) may only shrink: it can be changed together with the test, so it is read from MAIN, and main is only
    there when the checkout has it (a developer without it skips, a CI run without it is red: a comparison that compares nothing is not a green)."""
    document, ref = _main_document(WIRE_GATE)
    if document == "no ref":
        if os.environ.get("GITHUB_ACTIONS"):
            pytest.fail("origin/main is not in this checkout, so the wire-method debt cannot be compared with it: fetch it (git fetch origin main) before the tests")
        pytest.skip("no origin/main in this checkout: the wire-method debt is not compared with main (CI does)")
    if document is None:
        return      # main has no budget: this is the change that brings it, there is nothing to grow from
    mine = json.loads((ROOT / WIRE_GATE).read_text(encoding="utf-8"))
    debt = lambda doc: {(i["channel"], i["method"]) for i in doc["debt"]}
    partial = lambda doc: {(i["channel"], i["method"], i["detail"]) for i in doc["partial"]}
    grown = sorted(debt(mine) - debt(document))
    grown_partial = sorted(partial(mine) - partial(document))
    assert grown == [] and grown_partial == [], (
        "the wire-method budget has entries that %s does not have: a method the client sends is now neither served nor refused. Serve it or refuse it by name in "
        "perimeter.py; the budget only shrinks. debt: %s; partial: %s" % (ref, grown, grown_partial))
