"""A screenshot parameter that Playwright honours by MODIFYING THE PAGE is refused by name, not silently ignored.

Stock Playwright 1.61 takes `mask` (an overlay painted on the page), `style` (a `<style>` element injected), `animations="disabled"` (finite animations finished,
infinite ones cancelled) and `caret` (hidden with `caret-color: transparent`, drawn only for `"initial"`) by changing the page before the picture and putting it back
after. `PageDispatcher.op_screenshot` never read any of them: the call succeeded and the picture came back as if the parameter had not been written. The caller cannot tell
a masked picture from an unmasked one, and the picture is the one thing that must not lie. They are refused now, each by its name, with what to do instead.

The single point is `PageDispatcher.op_screenshot` (the element's reaches it with the caller's parameters, and a locator's is the element's), so the page, the full page,
a clip, an element and a locator are checked here at the wire. What passes: `animations="allow"`, `caret="hide"` (the engine never draws the caret, which is what stock's
default produces), an empty `mask`, an empty `style`, a `maskColor` with no mask, and `omitBackground` with jpeg (stock does nothing there).

`omitBackground` with png is Firefox's own: stock's `setBackgroundColor` throws `Not implemented` there (`FFPage.setBackgroundColor`, reached from `Screenshotter._screenshot`
for png only), and the text is the same here.

No browser: the dispatchers are built as tests/test_a_screenshot_sends_its_scale_to_the_engine.py builds them.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from invisible_core.juggler.connection import EventListeners
from invisible_core.juggler.lifecycle import Lifecycle
from invisible_playwright._juggler.dispatcher import Dispatcher, ProtocolException
from invisible_playwright._juggler.server import (
    ElementHandleDispatcher,
    FrameDispatcher,
    JugglerServer,
    PageDispatcher,
)

pytestmark = pytest.mark.unit

BOX = {"x": 0, "y": 0, "width": 1280, "height": 720, "fullWidth": 1280, "fullHeight": 3000}

# what the vendored client puts on the wire for `mask=[page.locator("#a")]`
MASK = [{"frame": "frame@1", "selector": "#a"}]


def make_page():
    server = JugglerServer()
    server.attach(SimpleNamespace(emit_message=lambda m: None))
    context = Dispatcher(server, None)
    context.options = {}
    page = object.__new__(PageDispatcher)
    page.context = context
    page.lifecycle = Lifecycle(EventListeners(), "session")
    page.injected = Mock()
    page.injected.evaluate.return_value = dict(BOX)
    page.actions = Mock()
    page._frames = {}
    page._navigation_requests = {}
    page._announced_closed = False
    Dispatcher.__init__(page, server, context)
    page.frame = FrameDispatcher(server, page, "main")
    page.lifecycle._on_event("Page.frameAttached", {"frameId": "main", "parentFrameId": None})
    page.send = Mock(return_value={"data": "AAAA"})
    return page


def _page_shot(page, params):
    return page.op_screenshot(params)


def _full_page_shot(page, params):
    return page.op_screenshot(dict(params, fullPage=True))


def _clip_shot(page, params):
    return page.op_screenshot(dict(params, clip={"x": 5, "y": 6, "width": 100, "height": 50}))


def _element_shot(page, params):
    handle = ElementHandleDispatcher(page.server, page.frame, "object-1")
    page.injected.bounding_box.return_value = {"x": 10, "y": 20, "width": 30, "height": 40}
    return handle.op_screenshot(params)


# a locator's screenshot is the element handle's (`Locator.screenshot` -> `_with_element` -> `ElementHandle.screenshot`), with the same parameters
KINDS = [_page_shot, _full_page_shot, _clip_shot, _element_shot]
KIND_IDS = ["page", "full_page", "clip", "element_or_locator"]

REFUSED = [
    ({"mask": MASK}, "mask"),
    ({"mask": MASK, "maskColor": "#00f"}, "mask"),
    ({"style": "* { color: red }"}, "style"),
    ({"animations": "disabled"}, "animations"),
    ({"caret": "initial"}, "caret"),
]
REFUSED_IDS = ["mask", "mask_with_color", "style", "animations_disabled", "caret_initial"]

ACCEPTED = [
    {},
    {"animations": "allow"},
    {"caret": "hide"},
    {"mask": []},
    {"mask": [], "maskColor": "#00f"},
    {"maskColor": "#00f"},
    {"style": ""},
    {"omitBackground": False},
    {"omitBackground": True, "type": "jpeg"},
    {"animations": "allow", "caret": "hide", "mask": [], "style": "", "scale": "css"},
]
ACCEPTED_IDS = ["nothing", "animations_allow", "caret_hide", "empty_mask", "empty_mask_with_color", "color_alone", "empty_style",
                "omit_background_false", "omit_background_jpeg", "all_the_harmless_ones"]


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
@pytest.mark.parametrize("params,name", REFUSED, ids=REFUSED_IDS)
def test_a_parameter_that_would_modify_the_page_is_refused_by_name(kind, params, name):
    page = make_page()
    with pytest.raises(ProtocolException) as refused:
        kind(page, params)
    message = str(refused.value)
    assert "screenshot(%s" % name in message
    assert "not served" in message
    page.send.assert_not_called()


@pytest.mark.parametrize("params,name,instead", [
    ({"mask": MASK}, "mask", "Hide the elements yourself"),
    ({"style": "p { color: red }"}, "style", "add_style_tag"),
    ({"animations": "disabled"}, "animations", "document.getAnimations"),
    ({"caret": "initial"}, "caret", 'Leave caret out or pass "hide"'),
], ids=["mask", "style", "animations", "caret"])
def test_the_refusal_says_why_and_what_to_do_instead(params, name, instead):
    page = make_page()
    with pytest.raises(ProtocolException) as refused:
        page.op_screenshot(params)
    message = str(refused.value)
    if name != "caret":
        assert "modifying the page" in message
    assert instead in message


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
@pytest.mark.parametrize("params", ACCEPTED, ids=ACCEPTED_IDS)
def test_what_does_not_modify_the_page_is_taken(kind, params):
    page = make_page()
    assert kind(page, params) == {"binary": "AAAA"}
    (method, sent), _kw = page.send.call_args
    assert method == "Page.screenshot"


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
@pytest.mark.parametrize("params", [{"omitBackground": True}, {"omitBackground": True, "type": "png"}], ids=["default_type", "png"])
def test_omit_background_with_png_answers_firefoxs_own_error(kind, params):
    page = make_page()
    with pytest.raises(ProtocolException) as refused:
        kind(page, params)
    assert str(refused.value) == "Not implemented"
    page.send.assert_not_called()


def test_the_first_refusal_wins_and_nothing_is_sent_when_several_are_asked():
    page = make_page()
    with pytest.raises(ProtocolException):
        page.op_screenshot({"mask": MASK, "style": "p {}", "animations": "disabled", "caret": "initial", "omitBackground": True})
    page.send.assert_not_called()


# ── the refusal comes FIRST: a refused element's or locator's call neither scrolls nor measures the page ────────────────────
REFUSED_ELEMENT = REFUSED + [({"omitBackground": True}, "omitBackground")]
REFUSED_ELEMENT_IDS = REFUSED_IDS + ["omit_background_png"]


@pytest.mark.parametrize("params,name", REFUSED_ELEMENT, ids=REFUSED_ELEMENT_IDS)
def test_a_refused_element_screenshot_sends_nothing_to_the_engine_not_even_a_scroll_or_a_measure(params, name):
    page = make_page()
    handle = ElementHandleDispatcher(page.server, page.frame, "object-1")
    page.injected.bounding_box.return_value = {"x": 10, "y": 20, "width": 30, "height": 40}
    with pytest.raises(ProtocolException):
        handle.op_screenshot(params)
    # the scroll (`scroll_into_view`), the box (`bounding_box`) and the document's scroll (`evaluate`) all go through `injected`,
    # the picture through `send`: not one of them was called
    assert page.injected.mock_calls == []
    page.send.assert_not_called()


@pytest.mark.parametrize("params,name", REFUSED_ELEMENT, ids=REFUSED_ELEMENT_IDS)
def test_a_refused_page_screenshot_measures_nothing_either(params, name):
    page = make_page()
    with pytest.raises(ProtocolException):
        page.op_screenshot(params)
    assert page.injected.mock_calls == []
    page.send.assert_not_called()


def test_the_element_screenshot_that_is_taken_still_scrolls_and_measures_first():
    page = make_page()
    page.injected.bounding_box.return_value = {"x": 10, "y": 20, "width": 30, "height": 40}
    handle = ElementHandleDispatcher(page.server, page.frame, "object-1")
    handle.op_screenshot({})
    names = [call[0] for call in page.injected.mock_calls]
    assert names[:2] == ["scroll_into_view", "bounding_box"]
    assert page.send.call_args[0][0] == "Page.screenshot"
