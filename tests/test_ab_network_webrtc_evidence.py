"""The WebRTC walk of the network oracle keeps its evidence OUTSIDE the leaves that are compared, and the hunt repeats that walk alone (scripts/ci_ab_network.py), no browser.

The A/A of the oracle found the published engine sending 8 or 9 TURN Allocate datagrams to 198.51.100.7:3478 in the WebRTC walk of one seed. The two readings are told apart by
what each datagram carried and when: a STUN transaction id repeats on a retransmission, and the page's own ICE times and the moments of the stages say where the close fell. This file holds
that evidence to its rules: it is in ``transcript["evidence"]`` (which ``compare_transcripts`` never reads), what the page returns for the compared fields is what it returned before, the
transaction id comes from the header of the datagram, the gateway's summary is the one it was, and the hunt (``AB_NETWORK_HUNT=webrtc:<seed>:<n>``, wired into e2e-hunt.yml) writes one file
per repetition and a summary and decides nothing.
"""
from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ci_ab_network as ab  # noqa: E402

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
HUNT_WORKFLOW = ROOT / ".github" / "workflows" / "e2e-hunt.yml"

COOKIE = b"\x21\x12\xa4\x42"
TID = bytes(range(1, 13))


def stun(message_type: int, tid: bytes = TID) -> bytes:
    """A STUN header (RFC 8489): type, length 0, the magic cookie and a 12-byte transaction id."""
    return message_type.to_bytes(2, "big") + b"\x00\x00" + COOKIE + tid


# ----------------------------------------------------------------------------------------------------------------- the transaction id
def test_the_transaction_id_is_bytes_8_to_20_of_a_stun_header():
    allocate = stun(0x0003)
    assert len(allocate) == 20
    assert ab.stun_tid(allocate) == TID.hex() == "0102030405060708090a0b0c"
    assert ab.stun_tid(allocate + b"\x00\x06\x00\x04lab\x00") == TID.hex(), "the attributes after the header change nothing"
    assert ab.stun_tid(stun(0x0113, b"\xff" * 12)) == "ff" * 12


@pytest.mark.parametrize("payload", [b"", b"\xc3" + b"\x00" * 40, b"\x40" + b"\x01" * 30, b"\x00\x03\x00\x00" + b"\x00" * 16, stun(0x0003)[:19], b"hello world" * 3])
def test_a_datagram_that_is_not_stun_has_no_transaction_id(payload):
    assert ab.stun_tid(payload) is None


def test_the_gateway_writes_the_transaction_id_of_each_datagram_and_the_summary_of_the_phase_does_not_change():
    from _doh_servers import FakeDoh
    from _turn_chain import TURN_IP, TURN_PORT

    assert "%s:%d" % (TURN_IP, TURN_PORT) == ab.TURN_TARGET, "the target the hunt counts is the TURN server of the lab"
    gateway_class, turn_class, _page = ab.make_lab_classes()
    rec = ab.Recorder()
    doh = FakeDoh({})
    turn = turn_class("127.0.0.1")
    gateway = gateway_class(rec, doh, turn, "127.0.0.1", "198.51.100.9")
    try:
        rec.set_phase("webrtc")
        gateway._route(TURN_IP, TURN_PORT, 1, stun(0x0003), 1, None)
        gateway._route(TURN_IP, TURN_PORT, 1, stun(0x0003), 1, None)
        gateway._route(TURN_IP, TURN_PORT, 1, stun(0x0003, b"\x09" * 12), 1, None)
        gateway._route("127.0.0.1", 5555, 1, b"\xc3" + b"\x00" * 20, 1, None)
    finally:
        gateway.close()
        turn.close()
        doh.close()
    events = rec.of("webrtc", "udp")
    assert [(e["target"], e["message"], e["tid"]) for e in events] == [
        (ab.TURN_TARGET, "stun-0x0003", TID.hex()), (ab.TURN_TARGET, "stun-0x0003", TID.hex()), (ab.TURN_TARGET, "stun-0x0003", "09" * 12), ("127.0.0.1:5555", "quic-long", None)]
    norm = ab.Normalizer({})
    without = [{k: v for k, v in e.items() if k != "tid"} for e in events]
    assert ab.udp_summary(events, norm) == ab.udp_summary(without, norm), "the datagram's transaction id is not in what the A/A compares"
    assert ab.udp_summary(events, norm)["counts"] == {ab.TURN_TARGET: 3, "127.0.0.1:5555": 1}


