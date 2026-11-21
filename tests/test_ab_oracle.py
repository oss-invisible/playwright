"""The A/B oracle's comparator (scripts/ci_ab_oracle.py), on synthetic dumps, no browser.

The oracle asks one question of two engines: same seed, same bytes. Each property is shown on the input it exists for: two dumps that differ in one leaf are red
(with the leaf, the two values and the seed), equal ones are green, a difference in a `noise` leaf or under diag. is the only thing excused, a visit that
compares fewer leaves than the host gate's own reference is red (a page that dumped less passes anything), and a visit missing on one side is red.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ci_ab_oracle as ab  # noqa: E402
import ci_host_gate as gate  # noqa: E402

pytestmark = pytest.mark.unit

VISITS = tuple((dpr, seed) for dpr in gate.DPRS for seed in gate.SEEDS)
SMALL = {dpr: 6 for dpr in gate.DPRS}


def items(seed=11):
    """A visit of the page: nested objects, an image, a list, and a diag. value."""
    return {
        "surface": {"navigator": {"hardwareConcurrency": 8 + seed % 3, "language": "en-US"}, "screen": {"width": 1920, "height": 1080}},
        "text": {"fonts": ["Arial", "Segoe UI"]},
        "blend": {"data": "AAEC", "enc": "png", "w": 1, "h": 1},
        "diag.webgl": {"webgl2": True, "message": "llvmpipe"},
    }


def visits():
    return {key: items(key[1]) for key in VISITS}


def run(a, b, noise=(), min_leaves=SMALL):
    return ab.compare_visits(a, b, list(noise), min_leaves)


def test_the_fake_visit_has_the_leaves_the_floor_expects():
    assert len(ab.comparable(items())) == 6  # hardwareConcurrency, language, width, height, fonts, blend (an image is one leaf)


def test_equal_dumps_are_green():
    report = run(visits(), visits())
    assert report["verdict"] == "green" and report["problems"] == [] and report["differences"] == []
    assert set(report["compared"]) == {ab.visit_name(*k) for k in VISITS}
    assert set(report["compared"].values()) == {6}


def test_a_difference_in_one_leaf_is_red_and_names_the_leaf_the_values_and_the_seed():
    b = visits()
    b[(1.5, 12)]["surface"]["navigator"]["language"] = "it-IT"
    report = run(visits(), b)
    assert report["verdict"] == "red"
    assert len(report["differences"]) == 1
    d = report["differences"][0]
    assert (d["seed"], d["dpr"], d["leaf"], d["a"], d["b"]) == (12, 1.5, "surface.navigator.language", "en-US", "it-IT")
    assert report["problems"] == ["seed 12 dpr 1.5 surface.navigator.language: \"en-US\" vs \"it-IT\""]


def test_the_same_leaf_differing_at_every_seed_is_one_difference_per_seed():
    b = visits()
    for key in b:
        b[key]["surface"]["screen"]["width"] = 1366
    report = run(visits(), b)
    assert len(report["differences"]) == len(VISITS) and {d["seed"] for d in report["differences"]} == set(gate.SEEDS)


def test_a_leaf_that_one_engine_does_not_have_is_red():
    b = visits()
    del b[(1.0, 11)]["surface"]["navigator"]["language"]
    report = run(visits(), b)
    d = report["differences"][0]
    assert report["verdict"] == "red" and d["leaf"] == "surface.navigator.language" and d["b"] == ab.MISSING


def test_a_leaf_that_the_other_engine_adds_is_red():
    b = visits()
    b[(1.0, 11)]["surface"]["navigator"]["maxTouchPoints"] = 0
    report = run(visits(), b)
    d = report["differences"][0]
    assert report["verdict"] == "red" and d["leaf"] == "surface.navigator.maxTouchPoints" and d["a"] == ab.MISSING


def test_a_list_that_differs_by_one_element_is_red():
    b = visits()
    b[(1.0, 13)]["text"]["fonts"].append("Consolas")
    assert run(visits(), b)["verdict"] == "red"


def test_an_image_that_differs_by_one_byte_is_red_and_is_said_by_its_bytes():
    b = visits()
    b[(1.0, 11)]["blend"]["data"] = "AAED"
    report = run(visits(), b)
    assert report["verdict"] == "red" and report["differences"][0]["leaf"] == "blend"
    assert "1 of 3 bytes differ" in report["differences"][0]["what"]


def test_a_difference_in_a_noise_leaf_is_green():
    b = visits()
    for key in b:
        b[key]["surface"]["navigator"]["language"] = "it-IT"
    noise = [{"keys": ["surface.navigator.language"], "why": "differs between two launches of retail itself"}]
    report = run(visits(), b, noise, min_leaves={dpr: 5 for dpr in gate.DPRS})  # the excused leaf is not compared, so not counted
    assert report["verdict"] == "green" and report["differences"] == []
    assert report["excused"] == len(VISITS) and set(report["compared"].values()) == {5}


def test_a_noise_entry_excuses_only_what_it_names_and_matches_wildcards():
    b = visits()
    b[(1.0, 11)]["surface"]["navigator"]["language"] = "it-IT"
    b[(1.0, 11)]["surface"]["screen"]["width"] = 1366
    noise = [{"keys": ["surface.navigator.*"], "why": "x"}]
    report = run(visits(), b, noise, min_leaves={dpr: 4 for dpr in gate.DPRS})
    assert [d["leaf"] for d in report["differences"]] == ["surface.screen.width"]


def test_a_difference_under_the_diag_prefix_is_green():
    b = visits()
    for key in b:
        b[key]["diag.webgl"]["message"] = "ANGLE"
        b[key]["diag.fonts"] = {"host": "linux"}
    report = run(visits(), b)
    assert report["verdict"] == "green" and report["differences"] == []
    assert set(report["compared"].values()) == {6}  # diag leaves are not compared, nor counted


def test_a_key_that_only_contains_diag_is_not_excused():
    b = visits()
    b[(1.0, 11)]["surface"]["diag"] = {"x": 1}
    assert run(visits(), b)["verdict"] == "red"


def test_fewer_leaves_than_the_minimum_is_red_even_when_the_dumps_are_equal():
    a = visits()
    for key in a:
        del a[key]["text"]
    report = ab.compare_visits(a, copy.deepcopy(a), [], SMALL)
    assert report["verdict"] == "red" and report["differences"] == []
    assert len(report["problems"]) == len(VISITS)
    assert report["problems"][0].endswith("fewer than the 6 of the host gate's reference: the page dumped less than it should")
    assert report["problems"][0].startswith("dpr1.0_seed11 compared 5 leaves")


def test_the_minimum_is_per_visit_and_per_dpr():
    a = visits()
    del a[(1.5, 13)]["text"]
    b = copy.deepcopy(a)
    report = ab.compare_visits(a, b, [], {1.0: 6, 1.5: 6})
    assert [p.split(" ")[0] for p in report["problems"]] == ["dpr1.5_seed13"]
    assert ab.compare_visits(a, b, [], {1.0: 6, 1.5: 5})["verdict"] == "green"


def test_leaves_that_noise_excuses_do_not_count_toward_the_minimum():
    noise = [{"keys": ["text.fonts"], "why": "x"}]
    report = ab.compare_visits(visits(), visits(), noise, {1.0: 6, 1.5: 6})
    assert report["verdict"] == "red" and set(report["compared"].values()) == {5}


def test_a_visit_missing_on_one_side_is_red():
    b = visits()
    del b[(1.5, 12)]
    report = run(visits(), b)
    assert report["verdict"] == "red" and report["problems"] == ["side B has no dump of dpr1.5_seed12"]
    report = run(b, visits())
    assert report["problems"] == ["side A has no dump of dpr1.5_seed12"]


def test_no_dumps_at_all_is_red_never_green():
    report = run({}, {})
    assert report["verdict"] == "red" and len(report["problems"]) == 2 and report["compared"] == {}


def test_a_visit_that_was_not_asked_for_is_red():
    b = visits()
    b[(1.0, 99)] = items()
    report = run(visits(), b)
    assert report["verdict"] == "red" and "not asked for: dpr1.0_seed99" in report["problems"][0]


def test_the_floor_is_what_the_host_gate_s_own_reference_holds_today():
    for dpr in gate.DPRS:
        ref = ab.comparable(gate.reference_items(gate.REFERENCE, dpr))
        assert ab.MIN_LEAVES[dpr] == len(ref), "the reference holds %d leaves at dpr %s: move MIN_LEAVES with it" % (len(ref), dpr)


def test_the_published_engine_is_firefox_68_and_its_dump_is_named_for_it():
    assert ab.ENGINE_TAG == "firefox-68" and ab.dump_name("firefox-68") == "dump-68.json"
    assert ab.dump_name("firefox-69") == "dump-69.json"


# --- the files --------------------------------------------------------------------------------------------------------------------------------

def write_gate_dump(directory, engine_visits):
    """What ci_host_gate.dump writes: a file per visit."""
    directory.mkdir(parents=True, exist_ok=True)
    for (dpr, seed), its in engine_visits.items():
        (directory / ("leg_dpr%s_seed%d.json" % (dpr, seed))).write_text(
            json.dumps({"leg": "leg", "dpr": dpr, "seed": seed, "result": {"format": gate.FORMAT, "items": its}}), encoding="utf-8")


def test_the_visits_read_back_from_the_gate_s_dump_and_from_the_packed_file(tmp_path):
    write_gate_dump(tmp_path / "raw", visits())
    read = ab.read_visits(tmp_path / "raw")
    assert read == visits()
    assert ab.unpack(json.loads(json.dumps(ab.pack("firefox-68", read)))) == read
    with pytest.raises(SystemExit):
        ab.unpack({"format": "other", "visits": []})


def test_compare_from_the_files_writes_the_diff_and_exits_by_the_verdict(tmp_path, capsys):
    asked = {key: items(key[1]) for key in ab.select_visits()}  # the visits `compare` asks for: the persona seeds, each at its own DPR
    ab.write_json(tmp_path / "dump-68.json", ab.pack("firefox-68", asked))
    ab.write_json(tmp_path / "dump-candidate.json", ab.pack("candidate", asked))
    # the floor of the real reference does not apply to the fake page: the run is held to it, so this is red for that reason alone
    report = json.loads(_compare(tmp_path, capsys)[1])
    assert report["verdict"] == "red" and report["differences"] == [] and all("fewer than the 16439" in p for p in report["problems"])


def _compare(directory, capsys):
    rc = ab.main(["compare", str(directory)])
    capsys.readouterr()
    return rc, (directory / "diff.json").read_text(encoding="utf-8")


def test_a_run_writes_the_three_files_and_is_green_when_the_engines_agree(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ab, "MIN_LEAVES", SMALL)
    rc = _fake_run(tmp_path, monkeypatch, candidate=visits())
    out = capsys.readouterr().out
    assert rc == 0 and "AB ORACLE OK" in out
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["diff.json", "dump-68.json", "dump-candidate.json"]
    diff = json.loads((tmp_path / "out" / "diff.json").read_text(encoding="utf-8"))
    assert diff["verdict"] == "green" and diff["engine"] == "firefox-68" and diff["differences"] == []


def test_a_run_is_red_and_prints_the_leaf_the_values_and_the_seed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ab, "MIN_LEAVES", SMALL)
    b = visits()
    b[(1.0, 12)]["surface"]["screen"]["height"] = 1024
    rc = _fake_run(tmp_path, monkeypatch, candidate=b)
    out = capsys.readouterr().out
    assert rc == 1 and "AB ORACLE FAILED" in out
    assert "DIFF seed 12 dpr 1.0 leaf surface.screen.height: firefox-68 1080 | candidate 1024" in out
    diff = json.loads((tmp_path / "out" / "diff.json").read_text(encoding="utf-8"))
    assert diff["verdict"] == "red" and diff["differences"][0]["seed"] == 12


def test_a_dump_that_fails_is_red_even_when_it_wrote_equal_visits(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ab, "MIN_LEAVES", SMALL)
    rc = _fake_run(tmp_path, monkeypatch, candidate=visits(), code={"candidate": 3})
    out = capsys.readouterr().out
    assert rc == 1 and "FAIL the dump with candidate ended with 3" in out


def test_a_run_whose_engine_cannot_be_fetched_is_red_and_says_why(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ab, "MIN_LEAVES", SMALL)

    def broken(tag, work):
        raise RuntimeError("the release index is down")

    monkeypatch.setattr(ab, "fetch_published", broken)
    rc = ab.run("cand", tmp_path / "cand.seal.json", "leg", tmp_path / "out", "firefox-68")
    out = capsys.readouterr().out
    assert rc == 1 and "FAIL RuntimeError: the release index is down" in out
    assert (tmp_path / "out" / "diff.json").is_file()


def _fake_run(tmp_path, monkeypatch, candidate, published=None, code=None):
    """ab.run with the dumps replaced by files made here; the binaries and seals are names."""
    code = code or {}
    seen = []

    def fake_dump(binary, seal, leg, out, visits_asked):
        name = "candidate" if binary == "cand" else "firefox-68"
        seen.append((binary, str(seal), leg))
        write_gate_dump(out, candidate if name == "candidate" else (published or visits()))
        return code.get(name, 0)

    monkeypatch.setattr(ab, "run_dump", fake_dump)
    rc = ab.run("cand", tmp_path / "cand.seal.json", "linux-x86_64", tmp_path / "out", "firefox-68",
                published=("pub", tmp_path / "pub.seal.json"), visits=list(VISITS))  # the fake page is the old grid: its visits are asked for
    assert seen == [("pub", str(tmp_path / "pub.seal.json"), "linux-x86_64"), ("cand", str(tmp_path / "cand.seal.json"), "linux-x86_64")]
    return rc


def test_each_dump_runs_under_its_own_seal_and_without_the_engine_log(tmp_path, monkeypatch):
    calls = []

    class Done:
        returncode = 0

    monkeypatch.setenv("MOZ_LOG", "timestamp,sync")
    monkeypatch.setenv("MOZ_LOG_FILE", "x")
    monkeypatch.setenv("INVISIBLE_SEAL_FILE", "somebody-else.json")
    monkeypatch.setattr(ab.subprocess, "run", lambda cmd, env, timeout: calls.append((cmd, env, timeout)) or Done())
    assert ab.run_dump("ff", tmp_path / "s.json", "linux-x86_64", tmp_path / "o") == 0
    cmd, env, _ = calls[0]
    assert cmd[1:] == [str(Path(gate.__file__)), "dump", "ff", "--leg", "linux-x86_64", "--out", str(tmp_path / "o")]
    assert env["INVISIBLE_SEAL_FILE"] == str(tmp_path / "s.json") and "MOZ_LOG" not in env and "MOZ_LOG_FILE" not in env
    assert env["INVISIBLE_CORE_AUTOFIX"] == "off" and env["INVISIBLE_CORE_PIN"] == "allow"


# --- the seeds: the persona seeds (ci/persona-seeds.json), each at its own DPR, in shards ---------------------------------------------------------

def test_the_default_visits_are_the_candidate_persona_seeds_each_at_its_own_dpr():
    import persona_cover as personas

    stored = personas.load()
    asked = ab.select_visits()
    assert [seed for _, seed in asked] == stored["candidate"] and len(asked) == 5
    assert all(dpr == stored["dpr"][str(seed)] for dpr, seed in asked)
    assert {dpr for dpr, _ in asked} == {1.0, 1.25, 1.5}, "the candidate must reach every DPR of the personas"


def test_the_final_set_split_in_shards_is_the_final_set_once():
    import persona_cover as personas

    final = personas.load()["final"]
    for n in (1, 2, 3, 4, 5, 20):
        parts = [ab.select_visits("final", "%d/%d" % (i, n)) for i in range(1, n + 1)]
        seeds = [seed for part in parts for _, seed in part]
        assert sorted(seeds) == final and len(seeds) == len(set(seeds)), n
        assert max(map(len, parts)) - min(map(len, parts)) <= 1, "the shards are even: %s" % [len(p) for p in parts]


@pytest.mark.parametrize("text", ["0/4", "5/4", "1/0", "a/b", "1", "1/2/3", "-1/4"])
def test_a_bad_shard_is_refused(text):
    with pytest.raises(SystemExit):
        ab.parse_shard(text)


def test_a_shard_with_no_seed_and_an_unknown_set_are_refused():
    with pytest.raises(SystemExit):
        ab.select_visits("candidate", "6/6")  # 5 seeds, the 6th of 6 shards has none
    with pytest.raises(SystemExit):
        ab.select_visits("everything")


def test_the_dump_of_the_persona_seeds_pins_no_dpr_and_names_each_visit(tmp_path, monkeypatch):
    calls = []

    class Done:
        returncode = 0

    monkeypatch.setattr(ab.subprocess, "run", lambda cmd, env, timeout: calls.append(cmd) or Done())
    ab.run_dump("ff", tmp_path / "s.json", "linux-x86_64", tmp_path / "o", [(1.25, 6), (1.0, 0)])
    cmd = calls[0]
    assert "--no-pin" in cmd and cmd[cmd.index("--visit") + 1] == "6:1.25" and cmd.count("--visit") == 2 and cmd[-1] == "0:1.0"
    assert "pin" not in gate.visit_options("ff", 6, None, "UTC") and gate.visit_options("ff", 6, 1.25, "UTC")["pin"] == {"screen.dpr": 1.25}


def test_a_visit_of_the_persona_set_that_is_missing_or_that_was_not_asked_for_is_red():
    asked = ab.select_visits()
    both = {key: items(key[1]) for key in asked}
    assert ab.compare_visits(both, copy.deepcopy(both), [], SMALL, expected=tuple(asked))["verdict"] == "green"
    short = dict(both)
    gone = short.pop(asked[-1])
    report = ab.compare_visits(both, short, [], SMALL, expected=tuple(asked))
    assert report["problems"] == ["side B has no dump of %s" % ab.visit_name(*asked[-1])]
    old_grid = dict(both)
    old_grid[(1.5, 11)] = gone
    assert "not asked for: dpr1.5_seed11" in ab.compare_visits(both, old_grid, [], SMALL, expected=tuple(asked))["problems"][0]


def test_a_dpr_the_reference_does_not_record_is_held_to_the_least_of_those_it_does():
    one = {key: items(key[1]) for key in [(1.25, 6)]}
    report = ab.compare_visits(one, copy.deepcopy(one), [], {1.0: 6, 1.5: 7}, expected=((1.25, 6),))
    assert report["verdict"] == "green"
    report = ab.compare_visits(one, copy.deepcopy(one), [], {1.0: 7, 1.5: 8}, expected=((1.25, 6),))
    assert report["verdict"] == "red" and "fewer than the 7" in report["problems"][0]
