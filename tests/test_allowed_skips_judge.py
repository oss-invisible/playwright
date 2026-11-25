"""scripts/ci_skips.py is the judge of a pytest leg (rule ci-13: nothing counts as passed that did not pass): it must be red on
every failure, error, unlisted skip (an xfail is one), testcase skipped with type "pytest.rerun" (even a listed one), invalid list or
row (not an array, malformed, no reason, bad os or python, test file gone) and shortfall against the floor of tests that passed (a
skipped test is not one), and only then; its `report` mode only prints and never fails. The junit files and the test files here are
written by hand in tmp_path, which is passed as --root (the folder that row files and classnames resolve against); the last test
reads the real ci/allowed-skips.json of the repository."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("ci_skips_under_test", ROOT / "scripts" / "ci_skips.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


skips = _load()

RULE = "Rule: ci-13 nothing counts as passed that did not pass"
FILE_A = "tests/test_a.py"
FILE_B = "tests/test_b.py"


@pytest.fixture(autouse=True)
def repo(tmp_path):
    """A fake repository root with two test files and a module that is not named test_*."""
    (tmp_path / "tests").mkdir()
    for f in (FILE_A, FILE_B, "tests/checks.py"):
        (tmp_path / f).write_text("def test_x():\n    pass\n", encoding="utf-8")
    return tmp_path


def _case(file, name, tag=None, cls=None, skip_type="pytest.skip", message="s"):
    classname = file[:-3].replace("/", ".") + ("." + cls if cls else "")
    inner = {None: "",
             "skipped": '<skipped type="%s" message="%s"/>' % (skip_type, message),
             "failure": '<failure message="f">x</failure>',
             "error": '<error message="e">x</error>'}[tag]
    return '<testcase classname="%s" name="%s" time="0.1">%s</testcase>' % (classname, name, inner)


def _junit(path, cases):
    Path(path).write_text('<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" tests="%d">%s'
                          "</testsuite></testsuites>" % (len(cases), "".join(cases)), encoding="utf-8")
    return str(path)


def _list(path, rows):
    Path(path).write_text(json.dumps(rows), encoding="utf-8")
    return str(path)


def _row(node=FILE_A, os="linux", python="3.11", reason="needs a hidden desktop that exists only on Windows"):
    return {"node": node, "os": os, "python": python, "reason": reason}


GREEN = [_case(FILE_A, "test_x"), _case(FILE_A, "test_y[p1]"), _case(FILE_B, "test_z")]


def _run(tmp_path, capsys, cases, rows, *, os="linux", python="3.11", floor=3, mode="judge", extra=None):
    """Run the command on a junit of `cases` and a list of `rows`; --root is tmp_path unless `extra` says otherwise."""
    junit = _junit(tmp_path / "j.xml", cases)
    allowed = _list(tmp_path / "allowed.json", rows)
    argv = [mode, "--junit", junit, "--allowed", allowed, "--os", os, "--python", python, "--min-tests", str(floor),
            *(["--root", str(tmp_path)] if extra is None else extra)]
    code = skips.main(argv)
    return code, capsys.readouterr().out


def _every_line_has_the_form(out):
    assert out.strip()
    for line in out.splitlines():
        assert line.startswith("FAIL ") and ": " in line and ". Fix: " in line and line.endswith(". " + RULE), line


def test_all_passed_is_green_with_one_ok_line(tmp_path, capsys):
    code, out = _run(tmp_path, capsys, GREEN, [])
    assert code == 0 and out.startswith("OK ") and len(out.splitlines()) == 1 and "FAIL" not in out


def test_an_unlisted_skip_is_red_and_the_line_has_the_form(tmp_path, capsys):
    code, out = _run(tmp_path, capsys, [*GREEN, _case(FILE_B, "test_w", "skipped", message="no display")], [])
    assert code == 1
    assert out.startswith("FAIL tests/test_b.py::test_w: ") and "skipped (no display)" in out
    _every_line_has_the_form(out)


def test_an_xfail_is_a_skip_and_is_judged_like_one(tmp_path, capsys):
    xfail = _case(FILE_B, "test_w", "skipped", skip_type="pytest.xfail", message="known bug")
    code, out = _run(tmp_path, capsys, [*GREEN, xfail], [])
    assert code == 1 and "tests/test_b.py::test_w" in out and "known bug" in out
    assert _run(tmp_path, capsys, [*GREEN, xfail], [_row(FILE_B)])[0] == 0


def test_a_listed_skip_is_ok_for_its_os_and_python(tmp_path, capsys):
    cases = [*GREEN, _case(FILE_B, "test_w", "skipped")]
    code, out = _run(tmp_path, capsys, cases, [_row(FILE_B)])
    assert code == 0, out
    assert _run(tmp_path, capsys, cases, [_row(FILE_B + "::test_w")])[0] == 0


def test_a_listed_node_id_covers_its_parametrized_testcases_only(tmp_path, capsys):
    cases = [*GREEN, _case(FILE_B, "test_w[a]", "skipped"), _case(FILE_B, "test_w[b]", "skipped")]
    assert _run(tmp_path, capsys, cases, [_row(FILE_B + "::test_w")])[0] == 0
    cases = [*GREEN, _case(FILE_B, "test_w2", "skipped")]
    code, out = _run(tmp_path, capsys, cases, [_row(FILE_B + "::test_w")])
    assert code == 1 and "test_w2" in out


def test_a_class_node_id_is_matched(tmp_path, capsys):
    cases = [*GREEN, _case(FILE_B, "test_w", "skipped", cls="TestK")]
    assert _run(tmp_path, capsys, cases, [_row(FILE_B + "::TestK::test_w")])[0] == 0
    assert _run(tmp_path, capsys, cases, [_row(FILE_B + "::TestK")])[0] == 0
    assert _run(tmp_path, capsys, cases, [_row(FILE_B + "::TestOther")])[0] == 1


def test_a_row_for_another_test_os_or_python_does_not_excuse_the_skip(tmp_path, capsys):
    cases = [*GREEN, _case(FILE_B, "test_w", "skipped")]
    assert _run(tmp_path, capsys, cases, [_row(FILE_A)])[0] == 1
    assert _run(tmp_path, capsys, cases, [_row(FILE_B, os="windows")], os="linux")[0] == 1
    assert _run(tmp_path, capsys, cases, [_row(FILE_B, python="3.12")], python="3.11")[0] == 1
    assert _run(tmp_path, capsys, cases, [_row(FILE_B, os=None, python=None)], os="linux", python="3.14")[0] == 0
    assert _run(tmp_path, capsys, cases, [_row(FILE_B, os=None, python=None)], os="windows", python="3.9")[0] == 0


def test_a_listed_row_without_a_reason_is_red(tmp_path, capsys):
    cases = [*GREEN, _case(FILE_B, "test_w", "skipped")]
    for reason in ("", "   ", None):
        code, out = _run(tmp_path, capsys, cases, [_row(FILE_B, reason=reason)])
        assert code == 1 and "no reason" in out, out
        _every_line_has_the_form(out)


def test_a_row_without_a_reason_is_red_even_when_nothing_is_skipped(tmp_path, capsys):
    code, out = _run(tmp_path, capsys, GREEN, [_row(FILE_B, reason="")])
    assert code == 1 and "no reason" in out
    row = _row(FILE_B)
    del row["reason"]
    code, out = _run(tmp_path, capsys, GREEN, [row])
    assert code == 1 and "exactly the keys" in out


def test_a_row_whose_test_file_does_not_exist_is_red(tmp_path, capsys):
    code, out = _run(tmp_path, capsys, GREEN, [_row("tests/test_deleted.py::test_x")])
    assert code == 1 and "tests/test_deleted.py does not exist" in out
    _every_line_has_the_form(out)


def test_a_row_with_a_bad_os_is_red(tmp_path, capsys):
    code, out = _run(tmp_path, capsys, GREEN, [_row(os="macos")])
    assert code == 1 and "macos" in out
    _every_line_has_the_form(out)


@pytest.mark.parametrize("python", ["three", "3", "3.11.2", "3.x", 3.11, 3, ""])
def test_a_row_with_a_bad_python_is_red(tmp_path, capsys, python):
    code, out = _run(tmp_path, capsys, GREEN, [_row(python=python)])
    assert code == 1 and "python is" in out
    _every_line_has_the_form(out)


def test_a_row_with_a_good_python_is_green(tmp_path, capsys):
    for python in ("3.11", "3.9", "3.14", None):
        assert _run(tmp_path, capsys, GREEN, [_row(python=python)])[0] == 0, python


def test_a_malformed_list_is_red(tmp_path, capsys):
    for rows in ({"node": FILE_A}, "text", [{"node": FILE_A}], [[]], [_row(node="")], [_row(node=None)],
                 [{**_row(), "extra": 1}]):
        code, out = _run(tmp_path, capsys, GREEN, rows)
        assert code == 1 and out.strip(), rows
        _every_line_has_the_form(out)


def test_a_list_that_is_not_an_array_says_so(tmp_path, capsys):
    code, out = _run(tmp_path, capsys, GREEN, {})
    assert code == 1 and "not a json array" in out


def test_a_list_that_is_not_json_or_is_absent_is_red(tmp_path, capsys):
    (tmp_path / "allowed.json").write_text("not json", encoding="utf-8")
    argv = ["judge", "--junit", _junit(tmp_path / "j.xml", GREEN), "--allowed", str(tmp_path / "allowed.json"),
            "--os", "linux", "--python", "3.11", "--min-tests", "3", "--root", str(tmp_path)]
    assert skips.main(argv) == 1
    argv[argv.index("--allowed") + 1] = str(tmp_path / "absent.json")
    assert skips.main(argv) == 1
    assert "allowed skips is missing" in capsys.readouterr().out


def test_fewer_tests_than_the_floor_is_red(tmp_path, capsys):
    code, out = _run(tmp_path, capsys, GREEN, [], floor=4)
    assert code == 1 and "3 tests passed, the floor is 4" in out
    _every_line_has_the_form(out)
    assert _run(tmp_path, capsys, GREEN, [], floor=3)[0] == 0
    assert _run(tmp_path, capsys, [], [], floor=1)[0] == 1


def test_a_skipped_test_does_not_count_for_the_floor(tmp_path, capsys):
    cases = [*GREEN, _case(FILE_B, "test_w", "skipped")]
    code, out = _run(tmp_path, capsys, cases, [_row(FILE_B)], floor=4)
    assert code == 1 and "3 tests passed, the floor is 4" in out
    assert _run(tmp_path, capsys, cases, [_row(FILE_B)], floor=3)[0] == 0


def test_the_floor_counts_every_junit_file(tmp_path, capsys):
    one = _junit(tmp_path / "one.xml", GREEN[:2])
    two = _junit(tmp_path / "two.xml", GREEN[2:])
    allowed = _list(tmp_path / "allowed.json", [])
    argv = ["judge", "--junit", one, "--junit", two, "--allowed", allowed, "--os", "linux", "--python", "3.11",
            "--min-tests", "3", "--root", str(tmp_path)]
    assert skips.main(argv) == 0
    argv[argv.index("--min-tests") + 1] = "4"
    assert skips.main(argv) == 1
    capsys.readouterr()


def test_a_failure_is_red(tmp_path, capsys):
    code, out = _run(tmp_path, capsys, [*GREEN, _case(FILE_A, "test_bad", "failure")], [])
    assert code == 1 and "tests/test_a.py::test_bad: the test failed or errored" in out
    _every_line_has_the_form(out)


def test_an_error_is_red(tmp_path, capsys):
    code, out = _run(tmp_path, capsys, [*GREEN, _case(FILE_A, "test_bad", "error")], [])
    assert code == 1 and "tests/test_a.py::test_bad: the test failed or errored" in out


def test_a_failure_is_red_even_when_the_node_is_listed(tmp_path, capsys):
    code, _ = _run(tmp_path, capsys, [*GREEN, _case(FILE_A, "test_bad", "failure")], [_row(FILE_A, os=None, python=None)])
    assert code == 1


def test_a_pytest_rerun_testcase_is_red(tmp_path, capsys):
    cases = [*GREEN, _case(FILE_B, "test_flaky", "skipped", skip_type="pytest.rerun", message="rerun")]
    code, out = _run(tmp_path, capsys, cases, [])
    assert code == 1 and "pytest.rerun" in out and out.startswith("FAIL tests/test_b.py::test_flaky: ")
    assert len(out.splitlines()) == 1
    _every_line_has_the_form(out)


def test_a_pytest_rerun_testcase_is_red_even_when_the_node_is_listed(tmp_path, capsys):
    cases = [*GREEN, _case(FILE_B, "test_flaky", "skipped", skip_type="pytest.rerun", message="rerun")]
    code, out = _run(tmp_path, capsys, cases, [_row(FILE_B, os=None, python=None)])
    assert code == 1 and "pytest.rerun" in out and len(out.splitlines()) == 1


def test_a_missing_or_unreadable_junit_is_red(tmp_path, capsys):
    allowed = _list(tmp_path / "allowed.json", [])
    base = ["judge", "--allowed", allowed, "--os", "linux", "--python", "3.11", "--min-tests", "1", "--root", str(tmp_path)]
    assert skips.main([*base, "--junit", str(tmp_path / "absent.xml")]) == 1
    assert "the junit is missing or not readable" in capsys.readouterr().out
    (tmp_path / "bad.xml").write_text("<testsuites>", encoding="utf-8")
    assert skips.main([*base, "--junit", str(tmp_path / "bad.xml")]) == 1
    assert "the junit is missing or not readable" in capsys.readouterr().out
    good = _junit(tmp_path / "good.xml", GREEN)
    assert skips.main([*base, "--junit", good, "--junit", str(tmp_path / "absent.xml")]) == 1
    capsys.readouterr()


def test_the_file_attribute_of_a_junit_is_used_first(tmp_path, capsys):
    case = '<testcase classname="x" name="test_w" file="tests\\test_b.py"><skipped type="pytest.skip" message="s"/></testcase>'
    code, _ = _run(tmp_path, capsys, [*GREEN, case], [_row(FILE_B)])
    assert code == 0
    code, out = _run(tmp_path, capsys, [*GREEN, case], [_row(FILE_A)])
    assert code == 1 and "tests/test_b.py::test_w" in out


def test_a_classname_resolves_to_the_longest_prefix_that_is_a_file_under_root(tmp_path, capsys):
    # tests/checks.py is not named test_*: only the lookup under --root finds it
    (tmp_path / "tests" / "sub").mkdir()
    (tmp_path / "tests" / "sub" / "test_c.py").write_text("", encoding="utf-8")
    cases = [*GREEN,
             '<testcase classname="tests.checks.TestK" name="test_w"><skipped type="pytest.skip" message="s"/></testcase>',
             '<testcase classname="tests.sub.test_c" name="test_v"><skipped type="pytest.skip" message="s"/></testcase>']
    code, out = _run(tmp_path, capsys, cases, [_row("tests/checks.py::TestK"), _row("tests/sub/test_c.py")])
    assert code == 0, out
    code, out = _run(tmp_path, capsys, cases, [_row("tests/sub/test_c.py")])
    assert code == 1 and "tests/checks.py::TestK::test_w" in out


def test_a_classname_of_a_file_that_is_gone_goes_up_to_the_first_test_module(tmp_path, capsys):
    case = '<testcase classname="tests.test_gone.TestK" name="test_w"><skipped type="pytest.skip" message="s"/></testcase>'
    code, out = _run(tmp_path, capsys, [*GREEN, case], [])
    assert code == 1 and "tests/test_gone.py::TestK::test_w" in out


def test_root_is_where_row_files_and_classnames_resolve(tmp_path, capsys, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    cases = [*GREEN, _case(FILE_B, "test_w", "skipped")]
    assert _run(tmp_path, capsys, cases, [_row(FILE_B)], extra=["--root", str(tmp_path)])[0] == 0
    code, out = _run(tmp_path, capsys, cases, [_row(FILE_B)], extra=["--root", str(elsewhere)])
    assert code == 1 and "tests/test_b.py does not exist" in out


def test_root_defaults_to_the_working_directory(tmp_path, capsys, monkeypatch):
    cases = [*GREEN, _case(FILE_B, "test_w", "skipped")]
    monkeypatch.chdir(tmp_path)
    assert _run(tmp_path, capsys, cases, [_row(FILE_B)], extra=[])[0] == 0
    monkeypatch.chdir(tmp_path / "tests")
    code, out = _run(tmp_path, capsys, cases, [_row(FILE_B)], extra=[])
    assert code == 1 and "does not exist" in out


def test_every_problem_is_printed_not_only_the_first(tmp_path, capsys):
    cases = [_case(FILE_A, "test_bad", "failure"), _case(FILE_B, "test_w", "skipped")]
    code, out = _run(tmp_path, capsys, cases, [_row(FILE_B, reason="")], floor=5)
    assert code == 1 and len(out.splitlines()) >= 4
    _every_line_has_the_form(out)


def test_wrong_usage_exits_2(tmp_path, capsys):
    junit = _junit(tmp_path / "j.xml", GREEN)
    allowed = _list(tmp_path / "allowed.json", [])
    good = ["--junit", junit, "--allowed", allowed, "--os", "linux", "--python", "3.11", "--min-tests", "3"]
    assert skips.main(["judge", *good, "--root", str(tmp_path)]) == 0
    capsys.readouterr()

    def exits(argv):
        with pytest.raises(SystemExit) as info:
            skips.main(argv)
        capsys.readouterr()
        return info.value.code

    assert exits([]) == 2
    assert exits(["judge"]) == 2
    for flag in ("--junit", "--allowed", "--os", "--python", "--min-tests"):
        i = good.index(flag)
        assert exits(["judge", *good[:i], *good[i + 2:]]) == 2, flag
    assert exits(["judge", *good[:-4], "--python", "three", "--min-tests", "3"]) == 2
    assert exits(["judge", *good[:-6], "--os", "macos", "--python", "3.11", "--min-tests", "3"]) == 2
    assert exits(["judge", *good[:-2], "--min-tests", "0"]) == 2


def test_report_prints_only_the_unlisted_skips_and_never_fails(tmp_path, capsys):
    cases = [_case(FILE_A, "test_bad", "failure"), _case(FILE_B, "test_w", "skipped", message="no display"),
             _case(FILE_B, "test_v", "skipped"), _case(FILE_B, "test_r", "skipped", skip_type="pytest.rerun")]
    code, out = _run(tmp_path, capsys, cases, [_row(FILE_B + "::test_v")], mode="report", floor=999)
    assert code == 0
    assert out.splitlines() == ["SKIP tests/test_b.py::test_w: no display"]


def test_report_options_are_all_optional_and_root_is_honoured(tmp_path, capsys, monkeypatch):
    assert skips.main(["report"]) == 0
    assert capsys.readouterr().out == ""
    junit = _junit(tmp_path / "j.xml", [_case("tests/checks.py", "test_w", "skipped", cls="TestK", message="m")])
    monkeypatch.chdir(tmp_path / "tests")
    assert skips.main(["report", "--junit", junit, "--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.splitlines() == ["SKIP tests/checks.py::TestK::test_w: m"]
    assert skips.main(["report", "--junit", junit, "--os", "linux", "--python", "3.11"]) == 0
    capsys.readouterr()


def test_report_never_fails_on_a_bad_list_or_a_missing_junit(tmp_path, capsys):
    allowed = _list(tmp_path / "allowed.json", [_row(reason="")])
    assert skips.main(["report", "--junit", str(tmp_path / "absent.xml"), "--allowed", allowed, "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out and "NOTE" in out
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    assert skips.main(["report", "--junit", _junit(tmp_path / "j.xml", GREEN), "--allowed", str(tmp_path / "bad.json")]) == 0
    assert "FAIL" not in capsys.readouterr().out
    assert skips.main(["report", "--junit", _junit(tmp_path / "j.xml", GREEN)]) == 0
    assert capsys.readouterr().out == ""


def test_the_real_list_is_valid_and_every_row_has_its_reason_and_file(tmp_path, capsys):
    real = ROOT / "ci" / "allowed-skips.json"
    rows = json.loads(real.read_text(encoding="utf-8"))
    assert isinstance(rows, list)
    for row in rows:
        assert row["reason"].strip() and (ROOT / row["node"].split("::", 1)[0]).is_file()
    argv =["judge", "--junit", _junit(tmp_path / "j.xml", [_case(FILE_A, "test_x")]), "--allowed", str(real),
            "--os", "linux", "--python", "3.11", "--min-tests", "1", "--root", str(ROOT)]
    assert skips.main(argv) == 0, capsys.readouterr().out
    capsys.readouterr()
