"""``getTranslatedShaderSource`` answers in the DECLARED shader language, not the host's.

Characterises, on the published engine (firefox-68), the hooks of the series folder
``fingerprint/webgl/shader-language`` in ``dom/canvas`` (``StealthWebGLShaderLanguage.cpp``;
hooks in ``WebGLShader.cpp``, ``WebGLShaderValidator.cpp``, ``WebGLContext.cpp``): with
``zoom.stealth.webgl.shader_output_language`` set to ``essl`` the page that asks
``WEBGL_debug_shaders`` for the translated source of a shader reads ANGLE's translation to ESSL,
the language a Windows Firefox (ANGLE, a GLES context) always answers in; the driver keeps
receiving its own.

Why a page reads it: the session declares a Windows ANGLE renderer, and on a host whose GL
context is not GLES (Linux through GLX) the same translator emits desktop GLSL, so the page
would read ``#version 450`` from a browser whose RENDERER says ANGLE: a contradiction inside
one page, with no need to know the OS.

The negative control is in the same test: the same engine, the same shaders, with the
declaration empty. It is adapted to the OS, as the engine is. On Linux the host's language is
desktop GLSL, so the declared and the undeclared sources DIFFER (the WebGL 2 shaders lose their
``#version 300 es`` line). On Windows the context already goes through ANGLE and is already
ESSL, so the two are EQUAL (and the declared one is the Windows one). On both, the driver still
compiles and links the shader: the page sees one language, the pixels do not move.
"""
from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.e2e

SEED = 4246

TRANSLATE_JS = """() => {
  const VS1 = 'attribute vec2 p; void main(){ gl_Position = vec4(p, 0.0, 1.0); }';
  const FS1 = 'precision mediump float; void main(){ gl_FragColor = vec4(1.0, 0.0, 0.0, 1.0); }';
  const VS2 = '#version 300 es\\nin vec2 p;\\nvoid main(){ gl_Position = vec4(p, 0.0, 1.0); }';
  const FS2 = '#version 300 es\\nprecision mediump float;\\nout vec4 o;\\n'
            + 'void main(){ o = vec4(1.0, 0.0, 0.0, 1.0); }';
  const out = {};
  for (const [kind, vs, fs] of [["webgl", VS1, FS1], ["webgl2", VS2, FS2]]) {
    const gl = document.createElement("canvas").getContext(kind);
    if (!gl) { out[kind] = null; continue; }
    const dbg = gl.getExtension("WEBGL_debug_shaders");
    const v = gl.createShader(gl.VERTEX_SHADER); gl.shaderSource(v, vs); gl.compileShader(v);
    const f = gl.createShader(gl.FRAGMENT_SHADER); gl.shaderSource(f, fs); gl.compileShader(f);
    const prog = gl.createProgram(); gl.attachShader(prog, v); gl.attachShader(prog, f);
    gl.linkProgram(prog);
    out[kind] = {
      has_extension: !!dbg,
      vertex: dbg ? dbg.getTranslatedShaderSource(v) : null,
      fragment: dbg ? dbg.getTranslatedShaderSource(f) : null,
      compiled: gl.getShaderParameter(v, gl.COMPILE_STATUS) && gl.getShaderParameter(f, gl.COMPILE_STATUS),
      linked: gl.getProgramParameter(prog, gl.LINK_STATUS),
    };
  }
  return out;
}"""


def _read(firefox_binary, language):
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(
            seed=SEED, binary_path=firefox_binary, headless=True,
            extra_prefs={"zoom.stealth.webgl.shader_output_language": language}) as browser:
        page = browser.new_page()
        page.goto("about:blank")
        return page.evaluate(TRANSLATE_JS)


def test_get_translated_shader_source_is_in_the_declared_language_and_the_driver_still_compiles(
        firefox_binary):
    declared = _read(firefox_binary, "essl")
    control = _read(firefox_binary, "")

    for kind in ("webgl", "webgl2"):
        for label, run in (("declared", declared), ("control", control)):
            got = run[kind]
            assert got is not None, "%s: no %s context at all" % (label, kind)
            assert got["has_extension"], (label, kind)
            # The driver is not touched by what the page is shown: it compiles and links.
            assert got["compiled"] is True and got["linked"] is True, (label, kind, got)
            assert got["vertex"] and got["fragment"], (label, kind, got)

    # Declared: ESSL, on every OS. A WebGL 1 shader has no #version line in ESSL 1.00 and a
    # WebGL 2 one keeps its "#version 300 es".
    assert "#version" not in declared["webgl"]["vertex"], declared["webgl"]
    assert "#version" not in declared["webgl"]["fragment"], declared["webgl"]
    assert declared["webgl2"]["vertex"].lstrip().startswith("#version 300 es"), declared["webgl2"]
    assert declared["webgl2"]["fragment"].lstrip().startswith("#version 300 es"), declared["webgl2"]

    if sys.platform.startswith("linux"):
        # The host's context is desktop GL: without the declaration the page reads desktop GLSL.
        # The declaration is what makes the two differ (as a pair of stages: a one-line vertex
        # shader may translate to the same text in both languages).
        for kind in ("webgl", "webgl2"):
            assert (control[kind]["vertex"], control[kind]["fragment"]) != (
                declared[kind]["vertex"], declared[kind]["fragment"]), (kind, control[kind])
        assert not control["webgl2"]["vertex"].lstrip().startswith("#version 300 es"), control["webgl2"]
        assert not control["webgl2"]["fragment"].lstrip().startswith("#version 300 es"), control["webgl2"]
    else:
        # Windows (ANGLE, a GLES context): the host's language is already ESSL, nothing to
        # translate twice, and the declaration changes nothing.
        for kind in ("webgl", "webgl2"):
            for stage in ("vertex", "fragment"):
                assert control[kind][stage] == declared[kind][stage], (kind, stage, control[kind])
