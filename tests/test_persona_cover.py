"""The seeds that cover the personas the core can declare (scripts/persona_cover.py, ci/persona-seeds.json).

The A/B oracle visits a few seeds, so which seeds it visits decides which values of the identity it can ever compare. ci/persona-seeds.json holds the sets,
computed from the prefs the core emits for the seeds 0..99999. Four things are held here:

  * the file is what a recompute gives, in the core this environment installs: a change of the core's tables (or of the code that reads them) is red until
    `python scripts/persona_cover.py write` has been run again and the file committed;
  * `final` reaches every value of every dimension, and no seed of it is needless; `candidate` reaches every renderer and every DPR;
  * a cover with a seed less is red, and says which values it no longer reaches;
  * the algorithms (greedy cover, reverse delete, the exact smallest candidate) on a small space whose answer is known, and a pref that varies without
    belonging to a dimension is an error.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import persona_cover as pc  # noqa: E402

pytestmark = pytest.mark.unit

STORED = pc.DEFAULT_PATH


@pytest.fixture(scope="module")
def stored():
    return pc.load(STORED)


@pytest.fixture(scope="module")
def recomputed(stored):
    """Every seed of the stored range through the core of this environment, once for the module (processes, one per core)."""
    return pc.compute(stored["max_seed"])


def test_the_stored_file_is_what_a_recompute_gives(stored, recomputed):
    fresh = pc.build(recomputed, stored["max_seed"])
    lines = pc.differences(stored, fresh)
    assert lines == [], "ci/persona-seeds.json is stale (core tables or the code that reads them changed): run `python scripts/persona_cover.py write`:\n" + "\n".join(lines)
    assert STORED.read_bytes() == pc.render(fresh), "the file is not the canonical rendering of its content"


def test_the_sha256_is_that_of_the_dimensions_and_the_features(stored):
    body = {"dimensions": stored["dimensions"], "features": stored["features"]}
    assert stored["sha256"] == hashlib.sha256(pc.canonical(body).encode("utf-8")).hexdigest()


def test_the_file_is_lf_only_and_sorted(stored):
    raw = STORED.read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")
    assert json.loads(raw) == stored and raw == pc.render(stored)


def test_final_reaches_every_value_of_every_dimension(stored):
    assert set(stored["dimensions"]) == set(pc.ALL_DIMENSIONS)
    for dim, values in stored["dimensions"].items():
        hit = {stored["features"][str(s)][dim] for s in stored["final"]}
        assert hit == set(values), "%s: final does not reach %s" % (dim, sorted(set(values) - hit))


def test_no_seed_of_final_is_needless(stored):
    features = {int(s): f for s, f in stored["features"].items()}
    for seed in stored["final"]:
        rest = [s for s in stored["final"] if s != seed]
        assert pc.covers(features, rest), "seed %d adds nothing to final: the reverse delete should have dropped it" % seed


def test_candidate_reaches_every_renderer_and_every_dpr_with_the_fewest_seeds(stored):
    for dim in pc.CANDIDATE_DIMENSIONS:
        hit = {stored["features"][str(s)][dim] for s in stored["candidate"]}
        assert hit == set(stored["dimensions"][dim]), "%s: candidate does not reach %s" % (dim, sorted(set(stored["dimensions"][dim]) - hit))
    # one seed per renderer is the least a set can do: there are as many renderers as seeds, so no seed of the candidate is shared
    assert len(stored["candidate"]) == len(stored["dimensions"]["webgl_renderer"]) == 5
    assert all(stored["dpr"][str(s)] in (1.0, 1.25, 1.5) for s in stored["candidate"])


def test_every_seed_has_its_own_dpr_and_it_is_the_one_its_features_say(stored):
    for seed, dpr in stored["dpr"].items():
        assert dpr == float(json.loads(stored["features"][seed]["dpr"].split("|")[0].split("=", 1)[1]))
    assert set(stored["dpr"]) == set(stored["features"]) == {str(s) for s in set(stored["final"]) | set(stored["candidate"])}


def test_a_cover_with_a_seed_less_is_red_and_says_what_it_no_longer_reaches(stored, recomputed):
    broken = dict(stored, final=stored["final"][1:])
    lost = pc.covers(recomputed, broken["final"])
    assert lost, "dropping %d from final must leave something unreached" % stored["final"][0]
    assert all(isinstance(pair, tuple) and pair[0] in pc.ALL_DIMENSIONS for pair in lost)


def test_a_stale_file_is_red_and_says_what_changed(stored, recomputed):
    fresh = pc.build(recomputed, stored["max_seed"])
    gone = copy.deepcopy(stored)
    gone["final"] = gone["final"][1:]
    gone["dimensions"]["cores"] = gone["dimensions"]["cores"][1:]
    first = next(iter(gone["features"]))
    gone["features"][first]["cores"] = "hw_concurrency=999"
    lines = pc.differences(gone, fresh)
    assert any(l.startswith("final:") for l in lines)
    assert any(l.startswith("dimension cores:") for l in lines)
    assert "seed %s: cores changed" % first in lines
    assert pc.differences(dict(stored, core_version="0.0.0"), fresh) == []  # the version alone is no difference


def test_the_dimensions_are_those_of_the_whole_range_not_only_of_the_chosen_seeds(stored, recomputed):
    assert reachable_of(recomputed) == stored["dimensions"]
    sizes = {d: len(v) for d, v in stored["dimensions"].items()}
    assert sizes["cores"] == 17 and sizes["webgl_renderer"] == 5 and sizes["gfx_device"] == 20 and sizes["dark_theme"] == 2


def reachable_of(features):
    return pc.reachable(features)


# --- the algorithms, on a space with a known answer ------------------------------------------------------------------------------------------------

def make(rows, dims):
    """features with only `dims` meaningful; the other dimensions hold one constant value."""
    return {seed: {d: row.get(d, "x") for d in pc.ALL_DIMENSIONS} for seed, row in rows.items()}, dims


def test_the_greedy_cover_takes_the_largest_gain_and_ties_go_to_the_smallest_seed():
    features, dims = make({
        5: {"cores": "a", "audio": "p"},
        3: {"cores": "a", "audio": "p"},  # the same pairs as 5: 3 is the one
        9: {"cores": "b", "audio": "q"},
        7: {"cores": "c", "audio": "p"},
    }, ("cores", "audio"))
    chosen = pc.greedy_cover(features, dims)
    assert chosen == [3, 9, 7]  # 3 and 7 and 9 all gain 2 at first: 3 is the smallest; then 9 gains two, 7 one. 5 is never chosen
    assert pc.covers(features, chosen, dims) == []


def test_the_reverse_delete_drops_a_seed_the_others_do_without():
    features, dims = make({1: {"cores": "a"}, 2: {"cores": "b"}, 3: {"cores": "a", "audio": "q"}, 4: {"audio": "q", "cores": "b"}}, ("cores", "audio"))
    assert pc.reverse_delete(features, [1, 2, 3, 4], dims) == [2, 3]  # 4 goes first (1, 2 and 3 do without it), then 1 (2 and 3 do without it); 2 and 3 are each the only one with something


def test_the_candidate_is_the_smallest_set_and_the_smallest_seeds_of_that_size():
    features = {
        10: {"webgl_renderer": "r1", "dpr": "d1"},
        11: {"webgl_renderer": "r2", "dpr": "d1"},
        12: {"webgl_renderer": "r2", "dpr": "d2"},
        13: {"webgl_renderer": "r1", "dpr": "d2"},
        14: {"webgl_renderer": "r1", "dpr": "d1"},
    }
    assert pc.candidate_cover(features) == [10, 12]  # r1+d1, r2+d2; 11+13 would do too, 10 and 12 are smaller


def test_a_pref_that_varies_and_belongs_to_no_dimension_is_an_error(monkeypatch):
    monkeypatch.setitem(pc.DIMENSIONS, "cores", ("zoom.stealth.nothing_of_the_kind",))
    with pytest.raises(SystemExit) as e:
        pc.compute(300, workers=1)
    assert "zoom.stealth.hw_concurrency" in str(e.value) and "belong to no dimension" in str(e.value)


def test_an_excluded_pref_that_stopped_varying_is_an_error(monkeypatch):
    monkeypatch.setitem(pc.EXCLUDED, "zoom.stealth.hw_concurrency_never_varies", "x")
    with pytest.raises(SystemExit) as e:
        pc.compute(50, workers=1)
    assert "no longer vary" in str(e.value)


def test_a_derived_pref_with_two_values_within_one_value_of_its_dimension_is_an_error():
    pref = "zoom.stealth.screen.ui_normal"  # of the dpr
    rows = {
        1: {"dpr": "d1", "_derived": {p: "1" for p in pc.DERIVED}},
        2: {"dpr": "d1", "_derived": {p: "1" for p in pc.DERIVED}},
        3: {"dpr": "d2", "_derived": {p: "2" for p in pc.DERIVED}},
    }
    rows = {seed: dict(f, screen_panel="panel-" + f["dpr"]) for seed, f in rows.items()}
    pc.check_derived(rows)  # a function of the dimension: fine
    rows[2]["_derived"][pref] = "9"  # the same dpr, another bar
    with pytest.raises(SystemExit) as e:
        pc.check_derived(rows)
    assert pref in str(e.value) and "not a function of the dimension dpr" in str(e.value)


def test_a_derived_pref_the_core_does_not_emit_is_accepted():
    rows = {s: {"dpr": "d%d" % (s % 2), "screen_panel": "p%d" % s, "_derived": {p: "<absent>" for p in pc.DERIVED}}
            for s in range(6)}
    pc.check_derived(rows)  # a core that predates the pref: no seed has it
    rows[3]["_derived"]["zoom.stealth.screen.ui_compact"] = "73"  # a core that emits it for some seeds only varies
    with pytest.raises(SystemExit):
        pc.check_derived(rows)


def test_a_derived_pref_is_in_no_dimension_and_names_a_dimension():
    in_a_dimension = {k for keys in pc.DIMENSIONS.values() for k in keys}
    assert not set(pc.DERIVED) & in_a_dimension
    assert not set(pc.DERIVED) & set(pc.EXCLUDED)
    assert set(pc.DERIVED.values()) <= set(pc.DIMENSIONS)
    assert pc.DERIVED["zoom.stealth.screen.maximized_ui"] == "screen_panel"


def test_the_derived_prefs_of_the_core_in_use_are_functions_of_their_dimensions():
    # compute() raises on the first seed pair that breaks it; this core's own prefs, over a range.
    features = pc.compute(2000, workers=1)
    assert all("_derived" not in f for f in features.values())
