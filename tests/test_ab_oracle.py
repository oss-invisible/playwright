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
    """A visit of the page: nested objects, an image, a list, and a diag. value. The visit of seed 0 also has the second launch on a reused profile, under relaunch."""
    out = {
        "surface": {"navigator": {"hardwareConcurrency": 8 + seed % 3, "language": "en-US"}, "screen": {"width": 1920, "height": 1080}},
        "text": {"fonts": ["Arial", "Segoe UI"]},
        "blend": {"data": "AAEC", "enc": "png", "w": 1, "h": 1},
        "diag.webgl": {"webgl2": True, "message": "llvmpipe"},
    }
    if seed == ab.RELAUNCH_SEED:
        out.update(copy.deepcopy(gate.relaunch_items(out)))
    return out


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


def test_a_launch_of_an_oracle_keeps_its_user_cache_to_itself_and_the_wrapper_keeps_finding_its_engine(tmp_path, monkeypatch):
    """run 44946411675: a cache of the bundle's fonts left in the user's folders made the first launch of the fontconfig test warm. The oracles' launches write theirs elsewhere."""
    import platformdirs

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "user"))
    monkeypatch.delenv("INVISIBLE_PLAYWRIGHT_CACHE_DIR", raising=False)
    where_the_engine_is = platformdirs.user_cache_dir("invisible-playwright")
    base = {"A": "1", "XDG_CACHE_HOME": str(tmp_path / "user")}
    env = ab.own_cache_env(base, str(tmp_path / "mine"), platform="linux")
    assert env["XDG_CACHE_HOME"] == str(tmp_path / "mine") and env["INVISIBLE_PLAYWRIGHT_CACHE_DIR"] == where_the_engine_is and env["A"] == "1"
    assert base == {"A": "1", "XDG_CACHE_HOME": str(tmp_path / "user")}, "the environment it was given is not changed"
    kept = ab.own_cache_env({"INVISIBLE_PLAYWRIGHT_CACHE_DIR": "/the/callers"}, "x", platform="linux")
    assert kept["INVISIBLE_PLAYWRIGHT_CACHE_DIR"] == "/the/callers", "a cache folder the caller chose is kept"
    given = {"A": "1"}
    assert ab.own_cache_env(given, "x", platform="win32") is given, "Windows has no fontconfig: nothing changes"


def test_every_dump_of_an_oracle_runs_in_a_cache_folder_of_its_own_that_is_gone_when_it_ends(tmp_path, monkeypatch):
    seen = []

    class Done:
        returncode = 0

    original = ab.own_cache_env
    monkeypatch.setattr(ab, "own_cache_env", lambda env, folder: original(env, folder, platform="linux"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "user"))
    monkeypatch.setattr(ab.subprocess, "run", lambda cmd, env, timeout: seen.append((env["XDG_CACHE_HOME"], Path(env["XDG_CACHE_HOME"]).is_dir())) or Done())
    ab.run_dump("ff", None, "linux-x86_64", tmp_path / "o", [(1.0, 0)])
    ab.run_dump("ff", None, "linux-x86_64", tmp_path / "o", [(1.0, 0)])
    (first, there1), (second, there2) = seen
    assert there1 and there2 and first != second and str(tmp_path / "user") not in (first, second)
    assert not Path(first).exists() and not Path(second).exists()


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


# --- the control: the A/A (the published engine twice), alone on a pull request, first with a candidate ----------------------------------------------

def _aa_run(tmp_path, monkeypatch, dumps, candidate=None, published=("pub", None), limit=None, codes=None):
    """ab.run_ab with the dumps replaced by files made here. `dumps` is the list of visits each successive dump writes; returns the code and the calls."""
    calls = []
    codes = list(codes or [])
    queue = list(dumps)

    def fake_dump(binary, seal, leg, out, visits_asked):
        calls.append((binary, None if seal is None else str(seal), leg, list(visits_asked)))
        write_gate_dump(out, queue.pop(0))
        return codes.pop(0) if codes else 0

    monkeypatch.setattr(ab, "run_dump", fake_dump)
    monkeypatch.setattr(ab, "MIN_LEAVES", SMALL)
    asked = ab.select_visits(limit=limit)
    full = {key: items(key[1]) for key in asked}
    queue = [full if d is None else d(full) for d in queue]
    rc = ab.run_ab("linux-x86_64", tmp_path / "out", "firefox-68", published, candidate, tmp_path / "cand.seal.json" if candidate else None, asked)
    return rc, calls


def _changed(full):
    other = copy.deepcopy(full)
    key = sorted(other)[0]
    other[key]["surface"]["screen"]["width"] = 1366
    return other


def test_the_a_a_alone_runs_the_published_engine_twice_and_is_green_when_it_repeats(tmp_path, monkeypatch, capsys):
    rc, calls = _aa_run(tmp_path, monkeypatch, [None, None], limit=2)
    out = capsys.readouterr().out
    assert rc == 0 and "AB ORACLE A/A OK" in out
    assert [c[0] for c in calls] == ["pub", "pub"] and all(c[1] is None and len(c[3]) == 2 for c in calls)
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["diff-a-a.json", "dump-68-again.json", "dump-68.json"]
    diff = json.loads((tmp_path / "out" / "diff-a-a.json").read_text(encoding="utf-8"))
    assert diff["verdict"] == "green" and diff["control"] is True and len(diff["visits"]) == 2


