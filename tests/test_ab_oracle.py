"""The A/B oracle's comparator (scripts/ci_ab_oracle.py), on synthetic dumps, no browser.

The oracle asks one question of two engines: same seed, same bytes. Each property is shown on the input it exists for: two dumps that differ in one leaf are red
(with the leaf, the two values and the seed), equal ones are green, a difference in a `noise` leaf or under diag. is the only thing excused, a visit that
compares fewer leaves than the host gate's own reference is red (a page that dumped less passes anything), and a visit missing on one side is red.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ci_ab_oracle as ab  # noqa: E402
import ci_crash_evidence as crash  # noqa: E402
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
    write_exits(0)


def write_exits(code, launches=1):
    """What the probe of the dump process writes where the oracle told it (`AB_CRASH_DIR`): how each launch's parent ended."""
    where = os.environ.get(crash.CRASH_DIR_ENV)
    if where:
        Path(where).mkdir(parents=True, exist_ok=True)
        (Path(where) / "exits.json").write_text(json.dumps({"format": crash.FORMAT, "launches": [
            {"n": n + 1, "label": {"seed": 0, "dpr": 1.0, "launch": "visit"}, "pid": 100 + n, "returncode": code} for n in range(launches)]}), encoding="utf-8")


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
    assert sorted(p.name for p in (tmp_path / "out").iterdir() if p.is_file()) == ["diff.json", "dump-68.json", "dump-candidate.json"]
    assert sorted(p.name for p in (tmp_path / "out" / "crash").iterdir()) == ["candidate", "firefox-68"], "how each engine's browsers ended is in the artifact, a folder per run"
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
    assert sorted(p.name for p in (tmp_path / "out").iterdir() if p.is_file()) == ["diff-a-a.json", "dump-68-again.json", "dump-68.json"]
    assert sorted(p.name for p in (tmp_path / "out" / "crash").iterdir()) == ["firefox-68", "firefox-68-again"]
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
    assert sorted(p.name for p in (tmp_path / "out").iterdir() if p.is_file()) == ["diff-a-a.json", "diff.json", "dump-68-again.json", "dump-68.json", "dump-candidate.json"]
    assert sorted(p.name for p in (tmp_path / "out" / "crash").iterdir()) == ["candidate", "firefox-68", "firefox-68-again"]


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


@pytest.mark.parametrize("path", ["scripts/ci_ab_oracle.py", "scripts/ci_crash_evidence.py", "scripts/persona_cover.py", "ci/persona-seeds.json", "scripts/ci_ab_pr_seeds.py"])
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
    # The Windows parts take their files from the rows wrapper.e2e.suite.windows.N of ci/required-suites.json (one partition with the candidate's
    # gate); the oracles' files are in none of those rows (tests/test_the_e2e_suite_is_accounted_for.py names them as excluded: their own jobs).
    assert 'wrapper.e2e.suite.windows.${{ matrix.part }}' in shared_windows
    import json

    rows = json.loads((root / "ci" / "required-suites.json").read_text(encoding="utf-8"))
    suite_entries = [t for r in rows if r["id"].startswith("wrapper.e2e.suite.") for t in r["tests"]]
    assert not [t for t in suite_entries if t.split("::")[0] in (network, host_file, page_file, "tests/test_ab_host_crash_linux_e2e.py")]
    assert network not in _job(text, "e2e-windows-control-a")
    # the budget of the test itself fits the job: 5 seeds, two engine runs, with its own setup
    import test_ab_network_e2e as network_e2e

    assert network_e2e.budget_s(5, 2) == 1800 and network_e2e.budget_s(5, 2) + 10 * 60 <= 40 * 60


def test_the_host_oracle_has_a_job_of_its_own_on_each_system_with_the_tools_it_traces_with():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github" / "workflows" / "e2e.yml").read_text(encoding="utf-8")
    linux, windows = _job(text, "e2e-host"), _job(text, "e2e-host-windows")
    assert "runs-on: ubuntu-24.04" in linux and "runs-on: windows-2022" in windows
    for body, minutes in ((linux, 65), (windows, 45)):
        assert body.count("tests/test_ab_host_e2e.py") == 1 and "scripts/run_e2e.py" not in body and "timeout-minutes: %d" % minutes in body and "needs: core-on-index" in body
        assert "      - name: Run the host oracle's A/A\n" in body and "skipped\": 0" in body
    assert "apt-get install -y strace" in linux and "xvfb-run -a" in linux, "Linux traces the tree with strace"
    import test_ab_host_e2e as host_e2e
    import test_ab_host_crash_linux_e2e as crash_e2e

    assert host_e2e.TIMEOUT_S + crash_e2e.TIMEOUT_S + 10 * 60 <= 65 * 60, "the A/A and the recorder's own test, one after the other, fit the Linux job"
    assert host_e2e.TIMEOUT_S + 10 * 60 <= 45 * 60, "the test's own limit fits the Windows job"


