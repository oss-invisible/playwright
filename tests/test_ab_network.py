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
            "journey": {"connects": {"order": ["127.0.0.1:A", "127.0.0.1:O"], "counts": {"127.0.0.1:A": 2, "127.0.0.1:O": 1}},
                        "dns": {"order": ["journey-a.test"], "types": {"journey-a.test": [1]}},
                        "steps": {"01": {"name": "open home", "ok": True, "idle": True, "url": "A/",
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
    b["seeds"]["11"]["journey"]["connects"]["counts"]["127.0.0.1:A"] = 3
    b["seeds"]["12"]["webrtc"]["gathering"] = "new"
    entry = {"pattern": "seeds/*/journey/connects/counts/*", "reason": "speculative connections", "aa_run": "run 1"}
    report = compare(transcript(), b, [entry])
    assert [d["path"] for d in report["differences"]] == ["seeds/12/webrtc/gathering"]
    assert report["excused"] == 4 and report["volatile"] == [{"pattern": entry["pattern"], "leaves": 4, "differing": 1}]
    assert report["leaves"] == compare(transcript(), b)["leaves"] - 4


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
    b["seeds"]["11"]["journey"]["connects"]["counts"]["127.0.0.1:A"] = 3
    b["seeds"]["12"]["journey"]["connects"]["counts"]["127.0.0.1:A"] = 4
    assert ab.propose_volatile(compare(transcript(), b)) == ["seeds/*/journey/connects/counts/127.0.0.1:A"]
    c = transcript("c")
    c["seeds"]["11"]["journey"]["steps"]["01"]["requests"]["GET A/"]["count"] = 2
    c["seeds"]["12"]["journey"]["steps"]["01"]["requests"]["GET A/"]["count"] = 2
    assert ab.propose_volatile(compare(transcript(), c)) == ["seeds/*/journey/steps/*/requests/GET A//count"]


# ------------------------------------------------------------------------------------------------------------------ the volatile list
def test_the_volatile_list_of_the_repository_is_well_formed_and_every_entry_names_its_evidence():
    entries = ab.load_volatile()
    assert all(e["aa_run"].strip() and e["reason"].strip() for e in entries)


def test_the_entries_of_the_repository_list_name_the_counts_the_grease_aead_and_the_optional_resumptions_and_nothing_next_to_them():
    import fnmatch

    entries = ab.load_volatile()
    patterns = [e["pattern"] for e in entries if not e.get("optional")]
    owners = [e["pattern"] for e in entries if e.get("optional")]
    assert sorted(owners) == ["seeds/*/h3/wire/resumed_connections", "seeds/*/https/*/wire/resumed_connections"]
    excused = lambda path: [q for q in patterns if fnmatch.fnmatchcase(path, q)]  # noqa: E731
    assert excused("seeds/0/journey/connects/counts/192.0.2.53:443") and excused("seeds/0/webrtc/connects/counts/192.0.2.53:443")
    ech = "/tls/extensions/encrypted_client_hello/"
    for role in ("https/h2", "https/h1", "h3"):
        for kind in ("connections", "resumed_connections"):
            base = "seeds/0/%s/wire/%s/0%s" % (role, kind, ech)
            assert excused(base + "aead") and not excused(base + "kdf") and not excused(base + "payload_length") and not excused(base + "type"), base
    for kept in ("seeds/0/journey/connects/order/0", "seeds/0/journey/dns/order/0", "seeds/0/journey/steps/01/requests/GET A//count", "seeds/0/journey/steps/01/requests/GET A//headers/0/1",
                 "seeds/0/journey/steps/01/requests/POST A/beacon?t=pagehide&n=2/count", "seeds/0/journey/steps/01/udp/counts/198.51.100.5:19302", "seeds/0/lab/launch/connects/order/0",
                 "seeds/0/webrtc/connects/order/0", "seeds/0/h3/udp/order/0", "seeds/0/https/h2/wire/protocols/0", "seeds/0/https/h2/wire/connections/0/tls/extensions/server_name"):
        assert not excused(kept), kept


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


def test_a_port_inside_a_url_parameter_and_the_token_of_the_slow_frame_are_normalised_so_two_loads_of_the_page_write_the_same_key():
    """A/A run 44946411675: the ad frame is told its sibling's origin as ``a=http%3A%2F%2Fjourney-a.test%3A<port>`` and the slow frame holds ``id=sf<token><i>``: both
    differ at every load, so the same request had a different key in each run and 5,500 leaves differed."""
    first, second = ab.Normalizer({41039: "A", 41040: "O"}), ab.Normalizer({46001: "A", 46002: "O"})
    ad = "/jf/ad?n=1&a=http%3A%2F%2Fjourney-a.test%3A"
    assert first.text(ad + "41039") == second.text(ad + "46001") == ad + "A"
    assert first.text("/jf/ad?n=0&a=http%3A%2F%2Fjourney-o.test%3a41040") == "/jf/ad?n=0&a=http%3A%2F%2Fjourney-o.test%3AO", "the encoded colon may be written in lower case"
    assert first.text("/hold?id=sfkjpsqn0") == second.text("/hold?id=sfx7q2m90") == "/hold?id=sf<TOK>0"
    assert first.text("/release?id=sfab12c32") == "/release?id=sf<TOK>2", "a token that ends in a digit leaves the index alone"
    assert first.text("/hold?id=sfkjpsqn0") != first.text("/hold?id=sfkjpsqn1"), "the index of the request is not random: three held requests stay three keys"
    assert first.text("/x?a=http%3A%2F%2Fother.test%3A9999") == "/x?a=http%3A%2F%2Fother.test%3A9999", "a number that is no port of the lab is left as it is"
    assert first.text("/r?kind=json&id=late3&u=sfun") == "/r?kind=json&id=late3&u=sfun"


def test_two_loads_of_the_journey_write_the_same_request_summary_and_a_request_that_really_differs_is_still_red():
    def requests(port_a, port_o, token):
        norm = ab.Normalizer({port_a: "A", port_o: "O"})
        head = lambda target, extra=(): {"role": "O", "method": "GET", "target": target, "headers": [["Host", "journey-o.test:%d" % port_o], *extra], "body_length": 0}  # noqa: E731
        return ab.request_summary([head("/hold?id=sf%s%d" % (token, i)) for i in range(3)] + [head("/release?id=sf%s%d" % (token, i)) for i in range(3)]
                                  + [head("/jf/ad?n=0&a=http%%3A%%2F%%2Fjourney-a.test%%3A%d" % port_a, [["Referer", "http://journey-o.test:%d/" % port_o]])], norm)

    one, two = requests(41039, 41040, "kjpsqn"), requests(46001, 46002, "x7q2m9")
    assert one == two and list(one) == ["GET O/hold?id=sf<TOK>0", "GET O/hold?id=sf<TOK>1", "GET O/hold?id=sf<TOK>2", "GET O/jf/ad?n=0&a=http%3A%2F%2Fjourney-a.test%3AA",
                                         "GET O/release?id=sf<TOK>0", "GET O/release?id=sf<TOK>1", "GET O/release?id=sf<TOK>2"]
    a, b = transcript(), transcript("b")
    for t, summary in ((a, one), (b, two)):
        t["seeds"]["11"]["journey"]["steps"]["01"]["requests"] = summary
    assert compare(a, b)["verdict"] == "green"
    two["GET O/jf/ad?n=0&a=http%3A%2F%2Fjourney-a.test%3AA"]["headers"][1] = ["Referer", "http://journey-o.test:O/other"]
    report = compare(a, b)
    (difference,) = report["differences"]
    assert report["verdict"] == "red" and difference["path"] == "seeds/11/journey/steps/01/requests/GET O/jf/ad?n=0&a=http%3A%2F%2Fjourney-a.test%3AA/headers/1/1"
    assert difference["a"] == "http://journey-o.test:O/" and difference["b"] == "http://journey-o.test:O/other"


def test_the_error_scan_names_a_failure_of_the_run_and_not_the_text_a_page_answered():
    seeds = {"0": {"journey": {"steps": {"14": {"extra": {"error": "Wrong user or password.", "user": "alice"}, "ok": True},
                                         "15": {"ok": False, "error": "TimeoutError: Page.goto: Timeout Nms exceeded"}}},
                   "h3": {"error": "RuntimeError: no quic"}, "https": {"h2": {"v1": {"connections": [{"error": "ConnectionResetError"}]}}}}}
    assert ab.transcript_errors(seeds) == ["seeds/0/h3/error", "seeds/0/https/h2/v1/connections/0/error", "seeds/0/journey/steps/15/error"]
    assert ab.transcript_errors({"0": {"journey": {"steps": {"14": {"extra": {"error": "Wrong user or password."}}}}}}) == []


def _journey_run(monkeypatch, connects_by_step, queries_by_step=None):
    """``ab.walk_journey`` with the driver replaced by a function that, in each step, makes the browser's connections and DNS questions arrive to the lab as the events that
    ``connects_by_step`` and ``queries_by_step`` say ({step: [target, ...]}); the requests of every step are the same."""
    from fakeweb import journey_site, profile_p4

    queries_by_step = queries_by_step or {}
    with ab.Lab(journey=True, host="127.0.0.1") as lab:
        port_a = int(lab.origin_a.rsplit(":", 1)[1])

        def drive(browser, origin_a, origin_o, workers, timeout):
            records = []
            for n in (1, 2, 3):
                journey_site.STATE.set_step(n)
                lab.rec.event("http", role="A", method="GET", target="/page%d" % n, headers=[["Host", "journey-a.test:%d" % port_a]], body_length=0)
                for target in connects_by_step.get(n, []):
                    lab.rec.event("connect", target=target.replace("<A>", "127.0.0.1:%d" % port_a))
                for name in queries_by_step.get(n, []):
                    lab.doh.queries.append(type("Q", (), {"name": name, "qtype": 1, "via": "lab"})())
                records.append({"n": n, "name": "step %d" % n, "ok": True, "idle": True, "url": "A/", "title": "t", "marker": "m", "cookies": [], "history_length": n, "pages": 1,
                                "extra": {}})
            return records

        monkeypatch.setattr(profile_p4, "drive", drive)
        return ab.walk_journey(object(), lab)


def test_a_connection_or_a_name_that_lands_in_another_step_of_the_journey_is_the_same_journey_and_a_new_destination_is_not(monkeypatch):
    """A/A run 44946411675: the browser's own connections and its second lookup of a name (the answer lives 30 s) were written under the step that was current, a different one in
    each run; 40 leaves differed that way on Linux and 60 on Windows. The CONNECTs and the DNS questions are one summary of the whole journey; the requests stay in their step."""
    doh = "192.0.2.53:443"
    first = _journey_run(monkeypatch, {1: [doh, "<A>", doh], 3: ["<A>"]}, {1: ["journey-a.test"], 3: ["journey-a.test"]})
    second = _journey_run(monkeypatch, {1: [doh], 2: ["<A>", "<A>"], 3: [doh, doh]}, {1: ["journey-a.test"], 2: ["journey-a.test"]})
    assert "order" not in first and set(first) == {"steps", "connects", "dns"}
    assert all(set(step) >= {"requests", "udp"} and "connects" not in step and "dns" not in step for step in first["steps"].values())
    assert first["connects"]["order"] == second["connects"]["order"] == [doh, "127.0.0.1:A"]
    assert first["dns"] == second["dns"] == {"order": ["journey-a.test"], "types": {"journey-a.test": [1]}}
    assert first["connects"]["counts"] != second["connects"]["counts"], "the pool's counts do differ, and the volatile list says so"
    a, b = transcript(), transcript("b")
    a["seeds"]["11"]["journey"], b["seeds"]["11"]["journey"] = first, second
    volatile = [{"pattern": "seeds/*/journey/connects/counts/*", "reason": "the pool", "aa_run": "x"}]
    report = compare(a, b, volatile)
    assert report["verdict"] == "green" and report["differences"] == [] and report["excused"] == 4
    third = _journey_run(monkeypatch, {1: [doh, "<A>", "192.0.2.77:443"]}, {1: ["journey-a.test"]})
    c = transcript("c")
    c["seeds"]["11"]["journey"] = third
    red = compare(a, c, volatile)
    assert red["verdict"] == "red"
    assert {"path": "seeds/11/journey/connects/order/2", "a": ab.MISSING, "b": "192.0.2.77:443"} in red["differences"]


def test_the_walk_waits_for_what_a_closed_tab_sends_as_it_leaves_so_the_requests_belong_to_the_step_that_closed_it(monkeypatch):
    """A/A run 44982522362, Windows seed 6: the tab of step 10 is closed by the walk; its two pagehide requests reached the site after the walk had moved to step 11 in 19 launches
    of 20, and before it in one (n=2 in step 10, n=1 in step 11). In every launch both were sent. The walk now goes on when the site has both."""
    import time
    from types import SimpleNamespace

    from fakeweb import journey_site, profile_p4

    state = journey_site.State()
    monkeypatch.setattr(journey_site, "STATE", state)
    arrivals = [lambda: state.record(1, "POST", "/beacon", {"t": ["pagehide"], "n": ["1"]}), lambda: state.record(1, "POST", "/beacon", {"t": ["pagehide"], "n": ["2"]})]
    polls = []

    def sleep(period):
        """The losing interleaving, built on purpose: the walk is already waiting when the requests reach the site, one at each look of its wait: nothing is timed."""
        polls.append(period)
        if arrivals:
            arrivals.pop(0)()

    monkeypatch.setattr(profile_p4, "time", SimpleNamespace(monotonic=time.monotonic, sleep=sleep))

    class Tab:
        url = "http://journey-o.test:1/partner/8"

        def wait_for_load_state(self, *a, **k):
            pass

        def wait_for_selector(self, *a, **k):
            pass

        def inner_text(self, *a, **k):
            return "Partner"

        def wait_for_function(self, *a, **k):
            pass  # the walk leaves the tab from its one point (Journey.leave), once the page has declared its noise done

        def close(self):
            pass  # the requests of the leaving page come after close() has returned, while the walk waits

    tab = Tab()
    ctx = SimpleNamespace(expect_page=lambda: SimpleNamespace(__enter__=None), pages=[])

    class Info:
        value = tab

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    ctx.expect_page = lambda: Info()
    journey = profile_p4.Journey.__new__(profile_p4.Journey)
    journey.ctx, journey.extra = ctx, {}
    journey.page = SimpleNamespace(click=lambda selector: None)
    got, cleanup = journey.s10_blank_tab()
    assert got is tab
    state.set_step(10)
    cleanup()
    assert state.step == 10 and [e["n"] for e in state.events if e["id"] == "pagehide"] == ["1", "2"] and {e["step"] for e in state.events} == {10}, "both are in the step that closed the tab"
    assert len(polls) == 2, "the walk looked until it had both, and no more"
    # no pagehide page (noise off): the walk does not wait for what will not come
    Tab.url = "http://journey-o.test:1/partner/8?noise=0"
    got, cleanup = journey.s10_blank_tab()
    polls.clear()
    cleanup()
    assert polls == [], "no wait at all"
    # a request that never comes does not block the walk for ever, and the site shows it missing
    monkeypatch.setattr(profile_p4, "LEAVING_FAILSAFE_S", 0.2)
    state.events.clear()
    assert profile_p4.wait_for_pagehide_requests(2, failsafe=0.2) is False and profile_p4.pagehide_requests() == 0


def test_no_two_servers_of_a_lab_share_a_port_number_and_a_lab_that_has_two_is_made_again():
    """A/A run 44982522362, Linux seed 4: the port of the origin O and the port of the HTTP/3 server were the same number, so 667 leaves read `journey-o.test:h3`."""
    with ab.Lab(journey=True, wire=True, host="127.0.0.1") as lab:
        assert lab.collisions == [] and len(set(lab.roles)) == len(lab.roles)
        before = dict(lab.roles)
        lab._claim(lab.h3.port, "h3")
        assert lab.collisions == [], "the same role claiming its own port again is no collision"
        lab._claim(int(lab.origin_o.rsplit(":", 1)[1]), "h3")
        assert lab.collisions == [(int(lab.origin_o.rsplit(":", 1)[1]), "O", "h3")]
        assert before != lab.roles
    made = []

    class Flaky:
        def __init__(self, **options):
            made.append(options)
            if len(made) < 3:
                raise ab.PortCollision("%d: O and h3" % len(made))

    original = ab.Lab
    ab.Lab = Flaky
    try:
        assert isinstance(ab.open_lab(journey=True), Flaky) and len(made) == 3
        made.clear()
        ab.Lab = type("Never", (), {"__init__": lambda self, **o: (_ for _ in ()).throw(ab.PortCollision("1: O and h3"))})
        with pytest.raises(RuntimeError, match="in a row"):
            ab.open_lab()
    finally:
        ab.Lab = original


def _tls(resumed=False, aead=1, cipher="0x1301"):
    hello = copy.deepcopy(wc.parse_client_hello(build_hello()))
    hello["cipher_suites"] = [cipher] + hello["cipher_suites"][1:]
    hello["extensions"]["encrypted_client_hello"] = {"type": 0, "kdf": 1, "aead": aead, "enc_length": 32, "payload_length": 528 if resumed else 240}
    hello["extensions"].pop("pre_shared_key", None)
    if resumed:
        hello["extensions"]["pre_shared_key"] = {"identities": 1, "binder_lengths": [48]}
    return hello


def _headers(path, kind="*/*", pseudo=(":method", ":path", ":authority", ":scheme")):
    values = {":method": "GET", ":path": path, ":authority": "h3.lab.test:h3", ":scheme": "https"}
    return [[n, values[n]] for n in pseudo] + [["user-agent", "Mozilla/5.0"], ["accept", kind], ["accept-language", "en-US,en;q=0.9"]]


def _h2(tls, requests, frames_after=3, priority=None, pseudo=(":method", ":path", ":authority", ":scheme")):
    """An HTTP/2 connection as ``wire_summary`` writes it; ``requests`` is [(stream, path)]."""
    return {"alpn": "h2", "tls": tls, "h2": {
        "settings": [[1, 65536], [2, 0], [4, 131072], [5, 16384]], "frame_types_before_the_first_request": ["SETTINGS", "WINDOW_UPDATE"],
        "window_updates_before_the_first_request": [{"stream": 0, "increment": 12517377}], "priority": [], "priority_update": [], "settings_ack_sent_by_client": True,
        "frames_after_the_first_request": frames_after,
        "headers_priority": [{"depends_on": 0, "exclusive": False, "stream": n, "weight": (priority or {}).get(path, 22)} for n, path in requests],
        "requests": [{"stream": n, "pseudo_order": list(pseudo), "header_names": ["user-agent", "accept", "accept-language"], "headers": _headers(path, pseudo=pseudo), "end_stream": True}
                     for n, path in requests]}}


def _quic(requests, pseudo=(":method", ":scheme", ":authority", ":path")):
    return {"alpn": "h3", "transport_parameters": {"max_idle_timeout": 30000}, "h3_settings": [["QPACK_MAX_TABLE_CAPACITY", 65536]],
            "requests": [{"pseudo_order": list(pseudo), "header_names": ["user-agent", "accept", "accept-language"], "headers": _headers(path, pseudo=pseudo)} for path in requests]}


def test_an_optional_entry_excuses_what_one_run_has_and_the_other_has_not_and_compares_what_both_have():
    """A/A run 44982522362: an origin had a resumed connection in one run and none in the other (108 leaves per seed)."""
    base = {"connections": [_h2(_tls(), [(3, "/a"), (5, "/b")])], "quic": []}
    with_resumption = {"connections": [_h2(_tls(), [(3, "/a")]), _h2(_tls(resumed=True), [(3, "/b")])], "quic": []}
    entry = {"pattern": "seeds/*/https/*/wire/resumed_connections", "optional": True, "reason": "ticket", "aa_run": "x"}

    def side(label, wire, other=None):
        t = transcript(label)
        t["seeds"]["11"]["https"] = {"h2": {"wire": ab.fold_wire(wire)}}
        if other is not None:
            t["seeds"]["12"]["https"] = {"h2": {"wire": ab.fold_wire(other)}}
        return t

    one_sided = compare(side("a", base, with_resumption), side("b", with_resumption, base), [entry])
    assert one_sided["verdict"] == "green" and one_sided["differences"] == [] and one_sided["excused"] > 0 and one_sided["stale"] == []
    assert one_sided["volatile"][0]["differing"] > 0
    assert compare(side("a", base, with_resumption), side("b", with_resumption, base))["verdict"] == "red", "without the entry the resumption is a difference"
    both = compare(side("a", with_resumption, with_resumption), side("b", with_resumption, with_resumption), [entry])
    assert both["verdict"] == "green" and both["excused"] == 0, "the same resumption on both sides is compared, not excused"
    changed = copy.deepcopy(with_resumption)
    changed["connections"][1]["tls"]["extensions"]["pre_shared_key"]["binder_lengths"] = [32]
    red = compare(side("a", with_resumption, with_resumption), side("b", changed, with_resumption), [entry])
    assert red["verdict"] == "red" and any("resumed_connections/0/tls/extensions/pre_shared_key/binder_lengths/0" in d["path"] for d in red["differences"])
    neither = compare(side("a", base), side("b", base), [entry])
    assert neither["verdict"] == "green" and neither["excused"] == 1 and neither["stale"] == [], "an origin with none on both sides has the owner leaf (an empty list), so the entry is not stale"
    assert compare(side("a", base), side("b", base), [dict(entry, pattern="seeds/*/https/*/wire/nothing_here")])["stale"] == ["seeds/*/https/*/wire/nothing_here"]
    # a fresh connection that differs is red whatever the entry says
    other = copy.deepcopy(with_resumption)
    other["connections"][0]["tls"] = _tls(cipher="0x1302")
    assert compare(side("a", with_resumption), side("b", other), [entry])["verdict"] == "red"


def test_a_connection_whose_clienthello_differs_is_red_and_so_is_a_request_that_differs_and_a_pseudo_header_order_that_changed():
    paths = ["/?v=1", "/wire.css"]
    base = {"connections": [_h2(_tls(), [(3, paths[0]), (5, paths[1])])], "quic": []}
    a = transcript()
    a["seeds"]["11"]["https"] = {"h2": {"wire": ab.fold_wire(base)}}

    def red(change):
        summary = copy.deepcopy(base)
        change(summary)
        b = transcript("b")
        b["seeds"]["11"]["https"] = {"h2": {"wire": ab.fold_wire(summary)}}
        report = compare(a, b)
        assert report["verdict"] == "red"
        return [d["path"] for d in report["differences"]]

    paths_cipher = red(lambda s: s["connections"][0].update(tls=_tls(cipher="0x1302")))
    assert any("/wire/connections/0/tls/cipher_suites/0" in p for p in paths_cipher), paths_cipher
    swapped = red(lambda s: s["connections"][0]["h2"].update(settings=[[1, 65536], [2, 0], [4, 262144], [5, 16384]]))
    assert any("/h2/settings/2/1" in p for p in swapped)
    header = red(lambda s: s["connections"][0]["h2"]["requests"][1]["headers"].__setitem__(4, ["accept", "text/css"]))
    assert any(p.startswith("seeds/11/https/h2/wire/requests/") for p in header)
    weight = red(lambda s: s["connections"][0]["h2"]["headers_priority"][1].update(weight=12))
    assert any("/priority/weight" in p for p in weight)
    missing = red(lambda s: s["connections"][0]["h2"]["requests"].pop())
    assert missing
    order = ab.fold_wire({"connections": [_h2(_tls(), [(3, paths[0])], pseudo=(":method", ":scheme", ":authority", ":path"))], "quic": []})
    assert order["connections"][0]["pseudo_orders"] == [[":method", ":scheme", ":authority", ":path"]]
    assert order != ab.fold_wire({"connections": [_h2(_tls(), [(3, paths[0])])], "quic": []}), "the order of the pseudo-headers is the engine's"


def _h3_run(visits, message="quic-short"):
    """``ab.walk_h3`` with a page that answers h2 until the last visit and h3 on it, and a QUIC server that has seen a connection; ``visits`` is the datagrams the relay carries
    in each visit's stage (the last ones of one visit arrive in the next one's, which is what the A/A showed)."""
    from types import SimpleNamespace

    rec = ab.Recorder()
    rec.attach("doh", [])
    lab = SimpleNamespace(rec=rec, norm=ab.Normalizer({}), h3=SimpleNamespace(port=5, quic=[object()]))
    state = {"visit": 0}

    class Page:
        def goto(self, url, **options):
            state["visit"] += 1
            for _ in range(visits[state["visit"] - 1]):
                rec.event("udp", target="127.0.0.1:5", message=message)

        def evaluate(self, script):
            return "h3" if state["visit"] == len(visits) else "h2"

        def wait_for_timeout(self, ms):
            pass

    class Context:
        def new_page(self):
            return Page()

        def close(self):
            pass

    browser = SimpleNamespace(new_context=lambda **kw: Context())
    original = ab.wire_summary
    ab.wire_summary = lambda server, phase, norm: {"connections": [], "quic": []}
    try:
        return ab.walk_h3(browser, lab)
    finally:
        ab.wire_summary = original


def test_the_datagrams_of_the_http3_walk_are_summarised_over_the_walk_and_by_kind_not_by_number():
    """A/A runs 44946411675 and 44960767715: 12+8 and 13+7, 23 and 22, 16 and 25 datagrams for the same walk: how many QUIC sends follows its acknowledgements and timers."""
    one, two, more = _h3_run([12, 8]), _h3_run([13, 7]), _h3_run([13, 8])
    assert one == two == more and one["visits"] == ["h2", "h3"]
    assert one["udp"] == {"order": ["127.0.0.1:5"], "messages": {"127.0.0.1:5": ["quic-short"]}} and all("udp" not in one[v] for v in ("v1", "v2"))
    assert _h3_run([12, 8], message="quic-long")["udp"] != one["udp"], "another kind of datagram is a difference"


def test_the_pool_does_not_show_in_the_fold_of_a_walk_the_number_of_connections_the_streams_the_frames_and_where_a_request_rode_are_not_kept():
    """A/A run 44960767715: Linux had a second connection with no request in one run (and in another stage); Windows had the page's requests 4 and 1 on HTTP/2 and HTTP/3 in one run, 1 and 4 in the other."""
    paths = ["/?v=1", "/wire.css", "/wire.js", "/wire.svg", "/favicon.ico"]
    weight = {"/?v=1": 42}
    one = {"connections": [_h2(_tls(aead=1), [(3 + 2 * i, p) for i, p in enumerate(paths[:4])], frames_after=9, priority=weight), _h2(_tls(resumed=True), [])], "quic": [_quic(paths[4:])]}
    two = {"connections": [_h2(_tls(aead=3), [(3, paths[0])], frames_after=3, priority=weight)], "quic": [_quic(paths[1:])]}
    three = {"connections": [_h2(_tls(aead=1), [(3, paths[0]), (5, paths[1])], priority=weight), _h2(_tls(aead=3), [(3, p) for p in paths[2:4]], frames_after=1),
                             _h2(_tls(resumed=True), [])], "quic": [_quic(paths[4:]), _quic([])]}
    folds = [ab.fold_wire(x, transport_race=True) for x in (one, two, three)]
    for f in folds:
        assert [c["transport"] for c in f["connections"]] == ["tcp", "quic"] and sum(r["count"] for r in f["requests"]) == 5
        assert all("frames_after_the_first_request" not in json.dumps(c) and "headers_priority" not in json.dumps(c) for c in f["connections"])
    assert folds[0]["requests"] == folds[1]["requests"] == folds[2]["requests"]
    assert [c["h2"] for c in folds[0]["connections"] if c["transport"] == "tcp"] == [c["h2"] for c in folds[2]["connections"] if c["transport"] == "tcp"]
    a, b, c = transcript(), transcript("b"), transcript("c")
    for t, wire in ((a, folds[0]), (b, folds[1]), (c, folds[2])):
        t["seeds"]["11"]["h3"] = {"wire": wire}
    volatile = [{"pattern": "seeds/*/h3/wire/connections/*/tls/extensions/encrypted_client_hello/aead", "reason": "grease", "aa_run": "x"}]
    for other in (b, c):
        report = compare(a, other, volatile)
        assert report["verdict"] == "green" and report["differences"] == [], report["differences"][:3]
    # outside the race (a role that is only HTTP/2) every request keeps its priority and its end of stream
    plain = ab.fold_wire(one)
    assert all("priority" in r for r in plain["requests"] if r["pseudo"][":path"] != "/favicon.ico")


def test_the_resumed_clienthellos_are_a_set_of_their_own_and_a_resumption_that_differs_is_red():
    plain = {"connections": [_h2(_tls(), [(3, "/a")]), _h2(_tls(resumed=True), [(3, "/b")])], "quic": []}
    one = ab.fold_wire(plain)
    twice = ab.fold_wire({"connections": plain["connections"] + [_h2(_tls(resumed=True, aead=3), [(3, "/c")]), _h2(_tls(), [(3, "/d")])], "quic": []})
    assert len(one["resumed_connections"]) == 1 and len(twice["resumed_connections"]) == 1 and len(twice["connections"]) == 1, "the same resumption twice is one fingerprint"
    assert "pre_shared_key" in one["resumed_connections"][0]["tls"]["extensions"] and "pre_shared_key" not in one["connections"][0]["tls"]["extensions"]
    assert "identities" not in json.dumps(one["resumed_connections"])
    more_tickets = copy.deepcopy(plain)
    more_tickets["connections"][1]["tls"]["extensions"]["pre_shared_key"]["identities"] = 2
    assert ab.fold_wire(more_tickets) == one, "how many tickets were offered is the pool's"
    a, b = transcript(), transcript("b")
    a["seeds"]["11"]["https"], b["seeds"]["11"]["https"] = {"h2": {"wire": one}}, {"h2": {"wire": twice}}
    other = copy.deepcopy(plain)
    other["connections"][1]["tls"]["extensions"]["pre_shared_key"]["binder_lengths"] = [32]
    changed = ab.fold_wire(other)
    assert changed["resumed_connections"] != one["resumed_connections"] and changed["connections"] == one["connections"]
    c = transcript("c")
    c["seeds"]["11"]["https"] = {"h2": {"wire": changed}}
    red = compare(a, c)
    assert red["verdict"] == "red" and any("/wire/resumed_connections/0/tls/extensions/pre_shared_key/binder_lengths/0" in d["path"] for d in red["differences"])
    gone = ab.fold_wire({"connections": [_h2(_tls(), [(3, "/a")]), _h2(_tls(resumed=True), [])], "quic": []})
    assert gone["resumed_connections"] == [] != one["resumed_connections"], "a resumption that no longer happens on a connection that asks is a difference"


def test_what_a_transport_adds_to_a_request_on_its_own_is_the_connections_in_a_race_and_a_change_of_it_is_red():
    """A/A run 44960767715, Windows: /wire.css with `te: trailers` over HTTP/2 in one run, with `alt-used` before the referer over HTTP/3 in the other."""
    def over_h2(path):
        c = _h2(_tls(), [(3, path)])
        c["h2"]["requests"][0]["headers"].append(["te", "trailers"])
        return c

    def over_quic(path, alt="h3.lab.test:h3"):
        q = _quic([path])
        q["requests"][0]["headers"].insert(7, ["alt-used", alt])
        return q

    doc = "/?v=1"
    one = ab.fold_wire({"connections": [over_h2(doc), over_h2("/wire.css")], "quic": [over_quic("/favicon.ico")]}, transport_race=True)
    two = ab.fold_wire({"connections": [over_h2(doc)], "quic": [over_quic("/wire.css"), over_quic("/favicon.ico")]}, transport_race=True)
    assert one["requests"] == two["requests"] and one["connections"] == two["connections"]
    assert all(h[0] not in ("te", "alt-used") for r in one["requests"] for h in r["headers"])
    assert [c["transport_headers"] for c in one["connections"]] == [["te: trailers"], ["alt-used: h3.lab.test:h3"]]
    other = ab.fold_wire({"connections": [over_h2(doc)], "quic": [over_quic("/favicon.ico", alt="elsewhere:1")]}, transport_race=True)
    assert [c["transport_headers"] for c in other["connections"]][1] == ["alt-used: elsewhere:1"] and other["connections"] != two["connections"]
    assert ab.fold_wire({"connections": [over_h2("/wire.css")], "quic": []})["requests"][0]["headers"][-1] == ["te", "trailers"], "outside a race the request keeps every header"


def test_a_resumption_that_never_happens_in_a_run_is_red_whatever_the_timing_of_the_tickets():
    """A thing that may come and go per launch (a resumption) must still happen in every run: an engine that stops resuming stays red."""
    base = {"connections": [_h2(_tls(), [(3, "/a"), (5, "/b")])], "quic": []}
    resumes = {"connections": [_h2(_tls(), [(3, "/a")]), _h2(_tls(resumed=True), [(3, "/b")])], "quic": []}
    entry = {"pattern": "seeds/*/https/*/wire/resumed_connections", "optional": True, "reason": "ticket", "aa_run": "x"}

    def run(label, per_seed):
        t = transcript(label)
        for seed, wire in per_seed.items():
            t["seeds"][str(seed)]["https"] = {"h1": {"wire": ab.fold_wire(wire)}}
        return t

    with_some = run("a", {11: resumes, 12: base})
    timing = run("b", {11: base, 12: resumes})
    report = compare(with_some, timing, [entry])
    assert report["verdict"] == "green" and report["invariants"] == [], "which seed resumes follows the timing"
    never = run("c", {11: base, 12: base})
    red = compare(with_some, never, [entry])
    assert red["verdict"] == "red" and len(red["invariants"]) == 1
    assert "a has something under seeds/*/https/h1/wire/resumed_connections" in red["invariants"][0] and "c never has" in red["invariants"][0]
    assert any("never has" in p for p in red["problems"])
    assert compare(never, with_some, [entry])["verdict"] == "red", "and the other way round: an engine that starts to resume where the published one does not"
    both_never = compare(never, run("d", {11: base, 12: base}), [entry])
    assert both_never["verdict"] == "green" and both_never["invariants"] == [], "an origin that never resumes in the published engine is not an invariant"


def test_two_connections_that_differ_only_by_the_random_aead_are_one_fingerprint():
    one = ab.fold_wire({"connections": [_h2(_tls(aead=1), [(3, "/a")]), _h2(_tls(aead=3), [(3, "/b")]), _h2(_tls(resumed=True, aead=3), [(3, "/c")])], "quic": []})
    two = ab.fold_wire({"connections": [_h2(_tls(aead=3), [(3, "/a")]), _h2(_tls(resumed=True, aead=1), [(3, "/c")]), _h2(_tls(aead=3), [(3, "/b")])], "quic": []})
    assert len(one["connections"]) == len(two["connections"]) == 1 and len(one["resumed_connections"]) == len(two["resumed_connections"]) == 1
    assert one["requests"] == two["requests"]


def test_the_protocols_that_carried_a_request_are_a_set_per_role_and_an_engine_that_drops_or_adds_one_is_red():
    quic = [_quic(["/wire.css"])]
    both = ab.fold_wire({"connections": [_h2(_tls(), [(3, "/?v=1")])], "quic": quic}, transport_race=True)
    split = ab.fold_wire({"connections": [_h2(_tls(), [(3, "/?v=1"), (5, "/wire.css")])], "quic": [_quic([])]}, transport_race=True)
    many = ab.fold_wire({"connections": [_h2(_tls(), [(3, "/?v=1")])], "quic": [_quic(["/wire.css"]), _quic(["/wire.js"])]}, transport_race=True)
    assert both["protocols"] == many["protocols"] == ["h2", "h3"] and split["protocols"] == ["h2"], "a QUIC connection that carried no request is not a protocol seen"
    a, b = transcript(), transcript("b")
    a["seeds"]["11"]["h3"], b["seeds"]["11"]["h3"] = {"wire": both}, {"wire": many}
    assert not [d for d in compare(a, b)["differences"] if "/protocols" in d["path"]], "how many requests rode each protocol is not compared"
    b["seeds"]["11"]["h3"] = {"wire": split}
    red = compare(a, b)
    assert red["verdict"] == "red" and {"path": "seeds/11/h3/wire/protocols/1", "a": "h3", "b": ab.MISSING} in red["differences"]
    h1 = ab.fold_wire({"connections": [{"alpn": "http/1.1", "tls": _tls(), "http1": [{"request_line": "GET / HTTP/1.1", "headers": [["Host", "x"]], "body_length": 0}]}], "quic": []})
    h2 = ab.fold_wire({"connections": [_h2(_tls(), [(3, "/")])], "quic": []})
    assert h1["protocols"] == ["h1"] and h2["protocols"] == ["h2"]
    c = transcript("c")
    c["seeds"]["11"]["https"], a["seeds"]["11"]["https"] = {"h2": {"wire": h1}}, {"h2": {"wire": h2}}
    assert any(d["path"] == "seeds/11/https/h2/wire/protocols/0" and d["a"] == "h2" and d["b"] == "h1" for d in compare(a, c)["differences"]), "h1 where the published engine used h2"


def test_a_connection_that_carried_no_request_or_resumed_one_is_out_of_the_fingerprints_and_the_requests_it_carried_stay():
    quiet = ab.fold_wire({"connections": [_h2(_tls(), [(3, "/a")]), _h2(_tls(resumed=True), [])], "quic": []})
    alone = ab.fold_wire({"connections": [_h2(_tls(), [(3, "/a")])], "quic": []})
    resumed = ab.fold_wire({"connections": [_h2(_tls(), [(3, "/a")]), _h2(_tls(resumed=True), [(3, "/b")])], "quic": []})
    assert quiet == alone and len(resumed["connections"]) == 1 and [r["pseudo"][":path"] for r in resumed["requests"]] == ["/a", "/b"]
    assert quiet["resumed_connections"] == [] and len(resumed["resumed_connections"]) == 1
    errors = ab.fold_wire({"connections": [{"alpn": "h2", "tls": _tls(), "error": "ConnectionResetError"}], "quic": []})
    assert errors["errors"] == ["ConnectionResetError"] and errors["connections"] == []
    two = ab.fold_wire({"connections": [_h2(_tls(aead=3), [(3, "/a")]), _h2(_tls(aead=1, cipher="0x1302"), [(3, "/b")])], "quic": []})
    other = ab.fold_wire({"connections": [_h2(_tls(aead=1), [(3, "/a")]), _h2(_tls(aead=3, cipher="0x1302"), [(3, "/b")])], "quic": []})
    assert [c["tls"]["cipher_suites"][0] for c in two["connections"]] == [c["tls"]["cipher_suites"][0] for c in other["connections"]], "the random AEAD does not order the fingerprints"


def test_the_http1_summary_keeps_the_request_lines_and_the_header_order_and_drops_the_connection_that_never_asked():
    head = {"request_line": "GET /?v=1 HTTP/1.1", "headers": [["Host", "h1.lab.test:h1"], ["Accept", "*/*"]], "body_length": 0}
    one = ab.fold_wire({"connections": [{"alpn": "http/1.1", "tls": _tls(), "http1": [head, dict(head, request_line="GET /wire.css HTTP/1.1")]},
                                        {"alpn": "http/1.1", "tls": _tls(resumed=True), "http1": []}], "quic": []})
    two = ab.fold_wire({"connections": [{"alpn": "http/1.1", "tls": _tls(), "http1": [head]}, {"alpn": "http/1.1", "tls": _tls(), "http1": [dict(head, request_line="GET /wire.css HTTP/1.1")]}],
                        "quic": []})
    assert one == two and [r["request_line"] for r in one["requests"]] == ["GET /?v=1 HTTP/1.1", "GET /wire.css HTTP/1.1"]
    swapped = ab.fold_wire({"connections": [{"alpn": "http/1.1", "tls": _tls(), "http1": [dict(head, headers=[["Accept", "*/*"], ["Host", "h1.lab.test:h1"]])]}], "quic": []})
    assert swapped["requests"][0]["headers"][0] == ["Accept", "*/*"] and swapped != ab.fold_wire({"connections": [{"alpn": "http/1.1", "tls": _tls(), "http1": [head]}], "quic": []})


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


def test_the_tee_says_which_connection_carried_each_request_and_when_so_that_a_run_that_differs_from_itself_can_be_explained():
    """A/A run 45025164169 (Windows): a request of step 15 counted twice in one run and once in the other, and the transcript, which folds a request into a count, could not say which page sent it
    or when. The tee knows the connection each head came on (its own number, in the order they were accepted) and the moment, and the request summary still leaves both out."""
    rec = ab.Recorder()
    server = _http_server()
    tee = ab.RawTee(rec, server.server_address[1], "A")
    try:
        first = socket.create_connection(("127.0.0.1", tee.port), timeout=60)
        first.sendall(b"GET /one HTTP/1.1\r\nHost: h\r\nReferer: http://h/\r\n\r\n")
        _read_response(first, b"/one")
        second = socket.create_connection(("127.0.0.1", tee.port), timeout=60)
        second.sendall(b"GET /two HTTP/1.1\r\nHost: h\r\n\r\n")
        _read_response(second, b"/two")
        first.sendall(b"GET /three HTTP/1.1\r\nHost: h\r\n\r\n")
        _read_response(first, b"/three")
        first.close()
        second.close()
    finally:
        tee.close()
        server.shutdown()
        server.server_close()
    events = rec.of("launch", "http")
    assert [(e["target"], e["connection"]) for e in events] == [("/one", 0), ("/two", 1), ("/three", 0)]
    assert [e["t"] for e in events] == sorted(e["t"] for e in events) and all(isinstance(e["t"], float) for e in events)
    summary = ab.request_summary(events, ab.Normalizer())
    assert sorted(summary) == ["GET A/one", "GET A/three", "GET A/two"] and "connection" not in json.dumps(summary) and '"t"' not in json.dumps(summary)


def _client_context():
    import ssl

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
    context.set_alpn_protocols(["http/1.1"])
    return context


def _http1_round(server, context, session=None, watch_end=False):
    """One request on a new TLS connection to ``server`` (a session belongs to the context that made it): ``(answer, the session the client holds, whether the server ended the connection after the answer)``.
    The end is watched only when asked: a connection read to its unclean end (no close_notify) makes OpenSSL refuse to resume its session, which is a fact about this client and not the browser's."""
    raw = socket.create_connection((server.address, server.port), timeout=60)
    tls = context.wrap_socket(raw, server_hostname="wire.test", session=session)
    tls.settimeout(60)
    try:
        tls.sendall(b"GET /wire.css HTTP/1.1\r\nHost: wire.test\r\n\r\n")
        answer = b""
        while b"p{color" not in answer:
            chunk = tls.recv(4096)
            assert chunk, "closed before the answer: %r" % answer
            answer += chunk
        session, ended = tls.session, None
        if watch_end:
            tls.settimeout(5)
            try:
                ended = tls.recv(4096) == b""
            except socket.timeout:
                ended = False
            except OSError:
                ended = True
        return answer, session, ended
    finally:
        tls.close()


def test_an_http1_server_that_ends_each_connection_after_its_answer_makes_every_next_request_a_resumed_connection_by_construction():
    """A/A run 45025164169 (Linux): the HTTP/1.1 site had a resumed connection on one seed of one run and on none of the five of the other, because whether a second connection opened while the
    first was still busy, before it had read the session ticket, is the pool's race. A server that says ``Connection: close`` and ends the connection leaves the client no choice: its next request
    rides a new connection, opened after the answer (and the ticket that came with it) was read, so it offers the session. The default stays keep-alive, for every other user of the server."""
    with wc.WireServer(alpn=("http/1.1",), dns_names=("wire.test",), close_after_response=True) as server:
        context = _client_context()
        answer, held, _ = _http1_round(server, context)
        assert b"Connection: close" in answer
        assert held is not None and held.has_ticket, "the first answer carries the ticket the next connection offers"
        _http1_round(server, context, session=held)
        _answer, _held, ended = _http1_round(server, context, watch_end=True)
        assert ended is True, "the server ends the connection after its answer"
        snapshot = server.snapshot()
    first, second, third = snapshot["connections"]
    assert not ab._resumed(first["tls"]) and ab._resumed(second["tls"]), "the second connection of the walk offers the session of the first"
    assert [len(c["http1"]) for c in snapshot["connections"]] == [1, 1, 1]
    wire = {"connections": [{"alpn": c["alpn"], "tls": c["tls"], "http1": [{k: r[k] for k in ("request_line", "headers", "body_length")} for r in c["http1"]]}
                            for c in snapshot["connections"]], "quic": []}
    folded = ab.fold_wire(ab.Normalizer().walk(wire))
    assert len(folded["resumed_connections"]) == 1 and len(folded["connections"]) == 1, "one fresh fingerprint and one resumed one, as the oracle folds them"
    with wc.WireServer(alpn=("http/1.1",), dns_names=("wire.test",)) as keep_alive:
        answer, _held, ended = _http1_round(keep_alive, _client_context(), watch_end=True)
        assert b"Connection: close" not in answer and not ended, "without the option the connection stays open"


def test_the_lab_ends_the_connections_of_its_http1_site_and_only_those():
    with ab.Lab(wire=True, host="127.0.0.1") as lab:
        assert lab.h1.close_after_response is True
        assert lab.h2.close_after_response is False and lab.h3.close_after_response is False


def test_the_evidence_of_a_seed_keeps_the_requests_of_the_journey_with_their_connection_and_referer_and_the_connections_of_the_https_site_with_their_moment_and_resumption():
    from fakeweb import journey_site

    with ab.Lab(journey=True, wire=True, host="127.0.0.1") as lab:
        journey_site.STATE.set_step(15)
        port_a = int(lab.origin_a.rsplit(":", 1)[1])
        sock, code = _socks_connect(lab.gateway.port, "127.0.0.1", port_a)
        assert code == 0
        sock.sendall(b"GET /release?id=lpabc123 HTTP/1.1\r\nHost: journey-a.test:%d\r\nReferer: http://journey-a.test:%d/account\r\n\r\n" % (port_a, port_a))
        _read_response(sock, b"ok")
        sock.close()
        lab.rec.set_phase("https.h1.v1")
        context = _client_context()
        _answer, held, _ended = _http1_round(lab.h1, context)
        _http1_round(lab.h1, context, session=held)
        evidence = ab.wire_evidence(lab)
    (request,) = evidence["journey"]
    assert request["phase"] == "journey.15" and request["connection"] == 0 and isinstance(request["t"], float)
    assert request["request"] == "GET A/release?id=lp<TOK>" and request["referer"] == "http://journey-a.test:A/account"
    first, second = evidence["https"]["h1"]
    assert (first["resumed"], second["resumed"]) == (False, True) and first["phase"] == second["phase"] == "https.h1.v1"
    assert first["requests"][0]["request"] == "GET /wire.css HTTP/1.1" and second["t"] >= first["t"]
    assert evidence["https"]["h2"] == []
    json.dumps(evidence)


def test_a_run_writes_what_each_seed_saw_request_by_request_outside_the_leaves_that_are_compared(tmp_path, monkeypatch):
    import _lab_egress
    import ci_crash_evidence as crash

    def walk_seed(binary, seed, with_idle, patch):
        ab.EVIDENCE[str(seed)] = {"journey": [{"t": 1.5, "connection": 3, "phase": "journey.15", "request": "GET A/release?id=lp<TOK>", "referer": "http://journey-a.test:A/"}], "https": {}}
        return {"journey": {}}

    monkeypatch.setattr(ab, "walk_seed", walk_seed)
    monkeypatch.setattr(_lab_egress, "install", lambda patch, work: None)
    monkeypatch.setenv(crash.CRASH_DIR_ENV, str(tmp_path / "t.crash"))
    try:
        assert ab.run("/e/firefox", "firefox-68", (11, 12), tmp_path / "t.json") == 0
    finally:
        crash.stop_probe()
    document = json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))
    assert document["evidence"]["format"] == ab.EVIDENCE_FORMAT and sorted(document["evidence"]["seeds"]) == ["11", "12"]
    assert document["evidence"]["seeds"]["11"]["journey"][0]["connection"] == 3
    other = json.loads(json.dumps(document))
    other["evidence"]["seeds"]["11"]["journey"][0]["t"] = 99.0
    assert compare(document, other)["differences"] == [] and compare(document, other)["leaves"] == compare(document, document)["leaves"], "evidence is never compared"


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
    step = {"udp": ab.udp_summary([], norm),
            "requests": ab.request_summary([request] * 3, norm), "name": "n", "ok": True, "idle": True, "url": "A/", "title": "t", "marker": "m", "cookies": [], "history_length": 2,
            "pages": 1, "extra": {}}
    tls = wc.parse_client_hello(build_hello())
    h2 = {"settings": [[1, 65536], [2, 0], [4, 131072], [5, 16384]], "frame_types_before_the_first_request": ["SETTINGS", "WINDOW_UPDATE"],
          "window_updates_before_the_first_request": [{"stream": 0, "increment": 12517377}], "priority": [], "priority_update": [], "headers_priority": [],
          "frames_after_the_first_request": 3, "settings_ack_sent_by_client": True,
          "requests": [{"stream": 1, "pseudo_order": [":method", ":path", ":authority", ":scheme"], "header_names": ["user-agent", "accept"], "headers": [[":method", "GET"], ["user-agent", "x"]],
                        "end_stream": True}]}
    body = {"journey": {"steps": {"%02d" % i: step for i in range(17)}, "connects": ab.connect_summary(["127.0.0.1:7000"], norm),
                        "dns": ab.dns_summary([("journey-a.test", 1, "lab")])}, "https": {"h2": {"v1": {"connections": [{"alpn": "h2", "tls": tls, "h2": h2}]}}},
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
    assert total > 600


def test_the_hang_guard_of_the_e2e_grows_with_the_seeds_and_the_subprocess_ends_before_the_worker_is_killed(monkeypatch, tmp_path):
    """Run 44963364060: 5 seeds under the default 420 s killed the xdist worker on Linux and on Windows."""
    import test_ab_network_e2e as e2e

    assert e2e.budget_s(1, 2) == 840 and e2e.budget_s(5, 2) == 1800 and e2e.budget_s(5, 3) == 2640
    assert e2e.budget_s(5, 2) > 420 and e2e.budget_s(2, 2) < e2e.budget_s(3, 2)
    monkeypatch.delenv("INVISIBLE_SEAL_FILE", raising=False)
    monkeypatch.delenv("AB_NETWORK_PR_SEEDS", raising=False)
    assert e2e.plan() == (1, 2)
    monkeypatch.setenv("AB_NETWORK_PR_SEEDS", "5")
    assert e2e.plan() == (5, 2)
    monkeypatch.setenv("AB_NETWORK_PR_SEEDS", "99")
    assert e2e.plan() == (len(ab.default_seeds()), 2), "no more seeds than the set has"
    seal = tmp_path / "seal.json"
    seal.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("INVISIBLE_SEAL_FILE", str(seal))
    assert e2e.plan() == (len(ab.default_seeds()), 3)
    mark = next(m for m in e2e.test_the_network_a_a_is_the_control_and_the_candidate_puts_on_the_wire_what_the_published_engine_puts.pytestmark if m.name == "timeout")
    assert mark.args == (e2e.BUDGET_S + 120,) and mark.kwargs == {"method": "thread"} and e2e.TIMEOUT_S == e2e.BUDGET_S


def _socks_connect_name(proxy_port: int, host: str, port: int):
    """A SOCKS5 CONNECT by NAME (address type 3), as a pingsender asks for the telemetry host: ``(socket, reply code)``."""
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=60)
    sock.settimeout(60)
    sock.sendall(b"\x05\x01\x00")
    assert sock.recv(2) == b"\x05\x00"
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host.encode() + struct.pack("!H", port))
    reply = b""
    while len(reply) < 10:
        chunk = sock.recv(10 - len(reply))
        if not chunk:
            break
        reply += chunk
    return sock, reply[1]


