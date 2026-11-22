"""What `page.evaluate(fn, arg)` hands the page is what Playwright hands it, in a real browser.

The unit file next to it (`test_every_argument_reaches_the_page_as_playwright_sends_it.py`) pins what the engine is asked; this one asks the page what it
saw. Until the server decoded the argument, a handle, a date, a URL, an error, a reference to an object already seen and `undefined` all reached the page
as `null`, and `0.0` too (the Python client sends `-0` for every float zero), with no error: the function ran on a wrong argument and answered.

Each expected value is what stock Playwright 1.61 gives the page, from the driver bundle's own functions: `parseEvaluationResultValue` (page side: `d` is a
Date, `u` a URL, `e` an Error with its name and message, `ref` the same object, `v: "-0"` is -0, `v: "NaN"` is NaN, `v: "undefined"` keeps the key of an
object) and, node side, `evaluateExpression` with `adoptIfNeeded` (an element of another frame is adopted, a handle of another context is refused with
`JSHandles can be evaluated only in the context they were created!`, a disposed one with `arg.handles[0]: no object with guid <guid>`).
"""
from __future__ import annotations

import datetime
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.e2e

PAGE = """<!doctype html><div id="outer" class="a">out</div><ul><li id="one">1</li><li id="two">2</li></ul>
<iframe id="f" srcdoc="&lt;!doctype html&gt;&lt;div id='inner'&gt;in&lt;/div&gt;"></iframe>"""

WHEN = datetime.datetime(2021, 7, 28, 20, 26, 32, 123000, tzinfo=datetime.timezone.utc)


@pytest.fixture(scope="module")
def browser(firefox_binary):
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=42, binary_path=firefox_binary, humanize=False, headless=True) as browser:
        yield browser


@pytest.fixture
def page(browser):
    page = browser.new_context().new_page()
    page.set_default_timeout(30000)
    page.set_content(PAGE)
    yield page
    page.context.close()


def test_a_date_arrives_as_a_date(page):
    # stock `parseEvaluationResultValue`: `d` -> new Date(value.d), and the node side keeps milliseconds
    assert page.evaluate("x => [x instanceof Date, x.toISOString()]", WHEN) == [True, "2021-07-28T20:26:32.123Z"]


def test_an_element_handle_arrives_as_the_element(page):
    # stock `evaluateExpression`: the handle is adopted into the context that runs the code and passed as itself
    outer = page.query_selector("#outer")
    assert page.evaluate("e => e.tagName", outer) == "DIV"


def test_a_handle_nested_in_an_object_and_an_array_arrives_as_the_element(page):
    one, two = page.query_selector("#one"), page.query_selector("#two")
    # stock: `h` inside `o` and `a` is `handles[i]` wherever it stands
    assert page.evaluate("o => [o.first.id, o.rest[0].id, o.k]", {"first": one, "rest": [two], "k": 3}) == ["one", "two", 3]


def test_a_handle_of_a_value_the_page_made_arrives_as_that_value(page):
    # a handle of the caller's own world (`evaluate_handle`) is passed as the object itself
    made = page.evaluate_handle("() => ({a: 1, b: [2]})")
    assert page.evaluate("o => [o.a, o.b[0]]", made) == [1, 2]
    assert page.evaluate("p => p.in.b[0]", {"in": made}) == 2


def test_a_primitive_handle_arrives_as_its_value(page):
    # stock `evaluateExpression`: a handle with no objectId falls through as `handle._value`
    number = page.evaluate_handle("() => 7")
    assert page.evaluate("x => x + 1", number) == 8


def test_a_float_zero_arrives_as_minus_zero_as_the_stock_client_sends_it(page):
    # the client's `serialize_value` tags every float zero `{v: "-0"}` (`value == float("-0")`), and stock's `parseEvaluationResultValue` makes it -0
    assert page.evaluate("x => Object.is(x, -0)", 0.0) is True
    assert page.evaluate("a => Object.is(a[0], -0)", [0.0]) is True
    assert page.evaluate("x => Object.is(x, 0)", 0) is True


