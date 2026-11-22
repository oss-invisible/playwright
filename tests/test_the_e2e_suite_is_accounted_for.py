"""Every e2e test of the wrapper is in exactly one row of the whole-e2e suite on each system, or is a named exclusion with its reason: nothing falls between.

The candidate's gate runs the e2e of the wrapper on the engine under test as rows of ``ci/required-suites.json``: ``wrapper.e2e.suite.linux.1`` to ``.3`` on Linux,
``wrapper.e2e.suite.windows.1`` to ``.4`` on Windows, and ``wrapper.e2e.suite.windows.control_a`` (the keyboard shortcut, alone on its own runner). The judge takes
files and node ids only, and a skipped test is red, so the partition is a list written by hand and this test is what keeps it whole. With no browser, from the
collection of ``-m e2e``:

* every collected test is covered by exactly ONE row of each system (a file entry covers all its tests, a node entry covers the parameters of that test), unless it
  is excluded on that system, and an excluded test is covered by NO row: a file entry that would hold an excluded test is refused, its tests go by name;
* every entry of a row is a file or a ``file::test`` of ``tests/`` that has a collected test, the entries are sorted and unique, and the minimum of the row
  is the number of tests it covers (the judge's rows are exact: a test added to a file moves the count here, by hand);
* the exclusions are a named list, each with the system it holds for and the reason, and the list cannot grow without its count: the tests of the rows of
  ``wrapper.e2e.udp.exit`` and ``wrapper.e2e.udp.sandbox`` (root, user namespaces: their own rows), the file of Linux that has no Windows counterpart, and the tests
  that skip on a system (a skip is red in the verdict, so a test that is for one system is not collected on the other: its row says so);
* the Control+A row is that one file, which is also in a row of each system's partition.

The files of the round after the publication (``tests/_post_publication.py``) are not collected by ``-m e2e`` and are in no row.

Known-bad inputs: a new test that no row names, a test in two rows, an excluded test named by a file entry, an exclusion added without the count or without a
reason or that matches nothing, a row that names a file with no collected test, a node entry with a parameter, a folder or an option, entries that are not sorted,
a minimum that is not the count, a Control+A row that is not that file. The green case is the state of the repository.
"""
from __future__ import annotations

import functools
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = ROOT / "ci" / "required-suites.json"
MARKER = "e2e"

PARTITION = {
    "linux": ["wrapper.e2e.suite.linux.1", "wrapper.e2e.suite.linux.2", "wrapper.e2e.suite.linux.3"],
    "windows": ["wrapper.e2e.suite.windows.1", "wrapper.e2e.suite.windows.2", "wrapper.e2e.suite.windows.3", "wrapper.e2e.suite.windows.4"],
}
CONTROL_A_ROW = "wrapper.e2e.suite.windows.control_a"
CONTROL_A_FILE = "tests/test_control_a_selects_all_in_a_field_e2e.py"

