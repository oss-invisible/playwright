"""A screenshot parameter that Playwright honours by modifying the page is refused with a clear error, and a caret is not in our picture.

Stock Playwright takes `mask`, `style` and `animations="disabled"` by changing the page before the picture and putting it back after, hides the caret by default
(`caret-color: transparent`) and draws it for `caret="initial"`, and its Firefox throws `Not implemented` for `omitBackground` with png. This package never read those
parameters and answered a picture as if they had not been written. Here the real server is asked, through the client, on a page, a locator and an element handle:

* `mask` (with `maskColor`), `style`, `animations="disabled"` and `caret="initial"` raise an `Error` that names the parameter, and the page is left as it was (no `<style>`, no
  overlay, the next screenshot works);
* `omitBackground` with png raises `Not implemented`; with jpeg it takes the picture;
* `animations="allow"`, `caret="hide"`, an empty `mask` and an empty `style` take the picture;
* the caret: the client sends `caret` only when the caller writes it, so a call that does not write it takes the picture the way `caret="hide"` does. Measured: a focused `<input>` with
  `caret-color: red` photographed with `caret` absent and with `"hide"` has no red pixel in it; a red border on the same input is found by the same reading, so the reading is not blind.
"""
from __future__ import annotations

import pytest

import _page_bytes

pytestmark = pytest.mark.e2e

HTML = (
    "<!doctype html><html style='background:#fff'><body style='margin:20px;background:#fff'>"
    "<p id='a' style='margin:0;width:200px;height:40px;background:#0a0'>masked</p>"
    "<input id='i' value='' style='display:block;margin-top:20px;width:200px;height:40px;border:0;outline:0;background:#fff;color:#000;caret-color:#f00;font-size:24px'>"
    "<input id='b' value='' style='display:block;margin-top:20px;width:200px;height:40px;border:0;border-right:6px solid #f00;background:#fff;color:#000;font-size:24px'>"
    "</body></html>"
)

JPEG, PNG = b"\xff\xd8", b"\x89PNG"
_SET = [None]       # the document as the page serialises it right after set_content


@pytest.fixture(scope="module")
def page(firefox_binary):
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=4281, binary_path=firefox_binary, headless=True) as browser:
        context = browser.new_context(viewport={"width": 800, "height": 400})
        page = context.new_page()
        page.set_content(HTML, wait_until="load")
        _SET[0] = page.content()
        yield page
        context.close()


def _red_pixels(png: bytes) -> int:
    image = _page_bytes.unfiltered(png)
    w, channels, data = image["w"], image["channels"], image["data"]
    count = 0
    for at in range(0, len(data), channels):
        r, g, b = data[at], data[at + 1], data[at + 2]
        if r > 200 and g < 60 and b < 60:
            count += 1
    return count


def _the_page_is_as_it_was(page):
    """No `<style>` was injected, no overlay was added, the document is the one that was set, and the next screenshot works."""
    assert page.evaluate("document.querySelectorAll('style').length") == 0
    assert page.content() == _SET[0]
    assert page.screenshot()[:4] == PNG


def _calls(page):
    """Each way of asking: the page, a locator, an element handle. Every one is a function of the keyword arguments."""
    handle = page.query_selector("#a")
    return {
        "page": lambda **k: page.screenshot(**k),
        "locator": lambda **k: page.locator("#a").screenshot(**k),
        "handle": lambda **k: handle.screenshot(**k),
    }


WAYS = ("page", "locator", "handle")

# The first line of stock Playwright's error for `omitBackground` with png on Firefox, read from Playwright 1.61.1's own code: the server's
# `FFPage.setBackgroundColor` throws `new Error("Not implemented")` (driver `coreBundle.js`, called when `options.omitBackground && format === "png"`),
# and the Python client prefixes the API name (`playwright/_impl/_connection.py`: `rewrite_error(error, f"{apiName}: {error}")`).
NOT_IMPLEMENTED = {
    "page": "Page.screenshot: Not implemented",
    "locator": "Locator.screenshot: Not implemented",
    "handle": "ElementHandle.screenshot: Not implemented",
}


@pytest.mark.parametrize("way", WAYS)
def test_a_mask_is_refused_by_name_and_the_page_is_untouched(page, way):
    from invisible_playwright.sync_api import Error

    shoot = _calls(page)[way]
    with pytest.raises(Error, match=r"screenshot\(mask=") as refused:
        shoot(mask=[page.locator("#a")])
    assert "maskColor" in str(refused.value)
    with pytest.raises(Error, match=r"screenshot\(mask="):
        shoot(mask=[page.locator("#a")], mask_color="#00f")
    _the_page_is_as_it_was(page)


@pytest.mark.parametrize("way", WAYS)
def test_a_style_is_refused_by_name_and_the_page_is_untouched(page, way):
    from invisible_playwright.sync_api import Error

    with pytest.raises(Error, match=r"screenshot\(style=.*add_style_tag"):
        _calls(page)[way](style="p { visibility: hidden }")
    _the_page_is_as_it_was(page)


