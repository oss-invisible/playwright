"""No workflow sets an intermediates list any more: the legs that did set the security state in its place.

The step "The intermediates list (a fixed file of the repository)" of e2e.yml, fakeweb.yml and tests.yml became "The security state (a fixed
directory of the repository)" (tests/test_ci_workflows_set_the_security_state.py says where and how). The file keeps its name because
ci/required-suites.json only grows (scripts/ci_verdict.py, against main): its test now guards that no leg goes back to the old step.
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
COMMAND = "python scripts/ci_lab_security_state.py set --pin ci/security-state.pin.json"


@pytest.mark.parametrize("name", ["e2e.yml", "fakeweb.yml", "tests.yml"])
def test_the_leg_sets_the_security_state_and_no_intermediates_list(name):
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    assert "- name: The intermediates list" not in text, "%s still has the intermediates step" % name
    assert COMMAND in text, "%s does not set the security state" % name
