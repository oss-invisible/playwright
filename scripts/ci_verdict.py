#!/usr/bin/env python3
"""CI verdict - the one judge of the mandatory test suites (stdlib only).

`ci/required-suites.json` lists the suites a round must run, per operating system, with the number of tests each must
collect. A test that is skipped, deselected or never collected is a hole nobody sees, so this script reads the junit
pytest wrote and fails on every one of them.

    ci_verdict.py args  --required ci/required-suites.json --suite wrapper.e2e.udp --os linux [--with-marker]
        prints, one per line, the pytest arguments of the suite (files and node ids); with --with-marker, `-m` and the
        suite's marker come first. An error (exit 2) if the suite does not run on that OS or the list is malformed.

    ci_verdict.py judge --required ci/required-suites.json --suite wrapper.e2e.udp --os linux
                        --junit junit.xml --binary PATH [--commit SHA] [--seal gate.seal.json]
                        [--out result/wrapper.e2e.udp.linux.json]
                        [--main-required PATH | --main-required PATH --main-has-no-list]
        reads the junit, writes the result json (with `verdict`: "green" or "red") and exits 1, printing EVERY problem
        with its reason, one per line, when: the junit or the row is missing; the junit is not xunit2 (a testcase with
        no `classname`); a test failed, errored or was skipped; fewer tests were collected than `min_tests`, or, when
        the row is `exact`, any number other than `min_tests` (a deselected test is not in the junit: `min_tests` goes
        up by hand when a test is added, never silently); a required file or node id did not pass; the binary is
        missing (--binary is mandatory: without it exit 2) or the commit cannot be known; the --seal is unreadable.
        With --main-required (the list of main) the list only grows: a suite of main is gone, its minimum went down,
        its `exact` went from true to false, its os or tests lost an element, or its marker changed is red.
        The one exception is `retired-tests.json` NEXT TO main's list (read from main, never from the judged branch): a file it names that
        no longer exists in the checkout may leave the rows, and the minimum of a row may go down by the tests main counted for it in that
        row (`tests: {row id: n}`), and by no more. A retired file still on disk, or any other lost test, stays red.
        The same file may also retire single tests whose file stays: {"nodeid": "<file>::<test>", "suite": <row id>, "count": n, "reason",
        "decided_by", "date", "from_min_tests"}. The minimum of that row may go down by the sum of the `count` of its entries and by no more, and
        only for a nodeid whose file the row of main lists, and only while main's minimum of that row still equals `from_min_tests` (once the removal
        is on main and its floor is lower, the entry is inert); a declared nodeid that the judged leg still collects is red, and so is any test that
        goes without an entry.
        What the file of main is can be said only by the gate, which checks main out (a failed download is not
        "main has no list"): the file exists -> it is compared; it does not exist and the gate passed
        --main-has-no-list (main was checked out and has no such file) -> passed, declared; it does not exist
        without --main-has-no-list -> RED, "it is not known what main has".
        The commit is --commit SHA or `git rev-parse HEAD` in the current directory (the checkout of the wrapper),
        NEVER GITHUB_SHA (in a called workflow it is the commit of the engine's repository). --seal reads the seal of
        the gate: the seal that `python -m invisible_core seal` writes, and the release seal, carry NO sha256 of the
        engine binary (an asset's `sha256` is the one of the archive, `omni_sha256` the one of omni.ja), so there is
        nothing to compare it with: the summary says so and the result records `seal_binary_hash: "absent"`.

    ci_verdict.py complete --required ci/required-suites.json --results DIR
        DIR holds the result json files `judge` wrote (also in subfolders: one artifact `result-<suite>-<os>` each).
        Exit 1, one problem per line, when: a (suite, os) of the list has no result; a result is not green; a result
        is of a (suite, os) that is not in the list, or twice; the commit is not the same in every result, or the
        binary sha256 is not the same in every result of one OS (or missing).

The list is validated by all three commands (a malformed one is exit 2, with the reason, never green): ids unique; per
row `os` a non-empty subset of {linux, windows}, `tests` a non-empty list of unique strings, `min_tests` an integer > 0,
`exact` a bool, `marker` a string, `needs_root` a bool or a list of the row's OSes.

Optional fields of a row, read by the gate to build the leg of the row (absent = the plain behaviour; a field that is
present with a wrong type or value is a malformed list, exit 2, naming the row and the field; a name that is not listed
here is ignored as it always was):
    runner_image     object OS -> runner image name, only OSes of the row, e.g. {"windows": "windows-2022"}
                     (default: ubuntu-24.04 on Linux, windows-latest on Windows)
    timeout_minutes  integer 1..360, the timeout of the job (default 25)
    needs_userns     bool or list of the row's OSes: user namespaces for a normal user (the sandbox suite)
    core_dumps       false, "cores" or "invpw-cores" (Linux only): where the kernel writes the cores of a crashed
                     browser, $RUNNER_TEMP/cores or $RUNNER_TEMP/invpw-cores
    stock_firefox    bool or list of the row's OSes: Playwright's own Firefox is installed for the leg
    location_service bool or list of the row's OSes: the Windows location service is turned on
    run              "pytest" (default) or "suite_run" (scripts/ci_suite_run.py, two passes)
    extra_steps      list of names from the closed set {fakeweb_profiles, keep_oracle_files}
    moz_log          string of MOZ_LOG modules appended to the suite's MOZ_LOG, no space
    juggler_trace    bool: the Juggler's own trace is kept with the result
    artifact         name of the artifact that keeps the oracle files (default: the suite id)

A required entry of "tests" is a file (at least one test of it must exist and EVERY one collected must pass) or a node
id `tests/x.py::name` (that test must pass; a parametrized test is matched on the name before `[`, and every one of its
parameters must pass).

What the gate must do (it is not done here):
* run pytest as `env -u PYTEST_ADDOPTS python -m pytest ... -p no:rerunfailures -o junit_family=xunit2
  --junitxml=junit.xml` (no addopts from outside, no rerun that turns a failure green, a junit that keeps `classname`;
  a junit with no `classname`, or with `file`/`line`, is red);
* on the Linux leg of the DoH suite, export INVPW_REQUIRE_TRAP=1 (the resolver trap must run, never be skipped);
* pass --commit or run in the checkout of the wrapper, pass --binary, and check out main before --main-required.

Exit 0 on a green verdict, 1 on a red one, 2 on a wrong use or a malformed list.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

OSES = ("linux", "windows")
#: The optional row fields of the form "bool or a list of the row's OSes" (besides needs_root).
OS_FLAGS = ("needs_userns", "stock_firefox", "location_service")
CORE_DUMPS = ("cores", "invpw-cores")
RUN_MODES = ("pytest", "suite_run")
#: The closed set of the extra steps a row may ask of the gate (a name here is a step the gate knows how to run).
EXTRA_STEPS = ("fakeweb_profiles", "keep_oracle_files")


class Usage(Exception):
    pass


class BadList(Usage):
    """The required list itself is malformed (exit 2)."""


# --------------------------------------------------------------------------------------------- the required list
def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def validate_list(data):
    """-> the list of reasons why `data` is not a well-formed required list (empty if it is)."""
    if not isinstance(data, list):
        return ["the required list must be a JSON list of suites"]
    if not data:
        return ["the required list is empty"]
    problems, seen = [], set()
    for i, row in enumerate(data):
        if not isinstance(row, dict):
            problems.append("row %d is not an object" % i)
            continue
        sid = row.get("id")
        where = "row %d" % i
        if not isinstance(sid, str) or not sid.strip():
            problems.append("%s has no id (a non-empty string)" % where)
        else:
            where = "row %d (%s)" % (i, sid)
            if sid in seen:
                problems.append("%s: the id appears more than once" % where)
            seen.add(sid)
        oses = row.get("os")
        oses_ok = (isinstance(oses, list) and oses and all(isinstance(o, str) for o in oses)
                   and set(oses) <= set(OSES) and len(set(oses)) == len(oses))
        if not oses_ok:
            problems.append("%s: os must be a non-empty list of distinct values among %s, not %r"
                            % (where, "/".join(OSES), oses))
        tests = row.get("tests")
        if not (isinstance(tests, list) and tests and all(isinstance(t, str) and t.strip() for t in tests)):
            problems.append("%s: tests must be a non-empty list of non-empty strings" % where)
        elif len(set(tests)) != len(tests):
            problems.append("%s: tests has duplicates: %s"
                            % (where, ", ".join(sorted({t for t in tests if tests.count(t) > 1}))))
        if not _is_int(row.get("min_tests")) or row.get("min_tests") <= 0:
            problems.append("%s: min_tests must be an integer > 0, not %r" % (where, row.get("min_tests")))
        if not isinstance(row.get("exact"), bool):
            problems.append("%s: exact must be true or false, not %r" % (where, row.get("exact")))
        if not isinstance(row.get("marker"), str) or not row.get("marker").strip():
            problems.append("%s: marker must be a non-empty string, not %r" % (where, row.get("marker")))
        for name in ("needs_root",) + OS_FLAGS:
            if name in row or name == "needs_root":
                value = row.get(name, False)
                if not _is_os_flag(value, oses, oses_ok):
                    problems.append("%s: %s must be a bool or a list of the OSes of the row, not %r" % (where, name, value))
        problems.extend(_row_field_problems(row, where, oses, oses_ok))
    return problems


def _is_os_flag(value, oses, oses_ok):
    """A bool, or a list of OSes that are all OSes of the row."""
    if isinstance(value, bool):
        return True
    return (isinstance(value, list) and all(isinstance(o, str) for o in value) and set(value) <= set(OSES)
            and (not oses_ok or set(value) <= set(oses)))


def _row_field_problems(row, where, oses, oses_ok):
    """The optional fields of a row that tell the gate how to build the leg (see the docstring): absent is the plain
    behaviour; a field that is present with a value outside its type or its closed set is a problem, named."""
    out = []
    if "runner_image" in row:
        image = row["runner_image"]
        if not (isinstance(image, dict) and image and all(isinstance(k, str) and isinstance(v, str) and v.strip()
                                                          for k, v in image.items())):
            out.append("%s: runner_image must be a non-empty object of OS -> image name, not %r" % (where, image))
        else:
            wrong = sorted(k for k in image if k not in OSES or (oses_ok and k not in oses))
            if wrong:
                out.append("%s: runner_image names an OS the row does not have: %s" % (where, ", ".join(wrong)))
    if "timeout_minutes" in row:
        minutes = row["timeout_minutes"]
        if not _is_int(minutes) or not 0 < minutes <= 360:
            out.append("%s: timeout_minutes must be an integer between 1 and 360, not %r" % (where, minutes))
    if "core_dumps" in row:
        dumps = row["core_dumps"]
        if dumps is not False and dumps not in CORE_DUMPS:
            out.append("%s: core_dumps must be false or one of %s, not %r" % (where, "/".join(CORE_DUMPS), dumps))
        elif dumps is not False and oses_ok and "linux" not in oses:
            out.append("%s: core_dumps is a Linux setting and the row has no linux leg" % where)
    if "run" in row and (row["run"] not in RUN_MODES):
        out.append("%s: run must be one of %s, not %r" % (where, "/".join(RUN_MODES), row["run"]))
    if "extra_steps" in row:
        steps = row["extra_steps"]
        if not (isinstance(steps, list) and all(isinstance(x, str) for x in steps) and len(set(steps)) == len(steps)):
            out.append("%s: extra_steps must be a list of distinct names, not %r" % (where, steps))
        else:
            unknown = sorted(set(steps) - set(EXTRA_STEPS))
            if unknown:
                out.append("%s: extra_steps names %s, outside the closed set %s"
                           % (where, ", ".join(unknown), ", ".join(EXTRA_STEPS)))
    if "moz_log" in row:
        modules = row["moz_log"]
        if not (isinstance(modules, str) and modules and not any(c.isspace() for c in modules)):
            out.append("%s: moz_log must be a non-empty string of MOZ_LOG modules with no space, not %r" % (where, modules))
    if "juggler_trace" in row and not isinstance(row["juggler_trace"], bool):
        out.append("%s: juggler_trace must be true or false, not %r" % (where, row["juggler_trace"]))
    if "artifact" in row:
        name = row["artifact"]
        if not (isinstance(name, str) and name and all(c.isalnum() or c in "._-" for c in name)):
            out.append("%s: artifact must be a non-empty name of letters, digits, '.', '_' and '-', not %r" % (where, name))
    return out


def _read_json_list(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Usage("cannot read the required list %s: %s" % (path, exc))


def load_required(path):
    data = _read_json_list(path)
    problems = validate_list(data)
    if problems:
        raise BadList("the required list %s is malformed:\n  %s" % (path, "\n  ".join(problems)))
    return data


def find_suite(suites, suite_id, os_name):
    if os_name not in OSES:
        raise Usage("--os must be one of %s, not %r" % (", ".join(OSES), os_name))
    rows = [s for s in suites if isinstance(s, dict) and s.get("id") == suite_id]
    if not rows:
        raise Usage("no suite %r in the required list" % suite_id)
    if len(rows) > 1:
        raise Usage("the suite %r appears %d times in the required list" % (suite_id, len(rows)))
    row = rows[0]
    if os_name not in row.get("os", []):
        raise Usage("the suite %r does not run on %s (it runs on: %s)"
                    % (suite_id, os_name, ", ".join(row.get("os", [])) or "nowhere"))
    return row


# ------------------------------------------------------------------------------------------------- the junit
def node_id(classname, name):
    """`tests.test_x` + `test_y[p]` -> `tests/test_x.py::test_y[p]`; a class segment (capitalised) goes after the file."""
    parts = [p for p in (classname or "").split(".") if p]
    module, klass = [], []
    for i, part in enumerate(parts):
        if part[:1].isupper() and module:
            klass = parts[i:]
            break
        module.append(part)
    if not module:
        return name or ""
    return "::".join(["/".join(module) + ".py"] + klass + [name or ""])


def parse_junit(path):
    """-> (list of (node id, outcome) with outcome in passed|failed|errors|skipped, names of the testcases that are not
    xunit2). Only xunit2 is accepted: a testcase with no `classname` cannot be told which file it is in, and the extra
    `file`/`line` attributes are what junit_family=xunit1 writes (pytest 9: it keeps the classname AND adds them)."""
    root = ET.parse(path).getroot()
    out, no_class = [], []
    for case in root.iter("testcase"):
        if not (case.get("classname") or "").strip() or case.get("file") is not None or case.get("line") is not None:
            no_class.append(case.get("name") or "?")
        nid = node_id(case.get("classname"), case.get("name"))
        tags = {child.tag for child in case}
        if "error" in tags:
            outcome = "errors"
        elif "failure" in tags:
            outcome = "failed"
        elif "skipped" in tags:
            outcome = "skipped"
        else:
            outcome = "passed"
        out.append((nid, outcome))
    return out, no_class


def read_junit(path):
    return parse_junit(path)[0]


def _base(nid):
    """The node id without the parameters of its last segment."""
    return nid.split("[", 1)[0]


def required_status(entry, cases):
    """-> None if the required entry is satisfied, else why not. EVERY collected test of the entry must have passed."""
    if "::" in entry:
        mine = [o for n, o in cases if n == entry or _base(n) == entry]
        what = "node id"
    else:
        mine = [o for n, o in cases if n.split("::", 1)[0] == entry]
        what = "file"
    if not mine:
        return "the %s %s did not run (it is not in the junit)" % (what, entry)
    bad = sorted({o for o in mine if o != "passed"})
    if bad:
        return "the %s %s has tests that did not pass (%s)" % (what, entry, ", ".join(bad))
    return None


def sha256_of(path):
    if not path or not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def commit_sha(explicit):
    """--commit, else `git rev-parse HEAD` in the current directory. Never GITHUB_SHA (in a called workflow it is the
    commit of the engine's repository, not of the wrapper)."""
    if explicit:
        return explicit.strip() or None
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30,
                             check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return sha or None


# ------------------------------------------------------------------------------------------------ against main
def _int(value):
    return value if _is_int(value) else 0


RETIRED_NAME = "retired-tests.json"


def validate_retired(data, suite_ids=None):
    """-> the reasons why `data` is not a well-formed list of retired tests (empty if it is). Two kinds of entry, in one list:
    a FILE that is gone ({file, reason, date, tests: {row id: number of tests the file had in that row on main, counted with the row's marker}}), and
    a single TEST whose file stays ({nodeid: "<file>::<test>", suite: row id, count > 0, reason, decided_by, date, from_min_tests: the minimum of
    the row on main when the entry was written, an integer >= count, the same for every entry of a suite}). `suite_ids`, when given, are the row
    ids of main's list: the suite of a nodeid entry must be one of them."""
    if not isinstance(data, list):
        return ["the retired list must be a JSON list"]
    problems, seen, seen_nodes, froms = [], set(), set(), {}
    for i, e in enumerate(data):
        where = "retired entry %d" % i
        if not isinstance(e, dict):
            problems.append("%s is not an object" % where)
            continue
        if "nodeid" in e or "suite" in e:
            problems += _validate_retired_node(e, where, suite_ids, seen_nodes)
            if isinstance(e.get("suite"), str) and _is_int(e.get("from_min_tests")):
                froms.setdefault(e["suite"], set()).add(e["from_min_tests"])
            continue
        f = e.get("file")
        if not isinstance(f, str) or not f.strip():
            problems.append("%s has no file" % where)
        elif f in seen:
            problems.append("%s: the file %s appears more than once" % (where, f))
        else:
            seen.add(f)
            where = "retired %s" % f
        for key in ("reason", "date"):
            if not isinstance(e.get(key), str) or not e.get(key).strip():
                problems.append("%s: %s must be a non-empty string" % (where, key))
        counts = e.get("tests")
        if not (isinstance(counts, dict) and counts and all(isinstance(k, str) and _is_int(v) and v > 0 for k, v in counts.items())):
            problems.append("%s: tests must be {row id: number of tests of the file in that row (> 0)}" % where)
    for suite, values in sorted(froms.items()):
        if len(values) > 1:
            problems.append("the retired tests of %s disagree on from_min_tests (%s): one suite, one floor of main" % (suite, sorted(values)))
    return problems


def _validate_retired_node(e, where, suite_ids, seen):
    problems = []
    nid, suite = e.get("nodeid"), e.get("suite")
    if "file" in e or "tests" in e:
        problems.append("%s: an entry is a retired file (file, tests) or a retired test (nodeid, suite, count), not both" % where)
    parts = nid.split("::", 1) if isinstance(nid, str) else []
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        problems.append("%s: nodeid must be \"<file>::<test>\"" % where)
    else:
        where = "retired test %s" % nid
    if not isinstance(suite, str) or not suite.strip():
        problems.append("%s: suite must be a non-empty string (the id of a row)" % where)
    elif suite_ids is not None and suite not in suite_ids:
        problems.append("%s: the suite %s is not a row of main's required list" % (where, suite))
    if isinstance(nid, str) and isinstance(suite, str):
        if (nid, suite) in seen:
            problems.append("%s: appears more than once for the suite %s" % (where, suite))
        seen.add((nid, suite))
    if not _is_int(e.get("count")) or e.get("count") < 1:
        problems.append("%s: count must be an integer >= 1 (the tests the suite loses)" % where)
    for key in ("reason", "decided_by", "date"):
        if not isinstance(e.get(key), str) or not e.get(key).strip():
            problems.append("%s: %s must be a non-empty string" % (where, key))
    frm = e.get("from_min_tests")
    if not _is_int(frm) or frm < 1 or (_is_int(e.get("count")) and frm < e["count"]):
        problems.append("%s: from_min_tests must be an integer >= count (the minimum of the suite on main when the entry was written)" % where)
    return problems


def retired_of_main(main_required):
    """The retired files and tests of main: ``ci/retired-tests.json`` next to main's list, read from main and never from the judged branch. It may
    not exist (main older than the format): nothing is retired. A malformed one is a problem, never an empty list. -> (entries, problems)."""
    path = Path(main_required).with_name(RETIRED_NAME)
    if not path.is_file():
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], ["the retired list of main %s cannot be read: %s" % (path, exc)]
    try:
        rows = json.loads(Path(main_required).read_text(encoding="utf-8"))
        suite_ids = {r.get("id") for r in rows if isinstance(r, dict)} if isinstance(rows, list) else None
    except (OSError, ValueError):
        suite_ids = None  # the list of main itself is read (and refused) by the caller
    bad = validate_retired(data, suite_ids)
    if bad:
        return [], ["the retired list of main %s is malformed: %s" % (path, "; ".join(bad))]
    return data, []


