"""The host-footprint oracle's comparator and readers (scripts/ci_ab_host.py), on synthetic traces, no browser.

The oracle compares the WHOLE TRACE of a closed run, from the launch to the exit of the browser: the processes by kind (with their count, but for the kinds of type
`tab`, whose count follows the idle time and only whose presence counts) and the set of host paths the tree touched (what is `ours` -- its package, its profile and temp
dirs, the run's cache, its own shared memory -- leaves the comparison as a class). Nothing is excused: there is no list of noise.
Each property is shown on the input it exists for: a host path on one side only is red (naming it), a path of ours on one side only is green, a kind with another count
is red, a tab count that differs is green and a tab kind that is absent on one side is red, the one declared difference passes in an A/B by kind, the Windows
processes are named by their command lines from the audit events, and on Linux a netlink socket, a netlink request or a public destination of a kind on one side
only is red while a loopback or lab destination is not compared.
"""
from __future__ import annotations

import copy
import datetime
import inspect
import json
import shutil
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ci_ab_host as host  # noqa: E402

pytestmark = pytest.mark.unit

SMALL = {"proc": 2, "file": 3}
SEEDS = (11, 12)
KINDS = {"parent": 1, "tab/isForBrowser": 2, "gpu": 1, "gfxtest": 1}
FILES = ["/etc/fonts/fonts.conf", "/etc/hosts", "/usr/lib/x86_64-linux-gnu/libc.so.6"]

LIN = host.Context("linux", app=["/srv/lab/runner/.cache/invisible/engines/firefox-68/firefox"],
                   temp=["/tmp/ab-host-tmp-x", "/tmp/ab-host-tmp-x/ab-host-y/seed11"], cache=["/tmp/ab-host-cache-x"])
DEVICES = {"\\Device\\HarddiskVolume3": "C:", "\\Device\\HarddiskVolume5": "D:"}
WIN = host.Context("win32", app=["D:\\lab\\runner\\AppData\\Local\\invisible\\engines\\firefox-68\\firefox"],
                   temp=["D:\\lab\\runner\\AppData\\Local\\Temp\\ab-host-tmp-x"], devices=DEVICES)


def seed_footprint(seed, kinds=None, files=None):
    return {"seed": seed, "steps": [{"n": 1, "name": "open home", "ok": True}],
            "kinds": dict(KINDS if kinds is None else kinds), "files": list(FILES if files is None else files), "ours": {"package": 40},
            "problems": [], "trace": {"source": "strace", "exited": True}}


def footprint(engine="firefox-68", platform="linux", kinds=None, files=None):
    return {"format": host.FORMAT, "engine": engine, "platform": platform, "problems": [],
            "seeds": [seed_footprint(s, copy.deepcopy(kinds), copy.deepcopy(files)) for s in SEEDS]}


def a_candidate_without_gfxtest():
    return footprint("candidate", kinds={k: n for k, n in KINDS.items() if k != "gfxtest"})


def judge(a, b, **kw):
    kw.setdefault("floors", SMALL)
    return host.judge(a, b, **kw)


def expected():
    return host.load_expected()


def fake_engine(root, package, payload=b"engine bytes"):
    """A package of an engine as a folder with a binary, its omni.ja and a link, under `root/package`; returns the path of the binary."""
    folder = Path(root) / package
    (folder / "browser").mkdir(parents=True)
    (folder / "firefox").write_bytes(payload)
    (folder / "omni.ja").write_bytes(payload + b" omni")
    (folder / "browser" / "omni.ja").write_bytes(payload + b" browser omni")
    return str(folder / "firefox")


@pytest.fixture(autouse=True)
def staging_in_the_test_folder(tmp_path, monkeypatch):
    """The engines of an `ab` are staged at `<RUNNER_TEMP>/ab-host/engine/firefox`: here, a folder of the test."""
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "runner_temp"))


# -
# The comparator.

def test_equal_footprints_are_green():
    report = judge(footprint(), footprint(), expected=[], a_a=True)
    assert report["verdict"] == "green" and report["problems"] == [] and report["differences"] == []
    assert set(report["leaves"]) == {"11", "12"}
    assert report["leaves"]["11"] == {"proc": 4, "file": 3, "net": 0}


def test_a_host_path_touched_on_one_side_only_is_red_and_names_the_path_and_both_sides():
    b = footprint(files=FILES + ["/etc/machine-id"])
    report = judge(footprint(), b, a_a=True)
    assert report["verdict"] == "red"
    assert [(d["seed"], d["leaf"], d["a"], d["b"]) for d in report["differences"]] == [
        (11, "file./etc/machine-id", host.MISSING, 1), (12, "file./etc/machine-id", host.MISSING, 1)]
    assert any("seed 11 file./etc/machine-id: A \"<missing>\" | B 1" in p for p in report["problems"])


def test_a_path_the_candidate_no_longer_touches_is_red_too():
    report = judge(footprint(), footprint("candidate", files=FILES[:-1]), a_a=True)
    assert sorted({d["leaf"] for d in report["differences"]}) == ["file." + FILES[-1]]


def test_a_path_of_ours_on_one_side_only_is_green_because_ours_is_not_compared():
    mine = ["/srv/lab/runner/.cache/invisible/engines/firefox-68/firefox/fonts/tahoma7_04.ttf", "/tmp/ab-host-tmp-x/profile/prefs.js",
            "/tmp/ab-host-cache-x/fontconfig/abc.cache-9", "/dev/shm/org.mozilla.ipc.1234.5", "/memfd:mozilla-ipc"]
    a_files, a_ours = host.classify_paths(FILES + mine[:2], LIN)
    b_files, b_ours = host.classify_paths(FILES + mine[2:], LIN)
    assert a_files == b_files == FILES and a_ours != b_ours
    a, b = footprint(files=a_files), footprint(engine="firefox-68 again", files=b_files)
    for fp, ours in ((a, a_ours), (b, b_ours)):
        for seed in fp["seeds"]:
            seed["ours"] = ours
    report = judge(a, b, a_a=True)
    assert report["verdict"] == "green" and report["differences"] == []
    assert report["ours"]["package"] == "reading its own package is not touching the host"


def test_how_many_processes_of_a_kind_a_launch_has_is_not_judged_only_whether_a_kind_is_there():
    """The kinds of process follow the rule of the paths (`file_rule`): a kind is red only when it is in all the measured launches of one engine and in none of the other's."""
    report = judge(footprint(), footprint(kinds=dict(KINDS, gpu=2)), a_a=True)
    assert report["verdict"] == "green" and report["differences"] == [], "gpu is in every launch of both: the count is not compared"
    assert report["not_judged"]["kinds"] == []


def test_a_kind_that_one_engine_has_in_every_measured_launch_and_the_other_in_none_is_red_and_names_the_kind_and_both_sides():
    b = footprint(kinds={k: n for k, n in KINDS.items() if k != "gpu"})
    report = judge(footprint(), b, a_a=True)
    assert report["verdict"] == "red"
    assert [(d["seed"], d["leaf"], d["a"], d["b"]) for d in report["differences"]] == [(11, "proc.gpu", 1, host.MISSING), (12, "proc.gpu", 1, host.MISSING)]
    assert any("seed 12 proc.gpu: A 1 | B \"<missing>\"" in p for p in report["problems"])


def test_a_kind_that_exists_on_one_side_only_is_red():
    b = footprint(kinds=dict(KINDS, rdd=1))
    report = judge(footprint(), b, a_a=True)
    assert sorted({(d["leaf"], d["a"], d["b"]) for d in report["differences"]}) == [("proc.rdd", host.MISSING, 1)]


def test_a_helper_that_one_engine_spawns_in_every_launch_and_the_other_in_none_is_red():
    report = judge(footprint(), footprint(kinds=dict(KINDS, **{"lsb_release": 1})), a_a=True)
    assert {d["leaf"] for d in report["differences"]} == {"proc.lsb_release"}


def six_launches(engine, extra=None, kinds=None):
    """A footprint of six measured launches (the warm-up and five seeds), launch `i` having the kinds `extra[i]` besides KINDS: the shape of a run of the oracle."""
    extra = extra or {}
    fp = footprint(engine, kinds=kinds)
    fp["seeds"] = [seed_footprint(s, dict(KINDS, **extra.get(i + 1, {}))) for i, s in enumerate(range(5))]
    return with_warmup(fp, kinds=dict(KINDS, **extra.get(0, {})), seed=0)


def test_the_separate_pingsender_is_red_in_one_launch_of_six_where_the_rule_of_the_kinds_would_only_list_it():
    """`pingsender[.exe]` is not a kind of the rule (`judged_kinds`): it is held in every launch (`pingsender_problems`), as before."""
    for exe in host.PINGSENDER_EXES:
        a, b = six_launches("A"), six_launches("B", {3: {exe: 1}})
        report = host.judge(a, b, a_a=True, floors=SMALL)
        assert report["verdict"] == "red" and any("the separate %s ran (B266" % exe in p for p in report["problems"])
        assert report["differences"] == [] and all(e["kind"] != exe for e in report["not_judged"]["kinds"])


def test_a_kind_in_six_launches_of_six_of_the_candidate_and_in_none_of_the_68_is_red():
    c = six_launches("candidate", {i: {"rdd": 1} for i in range(6)})
    report = host.judge(six_launches("A"), c, a_a=False, expected=[], also_68=[six_launches("A again")], floors=SMALL)
    assert report["verdict"] == "red" and [d["leaf"] for d in report["differences"]] == ["proc.rdd"] * 5
    assert {(d["a"], d["b"]) for d in report["differences"]} == {(host.MISSING, 1)} and report["not_judged"]["kinds"] == []


def test_a_kind_in_some_launches_of_one_engine_and_in_none_of_the_other_is_listed_not_red():
    c = six_launches("candidate", {0: {"rdd": 1}, 2: {"rdd": 1}, 5: {"rdd": 2}})
    report = host.judge(six_launches("A"), c, a_a=False, expected=[], also_68=[six_launches("A again")], floors=SMALL)
    assert report["verdict"] == "green" and report["differences"] == [], report["problems"]
    (entry,) = report["not_judged"]["kinds"]
    assert entry["kind"] == "rdd" and entry["A"]["measured"] == [0, 12] and entry["B"]["measured"] == [3, 6] and entry["watch"] is True, "in half of the launches of one engine and in none of the other's"
    assert entry["spawned_by"] == {"A": [], "B": []}


def test_the_count_of_the_tab_kind_is_not_compared_and_its_presence_is():
    report = judge(footprint(), footprint(kinds=dict(KINDS, **{"tab/isForBrowser": 9})), a_a=True)
    assert report["verdict"] == "green" and report["differences"] == []
    assert report["tab"]["counts"]["11"] == {"A": {"tab/isForBrowser": 2}, "B": {"tab/isForBrowser": 9}}, "the counts are kept as evidence"
    assert "idle time" in report["tab"]["reason"]
    gone = {k: n for k, n in KINDS.items() if k != "tab/isForBrowser"}
    red = judge(footprint(), footprint(kinds=gone), a_a=True)
    assert red["verdict"] == "red" and {(d["leaf"], d["a"], d["b"]) for d in red["differences"]} == {("proc.tab/isForBrowser", 1, host.MISSING)}


def test_the_content_processes_that_name_themselves_after_a_remote_type_are_of_type_tab():
    names = {"Web Content": 4, "Isolated Web Co": 4, "Privileged Cont": 1, "WebExtensions": 1}
    a, b = footprint(kinds=dict(KINDS, **names)), footprint(kinds=dict(KINDS, **dict(names, **{"Web Content": 5})))
    assert judge(a, b, a_a=True)["verdict"] == "green"
    del b["seeds"][0]["kinds"]["WebExtensions"]
    assert judge(a, b, a_a=True)["verdict"] == "green", "gone from one launch of B: not in all the launches of A and none of B's"
    del b["seeds"][1]["kinds"]["WebExtensions"]
    assert {d["leaf"] for d in judge(a, b, a_a=True)["differences"]} == {"proc.WebExtensions"}
    assert [host.is_tab(k) for k in ("tab", "tab/isForBrowser", "Web Content", "gpu", "utility/sandboxingKind=1", "fork<-forkserver")] == [
        True, True, True, False, False, False]


def test_a_footprint_below_the_floor_is_red():
    thin = footprint(files=FILES[:1])
    report = host.judge(thin, thin, a_a=True, floors={"proc": 2, "file": 3})
    assert report["verdict"] == "red" and report["differences"] == []
    assert any("seed 11 side A has 1 file leaves, fewer than the floor of 3" in p for p in report["problems"])


def test_the_real_floors_are_not_met_by_a_toy_footprint():
    assert host.judge(footprint(), footprint(), a_a=True)["verdict"] == "red"


def test_a_missing_seed_is_red():
    b = footprint()
    b["seeds"].pop()
    report = judge(footprint(), b, a_a=True)
    assert report["verdict"] == "red" and any("different seeds" in p for p in report["problems"])


def test_a_trace_that_could_not_be_taken_is_red_even_when_both_sides_say_it():
    a = footprint()
    a["seeds"][0]["problems"] = ["the tree did not exit within 120 s of the close: the trace is cut short"]
    report = judge(a, copy.deepcopy(a), a_a=True)
    assert report["verdict"] == "red" and any("seed 11 side A: the tree did not exit" in p for p in report["problems"])


def test_a_journey_step_that_failed_is_red():
    a = footprint()
    a["problems"] = ["seed 11: step 3 (scroll) failed: timeout"]
    report = judge(a, footprint(), a_a=True)
    assert report["verdict"] == "red" and "side A: seed 11: step 3" in report["problems"][0]


def test_nothing_is_excused_there_is_no_list_of_noise():
    assert not hasattr(host, "load_volatile") and not (Path(__file__).resolve().parents[1] / "ci" / "ab-host-volatile.json").exists()
    assert "excused" not in judge(footprint(), footprint(), a_a=True)


# -
# The declared differences.

def test_the_declared_difference_is_the_gfxtest_of_linux_by_kind_and_there_is_one_only():
    entries = expected()
    assert [(e["id"], e["os"], e["category"], e["name"]) for e in entries] == [("gfxtest-69", "linux", "proc", "gfxtest")]
    assert "0e896e4ee1" in entries[0]["reason"] and "GfxInfo.cpp:55" in entries[0]["reason"]
    assert len(entries) <= 1, "the list of declared differences may only shrink"


def test_the_declared_gfxtest_difference_passes_in_an_a_b():
    report = judge(footprint(), a_candidate_without_gfxtest(), expected=expected())
    assert report["verdict"] == "green", report["problems"]
    assert [d["leaf"] for d in report["declared"]] == ["proc.gfxtest", "proc.gfxtest"] and report["stale"] == []


def test_a_difference_next_to_the_declared_one_is_still_red():
    c = a_candidate_without_gfxtest()
    for seed in c["seeds"]:
        seed["kinds"]["utility/sandboxingKind=1"] = 1
    report = judge(footprint(), c, expected=expected())
    assert report["verdict"] == "red"
    assert [d["leaf"] for d in report["differences"]] == ["proc.utility/sandboxingKind=1"] * len(SEEDS)


def test_the_declared_difference_the_other_way_round_is_red():
    report = judge(a_candidate_without_gfxtest(), footprint(), expected=expected())
    assert report["verdict"] == "red" and any(d["leaf"] == "proc.gfxtest" for d in report["differences"])


def test_a_declared_difference_that_does_not_show_is_red_as_stale():
    report = judge(footprint(), footprint("candidate"), expected=expected())
    assert report["verdict"] == "red" and report["stale"] == ["gfxtest-69"]
    assert any("stale" in p and "gfxtest-69" in p for p in report["problems"])


def test_a_declared_difference_of_another_os_is_not_applied_and_not_stale_there():
    report = judge(footprint(platform="win32"), footprint("candidate", platform="win32"), expected=expected())
    assert report["verdict"] == "green", report["problems"]


def test_in_the_a_a_the_declared_list_is_not_applied():
    report = judge(footprint(), a_candidate_without_gfxtest(), expected=expected(), a_a=True)
    assert report["verdict"] == "red" and report["declared"] == [] and report["stale"] == []
    assert [d["leaf"] for d in report["differences"]] == ["proc.gfxtest", "proc.gfxtest"]


def test_the_declared_gfxtest_does_not_cover_a_path():
    c = a_candidate_without_gfxtest()
    for seed in c["seeds"]:
        seed["files"] = FILES + ["/usr/lib/x86_64-linux-gnu/libglx.so.0"]
    report = judge(footprint(), c, expected=expected())
    assert report["verdict"] == "red" and {d["leaf"] for d in report["differences"]} == {"file./usr/lib/x86_64-linux-gnu/libglx.so.0"}


def test_the_cli_says_broken_control_for_an_a_a_difference(tmp_path, capsys):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    for path, doc in ((a, footprint()), (b, a_candidate_without_gfxtest())):
        path.write_text(json.dumps(doc), encoding="utf-8")
    assert host.main(["compare", str(a), str(b), "--a-a"]) == 2
    out = capsys.readouterr().out
    assert "AB HOST (a-a) FAILED" in out and "OURS (not compared)" in out


def test_each_footprint_run_has_a_cache_folder_and_a_temp_root_of_its_own_that_are_gone_when_it_ends(tmp_path, monkeypatch):
    """The launches leave nothing in the user's folders (the fontconfig test of the same job needs its first launch cold), and what the engine makes in the
    run's own folders is `ours`."""
    seen = []

    class Done:
        returncode = 0

    original = host.oracle.own_cache_env
    monkeypatch.setattr(host.oracle, "own_cache_env", lambda env, folder: original(env, folder, platform="linux"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "user"))
    monkeypatch.setattr(host.subprocess, "run", lambda cmd, env, timeout: seen.append(
        (env["XDG_CACHE_HOME"], env["TMPDIR"], env["TEMP"], Path(env["XDG_CACHE_HOME"]).is_dir(), Path(env["TMPDIR"]).is_dir())) or Done())
    assert host.run_subprocess("ff", None, "linux-x86_64", tmp_path / "f.json", "firefox-68", [1]) == 0
    assert host.run_subprocess("ff", None, "linux-x86_64", tmp_path / "f.json", "firefox-68 again", [1]) == 0
    (cache1, tmp1, temp1, there1, tmp_there1), (cache2, tmp2, _t, there2, tmp_there2) = seen
    assert there1 and there2 and tmp_there1 and tmp_there2 and cache1 != cache2 and tmp1 != tmp2 and tmp1 == temp1
    assert str(tmp_path / "user") not in (cache1, cache2)
    assert not Path(cache1).exists() and not Path(tmp1).exists() and not Path(cache2).exists() and not Path(tmp2).exists()


def test_the_seeds_are_the_oracles_when_the_file_names_none(tmp_path, monkeypatch):
    monkeypatch.setattr(host, "SEEDS_FILE", tmp_path / "nope.json")
    assert host.default_seeds() == list(host.gate.SEEDS)
    f = tmp_path / "persona-seeds.json"
    f.write_text(json.dumps({"candidate": [5, 6]}), encoding="utf-8")
    monkeypatch.setattr(host, "SEEDS_FILE", f)
    assert host.default_seeds() == [5, 6]


# -
# Kinds.

@pytest.mark.parametrize("cmdline,kind", [
    (["firefox"], "parent"),
    (["C:\\app\\firefox.exe", "-contentproc", "-isForBrowser", "-prefsHandle", "1", "tab"], "tab/isForBrowser"),
    (["firefox", "-contentproc", "-notForBrowser", "-prefsLen", "5", "tab"], "tab/notForBrowser"),
    (["firefox", "-contentproc", "-sandboxingKind", "1", "-prefsLen", "5", "utility"], "utility/sandboxingKind=1"),
    (["firefox", "-contentproc", "-prefsLen", "5", '"GPU"'], "gpu"),
    (["firefox", "-contentproc", "forkserver"], "forkserver"),
])
def test_the_kind_of_a_process_is_the_last_argument_with_its_sandbox_and_browser_flags(cmdline, kind):
    assert host.kind_of(cmdline) == kind


# -
# What is ours and what is the host's.

@pytest.mark.parametrize("path,got", [
    ("/srv/lab/runner/.cache/invisible/engines/firefox-68/firefox/libxul.so", ("package", "libxul.so")),
    ("/srv/lab/runner/.cache/invisible/engines/firefox-68/firefox/gmp-clearkey/0.1/libclearkey.so", ("package", "gmp-clearkey/0.1/libclearkey.so")),
    ("/tmp/ab-host-tmp-x/tmpabc123/profile/startupCache/scriptCache.bin", ("profile", "tmpabc123/profile/startupCache/scriptCache.bin")),
    ("/tmp/ab-host-tmp-x/ab-host-y/seed11/engine/firefox", ("profile", "ab-host-y/seed11/engine/firefox")),
    ("/tmp/ab-host-cache-x/fontconfig/abc-le64.cache-9", ("cache", "fontconfig/abc-le64.cache-9")),
    ("/dev/shm/org.mozilla.ipc.1234.5", ("ipc", "/dev/shm/org.mozilla.ipc.1234.5")),
    ("/memfd:mozilla-ipc", ("ipc", "/memfd:mozilla-ipc")),
    ("/usr/lib/x86_64-linux-gnu/libc.so.6", ("host", "/usr/lib/x86_64-linux-gnu/libc.so.6")),
    ("/etc/fonts/fonts.conf", ("host", "/etc/fonts/fonts.conf")),
    ("/srv/lab/runner/.config/fontconfig/fonts.conf", ("host", "/srv/lab/runner/.config/fontconfig/fonts.conf")),
    ("/tmp/somebody-elses-file", ("host", "/tmp/somebody-elses-file")),
    ("/tmp/ab-host-tmp-xyz/other", ("host", "/tmp/ab-host-tmp-xyz/other")),
    ("/proc/4242/maps", ("host", "/proc/N/maps")),
    ("/proc/4242/task/4250/comm", ("host", "/proc/N/task/N/comm")),
    ("/proc/self/exe", ("host", "/proc/self/exe")),
    ("/proc/sys/kernel/random/boot_id", ("host", "/proc/sys/kernel/random/boot_id")),
    ("/run/user/1001/dconf/user", ("host", "/run/user/N/dconf/user")),
    ("<cwd>/fonts.conf", ("host", "<cwd>/fonts.conf")),
    ("", None),
])
def test_a_linux_path_is_ours_or_the_hosts_and_the_hosts_is_spelled_the_same_on_every_run(path, got):
    assert host.classify(path, LIN) == got


@pytest.mark.parametrize("path,got", [
    ("\\Device\\HarddiskVolume5\\lab\\runner\\AppData\\Local\\invisible\\engines\\firefox-68\\firefox\\xul.dll", ("package", "xul.dll")),
    ("d:\\LAB\\runner\\appdata\\local\\invisible\\engines\\firefox-68\\firefox\\gmp-clearkey\\0.1\\clearkey.dll", ("package", "gmp-clearkey/0.1/clearkey.dll")),
    ("D:\\lab\\runner\\AppData\\Local\\Temp\\ab-host-tmp-x\\tmpab12cd\\profile\\cache2\\x", ("profile", "tmpab12cd/profile/cache2/x")),
    ("\\Device\\HarddiskVolume3\\Windows\\System32\\KERNEL32.DLL", ("host", "c:\\windows\\system32\\kernel32.dll")),
    ("\\??\\C:\\Windows\\Fonts\\arial.ttf", ("host", "c:\\windows\\fonts\\arial.ttf")),
    ("C:\\Windows\\WinSxS\\amd64_microsoft.windows.common-controls_6595b64144ccf1df_6.0.26100.1_none_3e0e6d3c1d0f1b2c\\comctl32.dll",
     ("host", "c:\\windows\\winsxs\\amd64_microsoft.windows.common-controls_6595b64144ccf1df_6.0.26100.1_none_3e0e6d3c1d0f1b2c\\comctl32.dll")),
    ("D:\\lab\\runner\\AppData\\Roaming\\Mozilla\\x.dat", ("host", "d:\\lab\\runner\\appdata\\roaming\\mozilla\\x.dat")),
    ("D:\\lab\\runner\\AppData\\Local\\Temp\\somebody-elses.tmp", ("host", "d:\\lab\\runner\\appdata\\local\\temp\\somebody-elses.tmp")),
    ("\\Device\\NamedPipe\\gecko-crash-server-pipe.4242", ("ipc", "\\device\\namedpipe\\gecko-crash-server-pipe.4242")),
    ("\\Device\\NamedPipe\\lsass", ("host", "\\device\\namedpipe\\lsass")),
    ("\\Device\\HarddiskVolume9\\x\\y", ("host", "\\device\\harddiskvolume9\\x\\y")),
    ("C:\\Program Files\\Some Vendor\\", ("host", "c:\\program files\\some vendor")),
])
def test_a_windows_path_is_ours_or_the_hosts_and_the_hosts_is_spelled_the_same_on_every_run(path, got):
    assert host.classify(path, WIN) == got


def test_the_host_paths_are_a_sorted_set_and_ours_is_counted_by_class():
    paths = ["/etc/hosts", "/etc/hosts", "/proc/1/maps", "/proc/2/maps", "/srv/lab/runner/.cache/invisible/engines/firefox-68/firefox/libxul.so",
             "/dev/shm/org.mozilla.ipc.1.2", "/dev/shm/org.mozilla.ipc.3.4", "/tmp/ab-host-tmp-x/p"]
    assert host.classify_paths(paths, LIN) == (["/etc/hosts", "/proc/N/maps"], {"ipc": 2, "package": 1, "profile": 1})
    assert host.classify_paths(["C:\\Windows\\a.dll", "c:\\windows\\A.DLL"], WIN) == (["c:\\windows\\a.dll"], {})


# -
# The strace log: the processes the tree made, by what they ran, and the paths it touched.

REAL = "/e/firefox/firefox"
LOG = """\
100 execve("/e/firefox/firefox", ["/e/firefox/firefox", "-profile", "/tmp/p"], 0x7ffd /* 40 vars */) = 0
100 clone(child_stack=NULL, flags=CLONE_CHILD_CLEARTID|CLONE_CHILD_SETTID|SIGCHLD, child_tidptr=0x7f) = 101
101 prctl(PR_SET_NAME, "glxtest") = 0
100 clone3({flags=CLONE_VM|CLONE_FS|CLONE_FILES|CLONE_SIGHAND|CLONE_THREAD|CLONE_SYSVSEM, exit_signal=0, stack=0x7f}, 88) = 102
102 prctl(PR_SET_NAME, "Socket Thread") = 0
100 fork() = 103
103 execve("/proc/self/exe", ["/e/firefox/firefox", "-contentproc", "-isForBrowser", "-prefsLen", "1", "tab"], 0x7ffd /* 40 vars */) = 0
100 clone(child_stack=NULL, flags=CLONE_CHILD_CLEARTID|CLONE_CHILD_SETTID|SIGCHLD <unfinished ...>
100 <... clone resumed>, child_tidptr=0x7f) = 104
104 execve("/usr/bin/lsb_release", ["lsb_release", "-a"], 0x7ffd /* 40 vars */) = 0
100 clone(child_stack=NULL, flags=CLONE_CHILD_CLEARTID|CLONE_CHILD_SETTID|SIGCHLD, child_tidptr=0x7f) = 105
105 prctl(PR_SET_NAME, "Forked Thing") = 0
105 execve("/e/firefox/firefox", ["/e/firefox/firefox", "-contentproc", "-sandboxingKind", "1", "utility"], 0x7ffd) = 0
104 +++ exited with 0 +++
"""


def test_the_processes_the_tree_made_are_named_by_what_they_ran_and_threads_are_not_processes():
    procs = host.spawned_processes(LOG.splitlines(), REAL)
    assert {pid: p["label"] for pid, p in procs.items()} == {
        100: "parent", 101: "glxtest", 103: "tab/isForBrowser", 104: "lsb_release", 105: "utility/sandboxingKind=1"}
    assert host.spawned_counts(procs) == {"glxtest": 1, "lsb_release": 1, "parent": 1, "tab/isForBrowser": 1, "utility/sandboxingKind=1": 1}


def test_a_fork_with_no_exec_and_no_name_is_called_after_its_parent():
    lines = ['7 execve("/e/firefox/firefox", ["/e/firefox/firefox", "-contentproc", "forkserver"], 0x1) = 0',
             "7 fork() = 8", "8 getpid() = 8"]
    assert host.spawned_counts(host.spawned_processes(lines, REAL)) == {"fork<-forkserver": 1, "forkserver": 1}


def test_a_child_that_names_itself_before_its_parent_has_the_result_of_the_clone_is_still_named():
    lines = ['7 execve("/e/firefox/firefox", ["/e/firefox/firefox"], 0x1) = 0',
             '9 prctl(PR_SET_NAME, "glxtest") = 0',
             "7 clone(child_stack=NULL, flags=SIGCHLD) = 9"]
    assert host.spawned_counts(host.spawned_processes(lines, REAL)) == {"glxtest": 1, "parent": 1}


def test_a_failed_exec_does_not_change_what_a_process_is():
    lines = ['7 execve("/e/firefox/firefox", ["/e/firefox/firefox"], 0x1) = 0',
             "7 clone(child_stack=NULL, flags=SIGCHLD) = 9",
             '9 execve("/usr/bin/nope", ["nope"], 0x1) = -1 ENOENT (No such file or directory)']
    assert host.spawned_counts(host.spawned_processes(lines, REAL)) == {"fork<-parent": 1, "parent": 1}


def test_the_forkserver_children_named_after_a_remote_type_are_counted_by_name():
    lines = ['7 execve("/e/firefox/firefox", ["/e/firefox/firefox", "-contentproc", "forkserver"], 0x1) = 0']
    for pid, name in ((8, "Web Content"), (9, "Web Content"), (10, "Isolated Web Co"), (11, "GPU Process")):
        lines += ["7 fork() = %d" % pid, '%d prctl(PR_SET_NAME, "%s") = 0' % (pid, name)]
    counts = host.spawned_counts(host.spawned_processes(lines, REAL))
    assert counts == {"forkserver": 1, "Web Content": 2, "Isolated Web Co": 1, "GPU Process": 1}
    assert {k for k in counts if host.is_tab(k)} == {"Web Content", "Isolated Web Co"}


FILE_LOG = """\
100 execve("/e/firefox/firefox", ["/e/firefox/firefox"], 0x7ffd /* 40 vars */) = 0
100 openat(AT_FDCWD, "/etc/ld.so.cache", O_RDONLY|O_CLOEXEC) = 3</etc/ld.so.cache>
100 openat(AT_FDCWD, "/usr/lib/libmissing.so", O_RDONLY|O_CLOEXEC) = -1 ENOENT (No such file or directory)
100 openat(3</usr/share/fonts>, "a.ttf", O_RDONLY) = -1 ENOENT (No such file or directory)
100 openat(7, "orphan", O_RDONLY) = 8
100 open("/etc/hosts", O_RDONLY) = 9</etc/hosts>
101 stat("/etc/localtime", {st_mode=S_IFREG|0644, st_size=2945, ...}) = 0
101 lstat("/srv/lab/runner/.config", 0x7ffe) = -1 ENOENT (No such file or directory)
101 newfstatat(AT_FDCWD, "/proc/4242/status", {st_mode=S_IFREG|0444}, 0) = 0
101 newfstatat(4</proc/4242>, "", {st_mode=S_IFDIR|0555}, AT_EMPTY_PATH) = 0
101 statx(AT_FDCWD, "fonts.conf", AT_STATX_SYNC_AS_STAT, STATX_ALL, 0x7ffe) = 0
102 access("/etc/fonts/\\303\\250.conf", R_OK) = 0
102 readlink("/proc/self/exe", "/e/firefox/firefox", 4096) = 18
102 faccessat2(AT_FDCWD, "/sys/devices/system/cpu/online", R_OK, 0) = 0
102 openat(AT_FDCWD, "/late/path", O_RDONLY <unfinished ...>
102 <... openat resumed>) = 5</late/path>
102 execve("/usr/bin/lsb_release", ["lsb_release", "-a"], 0x7ffd /* 40 vars */) = 0
102 prctl(PR_SET_NAME, "/not/a/path") = 0
102 read(3, "/not/a/path/either", 10) = 10
"""


def test_every_path_the_tree_touched_is_collected_failed_attempts_included_and_relative_ones_are_joined_to_their_directory():
    assert host.touched_paths(FILE_LOG.splitlines()) == {
        "/e/firefox/firefox", "/etc/ld.so.cache", "/usr/lib/libmissing.so", "/usr/share/fonts/a.ttf", "<fd>/orphan", "/etc/hosts", "/etc/localtime",
        "/srv/lab/runner/.config", "/proc/4242/status", "<cwd>/fonts.conf", "/etc/fonts/\u00e8.conf", "/proc/self/exe", "/sys/devices/system/cpu/online",
        "/late/path", "/usr/bin/lsb_release"}


def test_a_trace_is_the_whole_log_so_a_path_touched_late_by_a_short_lived_child_is_in_it():
    late = FILE_LOG + '105 openat(AT_FDCWD, "/etc/machine-id", O_RDONLY) = 3\n'
    assert host.touched_paths(late.splitlines()) - host.touched_paths(FILE_LOG.splitlines()) == {"/etc/machine-id"}


