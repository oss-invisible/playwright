#!/usr/bin/env python3
"""A/B oracle: same seed, same bytes, the published engine against the candidate.

A page reads the same values from two engines that differ only by the work of one
release. This runs the host gate's dump (scripts/ci_host_gate.py dump: the page in
scripts/host_gate, whole APIs) twice on the SAME machine, with the same core and the same
wrapper, over the persona seeds (ci/persona-seeds.json, computed by scripts/persona_cover.py
from the prefs the core emits: the `candidate` set, 5 seeds that reach every webgl renderer
and every DPR, or the `final` set, 20 seeds that reach every value of every dimension). Each
seed runs at its OWN DPR (nothing is pinned):

  1. with the published engine (firefox-68 by default): its seal and its archive are
     downloaded the way the core downloads an engine (the release's seal.json, then
     invisible_core.download.ensure_binary under that seal, which checks the sha256 of
     the archive against the seal and the engine against the seal);
  2. with the candidate binary the gate already holds, under its own seal.

Then every leaf of every visit is compared. Everything must be equal. The only
differences allowed are the `noise` entries of host_gate/exceptions.json (values that
differ between two launches of one engine) and keys under the diag. prefix (what the
page records about the host). Any other difference is red and is printed with the leaf,
the two values and the seed. It is also red when a visit is missing on one side, or when a
visit compares fewer leaves than MIN_LEAVES says: a page that dumped less than the host gate's
own reference would pass anything.

    python ci_ab_oracle.py run <candidate-firefox> --candidate-seal gate.seal.json \\
        --leg linux-x86_64 --out ab-oracle [--engine-tag firefox-68] [--seed-set candidate|final] [--shard i/n]
    python ci_ab_oracle.py compare ab-oracle [--seed-set ...] [--shard i/n]   (again, from the two dumps)

--shard i/n (1-based) takes every n-th seed of the set, from the i-th: the final set can be split over n jobs
(more runners, not more browsers per runner). The seed set and the shard of `compare` must be those of the run:
they say which visits were asked for.

Writes, in --out: dump-<N>.json (the published engine's items), dump-candidate.json and
diff.json (the verdict, the compared leaves per visit, every difference).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_host_gate as gate  # noqa: E402
import persona_cover as personas  # noqa: E402

FORMAT = "ab-oracle/1"
#: The engine the candidate is held to: the last one published.
ENGINE_TAG = "firefox-68"
CANDIDATE = "candidate"
#: What the host gate's own reference holds per visit (non-diag leaves at each DPR, today).
#: A visit that compares fewer leaves than this is red. Moves by hand with the reference
#: (tests/test_ab_oracle.py holds the two together). A DPR the reference does not record (the personas
#: also have 1.25) is held to the least of the ones it does.
MIN_LEAVES = {1.0: 16439, 1.5: 16439}
#: The seeds a run visits, from ci/persona-seeds.json: the smallest set that reaches every webgl renderer and every DPR,
#: or the one that reaches every value of every dimension the core declares.
SEED_SETS = ("candidate", "final")
SEED_SET = "candidate"
#: A value in diff.json longer than this is replaced by its length and its sha256 (the full value is in the dumps).
CLIP = 400
#: How many differences one run prints; diff.json has all of them.
PRINT_LIMIT = 200
#: What a leaf that is not in a visit reads as.
MISSING = "<missing>"
#: Said by the run when no visit is dumped within this time (the host gate's own budget is 180 s per page).
DUMP_TIMEOUT_S = 40 * 60


def dump_name(engine_tag: str) -> str:
    """dump-68.json for firefox-68."""
    return "dump-%s.json" % engine_tag.rsplit("-", 1)[-1]


# -
# The comparison: two sets of visits, leaf by leaf.

def _clip(value):
    text = json.dumps(value, sort_keys=True)
    if len(text) <= CLIP:
        return value
    return {"clipped": text[:CLIP // 2], "len": len(text), "sha256": hashlib.sha256(text.encode()).hexdigest()}


def visit_name(dpr, seed) -> str:
    return "dpr%s_seed%d" % (dpr, seed)


def parse_shard(text: str | None) -> tuple:
    """(i, n) from "i/n" (1-based, 1 <= i <= n); None is the whole set, (1, 1)."""
    if text is None:
        return (1, 1)
    try:
        i, n = (int(x) for x in text.split("/"))
    except ValueError:
        raise SystemExit("--shard is i/n, two whole numbers, not %r" % (text,)) from None
    if not 1 <= i <= n:
        raise SystemExit("--shard is i/n with 1 <= i <= n, not %r" % (text,))
    return (i, n)


def select_visits(seed_set: str = SEED_SET, shard: str | None = None, path: Path = personas.DEFAULT_PATH) -> list:
    """[(dpr, seed)] of the seed set, each seed at its own DPR, the i-th of n shards (every n-th seed from the i-th)."""
    if seed_set not in SEED_SETS:
        raise SystemExit("--seed-set is one of %s, not %r" % ("/".join(SEED_SETS), seed_set))
    i, n = parse_shard(shard)
    every = [(dpr, seed) for seed, dpr in personas.load_visits(seed_set, path)]
    mine = every[i - 1::n]
    if not mine:
        raise SystemExit("shard %d/%d of the %d seeds of %r is empty" % (i, n, len(every), seed_set))
    return mine


def comparable(items: dict) -> dict:
    """The leaves of one visit that are held to equality: everything but diag."""
    return {k: v for k, v in gate.leaves(items).items() if not gate.is_diag(k)}


def compare_visits(a: dict, b: dict, noise: list, min_leaves: dict | None = None,
                   expected: tuple | None = None) -> dict:
    """a and b map (dpr, seed) to the items of that visit (as the dump returns them).

    Returns the report: {"verdict": "green"|"red", "problems": [str], "differences":
    [{"seed", "dpr", "leaf", "a", "b", "what"}], "compared": {visit: leaves}, "excused": n}.
    `noise` is the list of entries of exceptions.json's "noise". `min_leaves` maps a dpr to the
    fewest leaves a visit of that dpr may compare; `expected` is the visits both sides must have
    (default: every seed at every DPR of the host gate)."""
    min_leaves = MIN_LEAVES if min_leaves is None else min_leaves
    if expected is None:
        expected = tuple((dpr, seed) for dpr in gate.DPRS for seed in gate.SEEDS)
    problems, differences, compared, excused = [], [], {}, 0
    for side, visits in (("A", a), ("B", b)):
        gone = [visit_name(*k) for k in expected if k not in visits]
        if gone:
            problems.append("side %s has no dump of %s" % (side, ", ".join(gone)))
        stray = [visit_name(*k) for k in sorted(visits) if k not in expected]
        if stray:
            problems.append("side %s has dumps that were not asked for: %s" % (side, ", ".join(stray)))
    for key in expected:
        if key not in a or key not in b:
            continue
        dpr, seed = key
        la, lb = comparable(a[key]), comparable(b[key])
        count = 0
        for leaf in sorted(set(la) | set(lb)):
            if gate.matching(noise, leaf):
                excused += 1
                continue
            count += 1
            va, vb = la.get(leaf, MISSING), lb.get(leaf, MISSING)
            if va != vb:
                differences.append({"seed": seed, "dpr": dpr, "leaf": leaf, "a": _clip(va), "b": _clip(vb),
                                    "what": gate.describe(va, vb)})
        compared[visit_name(dpr, seed)] = count
        floor = min_leaves.get(dpr, min(min_leaves.values(), default=0))
        if count < floor:
            problems.append("%s compared %d leaves, fewer than the %d of the host gate's reference: "
                            "the page dumped less than it should" % (visit_name(dpr, seed), count, floor))
    for d in differences:
        problems.append("seed %d dpr %s %s: %s" % (d["seed"], d["dpr"], d["leaf"], d["what"]))
    return {"verdict": "red" if problems else "green", "problems": problems, "differences": differences,
            "compared": compared, "excused": excused}


# -
# The files: what a run writes and what compare reads back.

def read_visits(directory: Path) -> dict:
    """(dpr, seed) -> items, from the *.json the host gate's dump wrote in directory."""
    visits = {}
    for path in sorted(directory.glob("*.json")):
        d = json.loads(path.read_text(encoding="utf-8"))
        visits[(d["dpr"], d["seed"])] = d["result"]["items"]
    return visits