def _retired_tests_of(row, retired, collected, judged):
    """-> (the number of tests the row may lose because main retired them one by one, problems). An entry counts only if the file of its nodeid is
    in the row of main (a nodeid that the row does not run is not a test the row loses). The judge has the junit of the leg it judges and only
    counts of main, so a declared nodeid that the judged leg still collects is red (and counts for nothing), and the nodeid of another row is taken
    on its count, the leg of that row checking it. An entry counts only while the minimum of the row on main is still its `from_min_tests`: once
    the removal is on main the floor is lower and the entry is inert (it allows nothing and asks nothing)."""
    allowed, problems = 0, []
    listed = row.get("tests", [])
    for e in retired:
        if not (isinstance(e, dict) and isinstance(e.get("nodeid"), str) and e.get("suite") == row.get("id")):
            continue
        nid = e["nodeid"]
        if _int(row.get("min_tests")) != e.get("from_min_tests"):
            continue
        if nid not in listed and nid.split("::", 1)[0] not in listed:
            continue
        if collected is not None and judged == row.get("id") and any(n == nid or _base(n) == nid for n in collected):
            problems.append("the test %s is declared retired for %s, but the suite still collects it: it did not leave" % (nid, row.get("id")))
            continue
        allowed += _int(e.get("count"))
    return allowed, problems