def test_an_a_a_that_differs_is_a_broken_control_exit_2_and_names_the_leaf(tmp_path, monkeypatch, capsys):
    rc, calls = _aa_run(tmp_path, monkeypatch, [None, _changed], limit=2)
    out = capsys.readouterr().out
    assert rc == 2 and "BROKEN CONTROL" in out and "AB ORACLE A/A FAILED" in out
    assert "leaf surface.screen.width: firefox-68 1920 | firefox-68-again 1366" in out
    diff = json.loads((tmp_path / "out" / "diff-a-a.json").read_text(encoding="utf-8"))
    assert diff["verdict"] == "red" and len(diff["differences"]) == 1


def test_a_broken_control_does_not_run_the_candidate(tmp_path, monkeypatch, capsys):
    rc, calls = _aa_run(tmp_path, monkeypatch, [None, _changed, None], candidate="cand")
    assert rc == 2 and [c[0] for c in calls] == ["pub", "pub"]
    assert not (tmp_path / "out" / "dump-candidate.json").exists() and not (tmp_path / "out" / "diff.json").exists()


def test_a_dump_of_the_a_a_that_does_not_run_is_a_broken_control(tmp_path, monkeypatch, capsys):
    rc, _ = _aa_run(tmp_path, monkeypatch, [None, None], codes=[0, 3])
    out = capsys.readouterr().out
    assert rc == 2 and "FAIL the dump with firefox-68-again ended with 3" in out


def test_with_a_candidate_the_a_a_comes_first_and_the_a_b_is_against_the_first_dump(tmp_path, monkeypatch, capsys):
    rc, calls = _aa_run(tmp_path, monkeypatch, [None, None, None], candidate="cand")
    out = capsys.readouterr().out
    assert rc == 0 and "AB ORACLE A/A OK" in out and "AB ORACLE OK" in out
    assert [(c[0], c[1]) for c in calls] == [("pub", None), ("pub", None), ("cand", str(tmp_path / "cand.seal.json"))]
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["diff-a-a.json", "diff.json", "dump-68-again.json", "dump-68.json", "dump-candidate.json"]


def test_with_a_candidate_that_differs_the_a_b_is_red_exit_1_not_a_broken_control(tmp_path, monkeypatch, capsys):
    rc, _ = _aa_run(tmp_path, monkeypatch, [None, None, _changed], candidate="cand")
    out = capsys.readouterr().out
    assert rc == 1 and "BROKEN CONTROL" not in out and "AB ORACLE FAILED" in out and "firefox-68 1920 | candidate 1366" in out


def test_a_published_engine_that_cannot_be_fetched_is_a_broken_control(tmp_path, monkeypatch, capsys):
    def broken(tag, work):
        raise RuntimeError("the release index is down")

    monkeypatch.setattr(ab, "fetch_published", broken)
    monkeypatch.setattr(ab, "MIN_LEAVES", SMALL)
    rc = ab.run_ab("linux-x86_64", tmp_path / "out", "firefox-68", None, None, None, ab.select_visits(limit=1))
    assert rc == 2 and "FAIL RuntimeError: the release index is down" in capsys.readouterr().out


def test_the_a_a_floor_still_holds(tmp_path, monkeypatch, capsys):
    rc, _ = _aa_run(tmp_path, monkeypatch, [lambda full: {k: {"surface": {"x": 1}} for k in full}] * 2, limit=1)
    assert rc == 2 and "fewer than the 6 of the host gate's reference" in capsys.readouterr().out


def test_the_a_a_compares_again_from_the_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ab, "MIN_LEAVES", SMALL)
    asked = ab.select_visits(limit=2)
    full = {key: items(key[1]) for key in asked}
    ab.write_json(tmp_path / "dump-68.json", ab.pack("firefox-68", full))
    ab.write_json(tmp_path / ab.again_name("firefox-68"), ab.pack("firefox-68-again", full))
    assert ab.main(["compare", str(tmp_path), "--a-a", "--limit", "2"]) == 0
    ab.write_json(tmp_path / ab.again_name("firefox-68"), ab.pack("firefox-68-again", _changed(full)))
    assert ab.main(["compare", str(tmp_path), "--a-a", "--limit", "2"]) == 2


def test_the_limit_takes_the_first_seeds_and_the_command_line_needs_a_candidate_or_a_published_engine(tmp_path):
    assert ab.select_visits(limit=2) == ab.select_visits()[:2] and len(ab.select_visits(limit=2)) == 2
    for argv in (["run", "--leg", "l", "--out", str(tmp_path)],                       # nothing to run
                 ["run", "--leg", "l", "--out", str(tmp_path), "--published-seal", "s.json"],   # a seal without its binary
                 ["run", "cand", "--leg", "l", "--out", str(tmp_path), "--candidate-seal", "c.json", "--published-binary", "pub"]):  # a candidate and half a published engine
        with pytest.raises(SystemExit) as e:
            ab.main(argv)
        assert e.value.code == 2


def test_a_dump_without_a_seal_runs_under_the_seal_of_the_core(tmp_path, monkeypatch):
    seen = []

    class Done:
        returncode = 0

    monkeypatch.delenv("INVISIBLE_SEAL_FILE", raising=False)
    monkeypatch.setattr(ab.subprocess, "run", lambda cmd, env, timeout: seen.append(env) or Done())
    ab.run_dump("ff", None, "linux-x86_64", tmp_path / "o", [(1.0, 0)])
    assert "INVISIBLE_SEAL_FILE" not in seen[0]


