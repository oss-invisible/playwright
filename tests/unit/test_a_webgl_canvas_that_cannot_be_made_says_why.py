"""A WebGL test that gets no context writes down why, and its verdict stays the first attempt's. No browser.

tests/test_canvas_reads_as_drawn.py failed once on the engine-off launch with "webgl is unavailable: a WebGL canvas cannot be read" and nothing else was left of
the run (the probe returned null). The probe now returns what the canvas heard on `webglcontextcreationerror`; these tests hold how that result is read.
"""
import _webgl_unavailable as unavailable
import pytest

pytestmark = pytest.mark.unit

NO_CONTEXT = {"unavailable": True, "statusMessages": ["FEATURE_FAILURE_WEBGL_EXHAUSTED_DRIVERS"],
              "webglInterface": "function", "webgl2Interface": "function"}
PIXELS = {"url": "data:image/png;base64,AAAA", "blob": "AAAA", "premultipliedAlpha": False}


def test_a_probe_without_a_context_is_the_old_null_with_its_record():
    got, first = unavailable.split_probe(NO_CONTEXT)
    assert got is None
    assert first is NO_CONTEXT


def test_a_probe_with_a_context_is_read_as_before():
    got, first = unavailable.split_probe(PIXELS)
    assert got is PIXELS
    assert first is None


def test_a_probe_that_returns_nothing_is_still_no_context_but_has_no_record():
    # an old page or a failed evaluate: `None` is read as `None`, as it always was
    assert unavailable.split_probe(None) == (None, None)


def test_the_message_keeps_the_old_sentence_and_appends_both_attempts():
    second = {"statusMessages": [], "created": True, "renderer": "llvmpipe"}
    message = unavailable.unavailable_message(NO_CONTEXT, second)
    assert message.startswith("webgl is unavailable: a WebGL canvas cannot be read | first: ")
    assert message == ("webgl is unavailable: a WebGL canvas cannot be read | first: %r; second after 2 s: %r" % (NO_CONTEXT, second))
    assert "FEATURE_FAILURE_WEBGL_EXHAUSTED_DRIVERS" in message and "llvmpipe" in message


def test_the_pause_before_the_second_attempt_is_2_seconds():
    assert unavailable.SECOND_ATTEMPT_AFTER_MS == 2000