@pytest.mark.parametrize("value, check", [
    (float("nan"), "x => Number.isNaN(x)"),
    (float("inf"), "x => x === Infinity"),
    (float("-inf"), "x => x === -Infinity"),
], ids=["nan", "infinity", "minus-infinity"])
def test_nan_and_the_infinities_arrive_as_themselves(page, value, check):
    # stock: `v: "NaN" | "Infinity" | "-Infinity"`; never a bare NaN in the JSON
    assert page.evaluate(check, value) is True
    assert page.evaluate("a => a.map(" + check + ")", [value]) == [True]


def test_an_undefined_inside_an_object_keeps_its_key(page):
    # stock: `v: "undefined"` is `undefined` and the key stays (the client sends a value it cannot name as undefined)
    got = page.evaluate("o => [Object.keys(o), o.u === undefined, 'u' in o]", {"a": 1, "u": object()})
    assert got == [["a", "u"], True, True]


def test_a_repeated_object_arrives_as_one_object(page):
    shared = {"k": 1}
    # stock: `ref` is `refs.get(value.ref)`, the same object
    assert page.evaluate("o => o.x === o.y", {"x": shared, "y": shared}) is True


def test_a_cycle_arrives_as_a_cycle(page):
    cycle = {"name": "c"}
    cycle["self"] = cycle
    assert page.evaluate("o => [o.self === o, o.self.self.name]", cycle) == [True, "c"]


def test_a_url_arrives_as_a_url(page):
    assert page.evaluate("u => [u instanceof URL, u.href]", urlparse("https://page.test/a?b=1")) == [True, "https://page.test/a?b=1"]


def test_an_exception_arrives_as_an_error_with_its_name_and_message(page):
    # stock: `e` -> new Error(m) with name n
    assert page.evaluate("e => [e instanceof Error, e.name, e.message]", ValueError("boom")) == [True, "ValueError", "boom"]


def test_a_handle_s_own_evaluate_takes_a_date(page):
    outer = page.query_selector("#outer")
    assert outer.evaluate("(el, d) => [el.id, d instanceof Date]", WHEN) == ["outer", True]


def test_eval_on_selector_takes_a_handle_and_a_date(page):
    two = page.query_selector("#two")
    assert page.eval_on_selector("#one", "(el, other) => el.id + other.id", two) == "onetwo"
    assert page.eval_on_selector("#one", "(el, d) => d instanceof Date", WHEN) is True


def test_eval_on_selector_all_takes_a_date(page):
    assert page.eval_on_selector_all("li", "(els, d) => [els.length, d.getTime()]", WHEN) == [2, 1627503992123]


def test_wait_for_function_takes_a_date(page):
    epoch = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    handle = page.wait_for_function("d => d instanceof Date && d.getTime() === 0", epoch)
    assert handle.json_value() is True


def test_wait_for_function_takes_a_handle(page):
    outer = page.query_selector("#outer")
    handle = page.wait_for_function("e => e.id === 'outer'", outer)
    assert handle.json_value() is True


def test_an_element_of_a_same_origin_iframe_is_adopted_into_the_main_frame(page):
    frame = page.query_selector("#f").content_frame()
    inner = frame.wait_for_selector("#inner")
    # stock: `adoptIfNeeded` -> `Page.adoptNode` into the context that runs the code
    assert page.evaluate("e => [e.id, e.textContent]", inner) == ["inner", "in"]
    assert frame.evaluate("e => e.id", page.query_selector("#outer")) == "outer"


def test_a_disposed_handle_is_refused_with_stock_s_text(page):
    from invisible_playwright.sync_api import Error

    outer = page.query_selector("#outer")
    outer.dispose()
    with pytest.raises(Error, match=r"arg\.handles\[0\]: no object with guid "):
        page.evaluate("e => e.id", outer)


def test_a_handle_of_another_page_is_refused_with_stock_s_text(page):
    from invisible_playwright.sync_api import Error

    made = page.evaluate_handle("() => ({a: 1})")
    other = page.context.new_page()
    with pytest.raises(Error, match="JSHandles can be evaluated only in the context they were created!"):
        other.evaluate("o => o.a", made)


def test_an_exposed_function_answering_a_date_hands_the_page_a_date(page):
    # stock: the result of a binding goes to the page through the same `parseEvaluationResultValue`
    page.expose_function("whenIsIt", lambda: WHEN)
    assert page.evaluate("async () => { const r = await window.whenIsIt(); return [r instanceof Date, r.toISOString()]; }") == [
        True, "2021-07-28T20:26:32.123Z"]
