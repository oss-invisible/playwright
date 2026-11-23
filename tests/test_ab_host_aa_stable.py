"""The host oracle judges a path by ONE rule, in the A/A and in the A/B, on Linux and on Windows, over every host path (the system modules and the shortcut inventory included):

  red if and only if a path is in ALL the measured launches (the warm-up and the seeds) of one engine and in NO launch of the other (its long idle launch included);
  everything else that differs is listed, not judged (`report["not_judged"]`, the file `not-judged[-a-a].json` of the artifact), and the paths that are in at least half of the
  launches of one engine and in none of the other's are marked `watch`.

The data are the footprints of real runs of the oracle, reduced to what the rule reads (tests/data/ab_host_aa/*/SOURCES.md): for each launch, which paths it touched, who touched them,
the kinds of process, the pingsenders, the net entries and the shortcut inventory.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ci_ab_host as host  # noqa: E402

pytestmark = pytest.mark.unit

DATA = Path(__file__).resolve().parent / "data" / "ab_host_aa"
SMALL = {"proc": 2, "file": 3}
WINDOWS_RUNS = ("45060573559", "45077433049", "45088980743", "45077330681", "45228081536")
LINUX_RUN = "45088980743-linux"
BS = chr(92)


# -
# The real data.

def rebuild(doc: dict) -> dict:
    """The footprint of the oracle's format from its reduced fixture (a path with the mask of the launches it was in, who touched it, the other fields of each launch)."""
    names = doc["launches"]
    launches = {}
    for i, name in enumerate(names):
        files = [p for p, e in doc["paths"].items() if e["mask"][i] == "1"]
        launch = {"seed": doc["seeds"][name], "steps": [], "files": files, "ours": {}, "problems": [], "spawned_by": {},
                  "touched_by": {p: doc["paths"][p]["by"] for p in files}}
        launch.update(doc["launch"][name])
        launch["inventory"] = launch["inventory"] or []
        launches[name] = launch
    out = {"format": host.FORMAT, "engine": doc["engine"], "platform": doc["platform"], "problems": [], "seeds": [launches[n] for n in names if n.startswith("s")]}
    out.update({k: launches[k] for k in ("warmup", "idle") if k in launches})
    return out


def real(run: str, name: str) -> dict:
    return rebuild(json.loads((DATA / run / ("footprint-%s.json" % name)).read_text(encoding="utf-8")))


def file_leaves(report: dict) -> set:
    return {d["leaf"] for d in report["differences"] if d["category"] == "file"}


@pytest.mark.parametrize("run", WINDOWS_RUNS + (LINUX_RUN,))
@pytest.mark.parametrize("swap", (False, True))
def test_the_a_a_of_a_real_run_has_no_red_path(run, swap):
    first, again = real(run, "68"), real(run, "68-again")
    report = host.judge(*((again, first) if swap else (first, again)), a_a=True)
    assert report["verdict"] == "green", report["problems"][:5]
    assert report["problems"] == [] and report["differences"] == []


@pytest.mark.parametrize("run", ("45077433049", "45088980743"))
def test_the_a_b_of_a_real_run_whose_candidate_is_the_same_engine_has_no_red_path(run):
    first, again, candidate = real(run, "68"), real(run, "68-again"), real(run, "candidate")
    report = host.judge(first, candidate, expected=host.load_expected(), also_68=[again])
    assert file_leaves(report) == set(), sorted(file_leaves(report))[:5]


def test_the_install_folder_of_the_candidate_is_a_constant_difference_and_stays_red_in_the_a_b():
    """Run 45060573559: the candidate was installed in another folder, so the hash of the folder in the names the updater makes (uninstall_ping_<hash>_..., updatelock-<hash>,
    updates\\<hash>) differs in EVERY launch of each side."""
    first, again, candidate = real("45060573559", "68"), real("45060573559", "68-again"), real("45060573559", "candidate")
    report = host.judge(first, candidate, expected=host.load_expected(), also_68=[again])
    assert report["verdict"] == "red"
    hashed = {d["leaf"]: d for d in report["differences"] if d["category"] == "file" and any(
        h in d["leaf"] for h in ("uninstall_ping_", "updatelock-", BS + "updates" + BS, BS + "updates")) and "mozilla" in d["leaf"]}
    only_a = {leaf for leaf, d in hashed.items() if d["a"] == 1 and d["b"] == host.MISSING}
    only_b = {leaf for leaf, d in hashed.items() if d["b"] == 1 and d["a"] == host.MISSING}
    assert len(only_a) >= 17 and len(only_b) >= 17 and only_a | only_b == set(hashed), (len(only_a), len(only_b), len(hashed))
    assert all("4d49b3757c3cfd0c" in leaf for leaf in only_a) and all("1275c47b46d291f6" in leaf for leaf in only_b)