def test_the_linux_jobs_of_the_three_oracles_install_gdb_and_elfutils_and_point_the_kernel_s_core_pattern_at_a_folder_of_the_job():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github" / "workflows" / "e2e.yml").read_text(encoding="utf-8")
    for name in ("e2e-network", "e2e-host", "e2e-page", "e2e-page-final"):
        body = _job(text, name)
        step = body[body.index("      - name: Cores of a browser that did not end with 0"):]
        step = step[:step.index("      - name: The security state")]
        assert "apt-get install -y gdb elfutils" in step and 'sysctl -w kernel.core_pattern="$RUNNER_TEMP/cores/core.%e.%p.%t"' in step, name
        assert 'INVPW_CORE_DIR=$RUNNER_TEMP/cores' in step, name
        assert body.index("System deps (xvfb") < body.index("Cores of a browser") < body.index("Fetch the published firefox binary"), "after the apt update, before any launch"
        assert "MOZ_CRASHREPORTER" not in body and "extra_prefs" not in step, "no environment variable of the browser, no pref, no crash reporter option"
    for name in ("e2e-network-windows", "e2e-host-windows", "e2e-page-windows", "e2e-page-final-windows"):
        assert "Cores of a browser" not in _job(text, name), "Windows reads the `.extra` and the minidump names: no core, no gdb"


def test_the_recorders_own_test_runs_in_the_host_job_after_the_a_a_and_is_left_out_of_the_shared_job_and_has_its_own_linux_row():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github" / "workflows" / "e2e.yml").read_text(encoding="utf-8")
    crash_file = "tests/test_ab_host_crash_linux_e2e.py"
    host_job = _job(text, "e2e-host")
    assert host_job.index("Run the host oracle's A/A") < host_job.index("The host oracle's recorder of a browser that dies") < host_job.index("The leg ran with the pinned GeoIP database")
    step = host_job[host_job.index("      - name: The host oracle's recorder"):host_job.index("      - name: The leg ran with the pinned GeoIP database")]
    assert step.count(crash_file) == 1 and "if: success() || failure()" in step and "xvfb-run -a" in step and '{"tests": 1, "failures": 0, "errors": 0, "skipped": 0}' in step
    assert "host-crash-e2e.xml" in host_job.split("Upload what the test saved")[1]
    shared = _job(text, "e2e")
    assert crash_file not in _job(text, "e2e-host-windows") and crash_file in shared.split("OWN_JOB_FILES: ")[1].splitlines()[0]
    rows = {r["id"]: r for r in json.loads((root / "ci" / "required-suites.json").read_text(encoding="utf-8"))}
    row = rows["wrapper.e2e.ab_host_crash"]
    assert row["os"] == ["linux"] and row["linux_only"].strip() and row["tests"] == [crash_file] and row["min_tests"] == 1 and row["exact"] is True and row["marker"] == "e2e"
    assert rows["wrapper.e2e.ab_host"]["min_tests"] == 1 and rows["wrapper.e2e.ab_host"]["tests"] == ["tests/test_ab_host_e2e.py"], "the A/A is still the one test of its row"


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
    assert page in shared_linux.split("OWN_JOB_FILES: ")[1].splitlines()[0] and 'wrapper.e2e.suite.windows.${{ matrix.part }}' in shared_windows
    import json

    rows = json.loads((root / "ci" / "required-suites.json").read_text(encoding="utf-8"))
    assert not [t for r in rows if r["id"].startswith("wrapper.e2e.suite.") for t in r["tests"] if t.split("::")[0] == page], "the page oracle's file is in no row of the shared suite"
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


# --- a launch whose parent did not end with 0 is red in the page oracle (scripts/ci_crash_evidence.py) --------------------------------------------------

def a_launch_through_the_core(code, folder=None):
    """A launch as the core makes it: a `Connection` over two pipes whose process ended with `code`, closed as the wrapper closes it."""
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