def test_a_closed_linux_run_makes_a_footprint_of_its_kinds_and_its_host_paths(tmp_path, monkeypatch):
    log = tmp_path / "strace.log"
    files = FILE_LOG.replace("/etc/hosts", "/e/firefox/libxul.so").replace("102 execve", "104 execve")
    log.write_text(LOG + files + '101 openat(AT_FDCWD, "/tmp/ab-host-tmp-x/p/prefs.js", O_RDONLY) = 3\n', encoding="utf-8")
    monkeypatch.setattr(host.os.path, "realpath", lambda p: p)
    monkeypatch.setattr(host, "_context", lambda binary, windows, work: host.Context("linux", app=["/e/firefox"], temp=["/tmp/ab-host-tmp-x"]))
    fp = host._seed_result(REAL, 11, tmp_path, [], log, None, None, False)
    assert fp["kinds"] == {"glxtest": 1, "lsb_release": 1, "parent": 1, "tab/isForBrowser": 1, "utility/sandboxingKind=1": 1}
    assert "/etc/hosts" not in fp["files"] and "/etc/localtime" in fp["files"] and "/proc/N/status" in fp["files"] and "/e/firefox/libxul.so" not in fp["files"]
    assert fp["ours"] == {"package": 2, "profile": 1} and fp["trace"]["source"] == "strace" and fp["problems"] == []
    assert "tab/isForBrowser" in host.judged_kinds(fp) and not any(leaf.startswith(("file.", "proc.")) for leaf in host.leaves_of(fp)), "the host paths and the kinds are judged over all the launches"


def test_the_strace_stand_in_prints_the_paths_of_the_descriptors_only_when_asked(monkeypatch):
    import _strace_stand_in as stand_in

    class Help:
        stdout = "strace --kill-on-exit"

    monkeypatch.setattr(stand_in.shutil, "which", lambda name: "/usr/bin/strace")
    monkeypatch.setattr(stand_in.subprocess, "run", lambda *a, **k: Help())
    assert " -y" not in stand_in._strace_options() and " -y" in stand_in._strace_options(fd_paths=True)
    calls = host.STRACE_CALLS.split(",")
    assert all(call and " " not in call for call in calls)
    assert {"execve", "openat", "stat", "access", "readlink", "clone3", "prctl"} <= set(calls)


# -
# The exit of a closed run.

def test_a_run_is_quiet_when_its_tree_is_gone_and_two_reads_say_the_same():
    trees = iter([[{"pid": 1}], [{"pid": 1}, {"pid": 2}], [{"pid": 1}, {"pid": 2}], [{"pid": 1}, {"pid": 2}]])
    gone = {1, 2}
    now = iter(range(1000))
    tree, info = host.wait_exited(lambda: next(trees), lambda pid: pid not in gone, sleep=lambda s: None, now=lambda: next(now))
    assert [e["pid"] for e in tree] == [1, 2] and info == {"exited": True, "reads": 3}


def test_a_process_that_is_still_alive_is_not_an_exit_and_the_limit_says_so():
    now = iter(range(0, 10 ** 6, 10))
    tree, info = host.wait_exited(lambda: [{"pid": 1}], lambda pid: True, limit_s=100, sleep=lambda s: None, now=lambda: next(now))
    assert info["exited"] is False and [e["pid"] for e in tree] == [1]


def test_a_tree_that_keeps_growing_after_every_process_is_gone_is_read_again():
    reads = iter([[{"pid": 1}], [{"pid": 1}, {"pid": 2}], [{"pid": 1}, {"pid": 2}]])
    now = iter(range(1000))
    _tree, info = host.wait_exited(lambda: next(reads), lambda pid: False, sleep=lambda s: None, now=lambda: next(now))
    assert info == {"exited": True, "reads": 3}


# A pid is not a process: Windows hands the pid of a process that ended to the next one it makes (run 45334478026: pid 7352 of the tree ended at 05:05:33 and was
# `TiWorker.exe`, a service that lives minutes, at 05:05:38; the wait for the exit saw "alive" for 120 s and the A/A was red).
BIRTH = "2026-11-24T05:05:20.5000000Z"
BIRTH_EPOCH = datetime.datetime(2026, 11, 24, 5, 5, 20, 500000, tzinfo=datetime.timezone.utc).timestamp()


def _exit_with(tree, ends, create_time=None, alive=lambda pid: True):
    now = iter(range(0, 10 ** 6, 10))
    return host.wait_exited(lambda: tree, alive, limit_s=100, sleep=lambda s: None, now=lambda: next(now), read_ends=lambda: ends, create_time=create_time)


def test_a_pid_that_ended_in_the_audit_and_was_handed_to_another_process_is_not_the_tree_alive():
    tree = [{"rid": 10, "pid": 7352, "time": BIRTH}, {"rid": 11, "pid": 7360, "time": BIRTH}]
    ends = [{"rid": 40, "pid": 7352, "time": "2026-11-24T05:05:33.1800000Z"}, {"rid": 41, "pid": 7360, "time": "2026-11-24T05:05:33.2000000Z"}]
    # `pid_exists` says yes for both (TiWorker has 7352 now), and the process that has it was created at 05:05:38, long after the birth
    _tree, info = _exit_with(tree, ends, create_time=lambda pid: BIRTH_EPOCH + 18.1)
    assert info == {"exited": True, "reads": 2}


def test_an_end_of_the_pid_before_its_birth_is_not_the_end_of_that_process():
    # the 4689 of an earlier process that had the pid (record 5 < 10): the process born at record 10 is still there
    tree = [{"rid": 10, "pid": 7352, "time": BIRTH}]
    _tree, info = _exit_with(tree, [{"rid": 5, "pid": 7352, "time": "2026-11-24T05:00:00.0000000Z"}], create_time=lambda pid: BIRTH_EPOCH)
    assert info["exited"] is False


def test_a_process_with_no_end_and_the_creation_time_of_its_birth_is_still_alive():
    tree = [{"rid": 10, "pid": 7352, "time": BIRTH}]
    _tree, info = _exit_with(tree, [], create_time=lambda pid: BIRTH_EPOCH - 0.01)
    assert info["exited"] is False


def test_a_pid_with_no_audit_end_and_a_later_creation_time_is_another_process():
    tree = [{"rid": 10, "pid": 7352, "time": BIRTH}]
    _tree, info = _exit_with(tree, [], create_time=lambda pid: BIRTH_EPOCH + 18.1)
    assert info == {"exited": True, "reads": 2}


def test_a_pid_that_nobody_has_is_gone_and_a_process_that_cannot_be_read_is_not_made_to_look_ended():
    tree = [{"rid": 10, "pid": 7352, "time": BIRTH}]
    assert _exit_with(tree, [], alive=lambda pid: False)[1]["exited"] is True

    def denied(pid):
        raise PermissionError(pid)

    assert _exit_with(tree, [], create_time=denied)[1]["exited"] is False
    assert _exit_with(tree, [], create_time=lambda pid: None)[1]["exited"] is False  # no process has it: `alive` is the one that says so


def test_the_audit_time_is_read_as_utc_to_the_microsecond():
    assert host.audit_epoch(BIRTH) == BIRTH_EPOCH and host.audit_epoch("") is None and host.audit_epoch("not a time") is None


def test_the_decision_reads_the_end_by_time_when_the_records_have_no_id():
    event = {"pid": 7, "time": BIRTH}
    assert host.process_has_ended(event, [{"pid": 7, "time": "2026-11-24T05:05:33.0000000Z"}]) is True
    assert host.process_has_ended(event, [{"pid": 7, "time": "2026-11-24T05:00:00.0000000Z"}]) is False
    assert host.process_has_ended({"pid": 7}, []) is False


# -
# Windows: the processes with their command lines, from the process-creation audit.

@pytest.mark.parametrize("text,args", [
    ('"C:\\Program Files\\app\\firefox.exe" -contentproc --channel="1234.5.678" -prefsHandle 9 tab',
     ["C:\\Program Files\\app\\firefox.exe", "-contentproc", "--channel=1234.5.678", "-prefsHandle", "9", "tab"]),
    ("firefox.exe  -a   -b", ["firefox.exe", "-a", "-b"]),
    ('x.exe "a b\\\\" c', ["x.exe", "a b\\", "c"]),
    ('x.exe "say \\"hi\\"" y', ["x.exe", 'say "hi"', "y"]),
    ('x.exe "" z', ["x.exe", "", "z"]),
    ("", []),
])
def test_a_windows_command_line_is_split_as_the_system_splits_it(text, args):
    assert host.split_command_line(text) == args


NS = 'xmlns="http://schemas.microsoft.com/win/2004/08/events/event"'


def audit(rid, pid, ppid, image, cmd):
    return ('<Event %s><System><Provider Name="Microsoft-Windows-Security-Auditing"/><EventID>4688</EventID><EventRecordID>%d</EventRecordID></System>'
            '<EventData><Data Name="SubjectUserSid">S-1-5-18</Data><Data Name="NewProcessId">0x%x</Data><Data Name="NewProcessName">%s</Data>'
            '<Data Name="ProcessId">0x%x</Data><Data Name="CommandLine">%s</Data></EventData></Event>'
            % (NS, rid, pid, image, ppid, cmd.replace("&", "&amp;").replace('"', "&quot;")))


FIREFOX = "C:\\lab\\ff\\firefox.exe"
TREE = [
    audit(10, 100, 1, FIREFOX, '"%s" -profile "D:\\t\\p"' % FIREFOX),
    audit(11, 101, 100, "C:\\Windows\\System32\\conhost.exe", "conhost.exe 0xffffffff"),
    audit(12, 102, 100, FIREFOX, '"%s" -contentproc --channel="100.1.2" -isForBrowser -prefsHandle 9 tab' % FIREFOX),
    audit(13, 103, 100, FIREFOX, '"%s" -contentproc --channel="100.3.4" -sandboxingKind 1 -notForBrowser utility' % FIREFOX),
    audit(14, 104, 102, FIREFOX, '"%s" -contentproc --channel="100.5.6" -isForBrowser -prefsHandle 9 tab' % FIREFOX),
    audit(15, 105, 999, "C:\\Windows\\notepad.exe", "notepad.exe"),
    audit(16, 106, 3, "C:\\Windows\\System32\\svchost.exe", "svchost.exe -k x"),
    audit(17, 107, 100, "C:\\lab\\ff\\pingsender.exe", '"C:\\lab\\ff\\pingsender.exe" https://t.example/x'),
]


def test_the_audit_events_are_read_with_their_creator_and_their_command_line():
    events = host.parse_audit_events("\n".join(TREE))
    assert [(e["rid"], e["pid"], e["ppid"]) for e in events][:3] == [(10, 100, 1), (11, 101, 100), (12, 102, 100)]
    assert events[2]["image"] == FIREFOX and events[2]["cmd"].endswith("tab") and "&" not in events[2]["cmd"]
    assert host.parse_audit_events("") == []
    assert host.parse_audit_events('<?xml version="1.0"?>' + TREE[0])[0]["pid"] == 100


def test_the_tree_of_the_launch_is_what_comes_from_its_root_by_the_creators_pid_even_when_the_creator_is_gone():
    events = host.parse_audit_events("\n".join(TREE))
    assert [e["pid"] for e in host.process_tree(events, {100})] == [100, 101, 102, 103, 104, 107]
    assert [e["pid"] for e in host.process_tree(events, {102})] == [102, 104]


def test_the_windows_children_are_named_by_kind_from_their_command_lines_the_short_lived_ones_too():
    tree = host.process_tree(host.parse_audit_events("\n".join(TREE)), {100})
    procs = host.windows_processes(tree, FIREFOX)
    assert {pid: p["label"] for pid, p in procs.items()} == {
        100: "parent", 101: "conhost.exe", 102: "tab/isForBrowser", 103: "utility/sandboxingKind=1/notForBrowser", 104: "tab/isForBrowser",
        107: "pingsender.exe"}
    counts = host.spawned_counts(procs)
    assert counts == {"conhost.exe": 1, "parent": 1, "pingsender.exe": 1, "tab/isForBrowser": 2, "utility/sandboxingKind=1/notForBrowser": 1}


def test_a_windows_kind_that_is_gone_from_the_candidate_is_red_by_its_command_line():
    tree = host.process_tree(host.parse_audit_events("\n".join(TREE)), {100})
    kinds = host.spawned_counts(host.windows_processes(tree, FIREFOX))
    without = {k: n for k, n in kinds.items() if k != "utility/sandboxingKind=1/notForBrowser"}
    report = judge(footprint(platform="win32", kinds=kinds), footprint("candidate", platform="win32", kinds=without), expected=expected())
    assert report["verdict"] == "red" and {d["leaf"] for d in report["differences"]} == {"proc.utility/sandboxingKind=1/notForBrowser"}


def etw(*events):
    body = "".join('<Event %s><System><Provider Name="Microsoft-Windows-Kernel-File"/><EventID>12</EventID><Execution ProcessID="%d" ThreadID="7"/></System>'
                   '<EventData><Data Name="Irp">0x1</Data><Data Name="FileName">%s</Data></EventData></Event>' % (NS, pid, name) for pid, name in events)
    return "<Events>%s</Events>" % body


def test_the_etw_file_events_are_read_for_the_processes_of_the_tree_only(tmp_path):
    xml = tmp_path / "files.xml"
    xml.write_text(etw((100, "\\Device\\HarddiskVolume3\\Windows\\System32\\kernel32.dll"), (102, "C:\\x\\y.dll"), (999, "C:\\other.dll"), (100, "C:\\x\\y.dll")),
                   encoding="utf-8")
    seen, events = host.etw_files(xml, {100, 102})
    assert events == 4 and seen == {(100, "\\Device\\HarddiskVolume3\\Windows\\System32\\kernel32.dll"), (102, "C:\\x\\y.dll"), (100, "C:\\x\\y.dll")}
    xml.write_bytes(etw((100, "C:\\caf\u00e9.dll")).encode("utf-16"))
    assert host.etw_files(xml, {100})[0] == {(100, "C:\\caf\u00e9.dll")}


def test_the_audit_setting_is_read_from_the_csv_of_auditpol():
    csv = "Machine Name,Policy Target,Subcategory,Subcategory GUID,Inclusion Setting,Exclusion Setting\nRUNNER,System,Process Creation,{0CCE922B},Success,\n"
    assert host.parse_audit_setting(csv) == "Success" and host.parse_audit_setting("") == ""


REAL_SESSION_LIST = """
Data Collector Set                      Type                          Status
-------------------------------------------------------------------------------
Circular Kernel Context Logger          Trace                         Running
EventLog-Application                    Trace                         Running
EventLog-Security                       Trace                         Running
EventLog-System                         Trace                         Running
Microsoft-Windows-Rdp-Graphics-RdpIdd-Trace Trace                     Running
DiagLog                                 Trace                         Running
abhost-4242-123456789                   Trace                         Running
SgrmEtwSession                          Trace                         Stopped
UBPM                                    Trace                         Running

The command completed successfully.
"""


def logman_list(sessions):
    """The output of `logman query -ets` for {name: status}: a header, a line of dashes, a row per session, the closing line."""
    rows = "".join("%-39s %-29s %s\n" % (name, "Trace", status) for name, status in sessions.items())
    return "\nData Collector Set                      Type                          Status\n" + "-" * 79 + "\n" + rows + "\nThe command completed successfully.\n"


class FakeTools:
    """`_tool` of a Windows machine: the audit, the registry, the Security log, logman and tracerpt, with a record of what was asked."""

    def __init__(self, work, tree, files, canaries=("start", "end"), still_listed=0, list_failures=0):
        self.work, self.tree, self.files, self.canaries, self.calls = work, tree, files, canaries, []
        self.list_failures = list_failures  # the first reads of the list fail with 4200 (ERROR_WMI_GUID_NOT_FOUND)
        self.sessions, self.still_listed, self.stopped = {}, still_listed, {}  # a stopped session is listed `still_listed` more times, then gone

    def __call__(self, argv, timeout=300.0):
        self.calls.append(list(argv))
        tool = argv[0]
        if tool == "logman":
            if argv[1] == "create":
                self.sessions[argv[3]] = "Running"
            elif argv[1] == "query" and argv[2:] == ["-ets"]:
                if self.list_failures > 0:
                    self.list_failures -= 1
                    raise host.TraceError("logman query -ets failed with 0x80071068 (2147946600): The GUID passed was not recognized as valid by a WMI data provider.", 2147946600)
                for name in [n for n, left in self.stopped.items() if left == 0]:
                    self.sessions.pop(name, None)
                    del self.stopped[name]
                for name in self.stopped:
                    self.stopped[name] -= 1
                return logman_list(self.sessions)
            elif argv[1] == "query":  # the query of one session resolves the GUIDs of its providers through WMI: run 45257629988
                raise host.TraceError("logman query failed with 0x80071068", 2147946600)
            elif argv[1] == "stop":
                if argv[2] not in self.sessions or argv[2] in self.stopped:
                    raise host.TraceError("logman stop failed with 0x80071069", 2147946601)  # 4201, ERROR_WMI_INSTANCE_NOT_FOUND
                self.stopped[argv[2]] = self.still_listed
        if tool == "auditpol" and argv[1] == "/get":
            return "Machine Name,Policy Target,Subcategory,Subcategory GUID,Inclusion Setting,Exclusion Setting\nM,System,Process Creation,{G},No Auditing,\n"
        if tool == "reg" and argv[1] == "query":
            raise host.TraceError("the value is not there")
        if tool == "wevtutil":
            if "/c:1" in argv:
                return "<Event %s><System><EventRecordID>7</EventRecordID></System></Event>" % NS
            return "\n".join(self.tree)
        if tool == "tracerpt":
            mine = [(os.getpid(), str(p)) for p in sorted(self.work.glob("canary-*")) if any(c in p.name for c in self.canaries)]
            Path(argv[argv.index("-o") + 1]).write_text(etw(*(self.files + mine)), encoding="utf-8")
            Path(argv[argv.index("-summary") + 1]).write_text("Events Lost: 0\n", encoding="utf-8")
        return ""


def a_windows_run(tmp_path, monkeypatch, files, canaries=("start", "end")):
    fake = FakeTools(tmp_path, TREE, files, canaries)
    monkeypatch.setattr(host, "_tool", fake)
    trace = host.WindowsTrace(tmp_path)
    trace.start()
    return trace, fake


TREE_FILES = [(100, "C:\\lab\\ff\\xul.dll"), (102, "C:\\Windows\\System32\\kernel32.dll"), (103, "C:\\Windows\\System32\\kernel32.dll"),
              (104, "C:\\Windows\\System32\\kernel32.dll"), (101, "C:\\Windows\\System32\\conhost.exe"), (107, "C:\\Windows\\System32\\kernel32.dll"),
              (999, "C:\\unrelated.dll")]


def test_the_windows_trace_turns_the_audit_on_before_the_session_and_puts_the_machine_back(tmp_path, monkeypatch):
    trace, fake = a_windows_run(tmp_path, monkeypatch, TREE_FILES)
    got = trace.finish({100}, alive=lambda pid: False, sleep=lambda s: None)
    trace.cleanup()
    names = [(c[0], c[1]) for c in fake.calls]
    order = [names.index(("auditpol", "/set")), names.index(("reg", "add")), names.index(("logman", "create")), names.index(("logman", "stop")),
             names.index(("tracerpt", str(tmp_path / "files.etl")))]
    assert order == sorted(order)
    assert [c for c in fake.calls if c[:2] == ["auditpol", "/set"]][-1][-2:] == ["/success:disable", "/failure:disable"], "the audit is put back as it was"
    assert any(c[:2] == ["reg", "delete"] for c in fake.calls)
    assert [e["pid"] for e in got["tree"]] == [100, 101, 102, 103, 104, 107]
    assert got["problems"] == [] and got["exited"] == {"exited": True, "reads": 2}
    assert {pid for pid, _n in got["files"]} == {100, 101, 102, 103, 104, 107}, "a process of the machine that is not of the tree is not in the trace"


def test_the_windows_trace_is_red_when_its_session_did_not_see_its_own_canary(tmp_path, monkeypatch):
    trace, _fake = a_windows_run(tmp_path, monkeypatch, TREE_FILES, canaries=("start",))
    got = trace.finish({100}, alive=lambda pid: False, sleep=lambda s: None)
    assert len(got["problems"]) == 1 and "end canary" in got["problems"][0] and "was not recording" in got["problems"][0]


def test_the_windows_trace_is_red_for_a_process_it_has_no_file_of(tmp_path, monkeypatch):
    trace, _fake = a_windows_run(tmp_path, monkeypatch, [f for f in TREE_FILES if f[0] != 107])
    got = trace.finish({100}, alive=lambda pid: False, sleep=lambda s: None)
    assert got["problems"] == ["the file trace holds no open of the process(es) 107"]


def test_a_windows_tool_that_fails_says_so_and_is_not_an_empty_trace(tmp_path, monkeypatch):
    def refuse(argv, timeout=300.0):
        raise host.TraceError("auditpol /set failed with 5: Access is denied.")

    monkeypatch.setattr(host, "_tool", refuse)
    with pytest.raises(host.TraceError, match="Access is denied"):
        host.WindowsTrace(tmp_path).start()


# -
# The spellings of run 44963364060 (Windows): the runner's TEMP is the 8.3 one, the engine opens the same folder under both spellings.

RUNNER = host.Context("win32", app=["C:\\Lab\\labrunner\\AppData\\Local\\invisible-playwright\\invisible-playwright\\Cache\\firefox-68_159.0_20261121043125"],
                      temp=["C:\\Lab\\LABRUN~1\\AppData\\Local\\Temp\\ab-host-tmp-hut170cg", "C:\\Lab\\LABRUN~1\\AppData\\Local\\Temp\\ab-host-tmp-hut170cg\\ab-host-x\\seed0"],
                      devices={"\\Device\\HarddiskVolume3": "C:"}, aliases=[("C:\\Lab\\LABRUN~1", "C:\\Lab\\labrunner")])


@pytest.mark.parametrize("path", [
    "c:\\lab\\labrunner\\appdata\\local\\temp\\ab-host-tmp-hut170cg\\invisible_profile_8704_tdb3g1yy\\breach-alerts.db",
    "C:\\Lab\\LABRUN~1\\AppData\\Local\\Temp\\ab-host-tmp-hut170cg\\invisible_profile_8704_tdb3g1yy\\breach-alerts.db-wal",
    "\\Device\\HarddiskVolume3\\Lab\\LABRUN~1\\AppData\\Local\\Temp\\ab-host-tmp-hut170cg\\invisible_profile_8704_tdb3g1yy\\extension-store\\data.safe.bin",
    "\\??\\c:\\lab\\labrunner\\appdata\\local\\temp\\ab-host-tmp-hut170cg\\invisible_profile_8704_tdb3g1yy\\extension-store",
])
def test_a_path_of_the_launchs_temp_dir_is_ours_whatever_its_spelling(path):
    assert host.classify(path, RUNNER)[0] == "profile"


def test_the_package_is_ours_in_either_spelling_and_a_neighbour_of_the_temp_root_is_not():
    assert host.classify("C:\\Lab\\LABRUN~1\\AppData\\Local\\invisible-playwright\\invisible-playwright\\Cache\\firefox-68_159.0_20261121043125\\xul.dll", RUNNER) == ("package", "xul.dll")
    assert host.classify("c:\\lab\\labrunner\\appdata\\local\\temp\\ab-host-tmp-n4yegll6\\x", RUNNER)[0] == "host"
    assert host.classify("C:\\Lab\\LABRUN~1\\AppData\\Local\\Temp\\other.tmp", RUNNER) == ("host", "c:\\lab\\labrunner\\appdata\\local\\temp\\other.tmp")


def test_a_host_path_has_one_spelling_for_the_two_runs():
    assert host.classify_paths(["C:\\Lab\\LABRUN~1\\.gitconfig", "c:\\lab\\labrunner\\.gitconfig"], RUNNER) == (["c:\\lab\\labrunner\\.gitconfig"], {})


def test_the_uuid_of_the_uninstall_ping_is_not_a_difference_and_the_rest_of_its_path_is():
    one = "C:\\ProgramData\\Mozilla-1de4eec8-1241-4177-a864-e594e8d1fb38\\uninstall_ping_f8a3fe26202962af_6f9b4c24-2bcc-4463-9445-c996a5d02c39.json"
    two = "C:\\ProgramData\\Mozilla-1de4eec8-1241-4177-a864-e594e8d1fb38\\uninstall_ping_f8a3fe26202962af_90e83a0f-ee9e-43c6-9c48-ad33753d3a60.json.tmp"
    got = host.classify_paths([one, two, one + ".tmp"], RUNNER)[0]
    assert got == ["c:\\programdata\\mozilla-1de4eec8-1241-4177-a864-e594e8d1fb38\\uninstall_ping_f8a3fe26202962af_<uuid>.json",
                   "c:\\programdata\\mozilla-1de4eec8-1241-4177-a864-e594e8d1fb38\\uninstall_ping_f8a3fe26202962af_<uuid>.json.tmp"]
    other_machine = one.replace("f8a3fe26202962af", "0000000000000000")
    assert host.classify_paths([other_machine], RUNNER)[0][0].endswith("uninstall_ping_0000000000000000_<uuid>.json"), "the install hash stays in the name"
    a, b = footprint(platform="win32", files=got), footprint("again", platform="win32", files=got)
    assert judge(a, b, a_a=True, floors={"proc": 2, "file": 1})["verdict"] == "green"
    kept = host.classify("C:\\ProgramData\\Mozilla-1de4eec8-1241-4177-a864-e594e8d1fb38\\other_6f9b4c24-2bcc-4463-9445-c996a5d02c39.json", RUNNER)[1]
    assert kept.endswith("6f9b4c24-2bcc-4463-9445-c996a5d02c39.json"), "only the name of the ping is normalized"


def test_a_background_task_of_the_engine_is_a_kind_of_its_own_by_its_command_line():
    exe = "D:\\lab\\runner\\AppData\\Local\\invisible-playwright\\Cache\\firefox.exe"
    cmd = exe + " --backgroundtask pingsender https://incoming.telemetry.mozilla.org/submit/telemetry/244041fe/first-shutdown/Firefox/159.0/release/2026?v=4 C:\\x\\ping"
    events = host.parse_audit_events(audit(1, 200, 100, exe, cmd))
    assert host.windows_processes(events, exe)[200]["label"] == "backgroundtask/pingsender"
    assert host.label_of_exec("/e/firefox/firefox", ["/e/firefox/firefox", "--backgroundtask", "uninstall"], REAL) == "backgroundtask/uninstall"
    assert host.ping_type(host.split_command_line(cmd)) == "first-shutdown"


def test_the_session_is_recording_when_the_last_start_canary_is_in_the_trace_even_if_the_first_ones_are_not(tmp_path, monkeypatch):
    trace, fake = a_windows_run(tmp_path, monkeypatch, TREE_FILES, canaries=("start3", "end"))
    got = trace.finish({100}, alive=lambda pid: False, sleep=lambda s: None)
    assert got["problems"] == []
    stop = next(i for i, c in enumerate(fake.calls) if c[:2] == ["logman", "stop"])
    assert sum(1 for c in fake.calls[:stop] if c == ["logman", "query", "-ets"]) == 3, "a round trip to the list of the sessions before each canary"
    trace, _fake = a_windows_run(tmp_path, monkeypatch, TREE_FILES, canaries=("start1", "start2", "end"))
    problems = trace.finish({100}, alive=lambda pid: False, sleep=lambda s: None)["problems"]
    assert len(problems) == 1 and "last start canary" in problems[0]


# -
# The warm-up: a discarded launch of each engine before its measured ones, kept as evidence, never an excuse.

def with_pingsenders(fp, n, types=("event", "first-shutdown", "new-profile"), parent="parent"):
    fp = copy.deepcopy(fp)
    for seed in fp["seeds"]:
        seed["kinds"][host.PINGSENDER_KIND] = n
        seed["pingsenders"] = [{"type": types[i % len(types)], "form": "backgroundtask", "parent": parent} for i in range(n)]
        seed["spawned_by"] = {host.PINGSENDER_KIND: [parent]}
    return fp


def with_warmup(fp, kinds=None, files=None, touched_by=None, spawned_by=None, seed=11):
    fp = copy.deepcopy(fp)
    fp["warmup"] = {"seed": seed, "steps": [], "problems": [], "kinds": dict(KINDS if kinds is None else kinds), "files": list(FILES if files is None else files),
                    "touched_by": touched_by or {}, "spawned_by": spawned_by or {}}
    return fp


def test_an_engine_run_makes_its_warm_up_launch_first_of_the_first_seed_then_its_measured_ones_and_the_idle_launch_last(tmp_path, monkeypatch):
    calls = []

    def fake_run_seed(binary, seed, work, name=None, idle=False, datareporting_dir=None, audit_dir=None):
        calls.append((seed, name, idle))
        return seed_footprint(seed)

    monkeypatch.setattr(host, "run_seed", fake_run_seed)
    monkeypatch.setattr(host.os.path, "realpath", lambda p: p)
    doc = host.run_engine("ff", "linux-x86_64", [11, 12], "firefox-68")
    assert calls == [(11, "warmup", False), (11, None, False), (12, None, False), (11, "idle", True)], "no bare launch"
    assert doc["warmup"]["seed"] == 11 and [s["seed"] for s in doc["seeds"]] == [11, 12] and doc["idle"]["seed"] == 11 and "bare" not in doc and doc["problems"] == []


def test_a_warm_up_that_failed_is_red_in_the_document_like_a_measured_launch(monkeypatch):
    def fake_run_seed(binary, seed, work, name=None, idle=False, datareporting_dir=None, audit_dir=None):
        fp = seed_footprint(seed)
        if name == "warmup":
            fp["problems"] = ["the file trace did not see the end canary file of the oracle"]
            fp["steps"] = [{"n": 2, "name": "hover", "ok": False, "error": "timeout"}]
        return fp

    monkeypatch.setattr(host, "run_seed", fake_run_seed)
    monkeypatch.setattr(host.os.path, "realpath", lambda p: p)
    doc = host.run_engine("ff", "linux-x86_64", [11], "firefox-68")
    assert doc["problems"] == ["warm-up: the file trace did not see the end canary file of the oracle", "warm-up: seed 11: step 2 (hover) failed: timeout"]
    report = judge(dict(footprint(), problems=doc["problems"]), footprint(), a_a=True)
    assert report["verdict"] == "red" and any("side A: warm-up: " in p for p in report["problems"])


def test_the_runs_of_an_ab_follow_one_another_in_the_same_order_each_with_its_own_warm_up(tmp_path, monkeypatch):
    runs = []
    pub = fake_engine(tmp_path / "fetch" / "engines", "firefox-68_x", b"published")
    cand = fake_engine(tmp_path / "checkout" / "ff", ".", b"candidate")
    monkeypatch.setattr(host.oracle, "fetch_published", lambda tag, work: (pub, "pub-seal"))
    monkeypatch.setattr(host, "run_subprocess", lambda exe, seal, leg, out, label, seeds: runs.append((label, Path(exe).read_bytes(), list(seeds))) or 1)
    assert host.ab(cand, Path("cand-seal"), "linux-x86_64", tmp_path / "o1", "firefox-68", [11, 12]) == 2
    assert [(r[0], r[1]) for r in runs] == [("firefox-68", b"published"), ("firefox-68 again", b"published"), ("candidate", b"candidate")]
    runs.clear()
    assert host.ab(None, None, "linux-x86_64", tmp_path / "o2", "firefox-68", [11, 12]) == 2
    assert [(r[0], r[1]) for r in runs] == [("firefox-68", b"published"), ("firefox-68 again", b"published")], "the A/A: the published engine twice, in the same order"


def test_what_only_the_warm_up_did_is_listed_with_the_process_that_did_it():
    kinds = dict(KINDS, **{"backgroundtask/pingsender": 3, "socket": 2})
    fp = with_warmup(footprint(kinds={**KINDS, "socket": 1}), kinds=kinds, files=FILES + ["/etc/machine-id", "/usr/lib/libx.so"],
                     touched_by={"/etc/machine-id": ["backgroundtask/pingsender"], "/usr/lib/libx.so": ["parent", "tab/isForBrowser"]},
                     spawned_by={"backgroundtask/pingsender": ["parent"], "socket": ["parent"]})
    listing = host.warmup_only(fp)
    assert [(k["kind"], k["warmup"], k["measured"], k["spawned_by"]) for k in listing["kinds"]] == [
        ("backgroundtask/pingsender", 3, 0, ["parent"]), ("socket", 2, 1, ["parent"])]
    assert [(f["path"], f["touched_by"]) for f in listing["files"]] == [("/etc/machine-id", ["backgroundtask/pingsender"]), ("/usr/lib/libx.so", ["parent", "tab/isForBrowser"])]
    assert host.warmup_only(footprint()) is None, "a footprint with no warm-up lists nothing"


def test_the_count_of_a_tab_kind_in_the_warm_up_is_not_a_first_launch_behaviour():
    fp = with_warmup(footprint(), kinds=dict(KINDS, **{"tab/isForBrowser": 9}))
    assert host.warmup_only(fp)["kinds"] == []


def test_the_report_lists_what_is_only_in_the_warm_up_and_says_when_both_engines_have_it(capsys):
    kinds = dict(KINDS, **{"backgroundtask/pingsender": 3})
    spawned = {"backgroundtask/pingsender": ["parent"]}
    a = with_warmup(footprint(), kinds=kinds, spawned_by=spawned, files=FILES + ["/etc/machine-id"], touched_by={"/etc/machine-id": ["backgroundtask/pingsender"]})
    b = with_warmup(footprint("firefox-68 again"), kinds=kinds, spawned_by=spawned, files=FILES + ["/etc/machine-id"],
                    touched_by={"/etc/machine-id": ["backgroundtask/pingsender"]})
    report = judge(a, b, a_a=True)
    assert report["verdict"] == "green", "a first-launch-only behaviour that the measured launches do not have is written, not red"
    assert report["warmup"]["both"] == {"kinds": ["backgroundtask/pingsender"], "files": ["/etc/machine-id"], "net": []}
    host.print_report(report, "firefox-68", "firefox-68 again")
    out = capsys.readouterr().out
    assert "WARMUP-ONLY firefox-68 seed 11 proc.backgroundtask/pingsender: warm-up 3, measured 0 (spawned by: parent)" in out
    assert "WARMUP-ONLY firefox-68 seed 11 file./etc/machine-id (touched by: backgroundtask/pingsender)" in out
    assert "WARMUP-ONLY firefox-68 again seed 11 proc.backgroundtask/pingsender" in out


