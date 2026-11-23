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


def test_a_kind_with_another_count_is_red_and_names_the_kind_and_both_sides():
    b = footprint(kinds=dict(KINDS, gpu=2))
    report = judge(footprint(), b, a_a=True)
    assert report["verdict"] == "red"
    assert [(d["seed"], d["leaf"], d["a"], d["b"]) for d in report["differences"]] == [(11, "proc.gpu", 1, 2), (12, "proc.gpu", 1, 2)]
    assert any("seed 12 proc.gpu: A 1 | B 2" in p for p in report["problems"])


def test_a_kind_that_exists_on_one_side_only_is_red():
    b = footprint(kinds=dict(KINDS, rdd=1))
    report = judge(footprint(), b, a_a=True)
    assert sorted({(d["leaf"], d["a"], d["b"]) for d in report["differences"]}) == [("proc.rdd", host.MISSING, 1)]


def test_a_helper_that_is_spawned_one_more_time_is_red():
    report = judge(footprint(), footprint(kinds=dict(KINDS, **{"pingsender.exe": 1})), a_a=True)
    assert {d["leaf"] for d in report["differences"]} == {"proc.pingsender.exe"}


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
    c["seeds"][0]["kinds"]["utility/sandboxingKind=1"] = 1
    report = judge(footprint(), c, expected=expected())
    assert report["verdict"] == "red"
    assert [d["leaf"] for d in report["differences"]] == ["proc.utility/sandboxingKind=1"]


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
    assert host.leaves_of(fp)["proc.tab/isForBrowser"] == 1 and not any(leaf.startswith("file.") for leaf in host.leaves_of(fp)), "the host paths are judged over all the launches"


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


class FakeTools:
    """`_tool` of a Windows machine: the audit, the registry, the Security log, logman and tracerpt, with a record of what was asked."""

    def __init__(self, work, tree, files, canaries=("start", "end")):
        self.work, self.tree, self.files, self.canaries, self.calls = work, tree, files, canaries, []

    def __call__(self, argv, timeout=300.0):
        self.calls.append(list(argv))
        tool = argv[0]
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
    assert sum(1 for c in fake.calls if c[:2] == ["logman", "query"]) == 3, "a round trip to the session before each canary"
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

    def fake_run_seed(binary, seed, work, name=None, idle=False, bare=False):
        calls.append((seed, name, idle))
        return seed_footprint(seed)

    monkeypatch.setattr(host, "run_seed", fake_run_seed)
    monkeypatch.setattr(host.os.path, "realpath", lambda p: p)
    doc = host.run_engine("ff", "linux-x86_64", [11, 12], "firefox-68")
    assert calls == [(11, "warmup", False), (11, None, False), (12, None, False), (11, "idle", True), (11, "bare", False)]
    assert doc["warmup"]["seed"] == 11 and [s["seed"] for s in doc["seeds"]] == [11, 12] and doc["idle"]["seed"] == 11 and doc["bare"]["seed"] == 11 and doc["problems"] == []


def test_a_warm_up_that_failed_is_red_in_the_document_like_a_measured_launch(monkeypatch):
    def fake_run_seed(binary, seed, work, name=None, idle=False, bare=False):
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


def test_the_pingsenders_of_a_short_launch_are_none_or_the_three_of_a_first_run_and_nothing_else():
    pings = with_pingsenders(footprint(), 3)
    assert judge(pings, footprint("again"), a_a=True)["verdict"] == "green", "3 on one side and 0 on the other is the approved tolerance"
    assert judge(with_pingsenders(footprint(), 3), with_pingsenders(footprint("again"), 3), a_a=True)["verdict"] == "green"
    for n in (1, 2, 4):
        report = judge(with_pingsenders(footprint(), n), footprint("again"), a_a=True)
        assert report["verdict"] == "red" and any("%d pingsender processes, not 0 or 3" % n in p and "TelemetrySend.sys.mjs:1083" in p for p in report["problems"]), n


def test_three_pingsenders_that_are_not_the_pings_of_a_first_run_or_not_spawned_by_the_parent_are_red():
    wrong = with_pingsenders(footprint(), 3, types=("event", "event", "new-profile"))
    assert any("not the pings of a first run" in p for p in judge(wrong, footprint("again"), a_a=True)["problems"])
    orphan = with_pingsenders(footprint(), 3, parent="gpu")
    assert any("not all spawned by the parent" in p for p in judge(orphan, footprint("again"), a_a=True)["problems"])


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
    assert {d["leaf"] for d in one_side["differences"]} == {"file." + imagehlp}, "its paths are paths like any other: in every launch of one side and in none of the other"
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
# The geolocation step: the candidate starts no Windows location utility process and loads no location module; stock Firefox, the control, must start it.

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


