"""The network A/B oracle's comparator, its normalisation and its lab (scripts/ci_ab_network.py), on synthetic transcripts and local sockets, no browser.

The oracle asks one question of two engines: same journey, same seeds, same lab, same wire. Each property is shown on the input it exists for: two transcripts that
differ in one leaf are red (with the path and both values), equal ones are green, a transcript with fewer leaves than the floor is red (a run that saw less than it should
passes anything), a volatile entry that matches no leaf is red as stale (the list may only shrink), a connection that reached the lab around the proxy on EITHER side is red
by itself, and the A/A (one engine twice) is a control: its red is a broken control, exit 2, never a finding about a candidate.
"""
from __future__ import annotations

import copy
import json
import socket
import struct
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ci_ab_network as ab  # noqa: E402
import _wire_capture as wc  # noqa: E402
from test_wire_capture import build_hello  # noqa: E402

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def transcript(label="a", seeds=(11, 12)):
    """A small transcript with the categories a real one has: a journey step, a TLS hello, an HTTP/2 connection, a WebRTC gather, an idle window and the lab's direct lists."""
    def seed_body():
        return {
            "journey": {"order": ["127.0.0.1:A", "127.0.0.1:O"],
                        "steps": {"01": {"name": "open home", "ok": True, "idle": True, "url": "A/",
                                         "connects": {"order": ["127.0.0.1:A"], "counts": {"127.0.0.1:A": 2}},
                                         "requests": {"GET A/": {"count": 1, "headers": [["Host", "journey-a.test:A"], ["Accept", "*/*"]], "name_orders": ["Host,Accept"],
                                                                 "body_length": 0}}}}},
            "https": {"h2": {"v1": {"connections": [{"alpn": "h2", "tls": wc.parse_client_hello(build_hello())}]}}},
            "webrtc": {"candidates": ["udp comp1 prio100 srflx <exit>"], "gathering": "complete"},
            "idle": {"c1": {"destinations": {"connects": [], "dns": []}}},
            "lab": {"direct": {"witness_tcp": [], "witness_udp": [], "watcher": [], "doh_direct": [], "resolver_trap": [], "blind": []}},
        }
    return {"format": ab.FORMAT, "label": label, "seeds": {str(s): seed_body() for s in seeds}}


def compare(a, b, volatile=(), floor=0):
    return ab.compare_transcripts(a, b, list(volatile), floor, a.get("label", "A"), b.get("label", "B"))


# ----------------------------------------------------------------------------------------------------------------- the comparison
def test_equal_transcripts_are_green_and_the_leaves_are_counted_by_category():
    report = compare(transcript(), transcript())
    assert report["verdict"] == "green" and report["problems"] == [] and report["differences"] == []
    assert report["leaves"] > 100
    assert set(report["by_category"]) == {"journey", "https", "webrtc", "idle", "lab"}
    assert sum(report["by_category"].values()) == report["leaves"]


def test_a_difference_in_one_leaf_is_red_and_names_the_path_and_both_values():
    b = transcript("b")
    b["seeds"]["12"]["journey"]["steps"]["01"]["requests"]["GET A/"]["headers"][1][1] = "text/html"
    report = compare(transcript(), b)
    assert report["verdict"] == "red"
    (difference,) = report["differences"]
    assert difference["path"] == "seeds/12/journey/steps/01/requests/GET A//headers/1/1"
    assert difference["a"] == "*/*" and difference["b"] == "text/html"
    assert any(difference["path"] in p and "*/*" in p and "text/html" in p for p in report["problems"])


def test_a_leaf_that_one_side_does_not_have_is_a_difference():
    b = transcript("b")
    del b["seeds"]["11"]["https"]["h2"]["v1"]["connections"][0]["tls"]["extensions"]["record_size_limit"]
    (difference,) = compare(transcript(), b)["differences"]
    assert difference["a"] == 16385 and difference["b"] == ab.MISSING and difference["path"].endswith("/record_size_limit")


def test_an_order_that_changed_is_a_difference_at_the_index_where_it_changed():
    b = transcript("b")
    tls = b["seeds"]["11"]["https"]["h2"]["v1"]["connections"][0]["tls"]
    tls["cipher_suites"][1], tls["cipher_suites"][2] = tls["cipher_suites"][2], tls["cipher_suites"][1]
    paths = [d["path"] for d in compare(transcript(), b)["differences"]]
    assert paths == ["seeds/11/https/h2/v1/connections/0/tls/cipher_suites/1", "seeds/11/https/h2/v1/connections/0/tls/cipher_suites/2"]


def test_fewer_leaves_than_the_floor_is_red_even_when_every_leaf_is_equal():
    report = compare(transcript(), transcript(), floor=10 ** 6)
    leaves = report["leaves"]
    assert report["verdict"] == "red" and report["differences"] == []
    assert any(("only %d leaves" % leaves) in p for p in report["problems"])
    assert compare(transcript(), transcript(), floor=leaves)["verdict"] == "green"