_BOTH = ("linux", "windows")
#: What no row of the suite runs, by file or by ``file::test`` (every parameter of the test), the systems it holds for, and why. The skip reasons are the ones the
#: junit of run 44942540063 carries.
EXCLUDED = {
    "tests/test_a_proxy_that_cannot_carry_udp_leaks_nothing_direct_e2e.py":
        (_BOTH, "wrapper.e2e.udp.exit: the lab's exit is an address held on lo as root (Linux only), run in the root step"),
    "tests/test_the_whole_udp_chain_by_name_e2e.py::test_a_turn_srflx_by_name_arrives_through_the_relay_before_the_tick":
        (_BOTH, "wrapper.e2e.udp.exit: as root, with the exit of the lab on lo"),
    "tests/test_the_socket_process_is_sandboxed_e2e.py":
        (_BOTH, "wrapper.e2e.udp.sandbox: needs unprivileged user namespaces, which the runner refuses a normal user until its own step turns them on"),
    "tests/test_webrtc_realness.py::test_the_gather_is_real_without_a_proxy":
        (_BOTH, "wrapper.e2e.udp.sandbox: runs with the sandbox of the socket process, in its own row"),
    "tests/test_webrtc_realness.py::test_a_gateway_with_coherent_udp_gives_the_page_a_srflx_born_through_its_relay":
        (_BOTH, "wrapper.e2e.udp.sandbox: runs with the sandbox of the socket process, in its own row"),
    "tests/test_ice_names_by_the_context_proxy_e2e.py::test_a_gateway_without_udp_resolves_the_ice_name_then_ends_in_701":
        (_BOTH, "wrapper.e2e.udp.sandbox: runs with the sandbox of the socket process, in its own row"),
    "tests/test_juggler_injected.py::test_OUR_CLIENT_DOES_NOT_CROSS_into_the_page_realm":
        (_BOTH, "skips on every system: the trap page lives in the workbench, not in this checkout"),
    "tests/test_the_engine_is_born_in_the_job_e2e.py::test_when_the_client_is_killed_no_engine_process_is_left":
        (("linux",), "skips on Linux: the job object is Windows; Linux has no equivalent here"),
    "tests/test_hidden_desktop.py::test_hidden_desktop_takes_the_window_off_screen_but_keeps_rendering":
        (("linux",), "skips on Linux: the hidden desktop is the Windows path; Linux hides via Xvfb"),
    "tests/test_first_launch_from_a_new_path.py::test_the_first_launch_from_a_new_path_works":
        (("linux",), "skips on Linux: the launcher process exists only on Windows"),
    "tests/test_the_parent_reads_the_bundle_from_fontconfig_s_cache_linux_e2e.py":
        (("windows",), "wrapper.e2e.fontconfig is linux_only: fontconfig exists only on Linux (DirectWrite opens a font from its path)"),
    "tests/test_webgl_draws_on_the_engine_s_own_gl_e2e.py::test_webgl_draws_on_the_engine_s_own_gl":
        (("windows",), "skips on Windows: the engine's own GL is Linux's"),
    "tests/test_webgl_draws_on_the_engine_s_own_gl_e2e.py::test_every_declared_extension_answers_as_on_windows":
        (("windows",), "skips on Windows: the engine's own GL is Linux's"),
    "tests/test_hidden_display_linux.py::test_hidden_display_stays_with_its_session":
        (("windows",), "skips on Windows: Xvfb is the Linux path; Windows hides on a desktop"),
    "tests/test_juggler_posix_pipe.py::test_the_posix_pipe_carries_messages_both_ways":
        (("windows",), "skips on Windows: the POSIX leg does not execute there, the pipe travels as inheritable HANDLEs"),
    "tests/test_screenshot_of_text_returns_linux_e2e.py::test_a_screenshot_of_a_page_with_text_returns":
        (("windows",), "skips on Windows: the Linux text path"),
}
#: Changed by hand, together with the list above: an exclusion is a test the candidate does not run, and it may not be added in silence.
EXCLUDED_COUNT = 16


@functools.lru_cache(maxsize=None)
def _collected():
    """Every node id the marker collects, parameters included (the files of the round after the publication are left out by the conftest)."""
    done = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "-o", "addopts=", "-m", MARKER, "tests"],
                          cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, "the collection failed: " + done.stdout[-1500:] + done.stderr[-500:]
    return frozenset(line.strip() for line in done.stdout.splitlines() if "::" in line)


def _rows():
    by_id = {r["id"]: r for r in json.loads(REQUIRED.read_text(encoding="utf-8"))}
    ids = [i for ids in PARTITION.values() for i in ids] + [CONTROL_A_ROW]
    missing = [i for i in ids if i not in by_id]
    assert not missing, "rows missing from ci/required-suites.json: %s" % missing
    return {i: by_id[i] for i in ids}


def _file(node):
    return node.split("::", 1)[0]


def _base(node):
    """``file::test`` of a node id, without the parameters."""
    return node.split("[", 1)[0]


def _covers(entry, node):
    return _file(node) == entry if "::" not in entry else _base(node) == entry


def _excluded(node, system, excluded):
    return any(system in systems and _covers(entry, node) for entry, (systems, _why) in excluded.items())