def test_the_rule_is_for_the_candidate_on_windows_not_for_the_a_a_nor_for_linux():
    published, candidate = windows_footprints(candidate_kinds=dict(WIN_KINDS, **{LOCATION_KIND: 1}))
    a_a = judge(candidate, copy.deepcopy(candidate), a_a=True)
    assert not any("location" in p for p in a_a["problems"])
    side_a = judge(candidate, published, expected=[])
    assert not any("side B: the engine started" in p for p in side_a["problems"])
    on_linux = judge(footprint(), footprint("candidate", kinds=dict(KINDS, **{LOCATION_KIND: 1})), expected=[])
    assert not any("location" in p for p in on_linux["problems"])


def a_stock(kinds, files=(), steps=None, problems=()):
    return {"format": host.FORMAT, "engine": "stock", "platform": "win32",
            "stock": {"seed": 0, "steps": steps if steps is not None else [{"n": 5, "name": host.GEO_STEP, "ok": True}], "kinds": kinds, "files": list(files),
                      "problems": list(problems)}}


def test_stock_that_starts_the_location_utility_process_is_a_working_control():
    stock = a_stock(dict(WIN_KINDS, **{LOCATION_KIND: 1}), files=["c:\\windows\\system32\\geolocation.dll"])
    assert host.stock_control_problems(stock) == []


def test_stock_that_does_not_start_it_is_a_broken_control_and_says_the_step_does_not_reach_the_provider():
    problems = host.stock_control_problems(a_stock(WIN_KINDS))
    assert len(problems) == 1 and "did not start the Windows location utility process (-sandboxingKind 3)" in problems[0]
    assert "does not reach the platform provider" in problems[0]


def test_a_stock_run_that_failed_or_was_not_traced_is_a_broken_control_too():
    kinds = dict(WIN_KINDS, **{LOCATION_KIND: 1})
    assert host.stock_control_problems(a_stock(kinds, steps=[{"n": 5, "name": host.GEO_STEP, "ok": False, "error": "timeout"}])) == [
        "stock control: step 5 (%s) failed: timeout" % host.GEO_STEP]
    assert host.stock_control_problems(a_stock(kinds, problems=["the file trace lost 3 events"])) == ["stock control: the file trace lost 3 events"]
    assert host.stock_control_problems({"stock": {}}) == ["stock control: the run made no footprint"]


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
    assert run_seed.index("geolocation_step(") < run_seed.rindex("browser.close()")


def test_the_lab_page_of_the_step_is_served_by_the_oracle_itself():
    import urllib.request

    origin, stop = host.start_geo_lab()
    try:
        assert origin.startswith("http://127.0.0.1:")
        with urllib.request.urlopen(origin + host.GEO_PATH, timeout=60) as response:
            assert response.status == 200 and b"<title>geo</title>" in response.read()
    finally:
        stop()


def test_a_windows_ab_runs_the_stock_control_after_the_candidate_and_before_the_verdict_and_a_broken_one_is_exit_2(tmp_path, monkeypatch):
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
    stock["doc"] = a_stock(dict(WIN_KINDS, **{LOCATION_KIND: 1}))
    assert host.ab(cand, Path("seal"), "windows-x86_64", tmp_path / "o2", "firefox-68", [11]) == 0
    assert order[-2:] == [("stock", "windows-x86_64"), ("judge", "a-b")]
    order.clear()
    assert host.ab(None, None, "windows-x86_64", tmp_path / "o3", "firefox-68", [11]) == 0
    assert ("stock", "windows-x86_64") not in order, "the A/A has no candidate: no control of the geolocation step"


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
    # the pingsenders (and so what they spawn) on one side only: left out; the parent's own socket is compared
    a = with_pingsenders(footprint(kinds=kinds), 3)
    assert judge(a, footprint("again", kinds={k: n for k, n in kinds.items() if "pingsender" not in k}), a_a=True)["verdict"] == "green"
    # on both sides the spawned processes are compared
    b = with_pingsenders(footprint("again", kinds=dict(kinds, **{"socket<-pingsender": 3})), 3)
    report = judge(a, b, a_a=True)
    assert report["verdict"] == "red" and {(d["leaf"], d["a"], d["b"]) for d in report["differences"]} == {("proc.socket<-pingsender", 2, 3)}
    lost = footprint("again", kinds={k: n for k, n in kinds.items() if k != "socket"})
    assert {d["leaf"] for d in judge(with_pingsenders(footprint(kinds=kinds), 0), lost, a_a=True)["differences"]} == {"proc.socket"}, "the parent's own socket still counts"


