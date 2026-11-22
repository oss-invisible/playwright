"""`Page.elementAtom` answers for an element the way Marionette's atoms do: is it displayed, and what text it shows.

The engine's Juggler runs the source of Marionette's Selenium atoms
(`isElementDisplayed`, `getVisibleText`, from `remote/marionette/atom.sys.mjs`)
in a sandbox of its own over the page's window, with the element handed through
an Xray, so a page that replaced `getComputedStyle` or `getBoundingClientRect`
sees none of the calls. The sources come from the constant `ATOM_SOURCES` that
the engine's hook exports from `atom.sys.mjs` (folder `automation/element-atoms`
of the engine series, `remote/marionette/atom.sys.mjs`; consumer
`juggler/content/PageAgent.js` `_elementAtom`). If that export is missing,
`ATOM_SOURCES[atom]` throws and the command answers with an error.

**What is asserted.** On a visible and on a hidden element (`display: none`,
`visibility: hidden`, and one inside a hidden parent) the atom says what the
client API says for `is_visible`, and the text of the visible one is its
`inner_text`; the answers for the visible element and the hidden ones DIFFER
(the control is functional: not on/off, but one element against another, so an
atom that always answered the same could not pass).

The client API (`is_visible`, `inner_text`) does not use the command: it runs
Playwright's injected script. So the command is sent the way the engine's other
clients send it, on the in-process connection of the page's session, with the
frame and the object of an element handle.

Why a page cares: the atoms are how a client learns what a page shows without running
code in the page's realm, so a page that replaced `getComputedStyle` or
`getBoundingClientRect` does not see the lookups; this test holds that the atoms run
at all and answer for what the page shows.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

PAGE = """<!doctype html><html><body>
<p id="shown">hello visible world</p>
<p id="none" style="display:none">secret none</p>
<p id="invisible" style="visibility:hidden">secret invisible</p>
<div style="display:none"><p id="nested">secret nested</p></div>
</body></html>"""

HIDDEN = ("none", "invisible", "nested")


def _atom(page, selector, atom):
    """What `Page.elementAtom` answers for the element `selector` names, sent on the in-process connection of the
    page's session (the server runs in this process, as `test_injected_node_surface.py` uses it)."""
    handle = page.query_selector(selector)
    assert handle is not None, selector
    impl = handle._impl_obj
    server = impl._connection._transport._server
    element = server.object(impl._guid)
    injected = server.object(page._impl_obj._guid).injected
    answer = injected.c.send(
        "Page.elementAtom",
        {"frameId": element.frame.frame_id, "objectId": element.object_id, "atom": atom},
        session=injected.session, timeout=None)
    return answer["result"]


def test_the_atoms_run_in_a_sandbox_and_tell_a_visible_element_from_a_hidden_one(firefox_binary):
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=4242, binary_path=firefox_binary, headless=True) as browser:
        page = browser.new_context().new_page()
        page.set_content(PAGE)

        # what the client API says: the visible element is visible, the others are not
        assert page.is_visible("#shown") is True
        assert [page.is_visible("#" + name) for name in HIDDEN] == [False, False, False]
        assert page.inner_text("#shown") == "hello visible world"

        # what the atoms say, for the same elements
        assert _atom(page, "#shown", "isElementDisplayed") is True
        assert [_atom(page, "#" + name, "isElementDisplayed") for name in HIDDEN] == [False, False, False]

        shown_text = _atom(page, "#shown", "getVisibleText")
        assert shown_text == page.inner_text("#shown"), shown_text
        hidden_texts = [_atom(page, "#" + name, "getVisibleText") for name in HIDDEN]
        assert hidden_texts == ["", "", ""], hidden_texts

        # the control: one element against another gives different answers, whatever the atoms do with a page
        assert shown_text not in hidden_texts