def test_sides_that_ran_different_seeds_are_red():
    report = compare(transcript(seeds=(11, 12)), transcript(seeds=(11, 13)))
    assert report["verdict"] == "red" and any("different seeds" in p for p in report["problems"])


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("name", ["witness_tcp", "witness_udp", "watcher", "doh_direct", "resolver_trap", "blind"])
def test_a_direct_connection_on_either_side_is_red_by_itself(side, name):
    """Known-bad: the witness heard the browser, or the watcher saw a connection past the proxy, or the sink was blind: red even when the other side did the same."""
    a, b = transcript("a"), transcript("b")
    for t in ((a,) if side == "a" else (b,)):
        t["seeds"]["11"]["lab"]["direct"][name] = ["tcp from 192.0.2.1"]
    report = compare(a, b)
    assert report["verdict"] == "red" and report["direct"]
    assert any(("a, seed 11" if side == "a" else "b, seed 11") in p for p in report["problems"] if p.startswith("a direct connection"))
    both = copy.deepcopy(a)
    both["seeds"]["11"]["lab"]["direct"][name] = ["tcp from 192.0.2.1"]
    c = copy.deepcopy(both)
    assert compare(both, c)["verdict"] == "red", "a leak that both engines make is still a leak"


def test_a_volatile_entry_excuses_the_leaves_it_names_and_only_those():
    b = transcript("b")
    b["seeds"]["11"]["journey"]["steps"]["01"]["connects"]["counts"]["127.0.0.1:A"] = 3
    b["seeds"]["12"]["webrtc"]["gathering"] = "new"
    entry = {"pattern": "seeds/*/journey/steps/*/connects/counts/*", "reason": "speculative connections", "aa_run": "run 1"}
    report = compare(transcript(), b, [entry])
    assert [d["path"] for d in report["differences"]] == ["seeds/12/webrtc/gathering"]
    assert report["excused"] == 2 and report["volatile"] == [{"pattern": entry["pattern"], "leaves": 2, "differing": 1}]
    assert report["leaves"] == compare(transcript(), b)["leaves"] - 2


def test_a_volatile_entry_that_matches_no_leaf_is_stale_and_red():
    """The list may only shrink: an entry whose leaf is gone says nothing any more and hides what a new leaf of that shape would say."""
    entry = {"pattern": "seeds/*/journey/steps/*/ads/*", "reason": "a leaf that was removed", "aa_run": "run 1"}
    report = compare(transcript(), transcript(), [entry])
    assert report["verdict"] == "red" and report["stale"] == [entry["pattern"]]
    assert any("stale" in p for p in report["problems"])


def test_a_long_value_is_clipped_in_the_diff_with_its_hash():
    b = transcript("b")
    b["seeds"]["11"]["webrtc"]["gathering"] = "x" * 2000
    (difference,) = compare(transcript(), b)["differences"]
    assert difference["b"]["len"] > 2000 and len(difference["b"]["sha256"]) == 64 and difference["a"] == "complete"


# ----------------------------------------------------------------------------------------------------------- the exit codes and the files
def write(path, document):
    ab.write_json(path, document)
    return path


def test_the_exit_codes_green_red_and_broken_control(tmp_path, capsys):
    a = write(tmp_path / "a.json", transcript("a"))
    same = write(tmp_path / "same.json", transcript("b"))
    other = transcript("c")
    other["seeds"]["11"]["webrtc"]["gathering"] = "new"
    different = write(tmp_path / "different.json", other)
    empty = tmp_path / "volatile.json"
    empty.write_text(json.dumps({"format": ab.VOLATILE_FORMAT, "entries": []}), encoding="utf-8")
    base = ["compare", str(a), "--volatile", str(empty)]
    assert ab.main(base[:2] + [str(same)] + base[2:]) == 0
    assert ab.main(base[:2] + [str(different)] + base[2:] + ["--out", str(tmp_path / "diff.json")]) == 1
    assert ab.main(base[:2] + [str(different)] + base[2:] + ["--a-a"]) == 2, "the A/A that differs is a broken control, not a finding"
    assert ab.main(base[:2] + [str(same)] + base[2:] + ["--a-a"]) == 0
    printed = capsys.readouterr().out
    assert 'DIFF seeds/11/webrtc/gathering: a "complete" | c "new"' in printed and "AB NETWORK A/A FAILED" in printed and "AB NETWORK A/B FAILED" in printed
    diff = json.loads((tmp_path / "diff.json").read_text(encoding="utf-8"))
    assert diff["verdict"] == "red" and diff["differences"][0]["path"] == "seeds/11/webrtc/gathering"
    assert ab.main(["judge", str(tmp_path / "diff.json")]) == 1 and ab.main(["judge", str(tmp_path / "diff.json"), "--a-a"]) == 2


def test_a_file_that_is_not_a_transcript_is_refused(tmp_path):
    (tmp_path / "x.json").write_text(json.dumps({"format": "other"}), encoding="utf-8")
    with pytest.raises(SystemExit):
        ab.read_transcript(tmp_path / "x.json")


def test_an_a_a_that_differs_proposes_the_volatile_patterns_without_the_seed_and_the_step():
    b = transcript("b")
    b["seeds"]["11"]["journey"]["steps"]["01"]["connects"]["counts"]["127.0.0.1:A"] = 3
    b["seeds"]["12"]["journey"]["steps"]["01"]["connects"]["counts"]["127.0.0.1:A"] = 4
    assert ab.propose_volatile(compare(transcript(), b)) == ["seeds/*/journey/steps/*/connects/counts/127.0.0.1:A"]


