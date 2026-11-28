"""scripts/ci_verdict.py is the one judge of the mandatory suites: it must fail on every hole (a skip, a failure, too few
tests collected, a required file that never ran, a list that lost a row or a minimum against main) and only then. The
junit files here are written by hand in tmp_path; the last tests read the real ci/required-suites.json."""
import importlib.util
import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ROOT / "ci" / "required-suites.json"


def _load():
    spec = importlib.util.spec_from_file_location("ci_verdict_under_test", ROOT / "scripts" / "ci_verdict.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verdict = _load()

FILE_A = "tests/test_a.py"
FILE_B = "tests/test_b.py"
NODE = "tests/test_c.py::test_one"


def _suite(**over):
    row = {"id": "s.one", "giro": "G2", "os": ["linux", "windows"], "min_tests": 4,
           "tests": [FILE_A, FILE_B, NODE], "marker": "unit", "needs_root": False, "exact": True,
           "note": ""}
    row.update(over)
    return row


def _write_required(path, rows, fragments=None, consumed=None):
    """The list, and the folder of its fragments next to it (it always exists: it holds consumed.json). `fragments` is {slug: content}."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[\n" + ",\n".join("  " + json.dumps(r) for r in rows) + "\n]\n", encoding="utf-8")
    folder = path.with_name("required-suites.d")
    folder.mkdir(exist_ok=True)
    (folder / "consumed.json").write_text(json.dumps(consumed or []), encoding="utf-8")
    for slug, content in (fragments or {}).items():
        (folder / (slug + ".json")).write_text(json.dumps(content), encoding="utf-8")
    return path


def _case(file, name, tag=None, classname=None):
    classname = file[:-3].replace("/", ".") if classname is None else classname
    inner = {None: "", "skipped": '<skipped message="s"/>', "failure": '<failure message="f">x</failure>',
             "error": '<error message="e">x</error>'}[tag]
    return '<testcase classname="%s" name="%s" time="0.1">%s</testcase>' % (classname, name, inner)


def _junit(path, cases):
    path.write_text('<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" tests="%d">%s'
                    "</testsuite></testsuites>" % (len(cases), "".join(cases)), encoding="utf-8")
    return path


GREEN = [_case(FILE_A, "test_x"), _case(FILE_A, "test_y[p1]"), _case(FILE_B, "test_z"), _case("tests/test_c.py", "test_one")]


SHA = "a" * 40


def _judge(tmp_path, cases, rows=None, extra=(), suite="s.one", os_name="linux", with_binary=True, commit=SHA, fragments=None, consumed=None):
    required = _write_required(tmp_path / "required.json", rows or [_suite()], fragments, consumed)
    junit = _junit(tmp_path / "junit.xml", cases) if cases is not None else tmp_path / "absent.xml"
    binary = tmp_path / "firefox.bin"
    binary.write_bytes(b"engine")
    out = tmp_path / "result" / ("%s.%s.json" % (suite, os_name))
    argv = ["judge", "--required", str(required), "--suite", suite, "--os", os_name, "--junit", str(junit),
            "--out", str(out), *extra]
    if with_binary:
        argv += ["--binary", str(binary)]
    if commit:
        argv += ["--commit", commit]
    code = verdict.main(argv)
    return code, (json.loads(out.read_text(encoding="utf-8")) if out.exists() else None)


def test_everything_green_exits_0_and_writes_the_result(tmp_path, capsys):
    code, result = _judge(tmp_path, GREEN)
    assert code == 0, capsys.readouterr().out
    assert result["suite"] == "s.one" and result["os"] == "linux" and result["commit"] == SHA
    assert result["verdict"] == "green"
    assert (result["collected"], result["passed"], result["failed"], result["skipped"], result["errors"]) == (4, 4, 0, 0, 0)
    assert result["missing_tests"] == [] and result["problems"] == []


def test_the_binary_hash_is_the_sha256_of_the_file(tmp_path):
    import hashlib
    _code, result = _judge(tmp_path, GREEN)
    assert result["binary_sha256"] == hashlib.sha256(b"engine").hexdigest()


def test_judge_without_binary_is_a_wrong_use(tmp_path, capsys):
    code, result = _judge(tmp_path, GREEN, with_binary=False)
    assert code == 2 and result is None
    assert "--binary" in capsys.readouterr().err


def test_a_binary_that_does_not_exist_is_red_never_a_null_hash_green(tmp_path, capsys):
    required = _write_required(tmp_path / "required.json", [_suite()])
    junit = _junit(tmp_path / "junit.xml", GREEN)
    out = tmp_path / "r.json"
    code = verdict.main(["judge", "--required", str(required), "--suite", "s.one", "--os", "linux", "--junit", str(junit),
                         "--binary", str(tmp_path / "nowhere.bin"), "--commit", SHA, "--out", str(out)])
    result = json.loads(out.read_text(encoding="utf-8"))
    assert code == 1 and result["binary_sha256"] is None and result["verdict"] == "red"
    assert "does not exist" in capsys.readouterr().out


def test_a_skipped_test_is_red(tmp_path, capsys):
    cases = GREEN[:-1] + [_case("tests/test_c.py", "test_one", "skipped")]
    code, result = _judge(tmp_path, cases)
    out = capsys.readouterr().out
    assert code == 1 and result["skipped"] == 1
    assert "skipped" in out and NODE in out


def test_a_failed_test_is_red(tmp_path, capsys):
    cases = GREEN + [_case(FILE_B, "test_bad", "failure")]
    code, result = _judge(tmp_path, cases)
    out = capsys.readouterr().out
    assert code == 1 and result["failed"] == 1
    assert "failed" in out and FILE_B in out


def test_an_error_is_red(tmp_path):
    cases = GREEN + [_case(FILE_B, "test_bad", "error")]
    code, result = _judge(tmp_path, cases)
    assert code == 1 and result["errors"] == 1


def test_fewer_tests_than_the_minimum_is_red(tmp_path, capsys):
    code, result = _judge(tmp_path, GREEN, rows=[_suite(min_tests=5)])
    assert code == 1 and result["collected"] == 4
    assert "fewer than the minimum 5" in capsys.readouterr().out


def test_a_required_file_that_did_not_run_is_red(tmp_path, capsys):
    cases = [c for c in GREEN if "test_b" not in c]
    code, result = _judge(tmp_path, cases, rows=[_suite(min_tests=3)])
    assert code == 1 and result["missing_tests"] == [FILE_B]
    assert FILE_B in capsys.readouterr().out


def test_a_required_node_id_that_did_not_run_is_red(tmp_path):
    cases = [c for c in GREEN if "test_one" not in c]
    code, result = _judge(tmp_path, cases, rows=[_suite(min_tests=3)])
    assert code == 1 and result["missing_tests"] == [NODE]


def test_a_parametrized_node_id_is_matched_without_its_parameters(tmp_path):
    cases = [_case(FILE_A, "test_x"), _case("tests/test_c.py", "test_one[a]"), _case("tests/test_c.py", "test_one[b]")]
    rows = [_suite(tests=[FILE_A, NODE], min_tests=3)]
    assert _judge(tmp_path, cases, rows=rows)[0] == 0
    cases[2] = _case("tests/test_c.py", "test_one[b]", "failure")
    assert _judge(tmp_path, cases, rows=rows)[0] == 1


def test_a_missing_junit_is_red(tmp_path, capsys):
    code, result = _judge(tmp_path, None)
    assert code == 1 and result["collected"] == 0
    assert "junit" in capsys.readouterr().out


def test_every_problem_is_printed_one_per_line(tmp_path, capsys):
    cases = [_case(FILE_A, "test_x", "failure"), _case(FILE_A, "test_y", "skipped")]
    code, _result = _judge(tmp_path, cases)
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith("VERDICT RED")]
    assert code == 1 and len(lines) >= 5, lines


def test_a_row_the_suite_does_not_have_for_that_os_is_red(tmp_path, capsys):
    code, _result = _judge(tmp_path, GREEN, rows=[_suite(os=["linux"])], os_name="windows")
    assert code == 1 and "does not run on windows" in capsys.readouterr().out


def test_a_class_in_the_junit_is_part_of_the_node_id():
    assert verdict.node_id("tests.test_x.TestThing", "test_y") == "tests/test_x.py::TestThing::test_y"
    assert verdict.node_id("tests.test_x", "test_y[p]") == "tests/test_x.py::test_y[p]"


# --- against main ------------------------------------------------------------------------------------------------
def _main_file(tmp_path, rows, fragments=None, consumed=None):
    """main's list in a folder of its own: its fragments are the folder next to IT."""
    return _write_required(tmp_path / "mainside" / "main.json", rows, fragments, consumed)


def test_a_list_that_only_grew_is_green(tmp_path):
    main = _main_file(tmp_path, [_suite(min_tests=3, tests=[FILE_A])])
    code, _r = _judge(tmp_path, GREEN, rows=[_suite(), _suite(id="s.two")], extra=["--main-required", str(main)])
    assert code == 0


def test_a_suite_of_main_that_is_gone_is_red(tmp_path, capsys):
    main = _main_file(tmp_path, [_suite(), _suite(id="s.gone")])
    code, _r = _judge(tmp_path, GREEN, extra=["--main-required", str(main)])
    assert code == 1 and "s.gone" in capsys.readouterr().out


def test_a_lost_test_row_against_main_is_red(tmp_path, capsys):
    main = _main_file(tmp_path, [_suite(tests=[FILE_A, FILE_B, NODE, "tests/test_d.py"])])
    code, _r = _judge(tmp_path, GREEN, extra=["--main-required", str(main)])
    assert code == 1 and "tests/test_d.py" in capsys.readouterr().out


def test_a_higher_minimum_on_main_is_red(tmp_path, capsys):
    main = _main_file(tmp_path, [_suite(min_tests=9)])
    code, _r = _judge(tmp_path, GREEN, extra=["--main-required", str(main)])
    assert code == 1 and "went down" in capsys.readouterr().out


def test_a_main_list_that_does_not_exist_is_red_unless_the_gate_declares_main_has_none(tmp_path, capsys):
    missing = str(tmp_path / "no" / "required.json")
    code, result = _judge(tmp_path, GREEN, extra=["--main-required", missing])
    assert code == 1 and result["verdict"] == "red"
    assert "it is not known what main has" in capsys.readouterr().out


def test_a_main_list_declared_absent_is_said_and_passed(tmp_path, capsys):
    missing = str(tmp_path / "no" / "required.json")
    code, _r = _judge(tmp_path, GREEN, extra=["--main-required", missing, "--main-has-no-list"])
    assert code == 0 and "main was checked out and has none" in capsys.readouterr().out


def test_declaring_main_has_none_while_the_file_is_there_is_red(tmp_path, capsys):
    main = _main_file(tmp_path, [_suite()])
    code, _r = _judge(tmp_path, GREEN, extra=["--main-required", str(main), "--main-has-no-list"])
    assert code == 1 and "says main has none" in capsys.readouterr().out


def test_main_has_no_list_without_a_main_path_is_red(tmp_path):
    assert _judge(tmp_path, GREEN, extra=["--main-has-no-list"])[0] == 1


def test_an_exact_turned_off_against_main_is_red(tmp_path, capsys):
    main = _main_file(tmp_path, [_suite(exact=True)])
    code, _r = _judge(tmp_path, GREEN, rows=[_suite(exact=False)], extra=["--main-required", str(main)])
    assert code == 1 and "no longer exact" in capsys.readouterr().out


def test_a_reduced_os_against_main_is_red(tmp_path, capsys):
    main = _main_file(tmp_path, [_suite()])
    code, _r = _judge(tmp_path, GREEN, rows=[_suite(os=["linux"])], extra=["--main-required", str(main)])
    assert code == 1 and "no longer runs on windows" in capsys.readouterr().out


def test_a_changed_marker_against_main_is_red(tmp_path, capsys):
    main = _main_file(tmp_path, [_suite(marker="e2e")])
    code, _r = _judge(tmp_path, GREEN, extra=["--main-required", str(main)])
    assert code == 1 and "marker of s.one changed" in capsys.readouterr().out


def test_a_main_list_older_than_the_format_is_still_compared(tmp_path):
    old = {k: v for k, v in _suite(min_tests=3, tests=[FILE_A]).items() if k != "exact"}
    main = _main_file(tmp_path, [old])
    assert _judge(tmp_path, GREEN, extra=["--main-required", str(main)])[0] == 0


# --- retired files: the one way a test leaves a row -----------------------------------------------------------
GONE = "tests/test_d.py"


def _main_with(tmp_path, rows, retired=None, fragments=None, consumed=None):
    """main's list in its own folder, and the retired list of main next to it."""
    folder = tmp_path / "main"
    folder.mkdir(parents=True)
    required = _write_required(folder / "required.json", rows, fragments, consumed)
    if retired is not None:
        (folder / verdict.RETIRED_NAME).write_text(json.dumps(retired), encoding="utf-8")
    return required


def _retired(file=GONE, n=1, row="s.one"):
    return {"file": file, "reason": "feature cancelled", "date": "2026-11-21", "tests": {row: n}}


def _main_that_had_d(tmp_path, retired, min_tests=5):
    return _main_with(tmp_path, [_suite(min_tests=min_tests, tests=[FILE_A, FILE_B, NODE, GONE])], retired)


def test_a_retired_file_that_is_gone_may_leave_its_row_with_its_tests_is_green(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main = _main_that_had_d(tmp_path, [_retired()])
    assert _judge(tmp_path, GREEN, extra=["--main-required", str(main)])[0] == 0


def test_a_file_that_main_did_not_retire_leaving_its_row_is_red(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main = _main_that_had_d(tmp_path, [_retired("tests/test_other.py")])
    code, _r = _judge(tmp_path, GREEN, extra=["--main-required", str(main)])
    assert code == 1 and GONE in capsys.readouterr().out


def test_a_main_with_no_retired_list_excuses_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main = _main_that_had_d(tmp_path, None)
    code, _r = _judge(tmp_path, GREEN, extra=["--main-required", str(main)])
    assert code == 1 and GONE in capsys.readouterr().out


def test_a_retired_file_that_is_still_on_disk_is_not_excused(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / GONE).write_text("def test_d(): pass", encoding="utf-8")
    main = _main_that_had_d(tmp_path, [_retired()])
    code, _r = _judge(tmp_path, GREEN, extra=["--main-required", str(main)])
    out = capsys.readouterr().out
    assert code == 1 and GONE in out and "went down" in out


def test_a_minimum_that_went_down_by_more_than_the_retired_tests_is_red(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main = _main_that_had_d(tmp_path, [_retired(n=1)], min_tests=6)  # the file had 1 test: 6 - 1 = 5, and the row says 4
    code, _r = _judge(tmp_path, GREEN, extra=["--main-required", str(main)])
    assert code == 1 and "went down" in capsys.readouterr().out


def test_the_tests_of_a_retired_file_are_counted_per_row(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main = _main_that_had_d(tmp_path, [_retired(row="s.another")])  # the file had its tests in another row: none here
    code, _r = _judge(tmp_path, GREEN, extra=["--main-required", str(main)])
    assert code == 1 and "went down" in capsys.readouterr().out


def test_a_retired_list_of_the_branch_excuses_nothing(tmp_path, monkeypatch, capsys):
    """The judge reads the retired list next to main's list: a branch that writes its own next to its own list gets nothing."""
    monkeypatch.chdir(tmp_path)
    main = _main_that_had_d(tmp_path, None)
    (tmp_path / verdict.RETIRED_NAME).write_text(json.dumps([_retired()]), encoding="utf-8")
    code, _r = _judge(tmp_path, GREEN, extra=["--main-required", str(main)])
    assert code == 1 and GONE in capsys.readouterr().out


def test_a_malformed_retired_list_of_main_is_red(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main = _main_that_had_d(tmp_path, [{"file": GONE}])
    code, _r = _judge(tmp_path, GREEN, extra=["--main-required", str(main)])
    assert code == 1 and "retired list of main" in capsys.readouterr().out


def test_a_retired_file_does_not_excuse_another_loss_in_the_same_row(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main = _main_with(tmp_path, [_suite(min_tests=6, tests=[FILE_A, FILE_B, NODE, GONE, "tests/test_e.py"])], [_retired()])
    code, _r = _judge(tmp_path, GREEN, extra=["--main-required", str(main)])
    assert code == 1 and "tests/test_e.py" in capsys.readouterr().out


# --- retired tests: a single test of a file that stays, declared by nodeid ----------------------------------------
GONE_NODE = FILE_A + "::test_gone"


_OMIT = object()


def _retired_test(nodeid=GONE_NODE, suite="s.one", count=1, from_min_tests=_OMIT, **over):
    """An entry of main's retired-tests.json for a single test. `from_min_tests` is optional (and ignored): left out unless a test passes it."""
    entry = {"nodeid": nodeid, "suite": suite, "count": count, "reason": "removed with its bundle", "decided_by": "pm", "date": "2026-11-27"}
    if from_min_tests is not _OMIT:
        entry["from_min_tests"] = from_min_tests
    entry.update(over)
    return entry


def _main_that_had_the_test(tmp_path, retired, min_tests=5, fragments=None, consumed=None):
    """main's row of FILE_A, FILE_B, NODE with one more test (the one that is retired) in the floor."""
    return _main_with(tmp_path, [_suite(min_tests=min_tests)], retired, fragments, consumed)


def _retiring(*nodeids, row="s.one", value=-1):
    """The fragment of the PR that retires tests: a negative value for the row, and the retirements it consumes."""
    return {"retire-it": {row: value, "consumes": list(nodeids)}}


def test_a_declared_test_lets_the_floor_drop_by_exactly_its_count_is_green(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main = _main_that_had_the_test(tmp_path, [_retired_test()])
    code, _r = _judge(tmp_path, GREEN, rows=[_suite(min_tests=5)], fragments=_retiring(GONE_NODE), extra=["--main-required", str(main)])
    assert code == 0  # 5 on main, 5 in the list, -1 of the fragment: 4 here


def test_the_counts_of_the_entries_of_a_suite_are_summed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    other, third = FILE_B + "::test_also_gone", FILE_A + "::t3"
    main = _main_that_had_the_test(tmp_path, [_retired_test(from_min_tests=7), _retired_test(other, from_min_tests=7),
                                              _retired_test(nodeid=third, from_min_tests=7)], min_tests=7)
    code, _r = _judge(tmp_path, GREEN, rows=[_suite(min_tests=7)], fragments=_retiring(GONE_NODE, other, third, value=-3),
                      extra=["--main-required", str(main)])
    assert code == 0  # 7 -> 4: three declared


def test_a_floor_that_drops_by_more_than_the_declared_count_is_red(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main = _main_that_had_the_test(tmp_path, [_retired_test(count=1)], min_tests=6)  # 6 -> 4 here: one declared, two lost
    code, _r = _judge(tmp_path, GREEN, rows=[_suite(min_tests=6)], fragments=_retiring(GONE_NODE, value=-2), extra=["--main-required", str(main)])
    out = capsys.readouterr().out
    assert code == 1 and "went down" in out and "account for 1" in out


def test_a_test_that_goes_with_no_entry_is_red(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main = _main_that_had_the_test(tmp_path, None)
    code, _r = _judge(tmp_path, GREEN, extra=["--main-required", str(main)])
    assert code == 1 and "went down" in capsys.readouterr().out


def test_an_entry_of_another_suite_or_of_a_file_the_row_does_not_run_excuses_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    for i, entry in enumerate([_retired_test(suite="s.another"), _retired_test(nodeid="tests/test_unlisted.py::test_gone")]):
        folder = tmp_path / ("case%d" % i)
        folder.mkdir()
        main = _main_that_had_the_test(folder, [entry])
        sub = tmp_path / ("run%d" % i)
        sub.mkdir()
        code, _r = _judge(sub, GREEN, rows=[_suite(min_tests=5)], fragments=_retiring(entry["nodeid"]), extra=["--main-required", str(main)])
        assert code == 1 and "went down" in capsys.readouterr().out, entry


def test_a_declared_test_that_the_suite_still_collects_is_red(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main = _main_that_had_the_test(tmp_path, [_retired_test()])
    still = GREEN + [_case(FILE_A, "test_gone")]
    code, _r = _judge(tmp_path, still, rows=[_suite(min_tests=6)], fragments=_retiring(GONE_NODE), extra=["--main-required", str(main)])
    out = capsys.readouterr().out
    assert code == 1 and GONE_NODE in out and "still collects it" in out


def test_a_declared_test_still_collected_with_a_parameter_is_red_and_excuses_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main = _main_that_had_the_test(tmp_path, [_retired_test()])
    still = GREEN[:3] + [_case(FILE_A, "test_gone[p1]")]  # 4 tests, the floor of 4 lowered by the entry
    code, _r = _judge(tmp_path, still, rows=[_suite(min_tests=5)], fragments=_retiring(GONE_NODE), extra=["--main-required", str(main)])
    out = capsys.readouterr().out
    assert code == 1 and "still collects it" in out and "went down" in out


def test_the_retired_tests_of_the_branch_excuse_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main = _main_that_had_the_test(tmp_path, None)
    (tmp_path / verdict.RETIRED_NAME).write_text(json.dumps([_retired_test()]), encoding="utf-8")
    code, _r = _judge(tmp_path, GREEN, rows=[_suite(min_tests=5)], fragments=_retiring(GONE_NODE), extra=["--main-required", str(main)])
    out = capsys.readouterr().out
    assert code == 1 and "not a retired test of main" in out and "went down" in out


@pytest.mark.parametrize("bad, why", [
    ({"nodeid": FILE_A}, "nodeid must be"),
    ({"nodeid": FILE_A + "::"}, "nodeid must be"),
    ({"nodeid": "::test_gone"}, "nodeid must be"),
    ({"nodeid": 3}, "nodeid must be"),
    ({"suite": "s.unknown"}, "not a row of main"),
    ({"suite": ""}, "suite must be"),
    ({"count": 0}, "count must be"),
    ({"from_min_tests": 0}, "from_min_tests must be"),
    ({"count": 3, "from_min_tests": 2}, "from_min_tests must be"),
    ({"from_min_tests": "5"}, "from_min_tests must be"),
    ({"from_min_tests": None}, "from_min_tests must be"),
    ({"count": True}, "count must be"),
    ({"count": "1"}, "count must be"),
    ({"reason": ""}, "reason must be"),
    ({"decided_by": ""}, "decided_by must be"),
    ({"decided_by": None}, "decided_by must be"),
    ({"date": ""}, "date must be"),
    ({"file": GONE, "tests": {"s.one": 1}}, "not both"),
])
def test_a_malformed_retired_test_of_main_is_red(tmp_path, monkeypatch, capsys, bad, why):
    monkeypatch.chdir(tmp_path)
    main = _main_that_had_the_test(tmp_path, [_retired_test(**bad)])
    code, _r = _judge(tmp_path, GREEN, extra=["--main-required", str(main)])
    out = capsys.readouterr().out
    assert code == 1 and "retired list of main" in out and why in out, out
    assert why in " ".join(verdict.validate_retired([_retired_test(**bad)], {"s.one"}))


def test_the_same_test_declared_twice_for_a_suite_is_red():
    assert any("more than once" in p for p in verdict.validate_retired([_retired_test(), _retired_test()], {"s.one"}))
    assert verdict.validate_retired([_retired_test(), _retired_test(suite="s.two")], {"s.one", "s.two"}) == []


def test_files_and_tests_may_share_the_list():
    assert verdict.validate_retired([_retired(), _retired_test()], {"s.one"}) == []


def test_an_old_entry_with_from_min_tests_and_a_new_one_without_it_are_both_valid_and_the_number_is_ignored():
    assert verdict.validate_retired([_retired_test(from_min_tests=5), _retired_test(FILE_B + "::t")], {"s.one"}) == []
    assert verdict.validate_retired([_retired_test(from_min_tests=5, nodeid=FILE_B + "::t", suite="s.two")], {"s.one", "s.two"}) == []


def test_a_retirement_is_used_whatever_the_floor_of_main_is_now(tmp_path, monkeypatch):
    """The old entry counted only while main's floor was still `from_min_tests`; any merge in between made it inert and the retiring PR went red.
    Now it is used by name: main's floor is 7 (others merged), the entry says 5, and the fragment of the retirement is green."""
    monkeypatch.chdir(tmp_path)
    main = _main_that_had_the_test(tmp_path, [_retired_test(from_min_tests=5)], min_tests=7)
    code, _r = _judge(tmp_path, GREEN + [_case(FILE_A, "test_a"), _case(FILE_A, "test_b")], rows=[_suite(min_tests=7)],
                      fragments=_retiring(GONE_NODE), extra=["--main-required", str(main)])
    assert code == 0  # 7 on main, -1 of the fragment, 6 collected


def test_an_entry_already_consumed_asks_nothing_of_a_suite_that_collects_the_test_again(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main = _main_that_had_the_test(tmp_path, [_retired_test()], min_tests=4, consumed=[GONE_NODE])
    again = GREEN + [_case(FILE_A, "test_gone")]
    assert _judge(tmp_path, again, rows=[_suite(min_tests=5)], consumed=[GONE_NODE], extra=["--main-required", str(main)])[0] == 0


def test_the_entries_of_one_suite_must_share_one_from_min_tests():
    both = [_retired_test(from_min_tests=5), _retired_test(FILE_B + "::t", from_min_tests=6)]
    assert any("disagree on from_min_tests" in p for p in verdict.validate_retired(both, {"s.one"}))
    assert verdict.validate_retired([_retired_test(from_min_tests=5), _retired_test(FILE_B + "::t", from_min_tests=5)], {"s.one"}) == []
    other = [_retired_test(from_min_tests=5), _retired_test(FILE_B + "::t", suite="s.two", from_min_tests=9)]
    assert verdict.validate_retired(other, {"s.one", "s.two"}) == []


# --- args --------------------------------------------------------------------------------------------------------
def test_args_prints_the_files_and_node_ids_one_per_line(tmp_path, capsys):
    required = _write_required(tmp_path / "required.json", [_suite()])
    assert verdict.main(["args", "--required", str(required), "--suite", "s.one", "--os", "linux"]) == 0
    assert capsys.readouterr().out.splitlines() == [FILE_A, FILE_B, NODE]


def test_args_with_the_marker_puts_it_first(tmp_path, capsys):
    required = _write_required(tmp_path / "required.json", [_suite(marker="e2e")])
    assert verdict.main(["args", "--required", str(required), "--suite", "s.one", "--os", "windows", "--with-marker"]) == 0
    assert capsys.readouterr().out.splitlines()[:2] == ["-m", "e2e"]


def test_args_for_windows_of_a_linux_only_suite_is_an_error(tmp_path, capsys):
    required = _write_required(tmp_path / "required.json", [_suite(os=["linux"])])
    assert verdict.main(["args", "--required", str(required), "--suite", "s.one", "--os", "windows"]) != 0
    captured = capsys.readouterr()
    assert captured.out == "" and "does not run on windows" in captured.err


# --- the commit: the wrapper's, never GITHUB_SHA -------------------------------------------------------------------
def test_the_commit_is_not_taken_from_github_sha(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_SHA", "e" * 40)  # in a called workflow this is the commit of the ENGINE's repository
    code, result = _judge(tmp_path, GREEN, commit="b" * 40)
    assert code == 0 and result["commit"] == "b" * 40
    monkeypatch.chdir(tmp_path)  # not a git checkout
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    code, result = _judge(tmp_path, GREEN, commit=None)
    assert code == 1 and result["commit"] is None
    assert "commit of the wrapper is not known" in capsys.readouterr().out


def test_without_commit_it_is_git_head_of_the_current_directory(tmp_path, monkeypatch):
    import subprocess
    repo = tmp_path / "wrapper"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=ci-verdict-test"]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "x"], check=True)
    head = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    monkeypatch.setenv("GITHUB_SHA", "e" * 40)
    monkeypatch.chdir(repo)
    code, result = _judge(tmp_path, GREEN, commit=None)
    assert code == 0 and result["commit"] == head


# --- the seal of the gate ------------------------------------------------------------------------------------------
def test_a_readable_seal_is_noted_as_having_no_binary_hash(tmp_path, capsys):
    seal = tmp_path / "gate.seal.json"
    seal.write_text('{"schema": 2, "tag": "t", "assets": {}, "build_id": "1"}', encoding="utf-8")
    code, result = _judge(tmp_path, GREEN, extra=["--seal", str(seal)])
    assert code == 0 and result["seal_binary_hash"] == "absent"
    assert "carries no sha256 of the engine binary" in capsys.readouterr().out


def test_a_seal_that_cannot_be_read_is_red(tmp_path, capsys):
    code, _r = _judge(tmp_path, GREEN, extra=["--seal", str(tmp_path / "no.seal.json")])
    assert code == 1 and "cannot be read" in capsys.readouterr().out


# --- the GeoIP database of the leg ---------------------------------------------------------------------------------
MMDB_SHA = "c" * 64


def _geoip(tmp_path, **evidence):
    pin = tmp_path / "geoip.json"
    pin.write_text(json.dumps({"mmdb_sha256": MMDB_SHA}), encoding="utf-8")
    proof = tmp_path / "geoip-evidence.json"
    base = {"env": "/db/geoip-aio-all.mmdb", "exists": True, "sha256": MMDB_SHA}
    base.update(evidence)
    proof.write_text(json.dumps(base), encoding="utf-8")
    return ["--geoip", str(proof), "--geoip-pin", str(pin)]


def test_a_leg_that_ran_with_the_database_of_the_pin_is_green(tmp_path):
    code, result = _judge(tmp_path, GREEN, extra=_geoip(tmp_path))
    assert code == 0 and result["geoip_sha256"] == MMDB_SHA


def test_a_leg_launched_without_the_variable_is_red(tmp_path, capsys):
    code, result = _judge(tmp_path, GREEN, extra=_geoip(tmp_path, env="", exists=False, sha256=None))
    out = capsys.readouterr().out
    assert code == 1 and result["verdict"] == "red" and "without STEALTHFOX_GEOIP_MMDB" in out


def test_a_variable_that_names_no_file_is_red(tmp_path, capsys):
    code, _r = _judge(tmp_path, GREEN, extra=_geoip(tmp_path, exists=False, sha256=None))
    assert code == 1 and "is not a file" in capsys.readouterr().out


def test_a_database_that_is_not_the_one_of_the_pin_is_red(tmp_path, capsys):
    code, _r = _judge(tmp_path, GREEN, extra=_geoip(tmp_path, sha256="d" * 64))
    assert code == 1 and "the pin" in capsys.readouterr().out


def test_evidence_that_is_missing_or_unreadable_is_red(tmp_path, capsys):
    code, _r = _judge(tmp_path, GREEN, extra=["--geoip", str(tmp_path / "none.json"), "--geoip-pin", str(tmp_path / "geoip.json")])
    assert code == 1 and "did not say which database" in capsys.readouterr().out


def test_a_pin_that_cannot_be_read_is_red(tmp_path, capsys):
    extra = _geoip(tmp_path)
    (tmp_path / "geoip.json").write_text("not json", encoding="utf-8")
    code, _r = _judge(tmp_path, GREEN, extra=extra)
    assert code == 1 and "pin of the GeoIP database" in capsys.readouterr().out


# --- exact, every test of a file, xunit2 ---------------------------------------------------------------------------
def test_an_exact_suite_with_more_tests_than_min_tests_is_red(tmp_path, capsys):
    cases = GREEN + [_case(FILE_B, "test_new")]
    code, result = _judge(tmp_path, cases)
    assert code == 1 and result["collected"] == 5
    assert "exact" in capsys.readouterr().out


def test_a_suite_that_is_not_exact_allows_more_tests(tmp_path):
    cases = GREEN + [_case(FILE_B, "test_new")]
    assert _judge(tmp_path, cases, rows=[_suite(exact=False)])[0] == 0


def test_a_deselected_test_is_caught_by_the_exact_count(tmp_path, capsys):
    cases = [c for c in GREEN if "test_y" not in c]  # test_y[p1] deselected: not in the junit at all
    code, _r = _judge(tmp_path, cases)
    assert code == 1 and "fewer than the minimum 4" in capsys.readouterr().out


def test_a_file_with_one_test_that_did_not_pass_is_red_even_if_another_passed(tmp_path, capsys):
    cases = [_case(FILE_A, "test_x"), _case(FILE_A, "test_y[p1]", "skipped"), _case(FILE_B, "test_z"),
             _case("tests/test_c.py", "test_one")]
    code, result = _judge(tmp_path, cases)
    assert code == 1 and result["missing_tests"] == [FILE_A]
    assert "the file %s has tests that did not pass (skipped)" % FILE_A in capsys.readouterr().out


def test_a_junit_written_as_xunit1_is_red_even_if_it_keeps_the_classname(tmp_path, capsys):
    xunit1 = _case(FILE_A, "test_x").replace(' time=', ' file="tests\test_a.py" line="3" time=')
    code, _r = _judge(tmp_path, [xunit1, *GREEN[1:]])
    assert code == 1 and "not xunit2" in capsys.readouterr().out


def test_a_junit_without_classname_is_red(tmp_path, capsys):
    cases = [_case(FILE_A, "test_x", classname=""), *GREEN[1:]]  # what junit_family=xunit1 leaves
    code, _r = _judge(tmp_path, cases)
    out = capsys.readouterr().out
    assert code == 1 and "not xunit2" in out and "junit_family=xunit2" in out


# --- the list is validated -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad, why", [
    (dict(os=[]), "os must be"), (dict(os=["macos"]), "os must be"), (dict(os="linux"), "os must be"),
    (dict(os=["linux", "linux"]), "os must be"),
    (dict(tests=[]), "tests must be"), (dict(tests=[1]), "tests must be"), (dict(tests=[FILE_A, FILE_A]), "duplicates"),
    (dict(min_tests=0), "min_tests"), (dict(min_tests="4"), "min_tests"), (dict(min_tests=True), "min_tests"),
    (dict(exact="yes"), "exact"), (dict(marker=""), "marker"), (dict(id=""), "no id"),
    (dict(needs_root="linux"), "needs_root"), (dict(needs_root=["macos"]), "needs_root"),
    (dict(os=["linux"], needs_root=["windows"]), "needs_root"),
])
def test_a_malformed_row_is_exit_2_with_the_reason_in_all_three_commands(tmp_path, capsys, bad, why):
    required = _write_required(tmp_path / "required.json", [_suite(**bad)])
    assert verdict.main(["args", "--required", str(required), "--suite", "s.one", "--os", "linux"]) == 2
    assert why in capsys.readouterr().err
    code, result = _judge(tmp_path, GREEN, rows=[_suite(**bad)])
    assert code == 2 and result["verdict"] == "red" and why in capsys.readouterr().out
    assert verdict.main(["complete", "--required", str(required), "--results", str(tmp_path)]) == 2
    assert why in capsys.readouterr().err


def test_duplicate_ids_and_a_row_that_is_not_an_object_are_exit_2(tmp_path, capsys):
    required = _write_required(tmp_path / "required.json", [_suite(), _suite()])
    assert verdict.main(["args", "--required", str(required), "--suite", "s.one", "--os", "linux"]) == 2
    assert "more than once" in capsys.readouterr().err
    required.write_text('[1, "x"]', encoding="utf-8")
    assert verdict.main(["args", "--required", str(required), "--suite", "s.one", "--os", "linux"]) == 2
    assert "is not an object" in capsys.readouterr().err


# --- complete ------------------------------------------------------------------------------------------------------
def _results(tmp_path, rows):
    """Run judge for every (suite, os) of rows into tmp_path/results/<artifact>/, as the gate downloads them."""
    required = _write_required(tmp_path / "required.json", rows)
    for row in rows:
        for os_name in row["os"]:
            binary = tmp_path / ("engine-%s.bin" % os_name)
            binary.write_bytes(("engine-" + os_name).encode())
            junit = _junit(tmp_path / ("j-%s-%s.xml" % (row["id"], os_name)), GREEN)
            out = tmp_path / "results" / ("result-%s-%s" % (row["id"], os_name)) / ("%s.%s.json" % (row["id"], os_name))
            code = verdict.main(["judge", "--required", str(required), "--suite", row["id"], "--os", os_name,
                                 "--junit", str(junit), "--binary", str(binary), "--commit", SHA, "--out", str(out)])
            assert code == 0
    return required, tmp_path / "results"


def _complete(required, results):
    return verdict.main(["complete", "--required", str(required), "--results", str(results)])


def _edit(results, name, **fields):
    path = next(results.rglob(name))
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(fields)
    path.write_text(json.dumps(data), encoding="utf-8")


ROWS = [_suite(), _suite(id="s.two", os=["linux"])]


def test_complete_is_green_when_every_leg_reported_green_at_one_commit(tmp_path, capsys):
    required, results = _results(tmp_path, ROWS)
    capsys.readouterr()
    assert _complete(required, results) == 0
    assert "COMPLETE GREEN: 3 result(s)" in capsys.readouterr().out


def test_complete_fails_on_a_leg_that_never_reported(tmp_path, capsys):
    required, results = _results(tmp_path, ROWS)
    next(results.rglob("s.one.windows.json")).unlink()
    capsys.readouterr()
    assert _complete(required, results) == 1
    assert "s.one/windows has no result" in capsys.readouterr().out


def test_complete_fails_on_a_missing_results_folder(tmp_path, capsys):
    required = _write_required(tmp_path / "required.json", ROWS)
    assert _complete(required, tmp_path / "nothing") == 1
    out = capsys.readouterr().out
    assert "does not exist" in out and "s.two/linux has no result" in out


def test_complete_fails_on_a_red_result(tmp_path, capsys):
    required, results = _results(tmp_path, ROWS)
    _edit(results, "s.two.linux.json", verdict="red", problems=["1 test(s) failed"])
    capsys.readouterr()
    assert _complete(required, results) == 1
    assert "s.two/linux is not green" in capsys.readouterr().out


def test_complete_fails_on_a_result_with_no_verdict(tmp_path):
    required, results = _results(tmp_path, ROWS)
    _edit(results, "s.two.linux.json", verdict=None)
    assert _complete(required, results) == 1


def test_complete_fails_on_a_result_of_a_suite_that_is_not_in_the_list(tmp_path, capsys):
    required, results = _results(tmp_path, ROWS)
    (results / "result-s.ghost-linux").mkdir()
    (results / "result-s.ghost-linux" / "g.json").write_text(
        json.dumps({"suite": "s.ghost", "os": "linux", "verdict": "green"}), encoding="utf-8")
    capsys.readouterr()
    assert _complete(required, results) == 1
    assert "s.ghost/linux, which is not in the required list" in capsys.readouterr().out


def test_complete_fails_on_a_result_of_an_os_the_suite_does_not_run_on(tmp_path, capsys):
    required, results = _results(tmp_path, ROWS)
    (results / "result-s.two-windows").mkdir()
    (results / "result-s.two-windows" / "w.json").write_text(
        json.dumps({"suite": "s.two", "os": "windows", "verdict": "green"}), encoding="utf-8")
    capsys.readouterr()
    assert _complete(required, results) == 1
    assert "s.two/windows, which is not in the required list" in capsys.readouterr().out


def test_complete_fails_on_the_same_leg_reported_twice(tmp_path, capsys):
    required, results = _results(tmp_path, ROWS)
    src = next(results.rglob("s.two.linux.json"))
    (results / "again").mkdir()
    (results / "again" / "copy.json").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    capsys.readouterr()
    assert _complete(required, results) == 1
    assert "s.two/linux has two results" in capsys.readouterr().out


def test_complete_fails_on_legs_at_different_commits(tmp_path, capsys):
    required, results = _results(tmp_path, ROWS)
    _edit(results, "s.one.windows.json", commit="c" * 40)
    capsys.readouterr()
    assert _complete(required, results) == 1
    assert "different commits" in capsys.readouterr().out


def test_complete_fails_when_two_suites_of_one_os_ran_with_different_binaries(tmp_path, capsys):
    required, results = _results(tmp_path, ROWS)
    _edit(results, "s.two.linux.json", binary_sha256="f" * 64)
    capsys.readouterr()
    assert _complete(required, results) == 1
    assert "linux legs ran with different binaries" in capsys.readouterr().out


def test_complete_fails_on_a_result_with_no_binary_hash(tmp_path, capsys):
    required, results = _results(tmp_path, ROWS)
    _edit(results, "s.two.linux.json", binary_sha256=None)
    capsys.readouterr()
    assert _complete(required, results) == 1
    assert "no binary sha256" in capsys.readouterr().out


def test_complete_allows_different_binaries_for_different_os(tmp_path):
    required, results = _results(tmp_path, ROWS)  # engine-linux.bin and engine-windows.bin differ
    assert _complete(required, results) == 0


def test_complete_fails_on_an_unreadable_json_and_on_a_json_that_is_not_a_result(tmp_path, capsys):
    required, results = _results(tmp_path, ROWS)
    (results / "junk.json").write_text("{not json", encoding="utf-8")
    (results / "other.json").write_text("[1]", encoding="utf-8")
    capsys.readouterr()
    assert _complete(required, results) == 1
    out = capsys.readouterr().out
    assert "junk.json cannot be read" in out and "other.json is not a result" in out


# --- the real list-----------------------------------------------------------------------------------------------
def _real():
    """The rows of the real list as the judge reads them: the base plus the fragments (`min_tests` is the effective floor)."""
    return verdict.effective_rows(REQUIRED)


def test_the_real_list_is_well_formed_for_the_judge_and_every_row_is_exact():
    rows = _real()
    assert verdict.validate_list(rows) == []
    assert [r["id"] for r in rows if r["exact"] is not True] == []


def test_the_real_list_has_unique_ids_and_sane_rows():
    rows = _real()
    ids = [r["id"] for r in rows]
    assert rows and len(ids) == len(set(ids)), ids
    for r in rows:
        assert set(r["os"]) and set(r["os"]) <= {"linux", "windows"}, r["id"]
        assert isinstance(r["min_tests"], int) and r["min_tests"] > 0, r["id"]
        assert r["tests"] and len(r["tests"]) == len(set(r["tests"])), r["id"]
        assert r["giro"] in {"G1", "G2"}, r["id"]
        # needs_root is a bool (every OS of the row) or the list of the OSes that need root
        root = r["needs_root"]
        assert isinstance(root, bool) or (isinstance(root, list) and set(root) <= set(r["os"])), r["id"]


RETIRED = ROOT / "ci" / verdict.RETIRED_NAME


def test_the_real_retired_list_is_well_formed_and_names_rows_that_exist():
    data = json.loads(RETIRED.read_text(encoding="utf-8"))
    assert verdict.validate_retired(data) == []
    ids = {r["id"] for r in _real()}
    for entry in data:
        if "nodeid" in entry:
            assert entry["suite"] in ids, entry["nodeid"]
        else:
            assert set(entry["tests"]) <= ids, entry["file"]


def test_the_tests_a_retired_file_had_are_what_the_collection_counts_while_it_exists():
    """The number that lets a minimum go down is exact: for a retired file still in the tree (the one that retires it deletes it, and this
    asks nothing of a file that is gone) it is the number of tests the row's marker collects in it."""
    import subprocess
    import sys
    markers = {r["id"]: r["marker"] for r in _real()}
    for entry in json.loads(RETIRED.read_text(encoding="utf-8")):
        if "nodeid" in entry:
            continue  # a retired test is counted by the next test
        if not (ROOT / entry["file"]).is_file():
            continue
        for row, declared in entry["tests"].items():
            done = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "-o", "addopts=",
                                   "-m", markers[row], entry["file"]], cwd=str(ROOT), capture_output=True, text=True)
            counted = sum(1 for line in done.stdout.splitlines() if "::" in line)
            assert counted == declared, "%s in %s: declared %d, collected %d" % (entry["file"], row, declared, counted)


def test_every_retired_test_names_a_file_its_row_runs_and_counts_what_the_collection_counts_while_it_exists():
    """A retired test leaves a file that stays, in a row that lists that file or that very node id; while the test is still defined in the tree
    (the PR that removes it deletes it) its count is the number of tests the row's marker collects under that node id."""
    import subprocess
    import sys
    rows = {r["id"]: r for r in _real()}
    for entry in json.loads(RETIRED.read_text(encoding="utf-8")):
        if "nodeid" not in entry:
            continue
        nid, row = entry["nodeid"], rows[entry["suite"]]
        path, _sep, name = nid.partition("::")
        assert nid in row["tests"] or path in row["tests"], "%s is not run by %s" % (nid, row["id"])
        assert (ROOT / path).is_file(), "%s: the file of a retired test stays" % nid
        if _node_problems(row["id"], path, name, ROOT):
            continue  # already out of the tree
        done = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "-o", "addopts=",
                               "-m", row["marker"], nid], cwd=str(ROOT), capture_output=True, text=True)
        counted = sum(1 for line in done.stdout.splitlines() if "::" in line)
        assert counted == entry["count"], "%s in %s: declared %d, collected %d" % (nid, row["id"], entry["count"], counted)


WHOLE_SUITE_MARKER = "not slow and not e2e"  # the exact expression of the unit job of tests.yml (pyproject addopts)


def _node_problems(sid, path, name, root):
    file = root / path
    if not file.is_file():
        return ["%s: %s does not exist" % (sid, path)]
    if name:
        text = file.read_text(encoding="utf-8")
        base = name.split("[", 1)[0]
        if not re.search(r"^\s*(async\s+)?def %s\(" % re.escape(base), text, re.M):
            return ["%s: %s has no def %s" % (sid, path, base)]
    return []


def required_entry_problems(rows, root=ROOT):
    """What a row may name in `tests`: a file, with an optional `::name` defined in it. A folder (`tests`) and an option
    (`--deselect=...`, `-k`) are refused: the judge takes files and node ids, and a row that names a folder holds whatever
    the folder holds today, so a file that is added or that skips is not what the row says."""
    out = []
    for r in rows:
        for entry in r["tests"]:
            path, _sep, name = entry.partition("::")
            if entry.startswith("-"):
                out.append("%s: %s is an option, and a row names files and node ids" % (r["id"], entry))
            elif not path.endswith(".py"):
                out.append("%s: %s is not a file (a folder is refused: name its files)" % (r["id"], entry))
            else:
                out += _node_problems(r["id"], path, name, root)
    return out


def marker_problems(rows, root=ROOT):
    """The marker a row names: unit, e2e, or the expression of the whole unit suite (a row of the files of tests/ that tests.yml runs). A row of the
    marker unit holds files that carry it."""
    out = []
    for r in rows:
        if r["marker"] not in {"unit", "e2e", WHOLE_SUITE_MARKER}:
            out.append("%s: the marker %r is not one the gate knows" % (r["id"], r["marker"]))
        elif r["marker"] == "unit":
            for entry in r["tests"]:
                file = root / entry.split("::")[0]
                text = file.read_text(encoding="utf-8") if file.is_file() else ""
                if "pytestmark = pytest.mark.unit" not in text and "@pytest.mark.unit" not in text:
                    out.append("%s: %s does not carry the marker unit" % (r["id"], entry))
    return out


def test_every_required_file_exists_and_every_node_id_is_defined_in_it():
    assert required_entry_problems(_real()) == []


def test_a_required_suite_runs_with_the_marker_it_names():
    assert marker_problems(_real()) == []


_WHOLE = "wrapper.unit.all.x"
_FILE = "tests/test_ci_verdict.py"


def _whole(*tests, **over):
    row = {"id": _WHOLE, "marker": WHOLE_SUITE_MARKER, "tests": list(tests) or [_FILE]}
    row.update(over)
    return row


def test_a_row_of_files_is_accepted_with_the_marker_of_the_whole_suite():
    rows = [_whole(_FILE, "tests/test_a_unit_test_does_not_read_the_seal_of_the_job.py")]
    assert required_entry_problems(rows) == [] and marker_problems(rows) == []


def test_the_folder_tests_is_refused_in_every_row():
    for marker in (WHOLE_SUITE_MARKER, "unit", "e2e"):
        found = required_entry_problems([_whole("tests", marker=marker)])
        assert len(found) == 1 and "is not a file" in found[0], found


def test_another_folder_is_refused():
    for entry in ("tests/", "tests/data", "tests/unit", "src", "scripts", "."):
        assert required_entry_problems([_whole(entry)]) != [], entry


def test_a_deselect_is_refused_even_of_a_test_that_is_defined():
    entry = "--deselect=tests/test_ci_verdict.py::test_a_required_suite_runs_with_the_marker_it_names"
    found = required_entry_problems([_whole(_FILE, entry)])
    assert len(found) == 1 and "is an option" in found[0], found


def test_a_node_id_of_a_name_that_does_not_exist_is_refused():
    found = required_entry_problems([_whole("tests/test_ci_verdict.py::test_that_is_not_there")])
    assert len(found) == 1 and "has no def test_that_is_not_there" in found[0], found


def test_a_file_that_does_not_exist_is_refused():
    assert required_entry_problems([_whole("tests/test_nothing_here.py")]) != []
    assert required_entry_problems([_whole("tests/test_nothing_here.py::test_a")]) != []


def test_another_option_is_refused():
    for entry in ("-k", "-x", "--ignore=tests/test_ci_verdict.py", "--co", "-m"):
        assert required_entry_problems([_whole(entry)]) != [], entry


def test_an_unknown_marker_is_refused():
    assert marker_problems([_whole(marker="not slow")]) != []
    assert marker_problems([_whole(marker="unit or e2e")]) != []


def test_the_real_whole_suite_rows_name_files_only_and_hold_what_the_unit_job_of_tests_yml_holds():
    rows = [r for r in _real() if r["marker"] == WHOLE_SUITE_MARKER]
    assert sorted(r["id"] for r in rows) == ["wrapper.unit.all.linux", "wrapper.unit.all.windows"]
    for r in rows:
        assert r["giro"] == "G1" and r["exact"] is True, r["id"]
        assert [e for e in r["tests"] if "::" in e or e == "tests" or e.startswith("-")] == [], r["id"]


def test_a_suite_that_needs_root_only_on_linux_still_runs_on_windows():
    doh = [r for r in _real() if r["id"] == "wrapper.e2e.doh"][0]
    assert doh["os"] == ["linux", "windows"] and doh["needs_root"] == ["linux"]


# --- the floor is the list plus the fragments of the folder next to it ----------------------------------------------
def _cases(n):
    """A junit of n passing tests (the four of GREEN and more of FILE_A)."""
    return GREEN + [_case(FILE_A, "test_more_%d" % i) for i in range(n - len(GREEN))]


def _main_args(tmp_path, rows, fragments=None, consumed=None):
    return ["--main-required", str(_main_file(tmp_path, rows, fragments, consumed))]


def test_two_fragments_on_one_row_add_up(tmp_path):
    both = {"pr-a": {"s.one": 2}, "pr-b": {"s.one": 3}}
    assert _judge(tmp_path, _cases(9), fragments=both)[0] == 0  # 4 in the list, 2 + 3 in the fragments
    assert verdict.effective_rows(tmp_path / "required.json")[0]["min_tests"] == 9
    assert _judge(tmp_path, _cases(8), fragments=both)[0] == 1


def test_main_plus_two_open_prs_is_green_and_exact_holds_on_the_sum(tmp_path, capsys):
    both = {"pr-a": {"s.one": 2}, "pr-b": {"s.one": 3}}
    assert _judge(tmp_path, _cases(9), fragments=both, extra=_main_args(tmp_path, [_suite()]))[0] == 0
    capsys.readouterr()
    code, _r = _judge(tmp_path, _cases(10), fragments=both, extra=_main_args(tmp_path, [_suite()]))
    out = capsys.readouterr().out
    assert code == 1 and "exact: 9" in out and "add a fragment" in out
    # the same when main already has A and the branch adds B on top of it
    later = tmp_path / "later"
    assert _judge(later, _cases(9), fragments=both, extra=_main_args(later, [_suite()], {"pr-a": {"s.one": 2}}))[0] == 0


def test_a_fragment_of_main_that_is_missing_outside_a_fold_is_red(tmp_path, capsys):
    main = _main_args(tmp_path, [_suite()], {"pr-a": {"s.one": 2}})  # main collects 6
    code, _r = _judge(tmp_path, _cases(4), extra=main)  # the branch deleted the fragment: its own count is exact at 4
    out = capsys.readouterr().out
    assert code == 1 and "fragments of main were removed outside a fold" in out and "went down" in out


def test_a_fold_with_equal_sums_is_green(tmp_path):
    main = _main_args(tmp_path, [_suite()], {"pr-a": {"s.one": 2}})  # 4 + 2
    assert _judge(tmp_path, _cases(6), rows=[_suite(min_tests=6)], extra=main)[0] == 0  # the list now says 6, the fragment is gone


def test_a_fold_that_loses_one_is_red(tmp_path, capsys):
    main = _main_args(tmp_path, [_suite()], {"pr-a": {"s.one": 2}})
    code, _r = _judge(tmp_path, _cases(5), rows=[_suite(min_tests=5)], extra=main)
    out = capsys.readouterr().out
    assert code == 1 and "is not the one of main (6 on main, 5 here)" in out


def test_a_fold_moves_the_consumes_of_the_removed_fragments_into_consumed_json(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    retire = {"retire-it": {"s.one": -1, "consumes": [GONE_NODE]}}
    main = _main_that_had_the_test(tmp_path, [_retired_test()], min_tests=5, fragments=retire)  # 5 - 1 = 4
    assert _judge(tmp_path, GREEN, rows=[_suite(min_tests=4)], consumed=[GONE_NODE], extra=["--main-required", str(main)])[0] == 0
    capsys.readouterr()
    sub = tmp_path / "forgot"
    main = _main_that_had_the_test(sub, [_retired_test()], min_tests=5, fragments=retire)
    code, _r = _judge(sub, GREEN, rows=[_suite(min_tests=4)], extra=["--main-required", str(main)])  # consumed.json did not gain the nodeid
    assert code == 1 and "a fold moves the `consumes`" in capsys.readouterr().out


def test_a_fold_carries_no_new_fragment(tmp_path, capsys):
    main = _main_args(tmp_path, [_suite()], {"pr-a": {"s.one": 2}})
    code, _r = _judge(tmp_path, _cases(6), rows=[_suite(min_tests=5)], fragments={"pr-c": {"s.one": 1}}, extra=main)  # 5 + 1 = 6 as on main
    assert code == 1 and "a fold carries no new fragment" in capsys.readouterr().out


def test_a_fragment_of_main_that_is_edited_is_red(tmp_path, capsys):
    main = _main_args(tmp_path, [_suite()], {"pr-a": {"s.one": 2}})
    code, _r = _judge(tmp_path, _cases(7), fragments={"pr-a": {"s.one": 3}}, extra=main)
    assert code == 1 and "the fragment pr-a of main was changed here" in capsys.readouterr().out


def test_a_consumed_json_that_differs_from_main_outside_a_fold_is_red(tmp_path, capsys):
    main = _main_args(tmp_path, [_suite()])
    code, _r = _judge(tmp_path, GREEN, consumed=[GONE_NODE], extra=main)
    assert code == 1 and "consumed.json differs from main's outside a fold" in capsys.readouterr().out


def test_a_negative_value_with_no_consumes_is_a_malformed_list(tmp_path, capsys):
    code, _r = _judge(tmp_path, GREEN, rows=[_suite(min_tests=5)], fragments={"retire-it": {"s.one": -1}})
    assert code == 2 and "negative" in capsys.readouterr().out


def test_a_negative_value_that_consumes_a_test_main_never_retired_is_red(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main = _main_that_had_the_test(tmp_path, None)
    code, _r = _judge(tmp_path, GREEN, rows=[_suite(min_tests=5)], fragments=_retiring(GONE_NODE), extra=["--main-required", str(main)])
    assert code == 1 and "not a retired test of main" in capsys.readouterr().out


def test_a_retirement_that_main_consumed_in_its_consumed_json_cannot_be_consumed_again(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main = _main_that_had_the_test(tmp_path, [_retired_test()], consumed=[GONE_NODE])
    code, _r = _judge(tmp_path, GREEN, rows=[_suite(min_tests=5)], consumed=[GONE_NODE], fragments=_retiring(GONE_NODE),
                      extra=["--main-required", str(main)])
    out = capsys.readouterr().out
    assert code == 1 and "already consumed" in out and "main's consumed.json" in out


def test_a_retirement_that_a_fragment_of_main_consumed_cannot_be_consumed_again(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    first = {"first-retire": {"s.one": -1, "consumes": [GONE_NODE]}}
    main = _main_that_had_the_test(tmp_path, [_retired_test()], fragments=first)
    code, _r = _judge(tmp_path, GREEN, rows=[_suite(min_tests=5)], fragments=dict(first, **{"second-retire": {"s.one": -1, "consumes": [GONE_NODE]}}),
                      extra=["--main-required", str(main)])
    out = capsys.readouterr().out
    assert code == 1 and "already consumed" in out and "the fragment first-retire of main" in out


def test_the_same_retirement_in_two_fragments_of_the_branch_is_consumed_twice_and_red(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main = _main_that_had_the_test(tmp_path, [_retired_test()], min_tests=6)
    two = {"retire-a": {"s.one": -1, "consumes": [GONE_NODE]}, "retire-b": {"s.one": -1, "consumes": [GONE_NODE]}}
    code, _r = _judge(tmp_path, GREEN, rows=[_suite(min_tests=6)], fragments=two, extra=["--main-required", str(main)])
    assert code == 1 and "already consumed" in capsys.readouterr().out


def test_main_s_list_without_its_folder_is_red(tmp_path, capsys):
    (tmp_path / "mainside").mkdir()
    (tmp_path / "mainside" / "main.json").write_text(json.dumps([_suite()]), encoding="utf-8")
    code, _r = _judge(tmp_path, GREEN, extra=["--main-required", str(tmp_path / "mainside" / "main.json")])
    assert code == 1 and "main's fragments are not known" in capsys.readouterr().out


def test_a_malformed_fragment_of_main_is_red_never_ignored(tmp_path, capsys):
    main = _main_args(tmp_path, [_suite()], {"Not-A-Slug": {"s.one": 1}})
    code, _r = _judge(tmp_path, GREEN, extra=main)
    assert code == 1 and "main's fragments are not readable" in capsys.readouterr().out


def _bad_fragment(tmp_path, name, content):
    required = _write_required(tmp_path / "required.json", [_suite()])
    folder = required.with_name("required-suites.d")
    if name is None:
        (folder / "consumed.json").unlink()
        folder.rmdir()
    else:
        (folder / name).write_text(content, encoding="utf-8")
    return required


@pytest.mark.parametrize("name, content, why", [
    ("Upper.json", '{"s.one": 1}', "the name of a fragment"),
    ("notes.txt", "1", "the name of a fragment"),
    ("a.json", '{"s.unknown": 1}', "not a row of the list"),
    ("a.json", '{"s.one": 0}', "is 0"),
    ("a.json", '{"s.one": "2"}', "must be an integer"),
    ("a.json", '{"s.one": true}', "must be an integer"),
    ("a.json", '{"s.one": -1}', "has no `consumes`"),
    ("a.json", '[1]', "JSON object"),
    ("a.json", '{"s.one": 1', "cannot be read"),
    (None, "", "does not exist"),
])
def test_a_malformed_fragment_is_a_malformed_list_exit_2_with_the_reason(tmp_path, capsys, name, content, why):
    required = _bad_fragment(tmp_path, name, content)
    assert verdict.main(["args", "--required", str(required), "--suite", "s.one", "--os", "linux"]) == 2
    assert why in capsys.readouterr().err


def test_a_malformed_fragment_is_exit_2_for_judge_and_complete_too(tmp_path, capsys):
    required = _bad_fragment(tmp_path, "a.json", '{"s.one": 0}')
    junit = _junit(tmp_path / "junit.xml", GREEN)
    binary = tmp_path / "bin"
    binary.write_bytes(b"x")
    assert verdict.main(["judge", "--required", str(required), "--suite", "s.one", "--os", "linux", "--junit", str(junit),
                         "--binary", str(binary), "--commit", SHA]) == 2
    assert "is 0" in capsys.readouterr().out
    (tmp_path / "results").mkdir()
    assert verdict.main(["complete", "--required", str(required), "--results", str(tmp_path / "results")]) == 2
    assert "is 0" in capsys.readouterr().err


def test_the_rewritten_base_list_has_one_value_per_line_and_is_the_dump_of_its_content():
    text = REQUIRED.read_bytes().decode("utf-8")
    assert "\r" not in text and text.endswith("]\n")
    assert text == json.dumps(json.loads(text), indent=1, ensure_ascii=False) + "\n"
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip().rstrip(",")
        # a line holds one thing: a bracket, a key with a scalar, a key opening a container, or one array item
        assert stripped in ("[", "]", "{", "}") or stripped.startswith('"') or stripped.lstrip("-").isdigit() or stripped in ("true", "false"), (number, line)


def test_the_real_folder_of_fragments_exists_holds_consumed_json_and_every_fragment_loads():
    folder = verdict.fragments_dir(REQUIRED)
    assert folder.is_dir() and (folder / "consumed.json").is_file()
    assert [r["id"] for r in verdict.effective_rows(REQUIRED)] == [r["id"] for r in json.loads(REQUIRED.read_text(encoding="utf-8"))]


def _git(cwd, *args):
    import subprocess
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
                          cwd=str(cwd), check=True, capture_output=True, text=True)


def _repo_with_main(tmp_path, fragments=None):
    """A repository whose branch `main` has ci/required-suites.json and its folder (with `fragments`)."""
    repo = tmp_path / "repo"
    (repo / "ci").mkdir(parents=True)
    required = _write_required(repo / "ci" / "required-suites.json", [_suite()], fragments)
    _git(repo, "init", "-q")
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "main")
    return required


def test_the_fragment_helper_adds_a_suffix_when_the_name_exists_and_never_overwrites(tmp_path, capsys):
    required = _repo_with_main(tmp_path, {"taken-on-main": {"s.one": 1}})
    folder = required.with_name("required-suites.d")

    def run(*args):
        code = verdict.main(["fragment", "--required", str(required), "--main-ref", "main", *args])
        return code, capsys.readouterr().out.strip()

    code, out = run("--name", "feature-x", "--set", "s.one=2")
    assert code == 0 and Path(out) == folder / "feature-x.json" and json.loads(Path(out).read_text(encoding="utf-8")) == {"s.one": 2}
    first = (folder / "feature-x.json").read_bytes()
    code, out = run("--name", "feature-x", "--set", "s.one=5")
    assert code == 0 and Path(out).name == "feature-x-2.json" and (folder / "feature-x.json").read_bytes() == first
    code, out = run("--name", "feature-x", "--set", "s.one=7")
    assert code == 0 and Path(out).name == "feature-x-3.json" and (folder / "feature-x-2.json").read_bytes() != first
    # a name only main has (it is not in the working tree) is taken too
    (folder / "taken-on-main.json").unlink()
    code, out = run("--name", "taken-on-main", "--set", "s.one=1")
    assert code == 0 and Path(out).name == "taken-on-main-2.json" and not (folder / "taken-on-main.json").exists()
    # the effective list reads what the helper wrote
    assert verdict.effective_rows(required)[0]["min_tests"] == 4 + 2 + 5 + 7 + 1


def test_the_fragment_helper_refuses_what_the_loader_would_refuse(tmp_path, capsys):
    required = _repo_with_main(tmp_path)
    base = ["fragment", "--required", str(required), "--main-ref", "main"]
    assert verdict.main(base + ["--name", "x", "--set", "s.unknown=1"]) == 2
    assert verdict.main(base + ["--name", "x", "--set", "s.one=0"]) == 2
    assert verdict.main(base + ["--name", "x", "--set", "s.one=-1"]) == 2  # a negative value needs --consumes
    assert verdict.main(base + ["--name", "consumed", "--set", "s.one=1"]) == 2
    assert verdict.main(base + ["--name", "Bad Name", "--set", "s.one=1"]) == 2
    assert verdict.main(["fragment", "--required", str(required), "--main-ref", "no-such-ref", "--name", "x", "--set", "s.one=1"]) == 2
    assert sorted(p.name for p in required.with_name("required-suites.d").iterdir()) == ["consumed.json"]
    assert verdict.main(base + ["--name", "x", "--set", "s.one=-1", "--consumes", GONE_NODE]) == 0
    assert json.loads(next(required.with_name("required-suites.d").glob("x.json")).read_text(encoding="utf-8")) == {"s.one": -1, "consumes": [GONE_NODE]}


def _defines(path, name):
    """True if the file defines a function or a method called `name` (a static look: nothing is imported or collected)."""
    import ast
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name for n in ast.walk(tree))


def test_every_retired_test_that_is_gone_from_its_file_is_consumed_or_in_a_fragment():
    """A retirement whose test no longer exists was applied on main: it must be in consumed.json or in the `consumes` of a fragment, or a later
    PR could use it again to excuse an unrelated loss. A retirement whose test still exists is pending, and allowed."""
    required = verdict.read_required(REQUIRED)
    used = set(required.consumed) | {n for f in required.fragments.values() for n in f["consumes"]}
    unconsumed = []
    for entry in json.loads(RETIRED.read_text(encoding="utf-8")):
        if "nodeid" not in entry:
            continue
        file, test = entry["nodeid"].split("::", 1)
        if not _defines(ROOT / file, test.split("[", 1)[0].split("::")[-1]) and entry["nodeid"] not in used:
            unconsumed.append(entry["nodeid"])
    assert not unconsumed, "retired and gone, but not consumed: %s" % sorted(set(unconsumed))