def test_a_creator_pid_that_windows_reused_before_the_launch_is_not_in_its_tree():
    events = host.parse_audit_events("\n".join([
        audit(5, 200, 100, FIREFOX, '"%s" --backgroundtask pingsender https://t.example/submit/telemetry/1/event/x' % FIREFOX),
        audit(10, 100, 1, FIREFOX, '"%s" -profile "D:\\t\\p"' % FIREFOX),
        audit(12, 102, 100, FIREFOX, '"%s" -contentproc tab' % FIREFOX)]))
    assert [e["pid"] for e in host.process_tree(events, {100})] == [100, 102], "record 5 came before the root (record 10): its creator is an older process that had the same pid"


# -
# The bare launch (no client, no page; never compared) and the evidence of how a browser ended and what a crash leaves; the creates of the host.

def test_the_crash_files_are_read_when_they_appear_a_dmp_by_its_name_and_an_extra_by_its_content(tmp_path):
    folder = tmp_path / "minidumps"
    folder.mkdir()
    (folder / "a1b2.dmp").write_bytes(b"MDMP" + b"0" * 100)
    (folder / "a1b2.extra").write_text('{"MozCrashReason": "MOZ_CRASH(shutdown hang)", "ShutdownProgress": "profile-change-teardown", "CrashType": "SIGSEGV"}', encoding="utf-8")
    (folder / "notes.txt").write_text("not a crash file", encoding="utf-8")
    seen = {}
    host.scan_crash_files([folder, tmp_path / "missing"], seen)
    files = sorted(seen.values(), key=lambda f: f["name"])
    assert [f["name"] for f in files] == ["a1b2.dmp", "a1b2.extra"] and files[0]["size"] == 104 and "content" not in files[0]
    assert "MozCrashReason" in files[1]["content"] and "ShutdownProgress" in files[1]["content"]
    host.scan_crash_files([folder], seen)
    assert "MozCrashReason" in seen[str(folder / "a1b2.extra")]["content"], "a file that did not change is not read again"
    (folder / "a1b2.extra").write_text('{"MozCrashReason": "later", "more": "a file seen half written is read again when its size changes"}', encoding="utf-8")
    host.scan_crash_files([folder], seen)
    assert "later" in seen[str(folder / "a1b2.extra")]["content"]


def test_a_crash_file_seen_by_its_name_before_its_content_is_written_is_read_again_when_it_is_complete(tmp_path):
    """The losing interleaving, built on purpose: the collector looks when the file is there and empty, then half written, then complete. A collector that reads a file once
    keeps the empty text (the profile is deleted when the launch ends, so there is no second chance)."""
    folder = tmp_path / "profile" / "minidumps"
    folder.mkdir(parents=True)
    extra = folder / "x.extra"
    seen = {}
    extra.write_bytes(b"")
    host.scan_crash_files([folder], seen)
    assert seen[str(extra)]["content"] == "" and seen[str(extra)]["size"] == 0
    extra.write_bytes(b'{"CrashType": "SIG')
    host.scan_crash_files([folder], seen)
    assert seen[str(extra)]["content"] == '{"CrashType": "SIG'
    extra.write_bytes(b'{"CrashType": "SIGSEGV"}')
    host.scan_crash_files([folder], seen)
    assert seen[str(extra)]["content"] == '{"CrashType": "SIGSEGV"}' and seen[str(extra)]["size"] == len(b'{"CrashType": "SIGSEGV"}')
    host.scan_crash_files([folder], seen)
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