# -
# How many seeds the A/A of each oracle visits on a pull request (scripts/ci_ab_pr_seeds.py, the step of e2e.yml, the e2e tests).

import ci_ab_pr_seeds as pr  # noqa: E402

ALL, DEFAULT = {"AB_ORACLE_PR_SEEDS": 5, "AB_NETWORK_PR_SEEDS": 5, "AB_HOST_PR_SEEDS": 5}, {"AB_ORACLE_PR_SEEDS": 2, "AB_NETWORK_PR_SEEDS": 1, "AB_HOST_PR_SEEDS": 2}


def test_a_pull_request_that_touches_no_oracle_runs_the_default_seeds():
    assert pr.pr_seed_counts(["src/invisible_playwright/launcher.py", "README.md", "tests/test_the_wire_is_json.py"], every=5) == DEFAULT
    assert pr.pr_seed_counts([], every=5) == DEFAULT


@pytest.mark.parametrize("path,only", [
    ("scripts/ci_host_gate.py", "AB_ORACLE_PR_SEEDS"), ("scripts/host_gate/exceptions.json", "AB_ORACLE_PR_SEEDS"), ("scripts/host_gate/page/index.html", "AB_ORACLE_PR_SEEDS"),
    ("tests/test_ab_oracle.py", "AB_ORACLE_PR_SEEDS"), ("tests/test_ab_oracle_e2e.py", "AB_ORACLE_PR_SEEDS"),
    ("scripts/ci_ab_network.py", "AB_NETWORK_PR_SEEDS"), ("ci/ab-network-volatile.json", "AB_NETWORK_PR_SEEDS"), ("tests/test_ab_network_e2e.py", "AB_NETWORK_PR_SEEDS"),
    ("scripts/ci_ab_host.py", "AB_HOST_PR_SEEDS"), ("ci/ab-host-expected.json", "AB_HOST_PR_SEEDS"), ("tests/test_ab_host.py", "AB_HOST_PR_SEEDS"),
    ("tests/_strace_stand_in.py", "AB_HOST_PR_SEEDS"),
])
def test_a_pull_request_that_touches_the_files_of_one_oracle_runs_every_seed_of_that_oracle_only(path, only):
    got = pr.pr_seed_counts(["README.md", path], every=5)
    assert got == dict(DEFAULT, **{only: 5}), got


@pytest.mark.parametrize("path", ["scripts/ci_ab_oracle.py", "scripts/persona_cover.py", "ci/persona-seeds.json", "scripts/ci_ab_pr_seeds.py"])
def test_what_the_oracles_share_runs_every_seed_of_all_of_them(path):
    assert pr.pr_seed_counts([path], every=5) == ALL


def test_every_seed_is_the_candidate_set_of_the_persona_seeds_file():
    assert pr.candidate_seed_count() == len(json.loads((Path(__file__).resolve().parents[1] / "ci" / "persona-seeds.json").read_text(encoding="utf-8"))["candidate"]) == 5
    assert pr.pr_seed_counts(["scripts/ci_ab_oracle.py"]) == ALL


def test_a_test_reads_the_number_from_the_environment_and_falls_back_to_its_default():
    assert pr.pr_seed_count("AB_ORACLE_PR_SEEDS", 2, {}) == 2
    assert pr.pr_seed_count("AB_ORACLE_PR_SEEDS", 2, {"AB_ORACLE_PR_SEEDS": ""}) == 2
    assert pr.pr_seed_count("AB_ORACLE_PR_SEEDS", 2, {"AB_ORACLE_PR_SEEDS": "5"}) == 5
    assert pr.pr_seed_count("AB_ORACLE_PR_SEEDS", 2, {"AB_ORACLE_PR_SEEDS": "5\r"}) == 5, "a carriage return from a Windows line is not part of the number"
    for bad in ("0", "-1", "five", "2.5"):
        with pytest.raises(ValueError, match="AB_ORACLE_PR_SEEDS"):
            pr.pr_seed_count("AB_ORACLE_PR_SEEDS", 2, {"AB_ORACLE_PR_SEEDS": bad})


def test_the_changed_files_are_the_difference_from_the_base_commit_and_the_base_is_fetched_first():
    calls = []

    class Done:
        stdout = "scripts/ci_ab_host.py\n\n tests/test_ab_host.py \n"

    def run(cmd, **kw):
        calls.append(cmd)
        return Done()

    assert pr.changed_files("abc123", run=run) == ["scripts/ci_ab_host.py", "tests/test_ab_host.py"]
    assert calls == [["git", "fetch", "--no-tags", "--depth=1", "origin", "abc123"], ["git", "diff", "--name-only", "abc123", "HEAD"]]


def test_the_script_prints_one_variable_per_oracle_with_unix_line_ends_for_the_environment_file(monkeypatch, capsys):
    monkeypatch.setattr(pr, "changed_files", lambda base: ["tests/test_ab_host_e2e.py"] if base == "base" else [])
    assert pr.main(["base"]) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["AB_ORACLE_PR_SEEDS=2", "AB_NETWORK_PR_SEEDS=1", "AB_HOST_PR_SEEDS=5"] and "\r" not in captured.out
    assert "host oracle: 5 seeds (its files changed: tests/test_ab_host_e2e.py)" in captured.err and "page oracle: 2 seeds (default 2)" in captured.err
    assert pr.main([]) == 2


