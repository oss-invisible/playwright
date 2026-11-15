"""The synthetic bold of a fallback face is the one Windows makes: CJK and emoji text in a bold font that has no such glyphs.

The glyphs come from a fallback face that has no bold, so the engine makes it bold. With idwr in the canvas path every other draw
is the same bytes on Linux and Windows, and these 6 are not: on Linux the bold is an emboldening of the outline, on Windows it is
DWrite's bold simulation, which also widens the advance. Windows is 0.1 to 1.7 px wider and the ink differs (run 43744178165:
bold 16px Arial, bold 40px sans-serif, bold 44px Arial, each with CJK and emoji). Linux must do what DWrite's simulation does,
the advance included. Red on Linux until the synthetic bold is ported; it comes with that port. On Windows it holds already.
"""
from __future__ import annotations

import pytest

from test_text_bytes_are_the_same_on_every_host_e2e import (HOLDS_SYNTHETIC_BOLD, _draw_problems, _key_problems,  # noqa: F401
                                                            measured, reference, synthetic_bold_of_a_fallback)

pytestmark = pytest.mark.e2e


def test_the_synthetic_bold_of_a_fallback_face_draws_the_reference_bytes(measured, reference):
    _, draws = measured
    problems = _draw_problems(draws, reference, synthetic_bold_of_a_fallback)
    assert not problems, "the synthetic bold of a fallback face is not Windows' (%d draw(s)):\n  %s" % (len(problems), "\n  ".join(problems))


def test_the_composite_text_keys_that_hold_it_are_the_reference(measured, reference):
    entries, _ = measured
    only = {k: v for k, v in reference["entries"].items() if k.startswith(HOLDS_SYNTHETIC_BOLD)}
    problems = _key_problems(entries, {"entries": only})
    assert not problems, "%d composite text key(s) differ:\n  %s" % (len(problems), "\n  ".join(problems))