def test_the_declared_difference_of_a_real_linux_run_is_still_declared_and_not_red():
    first, again, candidate = real(LINUX_RUN, "68"), real(LINUX_RUN, "68-again"), real(LINUX_RUN, "candidate")
    report = host.judge(first, candidate, expected=host.load_expected(), also_68=[again])
    assert report["verdict"] == "green", report["problems"]
    assert report["differences"] == [] and report["stale"] == []
    assert {d["leaf"] for d in report["declared"]} >= {"proc.gfxtest"} and {d["id"] for d in report["declared"]} == {"gfxtest-69"}


# -
# The rule on built footprints: six measured launches (the warm-up and five seeds), then the idle launch.

SEEDS = (1, 2, 3, 4, 5)
MEASURED = ("warmup",) + tuple("s%d" % s for s in SEEDS)
LONG = ("idle",)
KINDS = {"parent": 1, "tab/isForBrowser": 2, "gpu": 1}
BASE = ["/etc/fonts/fonts.conf", "/etc/hosts", "/usr/lib/x86_64-linux-gnu/libc.so.6"]
P = "/var/lib/probe/the-path"


def built(engine: str, extra: dict | None = None, by: dict | None = None, kinds: dict | None = None, platform: str = "linux", long_launches: bool = True) -> dict:
    """`extra`: {launch name: [paths added to it]}; `by`: {path: [kinds that touched it]} (default `parent`)."""
    extra, by = extra or {}, by or {}

    def launch(name: str, seed: int) -> dict:
        files = sorted(set(BASE) | set(extra.get(name, [])))
        return {"seed": seed, "steps": [{"n": 1, "name": "open home", "ok": True}], "kinds": dict(kinds or KINDS), "files": files, "ours": {}, "problems": [],
                "touched_by": {p: by.get(p, ["parent"]) for p in files}, "inventory": []}

    out = {"format": host.FORMAT, "engine": engine, "platform": platform, "problems": [],
           "seeds": [launch("s%d" % s, s) for s in SEEDS], "warmup": launch("warmup", SEEDS[0])}
    if long_launches:
        out.update({name: launch(name, SEEDS[0]) for name in LONG})
    return out


def judge(a, b, **kw):
    kw.setdefault("floors", SMALL)
    return host.judge(a, b, **kw)


def listed(report: dict) -> dict:
    return {e["path"]: e for e in report["not_judged"]["paths"]}


def only_in(launches, paths=(P,), *more):
    """{launch: paths} for the launches given; further (launches, paths) pairs are merged in."""
    out = {name: list(paths) for name in launches}
    for names, extra in more:
        for name in names:
            out[name] = out.get(name, []) + list(extra)
    return out


@pytest.mark.parametrize("a_a", (True, False))
def test_a_path_in_every_measured_launch_of_one_engine_and_in_no_launch_of_the_other_is_red(a_a):
    report = judge(built("A", only_in(MEASURED)), built("B"), a_a=a_a, expected=[])
    assert report["verdict"] == "red" and file_leaves(report) == {"file." + P}
    assert P not in listed(report), "a red path is judged, not listed"
    back = judge(built("A"), built("B", only_in(MEASURED)), a_a=a_a, expected=[])
    assert back["verdict"] == "red" and file_leaves(back) == {"file." + P}, "the same the other way round"


def test_a_path_in_five_of_six_measured_launches_and_in_none_of_the_other_is_listed_to_watch_and_not_red():
    report = judge(built("A", only_in(MEASURED[1:])), built("B"), a_a=True)
    assert report["verdict"] == "green", report["problems"]
    entry = listed(report)[P]
    assert entry["watch"] is True and entry["A"]["measured"] == [5, 6] and entry["B"]["measured"] == [0, 6]