def test_how_many_pingsenders_a_launch_has_is_not_judged_it_is_listed():
    """Run 45228081536 (A/A of the 68, Windows): 2 pingsenders in seed 4 of one side was red ("not 0 or 3"), and the sockets they start, 3 in seed 63 of A and none of B, were a difference of
    counts. Mozilla's start of the pingsender varies from a launch to the next: any number of them, in any of the launches, is green, in the list of what is not judged."""
    ping = host.PINGSENDER_KIND
    a = six_launches("A", {i: {ping: 3} for i in range(6)})
    for n, where in ((0, {}), (1, {1: {ping: 1}}), (2, {1: {ping: 2}, 4: {ping: 2}}), (4, {0: {ping: 4}, 5: {ping: 4}})):
        report = host.judge(a, six_launches("B", where), a_a=True, floors=SMALL)
        assert report["verdict"] == ("red" if n == 0 else "green"), (n, report["problems"][:2])
        if n:
            (entry,) = report["not_judged"]["kinds"]
            assert entry["kind"] == ping and entry["A"]["measured"] == [6, 6] and entry["B"]["measured"][1] == 6 and 0 < entry["B"]["measured"][0] < 6, n
    pings = with_pingsenders(footprint(), 3)
    assert judge(with_pingsenders(footprint(), 3), with_pingsenders(footprint("again"), 3), a_a=True)["verdict"] == "green"
    report = judge(pings, footprint("again"), a_a=True)
    assert report["verdict"] == "red" and {d["leaf"] for d in report["differences"]} == {"proc." + ping}, "in every launch of one engine and in none of the other's: the rule of every kind"


def test_a_pingsender_kind_in_six_launches_of_six_of_one_engine_and_in_none_of_the_other_is_red_and_one_in_some_launches_is_listed():
    ping, child = host.PINGSENDER_KIND, "socket<-pingsender"
    both = {i: {ping: 3, child: 3} for i in range(6)}
    report = host.judge(six_launches("A", both), six_launches("B"), a_a=True, floors=SMALL)
    assert report["verdict"] == "red" and {d["leaf"] for d in report["differences"]} == {"proc." + ping, "proc." + child}
    some = host.judge(six_launches("A", both), six_launches("B", {0: {child: 1}, 3: {ping: 2, child: 2}}), a_a=True, floors=SMALL)
    assert some["verdict"] == "green" and {e["kind"] for e in some["not_judged"]["kinds"]} == {ping, child}


def test_the_types_and_the_parents_of_the_pingsenders_are_not_judged_only_the_separate_executable_is():
    wrong = with_pingsenders(footprint(), 3, types=("event", "event", "new-profile"))
    orphan = with_pingsenders(footprint(), 3, parent="gpu")
    for fp in (wrong, orphan):
        assert judge(fp, with_pingsenders(footprint("again"), 3), a_a=True)["problems"] == []


def test_the_separate_pingsender_executable_is_red_whatever_the_count():
    for exe in ("pingsender", "pingsender.exe"):
        fp = footprint(kinds=dict(KINDS, **{exe: 1}))
        assert any("the separate %s ran (B266" % exe in p for p in judge(fp, footprint("again"), a_a=True)["problems"])


def test_what_only_the_pingsender_touches_is_a_path_like_any_other_and_its_destinations_are_compared_when_it_is_on_both_sides_and_left_out_when_it_is_on_one_side_only():
    imagehlp = "c:\\windows\\system32\\imagehlp.dll"

    def with_surface(fp, n, files=(imagehlp,), net=("backgroundtask/pingsender: connect tcp AF_INET 141.101.90.1:443",)):
        fp = with_pingsenders(fp, n) if n else fp
        for seed in fp["seeds"]:
            if n:
                seed["files"] = sorted(set(seed["files"]) | set(files))
                seed["touched_by"] = {f: ["backgroundtask/pingsender"] for f in files}
                seed["net"] = list(net)
        return fp

    one_side = judge(with_surface(footprint(), 3), footprint("again"), a_a=True)
    assert {d["leaf"] for d in one_side["differences"]} == {"file." + imagehlp, "proc." + host.PINGSENDER_KIND}, "its paths and its kind are like any other: in every launch of one side and in none of the other"
    assert not any(d["category"] == "net" for d in one_side["differences"]), "its destinations on one side only are left out"
    same = judge(with_surface(footprint(), 3), with_surface(footprint("again"), 3), a_a=True)
    assert same["verdict"] == "green", same["problems"]
    a = with_surface(footprint(), 3)
    b = with_surface(footprint("again"), 3, files=(imagehlp, "c:\\windows\\system32\\winhttp.dll"))
    report = judge(a, b, a_a=True)
    assert report["verdict"] == "red" and {d["leaf"] for d in report["differences"]} == {"file.c:\\windows\\system32\\winhttp.dll"}, "a new read of the host by the pingsender shows"
    back = with_surface(footprint("again"), 3, net=())
    assert {d["leaf"] for d in judge(a, back, a_a=True)["differences"]} == {"net.backgroundtask/pingsender: connect public, telemetry"}
    wrong_exe = footprint("again", kinds=dict(KINDS, **{host.PINGSENDER_KIND: 3, "pingsender.exe": 3}))
    assert judge(with_surface(footprint(), 3), with_surface(wrong_exe, 3), a_a=True)["verdict"] == "red", "a return to the separate executable shows"


def test_a_pingsender_that_goes_to_the_telemetry_host_direct_is_the_products_behaviour_in_a_session_without_a_proxy_and_is_not_red():
    ping = ["backgroundtask/pingsender: connect tcp AF_INET 141.101.90.1:443"]
    other = ["backgroundtask/pingsender: connect tcp AF_INET6 [2600:1901::1]:443"]
    a, b = with_pingsenders(footprint(), 3), with_pingsenders(footprint("again"), 3)
    for seed in a["seeds"]:
        seed["net"] = ping
    for seed in b["seeds"]:
        seed["net"] = other
    assert judge(a, b, a_a=True)["verdict"] == "green", "the address and the family are the third party's: `public, telemetry`"
    assert host.pingsender_destinations(a["seeds"][0]) == ["backgroundtask/pingsender: connect public, telemetry"]
    idle = with_idle(footprint(), net=ping)
    assert judge(idle, with_idle(footprint("again")), a_a=True)["verdict"] == "green", "no rule about a pingsender that goes out direct in the host oracle"
    assert "session_proxy" not in inspect.getsource(host)


def test_a_path_that_differs_names_the_kinds_that_touched_it():
    a, b = footprint(files=FILES + ["/etc/machine-id"]), footprint("again")
    for seed in a["seeds"]:
        seed["touched_by"] = {"/etc/machine-id": ["gpu", "parent"]}
    report = judge(a, b, a_a=True)
    assert all(d["by"] == {"A": ["gpu", "parent"], "B": []} for d in report["differences"])
    assert any("file./etc/machine-id: A 1 | B \"<missing>\" (by A: gpu, parent | B: -)" in p for p in report["problems"])


# -
# The tree only: what a process outside the tree of the launch opens is not compared.

def test_a_path_a_service_opened_is_counted_by_process_name_and_left_out_and_the_trees_stays(tmp_path, monkeypatch):
    service = TREE_FILES + [(106, "C:\\ProgramData\\Microsoft\\Windows Defender\\Scans\\x.db"), (106, "C:\\Windows\\System32\\catroot2\\y"), (999, "C:\\Windows\\Prefetch\\z.pf")]
    trace, _fake = a_windows_run(tmp_path, monkeypatch, service)
    got = trace.finish({100}, alive=lambda pid: False, sleep=lambda s: None, namer=lambda pid: "sysmain.exe")
    assert got["outside"] == {"svchost.exe": 2, "sysmain.exe": 2}, "pid 106 is named by its audit event, the pid with no event by the machine (999 has the unrelated.dll and the .pf)"
    assert {pid for pid, _n in got["files"]} == {100, 101, 102, 103, 104, 107}
    assert not any("Defender" in n or "catroot2" in n for _p, n in got["files"])
    assert got["problems"] == []


def test_a_process_that_the_tree_made_is_in_the_tree_whatever_it_is_called():
    tree = host.process_tree(host.parse_audit_events("\n".join(TREE)), {100})
    assert 101 in {e["pid"] for e in tree} and 106 not in {e["pid"] for e in tree} and 105 not in {e["pid"] for e in tree}


def test_the_report_says_what_was_left_out_of_the_comparison(capsys):
    a, b = footprint(platform="win32"), footprint("again", platform="win32")
    a["seeds"][0]["outside_tree"] = {"svchost.exe": 12, "msmpeng.exe": 3}
    report = judge(a, b, a_a=True)
    assert report["verdict"] == "green" and report["outside_tree"] == {"A": {"11": {"svchost.exe": 12, "msmpeng.exe": 3}}, "B": {}}
    host.print_report(report, "firefox-68", "firefox-68 again")
    assert "OUTSIDE THE TREE (not compared) firefox-68 seed 11: msmpeng.exe 3 paths, svchost.exe 12 paths" in capsys.readouterr().out


STRACE_THREADS = """\
100 execve("/e/firefox/firefox", ["/e/firefox/firefox", "-contentproc", "forkserver"], 0x1) = 0
100 clone3({flags=CLONE_VM|CLONE_FS|CLONE_FILES|CLONE_SIGHAND|CLONE_THREAD|CLONE_SYSVSEM, exit_signal=0}, 88) = 150
100 fork() = 200
200 prctl(PR_SET_NAME, "Web Content") = 0
200 clone(child_stack=0x7f, flags=CLONE_VM|CLONE_THREAD|CLONE_SIGHAND <unfinished ...>
200 <... clone resumed>, child_tidptr=0x7f) = 250
150 openat(AT_FDCWD, "/etc/a", O_RDONLY) = 3
250 openat(AT_FDCWD, "/etc/b", O_RDONLY) = 3
200 openat(AT_FDCWD, "/etc/b", O_RDONLY) = 3
"""


def test_on_linux_the_strace_tree_is_the_tree_and_a_threads_path_is_its_processs():
    lines = STRACE_THREADS.splitlines()
    owners = host.thread_owners(lines)
    assert owners == {150: 100, 250: 200}
    assert host.touched_by_pid(lines) == {"/e/firefox/firefox": {100}, "/etc/a": {150}, "/etc/b": {250, 200}}
    procs = host.spawned_processes(lines, REAL)
    labels = {pid: p["label"] for pid, p in procs.items()}
    assert labels == {100: "forkserver", 200: "Web Content"}
    assert host.by_kind([(pid, p) for p, pids in host.touched_by_pid(lines).items() for pid in pids], lambda pid: labels.get(host.owner_of(pid, owners), "?"),
                        host.Context("linux")) == {"/e/firefox/firefox": ["forkserver"], "/etc/a": ["forkserver"], "/etc/b": ["Web Content"]}
    assert host.spawners(labels, {200: 100}) == {"Web Content": ["forkserver"], "forkserver": ["(oracle)"]}


def test_a_closed_linux_run_keeps_the_kinds_that_touched_each_path_and_nothing_is_outside_its_tree(tmp_path, monkeypatch):
    log = tmp_path / "strace.log"
    log.write_text(LOG + '101 openat(AT_FDCWD, "/etc/machine-id", O_RDONLY) = 3\n104 stat("/etc/machine-id", {st_mode=S_IFREG}) = 0\n', encoding="utf-8")
    monkeypatch.setattr(host.os.path, "realpath", lambda p: p)
    monkeypatch.setattr(host, "_context", lambda binary, windows, work: host.Context("linux", app=["/e/firefox"], temp=["/tmp/ab-host-tmp-x"]))
    fp = host._seed_result(REAL, 11, tmp_path, [], log, None, None, False)
    assert fp["touched_by"]["/etc/machine-id"] == ["glxtest", "lsb_release"]
    assert fp["spawned_by"]["glxtest"] == ["parent"] and fp["outside_tree"] == {}


# -
# The network of the tree (Linux): the netlink sockets, their requests, and the public destinations, per kind.

NET_LOG = r"""300 execve("/e/firefox/firefox", ["/e/firefox/firefox", "-profile", "/tmp/p"], 0x7ffd /* 40 vars */) = 0
300 socket(AF_NETLINK, SOCK_RAW|SOCK_CLOEXEC, NETLINK_ROUTE) = 41<socket:[81234]>
300 bind(41<socket:[81234]>, {sa_family=AF_NETLINK, nl_pid=0, nl_groups=00000511}, 12) = 0
300 clone3({flags=CLONE_VM|CLONE_FS|CLONE_FILES|CLONE_SIGHAND|CLONE_THREAD|CLONE_SYSVSEM, exit_signal=0, stack=0x7f}, 88) = 301
301 sendto(41<socket:[81234]>, [{nlmsg_len=20, nlmsg_type=RTM_GETLINK, nlmsg_flags=NLM_F_REQUEST|NLM_F_DUMP, nlmsg_seq=1700000001, nlmsg_pid=0}, {ifi_family=AF_UNSPEC, ifi_type=ARPHRD_NETROM, ifi_index=0, ifi_flags=0, ifi_change=0}], 20, 0, {sa_family=AF_NETLINK, nl_pid=0, nl_groups=00000000}, 12) = 20
301 sendmsg(41<socket:[81234]>, {msg_name={sa_family=AF_NETLINK, nl_pid=0, nl_groups=00000000}, msg_namelen=12, msg_iov=[{iov_base=[{nlmsg_len=24, nlmsg_type=RTM_GETADDR, nlmsg_flags=NLM_F_REQUEST|NLM_F_DUMP, nlmsg_seq=1700000002, nlmsg_pid=0}, {ifa_family=AF_UNSPEC, ifa_prefixlen=0, ifa_flags=0, ifa_scope=RT_SCOPE_UNIVERSE, ifa_index=0}], iov_len=24}], msg_iovlen=1, msg_controllen=0, msg_flags=0}, 0) = 24
300 socket(AF_NETLINK, SOCK_RAW|SOCK_CLOEXEC|SOCK_NONBLOCK, NETLINK_KOBJECT_UEVENT <unfinished ...>
300 <... socket resumed>) = 42<socket:[81240]>
300 socket(AF_INET, SOCK_STREAM|SOCK_CLOEXEC|SOCK_NONBLOCK, IPPROTO_IP) = 43<socket:[81250]>
301 connect(43<socket:[81250]>, {sa_family=AF_INET, sin_port=htons(443), sin_addr=inet_addr("8.8.8.8")}, 16) = -1 EINPROGRESS (Operation now in progress)
300 socket(AF_INET6, SOCK_DGRAM|SOCK_CLOEXEC, IPPROTO_UDP) = 44<socket:[81260]>
300 sendto(44<socket:[81260]>, "\x12\x34\x01\x00\x00\x01", 6, MSG_NOSIGNAL, {sa_family=AF_INET6, sin6_port=htons(53), sin6_flowinfo=htonl(0), inet_pton(AF_INET6, "2001:4860:4860::8888", &sin6_addr), sin6_scope_id=0}, 28) = 6
300 connect(43<socket:[81250]>, {sa_family=AF_INET, sin_port=htons(41234), sin_addr=inet_addr("127.0.0.1")}, 16) = 0
300 connect(43<socket:[81250]>, {sa_family=AF_INET, sin_port=htons(443), sin_addr=inet_addr("198.51.100.9")}, 16) = 0
300 connect(43<socket:[81250]>, {sa_family=AF_INET, sin_port=htons(80), sin_addr=inet_addr("10.1.0.4")}, 16) = 0
300 sendto(44<socket:[81260]>, "\x00", 1, 0, {sa_family=AF_INET6, sin6_port=htons(5353), sin6_flowinfo=htonl(0), inet_pton(AF_INET6, "ff02::fb", &sin6_addr), sin6_scope_id=0}, 28) = 1
300 connect(45<socket:[81270]>, {sa_family=AF_UNIX, sun_path="/run/dbus/system_bus_socket"}, 110) = 0
300 sendto(43<socket:[81250]>, "GET / HTTP/1.1\r\n", 16, MSG_NOSIGNAL, NULL, 0) = 16
300 fork() = 302
302 execve("/proc/self/exe", ["/e/firefox/firefox", "-contentproc", "-prefsLen", "1", "socket"], 0x7ffd /* 40 vars */) = 0
302 socket(AF_INET, SOCK_DGRAM|SOCK_CLOEXEC|SOCK_NONBLOCK, IPPROTO_IP) = 5<socket:[81300]>
302 sendmmsg(5<socket:[81300]>, [{msg_hdr={msg_name={sa_family=AF_INET, sin_port=htons(3478), sin_addr=inet_addr("1.1.1.1")}, msg_namelen=16, msg_iov=[{iov_base="\x00\x01\x00\x00", iov_len=4}], msg_iovlen=1, msg_controllen=0, msg_flags=0}, msg_len=4}], 1, 0) = 1
302 connect(5<socket:[81300]>, {sa_family=AF_INET6, sin6_port=htons(443), sin6_flowinfo=htonl(0), inet_pton(AF_INET6, "::ffff:203.0.113.7", &sin6_addr), sin6_scope_id=0}, 28) = 0
"""

PARENT_NET = ["parent: connect tcp AF_INET 8.8.8.8:443", "parent: netlink NETLINK_KOBJECT_UEVENT", "parent: netlink NETLINK_ROUTE",
              "parent: netlink NETLINK_ROUTE RTM_GETADDR", "parent: netlink NETLINK_ROUTE RTM_GETLINK", "parent: netlink NETLINK_ROUTE bind nl_groups=00000511",
              "parent: send udp AF_INET6 [2001:4860:4860::8888]:53"]


def test_the_network_of_a_linux_run_is_its_netlink_sockets_their_requests_and_its_public_destinations_per_kind(tmp_path, monkeypatch):
    log = tmp_path / "strace.log"
    log.write_text(NET_LOG, encoding="utf-8")
    monkeypatch.setattr(host.os.path, "realpath", lambda p: p)
    monkeypatch.setattr(host, "_context", lambda binary, windows, work: host.Context("linux", app=["/e/firefox"], temp=["/tmp/ab-host-tmp-x"]))
    fp = host._seed_result(REAL, 11, tmp_path, [], log, None, None, False)
    assert fp["net"] == PARENT_NET + ["socket: send udp AF_INET 1.1.1.1:3478"], "a thread's request is its process's, a socket is known by its inode"
    assert fp["net_not_compared"] == {"lab": 2, "loopback": 1, "multicast": 1, "private": 1}, "198.51.100.9 and ::ffff:203.0.113.7 are the lab's"
    leaves = host.leaves_of(fp)
    assert leaves["net.parent: netlink NETLINK_ROUTE RTM_GETADDR"] == 1 and not any("127.0.0.1" in k or "198.51.100" in k for k in leaves)


def test_a_netlink_message_strace_printed_as_bytes_is_read_for_its_type():
    lines = ["7 socket(AF_NETLINK, SOCK_RAW|SOCK_CLOEXEC, NETLINK_ROUTE) = 3<socket:[9]>",
             r'7 sendto(3<socket:[9]>, "\x14\x00\x00\x00\x16\x00\x01\x03\x01\x00\x00\x00\x00\x00\x00\x00\x02\x00\x00\x00", 20, 0, '
             r"{sa_family=AF_NETLINK, nl_pid=0, nl_groups=00000000}, 12) = 20",
             "7 sendto(8, {{len=20, type=RTM_GETROUTE, flags=NLM_F_REQUEST|NLM_F_DUMP, seq=1, pid=0}, {rtm_family=AF_INET}}, 20, 0, NULL, 0) = 20"]
    events, skipped = host.network_events(lines)
    assert events == [(7, "netlink NETLINK_ROUTE"), (7, "netlink NETLINK_ROUTE RTM_GETADDR"), (7, "netlink NETLINK_? RTM_GETROUTE")] and skipped == {}


@pytest.mark.parametrize("address,cls", [
    ("8.8.8.8", "public"), ("2001:4860:4860::8888", "public"), ("127.0.0.53", "loopback"), ("::1", "loopback"),
    ("192.0.2.53", "lab"), ("198.51.100.9", "lab"), ("203.0.113.80", "lab"), ("::ffff:198.51.100.5", "lab"),
    ("10.1.0.4", "private"), ("172.17.0.1", "private"), ("192.168.1.1", "private"), ("169.254.169.254", "private"), ("fe80::1%2", "private"),
    ("100.64.0.1", "private"), ("fd00::1", "private"), ("0.0.0.0", "private"), ("224.0.0.251", "multicast"), ("ff02::fb", "multicast"),
    ("not-an-address", "public"),
])
def test_only_a_public_destination_is_compared(address, cls):
    assert host.destination_class(address) == cls


def with_net(fp, entries, skipped=None):
    fp = copy.deepcopy(fp)
    for seed in fp["seeds"]:
        seed["net"] = list(entries)
        seed["net_not_compared"] = dict(skipped or {})
    return fp


def test_a_netlink_request_of_a_kind_on_one_side_only_is_red_and_names_the_kind_and_the_request(capsys):
    a = with_net(footprint(), PARENT_NET)
    b = with_net(footprint("candidate"), [e for e in PARENT_NET if "RTM_GETADDR" not in e])
    report = judge(a, b, expected=[], a_a=True)
    assert report["verdict"] == "red"
    assert [(d["seed"], d["category"], d["leaf"], d["a"], d["b"]) for d in report["differences"]] == [
        (11, "net", "net.parent: netlink NETLINK_ROUTE RTM_GETADDR", 1, host.MISSING), (12, "net", "net.parent: netlink NETLINK_ROUTE RTM_GETADDR", 1, host.MISSING)]
    assert 'seed 11 net.parent: netlink NETLINK_ROUTE RTM_GETADDR: A 1 | B "<missing>"' in report["problems"]
    host.print_report(report, "firefox-68", "candidate")
    assert 'DIFF seed 11 net.parent: netlink NETLINK_ROUTE RTM_GETADDR: firefox-68 1 | candidate "<missing>"' in capsys.readouterr().out


def test_the_same_request_from_another_kind_is_a_difference_too():
    moved = [e.replace("parent: netlink NETLINK_ROUTE RTM_GETADDR", "socket: netlink NETLINK_ROUTE RTM_GETADDR") for e in PARENT_NET]
    report = judge(with_net(footprint(), PARENT_NET), with_net(footprint("again"), moved), a_a=True)
    assert {(d["leaf"], d["a"], d["b"]) for d in report["differences"]} == {
        ("net.parent: netlink NETLINK_ROUTE RTM_GETADDR", 1, host.MISSING), ("net.socket: netlink NETLINK_ROUTE RTM_GETADDR", host.MISSING, 1)}


def test_a_public_destination_only_the_candidate_reaches_is_red_and_the_declared_list_does_not_cover_it():
    c = with_net(a_candidate_without_gfxtest(), PARENT_NET + ["socket: connect tcp AF_INET 8.8.4.4:80"])
    report = judge(with_net(footprint(), PARENT_NET), c, expected=expected())
    assert report["verdict"] == "red" and [d["id"] for d in report["declared"]] == ["gfxtest-69", "gfxtest-69"]
    assert [(d["seed"], d["leaf"], d["a"], d["b"]) for d in report["differences"]] == [
        (11, "net.socket: connect tcp public:80", host.MISSING, 1), (12, "net.socket: connect tcp public:80", host.MISSING, 1)]


def test_loopback_and_lab_destinations_are_not_compared_and_are_counted_as_evidence(capsys):
    a = with_net(footprint(), PARENT_NET, skipped={"loopback": 40, "lab": 2})
    b = with_net(footprint("again"), PARENT_NET, skipped={"loopback": 57})
    report = judge(a, b, a_a=True)
    assert report["verdict"] == "green" and report["differences"] == []
    assert report["net"]["covered"] and report["net"]["counts"]["A"]["11"] == {"lab": 2, "loopback": 40}
    assert set(report["net"]["not_compared"]) == {"loopback", "lab", "private", "multicast"}
    host.print_report(report, "firefox-68", "firefox-68 again")
    assert "NET (not compared) firefox-68 seed 11: lab 2, loopback 40" in capsys.readouterr().out


def test_identical_network_sets_are_green_and_are_counted_as_leaves():
    report = judge(with_net(footprint(), PARENT_NET), with_net(footprint("again"), PARENT_NET), a_a=True)
    assert report["verdict"] == "green" and report["leaves"]["11"] == {"proc": 4, "file": 3, "net": len(PARENT_NET)}


def test_a_network_entry_only_the_warm_up_had_is_listed_not_red(capsys):
    a = with_warmup(with_net(footprint(), PARENT_NET), kinds=KINDS)
    a["warmup"]["net"] = PARENT_NET + ["parent: connect tcp AF_INET 8.8.4.4:80"]
    report = judge(a, with_net(footprint("again"), PARENT_NET), a_a=True)
    assert report["verdict"] == "green" and report["warmup"]["A"]["net"] == ["parent: connect tcp AF_INET 8.8.4.4:80"]
    host.print_report(report, "firefox-68", "firefox-68 again")
    assert "WARMUP-ONLY firefox-68 seed 11 net.parent: connect tcp AF_INET 8.8.4.4:80" in capsys.readouterr().out


def test_on_windows_the_network_is_said_not_covered_with_the_reason(capsys):
    report = judge(footprint(platform="win32"), footprint("again", platform="win32"), a_a=True)
    assert report["verdict"] == "green" and report["net"] == {"covered": False, "reason": host.NET_WINDOWS}
    assert "Microsoft-Windows-Kernel-Network" in host.NET_WINDOWS and "GetAdaptersAddresses" in host.NET_WINDOWS
    host.print_report(report, "firefox-68", "firefox-68 again")
    assert "NET NOT COVERED: not covered on Windows." in capsys.readouterr().out


def test_the_stand_in_traces_the_network_only_when_the_host_oracle_asks(tmp_path, monkeypatch):
    import _strace_stand_in as stand_in

    class Help:
        stdout = "strace --kill-on-exit"

    monkeypatch.setattr(stand_in.shutil, "which", lambda name: "/usr/bin/strace")
    monkeypatch.setattr(stand_in.subprocess, "run", lambda *a, **k: Help())
    (tmp_path / "ff").mkdir()
    binary = tmp_path / "ff" / "firefox"
    binary.write_text("", encoding="utf-8")
    font = Path(stand_in._stand_in(str(binary), tmp_path / "font", tmp_path / "f.log", tmp_path / "f.pid")).read_text(encoding="utf-8")
    oracle = Path(stand_in._stand_in(str(binary), tmp_path / "host", tmp_path / "h.log", tmp_path / "h.pid", syscalls=host.STRACE_CALLS,
                                     fd_paths=True, network=True)).read_text(encoding="utf-8")
    assert "-e trace=%s " % stand_in._SYSCALLS in font and "socket" not in font, "the font tests' default is unchanged"
    assert "-e trace=%s,%s " % (host.STRACE_CALLS, stand_in._NET_SYSCALLS) in oracle and " -y" in oracle
    assert set(stand_in._NET_SYSCALLS.split(",")) == set(host.NET_CALLS)


# -
# The geolocation step: our engine starts no Windows location utility process and loads no location module; stock Firefox, the control, must load the first read of the
# host for the position (`capabilityaccessmanagerclient.dll`).

LOCATION_CMD = '"%s" -contentproc --channel="100.7.8" -sandboxingKind 3 -notForBrowser utility' % FIREFOX
WIN_FILES = ["c:\\windows\\system32\\kernel32.dll", "c:\\windows\\system32\\ntdll.dll", "c:\\windows\\system32\\user32.dll"]
WIN_KINDS = {"parent": 1, "tab/isForBrowser": 2, "utility/sandboxingKind=1/notForBrowser": 1}
LOCATION_KIND = "utility/sandboxingKind=3/notForBrowser"


def windows_footprints(candidate_kinds=None, candidate_files=None, engine="candidate"):
    published = footprint(platform="win32", kinds=WIN_KINDS, files=WIN_FILES)
    return published, footprint(engine, platform="win32", kinds=candidate_kinds or WIN_KINDS, files=candidate_files or WIN_FILES)


def test_the_location_utility_process_is_the_kind_3_of_a_real_windows_command_line():
    events = host.parse_audit_events(audit(18, 108, 100, FIREFOX, LOCATION_CMD))
    label = host.windows_processes(events, FIREFOX)[108]["label"]
    assert label == LOCATION_KIND
    assert host.location_utility_kinds({"parent": 1, label: 1, "utility/sandboxingKind=1/notForBrowser": 1, "utility/sandboxingKind=30/notForBrowser": 1}) == [LOCATION_KIND]
    assert host.LOCATION_UTILITY_KIND == "3"


def test_a_candidate_that_starts_the_location_utility_process_is_red_on_windows_and_says_so():
    published, candidate = windows_footprints(candidate_kinds=dict(WIN_KINDS, **{LOCATION_KIND: 1}))
    report = judge(published, candidate, expected=[])
    assert report["verdict"] == "red"
    messages = [p for p in report["problems"] if "location utility process (-sandboxingKind 3)" in p]
    assert len(messages) == len(SEEDS) and all("side B" in m and LOCATION_KIND in m for m in messages)


def test_the_rule_is_absolute_the_published_engine_having_the_process_too_does_not_excuse_the_candidate():
    both = dict(WIN_KINDS, **{LOCATION_KIND: 1})
    report = judge(footprint(platform="win32", kinds=both, files=WIN_FILES), footprint("candidate", platform="win32", kinds=both, files=WIN_FILES), expected=[])
    assert report["differences"] == [] and report["verdict"] == "red"


def test_a_candidate_that_loads_a_location_module_is_red_whatever_the_spelling_of_the_path():
    for path in ("C:\\Windows\\System32\\GeoLocation.dll", "c:\\windows\\system32\\locationframeworkps.dll", "C:\\Windows\\System32\\wlanapi.dll",
                 "c:\\windows\\system32\\capabilityaccessmanagerclient.dll", "c:\\windows\\system32\\locationapi.dll",
                 "c:\\windows\\system32\\windows.devices.geolocation.dll", "c:\\windows\\system32\\Windows.Devices.GeolocationExt.dll"):
        published, candidate = windows_footprints(candidate_files=WIN_FILES + [path])
        published["seeds"] = [dict(s, files=WIN_FILES + [path]) for s in published["seeds"]]
        report = judge(published, candidate, expected=[])
        assert report["verdict"] == "red" and any("loaded location modules" in p and path.rsplit("\\", 1)[-1] in p for p in report["problems"]), path


def test_a_neighbour_of_the_location_modules_is_not_one():
    assert host.location_modules(["c:\\windows\\system32\\geolocation.dll.mui", "c:\\windows\\system32\\location.dll", "c:\\lab\\geolocation.json"]) == []


def test_the_utility_process_of_the_media_decoder_is_not_the_location_one():
    published, candidate = windows_footprints()
    assert judge(published, candidate, expected=[])["verdict"] == "green"


def test_the_warm_up_launch_of_the_candidate_is_checked_too():
    published, candidate = windows_footprints()
    candidate["warmup"] = dict(seed_footprint(11, kinds=dict(WIN_KINDS, **{LOCATION_KIND: 1}), files=WIN_FILES), seed=11)
    report = judge(published, candidate, expected=[])
    assert report["verdict"] == "red" and any(p.startswith("warm-up side B: the engine started the Windows location utility process") for p in report["problems"])


def test_the_rule_is_for_our_engine_on_windows_both_sides_of_the_a_a_and_the_candidate_of_the_a_b_not_for_linux():
    published, candidate = windows_footprints(candidate_kinds=dict(WIN_KINDS, **{LOCATION_KIND: 1}))
    a_a = judge(candidate, copy.deepcopy(candidate), a_a=True)
    for side in ("A", "B"):
        assert sum(" side %s: the engine started the Windows location utility process" % side in p for p in a_a["problems"]) == len(SEEDS), side
    side_a = judge(candidate, published, expected=[])
    assert not any("side B: the engine started" in p for p in side_a["problems"])
    on_linux = judge(footprint(), footprint("candidate", kinds=dict(KINDS, **{LOCATION_KIND: 1})), expected=[])
    assert not any("location" in p for p in on_linux["problems"])


def a_stock(kinds, files=(), steps=None, problems=()):
    return {"format": host.FORMAT, "engine": "stock", "platform": "win32",
            "stock": {"seed": 0, "steps": steps if steps is not None else [{"n": 5, "name": host.GEO_STEP, "ok": True}], "kinds": kinds, "files": list(files),
                      "problems": list(problems)}}


FIRST_READ = "capabilityaccessmanagerclient.dll"
FIRST_READ_PATH = "c:\\windows\\system32\\capabilityaccessmanagerclient.dll"


def test_stock_that_loads_the_first_read_of_the_host_for_the_position_is_a_working_control():
    stock = a_stock(dict(WIN_KINDS, **{LOCATION_KIND: 1}), files=["c:\\windows\\system32\\geolocation.dll", FIRST_READ_PATH])
    assert host.stock_control_problems(stock) == []
    assert host.stock_control_problems(a_stock(WIN_KINDS, files=["C:\\Windows\\System32\\CapabilityAccessManagerClient.dll"])) == []


