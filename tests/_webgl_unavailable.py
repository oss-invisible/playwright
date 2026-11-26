"""What a test that cannot get a WebGL context writes down, so that the red says why (a pure helper, no browser).

A page asks for a context and gets ``null``: the page learns nothing more unless it listens for ``webglcontextcreationerror`` on the canvas
before it asks (``statusMessage``). The probe of the test returns an object with ``unavailable: true`` and what it heard instead of ``null``;
``split_probe`` turns that into the old ``None`` (so that the assertion stays as it was) and the record of the attempt, and ``unavailable_message``
is the sentence of the assertion with both attempts appended.
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

#: The pause, in milliseconds, between the first attempt and the second one (a diagnostic: the verdict is the first attempt's).
SECOND_ATTEMPT_AFTER_MS = 2000
UNAVAILABLE = "webgl is unavailable: a WebGL canvas cannot be read"


def split_probe(result: Any) -> Tuple[Optional[Any], Optional[dict]]:
    """``(got, first)``: ``got`` is what the test reads (``None`` when there was no context, exactly as the old probe returned), ``first`` the
    record of the failed attempt (``None`` when a context was made)."""
    if isinstance(result, dict) and result.get("unavailable") is True:
        return None, result
    return result, None


def unavailable_message(first: Optional[dict], second: Any) -> str:
    """The message of the assertion: the old sentence, then what the first attempt and the second one (2 s later) reported."""
    return "%s | first: %r; second after 2 s: %r" % (UNAVAILABLE, first, second)