def _job(text, name):
    """The text of one job of e2e.yml, from its header to the next one."""
    import re

    start = text.index("\n  %s:\n" % name)
    following = re.search(r"\n  [A-Za-z0-9_-]+:[ ]*\n", text[start + 1:])
    return text[start:start + 1 + following.start()] if following else text[start:]


def test_the_e2e_workflow_runs_the_step_on_a_pull_request_in_the_jobs_that_run_the_oracles_and_the_e2e_tests_read_its_variables():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github" / "workflows" / "e2e.yml").read_text(encoding="utf-8")
    legs = {name: _job(text, name) for name in ("e2e", "e2e-windows-control-a", "e2e-windows", "e2e-network", "e2e-network-windows", "e2e-host", "e2e-host-windows", "e2e-page", "e2e-page-windows")}
    step = ('      - name: How many seeds the A/A of each oracle visits (all of them when the pull request touches the oracle)\n'
            "        if: github.event_name == 'pull_request'\n        shell: bash\n        env:\n          BASE_SHA: ${{ github.event.pull_request.base.sha }}\n"
            '        run: python scripts/ci_ab_pr_seeds.py "$BASE_SHA" >> "$GITHUB_ENV"\n')
    assert [name for name, body in legs.items() if step in body] == ["e2e", "e2e-windows", "e2e-network", "e2e-network-windows", "e2e-host", "e2e-host-windows", "e2e-page", "e2e-page-windows"], "the jobs that run the oracles' tests, and only those"
    for name in ("e2e", "e2e-windows", "e2e-network", "e2e-network-windows", "e2e-host", "e2e-host-windows", "e2e-page", "e2e-page-windows"):
        assert legs[name].index("actions/setup-python") < legs[name].index(step) < legs[name].index("Fetch the published firefox binary")
    assert text.count("run: python scripts/ci_ab_pr_seeds.py") == 8 and "uses:" not in step
    for variable, source in (("AB_ORACLE_PR_SEEDS", "test_ab_oracle_e2e.py"), ("AB_NETWORK_PR_SEEDS", "test_ab_network_e2e.py"), ("AB_HOST_PR_SEEDS", "test_ab_host_e2e.py")):
        assert 'pr_seed_count("%s"' % variable in (root / "tests" / source).read_text(encoding="utf-8")
        assert variable in pr.ORACLES


def test_the_network_oracle_has_a_job_of_its_own_on_each_system_and_the_shared_jobs_leave_its_file_out():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github" / "workflows" / "e2e.yml").read_text(encoding="utf-8")
    network = "tests/test_ab_network_e2e.py"
    linux, windows = _job(text, "e2e-network"), _job(text, "e2e-network-windows")
    assert "runs-on: ubuntu-24.04" in linux and "runs-on: windows-2022" in windows
    for body in (linux, windows):
        assert body.count(network) == 1 and "scripts/run_e2e.py" not in body and "timeout-minutes: 40" in body and "needs: core-on-index" in body
        assert "      - name: Run the network oracle's A/A\n" in body and "skipped\": 0" in body, "the job refuses a skipped test: one test, passed"
    assert "xvfb-run -a" in linux
    shared_linux, shared_windows = _job(text, "e2e"), _job(text, "e2e-windows")
    assert "OWN_JOB_FILES: " + network in shared_linux and "$ROOT_FILES $SANDBOX_FILES $OWN_JOB_FILES" in shared_linux
    host_file = "tests/test_ab_host_e2e.py"
    page_file = "tests/test_ab_oracle_e2e.py"
    assert "OWN_JOB_FILES: %s %s %s" % (network, host_file, page_file) in shared_linux
    assert 'own_job = ["%s", "%s", "%s"]' % (network, host_file, page_file) in shared_windows and "f not in left_out and f not in own_job" in shared_windows
    assert network not in _job(text, "e2e-windows-control-a")
    # the budget of the test itself fits the job: 5 seeds, two engine runs, with its own setup
    import test_ab_network_e2e as network_e2e

    assert network_e2e.budget_s(5, 2) == 1800 and network_e2e.budget_s(5, 2) + 10 * 60 <= 40 * 60


def test_the_host_oracle_has_a_job_of_its_own_on_each_system_with_the_tools_it_traces_with():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github" / "workflows" / "e2e.yml").read_text(encoding="utf-8")
    linux, windows = _job(text, "e2e-host"), _job(text, "e2e-host-windows")
    assert "runs-on: ubuntu-24.04" in linux and "runs-on: windows-2022" in windows
    for body in (linux, windows):
        assert body.count("tests/test_ab_host_e2e.py") == 1 and "scripts/run_e2e.py" not in body and "timeout-minutes: 45" in body and "needs: core-on-index" in body
        assert "      - name: Run the host oracle's A/A\n" in body and "skipped\": 0" in body
    assert "apt-get install -y strace" in linux and "xvfb-run -a" in linux, "Linux traces the tree with strace"
    import test_ab_host_e2e as host_e2e

    assert host_e2e.TIMEOUT_S + 10 * 60 <= 45 * 60, "the test's own limit fits the job"