def against_main(current, main, retired=(), exists=os.path.exists, collected=None, judged=None):
    """The list only grows: every suite of main is still here, with no lower minimum, no exact turned off, no lost
    test or OS and the same marker. `main` is read leniently (it may be older than the current format).

    The one way a test leaves a row is `retired` (the list of MAIN, never of the branch): a file that main retired and that no longer exists
    (`exists`, in the checkout judged) is not a lost test, and the minimum of the row may go down by exactly the tests main counted for that file
    in that row (``tests[row id]``), and by no more. A retired file that is still on disk is not excused: it must stay in its rows.

    The other way is a single retired test (``nodeid``, ``suite``, ``count``) whose file stays in the row: the minimum of the suite may go down by
    the ``count`` of its entries, and by no more. ``collected`` (the node ids of the junit of the judged leg) and ``judged`` (that leg's suite) let
    the judge see that a declared nodeid is gone from the leg; of the collection of main it has only the count."""
    problems = []
    if not isinstance(main, list):
        return ["the list of main is not a JSON list: it cannot be compared"]
    retired = list(retired)
    gone = {e["file"]: e["tests"] for e in retired if isinstance(e, dict) and isinstance(e.get("file"), str)
            and isinstance(e.get("tests"), dict) and not exists(e["file"])}
    by_id = {s.get("id"): s for s in current if isinstance(s, dict)}
    for old in main:
        if not isinstance(old, dict):
            continue
        sid = old.get("id")
        new = by_id.get(sid)
        if new is None:
            problems.append("the suite %s of main is gone from the list" % sid)
            continue
        lost = [t for t in old.get("tests", []) if t not in new.get("tests", [])]
        excused = [t for t in lost if t in gone]
        lost = [t for t in lost if t not in gone]
        allowed = sum(_int(gone[t].get(sid)) for t in excused)
        by_test, why = _retired_tests_of(old, retired, collected, judged)
        allowed += by_test
        problems += why
        if _int(new.get("min_tests")) < _int(old.get("min_tests")) - allowed:
            problems.append("the minimum of %s went down: %s on main, %s here%s"
                            % (sid, old.get("min_tests"), new.get("min_tests"),
                               " (retired files and tests account for %d of it)" % allowed if allowed else ""))
        if old.get("exact") is True and new.get("exact") is not True:
            problems.append("the suite %s is no longer exact, as it is on main" % sid)
        if lost:
            problems.append("the suite %s lost %d required test(s) that main has: %s" % (sid, len(lost), ", ".join(lost)))
        lost_os = [o for o in old.get("os", []) if o not in new.get("os", [])]
        if lost_os:
            problems.append("the suite %s no longer runs on %s, where main runs it" % (sid, ", ".join(lost_os)))
        if old.get("marker") != new.get("marker"):
            problems.append("the marker of %s changed: %r on main, %r here" % (sid, old.get("marker"), new.get("marker")))
    return problems


