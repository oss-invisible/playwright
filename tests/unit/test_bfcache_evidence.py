"""The back-forward-cache test writes down what it knows BEFORE it asserts, and the lab holds the host's two inputs of the cache still. No browser.

A red of tests/test_a_worker_comes_back_with_its_page_from_the_cache_e2e.py used to be the single sentence "the page was loaded again instead of
restored from the back-forward cache" (hunt of 2026-11-12, one run of 15, [top-other-site-goto]) and nothing else was left of the run, so nobody could say whether the
engine had announced a restore, or Firefox had refused or evicted the page. These tests hold what that red now carries, and that the lab no
longer lets the memory of the machine decide whether the cache holds.
"""
import _bfcache_evidence as bfc
import pytest

pytestmark = pytest.mark.unit

RESTORED = [("Page.navigationStarted", {"navigationId": "nav-bfcache-1"}),
            ("Page.navigationCommitted", {"navigationId": "nav-bfcache-1", "url": "http://a.test:1/top-worker"}),
            ("Runtime.executionContextsCleared", {}),
            ("Page.frameDocumentRestored", {"frameId": "f1"}),
            ("Page.workerCreated", {"workerId": "w1"}),
            ("Page.eventFired", {"name": "pageshow"})]
LOADED_AGAIN = [("Page.navigationStarted", {"navigationId": "nav-7"}),
                ("Page.navigationCommitted", {"navigationId": "nav-7", "url": "http://a.test:1/top-worker"}),
                ("Runtime.executionContextsCleared", {}),
                ("Page.eventFired", {"name": "load"}),
                ("Page.workerCreated", {"workerId": "w2"})]


class _Page:
    def __init__(self, shown, entry="back_forward", state="complete"):
        self._answers = [shown, entry, state]

    def evaluate(self, expression):
        return self._answers.pop(0)


def test_the_evidence_is_read_from_the_page_and_the_events_since_go_back():
    prefix = [("Page.workerCreated", {"workerId": "old"})]            # before go_back: not part of the evidence
    ev = bfc.read_after_go_back(_Page([False, True]), prefix + RESTORED, len(prefix))
    assert ev.restored and ev.engine_announced_a_restore
    assert ev.navigation_type == "back_forward" and ev.ready_state == "complete"
    assert ev.events == ["Page.navigationStarted nav-bfcache-1",
                         "Page.navigationCommitted nav-bfcache-1 http://a.test:1/top-worker",
                         "Runtime.executionContextsCleared", "Page.frameDocumentRestored f1", "Page.workerCreated w1",
                         "Page.eventFired pageshow"]


def test_a_red_writes_the_evidence_down_before_it_asserts_and_the_message_carries_it():
    written = []
    ev = bfc.read_after_go_back(_Page([False]), LOADED_AGAIN, 0)
    with pytest.raises(AssertionError) as red:
        bfc.decide_outcome(ev, False, written.append)
    assert len(written) == 1, "the evidence was not written before the assertion"
    assert "__shown=[False]" in written[0] and "the engine announced a restore: no" in written[0] and "outcome: failed" in written[0]
    assert "Page.eventFired load" in written[0] and "nav-7" in written[0]
    assert "built again instead of restored" in str(red.value) and ev.text() in str(red.value) and written[0].startswith(ev.text())


def test_a_restore_the_engine_announced_but_the_page_did_not_see_is_a_defect_even_where_a_reload_is_accepted():
    ev = bfc.read_after_go_back(_Page([False]), RESTORED, 0)
    assert ev.engine_announced_a_restore and not ev.restored
    for may_reload in (False, True):
        with pytest.raises(AssertionError) as red:
            bfc.decide_outcome(ev, may_reload, lambda _line: None)
        assert "the engine announced a restore" in str(red.value) and "pageshow with persisted" in str(red.value)


def test_a_restored_page_is_said_restored_and_written_down_too():
    written = []
    assert bfc.decide_outcome(bfc.read_after_go_back(_Page([False, True]), RESTORED, 0), False, written.append) == "restored"
    assert len(written) == 1 and "__shown=[False, True]" in written[0] and "outcome: restored" in written[0]
    assert bfc.decide_outcome(bfc.read_after_go_back(_Page([False, True]), RESTORED, 0), True, lambda _l: None) == "restored"


def test_a_document_built_again_is_said_reloaded_only_where_firefox_may_refuse_the_cache():
    written = []
    ev = bfc.read_after_go_back(_Page([False]), LOADED_AGAIN, 0)
    assert bfc.decide_outcome(ev, True, written.append) == "reloaded"
    assert "outcome: reloaded" in written[0]
    with pytest.raises(AssertionError):
        bfc.decide_outcome(ev, False, lambda _l: None)


