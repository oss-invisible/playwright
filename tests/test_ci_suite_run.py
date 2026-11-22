"""scripts/ci_suite_run.py runs one row of the whole-e2e suite and hands the judge ONE junit (no browser here: the run of the core is a fake).

What is held: the arguments of the run are the files and node ids of the row, in its order, and nothing else of the row (the passes bring their own marker); the
two junit files of the passes become one xunit2 file with every testcase kept as pytest wrote it, counted from the cases and not from the headers, and the judge
reads it; the run reaches the judge when a test is red (exit 0 for the passes that ran to their end) and does not when a pass did not run to its end or nothing
was written; the variable that tells the core where its junit goes is set for the run and put back after it.

Known-bad inputs: a pass that wrote nothing, a second pass with no test, a skipped and a failed case among the passed, a row that is not in the list, a row of
the other system, a pass that died (exit 2), a run that wrote no junit at all.
"""
from __future__ import annotations

import importlib.util
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name + "_under_test", ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


suite_run = _load("ci_suite_run")
from invisible_core.testing import e2e as core_e2e  # noqa: E402

ROW = "s.e2e.one"
TESTS = ["tests/test_a_e2e.py", "tests/test_b_e2e.py::test_one", "tests/test_c_e2e.py"]


def _required(tmp_path, **over):
    row = {"id": ROW, "giro": "G2", "os": ["linux"], "min_tests": 4, "exact": True, "tests": TESTS, "marker": "e2e", "needs_root": False}
    row.update(over)
    path = tmp_path / "required.json"
    path.write_text(json.dumps([row]), encoding="utf-8")
    return str(path)


def _case(classname, name, outcome="passed", seconds="1.500"):
    case = ET.Element("testcase", {"classname": classname, "name": name, "time": seconds})
    if outcome != "passed":
        ET.SubElement(case, outcome, {"message": "x"})
    return case


def _write(path, cases):
    suite = ET.Element("testsuite", {"name": "pytest", "errors": "0", "failures": "0", "skipped": "0", "tests": str(len(cases)), "time": "9.999"})
    suite.extend(cases)
    root = ET.Element("testsuites")
    root.append(suite)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def _fake_run(code, per_pass, seen):
    """A stand-in for ``invisible_core.testing.e2e.main``: records its call and writes the junit of each pass where the core would."""
    def run(argv, repo):
        seen.append({"argv": list(argv), "repo": repo, "dir": suite_run.os.environ.get(core_e2e.JUNIT_DIR_ENV)})
        for (_width, selection), cases in zip(core_e2e.passes("4"), per_pass):
            if cases is not None:
                _write(core_e2e.junit_file(Path(seen[-1]["dir"]), selection), cases)
        return code
    return run


def test_the_arguments_are_the_files_and_the_nodes_of_the_row_in_its_order(tmp_path):
    assert suite_run.row_arguments(_required(tmp_path), ROW, "linux") == TESTS


def test_a_row_that_is_not_in_the_list_or_is_of_the_other_system_is_refused(tmp_path):
    required = _required(tmp_path)
    for suite, os_name in (("s.e2e.none", "linux"), (ROW, "windows")):
        with pytest.raises(suite_run.ci_verdict.Usage):
            suite_run.row_arguments(required, suite, os_name)
    assert suite_run.main(["--required", required, "--suite", "s.e2e.none", "--os", "linux", "--binary", "x", "--junit", str(tmp_path / "o.xml")]) == 2


def test_the_two_junit_files_of_the_passes_become_one_xunit2_file_the_judge_reads(tmp_path):
    first, second, out = tmp_path / "a.xml", tmp_path / "b.xml", tmp_path / "out" / "merged.xml"
    _write(first, [_case("tests.test_a_e2e", "test_x"), _case("tests.test_b_e2e", "test_one[p1]", "failure"), _case("tests.test_c_e2e", "test_y", "skipped")])
    _write(second, [_case("tests.test_c_e2e", "test_clock", "error", "2.000")])
    assert suite_run.merge_junits([first, second], out) == 4
    root = ET.parse(out).getroot()
    suites = list(root.iter("testsuite"))
    assert len(suites) == 1 and suites[0].attrib["tests"] == "4"
    assert (suites[0].attrib["failures"], suites[0].attrib["errors"], suites[0].attrib["skipped"]) == ("1", "1", "1")
    assert suites[0].attrib["time"] == "6.500"
    cases, no_class = suite_run.ci_verdict.parse_junit(out)
    assert no_class == [] and [n for n, _o in cases] == ["tests/test_a_e2e.py::test_x", "tests/test_b_e2e.py::test_one[p1]", "tests/test_c_e2e.py::test_y",
                                                           "tests/test_c_e2e.py::test_clock"]
    assert [o for _n, o in cases] == ["passed", "failed", "skipped", "errors"]


