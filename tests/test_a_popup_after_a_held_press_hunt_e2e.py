"""PROOF OF A MECHANISM, temporary (a ``hunt/`` branch, never main): a popup opened from a click whose press was held more than 5 s is refused.

The popup case of ``test_nothing_of_a_context_leaves_but_through_its_proxy_e2e.py`` once failed with ``made: False`` (``window.open`` in the click handler returned null).
The one road left by the code is the popup blocker: for a mouse, the transient user activation is born at ``pointerdown`` and lasts 5000 ms
(``dom.user_activation.transient.timeout``), and a ``window.open`` after that, from the ``click`` that ends the press, is refused: retail behaviour.

This holds the press on purpose: ``mouse.down()``, a wait, ``mouse.up()``, on the same button as the popup case (``_POPUP_BUTTON_JS``). Held 5.5 s the popup must NOT be made
(``window.__popup`` null, ``probe.isActive`` false, the click at least 5000 ms after the pointer went down); held 0.1 s (the control) it must be. The page is the one of the
popup case (a context behind the SOCKS5 gateway, ``page-a.test``); the read after the click is the popup part of ``_WINDOW_ICE_JS`` (``popupIs`` and ``probe``) without
the PeerConnection, which needs the lab and decides nothing here.
"""
from __future__ import annotations

import json

import pytest

import _doh_servers as ds
from test_doh_through_the_proxy_e2e import _hermetic_session, _launch, _page_in  # noqa: F401  (the hermetic session is autouse and imported by name)
from test_nothing_of_a_context_leaves_but_through_its_proxy_e2e import _POPUP_BUTTON_JS

pytestmark = [pytest.mark.e2e, pytest.mark.usefixtures("the_udp_line")]

#: What the page says of its popup: the same reads as the popup branch of ``_WINDOW_ICE_JS`` (``made``, ``popupIs``, ``probe``).
_READ_JS = """() => {
  const out = {made: !!window.__popup, popupIs: null, probe: null};
  try { out.popupIs = window.__popup === null ? 'null' : typeof window.__popup; out.probe = window.__probe || null; } catch (e) {}
  return out;
}"""


def _press_and_hold(firefox_binary, pages, gateway, held_s):
    proxy = gateway("gw-context")
    with _launch(firefox_binary, None) as browser:
        context, page = _page_in(browser, proxy=proxy.proxy)
        page.goto(pages.url("page-a.test"), wait_until="load", timeout=30000)
        page.evaluate(_POPUP_BUTTON_JS)
        box = page.locator("#open-dead").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        ds.wait_until(lambda: False, held_s)          # the press is held on purpose: this is the stimulus
        page.mouse.up()
        outcome = page.evaluate(_READ_JS)
        context.close()
    probe = outcome.get("probe") or {}
    gap = (probe["clickAt"] - probe["down"]) if "clickAt" in probe and "down" in probe else None
    outcome["gap_ms"] = gap
    print("HELD-PRESS " + json.dumps({"held_s": held_s, "made": outcome["made"], "popupIs": outcome["popupIs"], "probe": probe, "clickAt-down": gap}, sort_keys=True), flush=True)
    return outcome


def test_a_popup_after_a_press_held_5_5_s_is_refused(firefox_binary, pages, gateway):
    outcome = _press_and_hold(firefox_binary, pages, gateway, 5.5)
    probe = outcome["probe"] or {}
    assert outcome["made"] is False, outcome
    assert outcome["popupIs"] == "null", outcome
    assert probe.get("isActive") is False, outcome
    assert outcome["gap_ms"] is not None and outcome["gap_ms"] >= 5000, outcome


def test_a_popup_after_a_press_held_0_1_s_is_made(firefox_binary, pages, gateway):
    outcome = _press_and_hold(firefox_binary, pages, gateway, 0.1)
    assert outcome["made"] is True, outcome
