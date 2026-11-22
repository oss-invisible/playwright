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

The dump also reads what the host gate's retail comparison leaves alone, the ab. leaves (ci_host_gate.is_ab): ab.poison on every seed (zone, language, colour scheme, a
canary font and the audio context, which scripts/ci_poison_host.py holds to the declared identity on a poisoned host) and, on seeds 0 and 63, the extra visit of every realm and
window a page can make (--ab; ci_host_gate.ab_visit). They are compared like every other leaf; the diag. ones are not.

Then every leaf of every visit is compared. Everything must be equal. The only
differences allowed are the `noise` entries of host_gate/exceptions.json (values that
differ between two launches of one engine) and keys under the diag. prefix (what the
page records about the host). Any other difference is red and is printed with the leaf,
the two values and the seed. It is also red when a visit is missing on one side, or when a
visit compares fewer leaves than MIN_LEAVES says: a page that dumped less than the host gate's
own reference would pass anything.

    python ci_ab_oracle.py run <candidate-firefox> --candidate-seal gate.seal.json \\
        --leg linux-x86_64 --out ab-oracle [--engine-tag firefox-68] [--seed-set candidate|final] [--shard i/n]
    python ci_ab_oracle.py run --published-binary <firefox> --leg linux-x86_64 --out ab-oracle --limit 2   (the A/A alone)
    python ci_ab_oracle.py compare ab-oracle [--a-a] [--seed-set ...] [--shard i/n]   (again, from the dumps)

--shard i/n (1-based) takes every n-th seed of the set, from the i-th: the final set can be split over n jobs
(more runners, not more browsers per runner). The seed set and the shard of `compare` must be those of the run:
they say which visits were asked for.