def test_a_path_in_every_measured_launch_of_one_engine_and_in_one_launch_of_the_other_is_listed_and_not_red():
    for place in (MEASURED[2], "idle"):
        report = judge(built("A", only_in(MEASURED)), built("B", {place: [P]}), a_a=True)
        assert report["verdict"] == "green", (place, report["problems"])
        entry = listed(report)[P]
        assert entry["watch"] is False and entry["A"]["measured"] == [6, 6], place


def test_the_long_launches_of_the_other_engine_count_as_a_launch_of_it_and_a_path_only_a_long_launch_has_is_only_listed():
    assert judge(built("A", only_in(MEASURED)), built("B", {"idle": [P]}), a_a=True)["verdict"] == "green"
    report = judge(built("A"), built("B", only_in(LONG)), a_a=True)
    assert report["verdict"] == "green" and listed(report)[P]["B"] == {"measured": [0, 6], "long": [1, 1]}
    assert judge(built("A", only_in(LONG)), built("B"), a_a=True)["verdict"] == "green", "what the long launches of one engine alone do is not red either"


def test_watch_is_half_of_the_launches_of_an_engine_and_none_of_the_other():
    four = ("warmup", "s1", "s2", "idle")  # 4 of the 8 launches
    three = ("warmup", "s1", "idle")
    assert listed(judge(built("A", only_in(four)), built("B"), a_a=True))[P]["watch"] is True
    assert listed(judge(built("A", only_in(three)), built("B"), a_a=True))[P]["watch"] is False
    assert listed(judge(built("A", only_in(four)), built("B", {"s5": [P]}), a_a=True))[P]["watch"] is False, "in one launch of the other: not watch"


def test_the_watched_paths_come_first_and_each_entry_names_the_processes_that_touched_the_path():
    a = built("A", only_in(MEASURED[1:], ("/p/watched",), (("s1",), ("/p/once",))), by={"/p/watched": ["gfxtest", "parent"], "/p/once": ["rdd"]})
    report = judge(a, built("B"), a_a=True)
    paths = report["not_judged"]["paths"]
    assert [e["path"] for e in paths] == ["/p/watched", "/p/once"] and [e["watch"] for e in paths] == [True, False]
    assert paths[0]["touched_by"] == {"A": ["gfxtest", "parent"], "B": []} and paths[1]["touched_by"]["A"] == ["rdd"]


def test_there_is_no_cap_of_rare_system_modules_and_a_system_module_is_a_path_like_any_other():
    system = BS.join(("c:", "windows", "system32", "rare%d.dll"))
    many = [system % i for i in range(40)]
    report = judge(built("A", {"s1": many}, platform="win32"), built("B", platform="win32"), a_a=True)
    assert report["verdict"] == "green", report["problems"]
    assert len(report["not_judged"]["paths"]) == 40
    red = judge(built("A", only_in(MEASURED, many[:1]), platform="win32"), built("B", platform="win32"), a_a=True)
    assert red["verdict"] == "red" and file_leaves(red) == {"file." + many[0]}, "in every measured launch and in none of the other: red as any path"


def test_the_shortcut_inventory_is_judged_like_every_other_path():
    walked = BS.join(("c:", "programdata", "microsoft", "windows", "start menu", "desktop.ini"))
    a = built("A", only_in(MEASURED, (walked,)), platform="win32")
    for launch in [a["warmup"]] + a["seeds"]:
        launch["inventory"] = [walked]
    report = judge(a, built("B", platform="win32"), a_a=True)
    assert report["verdict"] == "red" and file_leaves(report) == {"file." + walked}, "the side with the inventory and the side without are no longer left out"
    only_two = built("A", only_in(("warmup", "s1"), (walked,)), platform="win32")
    only_two["warmup"]["inventory"] = only_two["seeds"][0]["inventory"] = [walked]
    assert judge(only_two, built("B", platform="win32"), a_a=True)["verdict"] == "green"


