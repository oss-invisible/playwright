"""The probe of the WebRTC walk must hold the credential the lab's TURN server signs with, and the A/A must not count retransmissions (scripts/ci_ab_network.py, tests/_turn_chain.py), no browser.

Two defects of the oracle, each held here:

* The probe gave the TURN server ``lab``/``lab`` while the server signs its answers with the long-term key of its own password: the browser found the MESSAGE-INTEGRITY of every
  Allocate success invalid, dropped it, retransmitted up to the engine's maximum and ended in error 701, so no relay candidate was ever made. And the fake server never checked the
  request's own MESSAGE-INTEGRITY, so it "allocated" for a client with the wrong password. The probe now takes the credential as an argument (the walk passes the server's own
  constant) and the server refuses a request whose integrity is wrong, as a real one does.
* The leaf ``seeds/<seed>/webrtc/udp/counts/<target>`` counted DATAGRAMS. A retransmission is network timing (the first RTO is 100 ms and doubles; a late 401 on a loaded host adds
  one), so 1+7 became 2+7 and the A/A of one engine read 8 against 9. The leaf now counts STUN TRANSACTIONS; a transaction with more transmits than the engine's maximum is a problem
  of the run, not a compared leaf.
"""
from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import re
import socket
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _turn_chain  # noqa: E402
import ci_ab_network as ab  # noqa: E402

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
COOKIE = 0x2112A442


# ----------------------------------------------------------------------------------------------------------------- the count of transactions
def walk_transcript(label, tids):
    """A transcript of seed 4 whose WebRTC walk sent ``tids`` (one entry per datagram, in order) to the TURN server, summarised as ``run`` summarises them."""
    events = [{"target": ab.TURN_TARGET, "message": "stun-0x0003", "tid": tid, "opener": None} for tid in tids]
    return {"format": ab.FORMAT, "label": label, "platform": "linux",
            "seeds": {"4": {"webrtc": {"candidates": ["udp comp1 prio1 relay <exit>"], "gathering": "complete", "udp": ab.udp_summary(events, ab.Normalizer({}))}}}}


A, B = "aa" * 12, "bb" * 12
#: The recorded shape: the first Allocate once and the second seven times, against the first twice (a late 401) and the second seven times.
FIRST_RUN, SECOND_RUN = [A] + [B] * 7, [A] * 2 + [B] * 7


def test_a_late_401_that_adds_a_retransmission_is_not_a_difference_of_the_engine():
    a, b = walk_transcript("a", FIRST_RUN), walk_transcript("b", SECOND_RUN)
    assert len(FIRST_RUN) == 8 and len(SECOND_RUN) == 9, "the recorded 8 against 9"
    counts = lambda t: t["seeds"]["4"]["webrtc"]["udp"]["counts"][ab.TURN_TARGET]  # noqa: E731
    assert (counts(a), counts(b)) == (2, 2), "two transactions on both sides"
    report = ab.compare_transcripts(a, b, [], 0, "a", "b")
    assert report["differences"] == [] and report["problems"] == [] and report["verdict"] == "green"


def test_a_transaction_that_one_run_did_not_make_is_still_a_difference():
    a = walk_transcript("a", FIRST_RUN)
    c = walk_transcript("c", [A] + [B] * 7 + ["cc" * 12])          # a third transaction: the engine asked for something more
    report = ab.compare_transcripts(a, c, [], 0, "a", "c")
    assert [d["path"] for d in report["differences"]] == ["seeds/4/webrtc/udp/counts/%s" % ab.TURN_TARGET]
    assert (report["differences"][0]["a"], report["differences"][0]["b"]) == (2, 3) and report["verdict"] == "red"


def test_a_datagram_that_is_not_stun_counts_one_each_and_the_messages_and_the_order_are_kept():
    events = [{"target": "127.0.0.1:5555", "message": "quic-long", "tid": None}] * 3 + [
        {"target": ab.TURN_TARGET, "message": "stun-0x0003", "tid": A}, {"target": ab.TURN_TARGET, "message": "stun-0x0113", "tid": A}]
    summary = ab.udp_summary(events, ab.Normalizer({}))
    assert summary == {"order": ["127.0.0.1:5555", ab.TURN_TARGET], "messages": {"127.0.0.1:5555": ["quic-long"], ab.TURN_TARGET: ["stun-0x0003", "stun-0x0113"]},
                       "counts": {"127.0.0.1:5555": 3, ab.TURN_TARGET: 1}}


