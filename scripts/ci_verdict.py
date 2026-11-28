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
        the row is `exact`, any number other than `min_tests` (a deselected test is not in the junit: a fragment is
        added when a test is added, never silently); a required file or node id did not pass; the binary is
        missing (--binary is mandatory: without it exit 2) or the commit cannot be known; the --seal is unreadable.
        With --main-required (the list of main) the list only grows, on EFFECTIVE rows on both sides (see the fragments, below): a suite of
        main is gone, its minimum went down, its `exact` went from true to false, its os or tests lost an element, or its marker changed is red.
        Main's fragments are read from the folder `required-suites.d` NEXT TO main's list; main's list without that folder is red ("main's
        fragments are not known"). A fragment of main that is missing or changed in the judged tree is allowed only as a FOLD: every effective row
        identical to main's, consumed.json gaining exactly the `consumes` of the removed fragments, and no fragment that main lacks.
        The one exception is `retired-tests.json` NEXT TO main's list (read from main, never from the judged branch): a file it names that
        no longer exists in the checkout may leave the rows, and the minimum of a row may go down by the tests main counted for it in that
        row (`tests: {row id: n}`), and by no more. A retired file still on disk, or any other lost test, stays red.
        The same file may also retire single tests whose file stays: {"nodeid": "<file>::<test>", "suite": <row id>, "count": n, "reason",
        "decided_by", "date"} (`from_min_tests` is optional and ignored). A single test leaves a row by a fragment of the branch that carries a
        NEGATIVE value for the row and `consumes` the nodeid: each consumed nodeid is a nodeid entry of main's list for that row, is not in main's
        consumed.json nor in a fragment of main, and the judged leg no longer collects it; |value| is at most the sum of the `count` of the
        consumed entries for that row, and only for a nodeid whose file the row of main lists. A retirement is consumed once, by name; a declared
        nodeid that the judged leg still collects is red, and so is any test that goes without an entry.
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

    ci_verdict.py fragment --required ci/required-suites.json --name <slug> --set ROW=N [--set ROW=N ...] [--consumes NODEID ...]
                           [--main-ref origin/main]
        writes ci/required-suites.d/<slug>.json ({row id: N}, plus `consumes` when given) and prints its path. If the name already exists in
        the working tree or on the main ref (`git ls-tree`), it takes -2, -3, ...; it NEVER edits an existing fragment.

THE FLOOR IS A SUM OF PARTS. The effective `min_tests` of a row is the `min_tests` of `ci/required-suites.json` plus the sum of the values of the
fragments in the folder `required-suites.d` next to the list (one loader, `effective_rows`; `args`, `judge` and `complete` read effective rows). The
folder always exists: it holds `consumed.json` (a JSON list of the retired nodeids already consumed) and one file `<slug>.json` per change
(the name starts with a lower-case letter or a digit, then only a-z 0-9 . _ -, and ends in .json; consumed.json is not a fragment), whose content is {"<row id>": integer, ..., optional "consumes": [nodeid, ...]}. A value is an
integer other than 0; a negative one is allowed only with `consumes`. A missing folder, a bad name, an unknown row id, a value that is not an
integer, 0, or a negative value without `consumes` is a malformed list (exit 2, with the reason).

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
import re
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


# ------------------------------------------------------------------------------------------------ the fragments
FRAGMENTS_DIR = "required-suites.d"
CONSUMED_NAME = "consumed.json"
CONSUMES = "consumes"
FRAGMENT_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*\.json$")


class Required:
    """A required list read with its fragments. `rows` are the EFFECTIVE rows (`min_tests` = the base + the sum of the fragments), `base` the
    rows as the list has them, `fragments` {slug: {"values": {row id: int}, "consumes": [nodeid]}}, `consumed` the list of consumed.json."""

    def __init__(self, base, rows, fragments, consumed):
        self.base, self.rows, self.fragments, self.consumed = base, rows, fragments, consumed


def fragments_dir(required_path):
    """The folder of the fragments of a list: `required-suites.d` next to it."""
    return Path(required_path).with_name(FRAGMENTS_DIR)


