#!/usr/bin/env python3
"""CI skips - the judge of a pytest leg, from junit only (stdlib only, Python >= 3.11).

Rule ci-13: nothing counts as passed that did not pass. A pytest leg is green only when every testcase of its junit passed:
a failure, an error, a skip that is not in the list below, a testcase that passed only at a later attempt, or fewer tests
than the floor is red. The judge never reads pytest's text ("N passed" is not evidence); it reads the junit xml.

    ci_skips.py judge  --junit <xml> [--junit <xml> ...] --allowed ci/allowed-skips.json --os <linux|windows>
                       --python <x.y> --min-tests N [--root <dir>]
        prints one line per problem and exits 1; prints one `OK` line and exits 0 when there is none:

            FAIL <where>: <what>. Fix: <how>. Rule: ci-13 nothing counts as passed that did not pass

        A problem is: a junit that is missing or unreadable; a testcase that failed or errored; a testcase skipped with
        type "pytest.rerun" (a failed attempt that was run again: even a listed node cannot excuse it); a skipped
        testcase (an xfail is one) that no row of the list matches for this os and python; a list that is not a json
        array; a row that is malformed, has no reason, has a bad os or python, or names a test file that does not exist
        under the root; fewer tests that passed than --min-tests (a skipped test is not one of them).

    ci_skips.py report --junit <xml> [--junit <xml> ...] [--allowed <list>] [--os <os>] [--python <x.y>] [--min-tests N]
                       [--root <dir>]
        only prints the skipped testcases that the list does not excuse (`SKIP <testcase>: <reason>`); decides nothing and
        always exits 0 (it is how a list is filled from a real run, never by guess). The same options as `judge`, none
        of them required.

The list, `ci/allowed-skips.json`, is a JSON array of rows (an empty array is a list that excuses nothing):

    {"node": "tests/test_x.py" | "tests/test_x.py::test_y" | "tests/test_x.py::Class::test_y",
     "os": "linux" | "windows" | null, "python": "3.11" | null, "reason": "<why this test cannot run there>"}

* `node` is a test file, or a node id inside it (a node id also covers its parametrized testcases `test_y[...]`);
* `os` and `python` null mean every system / every python;
* `reason` is mandatory, in words that say why THIS test cannot run on THAT system; a skip that exists only because a
  capability is missing from the runner is a defect to fix, not a row to add;
* the test file of a row must exist under the root: a row of a deleted test is red.

`--root` is the folder that the test files of the rows and the classnames of the junit resolve against (default: the working
directory, which is the root of the repository when the judge runs from it).

Both this script and the list are read from main by the workflow that uses them: a branch cannot edit its own judge or
its own list.

Exit codes: 0 green (or `report`), 1 red, 2 wrong usage.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

RULE = "ci-13 nothing counts as passed that did not pass"
OSES = ("linux", "windows")
ROW_KEYS = {"node", "os", "python", "reason"}
_PYTHON = re.compile(r"^\d+\.\d+$")


def _line(where: str, what: str, fix: str) -> str:
    return "FAIL %s: %s. Fix: %s. Rule: %s" % (where, what, fix, RULE)


def _file_of(classname: str, attr_file: str | None, root: Path) -> tuple[str, str]:
    """(test file, the rest of the node id) of a testcase: the `file` attribute when the junit has one, else the classname."""
    parts = classname.split(".") if classname else []
    if attr_file:
        file = attr_file.replace("\\", "/")
        stem = file[:-3].replace("/", ".") if file.endswith(".py") else file
        rest = classname[len(stem) + 1:] if classname.startswith(stem + ".") else ""
        return file, rest.replace(".", "::")
    for i in range(len(parts), 0, -1):  # the longest prefix that is a file that exists under the root
        candidate = "/".join(parts[:i]) + ".py"
        if (root / candidate).is_file():
            return candidate, "::".join(parts[i:])
    for i, part in enumerate(parts):  # a file that is gone: up to the first module named test_*
        if part.startswith("test_"):
            return "/".join(parts[:i + 1]) + ".py", "::".join(parts[i + 1:])
    return "/".join(parts) + ".py", ""


class Case:
    def __init__(self, element: ET.Element, root: Path):
        self.name = element.get("name", "")
        self.file, rest = _file_of(element.get("classname", ""), element.get("file"), root)
        self.nodeid = "::".join(x for x in (self.file, rest, self.name) if x)
        self.failed = element.find("failure") is not None or element.find("error") is not None
        skipped = element.find("skipped")
        self.skipped = skipped is not None
        self.skip_type = (skipped.get("type") or "") if skipped is not None else ""
        self.skip_message = ((skipped.get("message") or "") if skipped is not None else "").strip()
        self.passed = not self.failed and not self.skipped


def read_junits(paths: list[str], root: Path) -> tuple[list[Case], list[str]]:
    cases: list[Case] = []
    problems: list[str] = []
    for path in paths:
        try:
            tree_root = ET.parse(path).getroot()
        except (OSError, ET.ParseError) as exc:
            problems.append(_line(str(path), "the junit is missing or not readable (%s)" % exc,
                                  "the pytest of the leg must write it with --junitxml and -o junit_family=xunit2, and the step must not be skipped"))
            continue
        cases.extend(Case(e, root) for e in tree_root.iter("testcase"))
    return cases, problems


def read_rows(path: str | None, root: Path) -> tuple[list[dict], list[str]]:
    """The valid rows of the list and the problems of the list itself."""
    if not path:
        return [], []
    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], [_line(str(path), "the list of allowed skips is missing or is not json (%s)" % exc,
                          "read it from main: `git show origin/main:ci/allowed-skips.json`")]
    if not isinstance(rows, list):
        return [], [_line(str(path), "the list is not a json array of rows",
                          "write it as `[]` or as an array of {node, os, python, reason}")]
    good: list[dict] = []
    problems: list[str] = []
    for i, row in enumerate(rows):
        where = "%s row %d" % (path, i)
        if not isinstance(row, dict) or set(row) != ROW_KEYS:
            problems.append(_line(where, "a row must have exactly the keys node, os, python, reason",
                                  "write it as {\"node\": ..., \"os\": ..., \"python\": ..., \"reason\": ...}"))
            continue
        node, os_, python, reason = row["node"], row["os"], row["python"], row["reason"]
        bad = False
        if not isinstance(node, str) or not node.strip():
            problems.append(_line(where, "the node is empty", "name the test file or the node id this row excuses"))
            bad = True
        if os_ is not None and os_ not in OSES:
            problems.append(_line(where, "os is %r, not linux, windows or null" % (os_,),
                                  "use linux, windows or null (every system)"))
            bad = True
        if python is not None and not (isinstance(python, str) and _PYTHON.match(python)):
            problems.append(_line(where, "python is %r, not an x.y string or null" % (python,),
                                  "use a string like \"3.11\" or null (every python)"))
            bad = True
        if not isinstance(reason, str) or not reason.strip():
            problems.append(_line(where, "the row has no reason (%s)" % (node,),
                                  "write why this test cannot run there; if it only skips because the runner lacks a capability, fix the runner and delete the row"))
            bad = True
        if isinstance(node, str) and node.strip():
            file = node.split("::", 1)[0]
            if not (root / file).is_file():
                problems.append(_line(where, "the test file %s does not exist under the root" % file,
                                      "delete the row of a test that no longer exists, or correct the path"))
                bad = True
        if not bad:
            good.append(row)
    return good, problems


def _matches(row: dict, case: Case, os_: str | None, python: str | None) -> bool:
    if row["os"] is not None and row["os"] != os_:
        return False
    if row["python"] is not None and row["python"] != python:
        return False
    node = row["node"]
    return case.file == node or case.nodeid == node or case.nodeid.startswith(node + "::") or case.nodeid.startswith(node + "[")


def unlisted_skips(cases: list[Case], rows: list[dict], os_: str | None, python: str | None) -> list[Case]:
    return [c for c in cases if c.skipped and c.skip_type != "pytest.rerun" and not any(_matches(r, c, os_, python) for r in rows)]


def judge(junits: list[str], allowed: str | None, os_: str | None, python: str | None, min_tests: int,
          root: Path | str | None = None) -> list[str]:
    root = Path(root) if root is not None else Path.cwd()
    cases, problems = read_junits(junits, root)
    rows, row_problems = read_rows(allowed, root)
    problems += row_problems
    for c in cases:
        if c.failed:
            problems.append(_line(c.nodeid, "the test failed or errored", "fix the test or the code; a red test is never rerun into a green"))
        if c.skipped and c.skip_type == "pytest.rerun":
            problems.append(_line(c.nodeid, "a failed attempt that was run again (type pytest.rerun): it passed only at a later attempt",
                                  "find why it fails the first time and fix that; the leg runs with -p no:rerunfailures"))
    for c in unlisted_skips(cases, rows, os_, python):
        why = c.skip_message or "no message"
        problems.append(_line(c.nodeid, "skipped (%s) and no row of the list excuses it on %s python %s" % (why, os_, python),
                              "make the test run; only a test that cannot run on this system gets a row in ci/allowed-skips.json, with its reason"))
    ran = sum(1 for c in cases if c.passed)
    if ran < min_tests:
        problems.append(_line("%d junit file(s)" % len(junits), "%d tests passed, the floor is %d" % (ran, min_tests),
                              "a test was deselected, skipped or never collected: restore it, or raise the floor together with the tests"))
    return problems


def report(junits: list[str], allowed: str | None, os_: str | None, python: str | None,
           root: Path | str | None = None) -> list[str]:
    root = Path(root) if root is not None else Path.cwd()
    cases, notes = read_junits(junits, root)
    rows, row_notes = read_rows(allowed, root)
    out = [n.replace("FAIL ", "NOTE ", 1) for n in notes + row_notes]
    for c in unlisted_skips(cases, rows, os_, python):
        out.append("SKIP %s: %s" % (c.nodeid, c.skip_message or "no message"))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ci_skips.py", description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="mode", required=True)
    for mode in ("judge", "report"):
        p = sub.add_parser(mode)
        strict = mode == "judge"
        p.add_argument("--junit", action="append", required=strict, default=None, help="a pytest junit xml (repeatable)")
        p.add_argument("--allowed", required=strict, help="ci/allowed-skips.json")
        p.add_argument("--os", dest="os_name", required=strict, choices=OSES if strict else None)
        p.add_argument("--python", required=strict, help="x.y")
        p.add_argument("--min-tests", type=int, required=strict)
        p.add_argument("--root", type=Path, default=None, help="where test files and classnames resolve (default: the working directory)")
    args = ap.parse_args(argv)
    root = args.root if args.root is not None else Path.cwd()
    junits = args.junit or []
    if args.mode == "report":
        try:
            for line in report(junits, args.allowed, args.os_name, args.python, root):
                print(line)
        except Exception as exc:  # noqa: BLE001 - report never fails
            print("NOTE report: %s" % exc)
        return 0
    if not _PYTHON.match(args.python):
        ap.error("--python must look like 3.11")
    if args.min_tests < 1:
        ap.error("--min-tests must be at least 1")
    problems = judge(junits, args.allowed, args.os_name, args.python, args.min_tests, root)
    for line in problems:
        print(line)
    if problems:
        return 1
    print("OK ci_skips judge: every testcase passed (%d junit file(s), floor %d, os %s, python %s)"
          % (len(junits), args.min_tests, args.os_name, args.python))
    return 0


if __name__ == "__main__":
    sys.exit(main())