@pytest.mark.parametrize("way", WAYS)
def test_animations_disabled_is_refused_by_name_and_the_page_is_untouched(page, way):
    from invisible_playwright.sync_api import Error

    with pytest.raises(Error, match=r'screenshot\(animations="disabled"\)'):
        _calls(page)[way](animations="disabled")
    _the_page_is_as_it_was(page)


@pytest.mark.parametrize("way", WAYS)
def test_a_caret_that_is_asked_to_be_drawn_is_refused_by_name(page, way):
    from invisible_playwright.sync_api import Error

    with pytest.raises(Error, match=r'screenshot\(caret="initial"\)'):
        _calls(page)[way](caret="initial")
    _the_page_is_as_it_was(page)


@pytest.mark.parametrize("way", WAYS)
def test_omit_background_with_png_is_firefoxs_not_implemented(page, way):
    from invisible_playwright.sync_api import Error

    with pytest.raises(Error) as refused:
        _calls(page)[way](omit_background=True)
    assert str(refused.value).split("\n")[0] == NOT_IMPLEMENTED[way]
    with pytest.raises(Error) as refused:
        _calls(page)[way](omit_background=True, type="png")
    assert str(refused.value).split("\n")[0] == NOT_IMPLEMENTED[way]


@pytest.mark.parametrize("way", WAYS)
def test_omit_background_with_jpeg_takes_the_picture(page, way):
    assert _calls(page)[way](omit_background=True, type="jpeg")[:2] == JPEG


@pytest.mark.parametrize("way", WAYS)
def test_what_does_not_modify_the_page_takes_the_picture(page, way):
    shoot = _calls(page)[way]
    assert shoot(animations="allow")[:4] == PNG
    assert shoot(caret="hide")[:4] == PNG
    assert shoot(mask=[])[:4] == PNG
    assert shoot(style="")[:4] == PNG
    assert shoot()[:4] == PNG


def _caret_shots(page, **kwargs):
    page.locator("#i").focus()
    assert page.evaluate("document.activeElement.id") == "i"
    return [page.locator("#i").screenshot(**kwargs) for _ in range(3)]


def test_the_reading_of_red_is_not_blind(page):
    """The control: a red border on an input of the same size is found by the same reading, so `0` in the two tests below is a measurement."""
    assert _red_pixels(page.locator("#b").screenshot()) > 100


def test_no_caret_is_in_the_picture_when_the_caller_does_not_write_caret(page):
    assert [_red_pixels(png) for png in _caret_shots(page)] == [0, 0, 0]


def test_no_caret_is_in_the_picture_with_caret_hide(page):
    assert [_red_pixels(png) for png in _caret_shots(page, caret="hide")] == [0, 0, 0]


# ── a refused call does not touch the page's scroll: the refusal comes before the element is scrolled into view ────────────────
TALL = (
    "<!doctype html><html style='background:#fff'><body style='margin:0;background:#fff'>"
    "<div style='height:3000px;background:#eee'>above the fold</div>"
    "<p id='below' style='margin:0;width:200px;height:40px;background:#0a0'>below the fold</p>"
    "<div style='height:3000px;background:#eee'>after</div>"
    "</body></html>"
)
SCROLLED_TO = 100


@pytest.fixture
def scrolled(page):
    """A page scrolled to a known position with the element well below the fold."""
    other = page.context.new_page()
    other.set_content(TALL, wait_until="load")
    other.evaluate("window.scrollTo(0, %d)" % SCROLLED_TO)
    assert other.evaluate("window.scrollY") == SCROLLED_TO
    assert other.evaluate("document.getElementById('below').getBoundingClientRect().top") > 400
    yield other
    other.close()


REFUSALS = [
    ("mask", r"screenshot\(mask=", lambda p: {"mask": [p.locator("#below")]}),
    ("style", r"screenshot\(style=", lambda p: {"style": "p { visibility: hidden }"}),
    ("animations", r'screenshot\(animations="disabled"\)', lambda p: {"animations": "disabled"}),
    ("caret", r'screenshot\(caret="initial"\)', lambda p: {"caret": "initial"}),
    ("omit_background", None, lambda p: {"omit_background": True}),
]


@pytest.mark.parametrize("name,pattern,kwargs", REFUSALS, ids=[r[0] for r in REFUSALS])
@pytest.mark.parametrize("way", ["locator", "handle"])
def test_a_refused_element_screenshot_does_not_scroll_the_page(scrolled, way, name, pattern, kwargs):
    from invisible_playwright.sync_api import Error

    shoot = {
        "locator": lambda **k: scrolled.locator("#below").screenshot(**k),
        "handle": lambda **k: scrolled.query_selector("#below").screenshot(**k),
    }[way]
    with pytest.raises(Error, match=pattern) as refused:
        shoot(**kwargs(scrolled))
    if pattern is None:
        assert str(refused.value).split("\n")[0] == NOT_IMPLEMENTED[way]
    assert scrolled.evaluate("window.scrollY") == SCROLLED_TO
    # the control: the same call without the refused parameter DOES scroll the element in, so the unchanged scroll above is a measurement
    assert shoot()[:4] == PNG
    assert scrolled.evaluate("window.scrollY") != SCROLLED_TO