def test_a_dump_with_a_launch_that_died_is_red_and_names_the_side_the_seed_the_launch_and_the_code(tmp_path, monkeypatch, capsys):
    calls = []

    def fake_dump(binary, seal, leg, out, visits_asked):
        write_gate_dump(out, {key: items(key[1]) for key in visits_asked})
        if len(calls) == 1:  # the second dump of the published engine: its launch of seed 12 died
            where = Path(os.environ[crash.CRASH_DIR_ENV])
            (where / "exits.json").write_text(json.dumps({"format": crash.FORMAT, "launches": [
                {"n": 1, "label": {"seed": 11, "dpr": 1.0, "launch": "visit"}, "returncode": 0},
                {"n": 2, "label": {"seed": 12, "dpr": 1.5, "launch": "visit"}, "returncode": -11, "evidence": "crash/firefox-68-again/02-12-1.5-visit"}]}), encoding="utf-8")
        calls.append(binary)
        return 0

    monkeypatch.setattr(ab, "run_dump", fake_dump)
    monkeypatch.setattr(ab, "MIN_LEAVES", SMALL)
    rc = ab.run_ab("linux-x86_64", tmp_path / "out", "firefox-68", ("pub", None), None, None, ab.select_visits(limit=2))
    out = capsys.readouterr().out
    assert rc == 2 and "BROKEN CONTROL" in out
    line = [x for x in out.splitlines() if x.startswith("FAIL the parent process exited with code")]
    assert len(line) == 1
    for part in ("code -11 (SIGSEGV)", "run firefox-68-again", "side B", "seed 12", "dpr 1.5", "launch visit", "(evidence: crash/firefox-68-again/02-12-1.5-visit)"):
        assert part in line[0], part
    diff = json.loads((tmp_path / "out" / "diff-a-a.json").read_text(encoding="utf-8"))
    assert diff["verdict"] == "red" and diff["differences"] == [], "the dumps are equal: it is the launch that is red"


def test_a_dump_whose_probe_wrote_nothing_is_red_and_an_a_b_with_a_candidate_that_died_is_red_not_a_broken_control(tmp_path, monkeypatch, capsys):
    calls = []

    def fake_dump(binary, seal, leg, out, visits_asked):
        write_gate_dump(out, {key: items(key[1]) for key in visits_asked})
        where = Path(os.environ[crash.CRASH_DIR_ENV])
        if len(calls) == 0:
            (where / "exits.json").unlink()
        calls.append(binary)
        return 0

    monkeypatch.setattr(ab, "run_dump", fake_dump)
    monkeypatch.setattr(ab, "MIN_LEAVES", SMALL)
    rc = ab.run_ab("linux-x86_64", tmp_path / "out", "firefox-68", ("pub", None), "cand", tmp_path / "s.json", ab.select_visits(limit=2))
    out = capsys.readouterr().out
    assert rc == 2 and "left no record of how its browsers ended" in out and "the probe did not see them" in out

    def fake_again(binary, seal, leg, out, visits_asked):
        write_gate_dump(out, {key: items(key[1]) for key in visits_asked})
        if binary == "cand":
            write_exits(-6)
        return 0

    monkeypatch.setattr(ab, "run_dump", fake_again)
    rc = ab.run_ab("linux-x86_64", tmp_path / "out2", "firefox-68", ("pub", None), "cand", tmp_path / "s.json", ab.select_visits(limit=2))
    out = capsys.readouterr().out
    assert rc == 1 and "BROKEN CONTROL" not in out and "code -6 (SIGABRT): run candidate, side B" in out
    assert json.loads((tmp_path / "out2" / "diff.json").read_text(encoding="utf-8"))["verdict"] == "red"


def test_the_published_engine_dump_of_a_run_that_died_is_red_in_the_old_run_too(tmp_path, monkeypatch, capsys):
    def fake_dump(binary, seal, leg, out, visits_asked):
        write_gate_dump(out, {key: items(key[1]) for key in visits_asked})
        write_exits(-11 if binary == "cand" else 0, launches=2)
        return 0

    monkeypatch.setattr(ab, "run_dump", fake_dump)
    monkeypatch.setattr(ab, "MIN_LEAVES", SMALL)
    rc = ab.run("cand", tmp_path / "cand.seal.json", "linux-x86_64", tmp_path / "out", "firefox-68", published=("pub", tmp_path / "pub.seal.json"), visits=list(VISITS))
    out = capsys.readouterr().out
    assert rc == 1 and out.count("FAIL the parent process exited with code -11") == 2 and "run candidate, side B" in out