def test_the_engines_maximum_is_the_pref_of_the_engine_and_a_transaction_over_it_is_a_problem_while_one_at_it_is_not():
    assert ab.STUN_MAX_TRANSMITS == 7
    source = (ROOT / "scripts" / "ci_ab_network.py").read_text(encoding="utf-8")
    assert "media.peerconnection.ice.stun_client_maximum_transmits" in source and "modules/libpref/init/all.js" in source

    at_most = walk_transcript("a", [A] * 7)
    assert "over_limit" not in at_most["seeds"]["4"]["webrtc"]["udp"]
    assert ab.compare_transcripts(at_most, at_most, [], 0, "a", "a")["problems"] == []

    over = walk_transcript("b", [A] * 8)
    assert over["seeds"]["4"]["webrtc"]["udp"]["over_limit"] == {ab.TURN_TARGET: [8]}
    report = ab.compare_transcripts(at_most, over, [], 0, "a", "b")
    assert report["verdict"] == "red" and len(report["problems"]) == 1 and "8 times" in report["problems"][0] and "maximum of 7" in report["problems"][0]
    assert report["problems"][0].startswith("b: ") and report["differences"] == [], "a problem of the run, not a compared leaf"
    both = ab.compare_transcripts(over, over, [], 0, "a", "b")
    assert len(both["problems"]) == 2 and both["differences"] == [], "each side that did it is named"


def test_the_a_a_prints_a_transaction_over_the_maximum_as_a_fail_line_and_counts_it_among_the_problems(capsys):
    over = walk_transcript("a", [A] * 8)
    report = ab.compare_transcripts(over, walk_transcript("b", [A] * 7), [], 0, "a", "b")
    ab.print_report(report, True)
    out = capsys.readouterr().out
    assert re.search(r"^FAIL a: a STUN transaction to 198\.51\.100\.7:3478 was sent 8 times, more than the engine's maximum of 7 transmits", out, re.M), out
    assert "AB NETWORK A/A FAILED: 0 differences, 1 problems" in out and "\nDIFF " not in "\n" + out


# ----------------------------------------------------------------------------------------------------------------- the credential of the probe
def test_the_probe_has_no_credential_written_in_it_and_the_walk_passes_the_one_of_the_lab_server():
    js = ab.RTC_JS
    assert not re.search(r"""credential\s*:\s*['"]""", js) and not re.search(r"""username\s*:\s*['"]""", js), "no literal credential or user name in the page's script"
    assert "'lab'" not in js and _turn_chain.PASSWORD not in js
    assert "async (args) =>" in js and "const [stunUrl, turnUrl, username, credential] = args;" in js
    assert "{urls: [turnUrl], username, credential}" in js and "{urls: [stunUrl]}" in js, "the STUN entry has no credential, the TURN entry has the arguments"
    walk = inspect.getsource(ab.walk_webrtc)
    assert "from _turn_chain import PASSWORD" in walk and "[STUN_URL, TURN_URL, TURN_USERNAME, TURN_PASSWORD]" in walk
    assert _turn_chain.PASSWORD not in (ROOT / "scripts" / "ci_ab_network.py").read_text(encoding="utf-8"), "the password is written once, in tests/_turn_chain.py"


# ----------------------------------------------------------------------------------------------------------------- the TURN server refuses a wrong integrity
def attribute(kind, value):
    return struct.pack("!HH", kind, len(value)) + value + b"\x00" * (-len(value) % 4)


def signed(mtype, tid, user, password, extra=b""):
    """A request as a client builds it (RFC 5389 section 15.4), written here and not taken from the server's module: USERNAME, REALM, NONCE, then MESSAGE-INTEGRITY, the
    HMAC-SHA1 under md5(user:realm:password) over the message with the length of the header counting the attribute itself."""
    body = attribute(0x0006, user.encode()) + attribute(0x0014, _turn_chain.REALM.encode()) + attribute(0x0015, _turn_chain.NONCE.encode()) + extra
    head = struct.pack("!HHI", mtype, len(body) + 24, COOKIE) + tid
    key = hashlib.md5(("%s:%s:%s" % (user, _turn_chain.REALM, password)).encode(), usedforsecurity=False).digest()
    return head + body + attribute(0x0008, hmac.new(key, head + body, hashlib.sha1).digest())


def unsigned(mtype, tid):
    return struct.pack("!HHI", mtype, 0, COOKIE) + tid


def ask(server, request):
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(5)
    try:
        client.sendto(request, server.address)
        answer, _ = client.recvfrom(65536)
    finally:
        client.close()
    return struct.unpack("!H", answer[:2])[0], _turn_chain._attrs(answer)


def error_code(attrs):
    return attrs[0x0009][2] * 100 + attrs[0x0009][3]


@pytest.fixture()
def turn():
    server = _turn_chain.TurnServer()
    try:
        yield server
    finally:
        server.close()