TELEMETRY = "incoming.telemetry.mozilla.org:443"


def test_the_profile_of_a_session_is_the_argument_of_its_parent_process_and_not_a_content_process():
    class Proc:
        def __init__(self, cmd):
            self.cmd = cmd

        def cmdline(self):
            if self.cmd is None:
                raise OSError("gone")
            return self.cmd

    found = [Proc(["firefox", "-contentproc", "-profile", "/not/this", "tab"]), Proc(None), Proc(["firefox", "-no-remote", "-headless", "-profile", "/tmp/invisible_profile_1_x", "-juggler-pipe"])]
    assert ab.profile_of("token", find=lambda token: found) == Path("/tmp/invisible_profile_1_x")
    assert ab.profile_of("token", find=lambda token: found[:2]) is None


def test_the_gateway_answers_a_connect_to_the_telemetry_host_itself_with_host_unreachable_and_writes_it_down_and_reaches_nothing():
    from _doh_servers import FakeDoh

    gateway_class, turn_class, _page = ab.make_lab_classes()
    rec = ab.Recorder()
    rec.set_phase("exit")
    gateway = gateway_class(rec, FakeDoh({}), turn_class("127.0.0.1"), "127.0.0.1", "198.51.100.9")
    try:
        sock, code = _socks_connect_name(gateway.port, "incoming.telemetry.mozilla.org", 443)
        sock.close()
        assert code == 4, "SOCKS5 reply 4, host unreachable: the lab answers, nothing goes to the internet"
        assert [e["target"] for e in rec.of("exit", "connect")] == [TELEMETRY]
        assert ("incoming.telemetry.mozilla.org", 443) in gateway.refused and gateway.refused == [("incoming.telemetry.mozilla.org", 443)]
    finally:
        gateway.close()