def _read_fragments(folder, row_ids):
    """-> (fragments, consumed, problems) of the folder. Every reason it is malformed is in `problems`."""
    folder = Path(folder)
    if not folder.is_dir():
        return {}, [], ["the folder %s does not exist (it always holds %s)" % (folder, CONSUMED_NAME)]
    problems, fragments, consumed = [], {}, []
    names = sorted(os.listdir(folder))
    if CONSUMED_NAME not in names:
        problems.append("%s has no %s" % (folder, CONSUMED_NAME))
    for name in names:
        path = folder / name
        if name == CONSUMED_NAME:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                problems.append("%s cannot be read: %s" % (path, exc))
                continue
            if not (isinstance(data, list) and all(isinstance(n, str) and n.strip() for n in data) and len(set(data)) == len(data)):
                problems.append("%s must be a list of distinct non-empty nodeids" % path)
            else:
                consumed = data
            continue
        if not path.is_file() or not FRAGMENT_NAME.match(name):
            problems.append("%s: the name of a fragment is <slug>.json (lower-case letters, digits, '.', '_' and '-', starting with a letter or "
                            "a digit), and %s is not a fragment" % (path, CONSUMED_NAME))
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append("%s cannot be read: %s" % (path, exc))
            continue
        fragment, bad = _parse_fragment(data, row_ids)
        problems += ["fragment %s: %s" % (name, why) for why in bad]
        if not bad:
            fragments[name[:-len(".json")]] = fragment
    return fragments, consumed, problems


def _parse_fragment(data, row_ids):
    if not isinstance(data, dict):
        return None, ["a fragment is a JSON object {row id: integer}"]
    problems, values, consumes = [], {}, []
    for key, value in data.items():
        if key == CONSUMES:
            if not (isinstance(value, list) and all(isinstance(n, str) and n.strip() for n in value) and len(set(value)) == len(value)):
                problems.append("%s must be a list of distinct non-empty nodeids, not %r" % (CONSUMES, value))
            else:
                consumes = list(value)
        elif key not in row_ids:
            problems.append("%r is not a row of the list" % key)
        elif not _is_int(value):
            problems.append("the value of %s must be an integer, not %r" % (key, value))
        elif value == 0:
            problems.append("the value of %s is 0: a fragment that changes nothing" % key)
        else:
            values[key] = value
    if not values and not problems:
        problems.append("no row: a fragment names at least one row")
    for key, value in sorted(values.items()):
        if value < 0 and not consumes:
            problems.append("the value of %s is negative (%d) and the fragment has no `%s`: a test leaves a row only by a retirement it consumes"
                            % (key, value, CONSUMES))
    return {"values": values, "consumes": consumes}, problems


def _effective(base, fragments):
    rows = []
    for row in base:
        if isinstance(row, dict):
            row = dict(row)
            row["min_tests"] = _int(row.get("min_tests")) + sum(f["values"].get(row.get("id"), 0) for f in fragments.values())
        rows.append(row)
    return rows


def _bad_list(head, reasons):
    """A BadList whose message is `head` and then one reason per line."""
    return BadList(head + "".join("\n  " + reason for reason in reasons))


def read_required(path):
    """The list and its fragments, strictly: a list or a folder that is malformed is a BadList (exit 2), naming every reason."""
    base = _read_json_list(path)
    problems = validate_list(base)
    if problems:
        raise _bad_list("the required list %s is malformed:" % path, problems)
    fragments, consumed, bad = _read_fragments(fragments_dir(path), {r["id"] for r in base})
    if bad:
        raise _bad_list("the fragments of the required list %s are malformed:" % path, bad)
    rows = _effective(base, fragments)
    low = ["%s: the effective min_tests is %d (> 0 needed)" % (r["id"], r["min_tests"]) for r in rows if r["min_tests"] <= 0]
    if low:
        raise _bad_list("the fragments of the required list %s leave a row with no test:" % path, low)
    return Required(base, rows, fragments, consumed)


