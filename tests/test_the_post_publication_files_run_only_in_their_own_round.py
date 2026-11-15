"""The e2e files that install from the index and read GitHub run only in the round after a publication (no browser).

``test_release_e2e.py`` and ``test_upgrade_e2e.py`` (pip, uv, PyPI, the GitHub release and API) can be red for a reason that is nobody's change, and the rule
of the project is that no gate depends on a real site: the gates of a pull request and of a candidate (``e2e.yml``, ``scripts/run_e2e.py``, the hunt) must
not collect them, and ``user-install.yml`` (the round after the publication, weekly and by hand) must still run both. See ``tests/_post_publication.py``.

* the helper: a file is left out unless the command line names it (as ``path`` or ``path::test``), and only the files of the list are touched;
* a real collection (``pytest --collect-only``, no browser): ``-m e2e`` over the whole tests directory holds no test of the two files, and naming them
  brings them back; a unit-marked test of the same files is not an e2e test and is not touched;
* the wiring: ``user-install.yml`` names both files with ``-m e2e``, no suite of ``ci/required-suites.json`` lists them (a suite that did would be red when
  they are left out), and no other workflow names them.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import _post_publication as post

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"


def test_a_file_of_the_list_is_left_out_unless_the_command_line_names_it():
    release = TESTS / "test_release_e2e.py"
    other = TESTS / "test_doh_through_the_proxy_e2e.py"
    assert post.is_left_out(release, [])
    assert post.is_left_out(release, [str(TESTS)]) and post.is_left_out(release, ["tests"])
    assert not post.is_left_out(release, [str(release)])
    assert not post.is_left_out(release, ["%s::test_x" % release])
    assert post.is_left_out(release, [str(TESTS / "test_upgrade_e2e.py")]), "naming the other file does not name this one"
    assert not post.is_left_out(other, []), "only the files of the list are touched"


def _collect(*args: str) -> str:
    out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", "e2e", "-o", "addopts=", "-p", "no:cacheprovider", *args],
                         cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert out.returncode in (0, 5), out.stdout[-2000:] + out.stderr[-2000:]
    return out.stdout


def test_a_collection_of_the_e2e_that_names_no_file_holds_none_of_the_two():
    listed = _collect("-k", "release_e2e or upgrade_e2e or persistent_profile", "tests")
    assert "test_persistent_profile_keeps_state.py" in listed, "the selection finds nothing: the check below would pass for no reason\n" + listed[-500:]
    assert "test_release_e2e" not in listed and "test_upgrade_e2e" not in listed, listed


def test_naming_the_two_files_brings_back_their_e2e_tests_and_only_those():
    listed = _collect("tests/test_release_e2e.py", "tests/test_upgrade_e2e.py")
    assert "test_release_e2e.py::test_clean_install_the_way_a_user_does_it" in listed
    assert "test_upgrade_e2e.py::test_upgrading_from_the_previous_release_moves_the_core_too" in listed
    assert "test_every_test_that_installs_from_the_index_has_the_install_deadline" not in listed, "a test with no e2e marker is not collected by -m e2e"


def test_the_round_after_the_publication_names_both_files_and_nothing_else_does():
    user_install = (ROOT / ".github" / "workflows" / "user-install.yml").read_text(encoding="utf-8")
    runs = re.findall(r"python -m pytest (tests/test_release_e2e\.py tests/test_upgrade_e2e\.py) -m e2e", user_install)
    assert len(runs) == 2, "user-install.yml runs the two files on Linux and on Windows, each named with -m e2e: %r" % runs
    for name in post.FILES:
        assert (TESTS / name).is_file(), name
    suites = json.loads((ROOT / "ci" / "required-suites.json").read_text(encoding="utf-8"))
    listed = [t for row in suites for t in row["tests"] if Path(t.split("::")[0]).name in post.FILES]
    assert listed == [], "a required suite lists a file that the runs leave out: %s" % listed
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        if workflow.name == "user-install.yml":
            continue
        text = workflow.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            assert not any(name in line for name in post.FILES), "%s runs a post-publication file outside user-install.yml: %s" % (workflow.name, line.strip())