def pack(engine: str, visits: dict) -> dict:
    return {"format": FORMAT, "engine": engine,
            "visits": [{"dpr": dpr, "seed": seed, "items": visits[(dpr, seed)]} for dpr, seed in sorted(visits)]}


def unpack(document: dict) -> dict:
    if document.get("format") != FORMAT:
        raise SystemExit("not an %s dump: format %r" % (FORMAT, document.get("format")))
    return {(v["dpr"], v["seed"]): v["items"] for v in document["visits"]}


def judge(published: dict, candidate: dict, noise: list, extra_problems=(), expected: tuple | None = None) -> dict:
    report = compare_visits(published, candidate, noise, expected=expected)
    if extra_problems:
        report["problems"] = list(extra_problems) + report["problems"]
        report["verdict"] = "red"
    return report


def write_json(path: Path, document) -> None:
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")


def print_report(report: dict, published: str) -> None:
    n = len(report["differences"])
    print("compared %d visits, leaves per visit: %s; %d leaves excused as noise"
          % (len(report["compared"]), ", ".join("%s %d" % kv for kv in sorted(report["compared"].items())) or "none",
             report["excused"]))
    for d in report["differences"][:PRINT_LIMIT]:
        print("DIFF seed %d dpr %s leaf %s: %s %s | %s %s" % (d["seed"], d["dpr"], d["leaf"], published,
                                                              json.dumps(d["a"], sort_keys=True)[:160],
                                                              CANDIDATE, json.dumps(d["b"], sort_keys=True)[:160]))
    if n > PRINT_LIMIT:
        print("... and %d more differences in diff.json" % (n - PRINT_LIMIT))
    for p in report["problems"][:len(report["problems"]) - n]:  # the differences are the last n problems
        print("FAIL " + p)
    print("AB ORACLE %s: %d differences, %d problems"
          % ("OK" if report["verdict"] == "green" else "FAILED", n, len(report["problems"])))