def test_the_pingsender_has_no_treatment_of_its_own_a_path_only_it_touches_is_listed():
    ping = "/var/lib/probe/pingsender-only"
    a = built("A", only_in(("warmup", "s2"), (ping,)), by={ping: ["backgroundtask/pingsender"]})
    report = judge(a, built("B"), a_a=True)
    assert report["verdict"] == "green", report["problems"]
    assert listed(report)[ping]["touched_by"]["A"] == ["backgroundtask/pingsender"]
    both = judge(a, built("B", only_in(("s3",), (ping,)), by={ping: ["backgroundtask/pingsender"]}), a_a=True)
    assert both["verdict"] == "green" and listed(both)[ping]["B"]["measured"] == [1, 6]


# -
# What the rule does not touch.

GFX = "/var/lib/probe/gfxtest-only"


def test_a_difference_the_list_declares_stays_declared_with_its_paths_and_is_not_lost_nor_red():
    kinds = dict(KINDS, gfxtest=1)
    a = built("firefox-68", only_in(MEASURED, (GFX,)), by={GFX: ["gfxtest"]}, kinds=kinds)
    report = judge(a, built("candidate"), expected=host.load_expected())
    assert report["verdict"] == "green", report["problems"]
    assert {d["leaf"] for d in report["declared"]} == {"proc.gfxtest"} | {"file." + GFX} and {d["id"] for d in report["declared"]} == {"gfxtest-69"}
    assert report["differences"] == []
    assert judge(a, built("candidate"), expected=[])["verdict"] == "red", "undeclared it is red"


# -
# The artifact: the list that is not judged is saved next to the verdict.

def fake_engine(root, package, payload=b"engine bytes"):
    folder = Path(root) / package
    (folder / "browser").mkdir(parents=True)
    (folder / "firefox").write_bytes(payload)
    (folder / "omni.ja").write_bytes(payload + b" omni")
    (folder / "browser" / "omni.ja").write_bytes(payload + b" browser omni")
    return str(folder / "firefox")


def run_ab(tmp_path, monkeypatch, docs):
    """`ab` with the three runs given, as the footprints of a Windows runner (no declared difference applies there)."""
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "runner_temp"))
    pub, cand = fake_engine(tmp_path / "fetch", "pub"), fake_engine(tmp_path / "checkout", "cand")
    monkeypatch.setattr(host.oracle, "fetch_published", lambda tag, work: (pub, None))
    names = {"firefox-68": "footprint-68.json", "firefox-68 again": "footprint-68-again.json", "candidate": "footprint-candidate.json"}

    def fake_run(exe, seal, leg, out, label, seeds, crash_with=None):
        host.write_json(tmp_path / "o" / names[label], docs[names[label]])
        return 0

    monkeypatch.setattr(host, "run_subprocess", fake_run)
    monkeypatch.setattr(host, "FLOORS", SMALL)
    monkeypatch.setattr(host, "run_stock_control", lambda leg, out: {"stock": True})
    return host.ab(cand, Path("seal"), "win-x86_64", tmp_path / "o", "firefox-68", list(SEEDS))


def test_the_not_judged_list_is_a_file_of_its_own_next_to_the_verdict_of_the_a_a_and_of_the_a_b(tmp_path, monkeypatch):
    docs = {"footprint-68.json": built("firefox-68", only_in(MEASURED[1:]), platform="win32"), "footprint-68-again.json": built("firefox-68 again", platform="win32"),
            "footprint-candidate.json": built("candidate", platform="win32")}
    assert run_ab(tmp_path, monkeypatch, docs) == 0
    out = tmp_path / "o"
    for name, mode in (("not-judged-a-a.json", "a-a"), ("not-judged.json", "a-b")):
        document = json.loads((out / name).read_text(encoding="utf-8"))
        assert document["format"] == "ab-host-not-judged/1" and document["mode"] == mode and [e["path"] for e in document["paths"]] == [P], name
    for name in ("diff-a-a.json", "diff.json"):
        assert "not_judged" not in json.loads((out / name).read_text(encoding="utf-8")), "the verdict stays small"