def test_the_dump_process_names_each_launch_and_writes_how_it_ended_where_the_oracle_said(tmp_path, monkeypatch, capsys):
    """The dump of the page, with the browser replaced by launches through the core's own `Connection` (no browser): the probe sees each one, the labels come from the dump
    (the visit, and the two launches of the pair on one profile), and `exits.json` is there whatever the dump says of the page."""
    codes = iter([0, 0, -11])

    def read(options, port, dpr, timeout_ms):
        a_launch_through_the_core(next(codes))
        return {"format": gate.FORMAT, "items": {"surface": {"x": 1}}}

    monkeypatch.setattr(gate, "read_page", read)
    monkeypatch.setattr(gate, "reference_zone", lambda reference: "UTC")
    monkeypatch.setenv(crash.CRASH_DIR_ENV, str(tmp_path / "crash"))
    try:
        assert gate.dump("/e/firefox", "linux-x86_64", tmp_path / "dump", tmp_path / "reference", visits=[(1.0, 0)], pin_dpr=False, relaunch_seeds=(0,)) == 0
    finally:
        crash.stop_probe()
    exits = crash.read_exits(tmp_path / "crash" / "exits.json")
    assert [(x["label"]["launch"], x["returncode"]) for x in exits["launches"]] == [("visit", 0), ("relaunch-first", 0), ("relaunch-second", -11)]
    assert [x["label"]["seed"] for x in exits["launches"]] == [0, 0, 0]
    assert "EXIT the parent process exited with code -11 (SIGSEGV): run this dump, side A, seed 0, dpr 1.0, launch relaunch-second" in capsys.readouterr().out
    assert (tmp_path / "crash" / "03-0-1.0-relaunch-second" / "crash.json").is_file() and not (tmp_path / "crash" / "01-0-1.0-visit").exists()


def test_a_dump_that_was_not_given_a_folder_installs_no_probe_and_launches_as_the_host_gate_always_did(tmp_path, monkeypatch):
    from invisible_core.juggler.connection import Connection

    before = (Connection.__init__, Connection.close)
    monkeypatch.delenv(crash.CRASH_DIR_ENV, raising=False)
    seen = []
    monkeypatch.setattr(gate, "read_page", lambda *a: seen.append((Connection.__init__, Connection.close)) or {"format": gate.FORMAT, "items": {}})
    monkeypatch.setattr(gate, "reference_zone", lambda reference: "UTC")
    assert gate.dump("ff", "leg", tmp_path / "dump", tmp_path / "reference", visits=[(1.0, 2)], pin_dpr=False) == 0
    assert seen == [before] and not (tmp_path / "dump" / "exits.json").exists()


def test_the_dump_of_an_oracle_run_is_given_its_own_folder_for_the_probe_and_the_environment_is_put_back(tmp_path, monkeypatch):
    seen = []

    def fake_dump(binary, seal, leg, out, visits_asked):
        seen.append(os.environ.get(crash.CRASH_DIR_ENV))
        write_gate_dump(out, {key: items(key[1]) for key in visits_asked})
        return 0

    monkeypatch.setattr(ab, "run_dump", fake_dump)
    monkeypatch.delenv(crash.CRASH_DIR_ENV, raising=False)
    rc, problems = ab.dump_with_exits("firefox-68-again", "pub", None, "linux-x86_64", [(1.0, 0)], tmp_path / "t", tmp_path / "out")
    assert rc == 0 and problems == [] and seen == [str(tmp_path / "out" / "crash" / "firefox-68-again")]
    assert crash.CRASH_DIR_ENV not in os.environ, "the variable is the dump's, not the run's"
    monkeypatch.setattr(ab, "run_dump", lambda *a: (_ for _ in ()).throw(ab.subprocess.TimeoutExpired("x", 1)))
    rc, problems = ab.dump_with_exits("firefox-68", "pub", None, "linux-x86_64", [(1.0, 0)], tmp_path / "t", tmp_path / "out")
    assert rc == "timeout" and problems == [], "a dump that timed out is red already (the dump ended with timeout); its missing record is not said twice"


# --- what every launch leaves, a tab that died, a core with a parent that ended with 0 (scripts/ci_crash_evidence.py, scripts/ci_host_gate.py) ---------------------------------

class ProcessOf:
    pid = 4242

    def __init__(self, code):
        self.code = code

    def poll(self):
        return self.code

    def terminate(self):
        pass


class Tools:
    """`subprocess.run` that answers `gdb` with a backtrace and keeps what it was asked; every other tool of the evidence is not installed."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)

        class Done:
            stdout, stderr = "#0 0x00007f in mozilla::dom::ContentChild::Crash ()\n", ""

        return Done()

    which = staticmethod(lambda name: "/usr/bin/gdb" if name == "gdb" else None)


def a_launch(probe, code, label, text=b"", during=None):
    """A launch as `launch` makes it: the reader of the browser's output first (`ProcessOutput`, over a pipe the browser writes on), then the `Connection`, closed as the wrapper closes it."""
    from invisible_core.juggler.connection import Connection, ProcessOutput

    probe.set_label(**label)
    to_r, to_w = os.pipe()
    from_r, from_w = os.pipe()
    out_r, out_w = os.pipe()
    output = ProcessOutput(os.fdopen(out_r, "rb"))
    conn = Connection(to_w, from_r, ProcessOf(code))
    conn.output = output
    os.write(out_w, text)
    os.close(out_w)
    output.settle()
    if during:
        during()
    conn.close()
    os.close(from_w)
    os.close(to_r)