# -
# The run: two dumps, one per engine, as subprocesses (the seal a process runs under is read at import).

def fetch_published(tag: str, work: Path) -> tuple:
    """(binary, seal path) of the published engine `tag`, fetched the way the core fetches an engine."""
    os.environ.setdefault("INVISIBLE_CORE_AUTOFIX", "off")
    os.environ.setdefault("INVISIBLE_CORE_PIN", "allow")
    from invisible_core import download
    from invisible_core.constants import RELEASE_URL_TEMPLATE
    from invisible_core.seal import load_seal

    owner, repo = download._parse_owner_repo(RELEASE_URL_TEMPLATE)
    seal_path = work / ("seal-%s.json" % tag)
    sources = download.release_asset_urls(owner, repo, tag, "seal.json",
                                          RELEASE_URL_TEMPLATE.format(tag=tag, asset="seal.json"))
    download._download_file(sources, seal_path)
    seal = load_seal(seal_path)
    if seal.tag != tag or seal.is_local:
        raise SystemExit("the seal of the release %s says tag %r (local: %s)" % (tag, seal.tag, seal.is_local))
    print("published engine %s: %s" % (tag, seal.describe()))
    sys.stdout.flush()
    # the engine store of this fetch only: the dumps that follow run under the environment they were given
    before = os.environ.get(download.ENGINE_DIR_ENV)
    os.environ[download.ENGINE_DIR_ENV] = str(work / "engines")
    try:
        binary = download.ensure_binary(seal=seal)
    finally:
        if before is None:
            os.environ.pop(download.ENGINE_DIR_ENV, None)
        else:
            os.environ[download.ENGINE_DIR_ENV] = before
    return str(binary), seal_path


def run_dump(binary: str, seal: Path, leg: str, out: Path, visits: list | None = None) -> int:
    """The host gate's dump of `visits` ([(dpr, seed)], each seed at its own DPR) as a subprocess; None is the host gate's own grid."""
    env = dict(os.environ, INVISIBLE_SEAL_FILE=str(seal), INVISIBLE_CORE_AUTOFIX="off", INVISIBLE_CORE_PIN="allow")
    for name in ("MOZ_LOG", "MOZ_LOG_FILE"):  # the e2e legs log the engine; a dump runs as the host gate's does
        env.pop(name, None)
    cmd = [sys.executable, str(Path(gate.__file__)), "dump", binary, "--leg", leg, "--out", str(out)]
    if visits is not None:
        cmd += ["--no-pin"] + [a for dpr, seed in visits for a in ("--visit", "%d:%s" % (seed, dpr))]
    return subprocess.run(cmd, env=env, timeout=DUMP_TIMEOUT_S).returncode


