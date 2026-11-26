"""The TURN deallocation is evidence of the network oracle, never a compared leaf (scripts/ci_ab_network.py, tests/_turn_chain.py), no browser.

The engine ends an allocation with ONE Refresh whose LIFETIME is 0, best effort, while it shuts the TURN client down (nICEr ``nr_turn_client_deallocate``: "we are only sending a single
request here because we are in the process of shutting everything down"). It reaches the lab in some walks and not in others for the same engine (the A/A of PR 611: two seeds whose
``webrtc/turn/messages`` differed only by ``0x0004``). So it is recognised by what it MEANS (a Refresh, 0x0004, with LIFETIME, 0x000D, equal to 0), by the lab's TURN server, and the walk
leaves it out of the messages and of the transactions the gateway counted for the TURN address; it goes to the evidence. More than one for one allocation, or one for an address that has
no allocation, is a problem of the run. A Refresh that keeps the allocation alive (a lifetime above 0) is a message of the walk like any other.

The walks below are the real ``walk_webrtc`` on a real lab: a fake browser whose page sends the datagrams a TURN client sends to the lab's TURN server, over a socket on loopback, and
whose gateway events are written as the gateway writes them.
"""
from __future__ import annotations

import json
import socket
import struct
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _turn_chain  # noqa: E402
import ci_ab_network as ab  # noqa: E402

pytestmark = pytest.mark.unit

ALLOCATE, REFRESH_A, REFRESH_B, REFRESH_C = (bytes([n]) * 12 for n in (1, 2, 3, 4))
RELAY_CANDIDATE = "candidate:1 1 udp 100 192.0.2.1 5000 typ relay"
RULE = "Rule: the TURN deallocation is one best-effort send at teardown (nICEr nr_turn_client_deallocate)"


def signed_refresh(tid, lifetime, user=b"lab"):
    """A Refresh as a TURN client sends it after the 401: USERNAME and LIFETIME, signed with the long-term key."""
    body = _turn_chain._attr(0x0006, user) + _turn_chain._attr(0x000D, struct.pack("!I", lifetime))
    return _turn_chain._message(0x0004, tid, body, _turn_chain.long_term_key(user.decode()))


def signed_allocate(tid, user=b"lab"):
    return _turn_chain._message(0x0003, tid, _turn_chain._attr(0x0006, user), _turn_chain.long_term_key(user.decode()))


def wait_for(condition, what):
    for _ in range(200):
        if condition():
            return
        time.sleep(0.025)
    raise AssertionError("never happened: " + what)


class Client:
    """A TURN client's socket (one 5-tuple) that talks to the lab's TURN server and tells the recorder what the gateway would have written."""

    def __init__(self, lab):
        self.lab = lab
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(5)
        self.sock.bind(("127.0.0.1", 0))

    def send(self, datagram, message):
        before = len(self.lab.turn.requests)
        self.lab.gateway._route(_turn_chain.TURN_IP, _turn_chain.TURN_PORT, 1, datagram, 1, None)     # the gateway writes the event, as it does for a browser's datagram
        self.sock.sendto(datagram, self.lab.turn.address)
        wait_for(lambda: len(self.lab.turn.requests) > before, "the lab TURN heard the datagram")
        self.sock.recvfrom(2048)                                  # every request here is answered
        return self

    def close(self):
        self.sock.close()


def walk(label, script, times=None):
    """The WebRTC walk of seed 4 on a lab, as ``run`` makes it, whose page does ``script(lab, clients)`` (the datagrams during the page, and at the close of the context, as the engine
    ends its allocations when the page closes), and the transcript of that seed."""
    clients = []

    class Page:
        def goto(self, *args, **kwargs):
            pass

        def evaluate(self, js, arg):
            script(lab, clients, "page")
            result = {"candidates": [RELAY_CANDIDATE], "errors": [], "sdp": "v=0", "gathering": "complete"}
            return result if times is None else dict(result, times=times)

    class Browser:
        def new_context(self, **kwargs):
            return types.SimpleNamespace(new_page=lambda: Page(), close=lambda: script(lab, clients, "close"))

    try:
        with ab.Lab(host="127.0.0.1") as lab:
            out = ab.walk_webrtc(Browser(), lab)
            evidence = ab.webrtc_evidence(lab)
    finally:
        for client in clients:
            client.close()
    transcript = {"format": ab.FORMAT, "label": label, "platform": "linux", "seeds": {"4": {"webrtc": out}}}
    return transcript, evidence