# ------------------------------------------------------------------------------------------------------------------ the volatile list
def test_the_volatile_list_of_the_repository_is_well_formed_and_every_entry_names_its_evidence():
    entries = ab.load_volatile()
    assert all(e["aa_run"].strip() and e["reason"].strip() for e in entries)


@pytest.mark.parametrize("document,why", [
    ({"format": "x", "entries": []}, "format"),
    ({"format": ab.VOLATILE_FORMAT, "entries": "no"}, "list"),
    ({"format": ab.VOLATILE_FORMAT, "entries": [{"pattern": "a/*", "reason": "r"}]}, "aa_run"),
    ({"format": ab.VOLATILE_FORMAT, "entries": [{"pattern": "a/*", "reason": " ", "aa_run": "run"}]}, "reason"),
    ({"format": ab.VOLATILE_FORMAT, "entries": [{"pattern": "a/*", "reason": "r", "aa_run": "x"}, {"pattern": "a/*", "reason": "r", "aa_run": "y"}]}, "twice"),
])
def test_a_malformed_volatile_list_is_an_error_not_an_empty_list(tmp_path, document, why):
    path = tmp_path / "v.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SystemExit) as raised:
        ab.load_volatile(path)
    assert why in str(raised.value)


# ----------------------------------------------------------------------------------------------------------------------- the seeds
def test_the_seeds_come_from_the_persona_file_when_it_has_the_candidate_key_and_from_the_oracle_of_the_page_otherwise(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "SEEDS_FILE", tmp_path / "absent.json")
    assert ab.default_seeds() == ab.FALLBACK_SEEDS == (11, 12, 13)
    present = tmp_path / "persona-seeds.json"
    present.write_text(json.dumps({"candidate": [101, 102], "other": [1]}), encoding="utf-8")
    monkeypatch.setattr(ab, "SEEDS_FILE", present)
    assert ab.default_seeds() == (101, 102)
    present.write_text(json.dumps({"other": [1]}), encoding="utf-8")
    assert ab.default_seeds() == (11, 12, 13)
    assert ab.parse_seeds("5, 6,7") == (5, 6, 7)


# -------------------------------------------------------------------------------------------------------------------- the normalisation
def test_a_port_of_the_lab_becomes_its_role_everywhere_a_string_holds_it():
    norm = ab.Normalizer({40001: "A", 40002: "h3", 40003: "witness"})
    assert norm.text("127.0.0.1:40001") == "127.0.0.1:A"
    assert norm.text("http://journey-a.test:40001/article/2?x=1:40002") == "http://journey-a.test:A/article/2?x=1:h3"
    assert norm.text('h3=":40002"; ma=3600') == 'h3=":h3"; ma=3600'
    assert norm.text("12:30:45 and :40009 stay") == "12:30:45 and :40009 stay"
    assert norm.walk({"127.0.0.1:40003": ["a:40001", {"k": "b:40002"}], "n": 40001}) == {"127.0.0.1:witness": ["a:A", {"k": "b:h3"}], "n": 40001}


def test_the_random_token_of_a_page_and_the_value_of_a_cookie_are_dropped_and_their_names_stay():
    norm = ab.Normalizer({1111: "A"})
    assert norm.text("/hold?id=lp3f9k2a") == "/hold?id=lp<TOK>"
    assert norm.text("/hold?id=kapark3f9k2a&until=9") == "/hold?id=kapark<TOK>&until=9"
    assert norm.text("/jf/slow?tok=ab12cd") == "/jf/slow?tok=<TOK>"
    assert norm.text("/r?kind=json&id=late3") == "/r?kind=json&id=late3"
    assert norm.header("Cookie", "session=9f8e7d; theme=dark") == "session=<v>; theme=<v>"
    assert norm.header("Referer", "http://journey-a.test:1111/") == "http://journey-a.test:A/"
    assert norm.headers([["Host", "x:1111"], ["cookie", "a=1"]]) == [["Host", "x:A"], ["cookie", "a=<v>"]]


def test_the_requests_of_a_stage_keep_the_header_order_of_the_first_and_every_distinct_order():
    norm = ab.Normalizer({7000: "A"})
    first = {"role": "A", "method": "GET", "target": "/a?tok=zz11", "headers": [["Host", "h:7000"], ["Accept", "*/*"]], "body_length": 0}
    second = {"role": "A", "method": "GET", "target": "/a?tok=yy22", "headers": [["Host", "h:7000"], ["Cookie", "s=1"], ["Accept", "*/*"]], "body_length": 0}
    other = {"role": "O", "method": "POST", "target": "/beacon", "headers": [["Host", "o"]], "body_length": 3}
    summary = ab.request_summary([first, second, other], norm)
    assert list(summary) == ["GET A/a?tok=<TOK>", "POST O/beacon"]
    entry = summary["GET A/a?tok=<TOK>"]
    assert entry["count"] == 2 and entry["headers"] == [["Host", "h:A"], ["Accept", "*/*"]]
    assert entry["name_orders"] == ["Host,Accept", "Host,Cookie,Accept"]
    assert summary["POST O/beacon"]["body_length"] == 3