def test_stock_that_does_not_load_it_is_a_broken_control_and_says_the_step_does_not_reach_the_platform():
    problems = host.stock_control_problems(a_stock(WIN_KINDS))
    assert len(problems) == 1 and "did not load %s" % FIRST_READ in problems[0]
    assert "does not reach the platform" in problems[0]


def test_a_stock_run_that_failed_or_was_not_traced_is_a_broken_control_too():
    kinds = dict(WIN_KINDS, **{LOCATION_KIND: 1})
    assert host.stock_control_problems(a_stock(kinds, files=[FIRST_READ_PATH], steps=[{"n": 5, "name": host.GEO_STEP, "ok": False, "error": "timeout"}])) == [
        "stock control: step 5 (%s) failed: timeout" % host.GEO_STEP]
    assert host.stock_control_problems(a_stock(kinds, files=[FIRST_READ_PATH], problems=["the file trace lost 3 events"])) == ["stock control: the file trace lost 3 events"]
    assert host.stock_control_problems({"stock": {}}) == ["stock control: the run made no footprint"]


# The signal of the stock control is the first read of the host that Firefox makes for the position, not the utility process of the provider. Real footprints, reduced
# to what the rules read (tests/data/ab_host_geo/*/SOURCES.md): the stock of the gate republish 45257636942 (windows-2022: the module is loaded, the utility process is
# not), the stock of the pull request 45165805370 (windows-2022: both), and the candidate of 45257636942 (neither, in any launch).
GEO_DATA = Path(__file__).resolve().parent / "data" / "ab_host_geo"


def geo_fixture(run, name):
    return json.loads((GEO_DATA / run / name).read_text(encoding="utf-8"))


def without_first_read(stock):
    stock = copy.deepcopy(stock)
    stock["stock"]["files"] = [f for f in stock["stock"]["files"] if not f.endswith("\\" + FIRST_READ)]
    stock["stock"]["touched_by"] = {}
    return stock


def test_the_stock_of_45257636942_loaded_the_first_read_without_the_utility_process_and_the_control_is_working():
    """Run 45257636942 (windows-2022): the stock loaded `capabilityaccessmanagerclient.dll` and started no `-sandboxingKind 3` process (the runner has no location source: the
    provider answers `code 2`). The old control asked for the process and was broken; the first read is in every stock footprint measured (45165805370, 45170547415,
    45252267809, 45206303882, 45257636942), the process in 3 of the 5."""
    stock = geo_fixture("45257636942", "footprint-stock.json")
    assert host.location_first_reads(stock["stock"]["files"]) == [FIRST_READ_PATH]
    assert host.location_utility_kinds(stock["stock"]["kinds"]) == []
    assert host.stock_control_problems(stock) == []


def test_the_stock_of_45165805370_loaded_the_first_read_and_started_the_utility_process_and_the_control_is_working():
    stock = geo_fixture("45165805370", "footprint-stock.json")
    assert host.location_first_reads(stock["stock"]["files"]) == [FIRST_READ_PATH]
    assert host.location_utility_kinds(stock["stock"]["kinds"]) == ["utility/sandboxingKind=3"]
    assert host.stock_control_problems(stock) == []


def test_a_stock_that_did_not_load_the_first_read_is_a_broken_control_that_names_the_module_whatever_the_utility_process():
    for run in ("45257636942", "45165805370"):
        problems = host.stock_control_problems(without_first_read(geo_fixture(run, "footprint-stock.json")))
        assert len(problems) == 1 and FIRST_READ in problems[0] and "first read of the host for the position" in problems[0], run
        assert "does not reach the platform" in problems[0]
    # the utility process is not the signal: with it, and without the module, the control is broken all the same
    with_process = without_first_read(geo_fixture("45165805370", "footprint-stock.json"))
    assert host.location_utility_kinds(with_process["stock"]["kinds"]) == ["utility/sandboxingKind=3"]
    assert len(host.stock_control_problems(with_process)) == 1


def test_a_stock_control_says_on_the_output_which_module_is_missing_or_that_it_loaded_it(tmp_path, monkeypatch, capsys):
    broken = without_first_read(geo_fixture("45257636942", "footprint-stock.json"))
    out = tmp_path / "o"
    out.mkdir()
    monkeypatch.setattr(host, "run_stock_subprocess", lambda leg, path: host.write_json(path, broken) or 0)
    assert host.run_stock_control("windows-x86_64", out) is None
    printed = capsys.readouterr().out
    assert "BROKEN CONTROL" in printed and "%s is not loaded" % FIRST_READ in printed and "stock evidence: location modules loaded: wlanapi.dll" in printed
    working = geo_fixture("45257636942", "footprint-stock.json")
    monkeypatch.setattr(host, "run_stock_subprocess", lambda leg, path: host.write_json(path, working) or 0)
    assert host.run_stock_control("windows-x86_64", out) == working
    assert "GEOLOCATION CONTROL: stock Firefox loaded %s; location utility process: none" % FIRST_READ in capsys.readouterr().out


def geo_problems(report):
    return [p for p in report["problems"] if "location" in p]


def test_the_candidate_of_45257636942_is_not_red_for_the_geolocation_rule_in_any_launch():
    candidate = geo_fixture("45257636942", "footprint-candidate.json")
    for launch in [candidate["warmup"], candidate["idle"]] + candidate["seeds"]:
        assert host.location_first_reads(launch["files"]) == [] and host.location_utility_kinds(launch["kinds"]) == []
    assert geo_problems(judge(copy.deepcopy(candidate), candidate, floors={}, expected=[])) == []


@pytest.mark.parametrize("launch", ["warmup", "idle", "seed 0", "seed 1"])
def test_a_candidate_that_loads_the_first_read_in_one_launch_is_red_and_names_the_launch_and_the_module(launch):
    """The idle launch was not looked at by the old rule (it was in no launch of the candidates measured, so nothing showed it)."""
    candidate = geo_fixture("45257636942", "footprint-candidate.json")
    published = copy.deepcopy(candidate)
    target = candidate[launch] if launch in ("warmup", "idle") else candidate["seeds"][int(launch.split()[1])]
    target["files"] = sorted(target["files"] + [FIRST_READ_PATH])
    problems = geo_problems(judge(published, candidate, floors={}, expected=[]))
    label = {"warmup": "warm-up", "idle": "idle"}.get(launch, "seed %s" % target["seed"])
    assert len(problems) == 1, problems
    assert problems[0].startswith("%s side B: the engine loaded location modules: %s" % (label, FIRST_READ_PATH)) and "first read of the host" in problems[0]


def test_the_a_a_is_red_for_the_geolocation_rule_on_either_side_and_the_a_b_judges_the_candidate_only():
    """The 68 has neither the module nor the process in any launch measured (45121964904, 45206303882, 45257629988, 45257636942): the rule is on our engine, so on both
    sides of the A/A. In the A/B only the candidate (side B) is judged: side A is the published engine, which the A/A of the same job has judged."""
    engine = geo_fixture("45257636942", "footprint-candidate.json")
    assert geo_problems(judge(copy.deepcopy(engine), copy.deepcopy(engine), a_a=True, floors={}, expected=[])) == []
    first = copy.deepcopy(engine)
    first["idle"]["files"] = sorted(first["idle"]["files"] + [FIRST_READ_PATH])
    problems = geo_problems(judge(first, copy.deepcopy(engine), a_a=True, floors={}, expected=[]))
    assert len(problems) == 1 and problems[0].startswith("idle side A: the engine loaded location modules: %s" % FIRST_READ_PATH)
    second = copy.deepcopy(engine)
    second["seeds"][0]["kinds"] = dict(second["seeds"][0]["kinds"], **{LOCATION_KIND: 1})
    problems = geo_problems(judge(copy.deepcopy(engine), second, a_a=True, floors={}, expected=[]))
    assert len(problems) == 1 and problems[0].startswith("seed %s side B: the engine started the Windows location utility process" % second["seeds"][0]["seed"])
    assert geo_problems(judge(first, copy.deepcopy(engine), floors={}, expected=[])) == [], "the A/B does not judge the published engine"


class FakePage:
    def __init__(self, calls):
        self.calls = calls

    def goto(self, url):
        self.calls.append(("goto", url))

    def evaluate(self, script):
        self.calls.append(("evaluate", script))
        return {"answer": "error", "code": 2}


class FakeContext:
    def __init__(self, calls, options):
        self.calls = calls
        calls.append(("new_context", options))

    def grant_permissions(self, permissions, origin=None):
        self.calls.append(("grant_permissions", list(permissions), origin))

    def new_page(self):
        self.calls.append(("new_page",))
        return FakePage(self.calls)

    def close(self):
        self.calls.append(("close",))


class FakeBrowser:
    def __init__(self):
        self.calls = []

    def new_context(self, **options):
        return FakeContext(self.calls, options)


def test_the_step_grants_the_permission_to_the_lab_page_then_opens_it_then_asks_and_awaits_the_answer():
    browser = FakeBrowser()
    step = host.geolocation_step(browser, "http://127.0.0.1:9", host.GEO_DECLARED, 5)
    assert [c[0] for c in browser.calls] == ["new_context", "grant_permissions", "new_page", "goto", "evaluate", "close"]
    assert browser.calls[0][1] == {"geolocation": host.GEO_DECLARED}
    assert browser.calls[1][1:] == (["geolocation"], "http://127.0.0.1:9")
    assert browser.calls[3][1] == "http://127.0.0.1:9/geo"
    assert step == {"n": 5, "name": host.GEO_STEP, "ok": True, "geo": {"answer": "error", "code": 2}}


def test_the_stock_control_declares_no_position_so_that_it_has_to_ask_the_platform():
    browser = FakeBrowser()
    host.geolocation_step(browser, "http://127.0.0.1:9", None, 5)
    assert browser.calls[0] == ("new_context", {})


def test_the_request_ends_with_the_callback_or_the_error_and_no_timer_of_the_page_decides():
    assert "getCurrentPosition" in host.GEO_READ and "position =>" in host.GEO_READ and "error =>" in host.GEO_READ
    assert "setTimeout" not in host.GEO_READ and "sleep" not in host.GEO_READ.lower()


def test_a_step_that_threw_is_recorded_as_failed_and_closes_its_context():
    class Throws(FakePage):
        def evaluate(self, script):
            raise RuntimeError("page crashed\nmore")

    class B(FakeBrowser):
        def new_context(self, **options):
            context = FakeContext(self.calls, options)
            context.new_page = lambda: Throws(self.calls)
            return context

    browser = B()
    step = host.geolocation_step(browser, "http://127.0.0.1:9", None, 5)
    assert step["ok"] is False and step["error"] == "page crashed" and browser.calls[-1] == ("close",)


def test_the_journey_has_the_geolocation_step_after_its_own_steps_in_a_run():
    source = Path(host.__file__).read_text(encoding="utf-8")
    run_seed = source[source.index("def run_seed("):source.index("def _wait_for_tree_exit(")]
    assert run_seed.index("for n, (name, method) in enumerate(JOURNEY, 1)") < run_seed.index("geolocation_step(browser, geo_origin, GEO_DECLARED, len(JOURNEY) + 1)")
    assert run_seed.index("geolocation_step(") < run_seed.rindex("context.close()")  # the close of the page launch


def test_the_lab_page_of_the_step_is_served_by_the_oracle_itself():
    import urllib.request

    origin, stop = host.start_geo_lab()
    try:
        assert origin.startswith("http://127.0.0.1:")
        with urllib.request.urlopen(origin + host.GEO_PATH, timeout=60) as response:
            assert response.status == 200 and b"<title>geo</title>" in response.read()
    finally:
        stop()


def test_a_windows_ab_runs_the_stock_control_after_the_a_a_and_before_the_verdict_and_a_broken_one_is_exit_2(tmp_path, monkeypatch):
    order = []
    pub = fake_engine(tmp_path / "fetch", "pub")
    cand = fake_engine(tmp_path / "checkout", "cand")
    monkeypatch.setattr(host, "_windows", lambda: True)
    monkeypatch.setattr(host.oracle, "fetch_published", lambda tag, work: (pub, "pub-seal"))
    monkeypatch.setattr(host, "print_report", lambda *a, **k: None)
    monkeypatch.setattr(host, "load_expected", lambda path: [])
    stock = {"doc": a_stock(WIN_KINDS)}

    def fake_run(exe, seal, leg, out, label, seeds):
        order.append(("run", label))
        host.write_json(out, footprint(label, platform="win32"))
        return 0

    def fake_stock(leg, out):
        order.append(("stock", leg))
        host.write_json(out, stock["doc"])
        return 0

    def fake_judge(a, b, **kw):
        order.append(("judge", "a-a" if kw.get("a_a") else "a-b"))
        return {"verdict": "green", "problems": [], "not_judged": {}}

    monkeypatch.setattr(host, "run_subprocess", fake_run)
    monkeypatch.setattr(host, "run_stock_subprocess", fake_stock)
    monkeypatch.setattr(host, "judge", fake_judge)
    assert host.ab(cand, Path("seal"), "windows-x86_64", tmp_path / "o1", "firefox-68", [11]) == 2
    assert order == [("run", "firefox-68"), ("run", "firefox-68 again"), ("run", "candidate"), ("judge", "a-a"), ("stock", "windows-x86_64")]
    order.clear()
    stock["doc"] = a_stock(dict(WIN_KINDS, **{LOCATION_KIND: 1}), files=[FIRST_READ_PATH])
    assert host.ab(cand, Path("seal"), "windows-x86_64", tmp_path / "o2", "firefox-68", [11]) == 0
    assert order[-2:] == [("stock", "windows-x86_64"), ("judge", "a-b")]
    order.clear()
    assert host.ab(None, None, "windows-x86_64", tmp_path / "o3", "firefox-68", [11]) == 0
    assert order == [("run", "firefox-68"), ("run", "firefox-68 again"), ("judge", "a-a"), ("stock", "windows-x86_64")], "no candidate: the control of the geolocation step runs all the same"


def test_a_windows_run_with_no_candidate_runs_the_stock_control_after_a_green_a_a_and_a_broken_one_is_red(tmp_path, monkeypatch):
    """Run 45121964904 (the gate of the series): the control of the geolocation step ran only with a candidate, so a pull request never ran it."""
    pub = fake_engine(tmp_path / "fetch", "pub")
    monkeypatch.setattr(host, "_windows", lambda: True)
    monkeypatch.setattr(host.oracle, "fetch_published", lambda tag, work: (pub, "pub-seal"))
    monkeypatch.setattr(host, "print_report", lambda *a, **k: None)
    seen = []
    monkeypatch.setattr(host, "run_subprocess", lambda exe, seal, leg, out, label, seeds: host.write_json(out, footprint(label, platform="win32")) or 0)
    monkeypatch.setattr(host, "judge", lambda a, b, **kw: {"verdict": "green", "problems": [], "not_judged": {}})
    monkeypatch.setattr(host, "run_stock_control", lambda leg, out: seen.append(leg) or {"stock": True})
    assert host.ab(None, None, "windows-x86_64", tmp_path / "o1", "firefox-68", [11]) == 0 and seen == ["windows-x86_64"]
    monkeypatch.setattr(host, "run_stock_control", lambda leg, out: None)
    assert host.ab(None, None, "windows-x86_64", tmp_path / "o2", "firefox-68", [11]) == 2, "a broken control is red with no candidate too"
    monkeypatch.setattr(host, "_windows", lambda: False)
    seen.clear()
    monkeypatch.setattr(host, "run_stock_control", lambda leg, out: seen.append(leg) or {"stock": True})
    assert host.ab(None, None, "linux-x86_64", tmp_path / "o3", "firefox-68", [11]) == 0 and seen == [], "Linux has no Windows location provider: no control"


def fake_playwright(monkeypatch, executable, launch=None):
    """`playwright.sync_api.sync_playwright` whose Firefox has this executable path (and, if given, this launch)."""
    import types
    from contextlib import contextmanager

    class Firefox:
        executable_path = str(executable)

        def launch(self, **kw):
            return launch()

    @contextmanager
    def sync_playwright():
        yield types.SimpleNamespace(firefox=Firefox())

    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = sync_playwright
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)


class FakeTrace:
    def __init__(self, work):
        pass

    def start(self):
        pass

    def cleanup(self):
        pass


def test_a_stock_firefox_that_is_not_installed_is_said_by_name_and_is_not_blamed_on_the_platform_provider(tmp_path, monkeypatch, capsys):
    """Run 45121964904: `Executable doesn't exist at ...firefox-1532...firefox.exe` ended the stock run and the oracle printed that the step does not reach the provider."""
    missing = tmp_path / "ms-playwright" / "firefox-1532" / "firefox" / "firefox.exe"
    fake_playwright(monkeypatch, missing)
    monkeypatch.setattr(host, "WindowsTrace", FakeTrace)
    monkeypatch.setattr(host, "start_geo_lab", lambda: ("http://127.0.0.1:1", lambda: None))
    document = host.run_stock("windows-x86_64")
    text = "\n".join(host.stock_control_problems(document))
    assert "is not installed" in text and str(missing) in text and "python -m playwright install firefox" in text
    assert "does not reach the platform" not in text and "did not load %s" % FIRST_READ not in text
    # and the control says the same on the output
    out = tmp_path / "o"
    out.mkdir()
    monkeypatch.setattr(host, "run_stock_subprocess", lambda leg, path: host.write_json(path, document) or 0)
    assert host.run_stock_control("windows-x86_64", out) is None
    printed = capsys.readouterr().out
    assert "is not installed" in printed and "does not reach the platform" not in printed and "BROKEN CONTROL" in printed


def test_a_stock_firefox_that_does_not_launch_says_why_and_one_that_ran_without_the_provider_leaves_its_evidence(tmp_path, monkeypatch, capsys):
    exe = tmp_path / "firefox.exe"
    exe.write_text("x")

    def boom():
        raise RuntimeError("BrowserType.launch: Timeout 180000ms exceeded.\nCall log: ...")

    fake_playwright(monkeypatch, exe, launch=boom)
    monkeypatch.setattr(host, "WindowsTrace", FakeTrace)
    monkeypatch.setattr(host, "start_geo_lab", lambda: ("http://127.0.0.1:1", lambda: None))
    problems = host.stock_control_problems(host.run_stock("windows-x86_64"))
    assert problems == ["stock control: the stock Firefox of Playwright did not start: BrowserType.launch: Timeout 180000ms exceeded."]
    # started, no provider: the claim is the same as ever, and the log has what the tree did
    stock = a_stock(WIN_KINDS, files=["c:\\windows\\system32\\kernel32.dll", "c:\\windows\\system32\\ntdll.dll"])
    stock["stock"]["processes"] = [{"label": "parent", "cmd": "firefox.exe -no-remote"}]
    out = tmp_path / "o"
    out.mkdir()
    monkeypatch.setattr(host, "run_stock_subprocess", lambda leg, path: host.write_json(path, stock) or 0)
    assert host.run_stock_control("windows-x86_64", out) is None
    printed = capsys.readouterr().out
    assert "did not load %s" % FIRST_READ in printed and "BROKEN CONTROL: the geolocation step does not reach the platform on stock Firefox (%s is not loaded)" % FIRST_READ in printed
    assert "stock evidence: process parent: firefox.exe -no-remote" in printed and "kernel32.dll" in printed and "location modules loaded: none" in printed


def test_the_windows_host_job_installs_playwright_s_firefox_with_the_python_of_the_tests_before_the_oracle_runs():
    """Run 45121964904: the job did not install it and `run_stock` found no executable."""
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github" / "workflows" / "e2e.yml").read_text(encoding="utf-8")
    start = text.index("\n  e2e-host-windows:\n")
    job = text[start:text.index("\n  # ``The page oracle``", start)]
    assert "runs-on: windows-2022" in job and "-m pytest tests/test_ab_host_e2e.py" in job
    install = job.index("python -m playwright install firefox")
    assert job.index("actions/setup-python") < job.index('python -m pip install "playwright==') < install < job.index("-m pytest tests/test_ab_host_e2e.py")
    assert job.count("playwright install") == 1
    diagnostic = job[job.index("      - name: The state of the Windows location service"):]
    diagnostic = diagnostic[:diagnostic.index("      - name: The leg ran")]
    assert "if: always()" in diagnostic and "shell: pwsh" in diagnostic and "Get-Service lfsvc" in diagnostic and "Format-List" in diagnostic
    for key in ("HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\location",
                "HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\location",
                "HKLM:\\SYSTEM\\CurrentControlSet\\Services\\lfsvc\\Service\\Configuration"):
        assert key in diagnostic
    assert not re.search(r"Set-|New-|Remove-|Start-Service|Stop-Service", diagnostic), "read only"


def test_the_stock_command_is_for_windows_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(host, "_windows", lambda: False)
    assert host.main(["stock", "--leg", "linux-x86_64", "--out", str(tmp_path / "s.json")]) == 3
    assert "Linux has no Windows location provider" in capsys.readouterr().out


# -
# The evidence that proves WHEN in the shutdown a process came and HOW the browser ended, and who opens /proc/self/fd/<n>.

SHUTDOWN_LOG = """\
100 execve("/e/firefox/firefox", ["/e/firefox/firefox", "-profile", "/tmp/p"], 0x1) = 0
100 fork() = 101
101 execve("/e/firefox/firefox", ["/e/firefox/firefox", "-contentproc", "-sandboxingKind", "0", "utility"], 0x1) = 0
100 fork() = 102
102 execve("/e/firefox/firefox", ["/e/firefox/firefox", "--backgroundtask", "pingsender", "https://t.example/x", "/tmp/p/ping"], 0x1) = 0
101 +++ exited with 0 +++
100 +++ exited with 0 +++
102 +++ exited with 0 +++
"""


def test_the_lifecycle_says_when_each_process_came_and_how_it_ended_by_the_line_of_the_log():
    lines = SHUTDOWN_LOG.splitlines()
    procs = host.spawned_processes(lines, REAL)
    got = host.process_lifecycle(lines, procs)
    assert [(e["event"], e["kind"], e["how"]) for e in got] == [
        ("start", "parent", ""), ("start", "utility/sandboxingKind=0", ""), ("start", "backgroundtask/pingsender", ""),
        ("end", "utility/sandboxingKind=0", "exited 0"), ("end", "parent", "exited 0"), ("end", "backgroundtask/pingsender", "exited 0")]
    assert [e["line"] for e in got] == sorted(e["line"] for e in got), "the order is the log's"
    killed = host.process_lifecycle(lines[:-3] + ["100 +++ killed by SIGKILL +++"], procs)
    assert killed[-1] == {"line": 5, "event": "end", "kind": "parent", "how": "killed by SIGKILL"}, "a browser that was killed says so"


def test_the_open_of_a_proc_self_fd_names_the_process_the_line_and_the_memfd_that_made_the_descriptor():
    lines = ['100 memfd_create("mozilla-ipc", MFD_CLOEXEC|MFD_ALLOW_SEALING) = 31</memfd:mozilla-ipc (deleted)>',
             '100 openat(AT_FDCWD, "/proc/self/fd/31", O_RDONLY|O_CLOEXEC) = 32</memfd:mozilla-ipc (deleted)>',
             '100 openat(AT_FDCWD, "/proc/self/fd/40", O_RDONLY|O_CLOEXEC) = -1 ENOENT',
             '100 openat(AT_FDCWD, "/etc/hosts", O_RDONLY) = 3']
    got = host.proc_fd_opens(lines, {}, lambda pid: "parent")
    assert [(o["pid"], o["kind"], o["made_by"] is not None) for o in got] == [(100, "parent", True), (100, "parent", False)]
    assert "memfd_create(\"mozilla-ipc\"" in got[0]["made_by"] and "/proc/self/fd/31" in got[0]["line"]
    assert host.STRACE_CALLS.split(",").count("memfd_create") == 1


def test_the_strace_stand_in_keeps_the_exit_lines_and_records_the_fatal_signals_when_asked(monkeypatch):
    import _strace_stand_in as stand_in

    class Help:
        stdout = "strace --kill-on-exit"

    monkeypatch.setattr(stand_in.shutil, "which", lambda name: "/usr/bin/strace")
    monkeypatch.setattr(stand_in.subprocess, "run", lambda *a, **k: Help())
    assert " -qq " in stand_in._strace_options() + " " and "-e signal" not in stand_in._strace_options()
    asked = stand_in._strace_options(exit_status=True)
    assert "-q " in asked and "-qq" not in asked.replace("SIGQUIT", ""), "-q keeps the `+++ exited with N +++` lines"
    assert "-e signal=none" not in asked and "-e signal=%s " % stand_in.RECORDED_SIGNALS in asked + " ", "the signals are recorded now: the line that says who sent SIGSEGV and where it faulted"
    assert {"SIGSEGV", "SIGBUS", "SIGABRT", "SIGILL", "SIGTERM"} <= set(stand_in.RECORDED_SIGNALS.split(","))


def test_the_signal_lines_the_trace_now_records_are_not_data_of_the_comparison(tmp_path):
    """The known-bad input is a log with the lines `-e signal=none` used to drop: read as data they would be a process, a path, a call. They are dropped where the log is read, and
    the footprint of a log with them is the footprint of the log without."""
    clean = tmp_path / "clean.log"
    clean.write_text(SHUTDOWN_LOG, encoding="utf-8")
    lines = SHUTDOWN_LOG.splitlines()
    noisy = tmp_path / "noisy.log"
    signals = ["100 --- SIGCHLD {si_signo=SIGCHLD, si_code=CLD_EXITED, si_pid=104, si_uid=1000, si_status=0, si_utime=0, si_stime=0} ---",
               "100 --- SIGSEGV {si_signo=SIGSEGV, si_code=SEGV_MAPERR, si_addr=0x7f00dead} ---"]
    noisy.write_text("\n".join([lines[0]] + signals + lines[1:]) + "\n", encoding="utf-8")
    assert list(host._read_lines(noisy)) == list(host._read_lines(clean)) and not any("---" in x for x in host._read_lines(noisy))
    assert any("--- SIGSEGV" in x for x in noisy.read_text(encoding="utf-8").splitlines()), "the log itself keeps them: the evidence of a crash reads it"


def etw_stop(pid, code, time, image="firefox.exe"):
    return ('<Event %s><System><Provider Name="Microsoft-Windows-Kernel-Process"/><EventID>2</EventID><TimeCreated SystemTime="%s"/><Execution ProcessID="4" ThreadID="8"/></System>'
            '<EventData><Data Name="ProcessID">%d</Data><Data Name="ExitCode">%d</Data><Data Name="ImageName">%s</Data></EventData></Event>' % (NS, time, pid, code, image))


def test_the_process_stops_of_the_etw_file_are_read_with_their_exit_code(tmp_path):
    xml = tmp_path / "files.xml"
    xml.write_text("<Events>" + etw((100, "C:\\x.dll")).replace("<Events>", "").replace("</Events>", "")
                   + etw_stop(102, 0, "2026-11-22T10:00:02.5Z") + etw_stop(103, 1, "2026-11-22T10:00:03.1Z") + "</Events>", encoding="utf-8")
    exits = []
    seen, events = host.etw_files(xml, {100}, exits)
    assert seen == {(100, "C:\\x.dll")} and events == 3
    assert exits == [{"pid": 102, "code": 0, "time": "2026-11-22T10:00:02.5Z", "image": "firefox.exe"},
                     {"pid": 103, "code": 1, "time": "2026-11-22T10:00:03.1Z", "image": "firefox.exe"}]
    assert host.etw_files(xml, {100})[0] == seen, "the caller that asks for no exits gets what it did"


def test_the_windows_lifecycle_orders_the_starts_and_the_stops_by_their_time_and_says_how_each_ended():
    stamped = [dict(e, time="2026-11-22T10:00:0%d.0Z" % i) for i, e in enumerate(host.process_tree(host.parse_audit_events("\n".join(TREE)), {100}))]
    procs = host.windows_processes(stamped, FIREFOX)
    exits = [{"pid": 107, "code": 0, "time": "2026-11-22T10:00:09.0Z", "image": "pingsender.exe"}, {"pid": 100, "code": 1, "time": "2026-11-22T10:00:08.0Z", "image": "firefox.exe"}]
    got = host.windows_lifecycle(stamped, exits, procs)
    assert [(e["event"], e["kind"], e["how"]) for e in got][-2:] == [("end", "parent", "exited 1"), ("end", "pingsender.exe", "exited 0")]
    assert got[0]["event"] == "start" and got[0]["kind"] == "parent" and len(got) == len(stamped) + 2


def test_a_windows_session_that_cannot_add_the_process_provider_says_so_and_is_not_red(tmp_path, monkeypatch):
    fake = FakeTools(tmp_path, TREE, TREE_FILES)

    def refusing(argv, timeout=300.0):
        if argv[:3] == ["logman", "update", "trace"]:
            raise host.TraceError("logman update failed")
        return fake(argv, timeout)

    monkeypatch.setattr(host, "_tool", refusing)
    trace = host.WindowsTrace(tmp_path)
    trace.start()
    got = trace.finish({100}, alive=lambda pid: False, sleep=lambda s: None)
    assert got["exits_available"] is False and got["problems"] == []
    trace, _fake = a_windows_run(tmp_path, monkeypatch, TREE_FILES)
    assert trace.exits_ok is True and trace.finish({100}, alive=lambda pid: False, sleep=lambda s: None)["exits_available"] is True


def test_a_closed_linux_run_keeps_the_lifecycle_the_close_and_the_proc_fd_opens(tmp_path, monkeypatch):
    log = tmp_path / "strace.log"
    log.write_text(SHUTDOWN_LOG + '100 openat(AT_FDCWD, "/proc/self/fd/7", O_RDONLY) = 8\n', encoding="utf-8")
    monkeypatch.setattr(host.os.path, "realpath", lambda p: p)
    monkeypatch.setattr(host, "_context", lambda binary, windows, work: host.Context("linux", app=["/e/firefox"], temp=["/tmp/ab-host-tmp-x"]))
    fp = host._seed_result(REAL, 11, tmp_path, [], log, None, None, False, {"root_returncode": 0, "close_s": 1.5})
    assert fp["close"] == {"root_returncode": 0, "close_s": 1.5}
    assert [e["kind"] for e in fp["lifecycle"] if e["event"] == "start"] == ["parent", "utility/sandboxingKind=0", "backgroundtask/pingsender"]
    assert fp["proc_fd_opens"][0]["kind"] == "parent" and "/proc/self/fd/7" in fp["proc_fd_opens"][0]["line"]


# -
# The idle launch: the launch in which the exit pings MUST leave (the precondition is seen in the profile, then the browser closes).

def with_idle(fp, n=3, types=("event", "first-shutdown", "new-profile"), parent="parent", observed=True, net=None):
    fp = copy.deepcopy(fp)
    idle = seed_footprint(11, kinds=dict(KINDS, **{host.PINGSENDER_KIND: n}))
    idle["pingsenders"] = [{"type": types[i % len(types)], "form": "backgroundtask", "parent": parent} for i in range(n)]
    idle["net"] = list(net or [])
    idle["idle"] = {"notified": {"observed": observed, "polls": 7}}
    fp["idle"] = idle
    return fp


def test_the_idle_launch_is_green_when_the_notification_was_seen_and_the_three_pings_left_by_the_background_task_of_the_parent(capsys):
    a, b = with_idle(footprint()), with_idle(footprint("firefox-68 again"))
    report = judge(a, b, a_a=True)
    assert report["verdict"] == "green", report["problems"]
    assert report["idle"]["A"] == {"notified": {"observed": True, "polls": 7}, "pingsenders": 3, "types": ["event", "first-shutdown", "new-profile"],
                                   "parents": ["parent"]}
    host.print_report(report, "firefox-68", "firefox-68 again")
    assert "IDLE (the pings must leave) firefox-68: notification" in capsys.readouterr().out
    assert judge(footprint(), footprint("again"), a_a=True)["idle"] == {"A": None, "B": None}, "a footprint with no idle launch has nothing to compare"


@pytest.mark.parametrize("kwargs", [{"observed": False}, {"n": 0}, {"n": 1, "types": ("new-profile",)}, {"n": 2}, {"n": 5}, {"types": ("event", "event", "new-profile")}, {"parent": "gpu"}])
def test_the_idle_launch_holds_no_rule_on_the_notification_or_on_how_many_pingsenders_it_started(kwargs):
    """Run 44993377458: the notification is never recorded (the core declares the terms of use accepted, so canUpload is true from the start) and the long launch
    started 1 pingsender on Linux and 0 on Windows: what leaves is evidence until it is proven."""
    report = judge(with_idle(footprint(), **kwargs), with_idle(footprint("firefox-68 again")), a_a=True)
    assert report["verdict"] == "green", report["problems"]
    assert report["idle"]["A"]["pingsenders"] == kwargs.get("n", 3)


def test_the_separate_pingsender_executable_in_the_idle_launch_is_red_and_a_side_with_no_idle_launch_is_red():
    fp = with_idle(footprint())
    fp["idle"]["kinds"]["pingsender.exe"] = 1
    assert any("idle launch of side A: the separate pingsender.exe ran (B266" in p for p in judge(fp, with_idle(footprint("again")), a_a=True)["problems"])
    assert any("side B has no idle launch" in p for p in judge(with_idle(footprint()), footprint("again"), a_a=True)["problems"])


def test_the_notification_is_waited_for_in_the_profile_until_it_is_recorded_or_the_limit():
    texts = iter([None, "user_pref(\"a\", 1);", "user_pref(\"%s\", \"1\");" % host.NOTIFIED_PREF])
    clock = iter(range(1000))
    got = host.wait_for_notification(lambda: next(texts), sleep=lambda s: None, now=lambda: next(clock))
    assert got == {"observed": True, "polls": 3}
    clock = iter(range(0, 10 ** 6, 50))
    never = host.wait_for_notification(lambda: "user_pref(\"a\", 1);", limit_s=120, sleep=lambda s: None, now=lambda: next(clock))
    assert never["observed"] is False and never["polls"] >= 2