def test_with_the_right_credential_an_allocate_is_answered_401_and_then_with_the_relay_and_nothing_is_refused(turn):
    kind, attrs = ask(turn, unsigned(0x0003, b"\x01" * 12))
    assert kind == 0x0113 and error_code(attrs) == 401 and attrs[0x0014] == _turn_chain.REALM.encode() and attrs[0x0015] == _turn_chain.NONCE.encode()
    kind, attrs = ask(turn, signed(0x0003, b"\x02" * 12, "lab", _turn_chain.PASSWORD))
    assert kind == 0x0103 and 0x0016 in attrs and 0x0008 in attrs, "the success carries XOR-RELAYED-ADDRESS and is signed"
    assert [a[2] for a in turn.allocations] == ["lab"] and turn.refused == []
    assert len(turn.seen) == 2 and [r[3] for r in turn.requests] == [False, True], "requests and seen keep their meaning"


def test_with_a_wrong_credential_both_allocates_are_answered_401_and_nothing_is_allocated(turn):
    kind, attrs = ask(turn, unsigned(0x0003, b"\x01" * 12))
    assert kind == 0x0113 and error_code(attrs) == 401
    kind, attrs = ask(turn, signed(0x0003, b"\x02" * 12, "lab", "not-the-password"))
    assert kind == 0x0113 and error_code(attrs) == 401 and 0x0016 not in attrs, "no relay for a client with the wrong password"
    assert attrs[0x0014] == _turn_chain.REALM.encode() and attrs[0x0015] == _turn_chain.NONCE.encode(), "the 401 asks again, with the realm and the nonce"
    assert turn.allocations == []
    assert len(turn.refused) == 1 and turn.refused[0][2:4] == ("lab", b"\x02" * 12), turn.refused
    assert [r[3] for r in turn.requests] == [False, True], "the request still carried credentials: `requests` says what arrived, `refused` what was turned away"


def test_a_request_with_a_user_name_and_no_integrity_or_a_changed_message_is_refused_too(turn):
    body = attribute(0x0006, b"lab")
    kind, _ = ask(turn, struct.pack("!HHI", 0x0003, len(body), COOKIE) + b"\x03" * 12 + body)
    assert kind == 0x0113 and turn.allocations == [] and len(turn.refused) == 1
    good = signed(0x0003, b"\x04" * 12, "lab", _turn_chain.PASSWORD)
    tampered = good[:20] + attribute(0x0006, b"lbb") + good[20 + len(attribute(0x0006, b"lab")):]
    assert len(tampered) == len(good) and ask(turn, tampered)[0] == 0x0113 and turn.allocations == [] and len(turn.refused) == 2
    assert ask(turn, good)[0] == 0x0103 and len(turn.allocations) == 1


def test_a_refresh_with_a_wrong_integrity_is_refused_and_one_with_the_right_one_is_answered(turn):
    kind, attrs = ask(turn, signed(0x0004, b"\x05" * 12, "lab", "wrong"))
    assert kind == 0x0114 and error_code(attrs) == 401 and len(turn.refused) == 1
    kind, _ = ask(turn, signed(0x0004, b"\x06" * 12, "lab", _turn_chain.PASSWORD))
    assert kind == 0x0104 and len(turn.refused) == 1 and turn.allocations == []


def test_the_integrity_check_is_the_one_of_rfc_5389_with_the_length_adjusted():
    good = signed(0x0003, b"\x07" * 12, "lab", _turn_chain.PASSWORD)
    assert _turn_chain.integrity_valid(good, "lab") and not _turn_chain.integrity_valid(good, "other")
    # an attribute after MESSAGE-INTEGRITY (a FINGERPRINT) does not change what is signed
    assert _turn_chain.integrity_valid(good + attribute(0x8028, b"\x00" * 4), "lab")
    assert not _turn_chain.integrity_valid(unsigned(0x0003, b"\x07" * 12), "lab") and not _turn_chain.integrity_valid(good[:-4], "lab")
    assert _turn_chain.long_term_key("lab") == hashlib.md5(("lab:%s:%s" % (_turn_chain.REALM, _turn_chain.PASSWORD)).encode(), usedforsecurity=False).digest()


def test_the_server_of_the_lab_keeps_the_list_of_refusals_too():
    _gateway, turn_class, _page = ab.make_lab_classes()
    server = turn_class("127.0.0.1")
    try:
        assert server.refused == [] and isinstance(server, _turn_chain.TurnServer)
        kind, _ = ask(server, signed(0x0003, b"\x08" * 12, "lab", "wrong"))
        assert kind == 0x0113 and len(server.refused) == 1 and server.allocations == []
        kind, _ = ask(server, signed(0x0003, b"\x09" * 12, "lab", _turn_chain.PASSWORD))
        assert kind == 0x0103 and len(server.allocations) == 1
        json.dumps(server.exchanges)
    finally:
        server.close()