def test_a_pass_that_wrote_nothing_is_not_an_error_of_the_merge_and_a_pass_with_no_test_adds_none(tmp_path):
    first, empty, out = tmp_path / "a.xml", tmp_path / "e.xml", tmp_path / "m.xml"
    _write(first, [_case("tests.test_a_e2e", "test_x")])
    _write(empty, [])
    assert suite_run.merge_junits([first, tmp_path / "never.xml", empty], out) == 1
    assert suite_run.merge_junits([tmp_path / "never.xml"], out) == 0
    assert ET.parse(out).getroot().find("testsuite").attrib["tests"] == "0"


def test_the_counts_are_those_of_the_cases_and_not_of_the_headers(tmp_path):
    path, out = tmp_path / "a.xml", tmp_path / "m.xml"
    _write(path, [_case("tests.test_a_e2e", "test_x")])
    root = ET.parse(path).getroot()
    root.find("testsuite").attrib.update(tests="99", failures="7")
    ET.ElementTree(root).write(path)
    suite_run.merge_junits([path], out)
    attrs = ET.parse(out).getroot().find("testsuite").attrib
    assert (attrs["tests"], attrs["failures"]) == ("1", "0")


def test_a_run_with_a_red_test_still_hands_the_junit_to_the_judge_and_exits_zero(tmp_path):
    seen, out = [], tmp_path / "result" / "s.xml"
    run = _fake_run(1, [[_case("tests.test_a_e2e", "test_x"), _case("tests.test_c_e2e", "test_z", "failure")], [_case("tests.test_c_e2e", "test_clock")]], seen)
    code = suite_run.run(_required(tmp_path), ROW, "linux", "/ff/firefox", str(out), repo=tmp_path, run_e2e=run)
    assert code == 0
    assert seen[0]["argv"] == ["/ff/firefox"] + TESTS and seen[0]["repo"] == tmp_path
    assert [o for _n, o in suite_run.ci_verdict.parse_junit(out)[0]] == ["passed", "failed", "passed"]


def test_the_number_of_browsers_is_passed_to_the_run_only_when_asked(tmp_path):
    seen = []
    suite_run.run(_required(tmp_path), ROW, "linux", "ff", str(tmp_path / "o.xml"), repo=tmp_path, procs="2", run_e2e=_fake_run(0, [[_case("tests.t", "a")], None], seen))
    assert seen[0]["argv"][:3] == ["ff", "-n", "2"] and seen[0]["argv"][3:] == TESTS


def test_a_pass_that_did_not_run_to_its_end_is_not_zero(tmp_path):
    seen, out = [], tmp_path / "o.xml"
    code = suite_run.run(_required(tmp_path), ROW, "linux", "ff", str(out), repo=tmp_path, run_e2e=_fake_run(2, [[_case("tests.t", "a")], None], seen))
    assert code == 2
    assert out.is_file(), "what the passes wrote is still handed to the judge, which says the suite is short"


def test_a_run_that_wrote_no_junit_is_not_zero(tmp_path):
    out = tmp_path / "o.xml"
    assert suite_run.run(_required(tmp_path), ROW, "linux", "ff", str(out), repo=tmp_path, run_e2e=_fake_run(0, [None, None], [])) == 3
    assert suite_run.run(_required(tmp_path), ROW, "linux", "ff", str(out), repo=tmp_path, run_e2e=_fake_run(5, [None, None], [])) == 3


def test_the_directory_of_the_junit_is_set_for_the_run_and_put_back_after_it(tmp_path, monkeypatch):
    monkeypatch.delenv(core_e2e.JUNIT_DIR_ENV, raising=False)
    seen = []
    suite_run.run(_required(tmp_path), ROW, "linux", "ff", str(tmp_path / "o.xml"), repo=tmp_path, run_e2e=_fake_run(0, [[_case("tests.t", "a")], None], seen))
    assert seen[0]["dir"] and core_e2e.JUNIT_DIR_ENV not in suite_run.os.environ
    monkeypatch.setenv(core_e2e.JUNIT_DIR_ENV, "somewhere")
    suite_run.run(_required(tmp_path), ROW, "linux", "ff", str(tmp_path / "o.xml"), repo=tmp_path, run_e2e=_fake_run(0, [[_case("tests.t", "a")], None], seen))
    assert suite_run.os.environ[core_e2e.JUNIT_DIR_ENV] == "somewhere"


def test_the_passes_of_the_run_are_the_core_s_and_the_junit_names_are_its_own():
    names = [p.name for p in suite_run.pass_junits(Path("d"))]
    assert names == ["e2e-junit-e2e-and-not-wall_clock.xml", "e2e-junit-e2e-and-wall_clock.xml"], names
    assert [p.name for p in suite_run.pass_junits(Path("d"), "1")] == ["e2e-junit-e2e.xml"]