THE CONTROL. `run` first dumps the published engine TWICE and compares the two (the A/A): a difference there says the
oracle is broken (what is compared is not stable, or the engine is not repeatable), not that the candidate is wrong. It is
exit 2 and the A/B is not run. Without a candidate (a pull request: the engine under test IS the published one, given with
--published-binary) the A/A is the whole run; with a candidate (the gate's leg) the A/B follows, against the first dump of
the published engine. Exit codes: 0 green, 1 red (a finding), 2 broken control or a run that could not run.
--limit N visits only the first N seeds of the set (the A/A of a pull request, which has a time budget per test).

THE SECOND LAUNCH. The visit of seed 0 (RELAUNCH_SEED) is also launched twice on ONE profile folder (the wrapper's persistent context, the profile a user
keeps between runs): the dump keeps the items of the second launch in that visit under the prefix `relaunch.`, next to the visit's own items (a fresh
profile), so the retail comparison of the host gate is never touched. They are compared apart: the visit must have them on both sides, and as many as the
floor (they are not counted in the visit's own leaves, so a page that dumped less cannot hide behind them). The A/A and the A/B hold them to equality like the rest.

HOW EACH LAUNCH ENDS. Every browser a dump launches (the visits, the two launches of the pair on one profile, the ab visit) is seen by a probe in the dump process
(scripts/ci_crash_evidence.py: it reads the exit code of the browser's parent after the close the launch already makes, and a watcher reads the crash folders while it lives). A
parent that did not end with 0, or whose code was not seen, is a RED line that names the side, the seed, the dpr, the launch and the code, in the A/A and in the A/B alike; a dump
process whose probe wrote nothing is red too. What the launch left is kept in `<out>/crash/<run>/`: the core run through `gdb -batch` with the build ids, the `.extra` and the minidump
names, `said.txt` for what the collectors did not find, and `exits.json` with how every launch ended. This oracle runs no strace (a trace would slow the browser and change the
outcome it observes): the strace tail is the host oracle's.

WHAT A LAUNCH LEAVES, ALWAYS. The probe also saves, for EVERY launch of both engines (and of the A/A), what the browser wrote on stdout and stderr (the core merges them on one pipe) in
`<out>/crash/<run>/stderr/<launch>.txt`, green or not, and the cores written since the launch began (Linux) are run through gdb whatever code the parent ended with: a content process can
die and the parent still end with 0, and then the launch is red and names the core. A launch without a core says where it looked (`core_said` in exits.json). A TAB THAT DIED, or a session lost
during a visit (the core's `cannot find session`, a closed target), is the leaf `crash.tab.<launch>` of that visit, with the error and the step (`.error`, `.step`), and the comparison puts it FIRST:
`TAB CRASH side B seed 12 dpr 1.5 launch visit at step ...`. That leaf alone makes the verdict red; the leaves the dead tab could not read (or that only the other side has) are not listed as a
cascade of `<missing>`: they are counted in that line.

    python ci_ab_oracle.py touched BASE_SHA     (prints touched=true|false: does a pull request change a file of this oracle; e2e.yml's final-set job reads it)

Writes, in --out: dump-<N>.json and dump-<N>-again.json (the published engine's items, twice), diff-a-a.json (the A/A),
and with a candidate dump-candidate.json and diff.json (the verdict, the compared leaves per visit, every difference).
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
import ci_crash_evidence as crash  # noqa: E402
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
#: The seed whose visit is also launched twice on one profile folder, and the prefix its second launch's leaves have (scripts/ci_host_gate.py holds it).
RELAUNCH_SEED = 0
RELAUNCH = gate.RELAUNCH
#: A value in diff.json longer than this is replaced by its length and its sha256 (the full value is in the dumps).
CLIP = 400
#: How many differences one run prints; diff.json has all of them.
PRINT_LIMIT = 200
#: What a leaf that is not in a visit reads as.
MISSING = "<missing>"
#: How the verdict names a tab that died (or a session that was lost) during a visit, first of all the lines: the dump records it as the leaf `crash.tab.<launch>`
#: (scripts/ci_crash_evidence.py), and the leaves the dead tab could not read are consequences, not differences.
TAB_CRASH = "TAB CRASH"
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


def select_visits(seed_set: str = SEED_SET, shard: str | None = None, path: Path = personas.DEFAULT_PATH,
                  limit: int | None = None) -> list:
    """[(dpr, seed)] of the seed set, each seed at its own DPR, the i-th of n shards (every n-th seed from the i-th),
    the first `limit` of them when a limit is given."""
    if seed_set not in SEED_SETS:
        raise SystemExit("--seed-set is one of %s, not %r" % ("/".join(SEED_SETS), seed_set))
    i, n = parse_shard(shard)
    every = [(dpr, seed) for seed, dpr in personas.load_visits(seed_set, path)]
    mine = every[i - 1::n][:limit]
    if not mine:
        raise SystemExit("shard %d/%d of the %d seeds of %r is empty" % (i, n, len(every), seed_set))
    return mine


def relaunch_visits(visits) -> tuple:
    """The visits (dpr, seed) that are launched twice on one profile folder: the ones of RELAUNCH_SEED."""
    return tuple(key for key in visits if key[1] == RELAUNCH_SEED)


def _bare(leaf: str) -> str:
    """The leaf without the prefix of the second launch: what exceptions.json names."""
    return leaf[len(RELAUNCH):] if leaf.startswith(RELAUNCH) else leaf


def comparable(items: dict) -> dict:
    """The leaves of one visit that are held to equality: everything but diag (a second launch's leaves keep their prefix)."""
    return {k: v for k, v in gate.leaves(items).items() if not gate.is_diag(_bare(k))}


def split_launches(leaves: dict) -> tuple:
    """(the leaves of the visit on a fresh profile, the leaves of the second launch)."""
    first = {k: v for k, v in leaves.items() if not k.startswith(RELAUNCH)}
    return first, {k: v for k, v in leaves.items() if k.startswith(RELAUNCH)}


def tab_crashes(leaves: dict) -> list:
    """[(launch, error, step)] of the tab crashes one side's visit recorded: the leaves `crash.tab.<launch>.error` and `.step` (a second launch's keep their prefix)."""
    found: dict = {}
    for leaf, value in leaves.items():
        bare = _bare(leaf)
        if bare.startswith(crash.TAB_PREFIX):
            launch, _, field = bare[len(crash.TAB_PREFIX):].partition(".")
            found.setdefault((leaf.startswith(RELAUNCH), launch), {})[field or "error"] = value
    return [(launch, f.get("error"), f.get("step")) for (_second, launch), f in sorted(found.items())]


def compare_visits(a: dict, b: dict, noise: list, min_leaves: dict | None = None,
                   expected: tuple | None = None, relaunch: tuple | None = None) -> dict:
    """a and b map (dpr, seed) to the items of that visit (as the dump returns them).

    Returns the report: {"verdict": "green"|"red", "problems": [str], "differences":
    [{"seed", "dpr", "leaf", "a", "b", "what"}], "compared": {visit: leaves}, "excused": n}.
    `noise` is the list of entries of exceptions.json's "noise". `min_leaves` maps a dpr to the
    fewest leaves a visit of that dpr may compare; `expected` is the visits both sides must have
    (default: every seed at every DPR of the host gate). `relaunch` is the visits that were launched twice on one profile (default: those of RELAUNCH_SEED among the expected ones): each side must have the
    second launch's leaves (prefix `relaunch.`) in them, as many as the floor, counted apart (report["relaunch"]), and a side must not have them in a visit that
    was not asked to."""
    min_leaves = MIN_LEAVES if min_leaves is None else min_leaves
    if expected is None:
        expected = tuple((dpr, seed) for dpr in gate.DPRS for seed in gate.SEEDS)
    relaunch = relaunch_visits(expected) if relaunch is None else relaunch
    problems, differences, compared, excused, relaunched = [], [], {}, 0, {}
    crashes, cascade = [], {}
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
        count, again, skipped = 0, 0, 0
        floor = min_leaves.get(dpr, min(min_leaves.values(), default=0))
        died = {"A": tab_crashes(la), "B": tab_crashes(lb)}
        crashed = bool(died["A"] or died["B"])
        for side, found in died.items():
            crashes += [{"side": side, "seed": seed, "dpr": dpr, "launch": launch, "error": error, "step": step} for launch, error, step in found]
        for leaf in sorted(set(la) | set(lb)):
            if gate.matching(noise, _bare(leaf)):
                excused += 1
                continue
            if _bare(leaf).startswith(crash.TAB_PREFIX):
                continue  # said as a TAB CRASH, once, with its error and its step: not a difference of values
            if crashed and (leaf not in la or leaf not in lb):
                skipped += 1  # what the dead tab could not read (or the other side read and this one never got to): a consequence, said once below
                continue
            second = leaf.startswith(RELAUNCH)
            count, again = count + (not second), again + second
            va, vb = la.get(leaf, MISSING), lb.get(leaf, MISSING)
            if va != vb:
                differences.append({"seed": seed, "dpr": dpr, "leaf": leaf, "a": _clip(va), "b": _clip(vb),
                                    "what": gate.describe(va, vb)})
        compared[visit_name(dpr, seed)] = count
        if skipped:
            cascade[visit_name(dpr, seed)] = skipped
        if count < floor and not crashed:
            problems.append("%s compared %d leaves, fewer than the %d of the host gate's reference: "
                            "the page dumped less than it should" % (visit_name(dpr, seed), count, floor))
        for side, leaves_of in (("A", la), ("B", lb)):
            has = len(split_launches(leaves_of)[1])
            if key in relaunch and has < floor and not crashed:
                problems.append("side %s: the second launch of %s on a reused profile has %d leaves under %s, fewer than the %d of the host gate's "
                                "reference: it did not run, or the page dumped less than it should" % (side, visit_name(dpr, seed), has, RELAUNCH, floor))
            if key not in relaunch and has and not crashed:
                problems.append("side %s has the second launch of %s on a reused profile, which was not asked for" % (side, visit_name(dpr, seed)))
        if key in relaunch:
            relaunched[visit_name(dpr, seed)] = again
    tab_lines = ["%s side %s seed %d dpr %s launch %s at step %s: %s%s" % (
        TAB_CRASH, c["side"], c["seed"], c["dpr"], c["launch"], c["step"], c["error"],
        " (the %d leaves of this visit that the tab could not read, or that only the other side has, are not compared)" % cascade[visit_name(c["dpr"], c["seed"])]
        if visit_name(c["dpr"], c["seed"]) in cascade else "") for c in crashes]
    problems = tab_lines + problems  # the tab crash is the first line of the verdict, before anything it caused
    for d in differences:
        problems.append("seed %d dpr %s %s: %s" % (d["seed"], d["dpr"], d["leaf"], d["what"]))
    return {"verdict": "red" if problems else "green", "problems": problems, "differences": differences, "tab_crashes": crashes,
            "compared": compared, "relaunch": relaunched, "excused": excused}


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


def judge(published: dict, candidate: dict, noise: list, extra_problems=(), expected: tuple | None = None,
          relaunch: tuple | None = None) -> dict:
    report = compare_visits(published, candidate, noise, expected=expected, relaunch=relaunch)
    if extra_problems:
        crashed = [x for x in report["problems"] if x.startswith(TAB_CRASH)]  # a tab that died is named before the exit code of the dump it left
        report["problems"] = crashed + list(extra_problems) + report["problems"][len(crashed):]
        report["verdict"] = "red"
    return report


def write_json(path: Path, document) -> None:
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")


def print_report(report: dict, published: str, other: str = CANDIDATE, title: str = "AB ORACLE") -> None:
    n = len(report["differences"])
    print("compared %d visits, leaves per visit: %s; %d leaves excused as noise"
          % (len(report["compared"]), ", ".join("%s %d" % kv for kv in sorted(report["compared"].items())) or "none",
             report["excused"]))
    if report.get("relaunch"):
        print("second launch on a reused profile, leaves per visit: %s" % ", ".join("%s %d" % kv for kv in sorted(report["relaunch"].items())))
    for d in report["differences"][:PRINT_LIMIT]:
        print("DIFF seed %d dpr %s leaf %s: %s %s | %s %s" % (d["seed"], d["dpr"], d["leaf"], published,
                                                              json.dumps(d["a"], sort_keys=True)[:160],
                                                              other, json.dumps(d["b"], sort_keys=True)[:160]))
    if n > PRINT_LIMIT:
        print("... and %d more differences in diff.json" % (n - PRINT_LIMIT))
    for p in report["problems"][:len(report["problems"]) - n]:  # the differences are the last n problems
        print("FAIL " + p)
    print("%s %s: %d differences, %d problems"
          % (title, "OK" if report["verdict"] == "green" else "FAILED", n, len(report["problems"])))


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


def own_cache_env(env: dict, folder: str, platform: str | None = None) -> dict:
    """`env` with the engine's per-user cache in a folder of this run (Linux: `XDG_CACHE_HOME`), and the wrapper's own cache where it was.

    Why. fontconfig names the cache of the bundle's font folder by the md5 of its path, and writes it to the first writable cache folder
    it knows, `$XDG_CACHE_HOME/fontconfig` or the legacy `~/.fontconfig`; it reads all of them. Every launch of the published engine of a
    job has the same bundle path, so a launch that shares the user's folders leaves the cache of the bundle where a later launch of the
    job reads it and finds it warm: tests/test_the_parent_reads_the_bundle_from_fontconfig_s_cache_linux_e2e.py needs its first launch
    COLD (its run 44946411675 found `~/.fontconfig/<md5>-le64.cache-9` already there). The oracles' launches use a folder of their own, so
    they neither read what an earlier test left nor leave what a later one would read. Both engines of an A/A start from the same empty
    cache, which is also what makes their first launch the same.

    `XDG_CACHE_HOME` is also where the wrapper looks for its engine (`platformdirs`): `INVISIBLE_PLAYWRIGHT_CACHE_DIR` is pinned to where
    that is now, and an engine folder or a cache folder the caller set is kept. Windows has no fontconfig: `env` comes back as it was."""
    if (platform or sys.platform) == "win32":
        return env
    import platformdirs

    out = dict(env)
    out.setdefault("INVISIBLE_PLAYWRIGHT_CACHE_DIR", platformdirs.user_cache_dir("invisible-playwright"))
    out["XDG_CACHE_HOME"] = str(folder)
    return out


def dump_with_exits(name: str, exe: str, seal_file, leg: str, visits, target: Path, out: Path) -> tuple:
    """The dump of one engine run as `run_dump` makes it, with the folder where its probe writes how each browser ended (`<out>/crash/<name>`, named to the dump process by
    AB_CRASH_DIR): `(rc or "timeout", the red lines of the launches that did not end with 0, or that nobody saw)`. A dump process whose probe wrote nothing is red: an oracle
    that does not see how its browsers end does not see them."""
    crash_dir = out / "crash" / name
    try:
        with crash.environment(**{crash.CRASH_DIR_ENV: crash_dir}):
            rc = run_dump(exe, Path(seal_file) if seal_file else None, leg, target, visits)
    except subprocess.TimeoutExpired:
        rc = "timeout"
    return rc, crash.exits_of_run(crash_dir, name, windows=leg.startswith("win"), needed=(rc == 0))


def run_dump(binary: str, seal: Path | None, leg: str, out: Path, visits: list | None = None) -> int:
    """The host gate's dump of `visits` ([(dpr, seed)], each seed at its own DPR) as a subprocess; None is the host gate's own grid."""
    env = dict(os.environ, INVISIBLE_CORE_AUTOFIX="off", INVISIBLE_CORE_PIN="allow")
    if seal is not None:  # None: the engine runs under the seal of the core it is launched with (a pull request's published engine)
        env["INVISIBLE_SEAL_FILE"] = str(seal)
    for name in ("MOZ_LOG", "MOZ_LOG_FILE"):  # the e2e legs log the engine; a dump runs as the host gate's does
        env.pop(name, None)
    cmd = [sys.executable, str(Path(gate.__file__)), "dump", binary, "--leg", leg, "--out", str(out)]
    if visits is not None:
        if relaunch_visits(visits):
            cmd += ["--relaunch-seed", str(RELAUNCH_SEED)]
        cmd += ["--no-pin", "--ab"] + [a for dpr, seed in visits for a in ("--visit", "%d:%s" % (seed, dpr))]
    with tempfile.TemporaryDirectory(prefix="ab-oracle-cache-") as cache:
        return subprocess.run(cmd, env=own_cache_env(env, cache), timeout=DUMP_TIMEOUT_S).returncode


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
            rc, exits = dump_with_exits(name, exe, seal_file, leg, visits, target, out)
            if rc != 0:
                problems.append("the dump with %s ended with %s" % (name, rc))
            problems += exits
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


def again_name(engine_tag: str) -> str:
    """dump-68-again.json for firefox-68: the second dump of the published engine, the A/A's other side."""
    return dump_name(engine_tag)[:-len(".json")] + "-again.json"


def _dump_visits(name: str, exe: str, seal_file, leg: str, visits: list, work: Path, out: Path, file_name: str) -> tuple:
    """One engine's dump of `visits`: ({(dpr, seed): items}, problems); the packed dump is written to out/file_name."""
    target = work / name
    print("=== dump with %s: %s (seal %s), seeds %s"
          % (name, exe, seal_file or "of the core", " ".join("%d@%s" % (seed, dpr) for dpr, seed in visits)))
    sys.stdout.flush()
    rc, exits = dump_with_exits(name, exe, seal_file, leg, visits, target, out)
    problems = ([] if rc == 0 else ["the dump with %s ended with %s" % (name, rc)]) + exits
    dumped = read_visits(target) if target.is_dir() else {}
    write_json(out / file_name, pack(name, dumped))
    return dumped, problems


def run_ab(leg: str, out: Path, engine_tag: str, published: tuple | None = None, candidate: str | None = None,
           candidate_seal: Path | None = None, visits: list | None = None) -> int:
    """The A/A (the published engine twice), then, with a candidate, the A/B against the first dump. 0 green, 1 a finding,
    2 a broken control (the A/A differs, or a dump of it did not run: the A/B is not run) .

    `published` is (binary, seal or None); without it the engine is fetched the way the core fetches one."""
    visits = select_visits() if visits is None else visits
    expected = tuple(visits)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="ab-oracle-"))
    started = time.time()
    noise = gate.load_exceptions().get("noise", [])
    asked = ["%d@%s" % (seed, dpr) for dpr, seed in visits]
    try:
        try:
            binary, seal = published or fetch_published(engine_tag, work)
            first, p1 = _dump_visits(engine_tag, binary, seal, leg, visits, work, out, dump_name(engine_tag))
            again, p2 = _dump_visits(engine_tag + "-again", binary, seal, leg, visits, work, out, again_name(engine_tag))
            problems = p1 + p2
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001 - a run that could not get its engine is red and says why
            first, again, problems = {}, {}, ["%s: %s" % (type(e).__name__, str(e)[:500])]
        control = judge(first, again, noise, problems, expected=expected)
        control.update(format=FORMAT, engine=engine_tag, leg=leg, control=True, seconds=round(time.time() - started), visits=asked)
        write_json(out / "diff-a-a.json", control)
        print_report(control, engine_tag, engine_tag + "-again", "AB ORACLE A/A")
        if control["verdict"] != "green":
            print("BROKEN CONTROL: the published engine differs from itself (or did not run twice), so what is compared is not stable: "
                  "the A/B is not run, and the candidate is not judged")
            return 2
        if candidate is None:
            return 0
        cand, pc = _dump_visits(CANDIDATE, candidate, candidate_seal, leg, visits, work, out, "dump-%s.json" % CANDIDATE)
        report = judge(first, cand, noise, pc, expected=expected)
        report.update(format=FORMAT, engine=engine_tag, leg=leg, seconds=round(time.time() - started), visits=asked)
        write_json(out / "diff.json", report)
        print_report(report, engine_tag)
        return 0 if report["verdict"] == "green" else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


def clean_name(engine_tag: str) -> str:
    """poison-clean-68.json for firefox-68: the published engine's dump on the CLEAN runner, kept beside the poisoned run's (scripts/ci_poison_host.py)."""
    return "poison-clean-" + dump_name(engine_tag)[len("dump-"):]


def run_clean(leg: str, out: Path, engine_tag: str, visits: list, published: tuple | None = None) -> int:
    """The published engine's dump of `visits` on this machine, written to out/clean_name(engine_tag), before the poison is applied. It judges nothing: 0
    when it dumped, 1 when it did not (the by-product of the poisoned run is then missing and says so)."""
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="ab-oracle-clean-"))
    try:
        binary, seal = published or fetch_published(engine_tag, work)
        _, problems = _dump_visits(engine_tag + "-clean", binary, seal, leg, visits, work, out, clean_name(engine_tag))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - said, not raised: this is a by-product
        problems = ["%s: %s" % (type(e).__name__, str(e)[:500])]
    finally:
        shutil.rmtree(work, ignore_errors=True)
    for p in problems:
        print("CLEAN DUMP: " + p)
    return 1 if problems else 0


def recompare(directory: Path, engine_tag: str, visits: list | None = None, a_a: bool = False) -> int:
    visits = select_visits() if visits is None else visits
    published = unpack(json.loads((directory / dump_name(engine_tag)).read_text(encoding="utf-8")))
    if a_a:
        again = unpack(json.loads((directory / again_name(engine_tag)).read_text(encoding="utf-8")))
        report = judge(published, again, gate.load_exceptions().get("noise", []), expected=tuple(visits))
        report.update(format=FORMAT, engine=engine_tag, control=True, visits=["%d@%s" % (seed, dpr) for dpr, seed in visits])
        write_json(directory / "diff-a-a.json", report)
        print_report(report, engine_tag, engine_tag + "-again", "AB ORACLE A/A")
        return 0 if report["verdict"] == "green" else 2
    candidate = unpack(json.loads((directory / ("dump-%s.json" % CANDIDATE)).read_text(encoding="utf-8")))
    report = judge(published, candidate, gate.load_exceptions().get("noise", []), expected=tuple(visits))
    report.update(format=FORMAT, engine=engine_tag, visits=["%d@%s" % (seed, dpr) for dpr, seed in visits])
    write_json(directory / "diff.json", report)
    print_report(report, engine_tag)
    return 0 if report["verdict"] == "green" else 1


def touched(changed) -> bool:
    """Does a change to `changed` (paths of a pull request) touch a file of this oracle (scripts/ci_ab_pr_seeds.py holds the list)?"""
    import ci_ab_pr_seeds as pr

    return bool(pr.touches(changed, pr.ORACLES["AB_ORACLE_PR_SEEDS"][2]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("candidate", nargs="?", help="the candidate's firefox binary (none: the A/A of the published engine alone)")
    r.add_argument("--candidate-seal", type=Path, default=os.environ.get("INVISIBLE_SEAL_FILE"),
                   help="the candidate's seal (default: INVISIBLE_SEAL_FILE)")
    r.add_argument("--leg", required=True)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--engine-tag", default=ENGINE_TAG)
    r.add_argument("--seed-set", choices=SEED_SETS, default=SEED_SET,
                   help="the persona seeds to visit (ci/persona-seeds.json), each at its own DPR")
    r.add_argument("--shard", help="i/n: every n-th seed of the set from the i-th (the final set over n jobs)")
    r.add_argument("--limit", type=int, help="visit only the first N seeds of the set (the A/A of a pull request)")
    r.add_argument("--published-binary", type=Path, help="use this binary for the published engine instead of fetching it")
    r.add_argument("--published-seal", type=Path, help="with --published-binary: the seal of that engine")
    t = sub.add_parser("touched", help="touched=true|false: does the pull request (BASE_SHA..HEAD) change a file of this oracle")
    t.add_argument("base")
    k = sub.add_parser("clean", help="the published engine's dump on this (clean) machine, for the poisoned run's by-product")
    k.add_argument("--leg", required=True)
    k.add_argument("--out", type=Path, required=True)
    k.add_argument("--engine-tag", default=ENGINE_TAG)
    k.add_argument("--seed-set", choices=SEED_SETS, default=SEED_SET)
    k.add_argument("--shard")
    k.add_argument("--limit", type=int)
    c = sub.add_parser("compare")
    c.add_argument("directory", type=Path)
    c.add_argument("--engine-tag", default=ENGINE_TAG)
    c.add_argument("--seed-set", choices=SEED_SETS, default=SEED_SET)
    c.add_argument("--shard")
    c.add_argument("--limit", type=int)
    c.add_argument("--a-a", action="store_true", help="compare the two dumps of the published engine (the control)")
    args = ap.parse_args(argv)
    if args.cmd == "touched":
        import ci_ab_pr_seeds as pr

        sys.stdout.reconfigure(newline="\n")  # the line goes to $GITHUB_OUTPUT: a carriage return would be part of the value
        print("touched=%s" % ("true" if touched(pr.changed_files(args.base)) else "false"))
        return 0
    if args.cmd == "clean":
        return run_clean(args.leg, args.out, args.engine_tag, select_visits(args.seed_set, args.shard, limit=args.limit))
    if args.cmd == "compare":
        return recompare(args.directory, args.engine_tag, select_visits(args.seed_set, args.shard, limit=args.limit), args.a_a)
    if args.published_seal and not args.published_binary:
        ap.error("--published-seal goes with --published-binary")
    if args.candidate:
        if not args.candidate_seal:
            ap.error("--candidate-seal (or INVISIBLE_SEAL_FILE) is required: the candidate runs under its own seal")
        if args.published_binary and not args.published_seal:
            ap.error("--published-binary and --published-seal go together when there is a candidate")
    elif not args.published_binary:
        ap.error("a candidate, or --published-binary (the A/A of the published engine alone)")
    given = (str(args.published_binary), args.published_seal) if args.published_binary else None
    return run_ab(args.leg, args.out, args.engine_tag, given, args.candidate,
                  Path(args.candidate_seal) if args.candidate else None, select_visits(args.seed_set, args.shard, limit=args.limit))


if __name__ == "__main__":
    sys.exit(main())
