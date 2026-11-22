"""A WebGL context tells the page what the session DECLARES, never what the host's GL says.

Characterises, on the published engine (firefox-68), the hooks of the series folder
``fingerprint/webgl/parameters`` in ``dom/canvas`` (``StealthWebGLParams.cpp`` and the hooks in
``ClientWebGLContext.cpp``, ``WebGLContextValidate.cpp``): the numeric ``getParameter`` answers
(``zoom.stealth.webgl.int_params`` / ``int2_params`` / ``float_params``), the renderer and the
vendor strings (``zoom.stealth.webgl.renderer`` / ``vendor``), ``getShaderPrecisionFormat``
(``zoom.stealth.webgl.shader_precisions``), the extension list the page reads
(``zoom.stealth.webgl.extensions`` / ``zoom.stealth.webgl2.extensions``) and the stencil masks a
fresh context starts with. A page reads every one of them with a handful of lines, and they are
the biggest part of a WebGL fingerprint, so a value that came from the host's GPU or driver (a
Mesa llvmpipe on a Linux runner, a WARP adapter on a Windows one) is a value that tells the
machine apart from the Windows the session claims to be.

Each test declares a value no runner has (a ``MAX_TEXTURE_SIZE`` of 12345, a precision of
7:11:13, a Radeon HD 5850, an extension list in a deliberately wrong order) and reads it back
where a page reads it. The negative control is in the same test: the same page, the same
engine, with the declaration of that value empty, reads the host's own answer, and the test
asserts the two DIFFER, so it fails if the declaration is not what the page reads.

The renderer and the vendor have no empty control: an engine that is on refuses to start
without them (the declaration gate), and an engine that is off draws on the host's GL, which a
runner without a GPU may not give at all. The control there is the session's own persona (the
product default), which the page must NOT read once the declaration says otherwise.

The stencil masks have no pref at all: the engine starts them at all ones, as ANGLE does, where
the driver's own start is the stencil bits (Mesa: 255). The control is the same read after the
page writes 255 itself, which proves the read is live and that 255 is what the masks were
before the engine.

The test reads the engine through ``extra_prefs`` (applied last by the core), RFP stays off.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

SEED = 4246

#: gl.INVALID_ENUM
INVALID_ENUM = 1280

# -- numeric parameters ----------------------------------------------------------------------

#: A value no runner has for each (the host's are 16384 / 2048 / 32767 on a GL or ANGLE).
DECLARED_INT = "3379|12345,34024|23456,34076|9001,32883|4321,35071|777"
DECLARED_INT2 = "3386|12345:23456"
DECLARED_FLOAT = "33901|1:7777,33902|3:5"
EMPTY_NUMERIC = {
    "zoom.stealth.webgl.int_params": "",
    "zoom.stealth.webgl.int2_params": "",
    "zoom.stealth.webgl.float_params": "",
}
DECLARED_NUMERIC = {
    "zoom.stealth.webgl.int_params": DECLARED_INT,
    "zoom.stealth.webgl.int2_params": DECLARED_INT2,
    "zoom.stealth.webgl.float_params": DECLARED_FLOAT,
}

NUMERIC_JS = """() => {
  const NAMES = {
    MAX_TEXTURE_SIZE: 0x0D33, MAX_RENDERBUFFER_SIZE: 0x84E8, MAX_CUBE_MAP_TEXTURE_SIZE: 0x851C,
    MAX_VIEWPORT_DIMS: 0x0D3A, ALIASED_POINT_SIZE_RANGE: 0x846D, ALIASED_LINE_WIDTH_RANGE: 0x846E,
    MAX_3D_TEXTURE_SIZE: 0x8073, MAX_ARRAY_TEXTURE_LAYERS: 0x88FF,
  };
  const out = {};
  for (const kind of ["webgl", "webgl2"]) {
    const gl = document.createElement("canvas").getContext(kind);
    if (!gl) { out[kind] = null; continue; }
    gl.getError();
    const r = {};
    for (const [name, pname] of Object.entries(NAMES)) {
      const v = gl.getParameter(pname);
      const error = gl.getError();
      const view = v !== null && ArrayBuffer.isView(v);
      r[name] = {value: view ? Array.from(v) : v,
                 type: v === null ? "null" : (view ? v.constructor.name : typeof v), error};
    }
    out[kind] = r;
  }
  return out;
}"""

# -- renderer and vendor ---------------------------------------------------------------------

#: A device no runner has. ``SanitizeRenderer`` keeps the device of this Direct3D string as it
#: is (a "Radeon HD 5850" is its own bucket) and rewrites what comes after the suffix, so the
#: page reads this prefix whether the string is sanitized or not.
RENDERER = "ANGLE (AMD, Radeon HD 5850 Direct3D11 vs_5_0 ps_5_0, D3D11)"
RENDERER_READ_PREFIX = "ANGLE (AMD, Radeon HD 5850 Direct3D11 vs_5_0 ps_5_0"
VENDOR = "Google Inc. (AMD)"

RENDERER_JS = """() => {
  const out = {};
  for (const kind of ["webgl", "webgl2"]) {
    const gl = document.createElement("canvas").getContext(kind);
    if (!gl) { out[kind] = null; continue; }
    gl.getError();
    // UNMASKED_RENDERER_WEBGL before the extension is enabled: Mozilla's own error.
    const before = gl.getParameter(0x9246);
    const before_error = gl.getError();
    const ext = gl.getExtension("WEBGL_debug_renderer_info");
    out[kind] = {
      renderer: gl.getParameter(gl.RENDERER),
      vendor: gl.getParameter(gl.VENDOR),
      unmasked_before_extension: before,
      unmasked_before_extension_error: before_error,
      has_extension: !!ext,
      unmasked_renderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null,
      unmasked_vendor: ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : null,
      error: gl.getError(),
    };
  }
  return out;
}"""

# -- shader precision ------------------------------------------------------------------------

#: VERTEX_SHADER*LOW_FLOAT -> 7:11:13 and FRAGMENT_SHADER*HIGH_INT -> 3:5:2. No GPU has either.
DECLARED_PRECISION = "35633*36336|7:11:13,35632*36341|3:5:2"

PRECISION_JS = """() => {
  const out = {};
  for (const kind of ["webgl", "webgl2"]) {
    const gl = document.createElement("canvas").getContext(kind);
    if (!gl) { out[kind] = null; continue; }
    gl.getError();
    const read = (shader, precision) => {
      const f = gl.getShaderPrecisionFormat(shader, precision);
      const error = gl.getError();
      return {format: f === null ? null : [f.rangeMin, f.rangeMax, f.precision], error};
    };
    out[kind] = {
      vertex_low_float: read(gl.VERTEX_SHADER, gl.LOW_FLOAT),
      fragment_high_int: read(gl.FRAGMENT_SHADER, gl.HIGH_INT),
      vertex_high_float: read(gl.VERTEX_SHADER, gl.HIGH_FLOAT),
      bad_precision_type: read(gl.VERTEX_SHADER, 0x1234),
      bad_shader_type: read(gl.TEXTURE_2D, gl.LOW_FLOAT),
    };
  }
  return out;
}"""

# -- extension list --------------------------------------------------------------------------

#: ``WebGLExtensionID`` of ``dom/canvas/WebGLTypes.h``, in declaration order (``MOZ_debug`` is
#: never listed to a page): the one order a page ever reads, the retail one.
ENUM_ORDER = [
    "ANGLE_instanced_arrays", "EXT_blend_minmax", "EXT_clip_control", "EXT_color_buffer_float",
    "EXT_color_buffer_half_float", "EXT_depth_clamp", "EXT_disjoint_timer_query", "EXT_float_blend",
    "EXT_frag_depth", "EXT_polygon_offset_clamp", "EXT_shader_texture_lod", "EXT_sRGB",
    "EXT_texture_compression_bptc", "EXT_texture_compression_rgtc",
    "EXT_texture_filter_anisotropic", "EXT_texture_mirror_clamp_to_edge", "EXT_texture_norm16",
    "OES_draw_buffers_indexed", "OES_element_index_uint", "OES_fbo_render_mipmap",
    "OES_standard_derivatives", "OES_texture_float", "OES_texture_float_linear",
    "OES_texture_half_float", "OES_texture_half_float_linear", "OES_vertex_array_object",
    "OVR_multiview2", "WEBGL_color_buffer_float", "WEBGL_compressed_texture_astc",
    "WEBGL_compressed_texture_etc", "WEBGL_compressed_texture_etc1",
    "WEBGL_compressed_texture_pvrtc", "WEBGL_compressed_texture_s3tc",
    "WEBGL_compressed_texture_s3tc_srgb", "WEBGL_debug_renderer_info", "WEBGL_debug_shaders",
    "WEBGL_depth_texture", "WEBGL_draw_buffers", "WEBGL_explicit_present", "WEBGL_lose_context",
    "WEBGL_provoking_vertex",
]

#: Declared in an order that is NOT the enum's: the page must read them in the enum's.
DECLARED_EXTENSIONS_1 = ["WEBGL_lose_context", "OES_texture_float", "EXT_blend_minmax",
                         "OES_vertex_array_object", "ANGLE_instanced_arrays"]
DECLARED_EXTENSIONS_2 = ["WEBGL_lose_context", "EXT_texture_filter_anisotropic",
                         "EXT_color_buffer_float", "OES_texture_float_linear"]

EXTENSIONS_JS = """() => {
  const out = {};
  for (const kind of ["webgl", "webgl2"]) {
    const gl = document.createElement("canvas").getContext(kind);
    if (!gl) { out[kind] = null; continue; }
    out[kind] = {list: gl.getSupportedExtensions(),
                 depth_texture: gl.getExtension("WEBGL_depth_texture") === null ? null : "object"};
  }
  return out;
}"""

# -- stencil masks ---------------------------------------------------------------------------

STENCIL_JS = """() => {
  const PNAMES = {STENCIL_VALUE_MASK: 0x0B93, STENCIL_WRITEMASK: 0x0B98,
                  STENCIL_BACK_VALUE_MASK: 0x8CA4, STENCIL_BACK_WRITEMASK: 0x8CA5};
  const read = gl => {
    const r = {};
    for (const [name, pname] of Object.entries(PNAMES)) r[name] = gl.getParameter(pname);
    return r;
  };
  const out = {};
  for (const kind of ["webgl", "webgl2"]) {
    for (const stencil of [false, true]) {
      const key = kind + (stencil ? "+stencil" : "");
      const gl = document.createElement("canvas").getContext(kind, {stencil});
      if (!gl) { out[key] = null; continue; }
      const fresh = read(gl);
      // The control: the page writes 255 itself and reads it back.
      gl.stencilMask(255);
      gl.stencilMaskSeparate(gl.BACK, 255);
      gl.stencilFunc(gl.ALWAYS, 0, 255);
      gl.stencilFuncSeparate(gl.BACK, gl.ALWAYS, 0, 255);
      out[key] = {fresh, written: read(gl)};
    }
  }
  return out;
}"""


def _read(firefox_binary, prefs, js):
    """One short-lived product session with ``prefs`` overlaid, and what the page answers to ``js``."""
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=SEED, binary_path=firefox_binary, headless=True,
                             extra_prefs=dict(prefs)) as browser:
        page = browser.new_page()
        page.goto("about:blank")
        return page.evaluate(js)


def _contexts(got, label):
    for kind in ("webgl", "webgl2"):
        assert got[kind] is not None, "%s: no %s context at all" % (label, kind)


def test_declared_numeric_parameters_replace_the_hosts_and_webgl2_only_names_stay_null_on_webgl1(
        firefox_binary):
    declared = _read(firefox_binary, DECLARED_NUMERIC, NUMERIC_JS)
    control = _read(firefox_binary, EMPTY_NUMERIC, NUMERIC_JS)
    _contexts(declared, "declared")
    _contexts(control, "control")

    want = {
        "MAX_TEXTURE_SIZE": (12345, "number"),
        "MAX_RENDERBUFFER_SIZE": (23456, "number"),
        "MAX_CUBE_MAP_TEXTURE_SIZE": (9001, "number"),
        "MAX_VIEWPORT_DIMS": ([12345, 23456], "Int32Array"),
        "ALIASED_POINT_SIZE_RANGE": ([1, 7777], "Float32Array"),
        "ALIASED_LINE_WIDTH_RANGE": ([3, 5], "Float32Array"),
    }
    want_webgl2_only = {"MAX_3D_TEXTURE_SIZE": 4321, "MAX_ARRAY_TEXTURE_LAYERS": 777}

    for kind in ("webgl", "webgl2"):
        for name, (value, type_) in want.items():
            got = declared[kind][name]
            assert got == {"value": value, "type": type_, "error": 0}, (kind, name, got)
            # The control: the same page with the declaration empty reads the host's answer.
            assert control[kind][name]["value"] != value, (
                "%s %s: the host's own answer is the declared one, so this test cannot tell "
                "the declaration from the host: %r" % (kind, name, control[kind][name]))
            assert control[kind][name]["error"] == 0, (kind, name, control[kind][name])

    # WebGL 2 only: the declared number is read on a WebGL 2 context ...
    for name, value in want_webgl2_only.items():
        got = declared["webgl2"][name]
        assert got == {"value": value, "type": "number", "error": 0}, (name, got)
        assert control["webgl2"][name]["value"] != value, (name, control["webgl2"][name])
        # ... and on a WebGL 1 context the name does not exist: null and INVALID_ENUM, declared
        # or not (the declaration cannot make a name appear that the context does not have).
        for label, run in (("declared", declared), ("control", control)):
            got = run["webgl"][name]
            assert got == {"value": None, "type": "null", "error": INVALID_ENUM}, (
                label, name, got)


def test_declared_renderer_and_vendor_are_read_in_every_form(firefox_binary):
    declared = _read(firefox_binary, {"zoom.stealth.webgl.renderer": RENDERER,
                                      "zoom.stealth.webgl.vendor": VENDOR}, RENDERER_JS)
    # The control: the session's own persona, the product default (the declaration gate refuses
    # an empty pair on a running engine).
    control = _read(firefox_binary, {}, RENDERER_JS)
    _contexts(declared, "declared")
    _contexts(control, "control")

    for kind in ("webgl", "webgl2"):
        got = declared[kind]
        # Every form of the renderer: the masked constant, and the unmasked one through the
        # extension the page enables.
        assert got["renderer"].startswith(RENDERER_READ_PREFIX), (kind, got)
        assert got["has_extension"], (kind, got)
        assert got["unmasked_renderer"].startswith(RENDERER_READ_PREFIX), (kind, got)
        assert got["unmasked_vendor"] == VENDOR, (kind, got)
        # The masked vendor is Firefox's own constant, whatever is declared.
        assert got["vendor"] == "Mozilla", (kind, got)
        # Without the extension the unmasked constants are not a thing: Mozilla's error.
        assert got["unmasked_before_extension"] is None, (kind, got)
        assert got["unmasked_before_extension_error"] == INVALID_ENUM, (kind, got)
        assert got["error"] == 0, (kind, got)

        base = control[kind]
        assert base["has_extension"], (kind, base)
        assert not base["renderer"].startswith(RENDERER_READ_PREFIX), (kind, base)
        assert not base["unmasked_renderer"].startswith(RENDERER_READ_PREFIX), (kind, base)
        assert base["unmasked_vendor"] != VENDOR, (kind, base)
        assert base["vendor"] == "Mozilla", (kind, base)
        assert base["unmasked_before_extension"] is None, (kind, base)
        assert base["unmasked_before_extension_error"] == INVALID_ENUM, (kind, base)


def test_declared_shader_precision_replaces_the_hosts_and_a_bad_pair_still_errors(
        firefox_binary):
    declared = _read(firefox_binary,
                     {"zoom.stealth.webgl.shader_precisions": DECLARED_PRECISION}, PRECISION_JS)
    control = _read(firefox_binary, {"zoom.stealth.webgl.shader_precisions": ""}, PRECISION_JS)
    _contexts(declared, "declared")
    _contexts(control, "control")

    for kind in ("webgl", "webgl2"):
        got, base = declared[kind], control[kind]
        assert got["vertex_low_float"] == {"format": [7, 11, 13], "error": 0}, (kind, got)
        assert got["fragment_high_int"] == {"format": [3, 5, 2], "error": 0}, (kind, got)
        # The control: with the declaration empty the page reads the host's formats.
        assert base["vertex_low_float"]["format"] not in (None, [7, 11, 13]), (kind, base)
        assert base["fragment_high_int"]["format"] not in (None, [3, 5, 2]), (kind, base)
        assert base["vertex_low_float"]["error"] == 0, (kind, base)
        # A pair nothing declares is the host's, untouched by the declarations next to it.
        assert got["vertex_high_float"] == base["vertex_high_float"], (kind, got, base)
        assert got["vertex_high_float"]["format"] is not None, (kind, got)
        # A bad pair is Mozilla's error with or without a declaration in the pref.
        for name in ("bad_precision_type", "bad_shader_type"):
            for label, run in (("declared", got), ("control", base)):
                assert run[name] == {"format": None, "error": INVALID_ENUM}, (kind, label, name, run)


def test_the_declared_extension_list_is_read_in_firefoxs_order(firefox_binary):
    declared = _read(firefox_binary, {
        "zoom.stealth.webgl.extensions": ",".join(DECLARED_EXTENSIONS_1),
        "zoom.stealth.webgl2.extensions": ",".join(DECLARED_EXTENSIONS_2),
    }, EXTENSIONS_JS)
    control = _read(firefox_binary, {"zoom.stealth.webgl.extensions": "",
                                     "zoom.stealth.webgl2.extensions": ""}, EXTENSIONS_JS)
    _contexts(declared, "declared")
    _contexts(control, "control")

    for kind, names in (("webgl", DECLARED_EXTENSIONS_1), ("webgl2", DECLARED_EXTENSIONS_2)):
        assert names != sorted(names, key=ENUM_ORDER.index), "the declared order must be wrong"
        want = [e for e in ENUM_ORDER if e in names]
        assert declared[kind]["list"] == want, (kind, declared[kind]["list"])
        # The control: with the declaration empty the page reads the host's own list.
        assert control[kind]["list"] != want, (kind, control[kind]["list"])
        assert control[kind]["list"], (kind, "the host list is empty")
    # Only what is declared is served: the host has WEBGL_depth_texture, the declaration lacks it.
    assert declared["webgl"]["depth_texture"] is None, declared["webgl"]


def test_the_stencil_masks_start_at_all_ones(firefox_binary):
    got = _read(firefox_binary, {}, STENCIL_JS)
    all_ones = 4294967295
    names = ("STENCIL_VALUE_MASK", "STENCIL_WRITEMASK",
             "STENCIL_BACK_VALUE_MASK", "STENCIL_BACK_WRITEMASK")
    for key in ("webgl", "webgl+stencil", "webgl2", "webgl2+stencil"):
        run = got[key]
        assert run is not None, "%s: no context at all" % key
        # A fresh context, on any OS: all ones, as ANGLE starts them.
        assert run["fresh"] == {n: all_ones for n in names}, (key, run["fresh"])
        # The control: what the masks were before the engine on a driver GL is the stencil
        # bits (255 on Mesa), and a page that writes 255 reads 255 back, so the read is live and
        # all ones is not a constant of the page's reading.
        assert run["written"] == {n: 255 for n in names}, (key, run["written"])
