"""The size and pointer media queries answer the declaration, not the host.

What it fixes, from the page's side (`matchMedia`):

  * `(device-width: Wpx)` / `(device-height: Hpx)` are the declared screen (`zoom.stealth.screen.width/height`), not the
    host's monitor.
  * `(pointer: ...)` / `(any-pointer: ...)` / `(hover: ...)` are the declared pointer capabilities
    (`zoom.stealth.pointer.primary` / `.all`, the bitmask Coarse 1, Fine 2, Hover 4), not the host's (a Windows machine
    with a digitizer adds Coarse by itself).
  * A context made with `has_touch=True` (Juggler's touch override) still reads a COARSE pointer when a fine one is
    declared: the override of the context decides before the declaration does.

Engine hook: `fingerprint/screen-window/css-media` of the engine series (`layout/style/nsMediaFeatures.cpp`:
`GetDeviceSize`, `GetPointerCapabilities`; the declarations in `StealthMediaFeatures.h`).

Negative controls, in the same test: the device size is read on a launch declared with ANOTHER screen (2623x1411): the
wrapper hands the declared screen to the browser by two roads (the screen prefs and the screen of the context, which Juggler
emulates), so a launch with the prefs at 0 still reads the pinned screen; the queries must follow each declaration. The
pointer is read with the engine off (`hw_seed` 0, stock Firefox: an active engine refuses a declaration cleared with -1), the
host's answer; and has_touch is read in a context without it, on the same launch.

Linux and Windows run the same assertions: the declared values are the ones no runner has (a 2731x1537 screen; a coarse
primary pointer on a machine with a mouse).
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

#: A screen no runner has.
SCREEN = {"screen.width": 2731, "screen.height": 1537, "screen.dpr": 1.0}

#: Another screen no runner has: the control of the device size is a launch declared with this one.
OTHER_SCREEN = {"screen.width": 2623, "screen.height": 1411, "screen.dpr": 1.0}

#: The engine off (stock Firefox): the control of the pointer. The engine refuses an active launch with a declaration cleared.
ENGINE_OFF = {"zoom.stealth.fpp.hw_seed": 0}

COARSE, FINE, HOVER = 1, 2, 4

QUERIES = {name: "matchMedia('%s').matches" % query for name, query in {
    "pointer_coarse": "(pointer: coarse)", "pointer_fine": "(pointer: fine)",
    "any_coarse": "(any-pointer: coarse)", "any_fine": "(any-pointer: fine)",
    "hover_none": "(hover: none)", "any_hover_none": "(any-hover: none)"}.items()}

READ_POINTER = "() => ({%s})" % ", ".join("%s: %s" % (k, v) for k, v in QUERIES.items())


def _launch(binary, pin=None, extra_prefs=None):
    from invisible_playwright import InvisiblePlaywright

    return InvisiblePlaywright(seed=42, pin=pin, binary_path=binary, headless=True, humanize=False,
                               extra_prefs=extra_prefs)


def _read_pointer(binary, primary=None, every=None):
    """Declared pointer prefs, or (both None) the engine off."""
    prefs = ENGINE_OFF if primary is None else {"zoom.stealth.pointer.primary": primary, "zoom.stealth.pointer.all": every}
    with _launch(binary, extra_prefs=prefs) as browser:
        page = browser.new_page()
        page.goto("about:blank", timeout=30000)
        return page.evaluate(READ_POINTER)


def test_a_declared_pointer_is_what_the_media_queries_read(firefox_binary):
    declared = _read_pointer(firefox_binary, COARSE, COARSE)
    assert declared == {"pointer_coarse": True, "pointer_fine": False, "any_coarse": True, "any_fine": False,
                        "hover_none": True, "any_hover_none": True}, (
        "a coarse pointer without hover is declared and the page reads %r" % declared)

    # The control: the engine off is not that machine. A fine pointer with hover is the declaration of a desktop; the host
    # with no declaration answers its own, and it is not a coarse one without hover.
    undeclared = _read_pointer(firefox_binary)
    assert undeclared != declared, "with no pointer declared the page reads the same as with a coarse one: %r" % undeclared
    assert undeclared["pointer_coarse"] is False and undeclared["any_coarse"] is False, (
        "with no pointer declared the page reads %r" % undeclared)

    fine = _read_pointer(firefox_binary, FINE | HOVER, FINE | HOVER)
    assert fine == {"pointer_coarse": False, "pointer_fine": True, "any_coarse": False, "any_fine": True,
                    "hover_none": False, "any_hover_none": False}, (
        "a fine pointer with hover is declared and the page reads %r" % fine)


def test_has_touch_reads_a_coarse_pointer_even_when_a_fine_one_is_declared(firefox_binary):
    with _launch(firefox_binary, extra_prefs={"zoom.stealth.pointer.primary": FINE | HOVER,
                                              "zoom.stealth.pointer.all": FINE | HOVER}) as browser:
        plain = browser.new_context().new_page()
        plain.goto("about:blank", timeout=30000)
        touch = browser.new_context(has_touch=True).new_page()
        touch.goto("about:blank", timeout=30000)
        without = plain.evaluate(READ_POINTER)
        with_touch = touch.evaluate(READ_POINTER)
    # the control: the same launch, a context without the override, reads the declared fine pointer
    assert without["pointer_fine"] is True and without["pointer_coarse"] is False, (
        "a context without has_touch reads %r under a declared fine pointer" % without)
    assert with_touch["pointer_coarse"] is True and with_touch["pointer_fine"] is False, (
        "a context with has_touch reads %r under a declared fine pointer: the touch override must win" % with_touch)
    assert with_touch["any_coarse"] is True, "any-pointer in a has_touch context reads %r" % with_touch


def _read_device_size(binary, pin, ask):
    """The page of a launch pinned to ``pin``, asked for the device size ``ask`` (width, height) and for its own."""
    with _launch(binary, pin=pin) as browser:
        page = browser.new_page()
        page.goto("about:blank", timeout=30000)
        return page.evaluate("""([w, h]) => ({
            screen_w: screen.width, screen_h: screen.height,
            device_width: matchMedia('(device-width: ' + w + 'px)').matches,
            device_height: matchMedia('(device-height: ' + h + 'px)').matches,
            own_width: matchMedia('(device-width: ' + screen.width + 'px)').matches,
            own_height: matchMedia('(device-height: ' + screen.height + 'px)').matches})""", list(ask))


def test_the_device_size_media_queries_read_the_declared_screen(firefox_binary):
    first = (SCREEN["screen.width"], SCREEN["screen.height"])
    second = (OTHER_SCREEN["screen.width"], OTHER_SCREEN["screen.height"])

    declared = _read_device_size(firefox_binary, SCREEN, first)
    assert declared["device_width"] is True and declared["device_height"] is True, (
        "(device-width: 2731px) and (device-height: 1537px) do not match under a declared 2731x1537 screen: %r" % declared)
    assert (declared["screen_w"], declared["screen_h"]) == first
    assert declared["own_width"] is True and declared["own_height"] is True, (
        "the device size the media queries read is not the screen the page reads: %r" % declared)

    # The control: the wrapper hands the declared screen to the browser by two roads (the prefs, and the screen of the context
    # that Juggler emulates), so the absent declaration is not a control. A launch with ANOTHER declared screen is: the same
    # query for the first size does not match there, and the queries follow that declaration.
    other = _read_device_size(firefox_binary, OTHER_SCREEN, first)
    assert (other["screen_w"], other["screen_h"]) == second, other
    assert other["device_width"] is False and other["device_height"] is False, (
        "(device-width: 2731px) matches under a declared %dx%d screen: the media queries do not follow the declaration, %r"
        % (second + (other,)))
    assert other["own_width"] is True and other["own_height"] is True, (
        "the device size the media queries read is not the screen the page reads: %r" % other)
    assert second != first and other != declared