def test_the_page_oracle_has_a_job_of_its_own_on_each_system_and_the_shared_jobs_leave_its_file_out():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github" / "workflows" / "e2e.yml").read_text(encoding="utf-8")
    page = "tests/test_ab_oracle_e2e.py"
    code = lambda job: "\n".join(line for line in job.splitlines() if not line.lstrip().startswith("#"))  # the comment that follows the last job is the next one's
    linux, windows = code(_job(text, "e2e-page")), code(_job(text, "e2e-page-windows"))
    assert "name: e2e (page oracle, linux)" in linux and "name: e2e (page oracle, windows)" in windows
    assert "runs-on: ubuntu-24.04" in linux and "runs-on: windows-2022" in windows
    for body in (linux, windows):
        assert body.count(page) == 1 and "scripts/run_e2e.py" not in body and "timeout-minutes: 45" in body and "needs: core-on-index" in body
        assert "AB_ORACLE_SEED_SET" not in body, "the A/A of a pull request, not a shard of the final seeds (those have their own jobs)"
        assert "      - name: Run the page oracle's A/A\n" in body and "skipped\": 0" in body, "the job refuses a skipped test: one test, passed"
    assert "xvfb-run -a" in linux
    shared_linux, shared_windows = _job(text, "e2e"), _job(text, "e2e-windows")
    assert page in shared_linux.split("OWN_JOB_FILES: ")[1].splitlines()[0] and '"%s"]  # ' % page in shared_windows and "f not in left_out and f not in own_job" in shared_windows
    assert page not in _job(text, "e2e-windows-control-a")
    import test_ab_oracle_e2e as page_e2e

    # the A/A of a pull request that touches the oracle: 5 seeds, two engine runs, the second launch of seed 0; the guard of the test plus its margin fits the job with the setup
    assert page_e2e.budget_s(5, 2, True) + 120 + 12 * 60 <= 45 * 60, "the test's own limit fits the job"


# --- the second launch of seed 0 on a reused profile (H7) -----------------------------------------------------------------------------------------

SEED0 = (1.0, ab.RELAUNCH_SEED)


def zero_visits():
    """Two visits: seed 0, which has the second launch's leaves, and seed 2, which has none."""
    return {SEED0: items(0), (1.5, 2): items(2)}


def zero_run(a, b, noise=(), floor=SMALL):
    return ab.compare_visits(a, b, list(noise), floor, expected=tuple(sorted(a)))


def test_only_seed_0_is_launched_twice_and_the_visit_of_it_is_the_one_that_has_the_second_launch():
    assert ab.RELAUNCH_SEED == 0 and ab.relaunch_visits([(1.5, 2), (1.0, 0), (1.25, 6)]) == ((1.0, 0),)
    assert ab.relaunch_visits([(1.5, 2)]) == ()
    both = items(0)
    assert sorted(k for k in both if k.startswith(gate.RELAUNCH)) == ["relaunch.blend", "relaunch.surface", "relaunch.text"], "the diag. item of the second launch is not kept"
    assert not [k for k in items(2) if k.startswith(gate.RELAUNCH)]


def test_the_second_launch_of_two_equal_dumps_is_green_and_is_counted_apart_from_the_visit():
    report = zero_run(zero_visits(), zero_visits())
    assert report["verdict"] == "green" and report["problems"] == []
    assert report["compared"] == {"dpr1.0_seed0": 6, "dpr1.5_seed2": 6}, "the visit's own leaves only: the second launch's do not fill the floor"
    assert report["relaunch"] == {"dpr1.0_seed0": 6}


def test_a_leaf_that_differs_in_the_second_launch_only_is_red_and_carries_the_prefix_and_the_seed():
    b = zero_visits()
    b[SEED0]["relaunch.surface"]["navigator"]["language"] = "it-IT"
    report = zero_run(zero_visits(), b)
    assert report["verdict"] == "red" and len(report["differences"]) == 1
    d = report["differences"][0]
    assert (d["seed"], d["dpr"], d["leaf"], d["a"], d["b"]) == (0, 1.0, "relaunch.surface.navigator.language", "en-US", "it-IT")


def test_a_second_launch_that_is_missing_on_one_side_is_red_and_says_which():
    b = zero_visits()
    for k in [k for k in b[SEED0] if k.startswith(gate.RELAUNCH)]:
        del b[SEED0][k]
    report = zero_run(zero_visits(), b)
    assert report["verdict"] == "red"
    assert any(p.startswith("side B: the second launch of dpr1.0_seed0 on a reused profile has 0 leaves") for p in report["problems"]), report["problems"]
    assert not any(p.startswith("side A:") for p in report["problems"])


def test_a_second_launch_that_both_sides_lack_is_red_not_green():
    both = zero_visits()
    for k in [k for k in both[SEED0] if k.startswith(gate.RELAUNCH)]:
        del both[SEED0][k]
    report = zero_run(both, copy.deepcopy(both))
    assert report["verdict"] == "red" and report["differences"] == []
    assert len([p for p in report["problems"] if "second launch" in p]) == 2


def test_a_second_launch_that_dumped_less_than_the_floor_is_red_even_when_the_visit_is_full():
    both = zero_visits()
    del both[SEED0]["relaunch.text"]
    report = zero_run(both, copy.deepcopy(both))
    assert report["verdict"] == "red" and report["compared"]["dpr1.0_seed0"] == 6
    assert all("has 5 leaves under relaunch." in p and "fewer than the 6" in p for p in report["problems"]) and len(report["problems"]) == 2


def test_the_second_launch_does_not_make_up_for_a_visit_that_dumped_less():
    both = zero_visits()
    del both[SEED0]["text"]
    report = zero_run(both, copy.deepcopy(both))
    assert report["verdict"] == "red" and report["compared"]["dpr1.0_seed0"] == 5
    assert report["problems"] == ["dpr1.0_seed0 compared 5 leaves, fewer than the 6 of the host gate's reference: the page dumped less than it should"]