def test_a_connect_target_is_outside_the_lab_when_it_is_neither_loopback_nor_a_server_the_lab_runs():
    with ab.Lab(wire=True, host="127.0.0.1") as lab:
        assert ab.outside_the_lab(lab, TELEMETRY) and ab.outside_the_lab(lab, "162.159.207.1:443") and ab.outside_the_lab(lab, "incoming.telemetry.mozilla.org:80")
        assert not ab.outside_the_lab(lab, "127.0.0.1:%d" % lab.site.port) and not ab.outside_the_lab(lab, "192.0.2.53:443")
        assert not ab.outside_the_lab(lab, "127.0.0.1:%d" % lab.h1.port)


def _with_pings(t, targets, direct=None):
    norm = ab.Normalizer({})
    body = ab.exit_pings_summary(targets, norm)
    body["direct"] = direct if direct is not None else {"witness_tcp": [], "witness_udp": [], "watcher": [], "doh_direct": [], "resolver_trap": [], "blind": []}
    for seed in t["seeds"].values():
        seed["idle"]["exit_pings"] = copy.deepcopy(body)
    return t


def test_the_exit_pings_are_the_connects_to_the_proxy_towards_the_telemetry_host_and_they_are_compared_strictly():
    three = [TELEMETRY] * 3
    assert ab.exit_pings_summary(three, ab.Normalizer({})) == {"order": [TELEMETRY], "counts": {TELEMETRY: 3}}
    green = compare(_with_pings(transcript("a"), three), _with_pings(transcript("b"), three))
    assert green["verdict"] == "green"
    # a different number
    red = compare(_with_pings(transcript("a"), three), _with_pings(transcript("b"), [TELEMETRY] * 2))
    assert red["verdict"] == "red" and any(d["path"].endswith("/idle/exit_pings/counts/" + TELEMETRY) and d["a"] == 3 and d["b"] == 2 for d in red["differences"])
    # a different form: the address instead of the name
    form = compare(_with_pings(transcript("a"), three), _with_pings(transcript("b"), ["162.159.207.1:443"] * 3))
    assert form["verdict"] == "red" and any("/idle/exit_pings/order/0" in d["path"] and d["b"] == "162.159.207.1:443" for d in form["differences"])
    # no ping on one side only
    one_sided = compare(_with_pings(transcript("a"), three), _with_pings(transcript("b"), []))
    assert one_sided["verdict"] == "red" and any(d["path"].endswith("/idle/exit_pings/counts/" + TELEMETRY) for d in one_sided["differences"])
    # no ping on both: the A/A agrees
    both = compare(_with_pings(transcript("a"), []), _with_pings(transcript("b"), []))
    assert both["verdict"] == "green"
    # a ping that did not go to the proxy is red on either side, and when both do it
    leak = {"witness_tcp": [], "witness_udp": [], "watcher": ["tcp firefox -> 162.159.207.1:443"], "doh_direct": [], "resolver_trap": [], "blind": []}
    direct = compare(_with_pings(transcript("a"), three, direct=leak), _with_pings(transcript("b"), three))
    assert direct["verdict"] == "red" and direct["direct"] and any("(the exit pings)" in d for d in direct["direct"])
    assert compare(_with_pings(transcript("a"), three, direct=leak), _with_pings(transcript("b"), three, direct=leak))["verdict"] == "red"