def test_the_not_judged_file_has_the_fields_of_each_entry(tmp_path, monkeypatch):
    docs = {"footprint-68.json": built("firefox-68", only_in(MEASURED[1:]), by={P: ["rdd"]}, platform="win32"),
            "footprint-68-again.json": built("firefox-68 again", only_in(MEASURED[1:5]), platform="win32"), "footprint-candidate.json": built("candidate", platform="win32")}
    assert run_ab(tmp_path, monkeypatch, docs) == 0
    a_a = json.loads((tmp_path / "o" / "not-judged-a-a.json").read_text(encoding="utf-8"))
    assert a_a["launches"] == {"A": {"measured": 6, "long": 1}, "B": {"measured": 6, "long": 1}}
    assert a_a["paths"] == [{"path": P, "watch": False, "A": {"measured": [5, 6], "long": [0, 1]}, "B": {"measured": [4, 6], "long": [0, 1]},
                             "touched_by": {"A": ["rdd"], "B": ["parent"]}}]
    a_b = json.loads((tmp_path / "o" / "not-judged.json").read_text(encoding="utf-8"))
    assert a_b["launches"] == {"A": {"measured": 12, "long": 2}, "B": {"measured": 6, "long": 1}}, "the 68 is its two runs"
    assert a_b["paths"] == [{"path": P, "watch": True, "A": {"measured": [9, 12], "long": [0, 2]}, "B": {"measured": [0, 6], "long": [0, 1]},
                             "touched_by": {"A": ["parent", "rdd"], "B": []}}], "9 of the 14 launches of the 68 and none of the candidate's"


def test_the_log_prints_the_not_judged_list_with_the_watched_paths_before_the_others(capsys):
    report = judge(built("A", only_in(MEASURED[1:], ("/p/watched",), (("s1",), ("/p/once",)))), built("B"), a_a=True)
    host.print_report(report, "A", "B")
    lines = capsys.readouterr().out.splitlines()
    summary = next(i for i, line in enumerate(lines) if line.startswith("NOT JUDGED"))
    watch = next(i for i, line in enumerate(lines) if line.startswith("WATCH") and "/p/watched" in line)
    other = next(i for i, line in enumerate(lines) if line.startswith("LISTED") and "/p/once" in line)
    assert summary < watch < other and "2 paths, 1 to watch" in lines[summary]


def test_the_a_a_of_run_45228081536_was_red_on_the_pingsenders_and_is_green_now_that_they_are_listed():
    """Run 45228081536 (oss-invisible/playwright, job 137879971705, `e2e (host, windows)`): the A/A of the 68 was red on two lines, seed 4 side B `2 pingsender processes, not 0 or 3` and
    seed 63 `proc.socket<-pingsender: A 3 | B <missing>`. The pingsenders of the 68 were in all 6 measured launches of the first run and in 4 of the second, the sockets they start in 3 and
    in none: Mozilla's pingsender starts or not from a launch to the next. Now they are listed, not judged."""
    first, again = real("45228081536", "68"), real("45228081536", "68-again")
    assert [s["kinds"].get(host.PINGSENDER_KIND, 0) for s in again["seeds"]] == [3, 0, 2, 0, 3], "the data are the run's: 2 pingsenders in seed 4 of B"
    assert first["seeds"][4]["kinds"]["socket<-pingsender"] == 3 and "socket<-pingsender" not in again["seeds"][4]["kinds"], "seed 63"
    for a, b, mine, other in ((first, again, "A", "B"), (again, first, "B", "A")):
        report = host.judge(a, b, a_a=True)
        assert report["verdict"] == "green", report["problems"]
        assert report["problems"] == [] and report["differences"] == []
        listed = {e["kind"]: e for e in report["not_judged"]["kinds"]}
        assert set(listed) == {host.PINGSENDER_KIND, "socket<-pingsender"}, "the pingsenders and what they start are in the list of what is not judged"
        assert listed[host.PINGSENDER_KIND][mine]["measured"] == [6, 6] and listed[host.PINGSENDER_KIND][other]["measured"] == [4, 6]
        assert listed["socket<-pingsender"][mine]["measured"] == [3, 6] and listed["socket<-pingsender"][other]["measured"] == [0, 6]
        assert listed[host.PINGSENDER_KIND]["spawned_by"][mine] == ["parent"]


def test_the_kinds_of_a_real_run_that_are_there_in_every_launch_of_both_engines_are_not_listed():
    first, again = real("45228081536", "68"), real("45228081536", "68-again")
    report = host.judge(first, again, a_a=True)
    assert {"parent", "rdd", "tab/isForBrowser"} & {e["kind"] for e in report["not_judged"]["kinds"]} == set()