def effective_rows(required_path):
    """THE loader: the rows of the list with `min_tests` = the base + the sum of the fragments of the folder next to it."""
    return read_required(required_path).rows


def read_main_required(path):
    """The list of main and its fragments, leniently (main may be older than the format): -> (Required or None, problems). Main's list without
    its folder is a problem ("main's fragments are not known"), never an empty folder."""
    base = _read_json_list(path)
    if not isinstance(base, list):
        return None, ["the list of main is not a JSON list: it cannot be compared"]
    folder = fragments_dir(path)
    if not folder.is_dir():
        return None, ["main's fragments are not known: the list of main %s has no folder %s next to it" % (path, FRAGMENTS_DIR)]
    ids = {r.get("id") for r in base if isinstance(r, dict)}
    fragments, consumed, bad = _read_fragments(folder, ids)
    if bad:
        return None, ["main's fragments are not readable: %s" % "; ".join(bad)]
    return Required(base, _effective(base, fragments), fragments, consumed), []


def load_required(path):
    return effective_rows(path)


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
    a single TEST whose file stays ({nodeid: "<file>::<test>", suite: row id, count > 0, reason, decided_by, date; optional and ignored, from_min_tests: the minimum of
    the row on main when the entry was written, an integer >= count, the same for every entry of a suite that has one}). `suite_ids`, when given, are the row
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
    if "from_min_tests" in e:  # optional and ignored: the retirement is consumed by name now (see against_main); an old entry keeps it
        frm = e["from_min_tests"]
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


def _retirement_allowance(current, main, retired, collected, judged):
    """-> ({row id: how many tests the rows may lose by single-test retirements}, problems). Only the fragments the branch ADDS count (a fragment of
    main is already in main's effective rows). A fragment may carry a negative value for a row only with `consumes`, and only if: each consumed nodeid
    is a nodeid entry of main's retired-tests.json for one of its negative rows; it is not in main's consumed.json nor in a fragment of main (a
    retirement is used once, whatever merges in between), nor in another fragment of the branch; the judged leg no longer collects it; and |value|
    is at most the sum of the `count` of the consumed entries of that row whose file or node id the row of main lists."""
    allowed, problems = {}, []
    entries = [e for e in retired if isinstance(e, dict) and isinstance(e.get("nodeid"), str) and isinstance(e.get("suite"), str)]
    taken = {n: "main's %s" % CONSUMED_NAME for n in main.consumed}
    for name, frag in main.fragments.items():
        for n in frag["consumes"]:
            taken.setdefault(n, "the fragment %s of main" % name)
    main_rows = {r.get("id"): r for r in main.base if isinstance(r, dict)}
    for name in sorted(set(current.fragments) - set(main.fragments)):
        frag = current.fragments[name]
        negative = {row: v for row, v in frag["values"].items() if v < 0}
        usable = {row: 0 for row in negative}
        for nid in frag["consumes"]:
            if nid in taken:
                problems.append("the fragment %s consumes the retirement of %s, which %s has already consumed: a retirement is used once"
                                % (name, nid, taken[nid]))
                continue
            taken[nid] = "the fragment %s" % name
            mine = [e for e in entries if e["nodeid"] == nid and e["suite"] in negative]
            if not mine:
                problems.append("the fragment %s consumes %s, which is not a retired test of main's %s for any row it lowers (%s)"
                                % (name, nid, RETIRED_NAME, ", ".join(sorted(negative)) or "none"))
            for e in mine:
                row = main_rows.get(e["suite"])
                listed = row.get("tests", []) if isinstance(row, dict) else []
                if nid not in listed and nid.split("::", 1)[0] not in listed:
                    continue  # a nodeid the row of main does not run is not a test the row loses
                if collected is not None and judged == e["suite"] and any(n == nid or _base(n) == nid for n in collected):
                    problems.append("the test %s is declared retired for %s, but the suite still collects it: it did not leave" % (nid, e["suite"]))
                    continue
                usable[e["suite"]] += _int(e.get("count"))
        for row, value in sorted(negative.items()):
            if -value > usable[row]:
                problems.append("the fragment %s lowers %s by %d, but the retired tests it consumes account for %d of it"
                                % (name, row, -value, usable[row]))
            else:
                allowed[row] = allowed.get(row, 0) - value
    return allowed, problems


