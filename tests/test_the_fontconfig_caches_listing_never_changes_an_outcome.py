"""The listing of the fontconfig caches of the runner is evidence only: whatever goes wrong inside it, the outcome of a test does not change (no browser).

Why it exists. On a Linux e2e run the cold launch of ``test_the_warm_parent_reads_the_bundle_from_fontconfig_cache_and_does_not_scan_it`` found a valid cache of the bundle in
``~/.fontconfig/`` of the runner's home (written by an earlier launch on that runner, which one is unknown) and wrote none under its own ``XDG_CACHE_HOME``. To know WHO writes
``~/.fontconfig`` and ``~/.cache/fontconfig``, ``tests/_fontconfig_caches.py`` writes, after each e2e module on Linux, one greppable line per cache file that is new or changed
(``[fontconfig-caches] after <module>: new <path> mtime=<ISO, nanoseconds> size=<bytes>``), and the failure of that test carries the same listing. It never lists the folders
by creating them, never touches fontconfig and never isolates ``HOME`` (that would hide the read it is looking for).

What these tests hold. The helper is made to FAIL on purpose, and the test it wraps comes out the same:

* a folder that cannot be read, a file that vanishes between the listing and its ``stat``, a system that is not Linux, a line that cannot be written: each is a line that says
  what went wrong, and nothing escapes;
* the fixture yields once, whatever happens before and after it, and a real run of pytest with a passing and a failing test, under a conftest whose listing fails on every call,
  gives exactly ``1 failed, 1 passed`` and no error;
* the delta is right: a file that is new, a file that changed, a file that is gone, and nothing for the files that did not change.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import _fontconfig_caches as fcc

pytestmark = pytest.mark.unit

TESTS = Path(__file__).resolve().parent
LINE = re.compile(r"^\[fontconfig-caches\] after (?P<module>\S+): (?P<kind>new|changed|gone) (?P<path>\S+) "
                  r"mtime=(?P<mtime>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{9}Z) size=(?P<size>\d+)$")


def _says(lines, word):
    return [l for l in lines if l.startswith("[fontconfig-caches]") and word in l]


def test_a_folder_that_cannot_be_read_is_a_line_and_not_an_exception(tmp_path):
    def listdir(path):
        raise PermissionError(13, "Permission denied", path)

    folder = str(tmp_path / ".fontconfig")
    state, problems = fcc.snapshot([folder], platform="linux", listdir=listdir)
    assert state == {}
    assert len(problems) == 1 and folder in problems[0] and "PermissionError" in problems[0]


def test_a_missing_folder_is_not_a_problem_and_is_not_created(tmp_path):
    missing = tmp_path / "never" / "fontconfig"
    state, problems = fcc.snapshot([str(missing)], platform="linux")
    assert (state, problems) == ({}, [])
    assert not missing.exists() and not missing.parent.exists()


def test_a_file_that_vanishes_between_the_listing_and_its_stat_is_a_line_and_the_others_are_kept(tmp_path):
    (tmp_path / "keep.cache-9").write_bytes(b"abc")
    (tmp_path / "gone.cache-9").write_bytes(b"abcd")
    real_stat = os.stat

    def stat(path, *a, **k):
        if os.path.basename(path) == "gone.cache-9":
            raise FileNotFoundError(2, "No such file or directory", path)
        return real_stat(path, *a, **k)

    state, problems = fcc.snapshot([str(tmp_path)], platform="linux", stat=stat)
    assert list(state) == [str(tmp_path / "keep.cache-9")] and state[str(tmp_path / "keep.cache-9")][1] == 3
    assert len(problems) == 1 and "gone.cache-9" in problems[0] and "FileNotFoundError" in problems[0]


def test_a_system_that_is_not_linux_lists_nothing_and_says_so(tmp_path):
    def listdir(path):
        raise AssertionError("a folder was read on a system that is not Linux")

    state, problems = fcc.snapshot([str(tmp_path / ".fontconfig")], platform="win32", listdir=listdir)
    assert state == {} and len(problems) == 1 and "not Linux" in problems[0] and "win32" in problems[0]


def test_the_fixture_yields_once_and_nothing_escapes_when_every_part_of_it_fails():
    lines = []

    def listdir(path):
        raise OSError(5, "Input/output error", path)

    def emit(line):
        lines.append(line)
        raise RuntimeError("the log cannot be written")

    watch = fcc.Watch()
    generator = fcc.around_a_module("tests.test_x_e2e", True, ["/h/.fontconfig", "/h/.cache/fontconfig"], watch=watch,
                                    platform="linux", listdir=listdir, emit=emit)
    assert next(generator) is None  # it yielded: the module's tests would run
    with pytest.raises(StopIteration):
        next(generator)  # and the end of the module is silent
    assert _says(lines, "error"), lines
    assert any("OSError" in l and "/h/.fontconfig" in l for l in lines), lines
    assert all(l.startswith("[fontconfig-caches] ") for l in lines)


def test_a_module_that_is_not_active_writes_nothing_and_lists_nothing():
    lines = []

    def listdir(path):
        raise AssertionError("listed")

    generator = fcc.around_a_module("tests.test_x", False, ["/h/.fontconfig"], watch=fcc.Watch(), platform="linux", listdir=listdir, emit=lines.append)
    next(generator)
    with pytest.raises(StopIteration):
        next(generator)
    assert lines == []


def test_a_test_that_fails_and_a_test_that_passes_keep_their_outcome_when_the_listing_fails_on_every_call(tmp_path):
    (tmp_path / "conftest.py").write_text(
        "import sys, pytest\n"
        "sys.path.insert(0, %r)\n"
        "import _fontconfig_caches as fcc\n"
        "def _bad(*a, **k):\n"
        "    raise RuntimeError('on purpose')\n"
        "@pytest.fixture(scope='module', autouse=True)\n"
        "def _listing():\n"
        "    yield from fcc.around_a_module('m', True, ['/h/.fontconfig'], watch=fcc.Watch(), platform='linux', listdir=_bad, stat=_bad, emit=_bad)\n"
        % str(TESTS), encoding="utf-8")
    (tmp_path / "test_m.py").write_text("def test_green():\n    assert True\n\ndef test_red():\n    assert False, 'red as it was'\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", "test_m.py"],
                          cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120)
    summary = done.stdout.strip().splitlines()[-1]
    assert re.sub(r" in [\d.]+s.*$", "", summary) == "1 failed, 1 passed", done.stdout  # no "error" in it: a fixture that raised would be one
    assert "red as it was" in done.stdout and done.returncode == 1


def test_the_delta_is_the_files_that_are_new_or_changed_or_gone_and_nothing_else():
    before = {"/h/.fontconfig/a": (1_000_000_000, 10), "/h/.fontconfig/b": (2_000_000_000, 20), "/h/.fontconfig/d": (4_000_000_000, 40)}
    after = {"/h/.fontconfig/a": (1_000_000_000, 10), "/h/.fontconfig/b": (3_000_000_123, 20), "/h/.fontconfig/c": (5_000_000_000, 5)}
    assert fcc.delta(before, after) == [("changed", "/h/.fontconfig/b", 3_000_000_123, 20), ("new", "/h/.fontconfig/c", 5_000_000_000, 5),
                                        ("gone", "/h/.fontconfig/d", 4_000_000_000, 40)]
    assert fcc.delta(after, after) == []
    # the size alone is a change too
    assert fcc.delta({"/p": (1, 1)}, {"/p": (1, 2)}) == [("changed", "/p", 1, 2)]


def test_a_module_writes_the_delta_against_the_previous_listing_and_no_change_is_one_line(tmp_path):
    folder = tmp_path / ".fontconfig"
    folder.mkdir()
    lines = []
    watch = fcc.Watch()

    def one_module(name):
        generator = fcc.around_a_module(name, True, [str(folder)], watch=watch, platform="linux", emit=lines.append)
        next(generator)
        return generator

    first = one_module("tests.test_first_e2e")
    (folder / "aaaa-le64.cache-9").write_bytes(b"x" * 7)
    with pytest.raises(StopIteration):
        next(first)
    second = one_module("tests.test_second_e2e")
    with pytest.raises(StopIteration):
        next(second)
    third = one_module("tests.test_third_e2e")
    (folder / "aaaa-le64.cache-9").write_bytes(b"y" * 9)
    (folder / "bbbb-le64.cache-9").write_bytes(b"z")
    with pytest.raises(StopIteration):
        next(third)

    matched = [LINE.match(l) for l in lines if " after " in l and "no change" not in l]
    assert all(matched), lines
    got = [(m["module"], m["kind"], os.path.basename(m["path"]), int(m["size"])) for m in matched]
    assert got == [("tests.test_first_e2e", "new", "aaaa-le64.cache-9", 7),
                   ("tests.test_third_e2e", "changed", "aaaa-le64.cache-9", 9),
                   ("tests.test_third_e2e", "new", "bbbb-le64.cache-9", 1)], lines
    assert [l for l in lines if "no change" in l] == ["[fontconfig-caches] after tests.test_second_e2e: no change"]


def test_the_first_module_says_what_was_there_before_it(tmp_path):
    folder = tmp_path / ".fontconfig"
    folder.mkdir()
    (folder / "old-le64.cache-9").write_bytes(b"12345")
    lines = []
    generator = fcc.around_a_module("tests.test_first_e2e", True, [str(folder)], watch=fcc.Watch(), platform="linux", emit=lines.append)
    next(generator)
    with pytest.raises(StopIteration):
        next(generator)
    assert [l for l in lines if "before" in l and "old-le64.cache-9" in l and "size=5" in l], lines


def test_the_listing_of_a_failure_message_has_the_mtimes_and_never_raises(tmp_path):
    folder = tmp_path / ".fontconfig"
    folder.mkdir()
    (folder / "abc-le64.cache-9").write_bytes(b"123")
    text = fcc.listing_text([str(folder), str(tmp_path / "nofolder")], platform="linux")
    assert "abc-le64.cache-9" in text and re.search(r"mtime=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{9}Z size=3", text) and ": absent" in text

    def listdir(path):
        raise PermissionError(13, "Permission denied", path)

    text = fcc.listing_text([str(folder)], platform="linux", listdir=listdir)
    assert "PermissionError" in text
    assert "not Linux" in fcc.listing_text([str(folder)], platform="win32")


def test_the_three_folders_are_the_ones_the_runner_writes_to(tmp_path):
    home = tmp_path.as_posix()
    assert fcc.folders(home) == [home + "/.fontconfig", home + "/.cache/fontconfig", "/var/cache/fontconfig"]
