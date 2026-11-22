"""What the fake web's P2 case keeps when a round does not reach networkidle (no browser): the requests the served browser's lifecycle holds open, and their history on the
Juggler wire.

The lifecycle's open requests are read through the registry of the in-process server (`RemoteServer._endpoint.server._objects`, the `PageDispatcher`s, their
`lifecycle`), from a real `Lifecycle` driven by the events a page's engine sends. The history text (`p2-open-requests-history.txt`) is read from a wire log in the format of
`tests/_evidence_on_failure.py`. Known-bad input: a request whose resume was sent and never answered, whose frame was detached, and that no end ever came for: the text
must say all three, and must not list a request that ended.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from invisible_core.juggler.connection import EventListeners
from invisible_core.juggler.lifecycle import Lifecycle
from fakeweb import profile_p2

pytestmark = pytest.mark.unit


def _server_of(*lifecycles):
    objects = {"page@%d" % n: SimpleNamespace(TYPE="Page", guid="page@%d" % n, lifecycle=lc) for n, lc in enumerate(lifecycles)}
    objects["browser"] = SimpleNamespace(TYPE="Browser", guid="browser")
    return SimpleNamespace(_endpoint=SimpleNamespace(server=SimpleNamespace(_objects=objects)))


def _lifecycle():
    lifecycle = Lifecycle(EventListeners(), "session")
    lifecycle._on_event("Page.frameAttached", {"frameId": "main", "parentFrameId": None})
    lifecycle._on_event("Page.navigationCommitted", {"frameId": "main", "navigationId": "nav-1", "url": "http://a.test/p/x", "name": ""})
    lifecycle._on_event("Page.frameAttached", {"frameId": "sub", "parentFrameId": "main"})
    return lifecycle


def _sent(rid, frame, navigation=None, intercepted=True):
    return {"requestId": rid, "frameId": frame, "navigationId": navigation, "isIntercepted": intercepted, "method": "POST", "url": "http://o.test/beacon"}


def test_the_lifecycle_open_requests_are_read_with_their_owner_frame_and_navigation():
    lifecycle = _lifecycle()
    lifecycle._on_event("Network.requestWillBeSent", _sent("r-open", "sub", "nav-9"))
    lifecycle._on_event("Network.requestWillBeSent", _sent("r-done", "main"))
    lifecycle._on_event("Network.requestFinished", {"requestId": "r-done"})
    pages = profile_p2.lifecycle_open_requests(_server_of(lifecycle))
    assert len(pages) == 1 and pages[0]["lock_taken"] and pages[0]["main_frame"] == "main"
    held = {e["rid"]: e for e in pages[0]["open"]}
    assert list(held) == ["r-open"], held
    assert held["r-open"]["owner"] == "sub" and held["r-open"]["navigation"] == "nav-9" and held["r-open"]["owner_is_known"] is True
    assert held["r-open"]["owner_parent"] == "main"
    assert pages[0]["frame_tree"]["main"]["url"] == "http://a.test/p/x"


def _wire(*records):
    return [json.dumps({"t": t, "m": t - 1000, "dir": d, "len": len(msg), "cut": cut, "msg": msg}) for t, d, msg, cut in records]


def test_the_history_of_an_open_request_has_its_start_its_resume_without_a_reply_and_its_frame_events():
    sent = json.dumps({"method": "Network.requestWillBeSent", "params": _sent("r-open", "sub", "nav-9"), "sessionId": "s"})
    resume = json.dumps({"id": 7, "method": "Network.resumeInterceptedRequest", "params": {"requestId": "r-open"}, "sessionId": "s"})
    detached = json.dumps({"method": "Page.frameDetached", "params": {"frameId": "sub"}, "sessionId": "s"})
    other = json.dumps({"method": "Page.navigationCommitted", "params": {"frameId": "elsewhere", "url": "http://x/"}})
    done_sent = json.dumps({"method": "Network.requestWillBeSent", "params": _sent("r-done", "main")})
    done_end = json.dumps({"method": "Network.requestFinished", "params": {"requestId": "r-done"}})
    lines = _wire((1000.0, "in", sent, False), (1000.1, "out", resume, False), (1000.5, "in", detached, False), (1000.6, "in", other, False),
                  (1000.7, "in", done_sent, False), (1000.8, "in", done_end, False))
    lifecycle = _lifecycle()
    lifecycle._on_event("Network.requestWillBeSent", _sent("r-open", "sub", "nav-9"))
    cell = {"round": 3, "pattern": "leaving_beacons_subframe", "canary": False, "expired_at": {"t": 1011.0, "m": 11.0},
            "lifecycle_open": profile_p2.lifecycle_open_requests(_server_of(lifecycle))}
    text = profile_p2.open_requests_history(lines, [cell])
    assert "== request r-open" in text and "r-done" not in text
    assert "open at the expiry of round 3 leaving_beacons_subframe" in text and "owner frame sub" in text
    assert "requestWillBeSent frameId=sub isIntercepted=True navigationId=nav-9" in text
    assert "resumeInterceptedRequest sent, id=7 requestId=r-open" in text and "no reply to id=7 on the wire" in text
    assert "no requestFinished or requestFailed for it on the wire" in text
    assert "frameDetached" in text and "elsewhere" not in text


def test_a_reply_and_an_end_are_in_the_history_and_a_cut_message_is_read_from_its_fields():
    # longer than the wire log's limit: the log keeps its head, which is no JSON, with the fields that matter in it
    sent = json.dumps({"method": "Network.requestWillBeSent", "params": dict(_sent("r1", "main", "nav-1"), postData="x" * 6000)})[:4000]
    resume = json.dumps({"id": 4, "method": "Network.resumeInterceptedRequest", "params": {"requestId": "r1"}})
    reply = json.dumps({"id": 4, "result": {}})
    failed = json.dumps({"method": "Network.requestFailed", "params": {"requestId": "r1", "errorCode": "NS_BINDING_ABORTED"}})
    committed = json.dumps({"method": "Page.navigationCommitted", "params": {"frameId": "main", "navigationId": "nav-2", "url": "http://a.test/p/quiet"}})
    lines = _wire((50.0, "in", sent, True), (50.1, "out", resume, False), (50.2, "in", reply, False), (50.4, "in", failed, False), (50.5, "in", committed, False))
    cell = {"round": 1, "pattern": "quiet", "canary": True, "lifecycle_open": [{"open": [{"rid": "r1", "owner": "main", "navigation": "nav-1"}]}]}
    text = profile_p2.open_requests_history(lines, [cell])
    assert "requestWillBeSent frameId=main navigationId=nav-1" in text and "(cut)" in text
    assert "reply to id=4: ok" in text and "requestFailed errorCode=NS_BINDING_ABORTED" in text
    assert "no requestFinished or requestFailed" not in text
    assert "navigationCommitted navigationId=nav-2 url=http://a.test/p/quiet" in text


def test_a_run_with_nothing_open_says_so():
    assert "No request was held open" in profile_p2.open_requests_history([], [{"round": 1, "idle": True}])
    cell = {"round": 2, "lifecycle_open": [{"error": "RuntimeError()"}]}
    assert "No request was held open" in profile_p2.open_requests_history([], [cell])