def _fold_problems(current, main, removed):
    """The only way a fragment of main leaves: the fold. Every effective row is identical to main's, consumed.json gains exactly the `consumes` of the
    removed fragments, and the branch has no fragment that main lacks."""
    problems = []
    cur = {r.get("id"): r for r in current.rows if isinstance(r, dict)}
    old = {r.get("id"): r for r in main.rows if isinstance(r, dict)}
    for sid in sorted(set(cur) | set(old), key=str):
        if cur.get(sid) != old.get(sid):
            problems.append("fragments of main were removed outside a fold: the effective row %s is not the one of main (%s on main, %s here)"
                            % (sid, old[sid].get("min_tests") if sid in old else "absent", cur[sid].get("min_tests") if sid in cur else "absent"))
    expected = set(main.consumed) | {n for name in removed for n in main.fragments[name]["consumes"]}
    if set(current.consumed) != expected:
        problems.append("a fold moves the `%s` of the removed fragments into %s and nothing else: expected %d nodeid(s), found %d (missing: %s; extra: %s)"
                        % (CONSUMES, CONSUMED_NAME, len(expected), len(current.consumed), ", ".join(sorted(expected - set(current.consumed))) or "none",
                           ", ".join(sorted(set(current.consumed) - expected)) or "none"))
    added = sorted(set(current.fragments) - set(main.fragments))
    if added:
        problems.append("a fold carries no new fragment, and the branch adds: %s" % ", ".join(added))
    return problems


def against_main(current, main, retired=(), exists=os.path.exists, collected=None, judged=None):
    """The list only grows, on EFFECTIVE rows (the base + the fragments) on both sides: every suite of main is still here, with no lower minimum, no
    exact turned off, no lost test or OS and the same marker. `current` and `main` are `Required`; main is read leniently (it may be older than the
    current format).

    A fragment of main is never edited. It may leave (be missing here) only in a fold (`_fold_problems`); outside a fold consumed.json is main's.

    The one way a test leaves a row is `retired` (the list of MAIN, never of the branch): a file that main retired and that no longer exists
    (`exists`, in the checkout judged) is not a lost test, and the minimum of the row may go down by exactly the tests main counted for that file
    in that row (``tests[row id]``), and by no more. A retired file that is still on disk is not excused: it must stay in its rows.

    The other way is a single retired test (``nodeid``, ``suite``, ``count``) whose file stays in the row, consumed by a fragment of the branch with a
    negative value (`_retirement_allowance`). ``collected`` (the node ids of the junit of the judged leg) and ``judged`` (that leg's suite) let
    the judge see that a declared nodeid is gone from the leg; of the collection of main it has only the count."""
    problems = []
    retired = list(retired)
    changed = sorted(n for n in main.fragments if n in current.fragments and current.fragments[n] != main.fragments[n])
    removed = sorted(n for n in main.fragments if n not in current.fragments)
    for name in changed:
        problems.append("the fragment %s of main was changed here: a fragment is never edited" % name)
    if removed:
        problems += _fold_problems(current, main, removed)
    elif set(current.consumed) != set(main.consumed):
        problems.append("%s differs from main's outside a fold (a retirement is consumed by a fragment, and only a fold moves it)" % CONSUMED_NAME)
    gone = {e["file"]: e["tests"] for e in retired if isinstance(e, dict) and isinstance(e.get("file"), str)
            and isinstance(e.get("tests"), dict) and not exists(e["file"])}
    by_test, why = _retirement_allowance(current, main, retired, collected, judged)
    problems += why
    by_id = {s.get("id"): s for s in current.rows if isinstance(s, dict)}
    for old in main.rows:
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
        allowed = sum(_int(gone[t].get(sid)) for t in excused) + by_test.get(sid, 0)
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
        required = read_required(ns.required)
        suites = required.rows
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
        problems.append("collected %d test(s), but the suite is exact: %s, not more (a test was added: add a fragment, "
                        "`ci_verdict.py fragment --name <slug> --set %s=N`)" % (collected, minimum, ns.suite))
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
                main_required, main_problems = read_main_required(ns.main_required)
                problems += main_problems
                if main_required is not None:
                    problems += against_main(required, main_required, retired, collected={n for n, _o in cases}, judged=ns.suite)
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


