"""Every file of the unit suite is in the rows of ``ci/required-suites.json``, or is a named file of the environment: nothing falls between.

The candidate's judge accepts in ``tests`` only files and node ids, and each must show in the junit with all its tests passed. The whole unit suite
(``-m "not slow and not e2e"``) is therefore two rows that name every file, ``wrapper.unit.all.linux`` and ``wrapper.unit.all.windows``. A file that is
not in them is a file the candidate never runs; a file that is in them and depends on the machine that runs it (a package index, a workbench, a clone with
its hooks armed, a Windows job object) is a red for a reason that is not the code. This closes the circle both ways, on the files that the marker collects:

* every collected file is in BOTH rows, or in the row of its system alone when it is a file of one system (``ONE_SYSTEM``: it skips on the other), or is one of
  the files of the environment below, each with its reason, and in no row;
* every file a row names exists and has a test collected by the marker;
* the list of the files of the environment cannot grow without this test saying so: ``ENVIRONMENT_COUNT`` is changed by hand together with the list.

Known-bad inputs: a new file that no row names, a file of the environment added without the count, a row that names a file with no collected test, a file of
one system in the row of the other, a file in one row only, an entry that is a folder, a node id or an option. The green case is the state of the repository.
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
ROWS = {"linux": "wrapper.unit.all.linux", "windows": "wrapper.unit.all.windows"}
MARKER = "not slow and not e2e"

#: The files that are in no row because of WHERE they run, each with the reason. They are collected by the marker and skip (or are red) in the job of the
#: candidate; every one has its own file, with the reason in its name, so that what is left in the rows is what runs on every machine.
ENVIRONMENT_FILES = {
    "tests/test_version_is_not_taken.py": "asks the package index (a network call); the version job of tests.yml runs it with IPW_CHECK_VERSION",
    "tests/test_the_working_clone_has_the_workbench_next_to_it.py": "needs the workbench of the person who runs it next to the checkout",
    "tests/test_a_bundled_stock_firefox_is_refused_by_binary_path.py": "needs a stock Firefox bundled with Playwright in the cache of the user",
    "tests/test_the_working_clone_has_its_hooks_armed.py": "needs a clone with core.hooksPath armed, which a fresh checkout never has",
    "tests/test_the_working_clone_has_main_to_compare_the_debt.py": "needs origin/main in the checkout, which the job of the candidate does not fetch",
}
#: Changed by hand, together with the list above: a file of the environment is a hole in what the candidate runs, and it may not be added in silence.
ENVIRONMENT_COUNT = 5

#: The files that skip on the other system, and the only row that names each.
ONE_SYSTEM = {
    "tests/test_juggler_posix_pipe.py": "linux",
    "tests/test_prefs_composition_arm_on_windows.py": "windows",
    "tests/test_reaper_on_windows.py": "windows",
}


@functools.lru_cache(maxsize=None)
def _collected():
    """The files that have at least one test collected by the marker of the rows."""
    done = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "-o", "addopts=", "-m", MARKER, "tests"],
                          cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, "the collection failed: " + done.stdout[-1500:] + done.stderr[-500:]
    return frozenset(line.split("::", 1)[0] for line in done.stdout.splitlines() if "::" in line)


def _rows():
    by_id = {r["id"]: r for r in json.loads(REQUIRED.read_text(encoding="utf-8"))}
    return {system: by_id[row_id]["tests"] for system, row_id in ROWS.items()}


def accounting_problems(collected, rows, environment, one_system, environment_count):
    out = []
    for system, entries in rows.items():
        for entry in entries:
            if not (entry.startswith("tests/") and entry.endswith(".py")) or "::" in entry:
                out.append("%s: %r is not a file of tests/ (the judge takes files, and a node id only for the e2e)" % (ROWS[system], entry))
        if list(entries) != sorted(entries) or len(set(entries)) != len(entries):
            out.append("%s: the files are not sorted and unique" % ROWS[system])
        for entry in entries:
            if entry not in collected:
                out.append("%s: %s has no test collected by %r (it is not there, or nothing in it runs)" % (ROWS[system], entry, MARKER))
    if len(environment) != environment_count:
        out.append("the files of the environment are %d and the count says %d: change the count together with the list, and say why in the list"
                   % (len(environment), environment_count))
    for entry in sorted(environment):
        if entry not in collected:
            out.append("%s is a file of the environment and has no test collected by %r: take it out of the list" % (entry, MARKER))
        if not environment[entry].strip():
            out.append("%s is a file of the environment with no reason" % entry)
        for system, entries in rows.items():
            if entry in entries:
                out.append("%s is a file of the environment and %s names it" % (entry, ROWS[system]))
        if entry in one_system:
            out.append("%s is both a file of the environment and a file of one system" % entry)
    for entry in sorted(collected):
        if entry in environment:
            continue
        for system, entries in rows.items():
            wanted = one_system.get(entry, system) == system
            if wanted and entry not in entries:
                out.append("%s has tests and %s does not name it: add it to the row, or to the files of the environment with its reason" % (entry, ROWS[system]))
            if not wanted and entry in entries:
                out.append("%s is a file of %s only and %s names it" % (entry, one_system[entry], ROWS[system]))
    for entry, system in one_system.items():
        if system not in rows:
            out.append("%s: %r is not a system of the rows" % (entry, system))
    return out


def _state(**over):
    state = dict(collected=_collected(), rows=_rows(), environment=ENVIRONMENT_FILES, one_system=ONE_SYSTEM, environment_count=ENVIRONMENT_COUNT)
    state.update(over)
    return state


def test_every_file_with_tests_is_in_the_rows_or_is_a_named_file_of_the_environment():
    assert accounting_problems(**_state()) == []


def test_the_rows_do_not_name_the_folder_and_do_not_deselect():
    for entries in _rows().values():
        assert "tests" not in entries and [e for e in entries if e.startswith("-")] == []


# --- known-bad inputs ------------------------------------------------------------------------------------------------------------------------
_A = "tests/test_a.py"
_B = "tests/test_b.py"
_POSIX = "tests/test_posix_on_linux.py"
_WIN = "tests/test_win_on_windows.py"
_ENV = "tests/test_env.py"


def _fake(**over):
    state = dict(collected={_A, _B, _POSIX, _WIN, _ENV},
                 rows={"linux": [_A, _B, _POSIX], "windows": [_A, _B, _WIN]},
                 environment={_ENV: "needs the index"}, one_system={_POSIX: "linux", _WIN: "windows"}, environment_count=1)
    state.update(over)
    return state


def test_the_fake_state_is_green():
    assert accounting_problems(**_fake()) == []


def test_a_new_file_that_no_row_names_is_red():
    found = accounting_problems(**_fake(collected={_A, _B, _POSIX, _WIN, _ENV, "tests/test_new.py"}))
    assert len(found) == 2 and all("tests/test_new.py has tests and" in f for f in found), found


def test_a_file_of_the_environment_added_without_the_count_is_red():
    two = {_ENV: "needs the index", _B: "needs the workbench"}
    rows = {"linux": [_A, _POSIX], "windows": [_A, _WIN]}
    found = accounting_problems(**_fake(environment=two, rows=rows))
    assert any("the count says 1" in f for f in found), found
    assert accounting_problems(**_fake(environment=two, rows=rows, environment_count=2)) == []


def test_a_file_of_the_environment_with_no_reason_or_no_tests_is_red():
    assert accounting_problems(**_fake(environment={_ENV: " "})) != []
    found = accounting_problems(**_fake(environment={_ENV: "needs the index", "tests/test_gone.py": "gone"}, environment_count=2))
    assert any("tests/test_gone.py is a file of the environment and has no test" in f for f in found), found


def test_a_file_of_the_environment_that_a_row_names_is_red():
    found = accounting_problems(**_fake(rows={"linux": [_A, _B, _ENV, _POSIX], "windows": [_A, _B, _WIN]}))
    assert len(found) == 1 and "is a file of the environment and wrapper.unit.all.linux names it" in found[0], found


def test_a_row_that_names_a_file_with_no_collected_test_is_red():
    found = accounting_problems(**_fake(rows={"linux": [_A, _B, "tests/test_gone.py", _POSIX], "windows": [_A, _B, _WIN]}))
    assert any("tests/test_gone.py has no test collected" in f for f in found), found


def test_a_file_in_one_row_only_is_red():
    found = accounting_problems(**_fake(rows={"linux": [_A, _B, _POSIX], "windows": [_A, _WIN]}))
    assert len(found) == 1 and "tests/test_b.py has tests and wrapper.unit.all.windows does not name it" in found[0], found


def test_a_file_of_one_system_in_the_row_of_the_other_is_red():
    found = accounting_problems(**_fake(rows={"linux": [_A, _B, _POSIX], "windows": [_A, _B, _POSIX, _WIN]}))
    assert len(found) == 1 and "is a file of linux only and wrapper.unit.all.windows names it" in found[0], found
    found = accounting_problems(**_fake(rows={"linux": [_A, _B], "windows": [_A, _B, _WIN]}))
    assert len(found) == 1 and "test_posix_on_linux.py has tests and wrapper.unit.all.linux does not name it" in found[0], found


def test_a_folder_a_node_id_or_an_option_is_red():
    for entry in ("tests", "tests/test_a.py::test_x", "--deselect=tests/test_a.py::test_x", "-k"):
        found = accounting_problems(**_fake(rows={"linux": [_A, _B, _POSIX, entry], "windows": [_A, _B, _WIN]}))
        assert any("is not a file of tests/" in f for f in found), (entry, found)


def test_a_row_that_is_not_sorted_is_red():
    found = accounting_problems(**_fake(rows={"linux": [_B, _A, _POSIX], "windows": [_A, _B, _WIN]}))
    assert found == ["wrapper.unit.all.linux: the files are not sorted and unique"], found