def test_the_exit_launch_opens_a_page_waits_for_glean_then_closes_and_records_what_the_proxy_was_asked_until_the_tree_has_exited(tmp_path, monkeypatch):
    import _leaks

    profile = tmp_path / "profile"
    profile.mkdir()
    order = []

    class Watcher:
        watched_pids = {1}
        process_names: list = []
        direct: list = []

        def __init__(self, match=()):
            pass

        def start(self):
            return self

        def stop(self):
            order.append("watcher stopped")

    class Page:
        def __init__(self, lab):
            self.lab = lab

        def goto(self, url, **kwargs):
            order.append("goto %s in stage %s %s" % (url.rsplit("/", 2)[-2:], self.lab.rec.phase, sorted(kwargs.items())))
            self.lab.rec.event("connect", target="198.51.100.77:80")      # what the navigation asks of the proxy: it is not an exit ping

    class Browser:
        def __init__(self, lab):
            self.lab = lab

        def new_context(self, **kwargs):
            order.append("context %s" % sorted(kwargs.items()))
            return self

        def new_page(self):
            order.append("page")
            return Page(self.lab)

    class Session:
        _end_token = "token"

        def __init__(self, lab):
            self.lab = lab

        def __enter__(self):
            order.append("launched")
            return Browser(self.lab)

        def __exit__(self, *exc):
            order.append("closing in stage " + self.lab.rec.phase)
            for _ in range(3):                         # the pingsender of the shutdown, three times, past the browser's close
                self.lab.rec.event("connect", target=TELEMETRY)
            self.lab.rec.event("connect", target="192.0.2.53:443")
            order.append("tree exited")
            return False

    monkeypatch.setattr(_leaks, "Watcher", Watcher)
    monkeypatch.setattr(ab, "host_address", lambda: "127.0.0.1")
    monkeypatch.setattr(ab, "start_trap", lambda: None)
    monkeypatch.setattr(ab, "profile_of", lambda token, find=None: profile)
    monkeypatch.setattr(ab, "launch", lambda binary, seed, lab, patch: Session(lab))
    monkeypatch.setattr(ab.crash, "wait_for_glean", lambda p, *a, **k: order.append("glean waited") or 8.0)
    monkeypatch.setattr(ab, "save_datareporting", lambda profile, seed, launch_name: order.append("datareporting listed"))
    out = ab.run_exit_pings("/ff/firefox", 11, object())
    assert out["counts"] == {TELEMETRY: 3} and out["order"] == [TELEMETRY], "the DoH connection of the lab is not an exit ping"
    assert "recorded" not in out and out["direct"]["watcher"] == [] and out["direct"]["blind"] == []
    assert order == ["launched", "context [('ignore_https_errors', True)]", "page", "glean waited", "goto ['s', '0'] in stage sentinel.0 [('timeout', 60000), ('wait_until', 'load')]",
                     "datareporting listed", "closing in stage exit", "tree exited", "watcher stopped"], "page, Glean, a navigation to its load, then the close: the navigation is not an exit ping"
    # Glean never initialised: red, and the launch closes all the same
    order.clear()
    monkeypatch.setattr(ab.crash, "wait_for_glean", lambda p, *a, **k: (_ for _ in ()).throw(ab.crash.GleanNotReady("Glean did not initialise in 60 s")))
    late = ab.run_exit_pings("/ff/firefox", 11, object())
    assert late["glean"]["error"].startswith("GleanNotReady: Glean did not initialise in 60 s") and "tree exited" in order
    assert ab.transcript_errors({"11": {"idle": {"exit_pings": late}}}) == ["seeds/11/idle/exit_pings/glean/error"]