def test_the_lab_holds_the_two_host_inputs_of_the_cache_still():
    import test_a_worker_comes_back_with_its_page_from_the_cache_e2e as e2e

    prefs = e2e.LAB_PREFS
    assert prefs["browser.sessionhistory.max_total_viewers"] == 8
    assert prefs["browser.low_commit_space_threshold_percent"] == 0 and prefs["browser.low_commit_space_threshold_mb"] == 0
    assert all(prefs[k] == v for k, v in e2e.STRATEGY_1.items()), "the lab's prefs are the strategy's plus the two"


def test_the_test_has_no_repeat_no_skip_and_asks_for_the_evidence_and_the_cacheable_page():
    import pathlib

    source = (pathlib.Path(__file__).resolve().parent.parent / "test_a_worker_comes_back_with_its_page_from_the_cache_e2e.py").read_text("utf-8")
    for word in ("ATTEMPTS", "REFUSALS", "_NotRestored", "pytest.skip", "pytest.xfail", "reruns", "flaky"):
        assert word not in source, "%s: a refusal of the cache is a red with its evidence, never a repeat or a skip" % word
    assert "extra_prefs=LAB_PREFS" in source
    assert source.index("wait_cacheable(page)") < source.index("page.go_back("), "the page must be cacheable before the test leaves it"
    assert source.index("decide_outcome(") < source.index("Page.workerCreated", source.index("decide_outcome(")), \
        "the evidence is read and written before anything is asserted about the worker"
    assert 'may_reload=(how == "goto")' in source, "a reload is accepted for a navigation the parent starts (goto) and for nothing else"


def test_the_commit_order_of_a_worker_is_asserted_only_on_an_engine_that_promises_it(monkeypatch):
    import types

    import invisible_core.seal as seal
    import test_a_worker_comes_back_with_its_page_from_the_cache_e2e as e2e

    for tag, promised in (("firefox-62", False), ("firefox-63-candidate", True), ("firefox-64", True), ("local", False)):
        monkeypatch.setattr(seal, "active_seal", lambda tag=tag: types.SimpleNamespace(tag=tag))
        assert e2e._commit_order_is_promised() is promised, tag


FRAME_REPORTS = [{"host": "b.test", "path": "/report/pageshow", "query": {"persisted": ["false"], "top": ["false"]}},
                 {"host": "a.test", "path": "/report/loaded", "query": {}},
                 {"host": "b.test", "path": "/report/pageshow", "query": {"persisted": ["false"], "top": ["false"]}}]


def test_the_evidence_of_a_frame_is_written_without_asserting_anything_and_carries_the_reports_and_the_frames():
    written = []
    ev = bfc.read_after_go_back(_Page(None), LOADED_AGAIN, 0)           # the top of this page records no __shown
    text = bfc.write_evidence(ev, "frame pageshow persisted NOT seen", bfc.frame_evidence_lines(FRAME_REPORTS, ["http://a.test:1/top-shown", "http://b.test:1/shown"]),
                              written.append)
    assert written == [text], "written once, and the text returned is the text written"
    assert text.startswith(ev.text() + " => outcome: frame pageshow persisted NOT seen\n")
    assert "__shown=None" in text and "the engine announced a restore: no" in text
    assert "['b.test persisted=false top=false', 'b.test persisted=false top=false']" in text, "only the pageshow reports, in order"
    assert "report/loaded" not in text and "['http://a.test:1/top-shown', 'http://b.test:1/shown']" in text


def test_the_evidence_with_nothing_to_add_is_the_line_decide_outcome_always_wrote():
    ev = bfc.read_after_go_back(_Page([False, True]), RESTORED, 0)
    written = []
    assert bfc.write_evidence(ev, write=written.append) == ev.text() == written[0]


def test_the_frame_test_writes_its_evidence_before_it_asserts_and_keeps_its_verdict():
    import pathlib

    source = (pathlib.Path(__file__).resolve().parent.parent / "test_fission_strategy1_bench.py").read_text("utf-8")
    body = source[source.index("def test_oop_frame_comes_out_of_the_back_forward_cache_with_the_top"):source.index("def _evidence_after_go_back")]
    assert body.index("_evidence_after_go_back(") < body.index("assert shown_again"), "the evidence is written before the assertion"
    assert 'r["query"]["persisted"] == ["true"]' in body and 'r["query"]["top"] == ["false"]' in body, "the verdict is still the persisted frame pageshow"
    assert body.index("back = len(protocol_events)") < body.index("page.go_back("), "the events are counted before go_back"
    assert "@pytest.mark.e2e" in source and "sleep" not in body and "timeout" not in body