# ----------------------------------------------------------------------------------------------------------------- the evidence key
def lab_with(rec, **webrtc):
    return types.SimpleNamespace(rec=rec, webrtc=webrtc)


def recorded_walk():
    """A recorder that went through the stages of the walk, with the datagrams a TURN server's client sends: the first Allocate three times, the authenticated one twice, one late."""
    rec = ab.Recorder()
    rec.set_phase("webrtc")
    for target, message, tid in [(ab.TURN_TARGET, "stun-0x0003", "aa" * 12), (ab.TURN_TARGET, "stun-0x0003", "aa" * 12), ("198.51.100.5:19302", "stun-0x0001", "bb" * 12),
                                 (ab.TURN_TARGET, "stun-0x0003", "cc" * 12), ("127.0.0.1:5555", "quic-long", None)]:
        rec.event("udp", target=target, message=message, tid=tid, opener=None)
    counted = len(rec.of("webrtc", "udp"))
    rec.set_phase("webrtc.end")
    rec.event("udp", target=ab.TURN_TARGET, message="stun-0x0003", tid="cc" * 12, opener=None)
    return rec, counted


def test_the_evidence_of_the_walk_lists_every_datagram_with_its_time_target_message_and_transaction_and_the_moments_of_the_stages():
    rec, counted = recorded_walk()
    page = {"start": 1.0, "offer": 2.0, "candidates": [{"t": 3.0, "type": "host"}], "errors": [{"t": 4.0, "code": 701, "url": "turn:x"}], "gathered": 5.0, "closing": 6.0, "closed": 6.5}
    evidence = ab.webrtc_evidence(lab_with(rec, counted=counted, evaluate={"called": 0.5, "returned": 7.0}, page=page))
    json.dumps(evidence)
    assert set(evidence) == {"phase_t", "evaluate", "page", "datagrams"}
    assert evidence["page"] == page and evidence["evaluate"] == {"called": 0.5, "returned": 7.0}
    assert set(evidence["phase_t"]) == {"webrtc", "webrtc.end"} and evidence["phase_t"]["webrtc"] <= evidence["phase_t"]["webrtc.end"]
    datagrams = evidence["datagrams"]
    assert [(d["target"], d["message"], d["tid"], d["phase"], d["counted"]) for d in datagrams] == [
        (ab.TURN_TARGET, "stun-0x0003", "aa" * 12, "webrtc", True), (ab.TURN_TARGET, "stun-0x0003", "aa" * 12, "webrtc", True), ("198.51.100.5:19302", "stun-0x0001", "bb" * 12, "webrtc", True),
        (ab.TURN_TARGET, "stun-0x0003", "cc" * 12, "webrtc", True), ("127.0.0.1:5555", "quic-long", None, "webrtc", True),
        (ab.TURN_TARGET, "stun-0x0003", "cc" * 12, "webrtc.end", False)]
    assert all(isinstance(d["t"], float) for d in datagrams) and [d["t"] for d in datagrams] == sorted(d["t"] for d in datagrams)


def test_the_evidence_of_the_walk_leaves_out_the_datagrams_of_other_stages_and_says_nothing_when_the_walk_never_ran():
    rec = ab.Recorder()
    rec.set_phase("h3")
    rec.event("udp", target="127.0.0.1:1", message="quic-long", tid=None, opener=None)
    empty = ab.webrtc_evidence(lab_with(rec))
    assert empty == {"phase_t": {"webrtc": None, "webrtc.end": None}, "evaluate": None, "page": None, "datagrams": []}