def test_no_wait_for_the_policy_pref_is_left_in_the_network_oracle():
    assert not any(hasattr(ab, name) for name in ("wait_for_pref", "PREF_POLL_S", "PREF_LIMIT_S", "NOTIFIED_PREF")), "the exit launch waits for Glean and for nothing else"
    text = (ROOT / "scripts" / "ci_ab_network.py").read_text(encoding="utf-8")
    assert "recorded=" not in text and 'recorded"' not in text and "wait_for_pref" not in text


def test_the_idle_category_of_the_first_seed_holds_the_exit_pings(monkeypatch):
    monkeypatch.setattr(ab, "run_idle", lambda binary, seed, condition, patch: {"destinations": {}})
    monkeypatch.setattr(ab, "run_exit_pings", lambda binary, seed, patch: {"counts": {}})
    monkeypatch.setattr(ab, "run_main", lambda binary, seed, patch: {"journey": {}})
    assert ab.walk_seed("ff", 11, True, object())["idle"] == {"c1": {"destinations": {}}, "c2": {"destinations": {}}, "exit_pings": {"counts": {}}}
    assert "idle" not in ab.walk_seed("ff", 12, False, object())


def test_an_engine_process_is_named_by_its_kind_and_the_python_that_runs_the_lab_is_the_core():
    assert ab.browser_kind(["firefox", "-no-remote", "-profile", "/p", "-juggler-pipe"]) == "parent"
    assert ab.browser_kind(["firefox", "-contentproc", "-prefsHandle", "0:1", "socket"]) == "socket"
    assert ab.browser_kind(["firefox", "-contentproc", "-isForBrowser", '"Tab"']) == "tab"
    assert ab.browser_kind(["firefox", "--backgroundtask", "pingsender", "https://incoming.telemetry.mozilla.org/x"]) == "backgroundtask:pingsender"

    class Proc:
        def __init__(self, name, cmd):
            self.n, self.c = name, cmd

        def name(self):
            return self.n

        def cmdline(self):
            return self.c

    table = {10: Proc("python.exe", ["python"]), 20: Proc("firefox.exe", ["firefox", "-contentproc", "socket"]), 30: Proc("firefox", ["firefox", "--backgroundtask", "pingsender"]), 40: Proc("node", ["node"])}

    def process(pid):
        if pid not in table:
            raise LookupError("gone")
        return table[pid]

    assert ab.classify_pid(10, python_pid=10, process=process) == "core"
    assert ab.classify_pid(20, python_pid=10, process=process) == "browser:socket"
    assert ab.classify_pid(30, python_pid=10, process=process) == "browser:backgroundtask:pingsender"
    assert ab.classify_pid(40, python_pid=10, process=process) == "other:node"
    assert ab.classify_pid(99, python_pid=10, process=process) == "unknown" and ab.classify_pid(None, python_pid=10) == "unknown"


