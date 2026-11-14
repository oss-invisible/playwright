"""HUNT ONLY (branch hunt/cpu-render-path, never merged): a pytest plugin that adds prefs to every browser the wrapper's own tests launch.

    PYTHONPATH=scripts HUNT_EXTRA_PREFS='{"gfx.webrender.software": true}' python -m pytest -p hunt_extra_prefs_plugin tests/test_...py

The tests construct `InvisiblePlaywright(...)` (the sync class, tests/test_canvas_text_is_windows_e2e.py and others) or its async twin and
some of them pass `extra_prefs=` of their own. Nothing of the product and nothing of the tests is edited: this plugin wraps the two
`__init__` in place, in this process only, and merges HUNT_EXTRA_PREFS over the test's own extra_prefs (the hunt's value wins; the
test's other prefs stay). Without HUNT_EXTRA_PREFS the plugin does nothing, so the same command line runs the arm `ours`.

It prints one line per construction to a file (HUNT_EXTRA_PREFS_LOG, default <tmp>/hunt-extra-prefs.log) so that the run can say how many
browsers got the pref: a suite that launches its browser some other way would otherwise pass "with the pref" without having had it.
"""
from __future__ import annotations

import json
import os
import tempfile

_PREFS = json.loads(os.environ.get("HUNT_EXTRA_PREFS") or "{}")
_LOG = os.environ.get("HUNT_EXTRA_PREFS_LOG") or os.path.join(tempfile.gettempdir(), "hunt-extra-prefs.log")


def _wrap(cls) -> None:
    original = cls.__init__

    def __init__(self, *args, **kwargs):   # noqa: N807
        merged = dict(kwargs.get("extra_prefs") or {})
        merged.update(_PREFS)
        kwargs["extra_prefs"] = merged
        try:
            with open(_LOG, "a", encoding="utf-8") as f:
                f.write("%s.%s seed=%s extra_prefs=%s\n" % (cls.__module__, cls.__name__, kwargs.get("seed"), json.dumps(merged, sort_keys=True)))
        except OSError:
            pass
        original(self, *args, **kwargs)

    __init__.__wrapped__ = original
    cls.__init__ = __init__


def pytest_configure(config):   # noqa: D103
    if not _PREFS:
        return
    from invisible_playwright.async_api import InvisiblePlaywright as Async
    from invisible_playwright.launcher import InvisiblePlaywright as Sync
    _wrap(Sync)
    _wrap(Async)
    print("[hunt] extra_prefs %s added to every InvisiblePlaywright construction, logged to %s" % (json.dumps(_PREFS), _LOG))