def allocation_then(*closing):
    """A script: the allocation during the page, and ``closing`` (pairs of tid and lifetime sent from the allocation's own socket) when the context closes."""
    def script(lab, clients, stage):
        if stage == "page":
            clients.append(Client(lab).send(_turn_chain._message(0x0003, ALLOCATE, b""), "stun-0x0003"))
            clients[0].send(signed_allocate(ALLOCATE), "stun-0x0003")
        else:
            for tid, lifetime in closing:
                clients[0].send(signed_refresh(tid, lifetime), "stun-0x0004")
    return script


def test_a_deallocation_seen_on_one_side_and_not_on_the_other_is_no_difference_of_the_walk():
    with_it, evidence_with = walk("a", allocation_then((REFRESH_A, 0)))
    without_it, evidence_without = walk("b", allocation_then())
    for transcript in (with_it, without_it):
        turn = transcript["seeds"]["4"]["webrtc"]["turn"]
        assert turn["messages"] == ["0x0003"], "the deallocation is not a message of the walk"
        assert transcript["seeds"]["4"]["webrtc"]["udp"]["counts"] == {ab.TURN_TARGET: 1}, "nor a transaction of the TURN address (the Allocate was one: its 401 and its retry share the id)"
        assert transcript["seeds"]["4"]["webrtc"]["udp"]["messages"] == {ab.TURN_TARGET: ["stun-0x0003"]}
    report = ab.compare_transcripts(with_it, without_it, [], 0, "a", "b")
    assert report["differences"] == [] and report["problems"] == [] and report["verdict"] == "green", report["problems"]
    assert evidence_with["deallocation"]["seen"] is True and evidence_without["deallocation"] == {"seen": False, "records": []}


def test_a_refresh_with_a_lifetime_above_zero_stays_in_the_messages_and_in_the_count():
    kept, evidence = walk("a", allocation_then((REFRESH_A, 600)))
    walk_of = kept["seeds"]["4"]["webrtc"]
    assert walk_of["turn"]["messages"] == ["0x0003", "0x0004"] and walk_of["udp"]["counts"] == {ab.TURN_TARGET: 2}
    assert walk_of["udp"]["messages"] == {ab.TURN_TARGET: ["stun-0x0003", "stun-0x0004"]}
    assert evidence["deallocation"] == {"seen": False, "records": []}
    assert walk_of["turn"]["deallocation"] == {"repeated": 0, "unallocated": 0, "early": 0}
    gone, _ = walk("b", allocation_then())
    differing = [d["path"] for d in ab.compare_transcripts(kept, gone, [], 0, "a", "b")["differences"]]
    assert "seeds/4/webrtc/turn/messages/1" in differing and "seeds/4/webrtc/udp/counts/%s" % ab.TURN_TARGET in differing, "a keep-alive IS the engine's doing, and is compared"


def test_the_deallocation_goes_to_the_evidence_with_its_time_and_its_transaction_id():
    transcript, evidence = walk("a", allocation_then((REFRESH_A, 0)))
    json.dumps(evidence)
    (record,) = evidence["deallocation"]["records"]
    assert evidence["deallocation"]["seen"] is True
    assert record["tid"] == REFRESH_A.hex() and record["allocation"] == 0 and record["src"].startswith("127.0.0.1:")
    assert isinstance(record["t"], float) and record["t"] >= evidence["phase_t"]["webrtc"], "the time is the recorder's, the one of the other evidence"
    assert [d["tid"] for d in evidence["datagrams"]].count(REFRESH_A.hex()) == 1, "the datagram itself is still in the evidence, as every datagram is"
    assert transcript["seeds"]["4"]["webrtc"]["turn"]["deallocation"] == {"repeated": 0, "unallocated": 0, "early": 0}