def test_the_owner_of_a_client_socket_is_found_by_the_port_the_gateway_sees_and_the_port_of_the_gateway():
    from types import SimpleNamespace as NS

    rows = [NS(laddr=NS(port=5000), raddr=NS(port=7777), pid=111),         # the client end of a connection to the gateway
            NS(laddr=NS(port=7777), raddr=NS(port=5000), pid=222),         # the gateway's own end of it: not the opener
            NS(laddr=NS(port=5000), raddr=NS(port=443), pid=333),          # another connection that happens to use the same local port
            NS(laddr=(), raddr=(), pid=None)]
    assert ab.pid_of_client(5000, 7777, connections=lambda: rows) == 111
    assert ab.pid_of_client(5001, 7777, connections=lambda: rows) is None

    def broken():
        raise PermissionError("not allowed")

    assert ab.pid_of_client(5000, 7777, connections=broken) is None


def test_the_gateway_says_who_opened_a_connect_and_a_datagram_of_the_launch_stage_and_leaves_the_other_stages_alone():
    """This test process is the Python of the core: it opens the connections of the test, so the opener must read ``core``; a datagram carries the opener of its association."""
    from _socks_servers import MEASURE_NUMBER, STUN_PORT

    with ab.Lab(host="127.0.0.1") as lab:
        assert lab.gateway.attribute_in == ("launch",) and lab.rec.phase == "launch"
        sock, code = _socks_connect(lab.gateway.port, "192.0.2.77", 8080)          # refused, but written down with its opener
        sock.close()
        sock, code = _socks_connect_name(lab.gateway.port, "incoming.telemetry.mozilla.org", 443)
        sock.close()
        # a UDP association of this process, and one STUN Binding to the measurement server of the lab
        control = socket.create_connection(("127.0.0.1", lab.gateway.port), timeout=30)
        control.sendall(b"\x05\x01\x00")
        assert control.recv(2) == b"\x05\x00"
        control.sendall(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")
        reply = b""
        while len(reply) < 10:
            reply += control.recv(10 - len(reply))
        assert reply[1] == 0
        relay = (socket.inet_ntoa(reply[4:8]), struct.unpack("!H", reply[8:10])[0])
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        binding = b"\x00\x01\x00\x00\x21\x12\xa4\x42" + b"\x07" * 12
        udp.sendto(b"\x00\x00\x00\x01" + socket.inet_aton(MEASURE_NUMBER) + struct.pack("!H", STUN_PORT) + binding, relay)
        deadline = time.monotonic() + 30
        while not lab.rec.of("launch", "udp") and time.monotonic() < deadline:
            time.sleep(0.05)
        udp.close()
        control.close()
        connects = lab.rec.of("launch", "connect")
        assert [(e["target"], e["opener"]) for e in connects] == [("192.0.2.77:8080", "core"), ("incoming.telemetry.mozilla.org:443", "core")]
        (datagram,) = lab.rec.of("launch", "udp")
        assert datagram["target"] == "%s:%d" % (MEASURE_NUMBER, STUN_PORT) and datagram["message"] == "stun-0x0001" and datagram["opener"] == "core"
        data = ab.launch_data(lab)
        assert data["connects"]["openers"] == {"192.0.2.77:8080": ["core"], "incoming.telemetry.mozilla.org:443": ["core"]}
        assert data["udp"]["openers"] == {"%s:%d" % (MEASURE_NUMBER, STUN_PORT): ["core"]}
        # another stage: nothing is looked up, and the summaries have no openers
        lab.rec.set_phase("journey.01")
        sock, code = _socks_connect(lab.gateway.port, "192.0.2.78", 8080)
        sock.close()
        (later,) = lab.rec.of("journey.01", "connect")
        assert later["opener"] is None and "openers" not in ab.phase_data(lab, "journey.01")["connects"]


def test_the_dns_questions_of_a_stage_say_who_asked_by_the_connection_that_carried_them():
    from _doh_servers import DohQuery

    with ab.Lab(host="127.0.0.1") as lab:
        lab.gateway._openers[3] = "browser:socket"
        lab.gateway._openers[4] = "core"
        lab.doh.queries.append(DohQuery("journey-a.test", 1, "lab"))
        lab.doh.question_connections.append(("journey-a.test", 1, 3))
        lab.doh.queries.append(DohQuery("geo.example", 1, "lab"))
        lab.doh.question_connections.append(("geo.example", 1, 4))
        assert ab.phase_data(lab, "launch")["dns"] == {"order": ["journey-a.test", "geo.example"], "types": {"geo.example": [1], "journey-a.test": [1]},
                                                       "openers": {"geo.example": ["core"], "journey-a.test": ["browser:socket"]}}


def _with_launch(t, connects=(), udp=None):
    for seed in t["seeds"].values():
        seed["lab"]["launch"] = {"connects": ab.connect_summary([c for c, _o in connects], ab.Normalizer({}), [o for _c, o in connects]),
                                 "udp": {"order": ["198.51.100.6:19302"], "messages": {"198.51.100.6:19302": ["stun-0x0001"]}, "counts": {"198.51.100.6:19302": 1},
                                         "openers": {"198.51.100.6:19302": [udp or "core"]}}}
    return t


def test_a_connect_of_the_launch_stage_on_one_side_only_is_red_and_names_who_opened_it():
    one = [("203.0.113.9:443", "core")]
    green = compare(_with_launch(transcript("a"), one), _with_launch(transcript("b"), one))
    assert green["verdict"] == "green"
    red = compare(_with_launch(transcript("a"), one), _with_launch(transcript("b"), []))
    assert red["verdict"] == "red"
    paths = {d["path"]: (d["a"], d["b"]) for d in red["differences"]}
    assert paths["seeds/11/lab/launch/connects/openers/203.0.113.9:443/0"] == ("core", ab.MISSING), "the difference names the opener"
    assert paths["seeds/11/lab/launch/connects/order/0"] == ("203.0.113.9:443", ab.MISSING)
    # the same destination opened by another process is a difference too
    other = compare(_with_launch(transcript("a"), one), _with_launch(transcript("b"), [("203.0.113.9:443", "browser:socket")]))
    assert other["verdict"] == "red" and {(d["a"], d["b"]) for d in other["differences"]} == {("core", "browser:socket")}
    # and so is the STUN Binding of the launch coming from the browser where the published engine's core sent it
    stun = compare(_with_launch(transcript("a")), _with_launch(transcript("b"), udp="browser:parent"))
    assert stun["verdict"] == "red" and any(d["path"].endswith("/lab/launch/udp/openers/198.51.100.6:19302") and d["a"] == ["core"] and d["b"] == ["browser:parent"] for d in stun["differences"])


def _with_h3_udp(t, openers=None, unattributed=None):
    for seed in t["seeds"].values():
        udp = {"order": ["10.1.0.13:h3"], "messages": {"10.1.0.13:h3": ["quic-short"]}}
        if openers is not None:
            udp["openers"] = {"10.1.0.13:h3": openers}
        if unattributed is not None:
            udp["unattributed"] = unattributed
        seed["h3"] = {"udp": udp}
    return t


def test_the_attribution_of_datagrams_is_the_set_of_kinds_per_destination_and_a_side_that_found_none_says_nothing():
    """A/A run 44993377458: `browser:parent` on one side of a destination and nothing on the other (2 leaves on Linux, 3 on Windows): the attribution loses a race with the socket."""
    both = compare(_with_h3_udp(transcript("a"), ["browser:parent"], 0), _with_h3_udp(transcript("b"), ["browser:parent"], 0))
    assert both["verdict"] == "green"
    one_side = compare(_with_h3_udp(transcript("a"), ["browser:parent"], 0), _with_h3_udp(transcript("b"), None, 4))
    assert one_side["verdict"] == "green" and one_side["differences"] == [] and one_side["unattributed_datagrams"] == 8 and one_side["leaves"] > 100
    neither = compare(_with_h3_udp(transcript("a")), _with_h3_udp(transcript("b")))
    assert neither["verdict"] == "green"
    # two different kinds are red, and the order of the list does not matter
    red = compare(_with_h3_udp(transcript("a"), ["browser:parent"]), _with_h3_udp(transcript("b"), ["core"]))
    assert red["verdict"] == "red" and {"path": "seeds/11/h3/udp/openers/10.1.0.13:h3", "a": ["browser:parent"], "b": ["core"]} in red["differences"]
    more = compare(_with_h3_udp(transcript("a"), ["browser:parent"]), _with_h3_udp(transcript("b"), ["browser:parent", "core"]))
    assert more["verdict"] == "red", "a kind more on one side is a difference when both sides found some"
    same = compare(_with_h3_udp(transcript("a"), ["browser:parent", "core"]), _with_h3_udp(transcript("b"), ["browser:parent", "core"]))
    assert same["verdict"] == "green"


def test_the_unattributed_datagrams_are_counted_and_printed_and_connects_stay_strict(capsys):
    report = compare(_with_h3_udp(transcript("a"), ["core"], 2), _with_h3_udp(transcript("b"), ["core"], 5))
    assert report["verdict"] == "green" and report["unattributed_datagrams"] == 14
    ab.print_report(report, True)
    assert "14 datagrams were not attributed" in capsys.readouterr().out
    # a CONNECT with no opener on one side is not relaxed
    one = _with_launch(transcript("a"), [("203.0.113.9:443", "core")])
    two = _with_launch(transcript("b"), [("203.0.113.9:443", None)])
    assert compare(one, two)["verdict"] == "red"


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


def test_each_engine_run_of_the_network_oracle_has_a_cache_folder_of_its_own_that_is_gone_when_it_ends(tmp_path, monkeypatch):
    import ci_ab_oracle as oracle

    seen = []

    class Done:
        returncode = 0

    original = oracle.own_cache_env
    monkeypatch.setattr(oracle, "own_cache_env", lambda env, folder: original(env, folder, platform="linux"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "user"))
    monkeypatch.setattr(ab.subprocess, "run", lambda cmd, env, timeout: seen.append((env["XDG_CACHE_HOME"], Path(env["XDG_CACHE_HOME"]).is_dir())) or Done())
    assert ab.run_engine("ff", None, "firefox-68", (0,), tmp_path / "t.json") == 0
    assert ab.run_engine("ff", None, "firefox-68-again", (0,), tmp_path / "t.json") == 0
    (first, there1), (second, there2) = seen
    assert there1 and there2 and first != second and str(tmp_path / "user") not in (first, second)
    assert not Path(first).exists() and not Path(second).exists()


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


# ----------------------------------------------------------------------------------------------------------------- how each launch's parent ended
def exits_of(*codes, platform=None):
    """The `exits` a run writes (scripts/ci_crash_evidence.py): one launch per code, named as the oracle names its launches."""
    names = [("main", 11), ("idle-c1", 11), ("idle-c2", 11), ("exit-pings", 11), ("main", 12)]
    return {"format": "launch-exits/1", "launches": [{"n": n + 1, "label": {"seed": names[n][1], "launch": names[n][0]}, "pid": 100 + n, "returncode": code}
                                                     for n, code in enumerate(codes)]}


def test_a_transcript_whose_launches_all_ended_with_0_is_green_with_or_without_the_record():
    a, b = transcript("a"), transcript("b")
    a["exits"], b["exits"] = exits_of(0, 0, 0, 0, 0), exits_of(0, 0, 0, 0, 0)
    assert compare(a, b)["verdict"] == "green" and compare(a, transcript("b"))["verdict"] == "green"
    assert "exits" not in ab.flatten(a["seeds"]) and not any(p.startswith("exits") for p in ab.flatten(a))


def test_a_launch_that_died_is_red_and_names_the_side_the_seed_the_launch_and_the_code_on_either_side():
    a, b = transcript("firefox-68"), transcript("firefox-68-again")
    a["exits"], b["exits"] = exits_of(0, 0, 0, 0, 0), exits_of(0, 0, -11, 0, 0)
    b["platform"] = "linux"
    report = compare(a, b)
    assert report["verdict"] == "red" and report["differences"] == [], "the leaves are equal: the launch is what is red"
    [line] = report["problems"]
    for part in ("code -11 (SIGSEGV)", "run firefox-68-again", "side B", "seed 11", "launch idle-c2"):
        assert part in line, part
    swapped = compare(b, a)
    assert "side A" in swapped["problems"][0] and "run firefox-68-again" in swapped["problems"][0]
    windows = transcript("candidate")
    windows["platform"] = "win32"
    windows["exits"] = exits_of(3221225477)
    assert "0xC0000005 (access violation)" in compare(transcript("firefox-68"), windows)["problems"][0]


def test_a_run_that_recorded_no_launch_is_red_because_its_probe_did_not_see_a_browser():
    a, b = transcript("a"), transcript("b")
    a["exits"], b["exits"] = exits_of(0), {"format": "launch-exits/1", "launches": []}
    report = compare(a, b)
    assert report["verdict"] == "red" and "recorded no launch (side B)" in report["problems"][0]


def test_the_run_writes_how_every_launch_ended_into_the_transcript_and_prints_the_one_that_died(tmp_path, monkeypatch, capsys):
    import os

    import ci_crash_evidence as crash
    import _lab_egress

    def walk_seed(binary, seed, with_idle, patch):
        for code, launch in ((0, "main"), (-11, "idle-c1")) if with_idle else ((0, "main"),):
            crash.mark(seed=seed, launch=launch)
            from invisible_core.juggler.connection import Connection

            class Process:
                pid = 4242

                def poll(self):
                    return code

                def terminate(self):
                    pass

            to_r, to_w = os.pipe()
            from_r, from_w = os.pipe()
            conn = Connection(to_w, from_r, Process())
            conn.close()
            os.close(from_w)
            os.close(to_r)
        return {"journey": {}}

    monkeypatch.setattr(ab, "walk_seed", walk_seed)
    monkeypatch.setattr(_lab_egress, "install", lambda patch, work: None)
    monkeypatch.setenv(crash.CRASH_DIR_ENV, str(tmp_path / "t.crash"))
    try:
        assert ab.run("/e/firefox", "firefox-68", (11, 12), tmp_path / "t.json") == 0
    finally:
        crash.stop_probe()
    document = json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))
    assert [(x["label"]["seed"], x["label"]["launch"], x["returncode"]) for x in document["exits"]["launches"]] == [(11, "main", 0), (11, "idle-c1", -11), (12, "main", 0)]
    assert "EXIT the parent process exited with code" in capsys.readouterr().out
    assert (tmp_path / "t.crash" / "exits.json").is_file() and (tmp_path / "t.crash" / "02-11-idle-c1" / "crash.json").is_file()
    other = transcript("firefox-68-again")
    other["exits"] = exits_of(0, 0, 0)
    document["platform"] = "linux"
    assert compare(document, other)["verdict"] == "red"