def test_a_second_launch_in_a_visit_that_was_not_asked_to_have_one_is_red():
    b = zero_visits()
    b[(1.5, 2)].update(copy.deepcopy(gate.relaunch_items(items(2))))
    report = zero_run(zero_visits(), b)
    assert report["verdict"] == "red" and "side B has the second launch of dpr1.5_seed2 on a reused profile, which was not asked for" in report["problems"]


def test_a_noise_entry_names_a_leaf_of_the_second_launch_by_its_bare_name_and_diag_under_the_prefix_is_not_held():
    b = zero_visits()
    b[SEED0]["relaunch.surface"]["navigator"]["language"] = "it-IT"
    b[SEED0]["relaunch.diag.webgl"] = {"message": "other"}
    a = zero_visits()
    a[SEED0]["relaunch.diag.webgl"] = {"message": "llvmpipe"}
    noise = [{"keys": ["surface.navigator.language"], "why": "x"}]
    report = zero_run(a, b, noise, floor={dpr: 5 for dpr in gate.DPRS})
    assert report["verdict"] == "green" and report["differences"] == [] and report["relaunch"] == {"dpr1.0_seed0": 5}
    assert zero_run(a, b, [], floor={dpr: 5 for dpr in gate.DPRS})["verdict"] == "red", "without the entry it is a difference"


def test_a_candidate_set_visit_list_with_seed_0_expects_the_second_launch_of_it_from_judge_run_and_the_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ab, "MIN_LEAVES", SMALL)
    asked = ab.select_visits(limit=2)
    assert [seed for _, seed in asked][0] == 0
    full = {key: items(key[1]) for key in asked}
    assert ab.judge(full, copy.deepcopy(full), [], expected=tuple(asked))["relaunch"] == {ab.visit_name(*asked[0]): 6}
    lacking = copy.deepcopy(full)
    for k in [k for k in lacking[asked[0]] if k.startswith(gate.RELAUNCH)]:
        del lacking[asked[0]][k]
    assert ab.judge(full, lacking, [], expected=tuple(asked))["verdict"] == "red"
    ab.write_json(tmp_path / "dump-68.json", ab.pack("firefox-68", full))
    ab.write_json(tmp_path / "dump-candidate.json", ab.pack("candidate", lacking))
    assert ab.main(["compare", str(tmp_path), "--limit", "2"]) == 1
    assert "second launch of dpr" in capsys.readouterr().out


def test_the_dump_is_asked_for_the_second_launch_only_when_seed_0_is_among_the_visits(tmp_path, monkeypatch):
    calls = []

    class Done:
        returncode = 0

    monkeypatch.setattr(ab.subprocess, "run", lambda cmd, env, timeout: calls.append(cmd) or Done())
    ab.run_dump("ff", tmp_path / "s.json", "linux-x86_64", tmp_path / "o", [(1.5, 2), (1.0, 0)])
    ab.run_dump("ff", tmp_path / "s.json", "linux-x86_64", tmp_path / "o", [(1.5, 2), (1.25, 6)])
    ab.run_dump("ff", tmp_path / "s.json", "linux-x86_64", tmp_path / "o")
    with_zero, without, grid = calls
    assert with_zero[with_zero.index("--relaunch-seed") + 1] == "0" and with_zero.count("--relaunch-seed") == 1
    assert "--relaunch-seed" not in without and "--relaunch-seed" not in grid, "the host gate's own grid and the other shards do not launch twice"


# the dump itself, with the browser replaced: which launches it makes, on which folder, and what it keeps

def _fake_read(log, answers=None):
    """A read_page that records the options of every launch and answers items that say which launch they came from."""
    def read(options, port, dpr, timeout_ms):
        n = len(log)
        log.append(dict(options))
        answer = (answers or {}).get(n)
        if answer is not None:
            return answer
        return {"format": gate.FORMAT, "items": {"surface": {"launch": n}, "diag.webgl": {"launch": n}}}
    return read


def _dump(tmp_path, monkeypatch, visits_asked, relaunch, log, answers=None):
    monkeypatch.setattr(gate, "read_page", _fake_read(log, answers))
    monkeypatch.setattr(gate, "reference_zone", lambda reference: "UTC")
    return gate.dump("ff", "leg", tmp_path / "dump", tmp_path / "reference", visits=visits_asked, pin_dpr=False, relaunch_seeds=relaunch)


def test_the_dump_launches_seed_0_twice_on_one_profile_and_keeps_the_second_launch_under_its_own_prefix(tmp_path, monkeypatch, capsys):
    log = []
    assert _dump(tmp_path, monkeypatch, [(1.0, 0), (1.5, 2)], (0,), log) == 0
    assert len(log) == 4
    fresh, first, second, other = log
    assert "profile_dir" not in fresh and "profile_dir" not in other, "the visit itself, and every other seed, are on a fresh profile as they always were"
    assert first["profile_dir"] == second["profile_dir"] and first["seed"] == second["seed"] == 0 and first["timezone"] == second["timezone"] == "UTC"
    assert not Path(first["profile_dir"]).exists(), "the profile folder is removed when the second launch is done"
    zero = json.loads((tmp_path / "dump" / "leg_dpr1.0_seed0.json").read_text(encoding="utf-8"))["result"]["items"]
    assert zero["surface"] == {"launch": 0}, "the visit's own items are those of the fresh profile"
    assert zero["relaunch.surface"] == {"launch": 2}, "the second launch's, not the first's"
    assert "relaunch.diag.webgl" not in zero and zero["diag.webgl"] == {"launch": 0}
    two = json.loads((tmp_path / "dump" / "leg_dpr1.5_seed2.json").read_text(encoding="utf-8"))["result"]["items"]
    assert not [k for k in two if k.startswith(gate.RELAUNCH)]
    assert "second launch of seed 0 on a reused profile" in capsys.readouterr().out