def test_the_prefs_read_are_those_of_the_profile_the_launch_made_not_an_older_one(tmp_path, monkeypatch):
    monkeypatch.setattr(host.tempfile, "gettempdir", lambda: str(tmp_path))
    old = tmp_path / "invisible_profile_old"
    old.mkdir()
    (old / "prefs.js").write_text("user_pref(\"%s\", \"1\");" % host.NOTIFIED_PREF, encoding="utf-8")
    read = host.profile_prefs_reader(set(tmp_path.glob("invisible_profile_*")))
    assert read() is None, "no new profile yet"
    new = tmp_path / "invisible_profile_new"
    new.mkdir()
    assert read() is None, "the profile has no prefs.js yet"
    (new / "prefs.js").write_text("user_pref(\"a\", 1);", encoding="utf-8")
    assert read() == "user_pref(\"a\", 1);"


def test_no_pref_is_changed_and_no_endpoint_is_made_for_the_idle_launch():
    assert not hasattr(host, "PingServer") and "extra_prefs" not in inspect.getsource(host.run_seed) and "toolkit.telemetry.server" not in inspect.getsource(host.run_seed)


def test_the_pingsender_records_say_the_type_the_form_and_the_parent_of_each_process():
    cmd = "C:\\lab\\ff\\firefox.exe --backgroundtask pingsender https://t.example/submit/telemetry/1/new-profile/Firefox/159.0/release/2026?v=4 C:\\p\\ping"
    procs = {7: {"label": host.PINGSENDER_KIND, "cmd": cmd}, 8: {"label": "pingsender.exe", "cmd": "C:\\lab\\ff\\pingsender.exe https://t.example/submit/telemetry/2/event/x"},
             1: {"label": "parent", "cmd": "firefox.exe"}}
    labels = {pid: p["label"] for pid, p in procs.items()}
    assert host.ping_records(procs, labels, {7: 1, 8: 1}, True) == [
        {"type": "event", "form": "pingsender.exe", "parent": "parent"}, {"type": "new-profile", "form": "backgroundtask", "parent": "parent"}]
    linux = {7: {"label": host.PINGSENDER_KIND, "exec": ("/e/firefox", ["firefox", "--backgroundtask", "pingsender", "https://t.example/submit/telemetry/1/first-shutdown/Firefox/1/r", "/p/x"])}}
    assert host.ping_records(linux, {7: host.PINGSENDER_KIND}, {}, False) == [{"type": "first-shutdown", "form": "backgroundtask", "parent": "(oracle)"}]


def test_the_pingsender_destinations_of_the_two_warm_ups_are_compared_when_both_have_the_pingsender_and_left_out_when_one_has_it_and_its_paths_are_listed():
    kinds = dict(KINDS, **{host.PINGSENDER_KIND: 3})
    ping = "backgroundtask/pingsender: connect tcp AF_INET 141.101.90.1:443"
    a = with_warmup(footprint(), kinds=kinds, files=FILES + ["/etc/machine-id"], touched_by={"/etc/machine-id": [host.PINGSENDER_KIND]})
    b = with_warmup(footprint("again"), kinds=kinds, files=FILES + ["/etc/resolv.conf"], touched_by={"/etc/resolv.conf": [host.PINGSENDER_KIND]})
    a["warmup"]["net"] = [ping]
    report = judge(a, b, a_a=True)
    assert report["verdict"] == "red" and any("warm-up: the pingsender destinations differ: A only ['backgroundtask/pingsender: connect public, telemetry'] | B only []" in p
                                              for p in report["problems"])
    paths = {e["path"]: e for e in report["not_judged"]["paths"]}
    assert paths["/etc/machine-id"]["touched_by"]["A"] == [host.PINGSENDER_KIND] and paths["/etc/resolv.conf"]["touched_by"]["B"] == [host.PINGSENDER_KIND], "its paths are listed"
    assert judge(a, with_warmup(footprint("again")), a_a=True)["verdict"] == "green", "the pingsender is in one warm-up only"


# -
# Run 44988820319: the class of a public destination, the shortcut inventory, what the pingsenders spawn, a reused pid.

def with_net_entries(fp, entries):
    fp = copy.deepcopy(fp)
    for seed in fp["seeds"]:
        seed["net"] = list(entries)
    return fp


def test_a_public_destination_is_compared_as_its_class_whatever_the_address_the_dns_gave():
    a = with_net_entries(footprint(), ["parent: connect tcp AF_INET 8.8.8.8:443", "parent: connect tcp AF_INET 141.101.90.1:443", "parent: connect tcp AF_INET 1.1.1.1:80"])
    b = with_net_entries(footprint("again"), ["parent: connect tcp AF_INET 8.8.4.4:443", "parent: connect tcp AF_INET 1.0.0.1:80", "parent: connect tcp AF_INET 172.64.41.4:443",
                                             "parent: connect tcp AF_INET 162.159.61.4:443"])
    assert judge(a, b, a_a=True)["verdict"] == "green", "the number of distinct addresses of a class is the DNS rotation's"
    assert "DNS rotation" in host.NET_CLASS_REASON


@pytest.mark.parametrize("entry,expected", [
    ("parent: connect tcp AF_INET 8.8.8.8:443", "net.parent: connect tcp public:443"),
    ("parent: connect tcp AF_INET6 [2606:4700::1]:443", "net.parent: connect tcp public:443"),
    ("parent: connect udp AF_INET 162.159.207.1:0", "net.parent: connect udp public:0"),
    ("Web Content: send udp AF_INET 172.32.0.5:443", "net.Web Content: send udp public:443"),
])
def test_a_class_of_destination_is_the_kind_the_verb_the_transport_and_the_port(entry, expected):
    assert "net." + host.net_class(entry) == expected
    assert host.net_class("netlink NETLINK_ROUTE RTM_GETADDR") == "netlink NETLINK_ROUTE RTM_GETADDR", "a netlink request is itself"
    assert "telemetry" not in host.net_class(entry) and entry.split(" ")[-1].split(":")[0] not in host.net_class(entry), "no name is made up from an address"


def test_a_class_that_only_one_side_reaches_or_another_port_or_another_kind_is_red():
    a = with_net_entries(footprint(), ["parent: connect tcp AF_INET 8.8.8.8:443"])
    for other, leaf in ((["parent: connect tcp AF_INET 8.8.8.8:8443"], "net.parent: connect tcp public:8443"),
                        (["gpu: connect tcp AF_INET 8.8.8.8:443"], "net.gpu: connect tcp public:443"), ([], None)):
        report = judge(a, with_net_entries(footprint("again"), other), a_a=True)
        assert report["verdict"] == "red", other
        assert (leaf is None) or leaf in {d["leaf"] for d in report["differences"]}
    assert {d["leaf"] for d in judge(a, with_net_entries(footprint("again"), []), a_a=True)["differences"]} == {"net.parent: connect tcp public:443"}


INV = ["c:\\programdata\\microsoft\\windows\\start menu\\programs\\desktop.ini", "d:\\users\\public\\desktop\\desktop.ini",
       "d:\\lab\\runner\\appdata\\roaming\\microsoft\\internet explorer\\quick launch\\user pinned\\taskbar\\nightly.lnk",
       "c:\\windows\\immersivecontrolpanel\\resources.pri", "c:\\windows\\systemapps\\microsoft.windows.sechealthui_cw5n1h2txyewy\\resources.pri"]


def with_inventory(fp, paths=INV, present=True):
    fp = copy.deepcopy(fp)
    for seed in fp["seeds"]:
        if present:
            seed["files"] = sorted(set(seed["files"]) | set(paths))
            seed["inventory"] = sorted(paths)
    return fp


def test_the_shortcut_inventory_is_judged_like_every_other_path_one_side_with_it_and_the_other_without_is_red():
    report = judge(with_inventory(footprint(platform="win32")), footprint("again", platform="win32"), a_a=True)
    assert report["verdict"] == "red" and {d["leaf"] for d in report["differences"]} == {"file." + p for p in INV}
    back = judge(footprint("again", platform="win32"), with_inventory(footprint(platform="win32")), a_a=True)
    assert {d["leaf"] for d in back["differences"]} == {"file." + p for p in INV}
    assert judge(with_inventory(footprint(platform="win32")), with_inventory(footprint("again", platform="win32")), a_a=True)["verdict"] == "green"


def test_an_inventory_that_is_not_complete_on_both_sides_is_red_and_so_is_a_path_next_to_it_in_every_launch():
    short = with_inventory(footprint("again", platform="win32"), INV[:3])
    report = judge(with_inventory(footprint(platform="win32")), short, a_a=True)
    assert report["verdict"] == "red" and {d["leaf"] for d in report["differences"]} == {"file." + p for p in INV[3:]}
    extra = with_inventory(footprint(platform="win32"))
    for seed in extra["seeds"]:
        seed["files"] = seed["files"] + ["d:\\lab\\runner\\.ssh\\config"]
    other = judge(extra, with_inventory(footprint("again", platform="win32")), a_a=True)
    assert other["verdict"] == "red" and {d["leaf"] for d in other["differences"]} == {"file.d:\\lab\\runner\\.ssh\\config"}
    known = with_inventory(footprint(platform="win32"), INV + [FILES[0]])
    assert {d["leaf"] for d in judge(known, footprint("again", platform="win32"), a_a=True)["differences"]} == {"file." + p for p in INV}, "FILES[0] is in both"


def test_the_inventory_of_a_launch_is_what_the_parent_first_touched_from_the_first_to_the_last_open_of_the_places_it_walks():
    ctx = host.Context("win32", devices={"\\Device\\HarddiskVolume3": "C:"})
    order = {(10, "C:\\early.dll"): 1, (10, "D:\\Users\\Public\\Desktop\\desktop.ini"): 5, (10, "C:\\Windows\\System32\\windows.fileexplorer.common.dll"): 6,
             (10, "D:\\Users\\r\\AppData\\Roaming\\Microsoft\\Internet Explorer\\Quick Launch\\User Pinned\\TaskBar\\nightly.lnk"): 8,
             (11, "C:\\Windows\\System32\\gpu-only.dll"): 6, (10, "C:\\late.dll"): 20}
    labels = {10: "parent", 11: "gpu"}
    got = host.inventory_of(order, lambda pid: labels[pid], ctx)
    assert got == sorted(["d:\\users\\public\\desktop\\desktop.ini", "d:\\users\\r\\appdata\\roaming\\microsoft\\internet explorer\\quick launch\\user pinned\\taskbar\\nightly.lnk",
                   "c:\\windows\\system32\\windows.fileexplorer.common.dll"])
    assert host.inventory_of({(10, "C:\\early.dll"): 1}, lambda pid: "parent", ctx) == [], "a launch that did not run it has none"
    assert "StartupOSIntegration.sys.mjs" in open(host.__file__, encoding="utf-8").read(), "the source that launches it is cited"


def test_the_etw_file_keeps_the_order_of_the_first_open_of_every_path(tmp_path):
    xml = tmp_path / "files.xml"
    xml.write_text(etw((100, "C:\\a.dll"), (100, "C:\\b.dll"), (100, "C:\\a.dll"), (102, "C:\\a.dll")), encoding="utf-8")
    order = {}
    host.etw_files(xml, {100, 102}, None, order)
    assert order == {(100, "C:\\a.dll"): 1, (100, "C:\\b.dll"): 2, (102, "C:\\a.dll"): 4}


def test_what_a_pingsender_spawns_is_named_after_it_and_comes_and_goes_with_the_pingsenders():
    labels = {1: "parent", 2: "socket", 3: host.PINGSENDER_KIND, 4: "socket", 5: host.PINGSENDER_KIND, 6: "socket", 7: "tab/isForBrowser"}
    parents = {2: 1, 3: 1, 4: 3, 5: 1, 6: 5, 7: 1}
    got = host.derive_labels(labels, parents)
    assert got == {1: "parent", 2: "socket", 3: host.PINGSENDER_KIND, 4: "socket<-pingsender", 5: host.PINGSENDER_KIND, 6: "socket<-pingsender", 7: "tab/isForBrowser"}
    kinds = {"parent": 1, "socket": 1, "socket<-pingsender": 2, host.PINGSENDER_KIND: 2, "gpu": 1, "glxtest": 1, "tab/isForBrowser": 2}
    # the pingsenders (and so what they spawn) in every launch of one side and none of the other: red like any kind; the parent's own socket is a kind of its own
    a = with_pingsenders(footprint(kinds=kinds), 3)
    report = judge(a, footprint("again", kinds={k: n for k, n in kinds.items() if "pingsender" not in k}), a_a=True)
    assert {d["leaf"] for d in report["differences"]} == {"proc." + host.PINGSENDER_KIND, "proc.socket<-pingsender"}
    # on both sides, what they spawn is listed by the launches that have it, and its count is not compared
    b = with_pingsenders(footprint("again", kinds=dict(kinds, **{"socket<-pingsender": 3})), 3)
    report = judge(a, b, a_a=True)
    assert report["verdict"] == "green" and report["differences"] == [] and report["not_judged"]["kinds"] == []
    plain = {k: n for k, n in kinds.items() if "pingsender" not in k}
    lost = footprint("again", kinds={k: n for k, n in plain.items() if k != "socket"})
    assert {d["leaf"] for d in judge(footprint(kinds=plain), lost, a_a=True)["differences"]} == {"proc.socket"}, "the parent's own socket still counts"


def test_a_creator_pid_that_windows_reused_before_the_launch_is_not_in_its_tree():
    events = host.parse_audit_events("\n".join([
        audit(5, 200, 100, FIREFOX, '"%s" --backgroundtask pingsender https://t.example/submit/telemetry/1/event/x' % FIREFOX),
        audit(10, 100, 1, FIREFOX, '"%s" -profile "D:\\t\\p"' % FIREFOX),
        audit(12, 102, 100, FIREFOX, '"%s" -contentproc tab' % FIREFOX)]))
    assert [e["pid"] for e in host.process_tree(events, {100})] == [100, 102], "record 5 came before the root (record 10): its creator is an older process that had the same pid"


# -
# The evidence of how a browser ended and what a crash leaves; the creates of the host.

def test_the_crash_files_are_read_when_they_appear_a_dmp_by_its_name_and_an_extra_by_its_content(tmp_path):
    folder = tmp_path / "minidumps"
    folder.mkdir()
    (folder / "a1b2.dmp").write_bytes(b"MDMP" + b"0" * 100)
    (folder / "a1b2.extra").write_text('{"MozCrashReason": "MOZ_CRASH(shutdown hang)", "ShutdownProgress": "profile-change-teardown", "CrashType": "SIGSEGV"}', encoding="utf-8")
    (folder / "notes.txt").write_text("not a crash file", encoding="utf-8")
    seen = {}
    crash.scan_crash_files([folder, tmp_path / "missing"], seen)
    files = sorted(seen.values(), key=lambda f: f["name"])
    assert [f["name"] for f in files] == ["a1b2.dmp", "a1b2.extra"] and files[0]["size"] == 104 and "content" not in files[0]
    assert "MozCrashReason" in files[1]["content"] and "ShutdownProgress" in files[1]["content"]
    crash.scan_crash_files([folder], seen)
    assert "MozCrashReason" in seen[str(folder / "a1b2.extra")]["content"], "a file that did not change is not read again"
    (folder / "a1b2.extra").write_text('{"MozCrashReason": "later", "more": "a file seen half written is read again when its size changes"}', encoding="utf-8")
    crash.scan_crash_files([folder], seen)
    assert "later" in seen[str(folder / "a1b2.extra")]["content"]


def test_a_crash_file_seen_by_its_name_before_its_content_is_written_is_read_again_when_it_is_complete(tmp_path):
    """The losing interleaving, built on purpose: the collector looks when the file is there and empty, then half written, then complete. A collector that reads a file once
    keeps the empty text (the profile is deleted when the launch ends, so there is no second chance)."""
    folder = tmp_path / "profile" / "minidumps"
    folder.mkdir(parents=True)
    extra = folder / "x.extra"
    seen = {}
    extra.write_bytes(b"")
    crash.scan_crash_files([folder], seen)
    assert seen[str(extra)]["content"] == "" and seen[str(extra)]["size"] == 0
    extra.write_bytes(b'{"CrashType": "SIG')
    crash.scan_crash_files([folder], seen)
    assert seen[str(extra)]["content"] == '{"CrashType": "SIG'
    extra.write_bytes(b'{"CrashType": "SIGSEGV"}')
    crash.scan_crash_files([folder], seen)
    assert seen[str(extra)]["content"] == '{"CrashType": "SIGSEGV"}' and seen[str(extra)]["size"] == len(b'{"CrashType": "SIGSEGV"}')
    crash.scan_crash_files([folder], seen)
    assert len(seen) == 1, "a complete file is one entry"


def test_the_watcher_keeps_the_complete_file_though_its_last_look_saw_it_empty_and_the_launch_deletes_the_profile_after_the_stop(tmp_path):
    """No thread, no sleep: the watcher's look is driven by the test (`scan_once`), the way the thread does it, and `stop` looks once more before the profile goes."""
    folder = tmp_path / "profile" / "minidumps"
    folder.mkdir(parents=True)
    watcher = host.CrashWatcher(lambda: [folder])
    (folder / "x.extra").write_bytes(b"")
    watcher.scan_once()
    assert watcher.seen[str(folder / "x.extra")]["content"] == "" and watcher.scans == 1
    (folder / "x.extra").write_bytes(b'{"CrashType": "SIGSEGV"}')
    (folder / "x.dmp").write_bytes(b"MDMP")
    got = watcher.stop()
    shutil.rmtree(tmp_path / "profile")
    assert [f["name"] for f in got["files"]] == ["x.dmp", "x.extra"] and "SIGSEGV" in got["files"][1]["content"] and got["scans"] == 2