def test_two_deallocations_of_one_allocation_are_a_problem_and_no_leaf(capsys):
    twice, _ = walk("b", allocation_then((REFRESH_A, 0), (REFRESH_B, 0)))
    once, _ = walk("a", allocation_then((REFRESH_A, 0)))
    assert twice["seeds"]["4"]["webrtc"]["turn"]["deallocation"] == {"repeated": 1, "unallocated": 0, "early": 0}
    assert twice["seeds"]["4"]["webrtc"]["turn"]["messages"] == ["0x0003"]
    report = ab.compare_transcripts(once, twice, [], 0, "a", "b")
    assert report["differences"] == [] and report["verdict"] == "red" and len(report["problems"]) == 1
    (line,) = report["problems"]
    assert line.startswith("seeds/4/webrtc: b: 1 allocation(s) of the lab TURN were ended by more than one deallocation") and ". Fix: " in line and line.endswith(RULE)
    ab.print_report(report, True)
    assert "FAIL " + line in capsys.readouterr().out
    assert ab.compare_transcripts(once, once, [], 0, "a", "a")["problems"] == []


def test_a_deallocation_for_an_address_that_has_no_allocation_is_a_problem():
    def script(lab, clients, stage):
        if stage == "page":
            clients.append(Client(lab).send(signed_allocate(ALLOCATE), "stun-0x0003"))
        else:
            clients.append(Client(lab).send(signed_refresh(REFRESH_C, 0), "stun-0x0004"))      # another socket: another address, which was never allocated

    orphan, evidence = walk("b", script)
    assert orphan["seeds"]["4"]["webrtc"]["turn"]["deallocation"] == {"repeated": 0, "unallocated": 1, "early": 0}
    assert evidence["deallocation"]["records"][0]["allocation"] is None
    report = ab.compare_transcripts(orphan, orphan, [], 0, "a", "b")
    assert report["differences"] == [] and len(report["problems"]) == 2, "each side that did it is named"
    assert all("from an address that has no allocation. Fix: " in p and p.endswith(RULE) for p in report["problems"])
    assert [p.split(": ")[1] for p in report["problems"]] == ["a", "b"]


def serve_one(request_after_allocation):
    """A TurnServer on loopback that heard an allocation and then ``request_after_allocation`` from the same socket; what it recorded."""
    server = _turn_chain.TurnServer()
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(5)
    try:
        client.sendto(signed_allocate(ALLOCATE), server.address)
        assert struct.unpack("!H", client.recvfrom(2048)[0][:2])[0] == 0x0103
        client.sendto(request_after_allocation, server.address)
        answer, _ = client.recvfrom(2048)
        port = client.getsockname()[1]
    finally:
        client.close()
        server.close()
    return server, answer, port


def test_the_turn_server_records_a_signed_refresh_with_lifetime_zero_as_a_deallocation_and_answers_it_as_it_always_did():
    server, answer, port = serve_one(signed_refresh(REFRESH_A, 0))
    assert struct.unpack("!H", answer[:2])[0] == 0x0104, "the answer to a Refresh is what it was"
    assert server.deallocations == [("127.0.0.1", port, REFRESH_A, server.deallocations[0][3], 0)]
    assert server.deallocation_tids() == {REFRESH_A} and [r[2] for r in server.deallocation_transactions()] == [REFRESH_A]
    assert len(server.allocations) == 1 and server.refused == []