def test_the_crash_evidence_says_how_the_parent_ended_who_the_crash_reporters_are_and_what_files_the_crash_left():
    fp = {"lifecycle": [{"event": "start", "kind": "parent", "line": 0, "how": ""}, {"event": "end", "kind": "crashreporter", "line": 5, "how": "exited 0"},
                        {"event": "end", "kind": "parent", "line": 9, "how": "killed by SIGSEGV"}],
          "processes": [{"label": "parent", "exec": "firefox -profile /tmp/p"}, {"label": "crashreporter", "exec": "crashreporter --ping-cleanup /srv/lab/r/.config/mozilla/firefox/Crash Reports /tmp/p"},
                        {"label": "minidump-analyzer", "exec": "minidump-analyzer /tmp/p/minidumps/x.dmp"}], "close": {"root_returncode": -11}}
    got = host.crash_evidence({"files": [{"name": "x.dmp", "size": 5}], "scans": 7}, fp, {"core_pattern": "core", "cores": ["core.123"], "backtraces": {"core.123": "#0 0x0 in ?? ()"}})
    assert got["parent_exits"] == ["killed by SIGSEGV"] and got["root_returncode"] == -11
    assert [p["kind"] for p in got["crash_processes"]] == ["crashreporter", "minidump-analyzer"] and "--ping-cleanup" in got["crash_processes"][0]["argv"]
    assert got["files"] == [{"name": "x.dmp", "size": 5}] and got["core"]["cores"] == ["core.123"]
    summary = host._bare_summary({"notified": {"observed": False}, "crash": got})
    assert summary["parent_exits"] == ["killed by SIGSEGV"] and summary["dump_files"] == ["x.dmp"] and summary["cores"] == ["core.123"]
    assert host.crash_evidence({}, {"processes": []})["parent_exits"] is None, "no lifecycle: it is not said that the parent ended well"


def test_a_linux_core_is_found_where_the_launch_ran_and_its_backtrace_is_asked_of_gdb(tmp_path, monkeypatch):
    (tmp_path / "core.4242").write_bytes(b"\x7fELF")
    (tmp_path / "other.txt").write_text("x", encoding="utf-8")

    class Done:
        stdout, stderr = "#0  0x00007f in mozilla::ShutdownXPCOM ()\n", ""

    calls = []
    monkeypatch.setattr(host.shutil, "which", lambda name: "/usr/bin/gdb")
    got = host.core_info(tmp_path, "/e/firefox/firefox", run=lambda argv, **kw: calls.append(argv) or Done())
    assert got["cores"] == ["core.4242"] and "ShutdownXPCOM" in got["backtraces"]["core.4242"]
    assert calls == [["/usr/bin/gdb", "-batch", "-ex", "bt", "/e/firefox/firefox", str(tmp_path / "core.4242")]]
    monkeypatch.setattr(host.shutil, "which", lambda name: None)
    assert host.core_info(tmp_path, "/e/firefox/firefox")["backtraces"] == {"core.4242": "gdb is not installed"}
    assert host.core_info(tmp_path / "empty-nowhere", "x")["cores"] == []


def test_the_stand_in_allows_core_dumps_in_every_launch_and_only_the_bare_one_moves_to_its_own_folder(tmp_path, monkeypatch):
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
    cores = Path(stand_in._stand_in(str(real), tmp_path / "t2", tmp_path / "l2", tmp_path / "p2", core_dir=tmp_path / "cores"))
    text = cores.read_text(encoding="utf-8")
    assert "ulimit -c unlimited\ncd %s\nexec " % (tmp_path / "cores") in text


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


