"""The control launch of a WebGL test runs on the engine's own GL, never on the host's. No browser.

With the engine off (``zoom.stealth.fpp.hw_seed`` 0) Firefox loads the HOST's Mesa for WebGL; on a runner it once gave no context in 3083 launches
("Exhausted GL driver options"), and a test cannot fail because of the runner's Mesa. The engine's own GL (``llvmpipe/`` next to the executable) is
used only with the engine on, so the control of a test that compares a noised read with a read as drawn keeps the engine ON, with the same declared
GPU, and declares only the canvas noise off (``zoom.stealth.canvas.substitute_pixels`` false), the control tests/test_webgl_noise_active.py already
uses. These tests hold the prefs that the two e2e files pass to the launch, read from the call to ``InvisiblePlaywright`` itself (a recorder stands
in for it and raises at once), and the rule of the verdict on the GL that both files call:

* tests/test_canvas_reads_as_drawn.py, the non-premultiplied WebGL canvas: neither launch has hw_seed 0; the control declares the noise off; the
  noised launch does not;
* tests/test_canvas_create_pattern_projection_e2e.py: the same, for both of its launches;
* the verdict on the GL: some engine process maps the engine's own libEGL.so.1 and libgallium-*, and no process maps a libEGL that is not the
  engine's; it does not apply on Windows nor to an engine without a GL of its own.
* tests/test_the_host_gl_probe_runs_only_on_an_undeclared_engine_linux_e2e.py needs the engine off (it proves Mozilla's own probe of the
  host's GL): its page still asks for WebGL, and with the engine off the answer is written down and not judged; with the engine declared
  a missing context is still red.
"""
from __future__ import annotations

import importlib
import os

import pytest

import engine_process_tree as tree
import invisible_playwright

pytestmark = pytest.mark.unit

HW_SEED = "zoom.stealth.fpp.hw_seed"
SUBSTITUTE = "zoom.stealth.canvas.substitute_pixels"


class _Launched(Exception):
    """The recorder has what it came for: the launch goes no further."""


def _prefs_of_the_launch(monkeypatch, module_name, launch):
    """The ``extra_prefs`` that ``launch()`` hands to InvisiblePlaywright, in the module under test."""
    module = importlib.import_module(module_name)
    seen = {}

    class Recorder:
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)
            raise _Launched

    monkeypatch.setattr(module, "InvisiblePlaywright", Recorder, raising=False)
    monkeypatch.setattr(invisible_playwright, "InvisiblePlaywright", Recorder)
    with pytest.raises(_Launched):
        launch(module)
    return dict(seen.get("extra_prefs") or {})


def _the_two_launches_keep_the_engine_on_and_only_the_noise_differs(noised, control):
    assert (noised.get(HW_SEED) or 0) > 0, (
        "the noised launch does not declare the engine on (hw_seed %r): its prefs are %r" % (noised.get(HW_SEED), noised))
    assert (control.get(HW_SEED) or 0) > 0, (
        "the control launch has the engine off (hw_seed %r): WebGL would run on the host's GL, which a runner may not serve; its prefs are %r"
        % (control.get(HW_SEED), control))
    assert control.get(SUBSTITUTE) is False, (
        "the control launch does not declare the canvas noise off (%s is %r): its prefs are %r" % (SUBSTITUTE, control.get(SUBSTITUTE), control))
    assert noised.get(SUBSTITUTE) is not False, "the noised launch declares the canvas noise off: its prefs are %r" % noised


def test_the_non_premultiplied_canvas_control_keeps_the_engine_on_and_declares_only_the_noise_off(monkeypatch):
    name = "test_canvas_reads_as_drawn"
    noised = _prefs_of_the_launch(monkeypatch, name, lambda m: m._non_premultiplied(None, True))
    control = _prefs_of_the_launch(monkeypatch, name, lambda m: m._non_premultiplied(None, False))
    _the_two_launches_keep_the_engine_on_and_only_the_noise_differs(noised, control)