def _names_on_ref(folder, ref):
    """-> the file names of `folder` on the git ref `ref` (empty when the folder is not there on that ref). A ref git cannot read is a usage error:
    a name that main may already have must never be guessed."""
    try:
        done = subprocess.run(["git", "-C", str(folder.parent), "ls-tree", "--name-only", ref, "--", folder.name + "/"],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Usage("cannot ask git for %s: %s" % (ref, exc))
    if done.returncode != 0:
        raise Usage("git cannot list %s/ on %s (%s): fetch it, or name another ref with --main-ref"
                    % (folder.name, ref, done.stderr.strip() or "exit %d" % done.returncode))
    return {Path(line.strip()).name for line in done.stdout.splitlines() if line.strip()}


def cmd_fragment(ns):
    """Write the fragment of this change: a NEW file, never an edit of one that exists (here or on main)."""
    slug = ns.name
    if not re.match(r"^[a-z0-9][a-z0-9._-]*$", slug or "") or (slug + ".json") == CONSUMED_NAME:
        raise Usage("--name %r is not a slug: lower-case letters, digits, '.', '_' and '-', starting with a letter or a digit (and not 'consumed')" % slug)
    base = _read_json_list(ns.required)
    problems = validate_list(base)
    if problems:
        raise _bad_list("the required list %s is malformed:" % ns.required, problems)
    folder = fragments_dir(ns.required)
    if not folder.is_dir():
        raise Usage("the folder %s does not exist" % folder)
    ids = {row["id"] for row in base}
    content = {}
    for item in ns.set or []:
        row, _eq, number = item.rpartition("=")
        if not _eq or row not in ids:
            raise Usage("--set %r is not ROW=N with a row of the list" % item)
        try:
            value = int(number)
        except ValueError:
            raise Usage("--set %r: N must be an integer" % item)
        if row in content:
            raise Usage("--set names %s twice" % row)
        content[row] = value
    consumes = list(ns.consumes or [])
    fragment, bad = _parse_fragment(dict(content, **({CONSUMES: consumes} if consumes else {})), ids)
    if bad:
        raise Usage("the fragment would be malformed: %s" % "; ".join(bad))
    taken = {name for name in os.listdir(folder)} | _names_on_ref(folder, ns.main_ref)
    name, n = slug, 1
    while name + ".json" in taken:
        n += 1
        name = "%s-%d" % (slug, n)
    path = folder / (name + ".json")
    out = dict(content)
    if consumes:
        out[CONSUMES] = consumes
    with open(path, "xb") as fh:  # "x": never over a file that is there
        fh.write((json.dumps(out, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
    print(path)
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
    f = sub.add_parser("fragment", help="write the fragment of a change (a new file in required-suites.d)")
    f.add_argument("--required", required=True)
    f.add_argument("--name", required=True, help="the slug of the branch; -2, -3, ... is added if the name exists here or on main")
    f.add_argument("--set", action="append", metavar="ROW=N", help="the tests this change adds (N > 0) to a row; repeat for more rows")
    f.add_argument("--consumes", action="append", metavar="NODEID", help="the retired test a negative N consumes; repeat")
    f.add_argument("--main-ref", default="origin/main", help="the ref whose fragments the name must not collide with (default origin/main)")
    ns = ap.parse_args(argv)
    try:
        if ns.cmd == "judge":
            if not ns.binary:
                raise Usage("judge needs --binary (the engine the suite ran against: its hash goes in the result)")
            return cmd_judge(ns)
        if ns.cmd == "fragment":
            return cmd_fragment(ns)
        if ns.cmd == "complete":
            return cmd_complete(ns)
        return cmd_args(ns)
    except Usage as exc:
        print("ci_verdict: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
