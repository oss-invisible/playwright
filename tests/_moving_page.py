"""The `goto` of the tests of pages that move on their own (`test_a_page_that_moves_on_e2e.py`), and the model of where the frame goes."""
from __future__ import annotations


def _landing(url: str) -> str:
    """Where the page of `url` sends itself: /landed with the query of its round, which no other round of the module has."""
    return "/landed" + url[url.index("?"):]


#: The address each page (by `id`) was last sent to, or the page it went with instead (`_goto`).
_SENT: dict = {}


def _goto(page, url, *, the_page_before_may_move, our_page_may_stop_ours, **kwargs) -> str:
    """`page.goto`, and the address of the page the frame went with: `url` when `goto` answers.

    - ``the_page_before_may_move``: the page sent before ours may still be moving (the rounds of the read test go on
      while it does, module docstring). Its `location.replace` may win: `goto` says ours was interrupted by it, or
      stopped (NS_BINDING_ABORTED), and the frame lands on the landing of THAT page, whose address is returned.
    - ``our_page_may_stop_ours``: an engine before firefox-68, where `goto` may say NS_BINDING_ABORTED for our own
      document, which had committed and then moved
      (`test_networkidle_is_reached_only_on_the_page_that_replaced_a_document_still_arriving`); the frame then lands on
      our landing.

    Any other answer is red. The detector of a navigation that never answers is the timeout of the `goto` itself (each
    caller passes one): a bound of the test stricter than it only judged the speed of the machine."""
    from invisible_playwright.sync_api import Error

    before = _SENT.get(id(page)) if the_page_before_may_move else None
    _SENT[id(page)] = url
    try:
        page.goto(url, **kwargs)
        return url
    except Error as e:
        text = str(e)
        if "NS_BINDING_ABORTED" in text:
            ends = tuple(_landing(u) for u in (before, url if our_page_may_stop_ours else None) if u)
            assert ends, e
            page.wait_for_url(lambda u: u.endswith(ends), timeout=30_000)
            went = url if page.url.endswith(_landing(url)) else before
        else:
            assert before is not None, e
            assert "is interrupted by another navigation to" in text, e
            assert _landing(before) in text.split("another navigation to")[1], e
            went = before
        _SENT[id(page)] = went
        return went
