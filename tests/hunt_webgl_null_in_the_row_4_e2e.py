"""TEMPORARY, with .github/workflows/webgl-null-in-the-row-hunt.yml (deleted with its branch, never merged to main). Not collected by any run: the name has no `test_` prefix,
so only a command line that NAMES this file collects it (the hunt's). 15 cases of 60: each runs the real test of tests/test_canvas_reads_as_drawn.py (tests/_hunt_webgl_null.py)."""
import pytest

from _hunt_webgl_null import CASES_PER_FILE, run_case

SHARD = 4


@pytest.mark.e2e
@pytest.mark.parametrize("case", range(CASES_PER_FILE))
def test_the_real_webgl_test_beside_the_row_4(firefox_binary, case):
    run_case(firefox_binary, SHARD, case)