def evidence_transcript(label, datagrams):
    """A transcript as ``run`` writes it: ``seeds`` (compared) and ``evidence`` (not), with the webrtc evidence of a real recorder in it."""
    rec = ab.Recorder()
    rec.set_phase("webrtc")
    for tid in datagrams:
        rec.event("udp", target=ab.TURN_TARGET, message="stun-0x0003", tid=tid, opener=None)
    rec.set_phase("webrtc.end")
    evidence = ab.webrtc_evidence(lab_with(rec, counted=len(datagrams), page={"gathered": float(len(datagrams))}))
    return {"format": ab.FORMAT, "label": label, "platform": "linux",
            "seeds": {"4": {"webrtc": {"candidates": ["udp comp1 prio1 srflx <exit>"], "gathering": "complete", "udp": {"counts": {ab.TURN_TARGET: 8}}}}},
            "evidence": {"format": ab.EVIDENCE_FORMAT, "seeds": {"4": {"journey": [], "https": {}, "h3": [], "webrtc": evidence}}}}


def test_two_transcripts_that_differ_only_in_the_evidence_of_the_walk_have_no_difference():
    a = evidence_transcript("a", ["aa" * 12] * 8)
    b = evidence_transcript("b", ["aa" * 12] * 7 + ["bb" * 12, "cc" * 12])
    assert a["evidence"] != b["evidence"] and a["seeds"] == b["seeds"]
    report = ab.compare_transcripts(a, b, [], 0, "a", "b")
    assert report["differences"] == [] and report["problems"] == [] and report["verdict"] == "green"
    assert report["leaves"] == ab.compare_transcripts(a, a, [], 0, "a", "a")["leaves"]
    assert ab.flatten(a["seeds"]).keys().isdisjoint({p for p in ab.flatten(a["evidence"]["seeds"], "evidence")}), "no leaf of the evidence is a leaf of the seeds"
    # and the same difference in the compared part IS one: the control of the test above
    c = json.loads(json.dumps(a))
    c["seeds"]["4"]["webrtc"]["udp"]["counts"][ab.TURN_TARGET] = 9
    assert [d["path"] for d in ab.compare_transcripts(a, c, [], 0, "a", "c")["differences"]] == ["seeds/4/webrtc/udp/counts/198.51.100.7:3478"]


def test_a_run_writes_the_evidence_of_the_walk_under_the_evidence_of_its_seed(tmp_path, monkeypatch):
    import _lab_egress
    import ci_crash_evidence as crash

    def walk_seed(binary, seed, with_idle, patch):
        ab.EVIDENCE[str(seed)] = evidence_transcript("x", ["aa" * 12] * 2)["evidence"]["seeds"]["4"]
        return {"webrtc": {}}

    monkeypatch.setattr(ab, "walk_seed", walk_seed)
    monkeypatch.setattr(_lab_egress, "install", lambda patch, work: None)
    monkeypatch.setenv(crash.CRASH_DIR_ENV, str(tmp_path / "t.crash"))
    try:
        assert ab.run("/e/firefox", "firefox-68", (4,), tmp_path / "t.json") == 0
    finally:
        crash.stop_probe()
    document = json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))
    assert [d["tid"] for d in document["evidence"]["seeds"]["4"]["webrtc"]["datagrams"]] == ["aa" * 12] * 2
    assert set(document) >= {"seeds", "evidence", "exits"} and "webrtc" not in document["evidence"], "the evidence is under the seed, not beside the seeds"


# ----------------------------------------------------------------------------------------------------------------- the page's probe
def test_the_probe_returns_what_it_returned_and_its_times_beside_it():
    js = ab.RTC_JS
    assert "return {candidates: cands, errors, sdp, gathering, times: T};" in js, "the four fields that are compared come first and unchanged; `times` is the only addition"
    assert "cands.push(e.candidate.candidate)" in js and "errors.push({code: e.errorCode, url: e.url})" in js
    for what in ("T.offer = performance.now()", "T.gathered = performance.now()", "T.closing = performance.now()", "T.closed = performance.now()"):
        assert what in js, what
    assert re.search(r"T\.candidates\.push\(\{t: performance\.now\(\), type: ", js), "each icecandidate, with its type"
    assert re.search(r"T\.errors\.push\(\{t: performance\.now\(\), code: e\.errorCode, url: e\.url\}\)", js), "each icecandidateerror, with its code and url"
    assert js.index("T.closing = performance.now()") < js.index("pc.close()") < js.index("T.closed = performance.now()"), "the close is bracketed by its two times"


