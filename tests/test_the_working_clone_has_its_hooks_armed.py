"""A test of the clone it runs in: ``core.hooksPath`` points at ``.githooks`` only in a clone somebody armed.

``git`` does not arm a checked-in hook directory; a developer does it once per clone (``invisible_core.testing.assert_hooks_are_armed`` says how). The
checkout of the CI jobs is a fresh one and never has it, so the test is red there for a reason that is not the code. It lives in its own file, out of
``tests/test_publish_gate_wiring.py`` (which holds the wiring of the gate, true in any checkout), and is a file of the environment of
``tests/test_the_unit_suite_is_accounted_for.py``.
"""
from __future__ import annotations

import pathlib

import pytest

pytestmark = pytest.mark.unit

_REPO = pathlib.Path(__file__).resolve().parents[1]


def test_the_hooks_are_armed_in_this_clone():
    """A checked-in hook directory does nothing until core.hooksPath points at
    it, and git will not do that for you.

    Until 2026-07-27 both the installer and this reminder lived in the core, so
    THIS repo shipped a hook that nothing armed and nothing checked was armed:
    a fresh clone pushed with the tests, the pin, the name scan and the publish
    gate all skipped, and said nothing about it.
    """
    from invisible_core.testing import assert_hooks_are_armed

    assert_hooks_are_armed(_REPO)