@pytest.fixture()
def probe_of(tmp_path):
    made = []

    def make(**kw):
        tools = Tools()
        probe = crash.LaunchProbe("/e/firefox", tmp_path / "out" / "crash", windows=False, folders=[], core_folders=[tmp_path / "cores"], run=tools, which=tools.which, **kw)
        (tmp_path / "cores").mkdir(exist_ok=True)
        probe.tools = tools
        made.append(probe.install())
        return probe

    yield make
    for probe in made:
        probe.uninstall()


def test_what_the_browser_wrote_is_saved_for_every_launch_even_when_all_of_them_end_with_0(tmp_path, probe_of):
    probe = probe_of()
    lines = b"".join(b"line %d of the browser\n" % n for n in range(1, 301))  # the core's own reader keeps the last 200
    a_launch(probe, 0, {"seed": 11, "dpr": 1.0, "launch": "visit"}, lines)
    a_launch(probe, 0, {"seed": 12, "dpr": 1.5, "launch": "visit"}, b"[GFX1-]: a warning on stderr\n")
    exits = probe.write()
    folder = tmp_path / "out" / "crash"
    assert sorted(p.name for p in (folder / "stderr").iterdir()) == ["01-11-1.0-visit.txt", "02-12-1.5-visit.txt"]
    first = (folder / "stderr" / "01-11-1.0-visit.txt").read_text(encoding="utf-8")
    assert "line 1 of the browser\n" in first and "line 300 of the browser\n" in first
    assert "[GFX1-]: a warning on stderr" in (folder / "stderr" / "02-12-1.5-visit.txt").read_text(encoding="utf-8")
    document = crash.read_exits(exits)
    assert [x["returncode"] for x in document["launches"]] == [0, 0] and all(x["stderr"].startswith("crash/stderr/") for x in document["launches"])
    assert not [p for p in folder.iterdir() if p.is_dir() and p.name != "stderr"], "a green launch leaves no evidence folder: only its output"
    assert crash.exits_of_run(folder, "firefox-68", False) == [], "green stays green"


def test_the_tee_gives_the_reader_the_same_lines_and_the_probe_a_copy():
    from invisible_core.juggler.connection import ProcessOutput

    class Stream:
        def __init__(self):
            self.lines = iter([b"one\n", b"two\n", b""])

        def readline(self):
            return next(self.lines)

    record = crash.OutputRecord(1, {}, 0.0)
    output = ProcessOutput(crash.OutputTee(Stream(), record))
    output.settle()
    assert output.tail() == ["one", "two"] and record.text() == "one\ntwo\n"


def test_a_launch_whose_output_the_probe_could_not_keep_is_red_not_silent(tmp_path):
    from invisible_core.juggler.connection import Connection

    probe = crash.LaunchProbe("/e/firefox", tmp_path / "out" / "crash", windows=False, folders=[], core_folders=[tmp_path / "cores"]).install(Connection, output=None)
    try:
        a_launch(probe, 0, {"seed": 1, "launch": "visit"}, b"said something\n")
    finally:
        probe.uninstall()
    probe.write()
    lines = crash.exits_of_run(tmp_path / "out" / "crash", "candidate", False)
    assert len(lines) == 2 and "could not save what its browsers wrote" in lines[0] and "did not save what the browser of launch 1 wrote" in lines[1]
    assert not (tmp_path / "out" / "crash" / "stderr").exists()


def test_a_launch_that_ended_while_starting_still_has_its_output_in_the_artifact(tmp_path, probe_of):
    from invisible_core.juggler.connection import ProcessOutput

    probe = probe_of()
    probe.set_label(seed=3, dpr=1.0, launch="visit")
    out_r, out_w = os.pipe()
    output = ProcessOutput(os.fdopen(out_r, "rb"))  # `launch` raised after this: no Connection was made over it
    os.write(out_w, b"Gtk-WARNING: cannot open display\n")
    os.close(out_w)
    output.settle()
    probe.write()
    document = crash.read_exits(tmp_path / "out" / "crash" / "exits.json")
    assert document["launches"] == [] and [x["label"]["seed"] for x in document["launches_without_connection"]] == [3]
    assert "cannot open display" in (tmp_path / "out" / "crash" / "stderr" / "01-3-1.0-visit-no-connection.txt").read_text(encoding="utf-8")