def accounting_problems(collected, rows, partition, control_a_row, control_a_file, excluded, excluded_count):
    """-> why the partition is not whole (empty if it is). ``rows`` is {id: row of the list}."""
    out = []
    for rid, row in rows.items():
        entries = row["tests"]
        for entry in entries:
            if not (entry.startswith("tests/") and entry.split("::", 1)[0].endswith(".py")) or entry.count("::") > 1 or "[" in entry:
                out.append("%s: %r is not a file of tests/ or a `file::test` with no parameter" % (rid, entry))
            elif not any(_covers(entry, n) for n in collected):
                out.append("%s: %s has no test collected by %r (it is not there, or nothing in it runs)" % (rid, entry, MARKER))
        if list(entries) != sorted(entries) or len(set(entries)) != len(entries):
            out.append("%s: the entries are not sorted and unique" % rid)
    if len(excluded) != excluded_count:
        out.append("the exclusions are %d and the count says %d: change the count together with the list, and say why in the list" % (len(excluded), excluded_count))
    for entry, (systems, why) in sorted(excluded.items()):
        if not why.strip():
            out.append("%s is excluded with no reason" % entry)
        if not systems or not set(systems) <= set(partition):
            out.append("%s is excluded for %r, which are not systems of the partition" % (entry, systems))
        if not any(_covers(entry, n) for n in collected):
            out.append("%s is excluded and nothing collected by %r matches it: take it out of the list" % (entry, MARKER))
    for system, ids in partition.items():
        for node in sorted(collected):
            named = [rid for rid in ids if any(_covers(e, node) for e in rows[rid]["tests"])]
            if _excluded(node, system, excluded):
                if named:
                    out.append("%s is excluded on %s and %s names it: a file entry holds it, so its tests go by name" % (node, system, ", ".join(named)))
            elif not named:
                out.append("%s has a test no row of %s names (%s): add it to a row, or to the exclusions with its reason" % (_file(node), system, _base(node)))
            elif len(named) > 1:
                out.append("%s is in more than one row of %s: %s" % (node, system, ", ".join(named)))
        for rid in ids:
            held = sum(1 for n in collected if not _excluded(n, system, excluded) and any(_covers(e, n) for e in rows[rid]["tests"]))
            if rows[rid]["min_tests"] != held:
                out.append("%s: min_tests is %s and the row covers %d collected test(s)" % (rid, rows[rid]["min_tests"], held))
    row = rows[control_a_row]
    if row["tests"] != [control_a_file]:
        out.append("%s must be exactly %s, not %s" % (control_a_row, control_a_file, row["tests"]))
    else:
        held = sum(1 for n in collected if _file(n) == control_a_file)
        if row["min_tests"] != held:
            out.append("%s: min_tests is %s and the file has %d collected test(s)" % (control_a_row, row["min_tests"], held))
        for system, ids in partition.items():
            if not any(control_a_file in rows[rid]["tests"] for rid in ids):
                out.append("%s is in no whole-suite row of %s: the Control+A row is its twin, not its place" % (control_a_file, system))
    return out


def _state(**over):
    state = dict(collected=_collected(), rows=_rows(), partition=PARTITION, control_a_row=CONTROL_A_ROW, control_a_file=CONTROL_A_FILE,
                 excluded=EXCLUDED, excluded_count=EXCLUDED_COUNT)
    state.update(over)
    return state


def test_every_e2e_test_is_in_one_row_of_each_system_or_is_a_named_exclusion():
    assert accounting_problems(**_state()) == []


def test_the_rows_of_the_suite_are_exact_e2e_rows_on_their_own_system():
    for system, ids in PARTITION.items():
        for rid in ids:
            row = _rows()[rid]
            assert row["os"] == [system] and row["marker"] == MARKER and row["exact"] is True and row["giro"] == "G2", rid
    row = _rows()[CONTROL_A_ROW]
    assert row["os"] == ["windows"] and row["marker"] == MARKER and row["exact"] is True


# --- known-bad inputs ------------------------------------------------------------------------------------------------------------------------
_A, _B, _C, _D = "tests/test_a.py", "tests/test_b.py", "tests/test_c.py", "tests/test_ctl.py"
_NODES = {_A + "::test_one", _A + "::test_two[x]", _A + "::test_two[y]", _B + "::test_one", _B + "::test_skip", _C + "::test_one", _D + "::test_one",
          _D + "::test_two"}
_EXCLUDED = {_B + "::test_skip": (("linux",), "skips on Linux"), _C: (("windows",), "a file of Linux")}
_PARTITION = {"linux": ["l.1", "l.2"], "windows": ["w.1", "w.2"]}


def _row(entries, n, system):
    return {"os": [system], "marker": "e2e", "exact": True, "giro": "G2", "min_tests": n, "tests": entries}


def _rows_fake(**over):
    rows = {"l.1": _row([_A, _B + "::test_one", _D], 6, "linux"), "l.2": _row([_C], 1, "linux"),
            "w.1": _row([_A, _D], 5, "windows"), "w.2": _row([_B], 2, "windows"), "ctl": _row([_D], 2, "windows")}
    rows.update(over)
    return rows


def _fake(**over):
    state = dict(collected=_NODES, rows=_rows_fake(), partition=_PARTITION, control_a_row="ctl", control_a_file=_D, excluded=_EXCLUDED, excluded_count=2)
    state.update(over)
    return state


def test_the_fake_state_is_green():
    assert accounting_problems(**_fake()) == []


def test_a_new_test_that_no_row_names_is_red():
    found = accounting_problems(**_fake(collected=_NODES | {"tests/test_new.py::test_x"}))
    assert len(found) == 2 and all("tests/test_new.py has a test no row of" in f for f in found), found


def test_a_new_test_in_a_file_a_row_names_by_node_is_red():
    found = accounting_problems(**_fake(collected=_NODES | {_B + "::test_added"}))
    assert any("tests/test_b.py has a test no row of linux names (tests/test_b.py::test_added)" in f for f in found), found


def test_a_test_in_two_rows_of_one_system_is_red():
    found = accounting_problems(**_fake(rows=_rows_fake(**{"l.2": _row([_C, _A], 4, "linux")})))
    assert any("is in more than one row of linux: l.1, l.2" in f for f in found), found


