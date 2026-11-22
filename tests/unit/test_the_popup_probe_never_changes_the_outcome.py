"""The evidence of a popup that is not made never changes the outcome of the test it observes. No browser.

[popup-socks5] on Windows got `made: False` (run 45064266867): `window.__popup` was falsy after the click, and nothing said whether the transient activation
of the click had expired (a stalled runner, more than 5 s between pointerdown and click) or `window.open` returned null or threw with the activation still
valid. The test now records the activation, the gap and null against a throw. A probe is bookkeeping: every recording is made to fail here, and
the recording returns without raising, so the test goes on to the same assertions it had; the page's handler keeps what `window.__popup` held in the three
cases (a window, null, a throw).
"""
import json

import pytest

pytestmark = pytest.mark.unit

import test_nothing_of_a_context_leaves_but_through_its_proxy_e2e as e2e

POPUP = {"window": "popup", "made": False, "popupIs": "undefined", "click_s": 0.12,
         "probe": {"down": 100.0, "clickAt": 5600.5, "isActive": False, "hasBeenActive": True, "openNull": True}}


def test_the_probe_is_recorded_with_the_gap_and_the_open_result():
    saved = {}
    e2e._record_the_popup_probe(lambda name, value: saved.__setitem__(name, value), POPUP)
    said = json.loads(saved["popup_probe"])
    assert said["down_to_click_ms"] == 5500.5, "the gap between the pointerdown and the click is the evidence of an expired activation"
    assert said["probe"]["isActive"] is False and said["probe"]["openNull"] is True and said["popup_is"] == "undefined"


def test_a_window_that_is_not_a_popup_records_nothing():
    saved = {}
    e2e._record_the_popup_probe(lambda name, value: saved.__setitem__(name, value), {"window": "iframe", "made": True})
    assert saved == {}


def _failing(name, value):
    raise RuntimeError("the recording itself failed")


@pytest.mark.parametrize("outcome", [POPUP,
                                     {**POPUP, "probe": "not a dict"},
                                     {**POPUP, "probe": {"down": "x", "clickAt": None}},
                                     {"window": "popup"},
                                     None,
                                     "not even a dict"], ids=["recording-raises", "probe-is-text", "probe-has-no-numbers", "empty", "none", "text"])
def test_a_probe_that_fails_on_purpose_leaves_the_test_where_it_was(outcome, capsys):
    assert e2e._record_the_popup_probe(_failing, outcome) is None   # it returned: no exception reached the assertions of the test
    assert e2e._record_the_popup_probe(lambda n, v: None, outcome) is None


def test_the_click_time_never_fails_the_outcome_it_is_added_to():
    outcome = {"window": "popup"}
    e2e._note_the_click(outcome, 0.1234)
    assert outcome["click_s"] == 0.123
    e2e._note_the_click(None, 0.1)                 # nothing to write into: swallowed
    e2e._note_the_click(outcome, "not a number")   # not a number: swallowed, the outcome keeps what it had
    assert outcome["click_s"] == 0.123


def test_the_handler_keeps_what_the_popup_variable_held_in_a_window_null_and_throw_case():
    js = e2e._POPUP_BUTTON_JS
    assert "window.__popup = opened;" in js and "if (window.__popup) window.__popup.close();" in js
    assert "throw e;" in js, "a throw of window.open stays the handler's own: __popup is not assigned and the error is the page's, as before"
    assert js.index("throw e;") < js.index("window.__popup = opened;")
    assert "await" not in js and "setTimeout" not in js and "sleep" not in js, "a probe adds no wait"