def test_the_core_of_a_content_process_goes_through_gdb_though_the_parent_ended_with_0_and_is_red(tmp_path, probe_of):
    probe = probe_of()
    cores = tmp_path / "cores"
    a_launch(probe, 0, {"seed": 11, "dpr": 1.0, "launch": "visit"}, b"fine\n")
    a_launch(probe, 0, {"seed": 12, "dpr": 1.5, "launch": "visit"}, b"[Child 77] ###!!! ABORT: tab\n", during=lambda: (cores / "core.Web Content.77.1700000000").write_bytes(b"\x7fELF"))
    a_launch(probe, 0, {"seed": 13, "dpr": 1.0, "launch": "visit"}, b"fine\n")
    probe.write()
    launches = crash.read_exits(tmp_path / "out" / "crash" / "exits.json")["launches"]
    assert [x["cores"] for x in launches] == [[], ["core.Web Content.77.1700000000"], []], "a core is one launch's: the next launch does not get it again"
    gdb = [c for c in probe.tools.calls if c[0].endswith("gdb")]
    assert len(gdb) == 1 and all(command in gdb[0] for command in crash.GDB_COMMANDS)
    saved = tmp_path / "out" / launches[1]["evidence"] / "gdb-core.Web_Content.77.1700000000.txt"
    assert "ContentChild::Crash" in saved.read_text(encoding="utf-8")
    lines = crash.exits_of_run(tmp_path / "out" / "crash", "candidate", False)
    assert len(lines) == 1 and "a process of the browser crashed while the parent ended with 0" in lines[0] and "seed 12" in lines[0] and "core core.Web Content.77" in lines[0]
    assert "(evidence: crash/02-12-1.5-visit)" in lines[0] and "run candidate" in lines[0]


def test_a_missing_core_is_said_where_it_was_looked_for_and_never_left_silent(tmp_path, probe_of, monkeypatch):
    monkeypatch.setenv(crash.CORE_DIR_ENV, str(tmp_path / "cores"))
    probe = probe_of()
    a_launch(probe, 0, {"seed": 11, "dpr": 1.0, "launch": "visit"})
    a_launch(probe, -11, {"seed": 12, "dpr": 1.0, "launch": "visit"})
    probe.write()
    green, died = crash.read_exits(tmp_path / "out" / "crash" / "exits.json")["launches"]
    assert "no core file written since the launch began, in %s" % (tmp_path / "cores") in green["core_said"]
    assert "no core file written since the launch began" in " | ".join(died["said"]), "and the launch that died says it too, in its evidence"
    monkeypatch.setenv(crash.CORE_DIR_ENV, "/job/cores")
    assert "the kernel does not write into INVPW_CORE_DIR, so a core would not have been seen there" in crash.core_said([Path("/job/cores")], "|/usr/share/apport/apport %p", False)
    assert "no core folder to look in" in crash.core_said([], "core", False) and "not seen" in crash.core_said([], "core", False)
    assert "Windows" in crash.core_said([], None, True)


# the tab that died: a visit whose session was lost is a leaf of its own, and the comparison is red with that line first

def a_browser(monkeypatch, fail_on=None, error="Page.evaluate: cannot find session"):
    """`invisible_playwright.InvisiblePlaywright` as a fake whose page answers the host gate's page and the states, and raises `error` at the call whose text contains `fail_on`."""
    import types

    answers = {"window.__hostGate": {"format": gate.FORMAT, "items": {"surface": {"x": 1}}}, "__hostGateStates.prepare()": {"x": 5, "y": 6},
               "[innerWidth, innerHeight]": [100, 80], "__hostGateStates.measure()": {"innerWidth": 100}}

    class Mouse:
        def move(self, *a):
            pass

        def click(self, *a):
            pass

    class Page:
        mouse = Mouse()

        def goto(self, url, timeout=None):
            if fail_on == "goto":
                raise RuntimeError("Page.goto: Target page, context or browser has been closed")

        def wait_for_function(self, expression, timeout=None):
            pass

        def evaluate(self, expression):
            if fail_on and fail_on in expression:
                raise RuntimeError(error)
            return json.loads(json.dumps(answers.get(expression)))

    class Browser:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def new_context(self):
            return self

        def new_page(self):
            return Page()

    module = types.ModuleType("invisible_playwright")
    module.InvisiblePlaywright = lambda **options: Browser()
    monkeypatch.setitem(sys.modules, "invisible_playwright", module)