def test_connects_dns_and_datagrams_keep_the_first_seen_order_and_count_the_rest():
    norm = ab.Normalizer({7000: "A"})
    assert ab.connect_summary(["127.0.0.1:7000", "192.0.2.53:443", "127.0.0.1:7000"], norm) == {
        "order": ["127.0.0.1:A", "192.0.2.53:443"], "counts": {"127.0.0.1:A": 2, "192.0.2.53:443": 1}}
    assert ab.dns_summary([("b.test", 28, "lab"), ("a.test", 1, "lab"), ("b.test", 1, "lab"), ("b.test", 65, "lab")]) == {
        "order": ["b.test", "a.test"], "types": {"a.test": [1], "b.test": [1, 28, 65]}}
    stun = b"\x00\x01\x00\x00\x21\x12\xa4\x42" + b"\x00" * 12
    udp = ab.udp_summary([{"target": "198.51.100.5:19302", "message": ab.message_kind(stun)}, {"target": "198.51.100.5:19302", "message": ab.message_kind(stun)}], norm)
    assert udp == {"order": ["198.51.100.5:19302"], "messages": {"198.51.100.5:19302": ["stun-0x0001"]}, "counts": {"198.51.100.5:19302": 2}}
    assert ab.message_kind(b"\xc3" + b"\x00" * 30) == "quic-long" and ab.message_kind(b"\x43abc") == "quic-short" and ab.message_kind(b"\x01") == "other"


def test_an_ice_candidate_loses_what_it_draws_at_random_and_keeps_what_the_client_decides():
    exit_ip = "198.51.100.9"
    mdns = "candidate:0 1 UDP 2122252543 6f1c1e2a-9b8d-4c33-a1e0-5d7f7a9c0b11.local 51234 typ host"
    srflx = "candidate:1 1 UDP 1686052607 198.51.100.9 51234 typ srflx raddr 0.0.0.0 rport 0"
    relay = "candidate:2 1 UDP 92216831 203.0.113.5 61000 typ relay raddr 198.51.100.9 rport 40000"
    assert ab.candidate_summary(mdns, exit_ip) == "udp comp1 prio2122252543 host <mdns>"
    assert ab.candidate_summary(srflx, exit_ip) == "udp comp1 prio1686052607 srflx <exit> raddr 0.0.0.0 rport 0"
    assert ab.candidate_summary(relay, exit_ip) == "udp comp1 prio92216831 relay <other> raddr <exit> rport <port>"
    assert ab.candidate_summary("candidate:3 garbled", exit_ip).startswith("unparsed:")
    again = mdns.replace("6f1c1e2a-9b8d-4c33-a1e0-5d7f7a9c0b11", "11111111-2222-3333-4444-555555555555").replace("51234", "1")
    assert ab.candidate_summary(again, exit_ip) == ab.candidate_summary(mdns, exit_ip)


def test_an_sdp_keeps_its_shape_and_loses_its_random_values():
    sdp = "\r\n".join(["v=0", "o=mozilla...THIS_IS_SDPARTA-99.0 7134562788921547 0 IN IP4 0.0.0.0", "s=-", "t=0 0", "a=fingerprint:sha-256 AA:BB", "a=group:BUNDLE 0",
                       "m=application 54321 UDP/DTLS/SCTP webrtc-datachannel", "c=IN IP4 198.51.100.9", "a=candidate:0 1 UDP 1 198.51.100.9 1 typ srflx",
                       "a=ice-ufrag:abcd", "a=ice-pwd:0123456789", "a=mid:0", ""])
    lines = ab.sdp_summary(sdp, "198.51.100.9")
    assert "a=fingerprint:<v>" in lines and "a=ice-ufrag:<v>" in lines and "c=IN IP4 <exit>" in lines and "m=application <port> UDP/DTLS/SCTP webrtc-datachannel" in lines
    assert not any(line.startswith("a=candidate") for line in lines) and "o=mozilla...THIS_IS_SDPARTA-99.0 <id> <ver> IN IP4 0.0.0.0" in lines


# --------------------------------------------------------------------------------------------------------------------------- the recorder
def test_the_recorder_gives_each_item_to_the_stage_that_was_current_when_it_arrived():
    rec = ab.Recorder()
    questions: list = []
    rec.attach("doh", questions)
    questions.append("q0")
    rec.set_phase("journey.01")
    rec.event("connect", target="t1")
    questions += ["q1", "q2"]
    rec.set_phase("journey.02")
    rec.event("connect", target="t2")
    rec.event("http", role="A")
    questions.append("q3")
    rec.set_phase("webrtc")
    questions.append("q4")
    assert rec.slice("doh", "launch") == ["q0"]
    assert rec.slice("doh", "journey.01") == ["q1", "q2"]
    assert rec.slice("doh", "journey") == ["q1", "q2", "q3"], "a prefix holds the stages under it"
    assert rec.slice("doh", "webrtc") == ["q4"]
    assert [e["target"] for e in rec.of("journey", "connect")] == ["t1", "t2"] and [e["target"] for e in rec.of("journey.02", "connect")] == ["t2"]
    assert rec.of("journey.01", "http") == [] and len(rec.of("journey", "http")) == 1


