"""The rows of ``ci/required-suites.json`` carry what the gate needs to build the leg of each suite (runner image, timeout, user namespaces, core
dumps, stock Firefox, location service, how the suite runs, extra steps, extra MOZ_LOG modules, the Juggler trace, the artifact name): the gate derives its
matrix from the registry, so a detail of a leg that is not a field of the row is a detail nobody can read.

* the list validation (``scripts/ci_verdict.py``) accepts a row with the fields and refuses, naming the row and the field, a wrong type, a value outside
  a closed set, an image for an OS the row does not have, a core-dump setting on a row with no Linux leg;
* a field that is not known stays ignored, as before;
* the registry as committed carries exactly the fields of ``EXPECTED`` on exactly those rows: a change is made here by hand, on purpose.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = ROOT / "ci" / "required-suites.json"

FIELDS = ("runner_image", "timeout_minutes", "needs_userns", "core_dumps", "stock_firefox", "location_service", "run", "extra_steps", "moz_log",
          "juggler_trace", "artifact")
WIN2022 = {"windows": "windows-2022"}

#: The rows that carry any of the leg fields, and the value of each. Every other row carries none (the plain leg).
EXPECTED = {
    "wrapper.e2e.udp.sandbox": {"needs_userns": True},
    "wrapper.e2e.web": {"moz_log": "nsHttp:5,neqo_http3:5"},
    "wrapper.e2e.page": {"moz_log": "DocumentChannel:5,ProcessIsolation:5,nsDocShell:4,DocLoader:4", "juggler_trace": True},
    "wrapper.e2e.compositing": {"runner_image": WIN2022},
    "wrapper.e2e.fakeweb": {"stock_firefox": True, "extra_steps": ["fakeweb_profiles"]},
    "wrapper.e2e.securitystate": {"core_dumps": "invpw-cores"},
    "wrapper.e2e.suite.linux.1": {"run": "suite_run"},
    "wrapper.e2e.suite.linux.2": {"run": "suite_run"},
    "wrapper.e2e.suite.linux.3": {"run": "suite_run"},
    "wrapper.e2e.suite.windows.1": {"runner_image": WIN2022, "run": "suite_run"},
    "wrapper.e2e.suite.windows.2": {"runner_image": WIN2022, "run": "suite_run"},
    "wrapper.e2e.suite.windows.3": {"runner_image": WIN2022, "run": "suite_run"},
    "wrapper.e2e.suite.windows.4": {"runner_image": WIN2022, "run": "suite_run"},
    "wrapper.e2e.suite.windows.control_a": {"run": "suite_run"},
    "wrapper.e2e.ab_oracle": {"timeout_minutes": 50, "core_dumps": "cores", "extra_steps": ["keep_oracle_files"], "artifact": "ab-oracle"},
    "wrapper.e2e.ab_network": {"timeout_minutes": 75, "core_dumps": "cores", "extra_steps": ["keep_oracle_files"]},
    "wrapper.e2e.ab_host": {"runner_image": WIN2022, "timeout_minutes": 50, "core_dumps": "cores", "stock_firefox": ["windows"],
                            "location_service": ["windows"], "extra_steps": ["keep_oracle_files"]},
    "wrapper.e2e.ab_host_crash": {"timeout_minutes": 35, "core_dumps": "cores", "extra_steps": ["keep_oracle_files"]},
}


def _load():
    spec = importlib.util.spec_from_file_location("ci_verdict_for_the_fields", ROOT / "scripts" / "ci_verdict.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verdict = _load()


def _row(**over):
    row = {"id": "s.one", "giro": "G2", "os": ["linux", "windows"], "min_tests": 1, "tests": ["tests/test_a.py"], "marker": "e2e",
           "needs_root": False, "exact": True, "note": ""}
    row.update(over)
    return row


def _problems(**over):
    return verdict.validate_list([_row(**over)])


def test_a_row_with_every_field_is_accepted():
    assert _problems(runner_image=WIN2022, timeout_minutes=50, needs_userns=True, core_dumps="cores", stock_firefox=["windows"],
                     location_service=["windows"], run="suite_run", extra_steps=["fakeweb_profiles", "keep_oracle_files"],
                     moz_log="nsHttp:5,neqo_http3:5", juggler_trace=True, artifact="ab-oracle") == []


def test_a_row_without_the_fields_is_the_plain_leg():
    assert _problems() == []
    assert _problems(core_dumps=False, needs_userns=False, run="pytest", extra_steps=[]) == []


@pytest.mark.parametrize("field, value", [
    ("runner_image", "windows-2022"),
    ("runner_image", {}),
    ("runner_image", {"windows": ""}),
    ("runner_image", {"windows": 2022}),
    ("timeout_minutes", "50"),
    ("timeout_minutes", 0),
    ("timeout_minutes", True),
    ("timeout_minutes", 361),
    ("needs_userns", "yes"),
    ("needs_userns", ["macos"]),
    ("stock_firefox", 1),
    ("location_service", ["windows", 3]),
    ("core_dumps", "core"),
    ("core_dumps", True),
    ("core_dumps", ["cores"]),
    ("run", "xdist"),
    ("run", True),
    ("extra_steps", "keep_oracle_files"),
    ("extra_steps", ["keep_oracle_files", "keep_oracle_files"]),
    ("extra_steps", [1]),
    ("moz_log", ""),
    ("moz_log", "nsHttp:5, neqo_http3:5"),
    ("moz_log", ["nsHttp:5"]),
    ("juggler_trace", "true"),
    ("artifact", ""),
    ("artifact", "a b"),
    ("artifact", 7),
])
def test_a_known_field_with_a_bad_value_is_refused_by_name(field, value):
    problems = _problems(**{field: value})
    assert problems and all("s.one" in p and field in p for p in problems), problems


def test_an_extra_step_outside_the_closed_set_is_refused():
    problems = _problems(extra_steps=["fakeweb_profiles", "deploy_to_prod"])
    assert len(problems) == 1 and "deploy_to_prod" in problems[0] and "closed set" in problems[0]


def test_an_image_for_an_os_the_row_does_not_have_is_refused():
    problems = _problems(os=["linux"], runner_image=WIN2022)
    assert len(problems) == 1 and "runner_image" in problems[0] and "windows" in problems[0]
    assert _problems(os=["windows"], runner_image=WIN2022) == []
    assert _problems(runner_image={"macos": "macos-14"})


def test_an_os_flag_for_an_os_the_row_does_not_have_is_refused():
    for field in ("needs_userns", "stock_firefox", "location_service"):
        assert _problems(os=["linux"], **{field: ["windows"]}), field
        assert _problems(os=["linux"], **{field: ["linux"]}) == [], field


def test_a_core_dump_setting_on_a_row_with_no_linux_leg_is_refused():
    problems = _problems(os=["windows"], core_dumps="cores")
    assert len(problems) == 1 and "core_dumps" in problems[0] and "linux" in problems[0]
    assert _problems(os=["windows"], core_dumps=False) == []


def test_a_name_that_is_not_a_field_stays_ignored():
    assert _problems(some_future_field={"x": 1}, linux_only=True, note="free text") == []


def test_the_registry_as_committed_is_well_formed_and_carries_exactly_the_fields_of_the_table():
    rows = json.loads(REQUIRED.read_text(encoding="utf-8"))
    assert verdict.validate_list(rows) == []
    found = {r["id"]: {f: r[f] for f in FIELDS if f in r} for r in rows}
    found = {sid: fields for sid, fields in found.items() if fields}
    assert found == EXPECTED