def test_the_engine_run_is_given_a_folder_for_what_a_launch_that_died_leaves_beside_its_transcript(tmp_path, monkeypatch):
    import ci_crash_evidence as crash

    seen = []

    class Done:
        returncode = 0

    monkeypatch.setattr(ab.subprocess, "run", lambda cmd, env, timeout: seen.append(env.get(crash.CRASH_DIR_ENV)) or Done())
    assert ab.run_engine("ff", None, "firefox-68-again", (0,), tmp_path / "out" / "transcript-68-again.json") == 0
    assert seen == [str(tmp_path / "out" / "transcript-68-again.crash")]


# ----------------------------------------------------------------------------------------------------------------- Glean before the close
def _glean_fakes(monkeypatch, tmp_path, waiter):
    """`run_idle` with a fake lab, a fake launch and a fake `wait_for_glean`: what happened, in order, is the list returned."""
    import contextlib

    order = []
    profile = tmp_path / "profile"

    class Page:
        def goto(self, *a, **k):
            order.append("goto")

        def evaluate(self, *a, **k):
            return None

    class Context:
        def new_page(self):
            order.append("new_page")
            return Page()

    class Browser:
        def new_context(self, **k):
            return Context()

    class Session:
        _end_token = "token"

        def __enter__(self):
            order.append("launched")
            return Browser()

        def __exit__(self, *exc):
            order.append("closed")
            return False

    class Lab:
        class rec:
            phase = ""

            @staticmethod
            def set_phase(name):
                order.append("phase " + name)

        class norm:
            @staticmethod
            def walk(x):
                return x

        class site:
            port = 1

        h1 = h2 = None

    @contextlib.contextmanager
    def open_lab(**k):
        yield Lab

    monkeypatch.setattr(ab, "open_lab", open_lab)
    monkeypatch.setattr(ab, "launch", lambda binary, seed, lab, patch: Session())
    monkeypatch.setattr(ab, "launch_data", lambda lab: {})
    monkeypatch.setattr(ab, "sentinel_signature", lambda lab: {"connects": [], "dns": [], "udp": []})
    monkeypatch.setattr(ab, "profile_of", lambda token, find=None: order.append("profile of " + token) or profile)
    monkeypatch.setattr(ab.crash, "wait_for_glean", lambda p, *a, **k: order.append("glean waited in %s" % p.name) or waiter(p))
    monkeypatch.setattr(ab, "save_datareporting", lambda session, seed, launch_name: order.append("datareporting listed"))
    return order