def test_the_create_pattern_control_keeps_the_engine_on_and_declares_only_the_noise_off(monkeypatch):
    name = "test_canvas_create_pattern_projection_e2e"
    noised = _prefs_of_the_launch(monkeypatch, name, lambda m: m._read(None, True))
    control = _prefs_of_the_launch(monkeypatch, name, lambda m: m._read(None, False))
    _the_two_launches_keep_the_engine_on_and_only_the_noise_differs(noised, control)


@pytest.fixture
def engine_dir(tmp_path):
    """An engine folder with a binary; the maps below are written with its real path, as /proc gives them."""
    folder = tmp_path / "engine"
    folder.mkdir()
    (folder / "firefox").write_bytes(b"")
    real = os.path.realpath(str(folder))
    return str(folder / "firefox"), os.path.join(real, "llvmpipe") + os.sep


def test_a_process_that_maps_the_engines_own_gl_and_nothing_foreign_passes(engine_dir):
    binary, own = engine_dir
    tree.assert_the_gl_is_the_engines_own(binary, {10: [own + "libEGL.so.1", own + "libgallium-26.2.6.so"], 11: ["/usr/lib/libc.so.6"]})


def test_a_host_libegl_in_any_process_fails_and_names_the_pid(engine_dir):
    binary, own = engine_dir
    below = {10: [own + "libEGL.so.1", own + "libgallium-26.2.6.so"], 11: ["/usr/lib/x86_64-linux-gnu/libEGL.so.1"]}
    with pytest.raises(AssertionError) as caught:
        tree.assert_the_gl_is_the_engines_own(binary, below)
    assert "11" in str(caught.value) and "/usr/lib/x86_64-linux-gnu/libEGL.so.1" in str(caught.value)


def test_no_process_that_draws_on_the_engines_own_gl_fails(engine_dir):
    binary, own = engine_dir
    with pytest.raises(AssertionError):
        tree.assert_the_gl_is_the_engines_own(binary, {10: ["/usr/lib/x86_64-linux-gnu/libgallium-25.2.8.so"]})
    with pytest.raises(AssertionError):
        tree.assert_the_gl_is_the_engines_own(binary, {})


def test_the_verdict_on_the_gl_does_not_apply_where_it_has_no_maps(engine_dir):
    binary, _ = engine_dir
    tree.assert_the_gl_is_the_engines_own(binary, None)


def _host_gl_check(monkeypatch, webgl):
    """`_check` of tests/test_the_host_gl_probe_runs_only_on_an_undeclared_engine_linux_e2e.py with its launch stood in for: a probe born
    before the goto under the browser's parent, nothing wrong with the observation, and the page's answer to getContext('webgl') given."""
    import types

    module = importlib.import_module("test_the_host_gl_probe_runs_only_on_an_undeclared_engine_linux_e2e")
    probe = (types.SimpleNamespace(pid=11), types.SimpleNamespace(ppid=10, first=1.0))
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(module, "_require_strace_can_trace", lambda path: None)
    monkeypatch.setattr(module, "_launch", lambda binary, work, prefs: ([probe], [], webgl, "the report", {10}, 2.0))
    return module


@pytest.mark.parametrize("webgl", [None, False])
def test_the_host_gl_control_writes_down_a_missing_context_on_the_host_s_mesa_and_does_not_judge_it(monkeypatch, tmp_path, webgl):
    module = _host_gl_check(monkeypatch, webgl)
    module._check("firefox", tmp_path, module.UNDECLARED, want_probe=True)


def test_the_declared_engine_still_requires_a_webgl_context(monkeypatch, tmp_path):
    module = _host_gl_check(monkeypatch, None)
    with pytest.raises(AssertionError, match="null with the engine declared"):
        module._check("firefox", tmp_path, None, want_probe=True)