def test_the_report_carries_the_bare_launch_without_comparing_it(capsys):
    a = footprint()
    a["bare"] = {"seed": 11, "kinds": {"parent": 1, "crashreporter": 1}, "bare": {"notified": {"observed": False}, "crash": {
        "parent_exits": ["exited 0"], "root_returncode": 0, "crash_processes": [{"kind": "crashreporter", "argv": "crashreporter --ping-cleanup /x /y"}], "files": [], "core": None}}}
    b = footprint("again")
    b["bare"] = {"seed": 11, "kinds": {"parent": 1}, "bare": {"notified": {"observed": False}, "crash": {"parent_exits": ["exited 0"], "root_returncode": 0, "crash_processes": [], "files": []}}}
    report = judge(a, b, a_a=True)
    assert report["verdict"] == "green", "the bare launch is evidence, not a leaf"
    assert report["bare"]["A"]["crash_processes"] == [("crashreporter", "crashreporter --ping-cleanup /x /y")] and report["bare"]["B"]["crash_processes"] == []
    host.print_report(report, "firefox-68", "firefox-68 again")
    assert "BARE (no client, no page; not compared) firefox-68:" in capsys.readouterr().out


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
    for key in ("warmup", "idle", "bare"):
        if key in per_launch and not fp.get(key):
            fp[key] = seed_footprint(11)
    launches = {s["seed"]: s for s in fp["seeds"]}
    launches.update({k: fp[k] for k in ("warmup", "idle", "bare") if fp.get(k)})
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
    problems, left = host.end_of_launch(-11, False, 11, "measured", [], "/e/firefox", log, watched, None, 0.0, folders=[], run=Run({}))
    assert problems == ["the parent process exited with code -11 (SIGSEGV): seed 11, launch measured"]
    assert left["exit"] == {"code": -11, "text": "-11 (SIGSEGV)"} and left["strace"]["signals"][0][1].startswith("100 --- SIGSEGV")
    assert left["crash_files"]["extra"][0]["name"] == "u.extra"
    assert host.end_of_launch(0, False, 11, "measured", [], "/e/firefox", log, watched, None, 0.0, folders=[]) == ([], None)
    problems, left = host.end_of_launch(None, False, 11, "idle", [], "/e/firefox", log, {}, None, 0.0, folders=[], run=Run({}))
    assert "not seen" in problems[0] and left is not None
    windows, kept = host.end_of_launch(3221225477, True, 11, "bare", [], "D:\\e\\firefox.exe", None, {"files": []}, None, 0.0, run=Run({}))
    assert "0xC0000005 (access violation)" in windows[0] and "launch bare" in windows[0] and kept["strace"] is None


def test_the_bare_launch_looks_in_its_own_folder_for_a_core_as_well(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(host.crash, "collect", lambda code, binary, **kw: seen.update(kw) or {"exit": {}, "said": []})
    monkeypatch.setattr(host.crash, "core_dirs", lambda *a, **k: [Path("/job/cores")])
    host.end_of_launch(-11, False, 1, "bare", [], "b", tmp_path / "l", {}, tmp_path, 5.0)
    assert seen["core_folders"] == [Path("/job/cores"), tmp_path] and seen["since"] == 5.0


def test_a_launch_that_did_not_end_with_0_is_in_the_verdict_of_the_run_named_by_side_seed_and_launch_whatever_launch_it_was(monkeypatch, tmp_path):
    bad = crash.exit_problem(-11, False, seed=12, launch="measured")

    def fake_run_seed(binary, seed, work, name=None, idle=False, bare=False):
        fp = seed_footprint(seed)
        if name is None and seed == 12:
            fp["problems"] = [bad]
            fp["crash"] = {"exit": {"code": -11, "text": "-11 (SIGSEGV)"}, "said": ["no core file written"], "strace": None, "cores": None, "crash_files": {"dmp": [], "extra": []}}
        if name == "bare":
            fp["exit_problems"] = [crash.exit_problem(-6, False, seed=11, launch="bare")]
        if name == "idle":
            fp["problems"] = [crash.exit_problem(1, False, seed=11, launch="idle")]
        return fp

    monkeypatch.setattr(host, "run_seed", fake_run_seed)
    monkeypatch.setattr(host.os.path, "realpath", lambda p: p)
    doc = host.run_engine("ff", "linux-x86_64", [11, 12], "firefox-68", tmp_path / "x.crash")
    assert "idle: the parent process exited with code 1: seed 11, launch idle" in doc["problems"]
    assert "bare: the parent process exited with code -6 (SIGABRT): seed 11, launch bare" in doc["problems"]
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


def test_the_entry_covers_only_the_seeds_where_the_process_is_gone_and_the_path_of_another_seed_is_red():
    a = with_files(footprint(), GFX_PCI, GFX_ONLY)
    c = footprint("candidate", kinds={k: n for k, n in KINDS.items() if k != "gfxtest"})
    c["seeds"][1]["kinds"] = dict(KINDS)
    report = judge(a, c, expected=expected())
    assert report["verdict"] == "red" and {(d["seed"], d["leaf"]) for d in report["differences"]} == {(SEEDS[1], "file./sys/bus/pci"), (SEEDS[1], "file./sys/bus/pci/")}
    assert {d["seed"] for d in report["declared"]} == {SEEDS[0]}


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
