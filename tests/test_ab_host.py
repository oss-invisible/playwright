"""The host-footprint oracle's comparator and readers (scripts/ci_ab_host.py), on synthetic footprints and logs, no browser.

The oracle asks one question of two engines after the same journey: the same process kinds, the same mapped modules per kind, the same short-lived children.
Each property is shown on the input it exists for: one kind or one module different is red (naming it with both sides), equal footprints are green, a
footprint below the floor is red, the one declared difference passes in an A/B, a declared difference that does not show is red as stale, and in the A/A
no declared difference is applied.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ci_ab_host as host  # noqa: E402

pytestmark = pytest.mark.unit

SMALL = {"proc": 2, "mod": 3}
SEEDS = (11, 12)


def seed_footprint(seed, spawned=None):
    return {
        "seed": seed, "steps": [{"n": 1, "name": "open home", "ok": True}],
        "kinds": {"parent": 1, "tab/isForBrowser": 2, "gpu": 1},
        "modules": {"parent": ["<app>/libxul.so", "<memfd:mozilla-ipc>", "/usr/lib/x86_64-linux-gnu/libc.so.6"],
                    "tab/isForBrowser": ["<app>/libxul.so", "/usr/lib/x86_64-linux-gnu/libc.so.6"]},
        "unreadable": [], "spawned": spawned if spawned is not None else {"parent": 1, "tab/isForBrowser": 2, "gpu": 1, "glxtest": 1},
    }


def footprint(engine="firefox-68", platform="linux", spawned=None):
    return {"format": host.FORMAT, "engine": engine, "platform": platform, "problems": [],
            "seeds": [seed_footprint(s, copy.deepcopy(spawned)) for s in SEEDS]}


def a_candidate_without_glxtest():
    return footprint("candidate", spawned={"parent": 1, "tab/isForBrowser": 2, "gpu": 1})


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
    assert report["leaves"]["11"]["proc"] == 3 and report["leaves"]["11"]["mod"] == 5 and report["leaves"]["11"]["spawn"] == 4


def test_one_kind_more_is_red_and_names_the_kind_and_both_sides():
    b = footprint()
    b["seeds"][1]["kinds"]["tab/isForBrowser"] = 3
    report = judge(footprint(), b, a_a=True)
    assert report["verdict"] == "red"
    assert [(d["seed"], d["leaf"], d["a"], d["b"]) for d in report["differences"]] == [(12, "proc.tab/isForBrowser", 2, 3)]
    assert any("seed 12 proc.tab/isForBrowser: A 2 | B 3" in p for p in report["problems"])


def test_a_kind_that_exists_on_one_side_only_is_red():
    b = footprint()
    b["seeds"][0]["kinds"]["rdd"] = 1
    report = judge(footprint(), b, a_a=True)
    assert [(d["leaf"], d["a"], d["b"]) for d in report["differences"]] == [("proc.rdd", host.MISSING, 1)]


def test_one_module_different_is_red_and_names_the_kind_the_module_and_both_sides():
    b = footprint()
    b["seeds"][0]["modules"]["parent"].append("/usr/lib/x86_64-linux-gnu/libEGL.so.1")
    report = judge(footprint(), b, a_a=True)
    assert report["verdict"] == "red"
    assert [(d["seed"], d["leaf"], d["a"], d["b"]) for d in report["differences"]] == [
        (11, "mod.parent:/usr/lib/x86_64-linux-gnu/libEGL.so.1", host.MISSING, 1)]


def test_a_module_mapped_by_another_kind_is_not_the_same_module_set():
    b = footprint()
    b["seeds"][0]["modules"]["tab/isForBrowser"].remove("<app>/libxul.so")
    b["seeds"][0]["modules"]["gpu"] = ["<app>/libxul.so"]
    report = judge(footprint(), b, a_a=True)
    assert sorted(d["leaf"] for d in report["differences"]) == ["mod.gpu:<app>/libxul.so", "mod.tab/isForBrowser:<app>/libxul.so"]


def test_a_footprint_below_the_floor_is_red():
    thin = footprint()
    thin["seeds"][0]["modules"] = {"parent": ["<app>/libxul.so"]}
    report = host.judge(thin, thin, a_a=True, floors={"proc": 2, "mod": 3})
    assert report["verdict"] == "red" and report["differences"] == []
    assert any("seed 11 side A has 1 mod leaves, fewer than the floor of 3" in p for p in report["problems"])


def test_the_real_floors_are_not_met_by_a_toy_footprint():
    assert host.judge(footprint(), footprint(), a_a=True)["verdict"] == "red"


def test_a_missing_seed_is_red():
    b = footprint()
    b["seeds"].pop()
    report = judge(footprint(), b, a_a=True)
    assert report["verdict"] == "red" and any("different seeds" in p for p in report["problems"])


def test_a_module_that_could_not_be_read_is_red_even_when_both_sides_say_it():
    a = footprint()
    a["seeds"][0]["unreadable"] = ["tab/isForBrowser (pid 4): AccessDenied: "]
    report = judge(a, copy.deepcopy(a), a_a=True)
    assert report["verdict"] == "red" and any("cannot be read" in p for p in report["problems"])


def test_a_journey_step_that_failed_is_red():
    a = footprint()
    a["problems"] = ["seed 11: step 3 (scroll) failed: timeout"]
    report = judge(a, footprint(), a_a=True)
    assert report["verdict"] == "red" and "side A: seed 11: step 3" in report["problems"][0]


# -
# The declared differences.

def test_the_declared_difference_is_the_glxtest_of_linux_and_there_is_one_only():
    entries = expected()
    assert [(e["id"], e["os"], e["category"], e["name"]) for e in entries] == [("glxtest-69", "linux", "spawn", "glxtest")]
    assert "0e896e4ee1" in entries[0]["reason"] and "glxtest never reads the host" in entries[0]["reason"]
    assert len(entries) <= 1, "the list of declared differences may only shrink"


def test_the_declared_glxtest_difference_passes_in_an_a_b():
    report = judge(footprint(), a_candidate_without_glxtest(), expected=expected())
    assert report["verdict"] == "green", report["problems"]
    assert [d["leaf"] for d in report["declared"]] == ["spawn.glxtest", "spawn.glxtest"] and report["stale"] == []


def test_a_difference_next_to_the_declared_one_is_still_red():
    c = a_candidate_without_glxtest()
    c["seeds"][0]["spawned"]["utility/sandboxingKind=1"] = 1
    report = judge(footprint(), c, expected=expected())
    assert report["verdict"] == "red"
    assert [d["leaf"] for d in report["differences"]] == ["spawn.utility/sandboxingKind=1"]


def test_the_declared_difference_the_other_way_round_is_red():
    report = judge(a_candidate_without_glxtest(), footprint(), expected=expected())
    assert report["verdict"] == "red" and any(d["leaf"] == "spawn.glxtest" for d in report["differences"])


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
    assert [d["leaf"] for d in report["differences"]] == ["spawn.glxtest", "spawn.glxtest"]


def test_the_cli_says_broken_control_for_an_a_a_difference(tmp_path, capsys):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    for path, doc in ((a, footprint()), (b, a_candidate_without_glxtest())):
        path.write_text(json.dumps(doc), encoding="utf-8")
    assert host.main(["compare", str(a), str(b), "--a-a"]) == 2
    assert "AB HOST (a-a) FAILED" in capsys.readouterr().out


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
# The normalizer.

WIN = host.Context("win32", app=["C:\\Users\\runneradmin\\AppData\\Local\\invisible\\engines\\firefox-68\\firefox"],
                   drops=["C:\\Users\\runneradmin\\AppData\\Local\\Temp"], sysroot="C:\\Windows")
LIN = host.Context("linux", app=["/home/runner/.cache/invisible/engines/firefox-68/firefox"], drops=["/home/runner", "/tmp"])


@pytest.mark.parametrize("path,out", [
    ("C:\\Users\\runneradmin\\AppData\\Local\\invisible\\engines\\firefox-68\\firefox\\xul.dll", "<app>/xul.dll"),
    ("c:\\users\\runneradmin\\appdata\\local\\invisible\\engines\\firefox-68\\firefox\\gmp-clearkey\\0.1\\clearkey.dll",
     "<app>/gmp-clearkey/0.1/clearkey.dll"),
    ("C:\\Windows\\System32\\KERNEL32.DLL", "<sys>/kernel32.dll"),
    ("C:\\WINDOWS\\system32\\ntdll.dll", "<sys>/ntdll.dll"),
    ("C:\\Windows\\SysWOW64\\wow64.dll", "<syswow>/wow64.dll"),
    ("C:\\Windows\\WinSxS\\amd64_microsoft.windows.common-controls_6595b64144ccf1df_6.0.26100.1_none_3e0e6d3c1d0f1b2c\\comctl32.dll",
     "<winsxs>/comctl32.dll"),
    ("C:\\Windows\\Fonts\\arial.ttf", "<win>/fonts/arial.ttf"),
    ("C:\\Windows\\Globalization\\Sorting\\sortdefault.nls", "<win>/globalization/sorting/sortdefault.nls"),
    ("C:\\Users\\runneradmin\\AppData\\Local\\Temp\\tmpab12cd\\profile\\cache2\\x", "<profile>"),
    ("C:\\Users\\runneradmin\\AppData\\Roaming\\Mozilla\\x.dat", "<profile>"),
    ("\\Device\\HarddiskVolume3\\Windows\\System32\\bcrypt.dll", "<sys>/bcrypt.dll"),
    ("C:\\Program Files\\Some Vendor\\hook.dll", "c:\\program files\\some vendor\\hook.dll"),
])
def test_a_windows_path_is_spelled_the_same_on_every_machine(path, out):
    assert host.normalize_module(path, WIN) == out


@pytest.mark.parametrize("path,out", [
    ("/home/runner/.cache/invisible/engines/firefox-68/firefox/libxul.so", "<app>/libxul.so"),
    ("/home/runner/.cache/invisible/engines/firefox-68/firefox/gmp-clearkey/0.1/libclearkey.so", "<app>/gmp-clearkey/0.1/libclearkey.so"),
    ("/usr/lib/x86_64-linux-gnu/libc.so.6", "/usr/lib/x86_64-linux-gnu/libc.so.6"),
    ("/memfd:mozilla-ipc (deleted)", "<memfd:mozilla-ipc>"),
    ("/memfd:mozilla-ipc-4321 (deleted)", "<memfd:mozilla-ipc-N>"),
    ("/dev/shm/org.mozilla.ipc.1234.5 (deleted)", "<shm:org.mozilla.ipc.N.N>"),
    ("/dev/shm/org.mozilla.ipc.77.8", "<shm:org.mozilla.ipc.N.N>"),
    ("/tmp/tmpabc123/profile/startupCache/scriptCache.bin", "<profile>"),
    ("/home/runner/work/_temp/p/cache2/entries/AB", "<profile>"),
    ("/usr/share/fonts/x.ttf (deleted)", "/usr/share/fonts/x.ttf (deleted)"),
    ("[heap]", None),
    ("[vdso]", None),
    ("", None),
])
def test_a_linux_path_is_spelled_the_same_on_every_machine(path, out):
    assert host.normalize_module(path, LIN) == out


def test_a_proc_and_a_run_user_path_lose_their_numbers():
    other = host.Context("linux", app=[], drops=[])
    assert host.normalize_module("/proc/4242/maps", other) == "/proc/N/maps"
    assert host.normalize_module("/run/user/1001/dconf/user", other) == "/run/user/N/dconf/user"


def test_two_runs_with_different_numbers_normalize_to_the_same_module_set():
    one = host.normalize_modules(["/dev/shm/org.mozilla.ipc.1.2", "/memfd:x-9 (deleted)", "/usr/lib/a.so"], LIN)
    two = host.normalize_modules(["/dev/shm/org.mozilla.ipc.5000.6000 (deleted)", "/memfd:x-123", "/usr/lib/a.so"], LIN)
    assert one == two == ["/usr/lib/a.so", "<memfd:x-N>", "<shm:org.mozilla.ipc.N.N>"]


def test_a_windows_system_library_of_another_case_is_the_same_module():
    assert host.normalize_modules(["C:\\Windows\\System32\\Kernel32.dll", "c:\\windows\\system32\\KERNEL32.DLL"], WIN) == ["<sys>/kernel32.dll"]


# -
# The strace log: the processes the tree made, by what they ran.

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


# -
# The Windows children that are gone before the snapshot: the WMI trace's events, from the engine's pid.

def test_the_wmi_events_below_the_engine_are_counted_by_name_and_the_rest_of_the_machine_is_not():
    events = [(10, 1, "explorer.exe"), (20, 10, "python.exe"), (30, 20, "firefox.exe"), (31, 30, "firefox.exe"),
              (32, 31, "firefox.exe"), (33, 30, "pingsender.exe"), (40, 10, "notepad.exe"), (99, 98, "svchost.exe")]
    assert host.wmi_names(events, {30}) == {"firefox.exe": 3, "pingsender.exe": 1}


def test_a_child_started_by_a_launcher_that_is_gone_is_still_below_the_engine():
    events = [(30, 20, "firefox.exe"), (31, 30, "firefox.exe"), (50, 31, "firefox.exe")]
    assert host.wmi_names(events, {30}) == {"firefox.exe": 3}


# -
# The quiet point.

def test_the_tree_is_quiet_when_it_has_not_changed_for_the_polls_in_a_row():
    reads = iter([{1}, {1, 2}, {1, 2}, {1, 2, 3}, {1, 2, 3}, {1, 2, 3}, {1, 2, 3}])
    now = iter(range(1000))
    got, quiet = host.wait_quiet(lambda: next(reads), polls=3, sleep=lambda s: None, now=lambda: next(now))
    assert got == {1, 2, 3} and quiet == {"reached": True, "reads": 6}


def test_a_tree_that_never_settles_ends_at_the_limit_and_says_so():
    counter = iter(range(10 ** 6))
    clock = iter(range(0, 10 ** 6, 10))
    _got, quiet = host.wait_quiet(lambda: {next(counter)}, limit_s=100, sleep=lambda s: None, now=lambda: next(clock))
    assert quiet["reached"] is False


def test_the_seeds_are_the_oracles_when_the_file_names_none(tmp_path, monkeypatch):
    monkeypatch.setattr(host, "SEEDS_FILE", tmp_path / "nope.json")
    assert host.default_seeds() == list(host.gate.SEEDS)
    f = tmp_path / "persona-seeds.json"
    f.write_text(json.dumps({"candidate": [5, 6]}), encoding="utf-8")
    monkeypatch.setattr(host, "SEEDS_FILE", f)
    assert host.default_seeds() == [5, 6]