def test_what_the_page_measures_of_itself_is_in_the_evidence_and_the_summary_of_the_walk_is_the_same_with_any_times():
    class Page:
        def __init__(self, result):
            self.result = result

        def goto(self, *args, **kwargs):
            pass

        def evaluate(self, js, arg):
            assert js == ab.RTC_JS and arg == [ab.STUN_URL, ab.TURN_URL]
            return json.loads(json.dumps(self.result))

    class Browser:
        def __init__(self, result):
            self.page = Page(result)

        def new_context(self, **kwargs):
            return types.SimpleNamespace(new_page=lambda: self.page, close=lambda: None)

    base = {"candidates": ["candidate:1 1 udp 100 192.0.2.1 5000 typ host"], "errors": [{"code": 701, "url": "turn:198.51.100.7:3478"}], "sdp": "v=0\r\n", "gathering": "complete"}
    outs, labs = [], []
    for times in ({"start": 1.0, "gathered": 2.0}, {"start": 5.5, "gathered": 9.25, "closed": 11.0}, None):
        result = dict(base) if times is None else dict(base, times=times)
        with ab.Lab(host="127.0.0.1") as lab:
            outs.append(json.dumps(ab.walk_webrtc(Browser(result), lab), sort_keys=True))
            labs.append(lab.webrtc)
    assert outs[0] == outs[1] == outs[2], "the walk's summary does not depend on the page's times"
    assert '"times"' not in outs[0] and "gathered" not in outs[0]
    assert [x["page"] for x in labs] == [{"start": 1.0, "gathered": 2.0}, {"start": 5.5, "gathered": 9.25, "closed": 11.0}, None]
    assert all(x["evaluate"]["called"] <= x["evaluate"]["returned"] for x in labs) and all(isinstance(x["counted"], int) for x in labs)


# ----------------------------------------------------------------------------------------------------------------- the hunt
def repetition(engine, i, tids, late=0, error=None):
    """A repetition file as ``hunt_repetition`` writes it: ``tids`` are the transaction ids of the datagrams to the TURN server in order, the first ``late`` of the last ones after the close."""
    datagrams = [{"t": 0.1 * n, "phase": "webrtc.end" if n >= len(tids) - late else "webrtc", "target": ab.TURN_TARGET, "message": "stun-0x0003", "tid": tid, "counted": n < len(tids) - late}
                 for n, tid in enumerate(tids)]
    datagrams.append({"t": 0.05, "phase": "webrtc", "target": "198.51.100.5:19302", "message": "stun-0x0001", "tid": "dd" * 12, "counted": True})
    record = {"format": ab.HUNT_FORMAT, "engine": engine, "i": i, "seed": 4, "error": error, "evidence": {"phase_t": {}, "evaluate": None, "page": None, "datagrams": datagrams}}
    record["counts"] = ab.hunt_counts(record["evidence"])
    return record


def test_the_hunt_summary_counts_the_datagrams_to_the_turn_server_and_lists_the_transactions_of_each_target():
    a, b = "aa" * 12, "bb" * 12
    records = [repetition("firefox-68", 1, [a, a, a, b, b]), repetition("firefox-68", 2, [a, a, b, b, b, b], late=1), repetition("firefox-68-again", 1, [a] * 4 + [b] * 4),
               repetition("firefox-68-again", 2, [], error="Error: launch")]
    summary = ab.hunt_summary(records)
    json.dumps(summary)
    assert summary["format"] == ab.HUNT_SUMMARY_FORMAT and summary["target"] == "198.51.100.7:3478"
    first, second, third, failed = summary["repetitions"]
    assert (first["engine"], first["i"], first["count_3478"], first["count_3478_all"]) == ("firefox-68", 1, 5, 5)
    assert first["targets"] == {"198.51.100.5:19302": [["dd" * 12, 1]], ab.TURN_TARGET: [[a, 3], [b, 2]]}
    assert (second["count_3478"], second["count_3478_all"]) == (5, 6), "a datagram after the close is in `all` and not in the count the A/A makes"
    assert second["targets"][ab.TURN_TARGET] == [[a, 2], [b, 4]]
    assert (third["count_3478"], third["targets"][ab.TURN_TARGET]) == (8, [[a, 4], [b, 4]])
    assert failed["error"] == "Error: launch" and failed["count_3478"] == 0
    assert summary["count_3478_by_engine"] == {"firefox-68": {"5": 2}, "firefox-68-again": {"0": 1, "8": 1}}