def test_the_dump_without_a_seed_to_launch_twice_launches_each_visit_once_as_the_host_gate_always_did(tmp_path, monkeypatch):
    log = []
    assert _dump(tmp_path, monkeypatch, [(1.0, 0), (1.5, 2)], (), log) == 0
    assert len(log) == 2 and all("profile_dir" not in o for o in log)
    assert gate.visit_options("ff", 6, 1.25, "UTC") == {"seed": 6, "binary_path": "ff", "headless": True, "show_cursor": False, "locale": "en-US", "timezone": "UTC",
                                                       "pin": {"screen.dpr": 1.25}}
    assert gate.visit_options("ff", 6, None, "UTC", "p")["profile_dir"] == "p"


@pytest.mark.parametrize("bad", [1, 2])
def test_a_launch_of_the_pair_that_does_not_answer_the_format_is_a_failed_dump_and_the_folder_is_removed(tmp_path, monkeypatch, capsys, bad):
    log = []  # launch 0 is the visit on a fresh profile, 1 the first launch of the pair, 2 the second
    rc = _dump(tmp_path, monkeypatch, [(1.0, 0)], (0,), log, answers={bad: {"format": "old", "items": {}}})
    assert rc == 2 and len(log) == bad + 1
    assert "format 'old'" in capsys.readouterr().out
    assert not Path(log[-1]["profile_dir"]).exists()
    assert not (tmp_path / "dump" / "leg_dpr1.0_seed0.json").exists(), "a visit with a broken second launch is not written as if it were whole"


def test_the_profile_folder_is_removed_when_a_launch_of_the_pair_raises(tmp_path):
    seen = []

    def read(options, port, dpr, timeout_ms):
        seen.append(options["profile_dir"])
        if len(seen) == 2:
            raise RuntimeError("the second launch hung")
        return {"format": gate.FORMAT, "items": {}}

    with pytest.raises(RuntimeError, match="hung"):
        gate.second_launch({"seed": 0}, 1, 1.0, 1000, read=read)
    assert len(seen) == 2 and seen[0] == seen[1] and not Path(seen[0]).exists()


def test_a_launch_on_a_profile_opens_its_page_in_the_persistent_context_and_a_launch_without_one_in_a_new_context(monkeypatch):
    """With a profile_dir the wrapper hands back a BrowserContext, which has no new_context(): asking it for one fails, so the persistent launch must open the page in it."""
    import types

    class Page:
        mouse = types.SimpleNamespace(move=lambda *a: None, click=lambda *a: None)

        def goto(self, url, timeout):
            self.url = url

        def wait_for_function(self, expression, timeout):
            pass

        def evaluate(self, expression):
            return {"format": gate.FORMAT, "items": {"surface": {}}}

    class Context:
        def new_page(self):
            return Page()

    class Browser:
        def new_context(self):
            return Context()

    class Session:
        def __init__(self, **options):
            self.options = options

        def __enter__(self):
            return Context() if "profile_dir" in self.options else Browser()

        def __exit__(self, *exc):
            return False

    monkeypatch.setitem(sys.modules, "invisible_playwright", types.SimpleNamespace(InvisiblePlaywright=Session))
    monkeypatch.setattr(gate, "drive_states", lambda *a: {})
    for options in ({"seed": 0}, {"seed": 0, "profile_dir": "p"}):
        assert gate.read_page(options, 1234, 1.0, 1000)["format"] == gate.FORMAT


# --- the final set in shards (H4): the test's guard, the e2e.yml jobs, the touched step ---------------------------------------------------------------

def _e2e_module():
    import test_ab_oracle_e2e as e2e

    return e2e


def test_the_hang_guard_grows_with_the_seeds_the_engine_runs_and_the_second_launch():
    e2e = _e2e_module()
    base = e2e.budget_s(5, 2)
    assert base == e2e.SETUP_S + 2 * (e2e.FIRST_SEED_S + 4 * e2e.NEXT_SEED_S)
    assert e2e.budget_s(2, 2) < e2e.budget_s(3, 2) < base < e2e.budget_s(5, 3) and e2e.budget_s(5, 2, True) == base + 2 * e2e.RELAUNCH_S
    assert e2e.budget_s(5, 3, True) < 50 * 60, "the gate's leg has 50 minutes: a guard past the job's own timeout guards nothing"
    assert e2e.budget_s(5, 2, True) + 10 * 60 <= 40 * 60, "a shard's A/A fits the 40 minutes of its job with its setup"