def test_an_excluded_test_that_a_file_entry_holds_is_red():
    found = accounting_problems(**_fake(rows=_rows_fake(**{"l.1": _row([_A, _B, _D], 6, "linux")})))
    assert any("tests/test_b.py::test_skip is excluded on linux and l.1 names it" in f for f in found), found


def test_a_file_excluded_for_one_system_only_is_still_run_by_the_other_and_refused_where_it_is_excluded():
    assert accounting_problems(**_fake()) == [] and _C in _EXCLUDED and _C in _rows_fake()["l.2"]["tests"]
    found = accounting_problems(**_fake(rows=_rows_fake(**{"w.1": _row([_A, _C, _D], 6, "windows")})))
    assert any("tests/test_c.py::test_one is excluded on windows and w.1 names it" in f for f in found), found


def test_an_exclusion_added_without_the_count_or_without_a_reason_or_matching_nothing_is_red():
    more = dict(_EXCLUDED, **{_A + "::test_one": (("linux", "windows"), "because")})
    found = accounting_problems(**_fake(excluded=more))
    assert any("the count says 2" in f for f in found), found
    assert accounting_problems(**_fake(excluded=dict(_EXCLUDED, **{_C: (("windows",), " ")}))) != []
    found = accounting_problems(**_fake(excluded=dict(_EXCLUDED, **{"tests/test_gone.py": (("linux",), "gone")}), excluded_count=3))
    assert any("tests/test_gone.py is excluded and nothing collected" in f for f in found), found
    found = accounting_problems(**_fake(excluded=dict(_EXCLUDED, **{_C: (("macos",), "x")})))
    assert any("not systems of the partition" in f for f in found), found


def test_a_row_that_names_a_file_with_no_collected_test_is_red():
    found = accounting_problems(**_fake(rows=_rows_fake(**{"l.2": _row([_C, "tests/test_gone.py"], 1, "linux")})))
    assert any("tests/test_gone.py has no test collected" in f for f in found), found
    found = accounting_problems(**_fake(rows=_rows_fake(**{"l.2": _row([_C, _A + "::test_nothing"], 1, "linux")})))
    assert any("test_nothing has no test collected" in f for f in found), found


def test_a_parameter_a_folder_or_an_option_is_red():
    for entry in ("tests", "tests/test_a.py::test_two[x]", "--deselect=tests/test_a.py::test_one", "-k", "tests/test_a.py::T::test_one::deeper"):
        found = accounting_problems(**_fake(rows=_rows_fake(**{"l.2": _row([_C, entry], 1, "linux")})))
        assert any("is not a file of tests/" in f for f in found), (entry, found)


def test_entries_that_are_not_sorted_or_are_repeated_are_red():
    found = accounting_problems(**_fake(rows=_rows_fake(**{"l.1": _row([_D, _A, _B + "::test_one"], 6, "linux")})))
    assert found == ["l.1: the entries are not sorted and unique"], found
    found = accounting_problems(**_fake(rows=_rows_fake(**{"l.1": _row([_A, _A, _B + "::test_one", _D], 6, "linux")})))
    assert any("not sorted and unique" in f for f in found), found


def test_a_minimum_that_is_not_the_count_is_red():
    found = accounting_problems(**_fake(rows=_rows_fake(**{"w.2": _row([_B], 3, "windows")})))
    assert found == ["w.2: min_tests is 3 and the row covers 2 collected test(s)"], found
    found = accounting_problems(**_fake(rows=_rows_fake(**{"w.1": _row([_A, _D], 4, "windows")})))
    assert found == ["w.1: min_tests is 4 and the row covers 5 collected test(s)"], found


def test_a_control_a_row_that_is_not_that_file_or_a_file_in_no_partition_row_is_red():
    found = accounting_problems(**_fake(rows=_rows_fake(ctl=_row([_A], 3, "windows"))))
    assert any("ctl must be exactly tests/test_ctl.py" in f for f in found), found
    found = accounting_problems(**_fake(rows=_rows_fake(ctl=_row([_D], 3, "windows"))))
    assert found == ["ctl: min_tests is 3 and the file has 2 collected test(s)"], found
    # the Control+A file in another row of Windows is as good as in the first: what is refused is a Windows partition that does not hold it
    rows = _rows_fake(**{"w.1": _row([_A], 3, "windows"), "w.2": _row([_B, _D], 4, "windows")})
    assert accounting_problems(**_fake(rows=rows)) == []
    rows = _rows_fake(**{"w.1": _row([_A], 3, "windows")})
    found = accounting_problems(**_fake(rows=rows))
    assert any("has a test no row of windows names" in f for f in found), found
