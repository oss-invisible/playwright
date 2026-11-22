"""The code a client evaluates in the page is not named "debugger eval code".

Juggler's `Runtime.evaluate` runs the client's script through the debugger
(`Debugger.Object.executeInGlobal`), and SpiderMonkey names such a script
"debugger eval code" and gives it the introduction type "debugger eval". A page
reads the name from `new Error().stack` on a call it made inside an evaluation
of the client, and no retail Firefox has a frame with that name. The engine's
hook (js/src/debugger, `fingerprint/js-surface/eval-name`) gives the default
name "eval code" and the type "eval"; a name given by the caller still wins.

Negative control: a script that names itself (`//# sourceURL=`) is read back
with the name it gave, untouched, next to the one the engine gives to a script
that names nothing, so the default is the only name that changed. (A filename
passed to `executeInGlobal` by a caller is not reachable from a page; the
display URL is the name a caller can give.) Runs the same on Linux and Windows.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

_CALLER_NAME = "caller-named-script.js"


def _stacks(firefox_binary):
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=42, binary_path=firefox_binary, headless=True) as browser:
        page = browser.new_page()
        page.goto("about:blank")
        return {
            "expression": page.evaluate("new Error().stack"),
            "function": page.evaluate("() => new Error().stack"),
            "named": page.evaluate("new Error().stack\n//# sourceURL=%s" % _CALLER_NAME),
        }


def test_the_evaluated_code_has_no_debugger_name(firefox_binary):
    stacks = _stacks(firefox_binary)
    for how in ("expression", "function"):
        stack = stacks[how]
        assert "debugger eval" not in stack, (how, stack)
        # What the engine gives instead: the name retail gives to evaluated code.
        assert "eval code" in stack, (how, stack)
    # Control: the name the caller gave is the one read, and it replaces the
    # default one, which is therefore the only name the hook changes.
    assert _CALLER_NAME in stacks["named"], stacks["named"]
    assert "debugger eval" not in stacks["named"], stacks["named"]
    assert "eval code" not in stacks["named"], stacks["named"]
    assert _CALLER_NAME not in stacks["expression"], stacks["expression"]