@pytest.mark.parametrize("condition", ["c1", "c2"])
def test_an_idle_launch_waits_for_glean_before_it_closes_and_prints_when_it_was_ready(tmp_path, monkeypatch, capsys, condition):
    order = _glean_fakes(monkeypatch, tmp_path, lambda p: 8.25)
    out = ab.run_idle("/ff/firefox", 11, condition, object())
    assert order.index("new_page") < order.index("glean waited in profile") < order.index("goto"), "Glean is born when a window opens: the condition comes after the page and before the work"
    assert order[-2:] == ["datareporting listed", "closed"] and order.count("closed") == 1, "the list of datareporting is the last thing before the close"
    assert "profile of token" in order and order.index("profile of token") < order.index("glean waited in profile")
    assert "glean ready after 8.2 s (seed 11, launch idle-%s)" % condition in capsys.readouterr().out
    assert ab.transcript_errors({"11": out}) == [] and "glean" not in out


def test_a_launch_whose_glean_never_initialised_is_a_red_error_of_the_transcript_and_the_browser_still_closes(tmp_path, monkeypatch):
    def never(profile):
        raise ab.crash.GleanNotReady("Glean did not initialise in 60 s: no data.safe.bin in the profile %s" % profile)

    order = _glean_fakes(monkeypatch, tmp_path, never)
    out = ab.run_idle("/ff/firefox", 11, "c1", object())
    assert order[-1] == "closed" and order.count("closed") == 1 and "goto" in order, "the launch goes on and the close is the same: the exit of the `with`"
    assert out["glean"]["error"].startswith("GleanNotReady: Glean did not initialise in 60 s"), out
    assert "destinations" in out, "what the launch saw is kept: only the verdict is red"
    assert ab.transcript_errors({"11": out}) == ["seeds/11/glean/error"]


def test_a_launch_without_a_known_profile_is_red_the_same_way_for_glean(tmp_path, monkeypatch):
    real = ab.crash.wait_for_glean
    order = _glean_fakes(monkeypatch, tmp_path, lambda p: 0.0)
    monkeypatch.setattr(ab, "profile_of", lambda token, find=None: None)
    monkeypatch.setattr(ab.crash, "wait_for_glean", real)   # the real one: it knows what to say of a launch without a profile
    out = ab.run_idle("/ff/firefox", 11, "c1", object())
    assert "the profile of the launch is not known" in out["glean"]["error"] and order[-1] == "closed"


def test_the_main_launch_waits_for_glean_after_the_journey_and_before_the_rest_of_its_work(tmp_path, monkeypatch):
    import _leaks

    order = _glean_fakes(monkeypatch, tmp_path, lambda p: 3.0)

    class Watcher:
        def __init__(self, match=()):
            pass

        def start(self):
            return self

        def stop(self):
            pass

    monkeypatch.setattr(_leaks, "Watcher", Watcher)
    monkeypatch.setattr(ab, "start_trap", lambda: None)
    monkeypatch.setattr(ab, "direct_report", lambda lab, watcher, trap: {})
    monkeypatch.setattr(ab, "wire_evidence", lambda lab: {})
    for walker in ("walk_journey", "walk_h3", "walk_webrtc"):
        monkeypatch.setattr(ab, walker, lambda *a, _w=walker, **k: order.append(_w) or {})
    monkeypatch.setattr(ab, "walk_https", lambda *a, **k: order.append("walk_https") or {})
    out = ab.run_main("/ff/firefox", 11, object())
    assert order.index("walk_journey") < order.index("glean waited in profile") < order.index("walk_https") < order.index("walk_webrtc"), order
    assert order[-2:] == ["datareporting listed", "closed"], order
    assert "glean" not in out and "journey" in out


# ----------------------------------------------------------------------------------------------------------------- the guard that kills the whole tree
#: The oracle of the test: it writes the line of a launch, starts a grandchild that inherits its stdout and sleeps, and sleeps itself. The grandchild is the firefox that holds the pipe.
_TREE_CHILD = '''
import subprocess, sys, time
sys.path.insert(0, sys.argv[2])
import ci_crash_evidence as crash
print(crash.launch_line(11, "exit-pings", "firefox-68-again"), flush=True)
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(sys.argv[1], "w").write(str(grandchild.pid))
print("the grandchild is alive", flush=True)
time.sleep(60)
'''


def _alive(pid):
    import psutil

    return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE


def _wait_gone(pid, seconds=10.0):
    end = time.monotonic() + seconds
    while _alive(pid) and time.monotonic() < end:
        time.sleep(0.1)
    return not _alive(pid)


def _kill_if_alive(pid):
    import psutil

    try:
        psutil.Process(pid).kill()
    except psutil.Error:
        pass


def _tree_command(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    return [sys.executable, "-u", "-c", _TREE_CHILD, str(pidfile), str(ROOT / "scripts")], pidfile


def test_the_old_guard_leaves_the_grandchild_alive_and_on_windows_does_not_even_return_at_the_timeout(tmp_path):
    """The defect, shown: `subprocess.run(capture_output=True, timeout=...)` kills the child alone. The grandchild lives; on Windows `run` then waits for the pipe it holds
    (run 45121964904, Windows, job.log 546-557), here until we kill it."""
    import subprocess
    import threading

    command, pidfile = _tree_command(tmp_path)
    outcome = {}

    def old():
        try:
            subprocess.run(command, capture_output=True, text=True, timeout=3)
        except subprocess.TimeoutExpired:
            outcome["timeout"] = True

    thread = threading.Thread(target=old, daemon=True)
    thread.start()
    thread.join(3 + 5)
    try:
        assert pidfile.is_file(), "the child did not get to start its grandchild"
        grandchild = int(pidfile.read_text())
        assert _alive(grandchild), "the grandchild outlives the timeout of the old guard"
        if sys.platform == "win32":
            assert thread.is_alive(), "on Windows the old guard does not return: it waits for the pipe of the grandchild"
    finally:
        if pidfile.is_file():
            _kill_if_alive(int(pidfile.read_text()))
        thread.join(15)


def test_the_tree_guard_returns_at_the_timeout_kills_the_grandchild_and_names_the_launch_that_was_alive(tmp_path):
    import ci_crash_evidence as crash

    command, pidfile = _tree_command(tmp_path)
    began = time.monotonic()
    try:
        run = crash.run_tree(command, tmp_path / "out", timeout_s=3)
        spent = time.monotonic() - began
        assert run.timed_out and run.returncode is None
        assert spent < 3 + 5, "it returned a few seconds after the timeout, not when the grandchild would have ended (%.1f s)" % spent
        grandchild = int(pidfile.read_text())
        assert _wait_gone(grandchild), "the grandchild is dead: the tree was killed, not the child alone"
        assert "the grandchild is alive" in run.stdout, "the output is kept, from a file"
        message = run.timeout_message("the network oracle", 3)
        assert message.startswith("the network oracle did not end within 3 s: the launch exit-pings of seed 11 (firefox-68-again) was alive for ")
        assert message.endswith(" s; its tree was killed")
        assert 2 <= run.launch["alive_s"] < 3 + 5
    finally:
        if pidfile.is_file():
            _kill_if_alive(int(pidfile.read_text()))


def test_the_tree_guard_is_silent_when_the_run_ends_by_itself_and_keeps_stdout_stderr_and_the_code_apart(tmp_path):
    import ci_crash_evidence as crash

    run = crash.run_tree([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(2)"], tmp_path / "out", timeout_s=30)
    assert (run.returncode, run.timed_out, run.stdout.strip(), run.stderr.strip()) == (2, False, "out", "err")
    assert run.launch is None and "no launch had begun" in run.timeout_message("the oracle", 30)


def test_the_launch_line_is_read_back_and_the_last_one_wins():
    import datetime

    import ci_crash_evidence as crash

    at = datetime.datetime(2026, 11, 22, 10, 0, 0, tzinfo=datetime.timezone.utc)
    text = "\n".join([crash.launch_line(11, "main", "firefox-68", at), "noise", crash.launch_line(12, "idle-c2", "firefox-68-again", at), "more noise"])
    found = crash.last_launch(text, now=at + datetime.timedelta(seconds=95))
    assert (found["launch"], found["seed"], found["label"], found["alive_s"]) == ("idle-c2", "12", "firefox-68-again", 95.0)
    assert crash.last_launch("nothing here") is None


def test_the_oracle_names_each_launch_with_the_line_the_guard_reads(tmp_path, monkeypatch, capsys):
    import ci_crash_evidence as crash

    monkeypatch.setattr(ab, "RUN_LABEL", "firefox-68-again")
    ab.begin_launch(13, "exit-pings")
    found = crash.last_launch(capsys.readouterr().out)
    assert (found["launch"], found["seed"], found["label"]) == ("exit-pings", "13", "firefox-68-again")


# ----------------------------------------------------------------------------------------------------------------- what the profile held in datareporting/ before the close
def test_the_listing_of_datareporting_is_relative_sorted_and_with_sizes_and_is_written_where_the_stderr_of_the_launches_go(tmp_path, monkeypatch):
    import ci_crash_evidence as crash

    profile = tmp_path / "profile"
    (profile / "datareporting" / "glean" / "db").mkdir(parents=True)
    (profile / "datareporting" / "glean" / "db" / "data.safe.bin").write_bytes(b"x" * 37)
    (profile / "datareporting" / "glean" / "pending_pings").mkdir()
    (profile / "datareporting" / "state.json").write_text("{}", encoding="utf-8")
    rows = crash.datareporting_listing(profile)
    assert [r.split("\t") for r in rows if not r.startswith("#")] == [["glean/", "-"], ["glean/db/", "-"], ["glean/db/data.safe.bin", "37"], ["glean/pending_pings/", "-"], ["state.json", "2"]]
    crash_dir = tmp_path / "t.crash"
    probe = crash.LaunchProbe("/e/firefox", crash_dir)
    monkeypatch.setattr(crash, "PROBE", probe)
    probe.records.extend(["first", "second", "third"])      # the launch being made is the last record: the number of its stderr file
    probe.set_label(seed=11, launch="idle-c1")
    written = crash.save_datareporting(profile)
    assert written == "t.crash/datareporting/03-11-idle-c1.tsv"
    text = (crash_dir / "datareporting" / "03-11-idle-c1.tsv").read_text(encoding="utf-8")
    assert "glean/db/data.safe.bin" in text and "37" in text


def test_a_profile_that_cannot_be_read_is_said_in_the_file_and_never_raises(tmp_path, monkeypatch):
    import ci_crash_evidence as crash

    assert any("not known" in r for r in crash.datareporting_listing(None))
    assert any("datareporting" in r and r.startswith("# ") for r in crash.datareporting_listing(tmp_path / "gone"))
    monkeypatch.setattr(crash, "PROBE", None)
    assert crash.save_datareporting(tmp_path) is None, "no probe, no folder: nothing is written"


def test_the_oracle_lists_datareporting_of_the_profile_of_the_session_and_never_raises(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(ab, "profile_of", lambda token, find=None: tmp_path)
    monkeypatch.setattr(ab.crash, "save_datareporting", lambda profile: seen.append(profile))

    class Session:
        _end_token = "token"

    ab.save_datareporting(Session(), 11, "main")
    assert seen == [tmp_path]
    monkeypatch.setattr(ab.crash, "save_datareporting", lambda profile: 1 / 0)
    ab.save_datareporting(Session(), 11, "main")           # a failure of the evidence is not a failure of the launch