def run(candidate: str, candidate_seal: Path, leg: str, out: Path, engine_tag: str,
        published: tuple | None = None, visits: list | None = None) -> int:
    """`visits` is [(dpr, seed)] (default: the candidate set of the persona seeds, each at its own DPR)."""
    visits = select_visits() if visits is None else visits
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="ab-oracle-"))
    started = time.time()
    problems, dumps = [], {}
    try:
        binary, seal = published or fetch_published(engine_tag, work)
        for name, exe, seal_file in ((engine_tag, binary, seal), (CANDIDATE, candidate, candidate_seal)):
            target = work / name
            print("=== dump with %s: %s (seal %s), seeds %s"
                  % (name, exe, seal_file, " ".join("%d@%s" % (seed, dpr) for dpr, seed in visits)))
            sys.stdout.flush()
            try:
                rc = run_dump(exe, Path(seal_file), leg, target, visits)
            except subprocess.TimeoutExpired:
                rc = "timeout"
            if rc != 0:
                problems.append("the dump with %s ended with %s" % (name, rc))
            dumps[name] = read_visits(target) if target.is_dir() else {}
            write_json(out / (dump_name(engine_tag) if name == engine_tag else "dump-%s.json" % CANDIDATE),
                       pack(name, dumps[name]))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - a run that could not get its engine is red and says why
        problems.append("%s: %s" % (type(e).__name__, str(e)[:500]))
        for name in (engine_tag, CANDIDATE):
            dumps.setdefault(name, {})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    noise = gate.load_exceptions().get("noise", [])
    report = judge(dumps[engine_tag], dumps[CANDIDATE], noise, problems, expected=tuple(visits))
    report.update(format=FORMAT, engine=engine_tag, leg=leg, seconds=round(time.time() - started),
                  visits=["%d@%s" % (seed, dpr) for dpr, seed in visits])
    write_json(out / "diff.json", report)
    print_report(report, engine_tag)
    return 0 if report["verdict"] == "green" else 1


def recompare(directory: Path, engine_tag: str, visits: list | None = None) -> int:
    visits = select_visits() if visits is None else visits
    published = unpack(json.loads((directory / dump_name(engine_tag)).read_text(encoding="utf-8")))
    candidate = unpack(json.loads((directory / ("dump-%s.json" % CANDIDATE)).read_text(encoding="utf-8")))
    report = judge(published, candidate, gate.load_exceptions().get("noise", []), expected=tuple(visits))
    report.update(format=FORMAT, engine=engine_tag, visits=["%d@%s" % (seed, dpr) for dpr, seed in visits])
    write_json(directory / "diff.json", report)
    print_report(report, engine_tag)
    return 0 if report["verdict"] == "green" else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("candidate", help="the candidate's firefox binary")
    r.add_argument("--candidate-seal", type=Path, default=os.environ.get("INVISIBLE_SEAL_FILE"),
                   help="the candidate's seal (default: INVISIBLE_SEAL_FILE)")
    r.add_argument("--leg", required=True)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--engine-tag", default=ENGINE_TAG)
    r.add_argument("--seed-set", choices=SEED_SETS, default=SEED_SET,
                   help="the persona seeds to visit (ci/persona-seeds.json), each at its own DPR")
    r.add_argument("--shard", help="i/n: every n-th seed of the set from the i-th (the final set over n jobs)")
    r.add_argument("--published-binary", type=Path, help="use this binary for the published engine instead of fetching it")
    r.add_argument("--published-seal", type=Path, help="with --published-binary: the seal of that engine")
    c = sub.add_parser("compare")
    c.add_argument("directory", type=Path)
    c.add_argument("--engine-tag", default=ENGINE_TAG)
    c.add_argument("--seed-set", choices=SEED_SETS, default=SEED_SET)
    c.add_argument("--shard")
    args = ap.parse_args(argv)
    if args.cmd == "compare":
        return recompare(args.directory, args.engine_tag, select_visits(args.seed_set, args.shard))
    if not args.candidate_seal:
        ap.error("--candidate-seal (or INVISIBLE_SEAL_FILE) is required: the candidate runs under its own seal")
    given = None
    if args.published_binary or args.published_seal:
        if not (args.published_binary and args.published_seal):
            ap.error("--published-binary and --published-seal go together")
        given = (str(args.published_binary), args.published_seal)
    return run(args.candidate, Path(args.candidate_seal), args.leg, args.out, args.engine_tag, given,
               select_visits(args.seed_set, args.shard))


if __name__ == "__main__":
    sys.exit(main())