def test_the_hunt_summary_is_read_from_the_repetition_files_and_does_not_read_itself(tmp_path):
    for record in (repetition("firefox-68-again", 2, ["aa" * 12]), repetition("firefox-68", 10, ["aa" * 12]), repetition("firefox-68", 2, ["aa" * 12] * 2)):
        ab.write_json(tmp_path / ("%s-%d.json" % (record["engine"], record["i"])), record)
    ab.write_json(tmp_path / "summary.json", ab.hunt_summary(ab.read_hunt_records(tmp_path)))
    (tmp_path / "notes.txt").write_text("not json", encoding="utf-8")
    records = ab.read_hunt_records(tmp_path)
    assert [(r["engine"], r["i"]) for r in records] == [("firefox-68", 2), ("firefox-68", 10), ("firefox-68-again", 2)], "by engine, then by number (10 after 2)"
    assert len(ab.hunt_summary(records)["repetitions"]) == 3


@pytest.mark.parametrize("text, expected", [("webrtc:4:20", ("webrtc", 4, 20)), (" webrtc:11:1 ", ("webrtc", 11, 1))])
def test_the_hunt_spec_names_the_walk_the_seed_and_the_count(text, expected):
    assert ab.parse_hunt(text) == expected


@pytest.mark.parametrize("text", ["", "webrtc", "webrtc:4", "webrtc:four:3", "webrtc:4:0", "webrtc:4:-1", "h3:4:3", "webrtc:4:3:1", "4:3"])
def test_a_hunt_spec_that_does_not_read_is_refused(text):
    with pytest.raises(ValueError, match=ab.HUNT_ENV):
        ab.parse_hunt(text)



def test_ab_with_the_hunt_variable_runs_only_the_walk_n_times_for_each_engine_of_the_a_a_and_exits_0(tmp_path, monkeypatch, capsys):
    calls = []

    def hunt_engine(binary, seal, label, seed, n, out_dir):
        calls.append((binary, seal, label, seed, n))
        for i in range(1, n + 1):
            ab.write_json(out_dir / ("%s-%d.json" % (label, i)), repetition(label, i, ["aa" * 12] * (8 + i % 2)))
        return 0

    def never(*a, **k):
        raise AssertionError("the A/A, the A/B and the volatile list are not part of a hunt")

    monkeypatch.setattr(ab, "hunt_engine", hunt_engine)
    monkeypatch.setattr(ab, "run_engine", never)
    monkeypatch.setattr(ab, "compare_files", never)
    monkeypatch.setenv(ab.HUNT_ENV, "webrtc:4:3")
    assert ab.main(["ab", "--a-a", "--out", str(tmp_path / "network"), "--published-binary", "/e/firefox"]) == 0
    binary = str(Path("/e/firefox"))
    assert calls == [(binary, None, "firefox-68", 4, 3), (binary, None, "firefox-68-again", 4, 3)]
    hunt = tmp_path / "network" / "hunt"
    assert sorted(p.name for p in hunt.iterdir()) == sorted(["summary.json"] + ["%s-%d.json" % (e, i) for e in ("firefox-68", "firefox-68-again") for i in (1, 2, 3)])
    summary = json.loads((hunt / "summary.json").read_text(encoding="utf-8"))
    assert [(r["engine"], r["i"], r["count_3478"]) for r in summary["repetitions"]] == [("firefox-68", 1, 9), ("firefox-68", 2, 8), ("firefox-68", 3, 9), ("firefox-68-again", 1, 9), ("firefox-68-again", 2, 8),
                                                                                       ("firefox-68-again", 3, 9)]
    assert summary["repetitions"][0]["targets"][ab.TURN_TARGET] == [["aa" * 12, 9]]
    assert not (tmp_path / "network" / "diff-a-a.json").exists()
    assert "hunt summary written" in capsys.readouterr().out


