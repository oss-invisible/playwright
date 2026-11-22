"""The host's GL probe (``glxtest``) starts only when the engine is not declared (e2e, Linux, from firefox-69).

Why Linux only: the probe is a process of Firefox on GTK. ``GfxInfo::FireGLXTestProcess`` (widget/gtk/GfxInfo.cpp, fired early in ``XRE_mainStartup``,
toolkit/xre/nsAppRunner.cpp, and again by ``GfxInfo::GetData``) spawns ``<GRE>/gfxtest glx -f <fd>``, a child of the parent that dlopens the host's libGL,
libEGL, libpci and libdrm and writes what it found to a pipe. Windows has no such process (its GfxInfo reads the adapter in the parent), so this test has
no Windows form: the file is in no row of Windows (tests/test_the_e2e_suite_is_accounted_for.py, EXCLUDED) and is not collected there as a test to run.

What it fixes. With the engine declared (the product's configuration: the core declares the GPU, every feature status and the GL is the engine's own) the
probe has no use and is removed where it is born: the hook of ``platform/declaration-gate`` (``StealthNoHostGLProbe`` on the first line of
``GfxInfo::FireGLXTestProcess``, ``StealthFillGtkGfxInfoWithoutHost`` in ``GfxInfo::GetData``; src/gfx/thebes/StealthGfxFeatureStatus.cpp of the series).
With the engine NOT declared (``zoom.stealth.fpp.hw_seed`` = 0, the negative control) Mozilla's probe starts as in stock Firefox. On firefox-68 the probe
runs in both launches, and that is what the test asserts there (``engine_reaches("firefox-69")`` decides).

Why a page sees it: what the probe reads of the host decided, through the feature status, whether WebGL existed and on which path; a slow or failed probe
took WebGL away for the whole session. Each launch also asks a page for ``getContext('webgl')``.

How it is observed (the process, not a value). A thread samples the processes below this test with psutil from before the launch to after the browser
has closed (``tests/_process_sampler.py``). The probe lives for tens of milliseconds and a poll can fall between two samples, so the sampler alone never
says "absent": the browser runs under the strace stand-in (``tests/_strace_stand_in.py``, tracing only execve and the clones), and ptrace writes every exec
of the tree to the log, because the process is stopped AT the call. Every exec of the probe in the log must be a pid the sampler saw as the probe, the
sampler must cover the window from the launch to the page's WebGL context, and the log must hold the browser's own exec; otherwise the test is red with
the problem (``reconcile``), never green on a miss. The failure message lists every process seen (times from the launch, names, pid, ppid, command line)
and every exec strace wrote.
"""
from __future__ import annotations

import os
import shutil
import sys
import time

import pytest

from _process_sampler import TRACE, TreeSampler, browser_parents, describe, is_glx_probe, read_execs, reconcile
from _strace_stand_in import _kill_group, _require_strace_can_trace, _stand_in

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the GLX probe is a process of Firefox on GTK (Linux)"),
]

SEED = 42

#: The engine off: Mozilla's own path, the negative control.
UNDECLARED = {"zoom.stealth.fpp.hw_seed": 0}

HAS_WEBGL = "() => document.createElement('canvas').getContext('webgl') !== null"


def _launch(firefox_binary, work, extra_prefs):
    """One launch under the strace stand-in, sampled from before it to after the browser has closed. Returns
    ``(glx_probes, problems, webgl, report, parents)``."""
    from invisible_playwright import InvisiblePlaywright

    work.mkdir(parents=True)
    real = os.path.realpath(firefox_binary)
    log, pidfile = work / "strace.log", work / "stand-in.pid"
    stand_in = _stand_in(firefox_binary, work / "engine", log, pidfile, syscalls=TRACE)
    sampler = TreeSampler(os.getpid()).start()
    launched_at = time.monotonic()
    ready_at, webgl = None, None
    try:
        with InvisiblePlaywright(seed=SEED, binary_path=stand_in, headless=True, extra_prefs=extra_prefs) as browser:
            page = browser.new_page()
            page.goto("about:blank")
            webgl = page.evaluate(HAS_WEBGL)
            ready_at = time.monotonic()
    finally:
        _kill_group(pidfile)
        sampler.stop()
    seen, first, last, error = sampler.view()
    execs = read_execs(log.read_text(encoding="utf-8", errors="replace").splitlines())
    probes, problems = reconcile(seen, first, last, error, execs, real, launched_at, ready_at)
    report = "%d samples\n%s" % (sampler.samples, describe(seen, execs, launched_at))
    glx = [(e, seen[e.pid]) for e in probes if is_glx_probe(e) and e.pid in seen]
    return glx, problems, webgl, report, browser_parents(execs, real)


def _check(firefox_binary, tmp_path, extra_prefs, want_probe):
    if shutil.which("strace") is None:
        pytest.fail("needs strace (apt install strace): without it no absence can be proved", pytrace=False)
    _require_strace_can_trace(tmp_path)
    glx, problems, webgl, report, parents = _launch(firefox_binary, tmp_path / "launch", extra_prefs)
    assert problems == [], "the observation cannot be trusted:\n%s\n%s" % ("\n".join(problems), report)
    assert webgl is True, "getContext('webgl') is null\n" + report
    if want_probe:
        assert glx, "no GLX probe (gfxtest glx) below the browser's parent %s\n%s" % (parents, report)
        strays = [(e.pid, s.ppid) for e, s in glx if s.ppid not in parents]
        assert not strays, "a GLX probe that is not a child of the browser's parent %s: %s\n%s" % (parents, strays, report)
    else:
        assert not glx, "the GLX probe ran on a declared engine: %s\n%s" % ([e.pid for e, _s in glx], report)


def test_a_declared_engine_starts_no_host_gl_probe(firefox_binary, engine_reaches, tmp_path):
    """The product's configuration. firefox-69: no probe from the launch to the page's WebGL context. firefox-68: the probe runs (what 68 does)."""
    _check(firefox_binary, tmp_path, None, want_probe=not engine_reaches("firefox-69"))


def test_an_undeclared_engine_starts_mozilla_s_host_gl_probe(firefox_binary, tmp_path):
    """The negative control: with the engine off Mozilla's probe starts as in stock Firefox, on every engine, and the page still has WebGL."""
    _check(firefox_binary, tmp_path, UNDECLARED, want_probe=True)