def test_the_turn_server_does_not_record_a_refresh_with_lifetime_600_or_any_other_message_as_a_deallocation():
    server, answer, _ = serve_one(signed_refresh(REFRESH_A, 600))
    assert struct.unpack("!H", answer[:2])[0] == 0x0104 and server.deallocations == [] and server.deallocation_tids() == set()
    server, _, _ = serve_one(_turn_chain._message(0x0008, REFRESH_B, _turn_chain._attr(0x0006, b"lab") + _turn_chain._attr(0x000D, struct.pack("!I", 0)),
                                                   _turn_chain.long_term_key("lab")))
    assert server.deallocations == [], "a lifetime of 0 in another message type (CreatePermission) is not a deallocation: the type counts too"
    server, _, _ = serve_one(signed_refresh(REFRESH_C, 0)[:20] + _turn_chain._attr(0x0006, b"lab"))
    assert server.deallocations == [], "a Refresh with no LIFETIME is not one"


def test_the_gateway_marks_a_deallocation_from_its_payload_and_the_mark_alone_takes_it_out_of_the_count():
    """The datagram can be at the gateway before the lab's TURN server has recorded it: the count does not wait for the server."""
    def script(lab, clients, stage):
        if stage == "page":
            clients.append(Client(lab).send(signed_allocate(ALLOCATE), "stun-0x0003"))
        else:
            for tid, lifetime in ((REFRESH_A, 600), (REFRESH_B, 0)):
                lab.gateway._route(_turn_chain.TURN_IP, _turn_chain.TURN_PORT, 1, signed_refresh(tid, lifetime), 1, None)     # never sent to the server

    transcript, _ = walk("a", script)
    walk_of = transcript["seeds"]["4"]["webrtc"]
    assert walk_of["udp"]["counts"] == {ab.TURN_TARGET: 2} and walk_of["udp"]["messages"] == {ab.TURN_TARGET: ["stun-0x0003", "stun-0x0004"]}, "the 600 stays, the 0 goes, by the mark"
    with ab.Lab(host="127.0.0.1") as lab:
        lab.rec.set_phase("webrtc")
        for tid, lifetime in ((REFRESH_A, 600), (REFRESH_B, 0)):
            lab.gateway._route(_turn_chain.TURN_IP, _turn_chain.TURN_PORT, 1, signed_refresh(tid, lifetime), 1, None)
        lab.gateway._route("127.0.0.1", 5555, 1, signed_refresh(REFRESH_C, 0), 1, None)
        events = lab.rec.of("webrtc", "udp")
    assert [e["deallocation"] for e in events] == [False, True, True], "marked by meaning, whoever the datagram is for (the walk counts only the TURN address's)"


def test_a_deallocation_before_the_page_began_closing_is_a_problem_and_one_after_it_is_not():
    """The page's own times on the recorder's clock: the call of the page plus (closing - start) of its performance.now() is the moment it began closing (``closing_began``)."""
    def at_page(lab, clients, stage):
        if stage == "page":
            clients.append(Client(lab).send(signed_allocate(ALLOCATE), "stun-0x0003"))
            clients[0].send(signed_refresh(REFRESH_A, 0), "stun-0x0004")        # the deallocation comes while the page is still open
    before, _ = walk("b", at_page, times={"start": 100.0, "closing": 100.0 + 600000.0})        # the page closes ten minutes after the call
    assert before["seeds"]["4"]["webrtc"]["turn"]["deallocation"] == {"repeated": 0, "unallocated": 0, "early": 1}
    report = ab.compare_transcripts(before, before, [], 0, "a", "b")
    assert report["differences"] == [] and len(report["problems"]) == 2 and all(p.endswith(RULE) for p in report["problems"])
    assert all("reached the lab TURN before the page began closing its connection. Fix: " in p for p in report["problems"])

    after, _ = walk("b", allocation_then((REFRESH_A, 0)), times={"start": 100.0, "closing": 100.0})          # closing at the start: the call itself is the bound
    assert after["seeds"]["4"]["webrtc"]["turn"]["deallocation"] == {"repeated": 0, "unallocated": 0, "early": 0}
    assert ab.compare_transcripts(after, after, [], 0, "a", "b")["problems"] == []
    assert ab.closing_began(5.0, {"start": 250.0, "closing": 1750.0}) == 6.5 and ab.closing_began(5.0, None) is None and ab.closing_began(5.0, {"start": 1.0}) is None
