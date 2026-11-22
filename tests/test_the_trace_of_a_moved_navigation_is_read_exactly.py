"""The reader of the engine's trace (`tests/_navigation_moved_trace.py`) that the e2e of a moved navigation judges with, on lines written by hand.

The e2e (`test_a_navigation_that_moved_is_told_by_the_engine_e2e.py`) cannot run without a firefox-69 engine; what it matches is decided here, with no browser.

Known-bad inputs: a trace with no request for the address (a dead trace proves nothing), no moved line, two, a moved line of another navigation, an origin in
the parent process or with no browsing context, and a docShell other than the one the load left telling its end.
"""
from __future__ import annotations

import json

import pytest
from _navigation_moved_trace import (
    assert_not_moved,
    moved_lines,
    moved_origin,
    navigation_ids_of,
    read_trace,
)

pytestmark = pytest.mark.unit

URL = "http://localhost:1/second?moved=b"


def _line(what, where="navigations", proc="parent", **detail):
    return {"t": 1.0, "pid": 10, "proc": proc, "where": where, "what": what, "detail": detail}


def _request(nav, url=URL):
    return _line("onDocumentRequest", frameId="mainframe-3", navigationId=nav, url=url)


def _moved(nav, bc=7, child=5, frame="mainframe-3"):
    return _line("onNavigationMoved", frameId=frame, navigationId=nav, origin={"browsingContextId": bc, "childId": child})


def test_the_origin_is_the_one_of_the_single_moved_line_of_the_navigation():
    lines = [_request("nav-1", "http://x/first"), _request("nav-2"), _moved("nav-1", 6, 4), _moved("nav-2", 7, 5)]
    assert moved_origin(lines, URL) == (7, 5)


def test_a_docshell_that_told_the_end_is_the_one_the_load_left():
    told = _line("onNavigationAborted", frameId="mainframe-3", navigationId="nav-2", errorText="NS_BINDING_ABORTED",
                 reporter={"browsingContextId": 7, "childId": 5})
    stateful = _line("onStateChange abort", where="frame-tree", proc="content", frameId="mainframe-3", navigationId="nav-2",
                     reporter={"browsingContextId": 7, "childId": 5})
    assert moved_origin([_request("nav-2"), _moved("nav-2"), told, stateful], URL) == (7, 5)


def test_a_dead_trace_proves_nothing():
    for check in (lambda: moved_origin([], URL), lambda: assert_not_moved([], URL)):
        with pytest.raises(AssertionError, match="no document request"):
            check()


def test_no_moved_line_or_two_or_one_of_another_navigation_is_red():
    for lines, count in (([_request("nav-2")], 0), ([_request("nav-2"), _moved("nav-2"), _moved("nav-2")], 2),
                         ([_request("nav-2"), _moved("nav-9")], 0)):
        with pytest.raises(AssertionError, match="%d onNavigationMoved line" % count):
            moved_origin(lines, URL)


def test_an_origin_in_the_parent_process_or_with_no_browsing_context_or_of_a_subframe_is_red():
    for bad in (_moved("nav-2", child=0), _moved("nav-2", bc=0), _moved("nav-2", frame="subframe-9")):
        with pytest.raises(AssertionError):
            moved_origin([_request("nav-2"), bad], URL)


def test_a_docshell_other_than_the_one_the_load_left_telling_its_end_is_red():
    told = _line("onNavigationAborted", frameId="mainframe-3", navigationId="nav-2", errorText="NS_BINDING_ABORTED",
                 reporter={"browsingContextId": 7, "childId": 6})
    with pytest.raises(AssertionError, match="other than the one the load left"):
        moved_origin([_request("nav-2"), _moved("nav-2"), told], URL)


def test_a_navigation_that_stayed_has_no_moved_line_and_one_that_moved_is_refused():
    assert_not_moved([_request("nav-2"), _moved("nav-9")], URL)
    with pytest.raises(AssertionError, match="told that"):
        assert_not_moved([_request("nav-2"), _moved("nav-2")], URL)


def test_the_reader_takes_every_file_and_leaves_out_a_line_not_whole(tmp_path):
    (tmp_path / "juggler-1-1.jsonl").write_text(json.dumps(_request("nav-2")) + "\n" + json.dumps(_moved("nav-2")) + "\n" + '{"t": 1, "what": "onNa',
                                                encoding="utf-8")
    (tmp_path / "juggler-2-2.jsonl").write_text(json.dumps(_request("nav-5", "http://x/other")) + "\n", encoding="utf-8")
    (tmp_path / "meta.json").write_text("{}", encoding="utf-8")
    lines = read_trace(tmp_path)
    assert len(lines) == 3 and navigation_ids_of(lines, URL) == {"nav-2"} and len(moved_lines(lines)) == 1