def test_a_hunt_decides_nothing_even_when_an_engine_run_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "hunt_engine", lambda *a, **k: 1)
    monkeypatch.setenv(ab.HUNT_ENV, "webrtc:4:2")
    assert ab.main(["ab", "--a-a", "--out", str(tmp_path / "network"), "--published-binary", "/e/firefox"]) == 0
    assert json.loads((tmp_path / "network" / "hunt" / "summary.json").read_text(encoding="utf-8"))["repetitions"] == []


def test_a_hunt_variable_that_does_not_read_is_refused_and_runs_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ab, "hunt_engine", lambda *a, **k: pytest.fail("a spec that does not read runs nothing"))
    monkeypatch.setenv(ab.HUNT_ENV, "webrtc:4")
    assert ab.main(["ab", "--a-a", "--out", str(tmp_path / "network"), "--published-binary", "/e/firefox"]) == 2
    assert "FAIL AB_NETWORK_HUNT" in capsys.readouterr().out


def test_without_the_hunt_variable_ab_is_the_a_a_it_was(tmp_path, monkeypatch):
    monkeypatch.delenv(ab.HUNT_ENV, raising=False)
    monkeypatch.setattr(ab, "hunt_engine", lambda *a, **k: pytest.fail("no hunt without the variable"))
    ran = []
    monkeypatch.setattr(ab, "run_engine", lambda binary, seal, label, seeds, out: ran.append(label) or 1)
    assert ab.main(["ab", "--a-a", "--out", str(tmp_path / "network"), "--published-binary", "/e/firefox", "--seeds", "4"]) == 2
    assert ran == ["firefox-68", "firefox-68-again"], "the A/A runs the engine twice and says so when neither made a transcript: its own red, as before"


def test_the_engine_of_a_hunt_runs_in_a_process_of_its_own_with_its_seal_and_its_own_cache(tmp_path, monkeypatch):
    seen = {}

    class Done:
        returncode = 0

    monkeypatch.setattr(ab.subprocess, "run", lambda cmd, env, timeout: seen.update(cmd=cmd, env=env, timeout=timeout) or Done())
    assert ab.hunt_engine("/e/firefox", "/e/seal.json", "firefox-68-again", 4, 20, tmp_path / "hunt") == 0
    cmd = seen["cmd"]
    assert cmd[3:] == ["hunt-run", "--binary", "/e/firefox", "--label", "firefox-68-again", "--seed", "4", "--n", "20", "--out", str(tmp_path / "hunt")]
    assert seen["env"]["INVISIBLE_SEAL_FILE"] == "/e/seal.json" and seen["env"]["INVISIBLE_CORE_AUTOFIX"] == "off"
    assert seen["timeout"] == ab.HUNT_BASE_S + 20 * ab.HUNT_PER_REPETITION_S


def test_a_repetition_is_one_launch_the_walk_alone_and_the_evidence_read_after_the_close(monkeypatch):
    order = []

    class Session:
        def __enter__(self):
            order.append("open")
            return types.SimpleNamespace(new_context=lambda **k: types.SimpleNamespace(new_page=lambda: order.append("page")))

        def __exit__(self, *exc):
            order.append("closed")

    monkeypatch.setattr(ab, "launch", lambda binary, seed, lab, patch: Session())
    monkeypatch.setattr(ab, "begin_launch", lambda seed, name: order.append("begin " + name))
    monkeypatch.setattr(ab, "glean_ready", lambda session, seed, name: order.append("glean") or {})
    monkeypatch.setattr(ab, "walk_webrtc", lambda browser, lab: order.append("walk") or {"udp": {}})
    for walker in ("walk_journey", "walk_h3", "walk_https"):
        monkeypatch.setattr(ab, walker, lambda *a, **k: pytest.fail("a hunt walks nothing but the WebRTC page"))
    monkeypatch.setattr(ab, "webrtc_evidence", lambda lab: order.append("evidence") or {"datagrams": [{"target": ab.TURN_TARGET, "counted": True}, {"target": ab.TURN_TARGET, "counted": False}]})
    record = ab.hunt_repetition("/e/firefox", "firefox-68", 3, 4, object())
    assert order == ["begin hunt", "open", "page", "glean", "walk", "closed", "evidence"]
    assert (record["engine"], record["i"], record["seed"], record["error"], record["counts"]) == ("firefox-68", 3, 4, None, {"counted": 1, "all": 2})


