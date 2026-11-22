"""The host-footprint oracle's comparator and readers (scripts/ci_ab_host.py), on synthetic traces, no browser.

The oracle compares the WHOLE TRACE of a closed run, from the launch to the exit of the browser: the processes by kind (with their count, but for the kinds of type
`tab`, whose count follows the idle time and only whose presence counts) and the set of host paths the tree touched (what is `ours` -- its package, its profile and temp
dirs, the run's cache, its own shared memory -- leaves the comparison as a class). Nothing is excused: there is no list of noise.
Each property is shown on the input it exists for: a host path on one side only is red (naming it), a path of ours on one side only is green, a kind with another count
is red, a tab count that differs is green and a tab kind that is absent on one side is red, the one declared difference passes in an A/B by kind, and the Windows
processes are named by their command lines from the audit events.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ci_ab_host as host  # noqa: E402

pytestmark = pytest.mark.unit

SMALL = {"proc": 2, "file": 3}
SEEDS = (11, 12)
KINDS = {"parent": 1, "tab/isForBrowser": 2, "gpu": 1, "glxtest": 1}
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


def a_candidate_without_glxtest():
    return footprint("candidate", kinds={k: n for k, n in KINDS.items() if k != "glxtest"})


def judge(a, b, **kw):
    kw.setdefault("floors", SMALL)
    return host.judge(a, b, **kw)


def expected():
    return host.load_expected()


# -
# The comparator.

def test_equal_footprints_are_green():
    report = judge(footprint(), footprint(), expected=[], a_a=True)
    assert report["verdict"] == "green" and report["problems"] == [] and report["differences"] == []
    assert set(report["leaves"]) == {"11", "12"}
    assert report["leaves"]["11"] == {"proc": 4, "file": 3}


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

def test_the_declared_difference_is_the_glxtest_of_linux_by_kind_and_there_is_one_only():
    entries = expected()
    assert [(e["id"], e["os"], e["category"], e["name"]) for e in entries] == [("glxtest-69", "linux", "proc", "glxtest")]
    assert "0e896e4ee1" in entries[0]["reason"] and "glxtest never reads the host" in entries[0]["reason"]
    assert len(entries) <= 1, "the list of declared differences may only shrink"


def test_the_declared_glxtest_difference_passes_in_an_a_b():
    report = judge(footprint(), a_candidate_without_glxtest(), expected=expected())
    assert report["verdict"] == "green", report["problems"]
    assert [d["leaf"] for d in report["declared"]] == ["proc.glxtest", "proc.glxtest"] and report["stale"] == []


def test_a_difference_next_to_the_declared_one_is_still_red():
    c = a_candidate_without_glxtest()
    c["seeds"][0]["kinds"]["utility/sandboxingKind=1"] = 1
    report = judge(footprint(), c, expected=expected())
    assert report["verdict"] == "red"
    assert [d["leaf"] for d in report["differences"]] == ["proc.utility/sandboxingKind=1"]


def test_the_declared_difference_the_other_way_round_is_red():
    report = judge(a_candidate_without_glxtest(), footprint(), expected=expected())
    assert report["verdict"] == "red" and any(d["leaf"] == "proc.glxtest" for d in report["differences"])


def test_a_declared_difference_that_does_not_show_is_red_as_stale():
    report = judge(footprint(), footprint("candidate"), expected=expected())
    assert report["verdict"] == "red" and report["stale"] == ["glxtest-69"]
    assert any("stale" in p and "glxtest-69" in p for p in report["problems"])


def test_a_declared_difference_of_another_os_is_not_applied_and_not_stale_there():
    report = judge(footprint(platform="win32"), footprint("candidate", platform="win32"), expected=expected())
    assert report["verdict"] == "green", report["problems"]


def test_in_the_a_a_the_declared_list_is_not_applied():
    report = judge(footprint(), a_candidate_without_glxtest(), expected=expected(), a_a=True)
    assert report["verdict"] == "red" and report["declared"] == [] and report["stale"] == []
    assert [d["leaf"] for d in report["differences"]] == ["proc.glxtest", "proc.glxtest"]


def test_the_declared_glxtest_does_not_cover_a_path():
    c = a_candidate_without_glxtest()
    c["seeds"][0]["files"] = FILES + ["/usr/lib/x86_64-linux-gnu/libglx.so.0"]
    report = judge(footprint(), c, expected=expected())
    assert report["verdict"] == "red" and [d["leaf"] for d in report["differences"]] == ["file./usr/lib/x86_64-linux-gnu/libglx.so.0"]


def test_the_cli_says_broken_control_for_an_a_a_difference(tmp_path, capsys):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    for path, doc in ((a, footprint()), (b, a_candidate_without_glxtest())):
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
    assert host.leaves_of(fp)["proc.tab/isForBrowser"] == 1 and "file./etc/localtime" in host.leaves_of(fp)


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
    exe = "C:\\Lab\\labrunner\\AppData\\Local\\invisible-playwright\\Cache\\firefox.exe"
    cmd = exe + " --backgroundtask pingsender https://incoming.telemetry.mozilla.org/submit/telemetry/244041fe/first-shutdown/Firefox/159.0/release/2026?v=4 C:\\x\\ping"
    events = host.parse_audit_events(audit(1, 200, 100, exe, cmd))
    assert host.windows_processes(events, exe)[200]["label"] == "backgroundtask/pingsender"
    assert host.label_of_exec("/e/firefox/firefox", ["/e/firefox/firefox", "--backgroundtask", "uninstall"], REAL) == "backgroundtask/uninstall"
    a = footprint(platform="win32", kinds=dict(KINDS, **{"backgroundtask/pingsender": 3}))
    report = judge(a, footprint("again", platform="win32"), a_a=True)
    assert report["verdict"] == "red" and {d["leaf"] for d in report["differences"]} == {"proc.backgroundtask/pingsender"}


def test_the_session_is_recording_when_the_last_start_canary_is_in_the_trace_even_if_the_first_ones_are_not(tmp_path, monkeypatch):
    trace, fake = a_windows_run(tmp_path, monkeypatch, TREE_FILES, canaries=("start3", "end"))
    got = trace.finish({100}, alive=lambda pid: False, sleep=lambda s: None)
    assert got["problems"] == []
    assert sum(1 for c in fake.calls if c[:2] == ["logman", "query"]) == 3, "a round trip to the session before each canary"
    trace, _fake = a_windows_run(tmp_path, monkeypatch, TREE_FILES, canaries=("start1", "start2", "end"))
    problems = trace.finish({100}, alive=lambda pid: False, sleep=lambda s: None)["problems"]
    assert len(problems) == 1 and "last start canary" in problems[0]