def test_the_plan_reads_the_shard_the_set_and_whether_there_is_a_candidate_from_the_environment(tmp_path):
    e2e = _e2e_module()
    assert e2e.plan({}) == (2, 2, True), "a pull request that touches nothing: the first two seeds of the candidate set, seed 0 among them"
    assert e2e.plan({"AB_ORACLE_PR_SEEDS": "5"}) == (5, 2, True) and e2e.plan({"AB_ORACLE_PR_SEEDS": "99"}) == (5, 2, True)
    seal = tmp_path / "seal.json"
    assert e2e.plan({"INVISIBLE_SEAL_FILE": str(seal), "AB_ORACLE_PR_SEEDS": "1"}) == (5, 3, True), "with a candidate every seed, whatever the limit"
    sizes = [e2e.plan({"AB_ORACLE_SEED_SET": "final", "AB_ORACLE_SHARD": "%d/4" % i}) for i in (1, 2, 3, 4)]
    assert sizes == [(5, 2, True), (5, 2, False), (5, 2, False), (5, 2, False)], "20 seeds in four shards of five; seed 0 is in the first"
    assert e2e.plan({"AB_ORACLE_SEED_SET": "final", "AB_ORACLE_SHARD": "1/4", "AB_ORACLE_PR_SEEDS": "2"})[0] == 5, "the final set's A/A has no limit"
    assert e2e.plan({"AB_ORACLE_SEED_SET": "final"})[0] == 20 and e2e.plan({"AB_ORACLE_SEED_SET": "final", "INVISIBLE_SEAL_FILE": "x"}) == (20, 3, True)
    with pytest.raises(SystemExit):
        e2e.plan({"AB_ORACLE_SEED_SET": "everything"})
    with pytest.raises(SystemExit):
        e2e.plan({"AB_ORACLE_SEED_SET": "final", "AB_ORACLE_SHARD": "5/4"})


def test_the_hang_guard_of_the_test_is_the_budget_of_this_run_and_the_subprocess_ends_before_the_worker_is_killed():
    e2e = _e2e_module()
    assert e2e.BUDGET_S == e2e.budget_s(*e2e.plan()) and e2e.TIMEOUT_S == e2e.BUDGET_S
    mark = next(m for m in e2e.test_the_published_engine_repeats_itself_and_the_candidate_gives_the_same_bytes.pytestmark if m.name == "timeout")
    assert mark.args == (e2e.BUDGET_S + 120,) and mark.kwargs == {"method": "thread"}


def test_the_files_of_the_final_shards_are_the_files_of_the_page_oracle_and_nothing_else_is():
    assert ab.touched(["README.md", "scripts/ci_ab_oracle.py"]) and ab.touched(["tests/test_ab_oracle_e2e.py"]) and ab.touched(["scripts/host_gate/page/index.html"])
    assert not ab.touched(["README.md", "src/invisible_playwright/launcher.py", "scripts/ci_ab_network.py", "tests/test_ab_host.py"]) and not ab.touched([])


def test_the_touched_step_prints_one_line_with_a_unix_end_for_the_output_file(monkeypatch, capsys):
    monkeypatch.setattr(pr, "changed_files", lambda base: ["tests/test_ab_oracle_e2e.py"] if base == "base1" else ["README.md"])
    assert ab.main(["touched", "base1"]) == 0
    out = capsys.readouterr().out
    assert out == "touched=true\n" and "\r" not in out
    assert ab.main(["touched", "base2"]) == 0 and capsys.readouterr().out == "touched=false\n"


def test_the_final_set_has_jobs_of_its_own_in_four_shards_on_each_system_and_the_shared_jobs_do_not_grow():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github" / "workflows" / "e2e.yml").read_text(encoding="utf-8")
    page = "tests/test_ab_oracle_e2e.py"
    linux, windows, touched = _job(text, "e2e-page-final"), _job(text, "e2e-page-final-windows"), _job(text, "e2e-page-final-touched")
    assert "runs-on: ubuntu-24.04" in linux and "runs-on: windows-2022" in windows and "xvfb-run -a" in linux
    for body in (linux, windows):
        assert "shard: [1, 2, 3, 4]" in body and "AB_ORACLE_SEED_SET: final" in body and "AB_ORACLE_SHARD: ${{ matrix.shard }}/4" in body
        assert body.count(page) == 1 and "scripts/run_e2e.py" not in body and "timeout-minutes: 40" in body and "fail-fast: false" in body
        assert "needs: [core-on-index, e2e-page-final-touched]" in body and "needs.e2e-page-final-touched.outputs.touched == 'true'" in body
        assert "skipped\": 0" in body and '{"tests": 1, "failures": 0, "errors": 0, "skipped": 0}' in body, "one test, passed: a skipped one is red"
        assert "matrix.shard" in body.split("name: invpw-evidence-page-final", 1)[1].split("path:", 1)[0], "four shards, four artifact names"
    assert "scripts/ci_ab_oracle.py touched" in touched and "pull_request" in touched and "outputs:" in touched
    shared_linux, shared_windows = _job(text, "e2e"), _job(text, "e2e-windows")
    assert "AB_ORACLE_SEED_SET" not in shared_linux + shared_windows + _job(text, "e2e-windows-control-a"), "the shared jobs run the candidate set and do not grow"


def test_the_final_set_is_four_shards_of_five_that_reach_every_seed_once_and_seed_0_is_in_the_first():
    parts = [ab.select_visits("final", "%d/4" % i) for i in (1, 2, 3, 4)]
    assert [len(p) for p in parts] == [5, 5, 5, 5] and [seed for _, seed in parts[0]][0] == 0
    assert [i + 1 for i, p in enumerate(parts) if ab.relaunch_visits(p)] == [1]