def test_a_repetition_that_fails_is_a_record_with_its_error_and_the_hunt_goes_on(monkeypatch):
    def broken(binary, seed, lab, patch):
        raise RuntimeError("the engine did not start 17")

    monkeypatch.setattr(ab, "launch", broken)
    monkeypatch.setattr(ab, "begin_launch", lambda seed, name: None)
    record = ab.hunt_repetition("/e/firefox", "firefox-68", 1, 4, object())
    assert record["error"] == "RuntimeError: the engine did not start N" and record["evidence"] is None and record["counts"] == {"counted": 0, "all": 0}


# ----------------------------------------------------------------------------------------------------------------- e2e-hunt.yml
def steps_of(text):
    """The steps of the hunt job: ``{name: block}`` (the blocks of the lines that start with ``      - ``)."""
    job = text[text.index("\n  hunt:\n"):]
    blocks = re.split(r"\n(?=      - )", job)
    return {b.split("name:", 1)[1].split("\n", 1)[0].strip(): b for b in blocks if b.startswith("      - name:")}


def test_the_hunt_workflow_has_the_input_that_chooses_the_walk_and_stays_a_manual_one():
    text = HUNT_WORKFLOW.read_text(encoding="utf-8")
    on = text[text.index("\non:\n"):text.index("\npermissions:")]
    assert "  workflow_dispatch:\n" in on and "push:" not in on and "pull_request" not in on and "schedule:" not in on
    inputs = on[on.index("    inputs:\n"):]
    block = inputs[inputs.index("      ab_network_webrtc_seed:\n"):]
    block = block[:block.index("\n      os:")]
    assert 'default: ""' in block and "required: false" in block, "empty is today's behaviour"
    assert "      runs:\n" in inputs and "      os:\n" in inputs, "the inputs it had are still there"


def test_the_step_of_the_walk_runs_the_hunt_mode_only_when_the_input_is_set_and_the_e2e_loop_only_when_it_is_not():
    steps = steps_of(HUNT_WORKFLOW.read_text(encoding="utf-8"))
    walk = next(b for n, b in steps.items() if n.startswith("The network oracle's WebRTC walk of one seed"))
    loop = steps["Loop until it catches one"]
    assert "if: inputs.ab_network_webrtc_seed != ''" in walk and "if: inputs.ab_network_webrtc_seed == ''" in loop
    assert "AB_NETWORK_HUNT: webrtc:${{ inputs.ab_network_webrtc_seed }}:${{ inputs.runs }}" in walk, "the seed and the number of repetitions come from the inputs, through the environment"
    assert "${{ inputs." not in walk.split("run: |")[1], "no input is written into the script text"
    assert "scripts/ci_ab_network.py ab --a-a --out network --published-binary \"$FF\"" in walk
    assert "xvfb-run -a" in walk and 'RUNNER_OS" = "Linux"' in walk
    assert "scripts/run_e2e.py" in loop and "run_e2e" not in walk
    for name in ("The security state (a fixed directory of the repository)", "The GeoIP database (made by the repository, same bytes every run)"):
        assert "if: inputs.ab_network_webrtc_seed != ''" in steps[name], "what only the hunt needs is made only for it"
    names = list(steps)
    assert names.index("The security state (a fixed directory of the repository)") < names.index("Fetch the published firefox binary") < names.index(next(n for n in names if n.startswith("The network oracle's WebRTC walk")))


def test_the_hunt_uploads_network_hunt_as_an_artifact_and_every_action_it_uses_is_pinned_by_sha():
    text = HUNT_WORKFLOW.read_text(encoding="utf-8")
    steps = steps_of(text)
    upload = next(b for n, b in steps.items() if n.startswith("Upload the hunt of the network oracle's WebRTC walk"))
    assert "if: always() && inputs.ab_network_webrtc_seed != ''" in upload and "path: network/hunt" in upload
    assert re.search(r"uses: actions/upload-artifact@[0-9a-f]{40} # v4", upload)
    for line in text.splitlines():
        m = re.match(r"\s*- uses: (\S+)|\s+uses: (\S+)", line)
        if m and not (m.group(1) or m.group(2)).startswith("./"):
            assert re.search(r"@[0-9a-f]{40}\b", line), line