def test_a_session_lost_during_the_states_is_the_leaf_crash_tab_with_the_error_and_the_step(monkeypatch):
    a_browser(monkeypatch, fail_on="__hostGateStates.measure()")
    gate._LAUNCH[0] = "visit"
    result = gate.read_page({"seed": 12}, 1, 1.5, 1000)
    leaf = result["items"]["crash.tab.visit"]
    assert result["format"] == gate.FORMAT and "cannot find session" in leaf["error"] and leaf["step"] == "states: evaluate __hostGateStates.measure()"
    assert result["items"]["surface"] == {"x": 1}, "what the page had read before the tab died is kept"
    assert gate.tab_crash_lines("leg dpr 1.5 seed 12", result["items"]) == ["TAB CRASH leg dpr 1.5 seed 12 crash.tab.visit: %s, at the step states: evaluate __hostGateStates.measure()" % leaf["error"]]


def test_a_tab_that_died_before_the_page_answered_is_the_leaf_alone_and_the_dump_goes_on(monkeypatch):
    a_browser(monkeypatch, fail_on="goto")
    gate._LAUNCH[0] = "relaunch-second"
    try:
        result = gate.read_page({"seed": 0}, 1, 1.0, 1000)
    finally:
        gate._LAUNCH[0] = "visit"
    assert list(result["items"]) == ["crash.tab.relaunch-second"] and result["items"]["crash.tab.relaunch-second"]["step"] == "goto"


def test_an_error_that_is_not_a_lost_tab_is_still_raised_as_it_always_was(monkeypatch):
    a_browser(monkeypatch, fail_on="__hostGateStates.measure()", error="the page threw a TypeError")
    gate._LAUNCH[0] = "visit"
    result = gate.read_page({"seed": 12}, 1, 1.5, 1000)
    assert "crash.tab.visit" not in result["items"] and "error" in result["items"]["state.normal"], "the states say it, as before"
    a_browser(monkeypatch, fail_on="window.__hostGate", error="the page threw a TypeError")
    with pytest.raises(RuntimeError, match="TypeError"):
        gate.read_page({"seed": 12}, 1, 1.5, 1000)


def test_the_second_launch_keeps_the_tab_that_died_in_the_first():
    seen = iter([{"format": gate.FORMAT, "items": {"crash.tab.relaunch-first": {"error": "cannot find session", "step": "goto"}}}, {"format": gate.FORMAT, "items": {"surface": {"x": 1}}}])
    got = gate.second_launch({"seed": 0}, 1, 1.0, 1000, read=lambda *a: next(seen))
    assert got["items"]["crash.tab.relaunch-first"]["step"] == "goto" and got["items"]["surface"] == {"x": 1}


def test_the_ab_visit_records_the_tab_that_died_and_goes_no_further(monkeypatch):
    import types

    log = []

    class Page:
        def goto(self, url, timeout=None):
            pass

        def go_back(self, timeout=None):
            raise RuntimeError("Page.goBack: cannot find session")

        def wait_for_function(self, expression, timeout=None):
            pass

        def evaluate(self, expression):
            return {"top": 1} if expression == "window.__abResult" else {"n": 2, "persisted": False, "data": 1}

    class Context:
        def new_page(self):
            return Page()

        def close(self):
            log.append("closed")
            raise RuntimeError("Context.close: Target closed")  # closing the context of a dead tab says nothing more

    class Browser:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def new_context(self, **options):
            return Context()

    module = types.ModuleType("invisible_playwright")
    module.InvisiblePlaywright = lambda **options: Browser()
    monkeypatch.setitem(sys.modules, "invisible_playwright", module)
    got = gate.ab_visit("ff", 0, "UTC", 1.0, timeout_ms=10)
    assert list(got) == ["crash.tab.ab-visit"] and got["crash.tab.ab-visit"]["step"] == "go back (context launch)" and log == ["closed"]


def a_visit_where_the_tab_died(error="Page.evaluate: cannot find session"):
    """The visit as the dead side dumped it: what the page read before, the leaf of the crash, and the states the drive did not reach, each `{"error": ...}`."""
    its = items(12)
    its.update(crash.tab_crash_item("visit", "states: evaluate __hostGateStates.measure()", RuntimeError(error)))
    for name in gate.STATES:
        its[gate.STATE + name] = {"error": "RuntimeError: " + error}
    return its