# ------------------------------------------------------------------------------------------------ the commands
def cmd_args(ns):
    row = find_suite(load_required(ns.required), ns.suite, ns.os)
    lines = []
    if ns.with_marker and row.get("marker"):
        lines += ["-m", row["marker"]]
    lines += list(row.get("tests", []))
    # bytes, so that Windows does not turn the newlines into CRLF (a CR would stay glued to the last argument)
    sys.stdout.buffer.write("".join(line + "\n" for line in lines).encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


def _seal_problems(path, result):
    """The seal of the gate must be readable. It has no sha256 of the binary to compare (see the docstring)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as exc:
        result["seal_binary_hash"] = "unreadable"
        return ["the seal %s cannot be read: %s" % (path, exc)]
    result["seal_binary_hash"] = "absent"
    print("note: the seal %s carries no sha256 of the engine binary (an asset's sha256 is the one of the archive): "
          "the binary was not compared with it" % path)
    return []


def _geoip_problems(path, pin_path, result):
    """A leg launched with the GeoIP database of the pin, or it is red.

    ``path`` is the evidence the leg wrote with ``scripts/ci_lab_geoip.py evidence``, run in the very environment pytest was started in
    (``sudo -E`` included): the value of STEALTHFOX_GEOIP_MMDB, whether it is a file, its sha256. Without the variable the core asks GitHub which
    database is the latest on every call, from the python of the test (run 43211837926: a resolver trap counted github.com and api.github.com as
    the browser's), and a leg that reaches a site for its data is not a leg of the gate.
    """
    try:
        evidence = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(evidence, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as exc:
        result["geoip_sha256"] = "unreadable"
        return ["the GeoIP evidence %s cannot be read: %s: the leg did not say which database it ran with" % (path, exc)]
    result["geoip_sha256"] = evidence.get("sha256")
    if not evidence.get("env"):
        return ["the leg launched without STEALTHFOX_GEOIP_MMDB: the core would ask GitHub for its database on every call (the database of "
                "the gate is ci/geoip.json, made by scripts/ci_lab_geoip.py)"]
    if not evidence.get("exists") or not evidence.get("sha256"):
        return ["STEALTHFOX_GEOIP_MMDB=%s is not a file: the leg ran without a database" % evidence.get("env")]
    try:
        pin = json.loads(Path(pin_path).read_text(encoding="utf-8")) if pin_path else None
        expected = pin["mmdb_sha256"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return ["the pin of the GeoIP database %s cannot be read: %s" % (pin_path, exc)]
    if evidence["sha256"] != expected:
        return ["the leg ran with a GeoIP database of sha256 %s, the pin (%s) says %s" % (evidence["sha256"], pin_path, expected)]
    return []


def cmd_judge(ns):
    problems = []
    result = {"suite": ns.suite, "os": ns.os, "commit": commit_sha(ns.commit), "binary_sha256": sha256_of(ns.binary),
              "collected": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0, "missing_tests": [],
              "problems": problems}
    code_on_red = 1
    try:
        suites = load_required(ns.required)
        row = find_suite(suites, ns.suite, ns.os)
    except BadList as exc:
        problems.append(str(exc))
        return _finish(ns, result, 2)
    except Usage as exc:
        problems.append("no result for this row: %s" % exc)
        return _finish(ns, result, code_on_red)

    if result["commit"] is None:
        problems.append("the commit of the wrapper is not known (no --commit and `git rev-parse HEAD` failed here)")
    if result["binary_sha256"] is None:
        problems.append("the binary %s does not exist: a result with no hash of the engine is not a result" % ns.binary)
    if ns.seal:
        problems += _seal_problems(ns.seal, result)
    if ns.geoip:
        problems += _geoip_problems(ns.geoip, ns.geoip_pin, result)

    counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    cases = []
    if not os.path.isfile(ns.junit):
        problems.append("the junit %s is missing: the suite %s did not report on %s" % (ns.junit, ns.suite, ns.os))
    else:
        try:
            cases, no_class = parse_junit(ns.junit)
            if no_class:
                problems.append("the junit is not xunit2: %d testcase(s) have no classname or carry file/line (e.g. %s); the gate must run "
                                "pytest with -o junit_family=xunit2" % (len(no_class), no_class[0]))
        except (ET.ParseError, OSError) as exc:
            problems.append("the junit %s cannot be read: %s" % (ns.junit, exc))
    for _n, outcome in cases:
        counts[outcome] += 1
    collected = len(cases)

    if counts["failed"]:
        problems.append("%d test(s) failed" % counts["failed"])
    if counts["errors"]:
        problems.append("%d test(s) ended in error" % counts["errors"])
    if counts["skipped"]:
        problems.append("%d test(s) were skipped: a required suite allows no skip" % counts["skipped"])
    minimum = row.get("min_tests")
    if collected < minimum:
        problems.append("collected %d test(s), fewer than the minimum %s" % (collected, minimum))
    elif row.get("exact") and collected != minimum:
        problems.append("collected %d test(s), but the suite is exact: %s, not more (a test was added: raise min_tests "
                        "by hand)" % (collected, minimum))
    missing = []
    for entry in row.get("tests", []):
        why = required_status(entry, cases)
        if why:
            missing.append(entry)
            problems.append(why)

    if ns.main_has_no_list and not ns.main_required:
        problems.append("--main-has-no-list without --main-required: it says nothing")
    elif ns.main_required:
        exists = os.path.isfile(ns.main_required)
        if exists and ns.main_has_no_list:
            problems.append("the list of main %s exists, but --main-has-no-list says main has none" % ns.main_required)
        elif exists:
            try:
                retired, retired_problems = retired_of_main(ns.main_required)
                problems += retired_problems
                problems += against_main(suites, _read_json_list(ns.main_required), retired,
                                         collected={n for n, _o in cases}, judged=ns.suite)
            except Usage as exc:
                problems.append(str(exc))
        elif ns.main_has_no_list:
            print("the list of main %s does not exist and the gate declares that main was checked out and has none: "
                  "nothing to compare" % ns.main_required)
        else:
            problems.append("the list of main %s does not exist and main was not declared to have none "
                            "(--main-has-no-list): it is not known what main has, so the list cannot be compared"
                            % ns.main_required)

    result.update(counts, collected=collected, missing_tests=missing)
    return _finish(ns, result, code_on_red)


def _finish(ns, result, code_on_red):
    problems = result["problems"]
    result["verdict"] = "red" if problems else "green"
    if ns.out:
        out = Path(ns.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if problems:
        for p in problems:
            print("VERDICT RED %s/%s: %s" % (ns.suite, ns.os, p))
        return code_on_red
    print("VERDICT GREEN %s/%s: %d collected, %d passed" % (ns.suite, ns.os, result["collected"], result["passed"]))
    return 0


def cmd_complete(ns):
    suites = load_required(ns.required)
    expected = {(s["id"], o) for s in suites for o in s["os"]}
    problems = []
    folder = Path(ns.results)
    if not folder.is_dir():
        problems.append("the results folder %s does not exist: no suite reported" % ns.results)
    found = {}  # (suite, os) -> (path, data)
    for path in sorted(folder.rglob("*.json")) if folder.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append("%s cannot be read as a result: %s" % (path, exc))
            continue
        if not (isinstance(data, dict) and isinstance(data.get("suite"), str) and isinstance(data.get("os"), str)):
            problems.append("%s is not a result of judge (no suite and os)" % path)
            continue
        key = (data["suite"], data["os"])
        if key not in expected:
            problems.append("%s is the result of %s/%s, which is not in the required list" % (path, key[0], key[1]))
            continue
        if key in found:
            problems.append("%s/%s has two results: %s and %s" % (key[0], key[1], found[key][0], path))
            continue
        found[key] = (path, data)
    for suite, os_name in sorted(expected - set(found)):
        problems.append("%s/%s has no result: the leg did not report" % (suite, os_name))
    for key, (path, data) in sorted(found.items()):
        if data.get("verdict") != "green":
            why = data.get("problems") or ["no verdict in the result"]
            problems.append("%s/%s is not green (%s): %s" % (key[0], key[1], data.get("verdict"), "; ".join(map(str, why))))
        elif data.get("problems"):
            problems.append("%s/%s says green but lists problems: %s" % (key[0], key[1], "; ".join(map(str, data["problems"]))))
    commits = {}
    shas = {}
    for key, (_path, data) in found.items():
        commits.setdefault(data.get("commit"), []).append("%s/%s" % key)
        shas.setdefault(key[1], {}).setdefault(data.get("binary_sha256"), []).append("%s/%s" % key)
    if None in commits:
        problems.append("no commit in the result of: %s" % ", ".join(sorted(commits[None])))
    if len([c for c in commits if c]) > 1:
        problems.append("the legs ran at different commits: %s"
                        % "; ".join("%s: %s" % (c, ", ".join(sorted(v))) for c, v in sorted(commits.items(), key=str)))
    for os_name, by_sha in sorted(shas.items()):
        if None in by_sha:
            problems.append("no binary sha256 in the result of: %s" % ", ".join(sorted(by_sha[None])))
        if len([s for s in by_sha if s]) > 1:
            problems.append("the %s legs ran with different binaries: %s" % (
                os_name, "; ".join("%s: %s" % (s, ", ".join(sorted(v))) for s, v in sorted(by_sha.items(), key=str))))
    if problems:
        for p in problems:
            print("COMPLETE RED: %s" % p)
        return 1
    print("COMPLETE GREEN: %d result(s), all green, one commit, one binary per OS" % len(found))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("args", help="print the pytest arguments of a suite")
    a.add_argument("--required", required=True)
    a.add_argument("--suite", required=True)
    a.add_argument("--os", required=True)
    a.add_argument("--with-marker", action="store_true", help="print -m and the marker of the suite first")
    j = sub.add_parser("judge", help="judge the junit of a suite")
    j.add_argument("--required", required=True)
    j.add_argument("--suite", required=True)
    j.add_argument("--os", required=True)
    j.add_argument("--junit", required=True)
    j.add_argument("--binary")
    j.add_argument("--commit")
    j.add_argument("--seal")
    j.add_argument("--geoip", help="the evidence of the leg (scripts/ci_lab_geoip.py evidence): a leg launched without the database is red")
    j.add_argument("--geoip-pin", help="ci/geoip.json: the sha256 of the database the leg has to have run with (main's, when main has it)")
    j.add_argument("--out")
    j.add_argument("--main-required")
    j.add_argument("--main-has-no-list", action="store_true",
                   help="the gate checked main out and the list is not there (a failed checkout is not this)")
    c = sub.add_parser("complete", help="check that every (suite, os) reported green at one commit")
    c.add_argument("--required", required=True)
    c.add_argument("--results", required=True)
    ns = ap.parse_args(argv)
    try:
        if ns.cmd == "judge":
            if not ns.binary:
                raise Usage("judge needs --binary (the engine the suite ran against: its hash goes in the result)")
            return cmd_judge(ns)
        if ns.cmd == "complete":
            return cmd_complete(ns)
        return cmd_args(ns)
    except Usage as exc:
        print("ci_verdict: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