# -------------------------------------------------------------------------------------------------------------------------- the lab itself
def _http_server():
    """A plain HTTP/1.1 server on loopback that answers every request with its own path; ``port`` and ``close``."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            body = self.path.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET  # noqa: N815

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _read_response(sock, until: bytes) -> bytes:
    sock.settimeout(60)
    data = b""
    while until not in data:
        chunk = sock.recv(4096)
        assert chunk, "closed before %r: %r" % (until, data)
        data += chunk
    return data


def test_the_tee_writes_down_each_request_head_as_it_was_written_and_changes_no_byte():
    rec = ab.Recorder()
    server = _http_server()
    tee = ab.RawTee(rec, server.server_address[1], "A")
    try:
        with socket.create_connection(("127.0.0.1", tee.port), timeout=60) as client:
            client.sendall(b"GET /one?x=1 HTTP/1.1\r\nHost: h\r\nX-Mixed-Case: A\r\nx-empty:\r\n\r\n")
            assert _read_response(client, b"/one?x=1").startswith(b"HTTP/1.1 200")
            client.sendall(b"POST /two HTTP/1.1\r\nHost: h\r\nContent-Length: 3\r\nAccept: */*\r\n\r\nabc")
            assert _read_response(client, b"/two").endswith(b"/two")
    finally:
        tee.close()
        server.shutdown()
        server.server_close()
    events = rec.of("launch", "http")
    assert [(e["role"], e["method"], e["target"], e["body_length"]) for e in events] == [("A", "GET", "/one?x=1", 0), ("A", "POST", "/two", 3)]
    assert events[0]["headers"] == [["Host", "h"], ["X-Mixed-Case", "A"], ["x-empty", ""]]
    assert events[1]["headers"] == [["Host", "h"], ["Content-Length", "3"], ["Accept", "*/*"]]


def _socks_connect(proxy_port: int, host: str, port: int):
    """A SOCKS5 CONNECT by address, no authentication: ``(socket, reply code)``."""
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=60)
    sock.settimeout(60)
    sock.sendall(b"\x05\x01\x00")
    assert sock.recv(2) == b"\x05\x00"
    sock.sendall(b"\x05\x01\x00\x01" + socket.inet_aton(host) + struct.pack("!H", port))
    reply = b""
    while len(reply) < 10:
        reply += sock.recv(10 - len(reply))
    return sock, reply[1]


def test_the_gateway_writes_down_every_connect_sends_a_teed_port_to_its_tee_and_carries_nothing_past_loopback_that_the_lab_did_not_name():
    from _doh_servers import FakeDoh

    gateway_class, turn_class, _page = ab.make_lab_classes()
    rec = ab.Recorder()
    doh = FakeDoh({})
    turn = turn_class("127.0.0.1")
    gateway = gateway_class(rec, doh, turn, "127.0.0.1", "198.51.100.9")
    server = _http_server()
    tee = ab.RawTee(rec, server.server_address[1], "A")
    gateway.reroute[server.server_address[1]] = tee.port
    try:
        sock, code = _socks_connect(gateway.port, "127.0.0.1", server.server_address[1])
        assert code == 0
        sock.sendall(b"GET /through HTTP/1.1\r\nHost: h\r\n\r\n")
        assert _read_response(sock, b"/through").endswith(b"/through")
        sock.close()
        refused, refusal = _socks_connect(gateway.port, "192.0.2.77", 8080)
        refused.close()
        assert refusal != 0, "an address past loopback that the lab did not name must be refused"
    finally:
        tee.close()
        server.shutdown()
        server.server_close()
        gateway.close()
        turn.close()
        doh.close()
    assert [e["target"] for e in rec.of("launch", "connect")] == ["127.0.0.1:%d" % server.server_address[1], "192.0.2.77:8080"]
    assert [e["target"] for e in rec.of("launch", "http")] == ["/through"], "the request went through the tee: its head was written down"


def test_the_gateway_carries_a_datagram_to_the_turn_and_to_the_stun_of_the_lab_and_drops_every_other_one():
    from _doh_servers import FakeDoh
    from _turn_chain import TURN_IP, TURN_PORT

    gateway_class, turn_class, _page = ab.make_lab_classes()
    rec = ab.Recorder()
    doh = FakeDoh({})
    turn = turn_class("127.0.0.1")
    gateway = gateway_class(rec, doh, turn, "127.0.0.1", "198.51.100.9")
    gateway.udp_allowed.add(("127.0.0.1", 4444))
    stun = b"\x00\x01\x00\x00\x21\x12\xa4\x42" + b"\x07" * 12
    try:
        to_turn = gateway._route(TURN_IP, TURN_PORT, 1, stun, 1, None)
        assert to_turn == (turn.address, (TURN_IP, TURN_PORT))
        to_stun = gateway._route("198.51.100.5", 19302, 1, stun, 1, None)
        assert to_stun is not None and to_stun[0] == gateway.stun.address
        assert gateway._route("127.0.0.1", 4444, 1, b"\xc3", 1, None) == (("127.0.0.1", 4444), None)
        assert gateway._route("127.0.0.1", 5555, 1, b"", 1, None) is None, "a datagram to a witness or to anything else is dropped"
        assert gateway._route("name.test", 4444, 3, b"", 1, None) is None
    finally:
        gateway.close()
        turn.close()
        doh.close()
    assert [(e["target"], e["message"]) for e in rec.of("launch", "udp")] == [
        ("198.51.100.7:3478", "stun-0x0001"), ("198.51.100.5:19302", "stun-0x0001"), ("127.0.0.1:4444", "quic-long"), ("127.0.0.1:5555", "other"), ("name.test:4444", "other")]


def test_the_page_site_serves_the_probe_page_with_its_canary_and_counts_the_sentinels():
    import urllib.request

    _gateway, _turn, page_class = ab.make_lab_classes()
    site = page_class("http://192.0.2.1:9/c.png")
    try:
        base = "http://127.0.0.1:%d" % site.port
        assert b"http://192.0.2.1:9/c.png" in urllib.request.urlopen(base + "/rtc", timeout=60).read()
        assert urllib.request.urlopen(base + "/s/3", timeout=60).read() == b"ok"
        assert urllib.request.urlopen(base + "/", timeout=60).read().startswith(b"<!doctype html>")
        assert site.sentinels == ["/s/3"]
    finally:
        site.close()


# ------------------------------------------------------------------------------------------------------------------- what a transcript holds
def minimal_run():
    """The leaves per category of a minimal but complete run of ONE seed, from the real parsers and summaries: a floor under this number says the run saw less than
    one request on each road. (A real run holds many times more: the first A/A measures it.)"""
    norm = ab.Normalizer({7000: "A", 7001: "O", 7002: "h1", 7003: "h2", 7004: "h3"})
    headers = [["Host", "journey-a.test:7000"], ["User-Agent", "Mozilla/5.0"], ["Accept", "text/html"], ["Accept-Language", "en-US,en;q=0.5"], ["Accept-Encoding", "gzip, deflate"],
               ["Connection", "keep-alive"], ["Upgrade-Insecure-Requests", "1"], ["Cookie", "theme=dark"]]
    request = {"role": "A", "method": "GET", "target": "/", "headers": headers, "body_length": 0}
    step = {"connects": ab.connect_summary(["127.0.0.1:7000"], norm), "dns": ab.dns_summary([("journey-a.test", 1, "lab")]), "udp": ab.udp_summary([], norm),
            "requests": ab.request_summary([request] * 3, norm), "name": "n", "ok": True, "idle": True, "url": "A/", "title": "t", "marker": "m", "cookies": [], "history_length": 2,
            "pages": 1, "extra": {}}
    tls = wc.parse_client_hello(build_hello())
    h2 = {"settings": [[1, 65536], [2, 0], [4, 131072], [5, 16384]], "frame_types_before_the_first_request": ["SETTINGS", "WINDOW_UPDATE"],
          "window_updates_before_the_first_request": [{"stream": 0, "increment": 12517377}], "priority": [], "priority_update": [], "headers_priority": [],
          "frames_after_the_first_request": 3, "settings_ack_sent_by_client": True,
          "requests": [{"stream": 1, "pseudo_order": [":method", ":path", ":authority", ":scheme"], "header_names": ["user-agent", "accept"], "headers": [[":method", "GET"], ["user-agent", "x"]],
                        "end_stream": True}]}
    body = {"journey": {"steps": {"%02d" % i: step for i in range(17)}, "order": ["127.0.0.1:A"]}, "https": {"h2": {"v1": {"connections": [{"alpn": "h2", "tls": tls, "h2": h2}]}}},
            "webrtc": {"candidates": ["udp comp1 prio1 srflx <exit>"], "gathering": "complete"}, "idle": {"c1": {"destinations": {"connects": [], "dns": []}}},
            "lab": {"direct": {"witness_tcp": [], "witness_udp": [], "watcher": [], "doh_direct": [], "resolver_trap": [], "blind": []}}}
    leaves = ab.flatten({"11": body})
    by_category = {}
    for path in leaves:
        by_category[ab.category_of(path)] = by_category.get(ab.category_of(path), 0) + 1
    return by_category, len(leaves)


def test_a_whole_synthetic_transcript_of_a_plausible_run_holds_the_leaves_the_floor_is_chosen_from():
    by_category, total = minimal_run()
    assert set(by_category) == {"journey", "https", "webrtc", "idle", "lab"}
    assert by_category["https"] > 100, "the ClientHello alone is a hundred leaves or more"
    assert by_category["journey"] > 17 * 30
    assert total > 700


def test_the_script_names_the_categories_the_run_writes_and_the_e2e_floor_is_below_a_minimal_run():
    assert ab.CATEGORIES == ("journey", "https", "h3", "webrtc", "idle", "lab")
    import test_ab_network_e2e as e2e

    _by_category, minimal = minimal_run()
    assert set(e2e.FLOOR) == {"linux", "win32"}
    assert all(0 < floor < minimal for floor in e2e.FLOOR.values()), "the floor is above what a minimal complete run of one seed holds (%d): a healthy run would be red" % minimal


# ------------------------------------------------------------------------------------------------ the lab as a whole, with no browser
def test_the_whole_lab_carries_a_client_through_its_gateway_and_writes_down_each_road_by_stage():
    """The lab as a run assembles it (the journey site behind its tees, the HTTPS servers, the DoH, the gateway), driven by a plain client that speaks SOCKS5: the road of
    a page of the journey (through its tee), the road of an HTTPS page (through the gateway to a capture server), the stage each belongs to, the role that replaces each port."""
    import ssl

    from fakeweb import journey_site

    with ab.Lab(journey=True, wire=True, host="127.0.0.1") as lab:
        journey_site.STATE.set_step(3)
        assert lab.rec.phase == "journey.03", "the walk reaching a step moves the stage"
        port_a = int(lab.origin_a.rsplit(":", 1)[1])
        sock, code = _socks_connect(lab.gateway.port, "127.0.0.1", port_a)
        assert code == 0
        sock.sendall(b"GET /?noise=0 HTTP/1.1\r\nHost: journey-a.test:%d\r\nUser-Agent: smoke\r\nCookie: a=1\r\n\r\n" % port_a)
        assert _read_response(sock, b"</html>").startswith(b"HTTP/1.1 200")
        sock.close()
        lab.rec.set_phase("https.h1.v1")
        raw, code = _socks_connect(lab.gateway.port, "127.0.0.1", lab.h1.port)
        assert code == 0
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
        context.set_alpn_protocols(["h2", "http/1.1"])
        tls = context.wrap_socket(raw, server_hostname=ab.H1_NAME)
        tls.sendall(b"GET /wire.css HTTP/1.1\r\nHost: h1.lab.test:%d\r\nAccept: text/css\r\n\r\n" % lab.h1.port)
        assert b"p{color" in _read_response(tls, b"p{color")
        tls.close()
        journey = ab.phase_data(lab, "journey.03")
        assert journey["connects"] == {"order": ["127.0.0.1:A"], "counts": {"127.0.0.1:A": 1}}
        (signature,) = journey["requests"]
        assert signature == "GET A/?noise=0" and journey["requests"][signature]["headers"] == [["Host", "journey-a.test:A"], ["User-Agent", "smoke"], ["Cookie", "a=<v>"]]
        https = ab.phase_data(lab, "https.h1.v1")
        assert https["connects"]["order"] == ["127.0.0.1:h1"] and lab.roles[lab.h1.port] == "h1" and lab.roles[port_a] == "A"
        wire = ab.wire_summary(lab.h1, "https.h1.v1", lab.norm)
        (connection,) = wire["connections"]
        assert connection["alpn"] == "http/1.1" and connection["tls"]["extensions"]["server_name"] == ab.H1_NAME
        assert connection["http1"] == [{"request_line": "GET /wire.css HTTP/1.1", "headers": [["Host", "h1.lab.test:h1"], ["Accept", "text/css"]], "body_length": 0}]
        assert wire["quic"] == []
        assert ab.wire_summary(lab.h1, "journey.03", lab.norm) == {"connections": [], "quic": []}, "a connection belongs to the stage it arrived in"


def test_the_direct_report_counts_what_reached_the_witness_or_the_watcher_and_forgives_only_the_road_to_the_relay():
    import collections

    Direct = collections.namedtuple("Direct", "pid process protocol local remote status")

    class Watcher:
        def __init__(self, direct, pids=(1,)):
            self.direct, self.watched_pids, self.process_names = direct, set(pids), ["firefox.exe"]

    with ab.Lab(wire=True, host="127.0.0.1") as lab:
        clean = ab.direct_report(lab, Watcher([Direct(1, "firefox.exe", "udp", "x:1", "127.0.0.1:40999", "")]), None)
        assert clean == {"witness_tcp": [], "witness_udp": [], "watcher": [], "doh_direct": [], "resolver_trap": [],
                         "blind": []}, "a datagram to the address of the relay (a port that is not the HTTP/3 server's) is the road"
        leaks = ab.direct_report(lab, Watcher([Direct(1, "firefox.exe", "udp", "x:1", "127.0.0.1:%d" % lab.h3.port, ""),
                                               Direct(1, "firefox.exe", "tcp", "x:2", "203.0.113.9:443", "ESTABLISHED")]), None)
        assert leaks["watcher"] == ["tcp firefox -> 203.0.113.9:443", "udp firefox -> 127.0.0.1:h3"]
        blind = ab.direct_report(lab, Watcher([], pids=()), None)
        assert blind["blind"] and "never saw a browser" in blind["blind"][0]
        with socket.create_connection(("127.0.0.1", lab.witness.port), timeout=60):
            pass
        from _waits import wait_until

        assert wait_until(lambda: lab.witness.heard()["tcp"], 60, lambda: time.sleep(0.01)), "the witness never wrote the arrival down"
        assert ab.direct_report(lab, Watcher([]), None)["witness_tcp"] == ["tcp from 127.0.0.1"]
        trap = type("Trap", (), {"names": lambda self: ["leak-name.test"]})()
        assert ab.direct_report(lab, Watcher([]), trap)["resolver_trap"] == ["leak-name.test"]


def test_the_sentinel_signature_leaves_out_the_sentinel_and_the_idle_window_ends_when_two_rounds_agree():
    with ab.Lab(host="127.0.0.1") as lab:
        lab.rec.event("connect", target="127.0.0.1:%d" % lab.site.port)       # the sentinel's own road
        lab.rec.event("connect", target="192.0.2.53:443")
        lab.rec.event("udp", target="198.51.100.5:19302", message="stun-0x0001")
        lab.doh.queries.append(type("Q", (), {"name": ab.PAGE_NAME, "qtype": 1, "via": "lab"})())
        lab.doh.queries.append(type("Q", (), {"name": "background.test", "qtype": 1, "via": "lab"})())
        assert ab.sentinel_signature(lab) == {"connects": ["192.0.2.53:443"], "dns": ["background.test"], "udp": ["198.51.100.5:19302"]}
        lab.rec.event("connect", target="192.0.2.99:443")
        assert ab.sentinel_signature(lab)["connects"] == ["192.0.2.53:443", "192.0.2.99:443"]


def _fake_run_engine(monkeypatch, make):
    """Replace the process that runs one engine by a function that writes the transcript ``make(label)`` makes."""
    calls = []

    def run_engine(binary, seal, label, seeds, out):
        calls.append((binary, seal, label, seeds))
        ab.write_json(out, make(label))
        return 0

    monkeypatch.setattr(ab, "run_engine", run_engine)
    return calls


def _ab_args(tmp_path, *extra):
    empty = tmp_path / "volatile.json"
    empty.write_text(json.dumps({"format": ab.VOLATILE_FORMAT, "entries": []}), encoding="utf-8")
    return ["ab", "--out", str(tmp_path / "out"), "--published-binary", "published-ff", "--published-seal", "pub.seal.json", "--volatile", str(empty), "--seeds", "11,12", *extra]


def test_the_a_a_first_then_the_candidate_and_a_broken_control_stops_the_a_b(tmp_path, monkeypatch, capsys):
    calls = _fake_run_engine(monkeypatch, lambda label: transcript(label))
    assert ab.main(_ab_args(tmp_path, "--candidate", "cand-ff", "--candidate-seal", "cand.seal.json")) == 0
    assert [c[2] for c in calls] == ["firefox-68", "firefox-68-again", "candidate"], "the A/A runs first, the candidate last"
    assert calls[2][:2] == ("cand-ff", "cand.seal.json") and calls[0][:2] == ("published-ff", "pub.seal.json") and all(c[3] == (11, 12) for c in calls)
    names = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert names == ["diff-a-a.json", "diff.json", "transcript-68-again.json", "transcript-68.json", "transcript-candidate.json"]
    capsys.readouterr()

    def make(label):
        t = transcript(label)
        if label.endswith("-again"):
            t["seeds"]["11"]["webrtc"]["gathering"] = "noise"
        return t

    calls = _fake_run_engine(monkeypatch, make)
    out = tmp_path / "broken"
    args = _ab_args(tmp_path, "--candidate", "cand-ff", "--candidate-seal", "cand.seal.json")
    args[2] = str(out)
    assert ab.main(args) == 2, "an A/A that differs is a broken control"
    assert [c[2] for c in calls] == ["firefox-68", "firefox-68-again"], "the A/B is not run on a broken control"
    proposal = json.loads((out / "volatile-proposal.json").read_text(encoding="utf-8"))
    assert proposal["patterns"] == ["seeds/*/webrtc/gathering"]
    assert "AB NETWORK A/A FAILED" in capsys.readouterr().out

    calls = _fake_run_engine(monkeypatch, make)
    args = _ab_args(tmp_path, "--candidate", "cand-ff", "--candidate-seal", "cand.seal.json", "--measure")
    args[2] = str(tmp_path / "measure")
    assert ab.main(args) == 2 and [c[2] for c in calls] == ["firefox-68", "firefox-68-again", "candidate"]


def test_a_candidate_that_differs_from_a_sound_control_is_red_and_the_a_a_alone_needs_no_candidate(tmp_path, monkeypatch):
    def make(label):
        t = transcript(label)
        if label == "candidate":
            t["seeds"]["12"]["https"]["h2"]["v1"]["connections"][0]["tls"]["cipher_suites"].pop()
        return t

    _fake_run_engine(monkeypatch, make)
    assert ab.main(_ab_args(tmp_path, "--candidate", "cand-ff", "--candidate-seal", "cand.seal.json")) == 1
    diff = json.loads((tmp_path / "out" / "diff.json").read_text(encoding="utf-8"))
    assert diff["verdict"] == "red" and diff["differences"][0]["path"].endswith("/tls/cipher_suites/3")
    calls = _fake_run_engine(monkeypatch, lambda label: transcript(label))
    args = _ab_args(tmp_path, "--a-a")
    args[2] = str(tmp_path / "aa")
    assert ab.main(args) == 0 and [c[2] for c in calls] == ["firefox-68", "firefox-68-again"]
    assert ab.main(_ab_args(tmp_path)) == 2, "neither a candidate nor --a-a: nothing to run"