def test_a_visit_whose_tab_died_is_red_with_the_tab_crash_first_and_no_cascade_of_missing_leaves():
    a, b = visits(), visits()
    for side in (a, b):
        for key in side:
            for name in gate.STATES:
                side[key][gate.STATE + name] = {"innerWidth": 1920, "innerHeight": 1000, "screen": {"width": 1920}}
    b[(1.5, 12)] = a_visit_where_the_tab_died()
    report = run(a, b)
    assert report["verdict"] == "red"
    assert report["problems"][0].startswith("TAB CRASH side B seed 12 dpr 1.5 launch visit at step states: evaluate __hostGateStates.measure()")
    assert "cannot find session" in report["problems"][0] and "are not compared" in report["problems"][0]
    assert report["differences"] == [] and len(report["problems"]) == 1, "no leaf of the dead visit is listed: the cascade is one line"
    assert "<missing>" not in json.dumps(report) and report["tab_crashes"][0]["launch"] == "visit"


def test_the_leaf_alone_makes_the_comparison_red_though_every_other_leaf_is_equal():
    a, b = visits(), visits()
    b[(1.0, 11)].update(crash.tab_crash_item("visit", "goto", RuntimeError("Target page, context or browser has been closed")))
    report = run(a, b)
    assert report["verdict"] == "red" and report["differences"] == [] and len(report["problems"]) == 1
    assert report["problems"][0] == "TAB CRASH side B seed 11 dpr 1.0 launch visit at step goto: RuntimeError: Target page, context or browser has been closed"
    assert run(visits(), visits())["verdict"] == "green", "and without the leaf the same dumps are green"


def test_a_tab_that_died_on_the_published_side_is_named_on_side_a_and_a_second_launch_that_died_is_named_by_its_launch():
    a, b = {(1.0, 0): items(0)}, {(1.0, 0): items(0)}
    a[(1.0, 0)].update(crash.tab_crash_item("visit", "goto", RuntimeError("cannot find session")))
    b[(1.0, 0)].update({gate.RELAUNCH + k: v for k, v in crash.tab_crash_item("relaunch-second", "goto", RuntimeError("cannot find session")).items()})
    problems = ab.compare_visits(a, b, [], SMALL, expected=((1.0, 0),))["problems"]
    assert problems[0].startswith("TAB CRASH side A seed 0 dpr 1.0 launch visit") and problems[1].startswith("TAB CRASH side B seed 0 dpr 1.0 launch relaunch-second")


def test_the_tab_crash_comes_before_the_exit_of_the_dump_it_left_and_before_the_leaves():
    b = visits()
    b[(1.5, 12)] = a_visit_where_the_tab_died()
    b[(1.0, 13)]["surface"]["navigator"]["language"] = "it-IT"
    report = ab.judge(visits(), b, [], extra_problems=["the dump with candidate ended with 1"], expected=VISITS)
    assert report["verdict"] == "red"
    assert report["problems"][0].startswith("TAB CRASH") and report["problems"][1] == "the dump with candidate ended with 1"
    assert report["problems"][-1].startswith("seed 13 dpr 1.0 surface.navigator.language"), "the differences stay the last lines"


def test_a_dump_prints_the_tab_crash_and_writes_the_visit_with_its_leaf(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(crash.CRASH_DIR_ENV, raising=False)
    monkeypatch.setattr(gate, "read_page", lambda *a: {"format": gate.FORMAT, "items": a_visit_where_the_tab_died()})
    monkeypatch.setattr(gate, "reference_zone", lambda reference: "UTC")
    assert gate.dump("ff", "linux-x86_64", tmp_path / "dump", tmp_path / "reference", visits=[(1.5, 12)], pin_dpr=False) == 0
    assert "TAB CRASH linux-x86_64 dpr 1.5 seed 12 crash.tab.visit: RuntimeError: Page.evaluate: cannot find session" in capsys.readouterr().out
    written = json.loads((tmp_path / "dump" / "linux-x86_64_dpr1.5_seed12.json").read_text(encoding="utf-8"))
    assert written["result"]["items"]["crash.tab.visit"]["step"].startswith("states:")


def test_every_page_oracle_job_uploads_the_folder_where_the_oracle_keeps_what_each_launch_left_whatever_the_result():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github" / "workflows" / "e2e.yml").read_text(encoding="utf-8")
    for name in ("e2e-page", "e2e-page-windows", "e2e-page-final", "e2e-page-final-windows"):
        body = _job(text, name)
        step = body[body.index("      - name: Upload what the test saved"):]
        assert "if: always()" in step and "${{ runner.temp }}/invpw-evidence\n" in step, "%s: the evidence folder (ab-oracle/crash/<run>/stderr, exits.json, gdb files) is uploaded green or red" % name
    assert "RUNNER_TEMP" in (root / "tests" / "test_ab_oracle_e2e.py").read_text(encoding="utf-8") and '"invpw-evidence" / "ab-oracle"' in (root / "tests" / "test_ab_oracle_e2e.py").read_text(encoding="utf-8")
