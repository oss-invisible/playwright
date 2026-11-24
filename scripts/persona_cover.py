#!/usr/bin/env python3
"""The seeds that cover the personas the core can declare.

The core turns a seed into an identity (invisible_core._fpforge.profile.generate_profile) and the identity into
the prefs the engine is launched with (invisible_core.prefs.translate_profile_to_prefs). A page can only see what
those prefs make it see, so the values a seed can make a page read are the values of the prefs that VARY across
seeds. This computes them from the prefs themselves, never from a list of the tables:

  * every seed 0..MAX_SEED-1 goes through generate_profile and translate_profile_to_prefs (en-US, one zone: neither
    is a seed dimension);
  * every pref whose value differs between two seeds must belong to a DIMENSION below (a group of prefs read
    together: the renderer, the screen panel, the audio chain...) or be named in EXCLUDED with its reason. A pref
    that varies and is in neither is an error, so a new pref of a later core cannot go uncovered unnoticed;
  * a dimension's value for a seed is the joint value of its prefs; the reachable values of a dimension are the
    ones some seed in range produces.

From that, two sets of seeds (a greedy set cover, ties to the smallest seed, then a reverse delete of every seed
the rest does without):

  final      covers every reachable value of every dimension: the whole persona space the core declares;
  candidate  the smallest set that covers every webgl renderer and every DPR (exact: every set of that size is
             searched, the smallest seeds win): what the candidate run of the A/B oracle visits.

The result is ci/persona-seeds.json, with the sha256 of the canonical JSON of its dimensions and features.
`check` recomputes everything and exits 1 with what changed: a change of the core's tables (or of the code that
reads them) is red until `write` has been run again.

    python persona_cover.py write [--path ci/persona-seeds.json] [--max-seed 100000]
    python persona_cover.py check [--path ...]
    python persona_cover.py show  [--path ...]
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "ci" / "persona-seeds.json"
FORMAT = "persona-seeds/1"
MAX_SEED = 100_000
#: Neither is a seed dimension (locale and zone come from the exit), so one value of each is used for every seed.
LOCALE = "en-US"
ZONE = "America/New_York"

P = "zoom.stealth."
#: dimension -> the prefs that are read together. A pref may be in more than one dimension (the DPR is in the panel and
#: in the scale). "webgl_renderer" is every webgl.* pref: they must be a function of the renderer (checked).
DIMENSIONS = {
    "webgl_renderer": (P + "webgl.renderer",),
    "gfx_feature_status": (P + "gfx.feature_status",),
    "screen_panel": (P + "screen.width", P + "screen.height", P + "screen.avail_device_width",
                     P + "screen.avail_device_height", "layout.css.devPixelsPerPx"),
    "dpr": ("layout.css.devPixelsPerPx", P + "screen.frame_border", P + "screen.maximized_ui", P + "screen.popup_ui"),
    "cores": (P + "hw_concurrency",),
    "audio": (P + "audio.sample_rate", P + "audio.output_latency_ms", P + "audio.max_channel_count"),
    "codecs": ("media.av1.enabled", "media.encoder.webm.enabled", "media.mp4.enabled", "media.webm.enabled",
               P + "media.decode_support"),
    "dark_theme": ("ui.systemUsesDarkTheme", "layout.css.prefers-color-scheme.content-override"),
    "render_noise": (P + "fpp.hw_seed",),
}
#: Dimensions that are not prefs: the label of the device the persona declares (the prefs carry only its blocklist
#: outcome, gfx_feature_status, which has fewer values than there are devices).
PROFILE_DIMENSIONS = ("gfx_device",)
ALL_DIMENSIONS = tuple(DIMENSIONS) + PROFILE_DIMENSIONS
#: Prefs that vary and are not covered, with the reason: a dimension with hundreds of values cannot be visited.
EXCLUDED = {
    P + "media.enumeration_ms": "the time of the first enumerateDevices(): a draw from its own stream, hundreds of values, "
                                "no class of seeds to cover",
}
#: What the candidate run visits: the smallest set that reaches every one of these dimensions' values.
CANDIDATE_DIMENSIONS = ("webgl_renderer", "dpr")
WEBGL = P + "webgl."
#: A value longer than this is shown as its sha256 prefix.
LONG = 40


def _short(value) -> str:
    text = json.dumps(value, sort_keys=True)
    if isinstance(value, str) and len(value) > LONG:
        return "sha:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return text


def _dimension_value(prefs: dict, keys: tuple) -> str:
    return "|".join("%s=%s" % (k.removeprefix(P), _short(prefs.get(k, "<absent>"))) for k in keys)


# -
# One seed, and a range of seeds (a worker computes a range).

def seed_features(seed: int) -> tuple:
    """(features of the seed, {pref: its value as JSON}). The features are {dimension: value}."""
    from invisible_core._fpforge.profile import generate_profile
    from invisible_core.prefs import translate_profile_to_prefs

    profile = generate_profile(seed)
    prefs = translate_profile_to_prefs(profile, locale=LOCALE, timezone=ZONE)
    features = {dim: _dimension_value(prefs, keys) for dim, keys in DIMENSIONS.items()}
    features["webgl_renderer"] = prefs[P + "webgl.renderer"]  # readable: the whole string
    features["gfx_device"] = profile.gpu.device or "<none>"
    webgl = {k: json.dumps(v, sort_keys=True) for k, v in prefs.items() if k.startswith(WEBGL)}
    features["_webgl"] = hashlib.sha256(json.dumps(webgl, sort_keys=True).encode()).hexdigest()[:12]
    return features, {k: json.dumps(v, sort_keys=True) for k, v in prefs.items()}


def _range(bounds: tuple) -> tuple:
    """(features per seed, {pref: up to 3 distinct values}) for seeds lo..hi-1."""
    lo, hi = bounds
    features, seen = {}, {}
    for seed in range(lo, hi):
        f, prefs = seed_features(seed)
        features[seed] = f
        for k, v in prefs.items():
            s = seen.setdefault(k, set())
            if len(s) < 3:
                s.add(v)
        for k in set(seen) - set(prefs):  # a pref this seed does not have varies too
            seen[k].add("<absent>")
    return features, seen


def compute(max_seed: int = MAX_SEED, workers: int | None = None) -> dict:
    """The features of every seed 0..max_seed-1: {seed: {dimension: value}} (the dimensions of ALL_DIMENSIONS).

    Raises when a pref varies across the seeds and is in no dimension and not in EXCLUDED, or when the webgl prefs are
    not a function of the renderer."""
    workers = workers or max(1, min(os.cpu_count() or 1, 8))
    step = max(1, -(-max_seed // (workers * 4)))
    bounds = [(lo, min(lo + step, max_seed)) for lo in range(0, max_seed, step)]
    if workers == 1:
        parts = [_range(b) for b in bounds]
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            parts = list(pool.map(_range, bounds))
    features, seen = {}, {}
    for f, s in parts:
        features.update(f)
        for k, vs in s.items():
            seen.setdefault(k, set()).update(vs)
    covered = {k for keys in DIMENSIONS.values() for k in keys}
    varying = sorted(k for k, vs in seen.items() if len(vs) > 1)
    loose = [k for k in varying if k not in covered and k not in EXCLUDED and not k.startswith(WEBGL)]
    if loose:
        raise SystemExit("prefs that vary across seeds and belong to no dimension and are not excluded: %s\n"
                         "add each to a DIMENSION or to EXCLUDED (with the reason) in scripts/persona_cover.py" % loose)
    byrenderer = {}
    for seed in sorted(features):
        f = features[seed]
        if byrenderer.setdefault(f["webgl_renderer"], f["_webgl"]) != f["_webgl"]:
            raise SystemExit("the webgl prefs are not a function of the renderer (seed %d, %s): split the dimension"
                             % (seed, f["webgl_renderer"]))
        del f["_webgl"]
    stale = [k for k in EXCLUDED if k not in seen or len(seen[k]) < 2]
    if stale:
        raise SystemExit("excluded prefs that no longer vary: %s: remove them from EXCLUDED" % stale)
    return features


# -
# The covers.

def reachable(features: dict, dims=ALL_DIMENSIONS) -> dict:
    """{dimension: sorted reachable values}."""
    out = {d: set() for d in dims}
    for f in features.values():
        for d in dims:
            out[d].add(f[d])
    return {d: sorted(v) for d, v in out.items()}


def counts(features: dict) -> dict:
    out = {d: {} for d in ALL_DIMENSIONS}
    for f in features.values():
        for d in ALL_DIMENSIONS:
            out[d][f[d]] = out[d].get(f[d], 0) + 1
    return {d: dict(sorted(c.items())) for d, c in out.items()}


def _elements(f: dict, dims) -> frozenset:
    return frozenset((d, f[d]) for d in dims)


def greedy_cover(features: dict, dims=ALL_DIMENSIONS) -> list:
    """Greedy set cover over the (dimension, value) pairs, ties to the smallest seed. Seeds that produce the same
    pairs are one candidate (their smallest)."""
    first = {}
    for seed in sorted(features):
        first.setdefault(_elements(features[seed], dims), seed)
    todo = set().union(*first)
    chosen = []
    while todo:
        best = max(sorted(first.items(), key=lambda kv: kv[1]), key=lambda kv: len(kv[0] & todo))
        chosen.append(best[1])
        todo -= best[0]
    return chosen


def reverse_delete(features: dict, seeds: list, dims=ALL_DIMENSIONS) -> list:
    """Drops, last chosen first, every seed whose pairs the others cover."""
    keep = list(seeds)
    for seed in reversed(seeds):
        rest = [s for s in keep if s != seed]
        if rest and _elements(features[seed], dims) <= set().union(*(_elements(features[s], dims) for s in rest)):
            keep = rest
    return sorted(keep)


def final_cover(features: dict) -> list:
    return reverse_delete(features, greedy_cover(features))


def candidate_cover(features: dict, dims=CANDIDATE_DIMENSIONS) -> list:
    """The smallest set covering every value of dims: the exact minimum (every combination of the seeds that are the
    smallest of their kind is tried, size by size), the lexicographically smallest seeds of that size."""
    kinds = {}
    for seed in sorted(features):
        kinds.setdefault(_elements(features[seed], dims), seed)
    todo = set().union(*kinds)
    seeds = sorted(kinds.values())
    for size in range(1, len(seeds) + 1):
        for combo in itertools.combinations(seeds, size):
            if set().union(*(_elements(features[s], dims) for s in combo)) >= todo:
                return list(combo)
    raise AssertionError("unreachable: all the seeds cover everything")


def covers(features: dict, seeds, dims=ALL_DIMENSIONS) -> list:
    """The (dimension, value) pairs of the reachable space that `seeds` do not reach."""
    want = set().union(*(_elements(f, dims) for f in features.values()))
    got = set().union(*(_elements(features[s], dims) for s in seeds)) if seeds else set()
    return sorted(want - got)


# -
# The file.

def canonical(document) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def build(features: dict, max_seed: int) -> dict:
    import invisible_core

    final, candidate = final_cover(features), candidate_cover(features)
    stored = sorted(set(final) | set(candidate))
    dimensions = reachable(features)
    kept = {str(s): features[s] for s in stored}
    dpr = {str(s): float(json.loads(features[s]["dpr"].split("|")[0].split("=", 1)[1])) for s in stored}
    sha = hashlib.sha256(canonical({"dimensions": dimensions, "features": kept}).encode("utf-8")).hexdigest()
    return {
        "format": FORMAT, "core_version": invisible_core.__version__, "max_seed": max_seed,
        "candidate": candidate, "final": final, "dpr": dpr, "features": kept, "dimensions": dimensions,
        "counts": counts(features), "excluded": dict(sorted(EXCLUDED.items())), "sha256": sha,
    }


def render(document: dict) -> bytes:
    return (json.dumps(document, sort_keys=True, indent=1, ensure_ascii=True) + "\n").encode("utf-8")


def write(path: Path = DEFAULT_PATH, max_seed: int = MAX_SEED, workers: int | None = None) -> dict:
    document = build(compute(max_seed, workers), max_seed)
    Path(path).write_bytes(render(document))
    return document


def differences(stored: dict, fresh: dict) -> list:
    """What differs between the file and a recompute, as lines (empty when they agree). core_version alone is no
    difference: the same values from another version of the core are the same personas."""
    out = []
    for key in sorted(set(stored) | set(fresh)):
        if key == "core_version" or stored.get(key) == fresh.get(key):
            continue
        a, b = stored.get(key), fresh.get(key)
        if key == "dimensions" and isinstance(a, dict) and isinstance(b, dict):
            for dim in sorted(set(a) | set(b)):
                gone = sorted(set(a.get(dim, [])) - set(b.get(dim, [])))
                new = sorted(set(b.get(dim, [])) - set(a.get(dim, [])))
                if gone or new:
                    out.append("dimension %s: %d values no seed reaches any more %s; %d new %s"
                               % (dim, len(gone), gone[:3], len(new), new[:3]))
        elif key == "features" and isinstance(a, dict) and isinstance(b, dict):
            for seed in sorted(set(a) | set(b), key=int):
                if a.get(seed) != b.get(seed):
                    moved = sorted(d for d in set(a.get(seed) or {}) | set(b.get(seed) or {})
                                   if (a.get(seed) or {}).get(d) != (b.get(seed) or {}).get(d))
                    out.append("seed %s: %s changed" % (seed, ", ".join(moved) or "is not in one of the two"))
        else:
            out.append("%s: %s -> %s" % (key, json.dumps(a)[:160], json.dumps(b)[:160]))
    return out


def check(path: Path = DEFAULT_PATH, workers: int | None = None) -> list:
    stored = json.loads(Path(path).read_text(encoding="utf-8"))
    fresh = build(compute(stored["max_seed"], workers), stored["max_seed"])
    lines = differences(stored, fresh)
    if not lines and Path(path).read_bytes() != render(fresh):
        lines.append("the file is not the canonical rendering of its own content: run `write`")
    return lines


def load(path: Path = DEFAULT_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_seeds(kind: str = "candidate", path: Path = DEFAULT_PATH) -> list:
    """The seeds of `kind` ("candidate" or "final"), ascending."""
    document = load(path)
    if kind not in ("candidate", "final"):
        raise ValueError("kind must be 'candidate' or 'final', not %r" % (kind,))
    return sorted(document[kind])


def load_visits(kind: str = "candidate", path: Path = DEFAULT_PATH) -> list:
    """[(seed, the seed's own DPR)] of `kind`, ascending."""
    document = load(path)
    return [(s, document["dpr"][str(s)]) for s in load_seeds(kind, path)]


def show(document: dict) -> str:
    lines = ["core %s, seeds 0..%d, sha256 %s" % (document["core_version"], document["max_seed"] - 1, document["sha256"][:16])]
    for kind in ("candidate", "final"):
        lines.append("%s (%d): %s" % (kind, len(document[kind]), document[kind]))
    for dim, values in document["dimensions"].items():
        lines.append("%-20s %2d values" % (dim, len(values)))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=("write", "check", "show"))
    ap.add_argument("--path", type=Path, default=DEFAULT_PATH)
    ap.add_argument("--max-seed", type=int, default=MAX_SEED)
    ap.add_argument("--workers", type=int)
    args = ap.parse_args(argv)
    if args.cmd == "show":
        print(show(load(args.path)))
        return 0
    if args.cmd == "write":
        print(show(write(args.path, args.max_seed, args.workers)))
        return 0
    lines = check(args.path, args.workers)
    for line in lines:
        print("CHANGED " + line)
    print("PERSONA SEEDS %s" % ("OK" if not lines else "STALE: run `python scripts/persona_cover.py write` and commit ci/persona-seeds.json"))
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main())