def test_the_crash_folders_are_the_profiles_minidumps_and_the_pending_reports_of_the_user(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    win = host.crash_report_dirs(tmp_path / "invisible_profile_1", True)
    assert win == [tmp_path / "invisible_profile_1" / "minidumps", tmp_path / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Crash Reports" / "pending"]
    monkeypatch.setattr(host.os.path, "expanduser", lambda p: str(tmp_path / "home"))
    lin = host.crash_report_dirs(None, False)
    assert lin == [tmp_path / "home" / ".config" / "mozilla" / "firefox" / "Crash Reports" / "pending", tmp_path / "home" / ".mozilla" / "firefox" / "Crash Reports" / "pending"]


def test_a_linux_core_is_found_where_the_launch_ran_and_its_backtrace_is_asked_of_gdb(tmp_path, monkeypatch):
    (tmp_path / "core.4242").write_bytes(b"\x7fELF")
    (tmp_path / "other.txt").write_text("x", encoding="utf-8")

    class Done:
        stdout, stderr = "#0  0x00007f in mozilla::ShutdownXPCOM ()\n", ""

    calls = []
    monkeypatch.setattr(host.shutil, "which", lambda name: "/usr/bin/gdb")
    got = crash.core_info(tmp_path, "/e/firefox/firefox", run=lambda argv, **kw: calls.append(argv) or Done())
    assert got["cores"] == ["core.4242"] and "ShutdownXPCOM" in got["backtraces"]["core.4242"]
    assert calls == [["/usr/bin/gdb", "-batch", "-ex", "bt", "/e/firefox/firefox", str(tmp_path / "core.4242")]]
    monkeypatch.setattr(host.shutil, "which", lambda name: None)
    assert crash.core_info(tmp_path, "/e/firefox/firefox")["backtraces"] == {"core.4242": "gdb is not installed"}
    assert crash.core_info(tmp_path / "empty-nowhere", "x")["cores"] == []


def test_the_stand_in_allows_core_dumps_in_every_launch_and_a_folder_asked_for_is_where_it_moves_to(tmp_path, monkeypatch):
    import _strace_stand_in as stand_in

    class Help:
        stdout = "strace --kill-on-exit"

    monkeypatch.setattr(stand_in.shutil, "which", lambda name: "/usr/bin/strace")
    monkeypatch.setattr(stand_in.subprocess, "run", lambda *a, **k: Help())
    real = tmp_path / "firefox-real" / "firefox"
    real.parent.mkdir()
    real.write_text("x", encoding="utf-8")
    plain = Path(stand_in._stand_in(str(real), tmp_path / "t1", tmp_path / "l1", tmp_path / "p1"))
    assert "ulimit" not in plain.read_text(encoding="utf-8"), "the font tests' stand-in is as it was"
    every = Path(stand_in._stand_in(str(real), tmp_path / "t3", tmp_path / "l3", tmp_path / "p3", cores=True)).read_text(encoding="utf-8")
    assert "ulimit -c unlimited\nexec " in every and "\ncd " not in every, "a measured launch may dump core and its working directory is the launcher's (the paths the trace reads are the same)"


def create_event(pid, name, options):
    return ('<Event %s><System><EventID>12</EventID><Execution ProcessID="%d" ThreadID="7"/></System><EventData><Data Name="CreateOptions">0x%08x</Data>'
            '<Data Name="FileName">%s</Data></EventData></Event>' % (NS, pid, options, name))


def test_a_path_the_tree_asked_the_system_to_make_is_told_from_one_it_only_opened(tmp_path):
    ini = "C:\\ProgramData\\Mozilla-1de4eec8\\firefox_S-1-5-21-500_shortcuts.ini"
    xml = tmp_path / "files.xml"
    xml.write_text("<Events>" + create_event(10, ini, 0x03000060) + create_event(10, "C:\\Windows\\System32\\kernel32.dll", 0x01000060)
                   + create_event(11, "D:\\lab\\r\\x.lnk", 0x02000044) + create_event(10, "D:\\lab\\r\\old.tmp", 0x05000040) + "</Events>", encoding="utf-8")
    dispositions = {}
    host.etw_files(xml, {10, 11}, None, None, dispositions)
    assert dispositions[(10, ini)] == {3} and dispositions[(10, "C:\\Windows\\System32\\kernel32.dll")] == {1}
    got = host.creates_of({k: sorted(v) for k, v in dispositions.items()}, lambda pid: {10: "parent", 11: "tab"}[pid], host.Context("win32"))
    assert got == [{"path": "c:\\programdata\\mozilla-1de4eec8\\firefox_s-1-5-21-500_shortcuts.ini", "kinds": ["parent"], "how": ["OPEN_IF"]},
                   {"path": "d:\\lab\\r\\old.tmp", "kinds": ["parent"], "how": ["OVERWRITE_IF"]}, {"path": "d:\\lab\\r\\x.lnk", "kinds": ["tab"], "how": ["CREATE"]}]


# -
# The places of the shortcut code, before and after the idle launch (Windows): the direct proof of whether the engine left a shortcut or a log on the host.

def a_windows_home(tmp_path):
    env = {"APPDATA": str(tmp_path / "Roaming"), "ProgramData": str(tmp_path / "ProgramData"), "USERPROFILE": str(tmp_path / "User"), "PUBLIC": str(tmp_path / "Public")}
    for rel in ("Roaming/Microsoft/Windows/Start Menu/Programs/Accessories", "ProgramData/Microsoft/Windows/Start Menu/Programs", "User/Desktop", "Public/Desktop",
                "Roaming/Microsoft/Internet Explorer/Quick Launch/User Pinned/TaskBar", "Roaming/Microsoft/Internet Explorer/Quick Launch/User Pinned/ImplicitAppShortcuts",
                "ProgramData/Mozilla-1de4eec8", "ProgramData/Mozilla-other", "ProgramData/NotMozilla"):
        (tmp_path / rel).mkdir(parents=True)
    return env


def test_the_places_of_the_shortcut_code_are_the_start_menus_the_desktops_the_pinned_taskbar_and_every_mozilla_folder(tmp_path):
    env = a_windows_home(tmp_path)
    places = host.shortcut_places(env)
    names = [(str(p.relative_to(tmp_path)).replace("\\", "/"), r) for p, r in places]
    assert ("Roaming/Microsoft/Windows/Start Menu", False) in names and ("Roaming/Microsoft/Windows/Start Menu/Programs", True) in names
    assert ("ProgramData/Microsoft/Windows/Start Menu/Programs", True) in names and ("User/Desktop", False) in names and ("Public/Desktop", False) in names
    assert ("Roaming/Microsoft/Internet Explorer/Quick Launch/User Pinned/TaskBar", True) in names
    assert ("Roaming/Microsoft/Internet Explorer/Quick Launch/User Pinned/ImplicitAppShortcuts", True) in names
    assert ("ProgramData/Mozilla-1de4eec8", True) in names and ("ProgramData/Mozilla-other", True) in names and not any("NotMozilla" in n for n, _r in names)
    assert host.shortcut_places({}) == [], "a machine with none of these variables has none of these places"


def test_what_the_launch_made_changed_or_removed_in_those_places_is_the_difference_of_two_snapshots(tmp_path):
    import os as _os

    env = a_windows_home(tmp_path)
    places = host.shortcut_places(env)
    taskbar = tmp_path / "Roaming/Microsoft/Internet Explorer/Quick Launch/User Pinned/TaskBar"
    log = tmp_path / "ProgramData/Mozilla-1de4eec8/firefox_S-1-5-21-500_shortcuts.ini"
    log.write_text("[STARTMENU]\n", encoding="utf-8")
    (taskbar / "old.lnk").write_bytes(b"L")
    (tmp_path / "User/Desktop/stays.lnk").write_bytes(b"L")
    before = host.snapshot_places(places)
    log.write_text("[STARTMENU]\nShortcut0=Firefox.lnk\n", encoding="utf-8")
    _os.utime(log, (1_700_000_000, 1_700_000_000))
    (taskbar / "nightly.lnk").write_bytes(b"LNK" * 10)
    (tmp_path / "Roaming/Microsoft/Windows/Start Menu/Programs/Accessories/Private Browsing.lnk").write_bytes(b"LNK")
    (taskbar / "old.lnk").unlink()
    after = host.snapshot_places(places)
    got = host.diff_snapshots(before, after)
    assert [Path(e["path"]).name for e in got["new"]] == sorted(["Private Browsing.lnk", "nightly.lnk"], key=str.lower) or {Path(e["path"]).name for e in got["new"]} == {"Private Browsing.lnk", "nightly.lnk"}
    assert [Path(e["path"]).name for e in got["changed"]] == ["firefox_S-1-5-21-500_shortcuts.ini"] and got["changed"][0]["mtime"].startswith("2023-11-14")
    assert [Path(e["path"]).name for e in got["gone"]] == ["old.lnk"]
    assert [e["size"] for e in got["new"] if e["path"].endswith("nightly.lnk")] == [30]
    assert not any("stays.lnk" in e["path"] for kind in got.values() for e in kind), "a file the launch did not touch is not in it"
    assert host.diff_snapshots(after, host.snapshot_places(places)) == {"new": [], "changed": [], "gone": []}


def test_a_launch_that_left_nothing_says_so_and_the_report_carries_the_summary_line_without_comparing_it(capsys):
    a = footprint(platform="win32")
    a["idle"] = {"seed": 11, "kinds": {}, "idle": {"notified": {"observed": True, "polls": 2}, "shortcut_changes": {
        "new": [{"path": "C:/ProgramData/Microsoft/Windows/Start Menu/Programs/Private Browsing.lnk", "size": 1650, "mtime": "2026-11-22T05:20:08+00:00"}], "changed": [], "gone": []}}}
    b = footprint("again", platform="win32")
    b["idle"] = {"seed": 11, "kinds": {}, "idle": {"notified": {"observed": True, "polls": 2}, "shortcut_changes": {"new": [], "changed": [], "gone": []}}}
    report = judge(a, b, a_a=True)
    assert report["shortcuts"] == {"A": {"new": 1, "changed": 0, "gone": 0, "files": [("new", "Private Browsing.lnk", 1650)]}, "B": {"new": 0, "changed": 0, "gone": 0, "files": []}}
    host.print_report(report, "firefox-68", "firefox-68 again")
    out = capsys.readouterr().out
    assert "SHORTCUTS (the places of the shortcut code, before and after the idle launch; evidence) firefox-68: 1 new, 0 changed, 0 gone" in out and "Private Browsing.lnk" in out
    assert judge(footprint(), footprint("again"), a_a=True)["shortcuts"] == {"A": None, "B": None}, "Linux has none of this"


# -
# Windows footprints with paths added to chosen launches (the host paths are judged over all the launches: tests/test_ab_host_aa_stable.py).


def with_system(fp, per_launch):
    """`per_launch`: {seed or "warmup"/"idle": [paths]}: the paths added to that launch's files."""
    fp = copy.deepcopy(fp)
    for key in ("warmup", "idle"):
        if key in per_launch and not fp.get(key):
            fp[key] = seed_footprint(11)
    launches = {s["seed"]: s for s in fp["seeds"]}
    launches.update({k: fp[k] for k in ("warmup", "idle") if fp.get(k)})
    for key, add in per_launch.items():
        launch = launches[key]
        launch["files"] = sorted(set(launch["files"]) | set(add))
        launch["touched_by"] = dict(launch.get("touched_by") or {}, **{p: ["tab/isForBrowser"] for p in add})
    return fp


def win(engine="firefox-68", **kw):
    return footprint(engine, platform="win32", **kw)


def test_the_ab_passes_the_other_run_of_the_68_to_the_judge(tmp_path, monkeypatch):
    seen = {}
    pub = fake_engine(tmp_path / "fetch", "pub")
    cand = fake_engine(tmp_path / "checkout", "cand")
    monkeypatch.setattr(host.oracle, "fetch_published", lambda tag, work: (pub, None))
    docs = {"footprint-68.json": win(), "footprint-68-again.json": win("again"), "footprint-candidate.json": win("candidate")}

    def fake_run(exe, seal, leg, out, label, seeds):
        name = {"firefox-68": "footprint-68.json", "firefox-68 again": "footprint-68-again.json", "candidate": "footprint-candidate.json"}[label]
        host.write_json(tmp_path / "o" / name, docs[name])
        return 0

    monkeypatch.setattr(host, "run_subprocess", fake_run)
    monkeypatch.setattr(host, "FLOORS", SMALL)
    monkeypatch.setattr(host, "run_stock_control", lambda leg, out: {"stock": True})
    real = host.judge
    monkeypatch.setattr(host, "judge", lambda a, b, **kw: seen.update(kw) or real(a, b, **kw))
    host.ab(cand, Path("seal"), "win-x86_64", tmp_path / "o", "firefox-68", [11, 12])
    assert seen["also_68"][0]["engine"] == "again"


# -
# The preloaded about:newtab: absent or present per launch.

def test_the_preloaded_newtab_process_is_absent_or_present_per_launch_never_more_than_one_and_never_compared_between_sides():
    one = footprint(kinds=dict(KINDS, **{"Privileged Cont": 1}))
    assert judge(one, footprint("again"), a_a=True)["verdict"] == "green", "present on one side only"
    assert judge(one, copy.deepcopy(one), a_a=True)["verdict"] == "green"
    two = footprint(kinds=dict(KINDS, **{"Privileged Cont": 2}))
    report = judge(two, footprint("again"), a_a=True)
    assert report["verdict"] == "red" and any("2 Privileged Cont processes: the preloaded about:newtab is absent or present, never more than one" in p for p in report["problems"])
    assert "NewTabPagePreloading.sys.mjs:96" in host.PRIVILEGED_REASON and "firefox.js:1952" in host.PRIVILEGED_REASON and "idle time" in host.PRIVILEGED_REASON


# -
# A launch whose parent did not end with 0 is red in EVERY launch of the oracle, and what it left is saved (scripts/ci_crash_evidence.py: run 45030727459 had a firefox-68 parent
# die with -11 in a normal close and nothing was kept). Each recorder is shown on the input that makes it fail: a launch that died.

import ci_crash_evidence as crash  # noqa: E402

CRASH_LOG = "".join('%d openat(AT_FDCWD, "/usr/lib/x%d", O_RDONLY) = 3\n' % (100, i) for i in range(300)) + (
    "100 --- SIGSEGV {si_signo=SIGSEGV, si_code=SI_USER, si_pid=4000, si_uid=1000} ---\n"
    + "".join('100 write(2, "crash reporter line %d\\n", 20) = 20\n' % i for i in range(150))
    + "100 +++ killed by SIGSEGV (core dumped) +++\n")


class Run:
    """A stand-in for `subprocess.run` that answers by the tool it is asked for, and keeps what it was asked."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        text = self.answers.get(os.path.basename(argv[0]), "")

        class Done:
            stdout, stderr = text, ""

        return Done()


def with_tools(*names):
    return lambda name: "/usr/bin/" + name if name in names else None


def test_the_exit_code_of_a_parent_is_told_by_its_signal_on_linux_and_by_its_status_on_windows():
    assert crash.describe_exit(-11, False) == "-11 (SIGSEGV)" and crash.describe_exit(-6, False) == "-6 (SIGABRT)" and crash.describe_exit(1, False) == "1"
    assert crash.describe_exit(3221225477, True) == "0xC0000005 (access violation)" and crash.describe_exit(-1073741819, True) == "0xC0000005 (access violation)"
    assert "not seen" in crash.describe_exit(None, False)


def test_a_launch_that_did_not_end_with_0_is_a_red_line_that_names_the_seed_the_launch_and_the_code():
    assert crash.exit_problem(0, False, seed=5, launch="measured") is None
    line = crash.exit_problem(-11, False, seed=5, launch="measured")
    assert line == "the parent process exited with code -11 (SIGSEGV): seed 5, launch measured"
    assert "not seen" in crash.exit_problem(None, False, seed=5, launch="idle"), "an oracle that did not see how its browser ended did not see it"
    launches = [{"n": 1, "label": {"seed": 7, "dpr": 1.5, "launch": "visit"}, "returncode": 0},
                {"n": 2, "label": {"seed": 7, "launch": "relaunch-second"}, "returncode": -11, "evidence": "crash/02-7-relaunch-second"}]
    got = crash.exit_problems(launches, "firefox-68-again", False)
    assert len(got) == 1 and "code -11 (SIGSEGV)" in got[0] and "run firefox-68-again" in got[0] and "side B" in got[0] and "seed 7" in got[0] and "launch relaunch-second" in got[0]
    assert "(evidence: crash/02-7-relaunch-second)" in got[0]
    assert "side A" in crash.exit_problems(launches, "firefox-68", False)[0] and "side B" in crash.exit_problems(launches, "candidate", False)[0]
    assert "side A" in crash.exit_problems(launches, "x", False, side="A")[0]


def test_a_parent_the_lifecycle_says_did_not_exit_0_is_red_when_the_root_code_said_nothing_of_it():
    lifecycle = [{"event": "start", "kind": "parent", "how": ""}, {"event": "end", "kind": "tab", "how": "exited 1"}, {"event": "end", "kind": "parent", "how": "exited 3221225477"}]
    got = crash.parent_exit_problems(lifecycle, 0, seed=4, launch="measured")
    assert len(got) == 1 and "exited 3221225477" in got[0] and "seed 4" in got[0]
    assert crash.parent_exit_problems(lifecycle, -11, seed=4) == [], "the root's own code already said it"
    assert crash.parent_exit_problems([{"event": "end", "kind": "parent", "how": "exited 0"}], 0) == []


def test_the_strace_tail_is_the_last_100_lines_and_the_signal_line_that_is_further_back_is_kept_apart(tmp_path):
    log = tmp_path / "strace.log"
    log.write_text(CRASH_LOG, encoding="utf-8")
    tail = crash.strace_tail(log)
    assert len(tail) == 100 and tail[-1].endswith("+++ killed by SIGSEGV (core dumped) +++") and not any("--- SIGSEGV" in x for x in tail)
    signals = crash.strace_signals(log)
    assert len(signals) == 1 and signals[0][0] == 301 and "si_pid=4000" in signals[0][1], "the signal line is there though the crash reporter's calls pushed it out of the tail"
    short = tmp_path / "short.log"
    short.write_text('1 execve("/e", [], 0x1) = 0\n', encoding="utf-8")
    assert crash.strace_tail(short) == ['1 execve("/e", [], 0x1) = 0']


def test_the_core_is_run_through_gdb_with_the_three_commands_and_the_build_ids_are_asked_of_readelf_and_eu_unstrip(tmp_path):
    core = tmp_path / "core.firefox.4242.1"
    core.write_bytes(b"\x7fELF")
    binary = tmp_path / "engine" / "firefox"
    binary.parent.mkdir()
    binary.write_bytes(b"x")
    (binary.parent / "libxul.so").write_bytes(b"x")
    run = Run({"gdb": 'Thread 1 (Thread 0x7f (LWP 4242) "firefox"):\n#0  0x0 in mozilla::ShutdownXPCOM ()\n$1: 0x1 "MOZ_CRASH(shutdown)"\nFrom To Syms Read Shared Object Library\n',
               "readelf": "    Build ID: abc123\n", "eu-unstrip": "0x1+0x2 deadbeef@0x3 /usr/lib/libc.so.6 libc.so.6\n"})
    got = crash.collect(-11, str(binary), strace_log=None, watched={"files": [], "scans": 3}, core_folders=[tmp_path], since=0, windows=False, run=run,
                        which=with_tools("gdb", "readelf", "eu-unstrip"))
    gdb = [c for c in run.calls if c[0].endswith("gdb")][0]
    assert gdb[:2] == ["/usr/bin/gdb", "-batch"] and gdb[-2:] == [str(binary), str(core)]
    commands = [gdb[i + 1] for i, a in enumerate(gdb) if a == "-ex"]
    assert commands == ["thread apply all bt", "x/s *(char**)&gMozCrashReason", "info sharedlibrary"]
    assert "Thread 1 (" in got["cores"]["backtraces"][core.name] and "MOZ_CRASH(shutdown)" in got["cores"]["backtraces"][core.name]
    ids = got["cores"]["build_ids"][core.name]
    assert ids["binary"] == "Build ID: abc123" and ids["libxul"] == "Build ID: abc123" and "deadbeef" in ids["core"]
    assert [c for c in run.calls if c[0].endswith("eu-unstrip")][0][1:] == ["-n", "--core", str(core)]


def test_a_collector_that_finds_nothing_says_so_and_never_stays_silent(tmp_path):
    binary = tmp_path / "firefox"
    binary.write_bytes(b"x")
    got = crash.collect(-11, str(binary), strace_log=None, watched={"files": [], "scans": 0}, core_folders=[tmp_path / "cores"], since=0, windows=False, run=Run({}),
                        which=with_tools())
    said = " | ".join(got["said"])
    assert "runs no strace" in said and "no core file written" in said and "no minidump and no `.extra`" in said
    assert got["cores"]["cores"] == [] and got["cores"]["build_ids"]["binary only (no core)"]["binary"] == "readelf is not installed"
    core = tmp_path / "cores" / "core.firefox.1.1"
    core.parent.mkdir()
    core.write_bytes(b"\x7fELF")
    got = crash.collect(-11, str(binary), strace_log=None, watched={"files": []}, core_folders=[core.parent], since=0, windows=False, run=Run({}), which=with_tools())
    assert got["cores"]["backtraces"][core.name] == "gdb is not installed" and got["cores"]["build_ids"][core.name]["core"] == "eu-unstrip is not installed"
    missing = crash.collect(-11, str(binary), strace_log=tmp_path / "nowhere.log", watched={"files": []}, core_folders=[], windows=False, run=Run({}), which=with_tools())
    assert any("strace log could not be read" in s for s in missing["said"])
    quiet = tmp_path / "quiet.log"
    quiet.write_text("1 +++ exited with 1 +++\n", encoding="utf-8")
    assert any("no fatal signal line" in s
               for s in crash.collect(1, str(binary), strace_log=quiet, watched={"files": []}, core_folders=[], windows=False, run=Run({}), which=with_tools())["said"])
    files = [{"name": "a.dmp", "dir": "d", "size": 5}, {"name": "a.extra", "dir": "d", "size": 9, "content": '{"CrashType": "ACCESS"}'}]
    windows = crash.collect(3221225477, str(binary), strace_log=None, watched={"files": files}, windows=True)
    assert windows["strace"] is None and windows["cores"] is None and windows["crash_files"]["dmp"] == ["a.dmp"] and windows["crash_files"]["extra"][0]["name"] == "a.extra"
    assert windows["exit"]["text"] == "0xC0000005 (access violation)" and any("no strace and no core on Windows" in s for s in windows["said"])
    assert not any("no minidump" in s for s in windows["said"])


def test_a_core_of_an_earlier_launch_is_not_this_one_s(tmp_path):
    old, new = tmp_path / "core.old.1.1", tmp_path / "core.new.2.2"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    os.utime(old, (1000, 1000))
    now = new.stat().st_mtime
    assert crash.find_cores([tmp_path, tmp_path / "missing"], since=now - 1) == [new] and crash.find_cores([tmp_path], since=None) == [new, old]
    assert crash.core_dirs({crash.CORE_DIR_ENV: "/cores"}, pattern="|/usr/share/apport/apport -p%p") == [Path("/cores")], "a pipe leaves no file to look for"
    assert crash.core_dirs({}, pattern="/var/cores/core.%e.%p") == [Path("/var/cores")] and crash.core_dirs({}, pattern=None) == []


def test_the_evidence_is_written_as_files_a_person_can_read_in_the_artifact(tmp_path):
    log = tmp_path / "strace.log"
    log.write_text(CRASH_LOG, encoding="utf-8")
    core = tmp_path / "core.firefox.4242.1"
    core.write_bytes(b"\x7fELF")
    run = Run({"gdb": 'Thread 2 (Thread 0x7f (LWP 4243) "Socket Thread"):\n', "readelf": "Build ID: aa\n", "eu-unstrip": "x\n"})
    binary = tmp_path / "engine" / "firefox"
    binary.parent.mkdir()
    binary.write_bytes(b"x")
    watched = {"files": [{"name": "u.extra", "dir": "d", "size": 9, "content": '{"MozCrashReason": "MOZ_CRASH(x)"}'}, {"name": "u.dmp", "dir": "d", "size": 4}], "scans": 5}
    evidence = crash.collect(-11, str(binary), strace_log=log, watched=watched, core_folders=[tmp_path], since=0, windows=False, run=run,
                             which=with_tools("gdb", "readelf", "eu-unstrip"))
    written = crash.write_crash_files(evidence, tmp_path / "out" / "crash")
    assert {"strace-tail.txt", "strace-signals.txt", "gdb-core.firefox.4242.1.txt", "build-ids.txt", "u.extra.txt", "minidumps.txt", "said.txt", "crash.json"} <= set(written)
    folder = tmp_path / "out" / "crash"
    assert "--- SIGSEGV" in (folder / "strace-signals.txt").read_text(encoding="utf-8")
    assert "killed by SIGSEGV (core dumped)" in (folder / "strace-tail.txt").read_text(encoding="utf-8")
    assert "Thread 2 (" in (folder / "gdb-core.firefox.4242.1.txt").read_text(encoding="utf-8") and "MOZ_CRASH(x)" in (folder / "u.extra.txt").read_text(encoding="utf-8")
    assert (folder / "minidumps.txt").read_text(encoding="utf-8") == "u.dmp\n"
    assert json.loads((folder / "crash.json").read_text(encoding="utf-8"))["exit"]["code"] == -11


def test_a_fake_launch_that_died_with_minus_11_is_a_red_line_with_the_evidence_and_one_that_ended_with_0_is_neither(tmp_path):
    log = tmp_path / "strace.log"
    log.write_text(CRASH_LOG, encoding="utf-8")
    watched = {"files": [{"name": "u.extra", "dir": "d", "size": 9, "content": '{"CrashType": "SIGSEGV"}'}], "scans": 2}
    problems, left = host.end_of_launch(-11, False, 11, "measured", [], "/e/firefox", log, watched, 0.0, folders=[], run=Run({}))
    assert problems == ["the parent process exited with code -11 (SIGSEGV): seed 11, launch measured"]
    assert left["exit"] == {"code": -11, "text": "-11 (SIGSEGV)"} and left["strace"]["signals"][0][1].startswith("100 --- SIGSEGV")
    assert left["crash_files"]["extra"][0]["name"] == "u.extra"
    assert host.end_of_launch(0, False, 11, "measured", [], "/e/firefox", log, watched, 0.0, folders=[]) == ([], None)
    problems, left = host.end_of_launch(None, False, 11, "idle", [], "/e/firefox", log, {}, 0.0, folders=[], run=Run({}))
    assert "not seen" in problems[0] and left is not None
    windows, kept = host.end_of_launch(3221225477, True, 11, "idle", [], "D:\\e\\firefox.exe", None, {"files": []}, 0.0, run=Run({}))
    assert "0xC0000005 (access violation)" in windows[0] and "launch idle" in windows[0] and kept["strace"] is None


def test_a_launch_that_did_not_end_with_0_is_in_the_verdict_of_the_run_named_by_side_seed_and_launch_whatever_launch_it_was(monkeypatch, tmp_path):
    bad = crash.exit_problem(-11, False, seed=12, launch="measured")

    def fake_run_seed(binary, seed, work, name=None, idle=False, datareporting_dir=None, audit_dir=None):
        fp = seed_footprint(seed)
        if name is None and seed == 12:
            fp["problems"] = [bad]
            fp["crash"] = {"exit": {"code": -11, "text": "-11 (SIGSEGV)"}, "said": ["no core file written"], "strace": None, "cores": None, "crash_files": {"dmp": [], "extra": []}}
        if name == "idle":
            fp["problems"] = [crash.exit_problem(1, False, seed=11, launch="idle")]
        return fp

    monkeypatch.setattr(host, "run_seed", fake_run_seed)
    monkeypatch.setattr(host.os.path, "realpath", lambda p: p)
    doc = host.run_engine("ff", "linux-x86_64", [11, 12], "firefox-68", tmp_path / "x.crash")
    assert "idle: the parent process exited with code 1: seed 11, launch idle" in doc["problems"]
    assert (tmp_path / "x.crash" / "seed12" / "crash.json").is_file()
    assert (tmp_path / "x.crash" / "seed12" / "said.txt").read_text(encoding="utf-8") == "no core file written\n"
    other = footprint()
    other["seeds"][1] = doc["seeds"][1]
    red = judge(footprint(), other, a_a=True)
    assert red["verdict"] == "red" and "seed 12 side B: the parent process exited with code -11 (SIGSEGV): seed 12, launch measured" in red["problems"]
    named = judge(dict(footprint(), problems=doc["problems"]), footprint(), a_a=True)
    assert "side A: idle: the parent process exited with code 1: seed 11, launch idle" in named["problems"]


def test_the_signal_the_recorder_is_tested_with_goes_to_the_browser_s_parent_and_not_to_strace_or_a_content_process():
    class Child:
        def __init__(self, pid, cmd):
            self.pid, self._cmd = pid, cmd

        def cmdline(self):
            return self._cmd

    assert host.browser_parent(1, lambda: [Child(7, ["firefox", "-contentproc", "tab"]), Child(8, ["firefox", "-profile", "/tmp/p"])]) == 8
    with pytest.raises(RuntimeError, match="no browser parent"):
        host.browser_parent(1, lambda: [Child(7, ["firefox", "-contentproc", "tab"])])
    sent = []

    class Process:
        polls = 0

        def poll(self):
            Process.polls += 1
            return -11 if Process.polls > 2 else None

    assert host.crash_the_parent(1, Process(), 11, kill=lambda pid, sig: sent.append((pid, sig)), parent=8, sleep=lambda s: None) == 8
    assert sent == [(8, 11)] and Process.polls == 3, "it waits for the root to end, and sends the signal once"


def test_the_crash_probe_is_red_when_the_launch_that_died_is_in_the_footprint_and_fails_when_the_recorder_did_not_see_it(tmp_path, capsys):
    def made(problems):
        def run(binary, seal, leg, out, label, seeds, crash_with=None):
            assert crash_with == "SIGSEGV" and seeds == [11]
            Path(out).write_text(json.dumps({"format": host.FORMAT, "problems": problems, "seeds": []}), encoding="utf-8")
            return 0
        return run

    red = host.crash_probe("ff", "linux-x86_64", tmp_path / "a", 11, run=made([crash.exit_problem(-11, False, seed=11, launch="crash")]))
    assert red == 1 and "AB HOST CRASH PROBE FAILED" in capsys.readouterr().out
    assert host.crash_probe("ff", "linux-x86_64", tmp_path / "b", 11, run=made([])) == 0, "a recorder that saw nothing in a launch that died: the e2e test fails on this"
    assert "AB HOST CRASH PROBE OK" in capsys.readouterr().out
    assert host.crash_probe("ff", "linux-x86_64", tmp_path / "c", 11, run=lambda *a: 3) == 2


class FakeProcess:
    pid = 4242

    def __init__(self, code):
        self.code = code

    def poll(self):
        return self.code

    def terminate(self):
        pass


def a_launch_through_the_core(probe, code, label, folder=None):
    """A launch as the core makes it: a `Connection` over two pipes whose process ended with `code`, closed as the wrapper closes it."""
    from invisible_core.juggler.connection import Connection, ProcessOutput

    probe.set_label(**label)
    to_r, to_w = os.pipe()
    from_r, from_w = os.pipe()
    out_r, out_w = os.pipe()
    output = ProcessOutput(os.fdopen(out_r, "rb"))  # what `launch` makes before the connection: one reader of the browser's output, for the whole life of the process
    os.write(out_w, b"browser says: launch of seed %d\n" % label.get("seed", 0))
    os.close(out_w)
    conn = Connection(to_w, from_r, FakeProcess(code))
    conn.output = output
    if folder is not None:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "u.extra").write_text('{"CrashType": "SIGSEGV", "MozCrashReason": "MOZ_CRASH(glean)"}', encoding="utf-8")
    conn.close()
    os.close(from_w)
    os.close(to_r)
    return conn


def test_the_probe_sees_every_launch_the_core_makes_and_the_one_that_died_is_red_with_its_crash_files_while_the_others_are_not(tmp_path):
    from invisible_core.juggler.connection import Connection

    folder = tmp_path / "profile" / "minidumps"
    probe = crash.LaunchProbe("/e/firefox", tmp_path / "out" / "crash", windows=False, folders=[folder], which=with_tools(), run=Run({}))
    before = (Connection.__init__, Connection.close)
    probe.install()
    try:
        assert (Connection.__init__, Connection.close) != before
        a_launch_through_the_core(probe, 0, {"seed": 1, "launch": "visit"})
        a_launch_through_the_core(probe, -11, {"seed": 2, "launch": "relaunch-second"}, folder)
        a_launch_through_the_core(probe, 0, {"seed": 3, "launch": "visit"})
    finally:
        probe.uninstall()
    assert (Connection.__init__, Connection.close) == before
    assert [x["returncode"] for x in probe.launches] == [0, -11, 0]
    lines = crash.exit_problems(probe.launches, "firefox-68", False)
    assert len(lines) == 1 and "code -11 (SIGSEGV)" in lines[0] and "seed 2" in lines[0] and "launch relaunch-second" in lines[0] and "side A" in lines[0]
    evidence = [x for x in probe.launches if x["returncode"]][0]
    saved = tmp_path / "out" / "crash" / evidence["evidence"].split("/", 1)[1]
    assert "MOZ_CRASH(glean)" in (saved / "u.extra.txt").read_text(encoding="utf-8")
    assert "no core file written" in " | ".join(evidence["said"]) and "runs no strace" in " | ".join(evidence["said"])
    exits = tmp_path / "out" / "crash" / "exits.json"
    assert probe.write() == exits and crash.read_exits(exits)["launches"][1]["returncode"] == -11
    assert len([p for p in (tmp_path / "out" / "crash").iterdir() if p.is_dir() and p.name != crash.STDERR_DIR]) == 1, "a launch that ended with 0 leaves no evidence folder"
    assert sorted(f.name for f in (tmp_path / "out" / "crash" / crash.STDERR_DIR).iterdir()) == ["01-1-visit.txt", "02-2-relaunch-second.txt", "03-3-visit.txt"], "what the browser wrote is kept for every launch"
    assert crash.exits_of_run(tmp_path / "out" / "crash", "firefox-68", False) == lines


def test_a_run_whose_probe_wrote_nothing_is_red_because_the_oracle_did_not_see_its_browsers(tmp_path):
    assert "left no record" in crash.exits_of_run(tmp_path / "out" / "crash" / "firefox-68", "firefox-68")[0]
    folder = tmp_path / "crash" / "firefox-68"
    folder.mkdir(parents=True)
    (folder / "exits.json").write_text(json.dumps({"format": crash.FORMAT, "launches": []}), encoding="utf-8")
    assert "recorded no launch" in crash.exits_of_run(folder, "firefox-68")[0]
    assert crash.exits_of_run(folder, "firefox-68", needed=False) == [] and crash.exits_of_run(None, "x") == []
    (folder / "exits.json").write_text("not json", encoding="utf-8")
    assert "left no record" in crash.exits_of_run(folder, "firefox-68")[0]


def test_a_crash_watcher_does_not_take_the_reports_of_earlier_crashes_for_the_launch_s(tmp_path):
    pending = tmp_path / "Crash Reports" / "pending"
    pending.mkdir(parents=True)
    (pending / "old.extra").write_text('{"CrashType": "OLD"}', encoding="utf-8")
    already = crash.existing_crash_files([pending])
    watcher = crash.CrashWatcher(lambda: [pending], ignore=already)
    (pending / "new.extra").write_text('{"CrashType": "NEW"}', encoding="utf-8")
    got = watcher.stop()
    assert [f["name"] for f in got["files"]] == ["new.extra"]


# -
# Run from the same folder: where the engine sits is a variable of the experiment, not a difference of the paths.

def _ancestors(exe) -> tuple:
    return tuple(str(p) for p in Path(exe).parents)


def test_every_engine_run_of_an_ab_is_launched_from_the_same_folder_whatever_the_root_the_engine_came_from(tmp_path, monkeypatch):
    """The content processes call realpath(<install>/omni.ja), which reads the link of every ancestor of the install folder: run 45043617981 saw `readlink /srv/lab/work/...`
    for the candidate and `readlink /tmp/ab-host-fetch-<random>/engines` for the 68. Both come from different roots here, and what each run is given is one path."""
    seen = []
    pub = fake_engine(tmp_path / "tmp" / "ab-host-fetch-dc0cc7qh" / "engines", "firefox-68_x", b"published")
    cand = fake_engine(tmp_path / "srv" / "lab" / "work" / "checkout" / "checkout", "ff", b"candidate")
    monkeypatch.setattr(host.oracle, "fetch_published", lambda tag, work: (pub, "pub-seal"))

    def run(exe, seal, leg, out, label, seeds):
        folder = Path(exe).parent
        seen.append((label, exe, _ancestors(exe), sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*")), Path(exe).read_bytes(), (folder / "omni.ja").read_bytes()))
        return 1

    monkeypatch.setattr(host, "run_subprocess", run)
    assert host.ab(cand, Path("cand-seal"), "linux-x86_64", tmp_path / "o", "firefox-68", [11]) == 2
    assert [s[0] for s in seen] == ["firefox-68", "firefox-68 again", "candidate"]
    assert len({s[1] for s in seen}) == 1, "one absolute path for every run"
    assert len({s[2] for s in seen}) == 1, "so the links read are of the same ancestors"
    assert Path(seen[0][1]) == host.stage_dir() / "firefox" and Path(seen[0][1]).parent.parent == Path(os.environ["RUNNER_TEMP"]) / "ab-host" / "engine"
    assert [s[4] for s in seen] == [b"published", b"published", b"candidate"] and [s[5] for s in seen] == [b"published omni", b"published omni", b"candidate omni"]
    assert seen[0][3] == seen[2][3] == ["browser", "browser/omni.ja", "firefox", "omni.ja"], "the whole package tree is copied"
    assert not host.stage_dir().exists(), "and removed after each run"
    assert Path(pub).read_bytes() == b"published" and Path(cand).read_bytes() == b"candidate", "the engine itself is not touched"


def test_the_folder_of_the_engine_is_part_of_the_ours_class_and_its_ancestors_are_the_same_host_leaves_in_every_run(tmp_path):
    """What the oracle records of the install: the package root is `ours`, its ancestors are host leaves, and they are the same set for the 68 and for the candidate."""
    leaves = []
    for root, package, payload in ((tmp_path / "a" / "ab-host-fetch-x" / "engines", "firefox-68_y", b"1"), (tmp_path / "b" / "srv" / "lab" / "work", "ff", b"2")):
        with host.staged_engine(fake_engine(root, package, payload)) as exe:
            folder = os.path.dirname(exe)
            ctx = host.Context("linux", app=[folder])
            ancestors = [str(p).replace("\\", "/") for p in Path(folder).parents if str(p) != str(Path(folder).anchor)]
            leaves.append((host.classify(folder, ctx)[0], host.classify_paths(ancestors, host.Context("linux")), len(ancestors)))
    assert leaves[0][0] == leaves[1][0] == "package" and leaves[0] == leaves[1]


def test_a_host_path_outside_the_root_is_still_red_nothing_is_normalised():
    """The comparison is not told to ignore the ancestors of the install: one extra path outside the package root on one side is a difference."""
    c = footprint("candidate")
    for seed in c["seeds"]:
        seed["files"] = FILES + ["/srv/lab/work"]
        seed["touched_by"] = {"/srv/lab/work": ["Web Content", "forkserver"]}
        seed["touched_calls"] = {"/srv/lab/work": ["readlink"]}
    report = judge(footprint(), c, expected=expected())
    assert report["verdict"] == "red" and [d["leaf"] for d in report["differences"]] == ["file./srv/lab/work"] * 2
    assert any("file./srv/lab/work" in p and "via readlink" in p for p in report["problems"])


def test_the_staged_package_is_a_copy_of_the_whole_tree_with_its_links_and_a_leftover_of_an_earlier_run_does_not_stay(tmp_path):
    engine = fake_engine(tmp_path / "src", "pkg", b"bytes")
    if hasattr(os, "symlink"):
        try:
            os.symlink("omni.ja", str(Path(engine).parent / "link.ja"))
        except OSError:
            pass
    stale = host.stage_dir() / "leftover.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("an earlier run", encoding="utf-8")
    with host.staged_engine(engine) as exe:
        folder = Path(exe).parent
        assert folder == host.stage_dir() and not (folder / "leftover.txt").exists()
        assert sorted(p.name for p in folder.iterdir()) == sorted(p.name for p in Path(engine).parent.iterdir())
        assert (folder / "browser" / "omni.ja").read_bytes() == b"bytes browser omni" and os.access(exe, os.R_OK)
        if (Path(engine).parent / "link.ja").is_symlink():
            assert (folder / "link.ja").is_symlink()
    assert not host.stage_dir().exists()


# -
# The graphics probe is `gfxtest`: the declared difference names the real process, and covers what only that process touched.

GFX_PCI = ["/sys/bus/pci", "/sys/bus/pci/"]
GFX_ONLY = {path: ["gfxtest"] for path in GFX_PCI}


def with_files(fp, files, touched_by, calls=None, seeds=None):
    """`fp` whose seeds (every one, or `seeds`) also touched `files`, with the kinds `touched_by` says touched each of them."""
    for seed in fp["seeds"]:
        if seeds is None or seed["seed"] in seeds:
            seed["files"] = list(seed["files"]) + list(files)
            seed["touched_by"] = dict(seed.get("touched_by", {}), **touched_by)
            seed["touched_calls"] = dict(seed.get("touched_calls", {}), **(calls or {path: ["access"] for path in files}))
    return fp


def test_the_probe_of_the_68_is_named_gfxtest_by_what_it_ran_in_every_spelling_the_engine_runs_it():
    argv = ["gfxtest", "glx", "-f", "18"]
    assert host.label_of_exec("/srv/lab/work/checkout/checkout/ff/gfxtest", argv, REAL) == "gfxtest"
    assert host.label_of_exec("/proc/self/exe", argv, REAL) == "gfxtest"
    assert host.label_of_exec(REAL, argv, REAL) == "gfxtest" and host.label_of_exec(REAL, ["/e/firefox/firefox"], REAL) == "parent"
    log = ['100 execve("/e/firefox/firefox", ["/e/firefox/firefox", "-profile", "/tmp/p"], 0x7ffd /* 40 vars */) = 0',
           "100 fork() = 101", '101 execve("/proc/self/exe", ["gfxtest", "glx", "-f", "18"], 0x7ffd /* 40 vars */) = 0']
    procs = host.spawned_processes(log, REAL)
    assert {pid: p["label"] for pid, p in procs.items()} == {100: "parent", 101: "gfxtest"} and host.spawned_counts(procs)["gfxtest"] == 1


def test_the_declared_difference_covers_the_gfxtest_process_and_the_paths_only_it_touched_in_the_seeds_where_the_candidate_has_none_of_it():
    a = with_files(footprint(), GFX_PCI, GFX_ONLY)
    report = judge(a, a_candidate_without_gfxtest(), expected=expected())
    assert report["verdict"] == "green", report["problems"]
    assert report["stale"] == [] and {d["id"] for d in report["declared"]} == {"gfxtest-69"}
    assert sorted({d["leaf"] for d in report["declared"]}) == ["file./sys/bus/pci", "file./sys/bus/pci/", "proc.gfxtest"]
    assert [d["seed"] for d in report["declared"] if d["leaf"] == "proc.gfxtest"] == list(SEEDS)


def test_a_path_the_gfxtest_touched_with_another_process_stays_a_difference():
    a = with_files(footprint(), GFX_PCI + ["/sys/devices/pci0000:00"], dict(GFX_ONLY, **{"/sys/devices/pci0000:00": ["gfxtest", "parent"]}))
    report = judge(a, a_candidate_without_gfxtest(), expected=expected())
    assert report["verdict"] == "red" and [d["leaf"] for d in report["differences"]] == ["file./sys/devices/pci0000:00"] * len(SEEDS)
    assert report["differences"][0]["by"]["A"] == ["gfxtest", "parent"]
    assert {d["leaf"] for d in report["declared"]} == {"proc.gfxtest", "file./sys/bus/pci", "file./sys/bus/pci/"}


def test_a_path_only_another_process_touched_or_only_the_candidate_has_is_not_covered_by_the_gfxtest_entry():
    a = with_files(footprint(), ["/etc/only-the-parent-has"], {"/etc/only-the-parent-has": ["parent"]})
    report = judge(a, a_candidate_without_gfxtest(), expected=expected())
    assert report["verdict"] == "red" and {d["leaf"] for d in report["differences"]} == {"file./etc/only-the-parent-has"}
    c = with_files(a_candidate_without_gfxtest(), ["/sys/bus/pci/candidate"], {"/sys/bus/pci/candidate": ["gfxtest"]})
    report = judge(footprint(), c, expected=expected())
    assert report["verdict"] == "red" and {d["leaf"] for d in report["differences"]} == {"file./sys/bus/pci/candidate"}, "the entry is the published engine having it, never the candidate"


def test_a_path_only_the_gfxtest_touched_in_a_seed_where_both_sides_have_it_is_a_difference():
    a = with_files(footprint(), GFX_PCI, GFX_ONLY)
    report = judge(a, footprint("candidate"), expected=expected())
    assert report["verdict"] == "red" and {d["leaf"] for d in report["differences"]} >= {"file./sys/bus/pci", "file./sys/bus/pci/"}
    assert report["stale"] == ["gfxtest-69"]


def test_a_candidate_that_still_spawns_the_gfxtest_in_one_seed_does_not_show_the_declared_difference_and_lists_the_paths():
    """The kind is judged over all the launches (`file_rule`), not seed by seed: a candidate that has the gfxtest in one seed is not the 69 that no longer spawns it, the entry is stale
    and red, and the paths only the gfxtest touched stay differences like any path only one engine has (no process of the candidate touches them)."""
    a = with_files(footprint(), GFX_PCI, GFX_ONLY)
    c = footprint("candidate", kinds={k: n for k, n in KINDS.items() if k != "gfxtest"})
    c["seeds"][1]["kinds"] = dict(KINDS)
    report = judge(a, c, expected=expected())
    assert report["verdict"] == "red" and report["stale"] == ["gfxtest-69"] and report["declared"] == []
    assert {d["leaf"] for d in report["differences"]} == {"file." + p for p in GFX_PCI}, "no proc.gfxtest: the kind is in a launch of the candidate"
    assert {e["kind"] for e in report["not_judged"]["kinds"]} == {"gfxtest"}


def test_the_declared_list_may_only_shrink_and_stays_applied_by_kind():
    entries = expected()
    assert len(entries) == 1 and entries[0]["category"] == "proc" and entries[0]["name"] == "gfxtest" and "glxtest" not in json.dumps(entries)
    a = with_files(footprint(), GFX_PCI, GFX_ONLY)
    assert judge(a, a_candidate_without_gfxtest(), expected=expected(), a_a=True)["declared"] == [], "the A/A applies none"
    assert judge(footprint(platform="win32"), footprint("candidate", platform="win32"), expected=expected())["verdict"] == "green", "another OS: not applied, not stale"


# -
# Windows: the WOF stream of a file is the file.

WOF = ":wofcompresseddata"
SYS = "c:\\windows\\system32\\"
REAL_PLAIN = [SYS + "msauddecmft.dll", SYS + "en-us\\ole32.dll.mui", SYS + "ole32.dll"]
REAL_STREAMS = [SYS + "msauddecmft.dll" + WOF, SYS + "en-us\\ole32.dll.mui" + WOF]


def test_the_wof_stream_of_a_file_is_the_file_the_process_opened(tmp_path):
    """Run 45043617981: 31 paths `...:wofcompresseddata` only the first run of the 68 had (the files were opened by all three runs: the stream is read when the decompressed data
    is not cached, the first access after boot)."""
    def read(name, *events):
        xml = tmp_path / name
        xml.write_text(etw(*events), encoding="utf-8")
        return host.etw_files(xml, {100})[0]

    plain = read("plain.xml", *[(100, p) for p in REAL_PLAIN])
    streams = read("streams.xml", *[(100, p) for p in REAL_PLAIN + REAL_STREAMS])
    assert plain == {(100, p) for p in REAL_PLAIN}
    assert streams == plain, "a run with the stream variants and the plain paths is a run with the plain paths"
    assert read("only.xml", (100, REAL_STREAMS[0])) == {(100, REAL_PLAIN[0])}, "the stream alone is the file it belongs to"
    assert read("case.xml", (100, REAL_PLAIN[0] + ":WofCompressedData")) == {(100, REAL_PLAIN[0])}


def test_another_alternate_stream_is_a_path_of_its_own(tmp_path):
    names = [REAL_PLAIN[2], REAL_PLAIN[2] + ":Zone.Identifier", REAL_PLAIN[2] + ":other", WOF, "c:\\x" + WOF + "x"]
    xml = tmp_path / "other.xml"
    xml.write_text(etw(*[(100, n) for n in names]), encoding="utf-8")
    assert host.etw_files(xml, {100})[0] == {(100, n) for n in names}


# -
# The real lines of run 45060573559 (engine candidate, built with a wrapper without the fixes above), as data: the Windows A/A (job 137346793388) and the Linux A/B (job 137346793511).

def bs(paths):
    """The paths of the data below, written with `/`, as Windows spells them."""
    return [p.replace("/", chr(92)) for p in paths]


#: The 13 paths of the red of the Windows A/A "the system modules only run A has in its measured launches are 13, more than 5": every one is `<file>:wofcompresseddata`, only in the
#: warm-up of the first run of the 68 (7 of a resource file or a catalog, 6 of a library).
WIN_STREAMS = bs([
    "c:/windows/system32/catroot/{f750e6c3-38ee-11d1-85e5-00c04fc295ee}/microsoft-windows-server-features-package011~31bf3856ad364e35~amd64~~10.0.26100.1742.cat:wofcompresseddata",
    "c:/windows/system32/en-us/avrt.dll.mui:wofcompresseddata",
    "c:/windows/system32/en-us/networkexplorer.dll.mui:wofcompresseddata",
    "c:/windows/system32/en-us/powrprof.dll.mui:wofcompresseddata",
    "c:/windows/system32/en-us/windows.applicationmodel.dll.mui:wofcompresseddata",
    "c:/windows/system32/en-us/windows.system.launcher.dll.mui:wofcompresseddata",
    "c:/windows/system32/en-us/wpnapps.dll.mui:wofcompresseddata",
    "c:/windows/system32/evr.dll:wofcompresseddata",
    "c:/windows/system32/mfperfhelper.dll:wofcompresseddata",
    "c:/windows/system32/mp3dmod.dll:wofcompresseddata",
    "c:/windows/system32/msauddecmft.dll:wofcompresseddata",
    "c:/windows/system32/msdmo.dll:wofcompresseddata",
    "c:/windows/system32/msmpeg2vdec.dll:wofcompresseddata",
])
#: The plain paths that same warm-up has and the second run does not: the files of 7 of the 13 above, and 10 more (13 resource files and 4 catalogs in all).
WIN_ONLY_A = bs([
    "c:/windows/system32/catroot/{f750e6c3-38ee-11d1-85e5-00c04fc295ee}/microsoft-windows-server-features-package011~31bf3856ad364e35~amd64~~10.0.26100.1742.cat",
    "c:/windows/system32/catroot/{f750e6c3-38ee-11d1-85e5-00c04fc295ee}/microsoft-windows-server-features-package012~31bf3856ad364e35~amd64~~10.0.26100.33828.cat",
    "c:/windows/system32/catroot/{f750e6c3-38ee-11d1-85e5-00c04fc295ee}/microsoft-windows-server-features-package013~31bf3856ad364e35~amd64~~10.0.26100.33828.cat",
    "c:/windows/system32/catroot/{f750e6c3-38ee-11d1-85e5-00c04fc295ee}/microsoft-windows-server-features-package03~31bf3856ad364e35~amd64~~10.0.26100.33828.cat",
    "c:/windows/system32/en-us/avrt.dll.mui",
    "c:/windows/system32/en-us/explorerframe.dll.mui",
    "c:/windows/system32/en-us/fwpuclnt.dll.mui",
    "c:/windows/system32/en-us/iertutil.dll.mui",
    "c:/windows/system32/en-us/mmdevapi.dll.mui",
    "c:/windows/system32/en-us/mscms.dll.mui",
    "c:/windows/system32/en-us/networkexplorer.dll.mui",
    "c:/windows/system32/en-us/powrprof.dll.mui",
    "c:/windows/system32/en-us/setupapi.dll.mui",
    "c:/windows/system32/en-us/urlmon.dll.mui",
    "c:/windows/system32/en-us/windows.applicationmodel.dll.mui",
    "c:/windows/system32/en-us/windows.system.launcher.dll.mui",
    "c:/windows/system32/en-us/wpnapps.dll.mui",
])
#: The libraries of those resource files (in both runs, but networkexplorer.dll, which only the first has; three of them are in a long launch) and the 6 libraries the streams belong to.
WIN_LIBS = bs([
    "c:/windows/system32/avrt.dll",
    "c:/windows/system32/explorerframe.dll",
    "c:/windows/system32/fwpuclnt.dll",
    "c:/windows/system32/iertutil.dll",
    "c:/windows/system32/mmdevapi.dll",
    "c:/windows/system32/mscms.dll",
    "c:/windows/system32/networkexplorer.dll",
    "c:/windows/system32/powrprof.dll",
    "c:/windows/system32/setupapi.dll",
    "c:/windows/system32/urlmon.dll",
    "c:/windows/system32/windows.applicationmodel.dll",
    "c:/windows/system32/windows.system.launcher.dll",
    "c:/windows/system32/wpnapps.dll",
])
WIN_LIBS_LATE = bs(["c:/windows/system32/networkexplorer.dll", "c:/windows/system32/iertutil.dll", "c:/windows/system32/urlmon.dll"])
WIN_STREAM_LIBS = bs([
    "c:/windows/system32/evr.dll",
    "c:/windows/system32/mfperfhelper.dll",
    "c:/windows/system32/mp3dmod.dll",
    "c:/windows/system32/msauddecmft.dll",
    "c:/windows/system32/msdmo.dll",
    "c:/windows/system32/msmpeg2vdec.dll",
])


def etw_names(tmp_path, name, names):
    """What the oracle reads of one launch: the names of the file events of an ETW export, through `etw_files` (where a WOF stream becomes its file), sorted."""
    xml = tmp_path / name
    xml.write_text(etw(*[(100, n) for n in names]), encoding="utf-8")
    return sorted({n for _pid, n in host.etw_files(xml, {100})[0]})


def real_win_runs(tmp_path):
    """The two runs of the 68 of the A/A as the oracle reads them (the footprint of each launch is what `etw_files` returns): run A has the stream variants and the plain paths in its
    warm-up, run B the plain libraries only. Returns the run A, the run B and the warm-up of A."""
    first = [p for p in WIN_LIBS if p not in WIN_LIBS_LATE]
    a_warm = etw_names(tmp_path, "a_warm.xml", WIN_STREAMS + WIN_ONLY_A + first + WIN_STREAM_LIBS)
    a_idle = etw_names(tmp_path, "a_idle.xml", WIN_LIBS_LATE)
    b_warm = etw_names(tmp_path, "b_warm.xml", first + WIN_STREAM_LIBS)
    b_idle = etw_names(tmp_path, "b_idle.xml", [p for p in WIN_LIBS_LATE if not p.endswith("networkexplorer.dll")])
    return with_system(win(), {"warmup": a_warm, "idle": a_idle}), with_system(win("again"), {"warmup": b_warm, "idle": b_idle}), a_warm


def test_the_13_wof_streams_of_run_45060573559_are_their_files_and_the_a_a_has_no_problem(tmp_path):
    a, b, a_warm = real_win_runs(tmp_path)
    assert len(WIN_STREAMS) == 13 and not any(p.endswith(host.WOF_STREAM) for p in a_warm), "no stream is left in the footprint of the run"
    assert all(host.wof_base(p) in a_warm for p in WIN_STREAMS), "each of the 13 is a file the run has"
    report = judge(a, b, a_a=True)
    assert report["problems"] == [] and report["verdict"] == "green" and report["differences"] == []
    assert not any(e["path"].endswith(host.WOF_STREAM) for e in report["not_judged"]["paths"]), "none of the 13 is a path of its own"


def test_the_same_13_streams_are_paths_of_their_own_listed_and_not_red_without_the_mapping_of_a_stream_to_its_file(tmp_path, monkeypatch):
    monkeypatch.setattr(host, "wof_base", lambda name: name)
    a, b, a_warm = real_win_runs(tmp_path)
    assert [p for p in a_warm if p.endswith(host.WOF_STREAM)] == sorted(WIN_STREAMS)
    report = judge(a, b, a_a=True)
    assert report["verdict"] == "green", "the real 13 are in the warm-up of run A only: not in all its measured launches"
    assert sorted(e["path"] for e in report["not_judged"]["paths"] if e["path"].endswith(host.WOF_STREAM)) == sorted(WIN_STREAMS)


# -
# Linux: the 45 differences of the A/B (5 seeds, 9 each): 15 of the probe gfxtest, 30 of where the two engines were installed.

#: The DIFF lines of the job as the oracle printed them. The root of the runner's checkout is a neutral one here (the structure is kept: `/home` is `/srv`, `/home/<user>/work` is
#: `/srv/ci/work`), because the disclosure scanner refuses local paths.
REAL_AB_LINES = """
seed 0 file./srv: firefox-68 "<missing>" | candidate 1 (by A: - | B: Isolated Web Co, Privileged Cont, Web Content, WebExtensions, forkserver, via readlink)
seed 0 file./srv/ci/work: firefox-68 "<missing>" | candidate 1 (by A: - | B: Isolated Web Co, Privileged Cont, Web Content, WebExtensions, forkserver, via readlink)
seed 0 file./srv/ci/work/firefox_antidetect_patch: firefox-68 "<missing>" | candidate 1 (by A: - | B: Isolated Web Co, Privileged Cont, Web Content, WebExtensions, forkserver, via readlink)
seed 0 file./srv/ci/work/firefox_antidetect_patch/firefox_antidetect_patch: firefox-68 "<missing>" | candidate 1 (by A: - | B: Isolated Web Co, Privileged Cont, Web Content, WebExtensions, forkserver, via readlink)
seed 0 file./sys/bus/pci: firefox-68 1 | candidate "<missing>" (by A: gfxtest | B: -, via access)
seed 0 file./sys/bus/pci/: firefox-68 1 | candidate "<missing>" (by A: gfxtest | B: -, via access)
seed 0 file./tmp/ab-host-fetch-rhvlva0e: firefox-68 1 | candidate "<missing>" (by A: Isolated Web Co, Privileged Cont, Web Content, WebExtensions, forkserver | B: -, via readlink)
seed 0 file./tmp/ab-host-fetch-rhvlva0e/engines: firefox-68 1 | candidate "<missing>" (by A: Isolated Web Co, Privileged Cont, Web Content, WebExtensions, forkserver | B: -, via readlink)
seed 0 proc.gfxtest: firefox-68 1 | candidate "<missing>" (by A: parent | B: -)
seed 2 file./srv: firefox-68 "<missing>" | candidate 1 (by A: - | B: Isolated Web Co, Web Content, WebExtensions, forkserver, via readlink)
seed 2 file./srv/ci/work: firefox-68 "<missing>" | candidate 1 (by A: - | B: Isolated Web Co, Web Content, WebExtensions, forkserver, via readlink)
seed 2 file./srv/ci/work/firefox_antidetect_patch: firefox-68 "<missing>" | candidate 1 (by A: - | B: Isolated Web Co, Web Content, WebExtensions, forkserver, via readlink)
seed 2 file./srv/ci/work/firefox_antidetect_patch/firefox_antidetect_patch: firefox-68 "<missing>" | candidate 1 (by A: - | B: Isolated Web Co, Web Content, WebExtensions, forkserver, via readlink)
seed 2 file./sys/bus/pci: firefox-68 1 | candidate "<missing>" (by A: gfxtest | B: -, via access)
seed 2 file./sys/bus/pci/: firefox-68 1 | candidate "<missing>" (by A: gfxtest | B: -, via access)
seed 2 file./tmp/ab-host-fetch-rhvlva0e: firefox-68 1 | candidate "<missing>" (by A: Isolated Web Co, Web Content, WebExtensions, forkserver | B: -, via readlink)
seed 2 file./tmp/ab-host-fetch-rhvlva0e/engines: firefox-68 1 | candidate "<missing>" (by A: Isolated Web Co, Web Content, WebExtensions, forkserver | B: -, via readlink)
seed 2 proc.gfxtest: firefox-68 1 | candidate "<missing>" (by A: parent | B: -)
seed 4 file./srv: firefox-68 "<missing>" | candidate 1 (by A: - | B: Isolated Web Co, Privileged Cont, Web Content, WebExtensions, forkserver, via readlink)
seed 4 file./srv/ci/work: firefox-68 "<missing>" | candidate 1 (by A: - | B: Isolated Web Co, Privileged Cont, Web Content, WebExtensions, forkserver, via readlink)
seed 4 file./srv/ci/work/firefox_antidetect_patch: firefox-68 "<missing>" | candidate 1 (by A: - | B: Isolated Web Co, Privileged Cont, Web Content, WebExtensions, forkserver, via readlink)
seed 4 file./srv/ci/work/firefox_antidetect_patch/firefox_antidetect_patch: firefox-68 "<missing>" | candidate 1 (by A: - | B: Isolated Web Co, Privileged Cont, Web Content, WebExtensions, forkserver, via readlink)
seed 4 file./sys/bus/pci: firefox-68 1 | candidate "<missing>" (by A: gfxtest | B: -, via access)
seed 4 file./sys/bus/pci/: firefox-68 1 | candidate "<missing>" (by A: gfxtest | B: -, via access)
seed 4 file./tmp/ab-host-fetch-rhvlva0e: firefox-68 1 | candidate "<missing>" (by A: Isolated Web Co, Web Content, WebExtensions, forkserver | B: -, via readlink)
seed 4 file./tmp/ab-host-fetch-rhvlva0e/engines: firefox-68 1 | candidate "<missing>" (by A: Isolated Web Co, Web Content, WebExtensions, forkserver | B: -, via readlink)
seed 4 proc.gfxtest: firefox-68 1 | candidate "<missing>" (by A: parent | B: -)
seed 6 file./srv: firefox-68 "<missing>" | candidate 1 (by A: - | B: Isolated Web Co, Privileged Cont, Web Content, WebExtensions, forkserver, via readlink)
seed 6 file./srv/ci/work: firefox-68 "<missing>" | candidate 1 (by A: - | B: Isolated Web Co, Privileged Cont, Web Content, WebExtensions, forkserver, via readlink)
seed 6 file./srv/ci/work/firefox_antidetect_patch: firefox-68 "<missing>" | candidate 1 (by A: - | B: Isolated Web Co, Privileged Cont, Web Content, WebExtensions, forkserver, via readlink)
seed 6 file./srv/ci/work/firefox_antidetect_patch/firefox_antidetect_patch: firefox-68 "<missing>" | candidate 1 (by A: - | B: Isolated Web Co, Privileged Cont, Web Content, WebExtensions, forkserver, via readlink)
seed 6 file./sys/bus/pci: firefox-68 1 | candidate "<missing>" (by A: gfxtest | B: -, via access)
seed 6 file./sys/bus/pci/: firefox-68 1 | candidate "<missing>" (by A: gfxtest | B: -, via access)
seed 6 file./tmp/ab-host-fetch-rhvlva0e: firefox-68 1 | candidate "<missing>" (by A: Isolated Web Co, Privileged Cont, Web Content, WebExtensions, forkserver | B: -, via readlink)
seed 6 file./tmp/ab-host-fetch-rhvlva0e/engines: firefox-68 1 | candidate "<missing>" (by A: Isolated Web Co, Privileged Cont, Web Content, WebExtensions, forkserver | B: -, via readlink)
seed 6 proc.gfxtest: firefox-68 1 | candidate "<missing>" (by A: parent | B: -)
seed 63 file./srv: firefox-68 "<missing>" | candidate 1 (by A: - | B: Isolated Web Co, Privileged Cont, Web Content, WebExtensions, forkserver, via readlink)
seed 63 file./srv/ci/work: firefox-68 "<missing>" | candidate 1 (by A: - | B: Isolated Web Co, Privileged Cont, Web Content, WebExtensions, forkserver, via readlink)
seed 63 file./srv/ci/work/firefox_antidetect_patch: firefox-68 "<missing>" | candidate 1 (by A: - | B: Isolated Web Co, Privileged Cont, Web Content, WebExtensions, forkserver, via readlink)
seed 63 file./srv/ci/work/firefox_antidetect_patch/firefox_antidetect_patch: firefox-68 "<missing>" | candidate 1 (by A: - | B: Isolated Web Co, Privileged Cont, Web Content, WebExtensions, forkserver, via readlink)
seed 63 file./sys/bus/pci: firefox-68 1 | candidate "<missing>" (by A: gfxtest | B: -, via access)
seed 63 file./sys/bus/pci/: firefox-68 1 | candidate "<missing>" (by A: gfxtest | B: -, via access)
seed 63 file./tmp/ab-host-fetch-rhvlva0e: firefox-68 1 | candidate "<missing>" (by A: Isolated Web Co, Privileged Cont, Web Content, WebExtensions, forkserver | B: -, via readlink)
seed 63 file./tmp/ab-host-fetch-rhvlva0e/engines: firefox-68 1 | candidate "<missing>" (by A: Isolated Web Co, Privileged Cont, Web Content, WebExtensions, forkserver | B: -, via readlink)
seed 63 proc.gfxtest: firefox-68 1 | candidate "<missing>" (by A: parent | B: -)
""".strip().splitlines()

REAL_LINE = re.compile(r'^seed (\d+) ((?:file|proc)\.\S+): firefox-68 ("<missing>"|\d+) \| candidate ("<missing>"|\d+) \(by A: (.*?) \| B: (.*?)(?:, via (\w+))?\)$')
NO_PROBE = {k: n for k, n in KINDS.items() if k != "gfxtest"}


def real_ab_entries():
    """The 45 lines as {seed, leaf, a, b, by: {A, B}, via}."""
    out = []
    for line in REAL_AB_LINES:
        m = REAL_LINE.match(line)
        assert m, line
        seed, leaf, a, b, by_a, by_b, via = m.groups()
        value = lambda v: host.MISSING if v == '"<missing>"' else int(v)
        names = lambda text: [] if text == "-" else text.split(", ")
        out.append({"seed": int(seed), "leaf": leaf, "a": value(a), "b": value(b), "by": {"A": names(by_a), "B": names(by_b)}, "via": via})
    return out


def is_gfx(entry):
    return entry["leaf"] in ("proc.gfxtest", "file./sys/bus/pci", "file./sys/bus/pci/")


def real_ab_footprints(entries):
    """The footprints of the 68 and of the candidate: the seeds of `entries`, the same base in both, and where a line says one side has a leaf, that side has it (with who touched it
    and the call, or who spawned it)."""
    seeds = sorted({e["seed"] for e in entries})
    a = {"format": host.FORMAT, "engine": "firefox-68", "platform": "linux", "problems": [], "seeds": [seed_footprint(s, kinds=KINDS) for s in seeds]}
    b = {"format": host.FORMAT, "engine": "candidate", "platform": "linux", "problems": [], "seeds": [seed_footprint(s, kinds=NO_PROBE) for s in seeds]}
    for e in entries:
        for side, fp, value in (("A", a, e["a"]), ("B", b, e["b"])):
            if value == host.MISSING:
                continue
            seed = next(s for s in fp["seeds"] if s["seed"] == e["seed"])
            category, _dot, name = e["leaf"].partition(".")
            if category == "proc":
                seed["spawned_by"] = dict(seed.get("spawned_by", {}), **{name: e["by"][side]})
            else:
                seed["files"] = seed["files"] + [name]
                seed["touched_by"] = dict(seed.get("touched_by", {}), **{name: e["by"][side]})
                seed["touched_calls"] = dict(seed.get("touched_calls", {}), **{name: [e["via"]]})
    return a, b


LOOSE = {"proc": 1, "file": 1}


def test_the_45_real_lines_are_5_seeds_by_15_of_the_probe_and_30_of_the_install_and_the_footprints_built_from_them_give_them_back():
    entries = real_ab_entries()
    assert len(entries) == 45 and {e["seed"] for e in entries} == {0, 2, 4, 6, 63}
    assert sum(is_gfx(e) for e in entries) == 15 and sum(not is_gfx(e) for e in entries) == 30
    a, b = real_ab_footprints(entries)
    report = judge(a, b, expected=[], floors=LOOSE)
    got = sorted((d["seed"], d["leaf"], d["a"], d["b"], d["by"]["A"], d["by"]["B"]) for d in report["differences"])
    assert got == sorted((e["seed"], e["leaf"], e["a"], e["b"], e["by"]["A"], e["by"]["B"]) for e in entries), "the same 45, nothing is normalised"


def test_the_15_real_gfxtest_lines_are_covered_by_the_declared_entry_and_the_entry_is_not_stale():
    entries = [e for e in real_ab_entries() if is_gfx(e)]
    assert len(entries) == 15 and sorted({e["leaf"] for e in entries}) == ["file./sys/bus/pci", "file./sys/bus/pci/", "proc.gfxtest"]
    assert all(e["by"] == {"A": ["gfxtest"], "B": []} for e in entries if e["leaf"].startswith("file.")) and all(e["a"] == 1 and e["b"] == host.MISSING for e in entries)
    a, b = real_ab_footprints(entries)
    report = judge(a, b, expected=expected(), floors=LOOSE)
    assert report["verdict"] == "green", report["problems"]
    assert report["differences"] == [] and report["stale"] == [] and {d["id"] for d in report["declared"]} == {"gfxtest-69"}
    assert sorted((d["seed"], d["leaf"]) for d in report["declared"]) == sorted((e["seed"], e["leaf"]) for e in entries), "all 15, none more"
    assert len(judge(a, b, expected=[], floors=LOOSE)["differences"]) == 15, "without the entry they are red"


def test_the_30_real_install_lines_are_red_from_two_roots_and_gone_when_both_engines_run_from_the_staged_folder(monkeypatch):
    """The fix is at the source: every engine is run from `stage_dir()`, so the links read of its ancestors are the same leaves in both runs. The comparison is as strict as it was: the
    footprints of two different roots still give the 30; each engine run from the staged folder is the footprint of the same ancestors in both, and nothing is left."""
    monkeypatch.setenv("RUNNER_TEMP", "/srv/ci/work/_temp")
    entries = [e for e in real_ab_entries() if not is_gfx(e)]
    assert len(entries) == 30
    a, b = real_ab_footprints(entries)
    red = judge(a, b, expected=expected(), floors=LOOSE)
    assert red["verdict"] == "red" and len(red["differences"]) == 30 and len(red["declared"]) == 5, "from two roots they are still red (the probe is declared, the 5 seeds of it)"
    stage = Path(host.stage_dir().as_posix())
    ancestors = [p.as_posix() for p in stage.parents if p.as_posix() not in ("/", ".")]
    assert ancestors == ["/srv/ci/work/_temp/ab-host/engine", "/srv/ci/work/_temp/ab-host", "/srv/ci/work/_temp", "/srv/ci/work", "/srv/ci", "/srv"]
    install = {e["leaf"][len("file."):] for e in entries}
    for side, fp in (("A", a), ("B", b)):
        for seed in fp["seeds"]:
            by = next(e["by"][side] for e in entries if e["seed"] == seed["seed"] and e["by"][side])
            seed["files"] = [p for p in seed["files"] if p not in install] + ancestors
            seed["touched_by"] = dict({p: v for p, v in seed.get("touched_by", {}).items() if p not in install}, **{p: by for p in ancestors})
            seed["touched_calls"] = dict({p: v for p, v in seed.get("touched_calls", {}).items() if p not in install}, **{p: ["readlink"] for p in ancestors})
    staged = judge(a, b, expected=expected(), floors=LOOSE)
    assert staged["verdict"] == "green" and staged["differences"] == [] and staged["problems"] == [], "no difference left"
    assert len(entries) + sum(is_gfx(e) for e in real_ab_entries()) == len(real_ab_entries()) == 45


def test_a_staged_ancestor_that_only_one_run_read_is_still_a_difference(monkeypatch):
    monkeypatch.setenv("RUNNER_TEMP", "/srv/ci/work/_temp")
    a = with_files(footprint(), ["/srv/ci/work/_temp"], {"/srv/ci/work/_temp": ["forkserver"]}, {"/srv/ci/work/_temp": ["readlink"]})
    report = judge(a, footprint("candidate"), expected=[])
    assert report["verdict"] == "red" and {d["leaf"] for d in report["differences"]} == {"file./srv/ci/work/_temp"}


# -
# Glean: a launch with a page waits for it right after its first page, and the list of `datareporting` is saved before the close.

def test_the_wait_for_glean_reads_the_profile_of_the_launch_says_how_long_and_a_condition_not_met_is_a_problem_that_does_not_stop_the_launch(monkeypatch, capsys):
    seen = []

    class GleanNotReady(Exception):
        pass

    # `raising=False`: the names are the contract of scripts/ci_crash_evidence.py (`wait_for_glean(profile)` and `GleanNotReady`), written by the other half of this work
    monkeypatch.setattr(host.crash, "GleanNotReady", GleanNotReady, raising=False)
    monkeypatch.setattr(host.crash, "wait_for_glean", lambda profile: seen.append(profile) or 1.5, raising=False)
    profile = Path("/tmp/invisible_profile_x")
    assert host.wait_for_glean_of_launch(lambda: profile, 11, "measured") is None and seen == [profile]
    assert "glean ready after 1.5 s (seed 11, launch measured)" in capsys.readouterr().out

    def not_ready(profile):
        raise GleanNotReady("Glean did not initialise in 60 s: no data.safe.bin")

    monkeypatch.setattr(host.crash, "wait_for_glean", not_ready, raising=False)
    assert host.wait_for_glean_of_launch(lambda: profile, 12, "idle") == "Glean did not initialise in 60 s: no data.safe.bin"
    assert "glean NOT ready (seed 12, launch idle)" in capsys.readouterr().out


def test_run_seed_waits_for_glean_after_the_first_page_and_before_the_work_of_the_launch_in_the_measured_and_the_idle_launch_and_has_no_bare_launch():
    source = inspect.getsource(host.run_seed)
    measured = source[source.index("            else:\n                browser = p.firefox.connect"):]
    idle = source[source.index("            elif idle:"):source.index("            else:\n                browser = p.firefox.connect")]
    assert measured.index("page = context.new_page()") < measured.index("wait_for_glean_of_launch(") < measured.index("journey = profile_p4.Journey(") < measured.index("for n, (name, method) in enumerate(JOURNEY, 1)")
    assert idle.index("context.new_page()") < idle.index("wait_for_glean_of_launch(") < idle.index("wait_for_notification(profile_prefs_reader(before))")
    for part in (idle, measured):
        assert part.index("save_datareporting(") < part.index("context.close()"), "the list is taken just before the close"
    assert source.count("wait_for_glean_of_launch(") == 2
    assert "bare" not in source and "bare" not in inspect.signature(host.run_seed).parameters
    assert "close_after_glean" not in inspect.getsource(host), "the wait is not made at the close any more"
    assert not [name for name in dir(host) if "bare" in name.lower()], "no bare launch is left in the oracle"


def test_the_list_of_datareporting_is_relative_sorted_with_sizes_and_a_read_error_is_a_line_of_the_file(tmp_path, monkeypatch):
    profile = tmp_path / "invisible_profile_x"
    (profile / "datareporting" / "glean" / "db").mkdir(parents=True)
    (profile / "datareporting" / "glean" / "db" / "data.safe.bin").write_bytes(b"x" * 7)
    (profile / "datareporting" / "state.json").write_text("{}", encoding="utf-8")
    (profile / "datareporting" / "glean" / "pending_pings").mkdir()
    assert host.datareporting_listing(profile) == "path\tsize\nglean/db/data.safe.bin\t7\nstate.json\t2\n"
    host.save_datareporting(profile, tmp_path / "out" / "dr", "measured", 11)
    assert (tmp_path / "out" / "dr" / "measured-11.tsv").read_text(encoding="utf-8") == "path\tsize\nglean/db/data.safe.bin\t7\nstate.json\t2\n"
    assert host.datareporting_listing(tmp_path / "nowhere").startswith("path\tsize\n# error\t")
    assert host.datareporting_listing(None) == "path\tsize\n# error\tthe profile of the launch was not found\n"
    real = Path.stat

    def broken(self, *a, **k):
        if self.name == "state.json":
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "stat", broken)
    assert "# error\t" in host.datareporting_listing(profile) and "state.json" in host.datareporting_listing(profile)
    host.save_datareporting(profile, None, "idle", 11)  # no folder: nothing happens


def test_an_engine_run_hands_the_datareporting_folder_to_every_launch_and_the_run_command_puts_it_next_to_the_footprint(tmp_path, monkeypatch):
    got = []

    def fake_run_seed(binary, seed, work, name=None, idle=False, datareporting_dir=None, audit_dir=None):
        got.append((name, datareporting_dir, audit_dir))
        return seed_footprint(seed)

    monkeypatch.setattr(host, "run_seed", fake_run_seed)
    monkeypatch.setattr(host.os.path, "realpath", lambda p: p)
    host.run_engine("ff", "linux-x86_64", [11], "firefox-68", None, tmp_path / "dr", tmp_path / "audit")
    assert got == [("warmup", tmp_path / "dr", tmp_path / "audit"), (None, tmp_path / "dr", tmp_path / "audit"), ("idle", tmp_path / "dr", tmp_path / "audit")]
    seen = {}
    monkeypatch.setattr(host, "run_engine", lambda binary, leg, seeds, label, crash_dir=None, datareporting_dir=None, audit_dir=None:
                        seen.update(crash=crash_dir, dr=datareporting_dir, audit=audit_dir) or {"format": host.FORMAT, "problems": []})
    (tmp_path / "o").mkdir()
    host.main(["run", "ff", "--leg", "linux-x86_64", "--out", str(tmp_path / "o" / "footprint-68.json"), "--seeds", "11"])
    assert seen["dr"] == tmp_path / "o" / "footprint-68.datareporting" and seen["crash"] == tmp_path / "o" / "footprint-68.crash"
    assert seen["audit"] == tmp_path / "o" / "footprint-68.audit", "the raw events of the audit of every launch go in the folder that is the artifact, next to the datareporting lists"


# -
# A stock control that stands still says where (run 45153612447, PR #578, Windows: more than 26 minutes with no word), and the test that runs the oracle kills its whole tree.

def test_the_host_test_runs_the_oracle_through_run_tree_and_not_through_a_pipe_a_grandchild_holds():
    source = (Path(__file__).resolve().parent / "test_ab_host_e2e.py").read_text(encoding="utf-8")
    assert "crash.run_tree(" in source and "timeout_message(" in source and "done.timed_out" in source
    assert "subprocess.run(" not in source and "capture_output" not in source, "the child alone is killed at the timeout and communicate() waits for the grandchild that holds the pipe"
    assert "TIMEOUT_S = 35 * 60" in source


def test_the_launches_and_the_stock_control_write_the_launch_line_the_test_reads_back_when_the_run_does_not_end(capsys, monkeypatch):
    assert "crash.launch_line(seed, launch_name, HOST_LABEL)" in inspect.getsource(host.run_seed)
    line = host.crash.launch_line(11, "measured", host.HOST_LABEL)
    assert host.crash.last_launch("x\n" + line + "\n")["launch"] == "measured"
    assert "crash.launch_line(0, \"stock\", STOCK_LABEL)" in inspect.getsource(host.run_stock)


class FakeProc:
    def __init__(self, pid, name, cmdline, created):
        self.pid, self._name, self._cmd, self._created = pid, name, cmdline, created

    def name(self):
        return self._name

    def cmdline(self):
        return self._cmd

    def create_time(self):
        return self._created


def fake_psutil(monkeypatch, children):
    """A `psutil` whose process (the oracle) has these descendants."""
    import types

    class Process:
        def __init__(self, pid):
            self.pid = pid

        def children(self, recursive=False):
            assert recursive, "the browser's parent is a grandchild of the oracle (the driver's child)"
            return list(children)

    module = types.ModuleType("psutil")
    module.Process = Process
    monkeypatch.setitem(sys.modules, "psutil", module)


def phases(text):
    return [m.group(1) for m in re.finditer(r"^stock phase (.*?) at \d{4}-\d\d-\d\dT[\d:]+\+00:00$", text, re.M)]


def test_the_stock_step_says_each_call_before_it_makes_it_in_order_and_the_answer_after_it_and_a_declared_step_says_nothing(capsys):
    host.geolocation_step(FakeBrowser(), "http://127.0.0.1:9", None, 5)
    assert phases(capsys.readouterr().out) == ["new_context", "grant_permissions", "new_page", "goto", "evaluate begin", 'evaluate end: {"answer": "error", "code": 2}']
    host.geolocation_step(FakeBrowser(), "http://127.0.0.1:9", host.GEO_DECLARED, 5)
    assert "stock phase" not in capsys.readouterr().out


def test_a_stock_step_that_stands_still_leaves_the_last_phase_as_the_place_it_stood(capsys):
    class Stuck(FakePage):
        def evaluate(self, script):
            self.calls.append(("evaluate", script))
            raise RuntimeError("stuck")

    class B(FakeBrowser):
        def new_context(self, **options):
            context = FakeContext(self.calls, options)
            context.new_page = lambda: Stuck(self.calls)
            return context

    host.geolocation_step(B(), "http://127.0.0.1:9", None, 5)
    assert phases(capsys.readouterr().out) == ["new_context", "grant_permissions", "new_page", "goto", "evaluate begin"]


def test_run_stock_says_its_phases_from_the_trace_to_the_result_and_the_subprocess_prints_them_on_the_log(tmp_path, monkeypatch, capsys):
    exe = tmp_path / "firefox.exe"
    exe.write_text("x")

    class Browser(FakeBrowser):
        def close(self):
            self.calls.append(("browser-close",))

    fake_playwright(monkeypatch, exe, launch=Browser)
    monkeypatch.setattr(host, "WindowsTrace", FakeTrace)
    monkeypatch.setattr(host, "start_geo_lab", lambda: ("http://127.0.0.1:1", lambda: None))
    fake_psutil(monkeypatch, [FakeProc(4001, "firefox.exe", ["firefox.exe", "-no-remote"], 5.0)])
    monkeypatch.setattr(host, "_seed_result", lambda *a, **k: {"seed": 0, "steps": [], "kinds": {}, "files": []})
    document = host.run_stock("windows-x86_64")
    out = capsys.readouterr().out
    assert document["stock"]["started"] is True
    assert out.splitlines()[0].startswith("=== launch stock seed 0 of stock control began at ")
    assert phases(out) == ["trace start", "geolocation lab", "launch", "browser parent 4001", "new_context", "grant_permissions", "new_page", "goto", "evaluate begin",
                           'evaluate end: {"answer": "error", "code": 2}', "browser close", "seed result"]
    source = inspect.getsource(host.run_stock_subprocess)
    assert "crash.run_tree(" in source and "sys.stdout.write(done.stdout)" in source, "its output is run_tree's files, printed on the output of the oracle: the phases are in the log"


def test_a_stock_process_that_stands_still_is_a_broken_control_that_names_its_last_phase_within_the_limit_and_the_log_keeps_what_it_said(tmp_path, capsys, monkeypatch):
    import time

    script = ("import sys, time, datetime\n"
              "print('stock phase launch at', datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'), flush=True)\n"
              "print('stock phase evaluate begin at', datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'), flush=True)\n"
              "time.sleep(60)\n")
    out = tmp_path / "footprint-stock.json"
    began = time.monotonic()
    rc = host.run_stock_subprocess("windows-x86_64", out, limit_s=2, command=[sys.executable, "-u", "-c", script])
    assert time.monotonic() - began < 20 and rc == 0
    document = host.read_json(out)
    problems = host.stock_control_problems(document)
    assert len(problems) == 1 and "did not end within 2 s" in problems[0] and "`evaluate begin`" in problems[0] and "tree was killed" in problems[0]
    assert "does not reach the platform" not in problems[0] and document["stock"]["started"] is False
    printed = capsys.readouterr().out
    assert "stock phase evaluate begin at" in printed, "the log keeps the phases"
    # and the control reports it: the verdict is the broken control that says where it stood
    monkey_out = tmp_path / "o"
    monkey_out.mkdir()
    real = host.run_stock_subprocess
    monkeypatch.setattr(host, "run_stock_subprocess", lambda leg, path: real(leg, path, limit_s=2, command=[sys.executable, "-u", "-c", script]))
    assert host.run_stock_control("windows-x86_64", monkey_out) is None
    printed = capsys.readouterr().out
    assert "FAIL stock control: the stock control did not end within 2 s" in printed and "BROKEN CONTROL: the stock control did not run to the end" in printed


def test_the_stock_limit_is_short_and_declared_and_a_process_that_ends_returns_its_code_and_its_output(tmp_path, capsys):
    assert host.STOCK_LIMIT_S == 120 and host.STOCK_LIMIT_S < host.RUN_TIMEOUT_S
    assert host.last_stock_phase("stock phase a at 2026-11-23T10:00:00+00:00\nstock phase b c at 2026-11-23T10:00:05+00:00\n",
                                 now=host.datetime.datetime(2026, 11, 23, 10, 0, 9, tzinfo=host.datetime.timezone.utc)) == ("b c", host.datetime.timedelta(seconds=4))
    assert host.last_stock_phase("nothing") == (None, None)
    rc = host.run_stock_subprocess("windows-x86_64", tmp_path / "f.json", limit_s=30, command=[sys.executable, "-c", "print('hello'); raise SystemExit(3)"])
    assert rc == 3 and "hello" in capsys.readouterr().out and not (tmp_path / "f.json").exists()


def test_the_root_of_the_stock_trace_is_the_parent_of_the_firefox_playwright_launched_and_never_the_oracle(tmp_path, monkeypatch, capsys):
    """Run 45162128126: the oracle's own pid was the root, its tree cannot exit, and the trace waited all of EXIT_LIMIT_S (116 s in `seed result`) until the 120 s of the control ended it."""
    children = [FakeProc(3001, "node.exe", ["node.exe", "driver"], 1.0), FakeProc(4101, "firefox.exe", ["firefox.exe", "-contentproc", "-isForBrowser", "tab"], 4.0),
                FakeProc(4001, "Firefox.exe", ["firefox.exe", "-no-remote", "-profile", "x"], 3.0), FakeProc(4009, "firefox.exe", ["firefox.exe", "-no-remote"], 9.0)]
    fake_psutil(monkeypatch, children)
    assert host.stock_browser_parent() == 4001, "the oldest firefox.exe without -contentproc"
    exe = tmp_path / "firefox.exe"
    exe.write_text("x")

    class Browser(FakeBrowser):
        def close(self):
            pass

    fake_playwright(monkeypatch, exe, launch=Browser)
    monkeypatch.setattr(host, "WindowsTrace", FakeTrace)
    monkeypatch.setattr(host, "start_geo_lab", lambda: ("http://127.0.0.1:1", lambda: None))
    roots = []
    monkeypatch.setattr(host, "_seed_result", lambda *a, **k: roots.append(a[6]) or {"seed": 0, "steps": [], "kinds": {}, "files": []})
    host.run_stock("windows-x86_64")
    assert roots == [4001] and roots != [os.getpid()]
    assert "stock phase browser parent 4001 at" in capsys.readouterr().out


def test_a_stock_firefox_with_no_parent_found_is_a_broken_control_that_says_so_and_never_traces_the_oracle(tmp_path, monkeypatch, capsys):
    fake_psutil(monkeypatch, [FakeProc(4101, "firefox.exe", ["firefox.exe", "-contentproc", "tab"], 4.0), FakeProc(3001, "node.exe", ["node.exe"], 1.0)])
    exe = tmp_path / "firefox.exe"
    exe.write_text("x")

    class Browser(FakeBrowser):
        def close(self):
            self.calls.append(("browser-close",))

    fake_playwright(monkeypatch, exe, launch=Browser)
    monkeypatch.setattr(host, "WindowsTrace", FakeTrace)
    monkeypatch.setattr(host, "start_geo_lab", lambda: ("http://127.0.0.1:1", lambda: None))
    monkeypatch.setattr(host, "_seed_result", lambda *a, **k: pytest.fail("the trace must not be read with this process as its root"))
    document = host.run_stock("windows-x86_64")
    problems = host.stock_control_problems(document)
    assert len(problems) == 1 and "parent process of the stock Firefox of Playwright was not found" in problems[0] and "does not reach the platform" not in problems[0]


# -
# A pid is not a process (run 45221868374): Windows hands the pid of a process that ended to the next one it creates.

def audit_with_parent(rid, pid, ppid, image, cmd, parent_image):
    """A 4688 event that also carries the image the audit gives for the creator (`ParentProcessName`)."""
    return audit(rid, pid, ppid, image, cmd).replace("</EventData>", '<Data Name="ParentProcessName">%s</Data></EventData>' % parent_image)


def audit_end(rid, pid):
    """A 4689 event: the process `pid` ended."""
    return ('<Event %s><System><Provider Name="Microsoft-Windows-Security-Auditing"/><EventID>4689</EventID><EventRecordID>%d</EventRecordID></System>'
            '<EventData><Data Name="SubjectUserSid">S-1-5-18</Data><Data Name="Status">0x0</Data><Data Name="ProcessId">0x%x</Data>'
            '<Data Name="ProcessName">%s</Data></EventData></Event>' % (NS, rid, pid, FIREFOX))


def ev(rid, pid, ppid, image=FIREFOX, parent_image="", kind=""):
    return {"rid": rid, "pid": pid, "ppid": ppid, "image": image, "parent_image": parent_image, "cmd": "", "time": "", "kind": kind}


def test_the_audit_reads_the_end_of_a_process_and_the_image_of_its_creator():
    xml = "\n".join([audit_with_parent(5, 102, 100, FIREFOX, "x", "C:\\lab\\ff\\FIREFOX.exe"), audit_end(6, 102), audit(7, 103, 100, FIREFOX, "y")])
    events = host.parse_audit_events(xml)
    assert [(e["pid"], e["parent_image"]) for e in events] == [(102, "C:\\lab\\ff\\FIREFOX.exe"), (103, "")], "the end is not a creation; a creation without the creator's image says ''"
    assert host.parse_audit_ends(xml) == [{"rid": 6, "pid": 102, "image": FIREFOX, "time": ""}]
    assert host.parse_audit_ends("") == []


def test_a_child_born_while_its_creator_lives_stays_in_the_tree_though_the_pid_is_taken_later_by_a_stranger():
    events = [ev(1, 100, 50), ev(2, 101, 100), ev(3, 102, 101), ev(5, 101, 900, "C:\\Windows\\notepad.exe"), ev(6, 103, 101, "C:\\Windows\\notepad.exe"), ev(7, 104, 100)]
    ends = [{"rid": 4, "pid": 101}, {"rid": 8, "pid": 104}]
    assert [e["pid"] for e in host.process_tree(events, {100}, ends)] == [100, 101, 102, 104], "101 and its child were born alive; the stranger of record 5 and its child (6) are not"
    # without the ends the stranger is still seen: it carries the pid of a process of the tree and its creator is not in the tree, so the pid is no longer that process's
    assert [e["pid"] for e in host.process_tree(events, {100})] == [100, 101, 102, 104]
    assert [e["rid"] for e in host.process_tree(events, {100}, ends)] == [1, 2, 3, 7], "the births of the tree are the ones before the pid was taken"


def test_a_child_registered_after_the_end_of_its_creator_is_in_the_tree_while_the_pid_has_not_been_born_again():
    """Run 45228081536: the pingsender of the parent is registered (4688) after the end (4689) of the parent: the creator's end is not a reason to leave it out. Red on e3572f28, where
    the pid of a creator that had ended was no longer the tree's."""
    events = [ev(1, 100, 50), ev(2, 101, 100), ev(4, 105, 101, "C:\\lab\\ff\\pingsender.exe")]
    ends = [{"rid": 3, "pid": 101}]
    assert [e["pid"] for e in host.process_tree(events, {100}, ends)] == [100, 101, 105]
    assert [e["pid"] for e in host.process_tree(events, {100})] == [100, 101, 105], "the ends are read, they do not decide"


def test_a_child_of_a_pid_that_was_born_again_as_a_stranger_is_not_in_the_tree_the_ends_or_not():
    """Run 45221868374: `svchost -s wlidsvc`, `wevtutil` and `wsl` took pids that processes of the tree had had. The pid of the tree stops being the tree's only when the same pid is
    BORN again as a process that does not join; what is born from that pid after it is the stranger's."""
    events = [ev(1, 100, 50), ev(2, 101, 100), ev(4, 101, 900, "C:\\Windows\\System32\\svchost.exe"), ev(5, 105, 101, "C:\\Windows\\System32\\wevtutil.exe"), ev(6, 106, 100)]
    for ends in ([], [{"rid": 3, "pid": 101}]):
        assert [e["pid"] for e in host.process_tree(events, {100}, ends)] == [100, 101, 106], ends
    again = [ev(1, 100, 50), ev(2, 101, 100), ev(4, 101, 100), ev(5, 105, 101)]
    assert [e["pid"] for e in host.process_tree(again, {100})] == [100, 101, 101, 105], "the same pid born again from the tree is the tree's again"


def test_the_creator_the_audit_names_must_be_the_image_the_creator_was_born_with():
    events = [ev(1, 100, 50), ev(2, 101, 100, parent_image="c:\\LAB\\ff\\firefox.EXE"), ev(3, 102, 100, parent_image="C:\\Windows\\System32\\python.exe"), ev(4, 103, 100)]
    assert [e["pid"] for e in host.process_tree(events, {100})] == [100, 101, 103], "the same file name under another spelling or no name stays; python.exe is not firefox.exe"
    assert [e["pid"] for e in host.process_tree(events, {999})] == [], "a root that never appears has no tree"
    # a root born before the audit (no event of its own) has no image to compare: its children join by its pid
    assert [e["pid"] for e in host.process_tree([ev(4, 7, 100, parent_image="C:\\Windows\\System32\\python.exe")], {100})] == [7]


def reconstruct_idle_launch_of_run_45221868374():
    """The 4688 and 4689 events of the idle launch of run 45221868374, rebuilt from the rows of its lifecycle (kind, start or end, order): the footprint keeps no pid
    and no creator, so these are the test's. The real firefox processes: the first `parent` is the root (its creator is the oracle's pid 50), the second parent is its
    child, the tabs, the utility and the pingsender are the second parent's, the last socket is the pingsender's. What the run took into the tree besides them
    (`svchost -s wlidsvc`, `wevtutil`, `wsl`, and the socket of 16:59:20, born before any pingsender and whose command line names another parent) is given a pid
    Windows had already handed out: the pid of the process that ended last in the tree, when one had (a process taking the pid of one that is gone, its creator the
    oracle), and before any process ended, a new pid whose creator is the root's pid and whose audit names python.exe as that creator.
    Returns `(events, ends)`, each event with its `kind` and `legit`."""
    import collections

    rows = json.loads((Path(__file__).parent / "data" / "ab_host_pid_reuse" / "45221868374" / "idle-lifecycle.json").read_text(encoding="utf-8"))["lifecycle"]
    tools = {"wevtutil.exe": "C:\\Windows\\System32\\wevtutil.exe", "svchost.exe": "C:\\Windows\\System32\\svchost.exe", "wsl.exe": "C:\\Windows\\System32\\wsl.exe"}
    events, ends, running, dead, rid, fresh = [], [], {}, [], 0, iter(range(2000, 3000))
    seen = collections.Counter()
    for row in rows:
        kind, rid = row["kind"], rid + 1
        if row["event"] == "end":
            pid = running[kind].pop(0)
            ends.append({"rid": rid, "pid": pid})
            dead.append(pid)
            continue
        legit = kind in ("parent", "tab/isForBrowser", "utility/sandboxingKind=0", "backgroundtask/pingsender") or (kind == "socket" and seen["backgroundtask/pingsender"] > 0)
        if legit:
            pid = [1000, 1001][seen["parent"]] if kind == "parent" else next(fresh)
            creator = {"parent": [50, 1000][min(seen["parent"], 1)], "socket": (running.get("backgroundtask/pingsender") or [None])[0]}.get(kind, 1001)
            events.append(dict(ev(rid, pid, creator, FIREFOX, FIREFOX, kind), legit=True))
        else:
            if dead:
                pid, creator, said = dead.pop(), 50, ""
            else:
                pid, creator, said = next(fresh), 1000, "C:\\Windows\\System32\\python.exe"
            events.append(dict(ev(rid, pid, creator, tools.get(kind, FIREFOX), said, kind), legit=False))
        seen[kind] += 1
        running.setdefault(kind, []).append(pid)
    return events, ends


def test_the_idle_launch_of_run_45221868374_has_only_the_processes_of_the_engine_in_its_tree():
    events, ends = reconstruct_idle_launch_of_run_45221868374()
    intruders = [e["kind"] for e in events if not e["legit"]]
    assert sorted(intruders) == sorted(["svchost.exe"] * 2 + ["wevtutil.exe"] * 15 + ["wsl.exe"] * 2 + ["socket"]), "the rows of the run: what was not the engine's"
    tree = host.process_tree(events, {1000}, ends)
    assert [e["kind"] for e in tree if not e["legit"]] == [], "no service of the machine and no tool of the oracle is in the tree"
    assert sorted(e["kind"] for e in tree) == sorted(["parent", "parent", "tab/isForBrowser", "tab/isForBrowser", "utility/sandboxingKind=0", "backgroundtask/pingsender", "socket"]), \
        "every process of the engine is still in it"
    # and a tree that holds none of them is a tree that can exit: every process of it ended in the run
    assert {e["pid"] for e in tree} <= {x["pid"] for x in ends}


def _name(path):
    return re.split(r"[\\/]", path)[-1]


# -
# The raw events of the audit of every Windows launch are saved next to the footprint (run 45228081536: a process that was or was not in the tree could not be told from the artifact).

def a_launch_with_a_stranger_and_an_end(tmp_path, monkeypatch):
    stranger = audit_with_parent(30, 107, 5, "C:\\Windows\\System32\\svchost.exe", "svchost.exe -k netsvcs -p -s wlidsvc", "C:\\Windows\\System32\\services.exe")
    fake = FakeTools(tmp_path, TREE + [audit_end(20, 107), stranger], TREE_FILES)
    monkeypatch.setattr(host, "_tool", fake)
    trace = host.WindowsTrace(tmp_path)
    trace.start()
    got = trace.finish({100}, alive=lambda pid: False, sleep=lambda s: None)
    trace.cleanup()
    return got


def test_the_audit_of_a_launch_lists_every_event_it_read_the_excluded_ones_with_the_reason(tmp_path, monkeypatch):
    got = a_launch_with_a_stranger_and_an_end(tmp_path, monkeypatch)
    events, ends, roots = got["audit"]
    listing = host.audit_listing(events, roots, ends, "measured", 4)
    assert listing["format"] == host.AUDIT_FORMAT and (listing["launch"], listing["seed"], listing["roots"]) == ("measured", 4, [100])
    rows = listing["events"]
    assert [r["rid"] for r in rows] == sorted(r["rid"] for r in rows) and len(rows) == len(TREE) + 2, "every 4688 and 4689 the log gave, in the order of the log"
    by_rid = {r["rid"]: r for r in rows}
    assert (by_rid[10]["event"], by_rid[10]["pid"], by_rid[10]["ppid"], by_rid[10]["in_tree"]) == (4688, 100, 1, True) and "root" in by_rid[10]["why"]
    assert by_rid[12]["image"] == FIREFOX and by_rid[12]["in_tree"] and by_rid[12]["time"] == "" and by_rid[12]["cmd"].endswith("tab")
    assert (by_rid[15]["pid"], by_rid[15]["in_tree"]) == (105, False) and "creator (pid 999) is not a process of the tree" in by_rid[15]["why"]
    assert (by_rid[20]["event"], by_rid[20]["pid"], by_rid[20]["ppid"], by_rid[20]["in_tree"]) == (4689, 107, None, True)
    assert by_rid[20]["image"] == FIREFOX
    stranger = by_rid[30]
    assert (stranger["pid"], stranger["in_tree"], stranger["parent_image"]) == (107, False, "C:\\Windows\\System32\\services.exe") and "stranger" in stranger["why"]
    assert listing["in_tree"] == sum(1 for r in rows if r["in_tree"])


def test_a_creator_the_audit_names_with_another_image_is_said_in_the_reason():
    events = [ev(1, 100, 50, FIREFOX), ev(2, 101, 100, "C:\\Windows\\System32\\wevtutil.exe", parent_image="C:\\Windows\\System32\\python.exe")]
    (root, other) = host.walk_tree(events, {100})
    assert other["in_tree"] is False and "python.exe" in other["why"] and "firefox.exe" in other["why"]


def test_the_audit_of_a_launch_is_saved_as_launch_dash_seed_json_and_a_write_that_fails_is_said_not_raised(tmp_path, monkeypatch, capsys):
    got = a_launch_with_a_stranger_and_an_end(tmp_path, monkeypatch)
    folder = tmp_path / "footprint-68.audit"
    host.save_audit(folder, "measured", 4, got["audit"])
    host.save_audit(folder, "idle", 0, got["audit"])
    assert sorted(p.name for p in folder.iterdir()) == ["idle-0.json", "measured-4.json"]
    saved = json.loads((folder / "measured-4.json").read_text(encoding="utf-8"))
    assert saved["format"] == host.AUDIT_FORMAT and saved["events"][0]["rid"] == 10 and any(not r["in_tree"] for r in saved["events"])
    host.save_audit(None, "measured", 4, got["audit"])  # no folder: nothing happens
    host.save_audit(folder, "measured", 4, None)  # nothing read: nothing happens
    blocked = tmp_path / "file"
    blocked.write_text("x", encoding="utf-8")
    host.save_audit(blocked / "audit", "measured", 5, got["audit"])
    assert "the audit of the tree could not be saved (seed 5, launch measured)" in capsys.readouterr().out


def test_the_audit_goes_to_the_save_and_never_into_the_footprint_the_judge_reads(tmp_path, monkeypatch):
    got = a_launch_with_a_stranger_and_an_end(tmp_path, monkeypatch)

    class Done:
        def finish(self, roots):
            return got

    kept = []
    monkeypatch.setattr(host, "_context", lambda binary, windows, work: host.Context("win32", app=["D:\\lab\\ff"], temp=["D:\\lab\\tmp"], devices=DEVICES))
    fp = host._seed_result(FIREFOX, 4, tmp_path, [], tmp_path / "none.log", Done(), 100, True, None, kept.append)
    assert kept == [got["audit"]] and "audit" not in fp and "events" not in fp and "rid" not in json.dumps(fp)
    assert host.leaves_of(fp) == {}


def test_the_windows_trace_reads_the_ends_and_the_tree_leaves_the_stranger_that_took_a_pid_of_it(tmp_path, monkeypatch):
    stranger = audit(30, 107, 5, "C:\\Windows\\System32\\svchost.exe", "svchost.exe -k netsvcs -p -s wlidsvc")
    child = audit(31, 108, 107, "C:\\Windows\\System32\\wevtutil.exe", "wevtutil qe Security")
    fake = FakeTools(tmp_path, TREE + [audit_end(20, 107), stranger, child], TREE_FILES)
    monkeypatch.setattr(host, "_tool", fake)
    trace = host.WindowsTrace(tmp_path)
    trace.start()
    got = trace.finish({100}, alive=lambda pid: False, sleep=lambda s: None)
    trace.cleanup()
    assert [(e["pid"], _name(e["image"])) for e in got["tree"]][-1] == (107, "pingsender.exe") and len(got["tree"]) == 6, "the svchost that has the pid of the pingsender is not the pingsender"
    asked = [c for c in fake.calls if c[0] == "wevtutil" and "/c:1" not in c][0]
    assert "EventID=4689" in asked[3] and trace.ends_ok is True
    sets = [c for c in fake.calls if c[:2] == ["auditpol", "/set"]]
    assert [c[2] for c in sets] == ["/subcategory:" + host.AUDIT_GUID, "/subcategory:" + host.AUDIT_END_GUID, "/subcategory:" + host.AUDIT_END_GUID, "/subcategory:" + host.AUDIT_GUID]
    assert sets[1][-1] == "/success:enable" and sets[2][-2:] == ["/success:disable", "/failure:disable"], "the termination audit is turned on, and put back as it was"


def test_a_machine_that_cannot_audit_the_ends_says_so_and_tells_the_strangers_by_their_birth(tmp_path, monkeypatch):
    fake = FakeTools(tmp_path, TREE + [audit(30, 107, 5, "C:\\Windows\\System32\\svchost.exe", "svchost.exe -k x")], TREE_FILES)

    def refusing(argv, timeout=300.0):
        if argv[:2] == ["auditpol", "/set"] and argv[2] == "/subcategory:" + host.AUDIT_END_GUID:
            raise host.TraceError("auditpol /set failed")
        return fake(argv, timeout)

    monkeypatch.setattr(host, "_tool", refusing)
    trace = host.WindowsTrace(tmp_path)
    trace.start()
    got = trace.finish({100}, alive=lambda pid: False, sleep=lambda s: None)
    trace.cleanup()
    assert trace.ends_ok is False and got["problems"] == []
    assert [e["pid"] for e in got["tree"]] == [100, 101, 102, 103, 104, 107], "107 is the pingsender: the svchost that took its pid has a creator that is not in the tree"
    assert [c[2] for c in fake.calls if c[:2] == ["auditpol", "/set"]] == ["/subcategory:" + host.AUDIT_GUID] * 2


# -
# The session of a Windows launch (run 45257629988, seed 6, side A: `logman query <name> -ets` failed with 0x80071068 after `logman create` had succeeded, the launch had no trace).

def test_the_round_trip_before_the_canaries_is_the_list_of_the_sessions_and_never_the_query_of_one(tmp_path, monkeypatch):
    trace, fake = a_windows_run(tmp_path, monkeypatch, TREE_FILES)  # the fake fails `logman query <name>` with 2147946600, as the runner did
    queries = [c for c in fake.calls if c[:2] == ["logman", "query"]]
    assert queries == [["logman", "query", "-ets"]] * 3 and trace._start_canary
    assert trace.finish({100}, alive=lambda pid: False, sleep=lambda s: None)["problems"] == []


@pytest.mark.parametrize("listed", [{}, {"abhost-other": "Running"}, "stopped"])
def test_a_session_that_is_not_in_the_list_or_not_running_is_an_error_that_says_what_was_read(tmp_path, monkeypatch, listed):
    fake = FakeTools(tmp_path, TREE, TREE_FILES)

    def lists(argv, timeout=300.0):
        out = fake(argv, timeout)
        if argv == ["logman", "query", "-ets"]:
            return logman_list({trace.name: "Stopped"} if listed == "stopped" else listed)
        return out

    monkeypatch.setattr(host, "_tool", lists)
    trace, clock = host.WindowsTrace(tmp_path), Clock()
    with pytest.raises(host.TraceError, match="is not running in the list of `logman query -ets`") as err:
        trace.start(sleep=clock.sleep, now=clock.now, limit=1.0)
    assert clock.t >= 1.0, "read again every quarter of a second until the guard"
    assert trace.name in str(err.value) and ("Stopped" in str(err.value) if listed == "stopped" else "nothing of it" in str(err.value))
    assert ("abhost-other (Running)" in str(err.value)) == (listed == {"abhost-other": "Running"})


class Clock:
    """A clock that only the sleeps move: a wait for a condition is read without waiting."""

    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def now(self):
        return self.t


def test_a_stopped_session_that_is_still_listed_is_waited_for_and_then_the_launch_goes_on(tmp_path, monkeypatch):
    fake = FakeTools(tmp_path, TREE, TREE_FILES, still_listed=2)
    monkeypatch.setattr(host, "_tool", fake)
    trace, clock = host.WindowsTrace(tmp_path), Clock()
    trace.start()
    got = trace.finish({100}, alive=lambda pid: False, sleep=clock.sleep, now=clock.now)
    names = [c[:2] for c in fake.calls]
    after_stop = [c for c in fake.calls[names.index(["logman", "stop"]):] if c == ["logman", "query", "-ets"]]
    assert got["problems"] == [] and len(after_stop) == 3, "listed for two reads, gone at the third"
    assert trace.name not in fake.sessions and trace.session is False
    assert names.index(["logman", "stop"]) < max(i for i, c in enumerate(fake.calls) if c == ["logman", "query", "-ets"]) < names.index(["tracerpt", str(tmp_path / "files.etl")])


def test_a_stopped_session_that_never_leaves_the_list_is_an_error_after_the_guard(tmp_path, monkeypatch):
    fake = FakeTools(tmp_path, TREE, TREE_FILES, still_listed=10 ** 9)
    monkeypatch.setattr(host, "_tool", fake)
    trace, clock = host.WindowsTrace(tmp_path), Clock()
    trace.start()
    with pytest.raises(host.TraceError, match="is still in the list of `logman query -ets` 2 s after `logman stop`"):
        trace.finish({100}, alive=lambda pid: False, sleep=clock.sleep, now=clock.now, gone_limit=2.0)
    assert clock.t >= 2.0 and trace.session is True, "the session is not taken for closed"
    assert not any(c[:1] == ["tracerpt"] for c in fake.calls)


def test_a_stop_that_fails_because_the_session_is_gone_is_not_an_error_when_the_list_says_so(tmp_path, monkeypatch):
    fake = FakeTools(tmp_path, TREE, TREE_FILES)
    monkeypatch.setattr(host, "_tool", fake)
    trace = host.WindowsTrace(tmp_path)
    trace.start()
    fake.sessions.clear()  # the session ended by itself: `logman stop` says 4201
    assert trace.finish({100}, alive=lambda pid: False, sleep=lambda s: None, now=Clock().now)["exited"]["exited"] is True


def test_a_stop_that_fails_while_the_session_is_still_listed_is_an_error(tmp_path, monkeypatch):
    fake = FakeTools(tmp_path, TREE, TREE_FILES)

    def refusing(argv, timeout=300.0):
        if argv[:2] == ["logman", "stop"]:
            raise host.TraceError("logman stop failed with 0x80070005: Access is denied.", 5)
        return fake(argv, timeout)

    monkeypatch.setattr(host, "_tool", refusing)
    trace = host.WindowsTrace(tmp_path)
    trace.start()
    with pytest.raises(host.TraceError, match="Access is denied"):
        trace.finish({100}, alive=lambda pid: False, sleep=lambda s: None)


def test_the_cleanup_of_a_launch_also_waits_for_its_session_to_leave_the_list(tmp_path, monkeypatch):
    fake = FakeTools(tmp_path, TREE, TREE_FILES, still_listed=2)
    monkeypatch.setattr(host, "_tool", fake)
    trace, clock = host.WindowsTrace(tmp_path), Clock()
    trace.start()
    trace.cleanup(sleep=clock.sleep, now=clock.now)
    assert clock.sleeps == [0.25, 0.25] and trace.name not in fake.sessions and trace.session is False


class Done:
    def __init__(self, code, text):
        self.returncode, self.stdout, self.stderr = code, "", text


WMI_TEXT = "Error:\nThe GUID passed was not recognized as valid by a WMI data provider."


def a_tool_that_fails_with(monkeypatch, code, text, *argv):
    monkeypatch.setattr(host.subprocess, "run", lambda *a, **k: Done(code, text))
    with pytest.raises(host.TraceError) as err:
        host._tool(list(argv))
    return err.value


def test_a_failed_windows_tool_says_its_command_its_code_in_hex_and_the_text_of_windows(monkeypatch):
    e = a_tool_that_fails_with(monkeypatch, 2147946600, WMI_TEXT, "logman", "query", "abhost-1-2", "-ets")
    assert "logman query abhost-1-2 -ets" in str(e) and "0x80071068" in str(e) and "The GUID passed was not recognized as valid by a WMI data provider." in str(e)
    assert e.code == 2147946600 and e.access_denied is False
    line = host.trace_not_started(e)
    assert line.startswith("the trace of a Windows run could not be started: logman query") and "needs an administrator" not in line


@pytest.mark.parametrize("code", [5, 0x80070005, -2147024891])
def test_access_denied_is_the_one_failure_that_says_the_trace_needs_an_administrator(monkeypatch, code):
    e = a_tool_that_fails_with(monkeypatch, code, "Access is denied.", "auditpol", "/set", "/subcategory:{G}")
    assert e.access_denied is True and "Access is denied." in str(e)
    assert "(it needs an administrator): auditpol /set" in host.trace_not_started(e)


def test_a_tool_that_could_not_run_has_no_code_and_does_not_blame_the_administrator(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("logman")

    monkeypatch.setattr(host.subprocess, "run", missing)
    with pytest.raises(host.TraceError) as err:
        host._tool(["logman", "create"])
    assert err.value.code is None and "needs an administrator" not in host.trace_not_started(err.value) and "logman create" in str(err.value)


def test_the_list_of_the_sessions_is_read_from_the_output_of_logman_query_ets():
    got = host.parse_sessions(REAL_SESSION_LIST)
    assert got["abhost-4242-123456789"] == "Running" and got["SgrmEtwSession"] == "Stopped" and got["Circular Kernel Context Logger"] == "Running"
    assert got["Microsoft-Windows-Rdp-Graphics-RdpIdd-Trace"] == "Running", "a name as wide as its column leaves one space before the type"
    assert len(got) == 9 and not any(k.startswith(("Data", "-", "The command")) for k in got)
    assert host.parse_sessions("") == {} and host.parse_sessions("The command completed successfully.\n") == {}


def test_a_read_of_the_list_that_fails_is_read_again_until_the_session_is_running(tmp_path, monkeypatch):
    fake = FakeTools(tmp_path, TREE, TREE_FILES, list_failures=2)  # run 45257629988: the read failed with 2147946600, the session was there
    monkeypatch.setattr(host, "_tool", fake)
    trace, clock = host.WindowsTrace(tmp_path), Clock()
    trace.start(sleep=clock.sleep, now=clock.now)
    assert clock.sleeps == [0.25, 0.25] and trace._start_canary
    assert sum(1 for c in fake.calls if c == ["logman", "query", "-ets"]) == 5, "two failed reads, then the three of the canaries"
    assert trace.finish({100}, alive=lambda pid: False, sleep=lambda s: None)["problems"] == []


def test_a_read_of_the_list_that_always_fails_ends_at_the_guard_with_its_last_error(tmp_path, monkeypatch):
    fake = FakeTools(tmp_path, TREE, TREE_FILES, list_failures=10 ** 9)
    monkeypatch.setattr(host, "_tool", fake)
    trace, clock = host.WindowsTrace(tmp_path), Clock()
    with pytest.raises(host.TraceError, match="could not be read in 3 s, the last error") as err:
        trace.start(sleep=clock.sleep, now=clock.now, limit=3.0)
    assert "0x80071068" in str(err.value) and "WMI data provider" in str(err.value) and err.value.code == 2147946600 and clock.t >= 3.0
    assert "needs an administrator" not in host.trace_not_started(err.value)
    assert not any(c[:1] == ["tracerpt"] for c in fake.calls) and trace.session is True, "the launch does not start; cleanup closes the session"


def test_a_read_of_the_list_that_fails_after_stop_is_read_again_until_the_session_is_gone(tmp_path, monkeypatch):
    fake = FakeTools(tmp_path, TREE, TREE_FILES)
    monkeypatch.setattr(host, "_tool", fake)
    trace, clock = host.WindowsTrace(tmp_path), Clock()
    trace.start()
    fake.list_failures = 2
    trace.cleanup(sleep=clock.sleep, now=clock.now)
    assert clock.sleeps == [0.25, 0.25] and trace.name not in fake.sessions and trace.session is False
    trace2 = host.WindowsTrace(tmp_path)
    trace2.start()
    fake.list_failures = 10 ** 9
    with pytest.raises(host.TraceError, match="could not be read in 1 s after `logman stop`, the last error: logman query -ets failed with 0x80071068"):
        trace2._stop_session(clock.sleep, clock.now, 1.0)

