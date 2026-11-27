"""The identity exceptions of the host gate (scripts/host_gate/exceptions.json) cover only leaves that depend on the identity.

An `identity` exception tells the gate: "this value is the seed's, hold it to host-independence only, not to retail". That is true of a
leaf the core draws from the persona (the panel, the scale, the core count, the GPU), and false of a leaf every persona answers the same
(colorDepth 24, availTop 0, orientation landscape-primary, a WebGPU limit): a glob that swallows one of those lets a wrong constant pass
for ever, because nothing compares it with retail. This test finds them.

How: every pattern of every identity entry is expanded against the leaves of the retail references (both scales). For each leaf it covers,
the core's prefs of a spread of seeds (the declared values the engine answers: the panel, the scale, the frame, the cores, the GPU
persona, the WebGPU tables) say whether the answer varies. A leaf is DECLARED FROM the prefs listed for it in DECLARED_FROM; a leaf with
no entry has no declared source in the profile, so it is a constant of Firefox. Red when the leaf does not vary, with the leaf and the
pattern named. A pattern that covers no leaf of the references is red too: it would swallow a leaf nobody looked at.

`reference_machine` entries (a value the retail reference run itself had: where its window sat, which speech voices its Windows had) are
the other way to leave the retail comparison. They never leave host-independence, they carry no glob, and the leaf must be one the
identity does not depend on (a leaf that varies belongs under `identity`).
"""
from __future__ import annotations

import fnmatch
import functools
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ci_host_gate as gate  # noqa: E402

pytestmark = pytest.mark.unit

core_profile = pytest.importorskip("invisible_core._fpforge")
from invisible_core.constants import WINDOW_FRAME_BY_DPR  # noqa: E402
from invisible_core.prefs import translate_profile_to_prefs  # noqa: E402

#: The seeds whose declared values are looked at. The persona space the engine draws from is finite (four scales, a table of panels, a
#: pool of GPU personas, a table of core counts); this many seeds meet every scale, many panels and the personas that are not rare.
SEEDS = tuple(range(0, 400))

# -
# What each leaf is declared from. A key is a pattern over leaves; the value takes (leaf, prefs) and returns what the core declared for it
# (anything comparable). Order matters: the first pattern that matches decides.

W = "zoom.stealth.screen.width"
H = "zoom.stealth.screen.height"
AW = "zoom.stealth.screen.avail_device_width"
AH = "zoom.stealth.screen.avail_device_height"
BORDER = "zoom.stealth.screen.frame_border"
UI = "zoom.stealth.screen.maximized_ui"
DPR = "layout.css.devPixelsPerPx"


def _prefs(*names):
    return lambda leaf, prefs: tuple(prefs.get(n) for n in names)


#: Windowed window and screen (the page's own window, and the normal and after_fullscreen states): the panel in device pixels, the scale,
#: the frame (the resize border, the UI above the content). In fullscreen the window is the screen: no frame, origin (0, 0).
_WINDOWED = {
    "screen.width": (W, DPR), "screen.height": (H, DPR), "screen.availWidth": (AW, DPR), "screen.availHeight": (AH, DPR),
    "innerWidth": (W, DPR), "innerHeight": (AH, UI, DPR), "outerWidth": (W, BORDER, DPR), "outerHeight": (AH, BORDER, DPR),
    "screenX": (BORDER, DPR), "screenY": (BORDER, DPR), "screenLeft": (BORDER, DPR), "screenTop": (BORDER, DPR),
    "mozInnerScreenY": (UI, DPR),
    "visualViewport.width": (W, DPR), "visualViewport.height": (AH, UI, DPR),
    "body": (W, DPR),
    "events": (BORDER, UI, DPR),
}
_FULLSCREEN = {
    "screen.width": (W, DPR), "screen.height": (H, DPR), "screen.availWidth": (W, DPR), "screen.availHeight": (H, DPR),
    "innerWidth": (W, DPR), "innerHeight": (H, DPR), "outerWidth": (W, DPR), "outerHeight": (H, DPR),
}


_STATE_FIELD = re.compile(r"state\.(?P<state>normal|fullscreen|after_fullscreen)\.(?P<field>screen\.\w+|events\.\w+|\w+)$")
_SURFACE_FIELD = re.compile(r"surface\.(?:(?P<screen>screen\.[\w.]+)|window\.(?P<window>\w+)|(?P<viewport>visualViewport\.\w+)"
                            r"|(?P<body>computedStyle\.body\.[\w-]+))$")


def _geometry(leaf, prefs):
    """What a window or screen leaf is declared from (see _WINDOWED), or None for a leaf the profile does not declare."""
    m = _STATE_FIELD.match(leaf)
    if m:
        table = _FULLSCREEN if m.group("state") == "fullscreen" else _WINDOWED
        field = "events" if m.group("field").startswith("events.") else m.group("field")
    else:
        m = _SURFACE_FIELD.match(leaf)
        table = _WINDOWED
        field = None if m is None else (m.group("screen") or m.group("window") or m.group("viewport") or "body")
    names = table.get(field)
    return None if names is None else tuple(prefs.get(n) for n in names)


def _table(prefs, name):
    """A pref of `enum|value,enum|value` pairs as {enum: value}."""
    return {int(e): v for e, v in (item.split("|", 1) for item in prefs.get(name, "").split(",") if item)}


#: The GL names the core declares by number (GLConsts.h of the engine source; MAX_CLIENT_WAIT_TIMEOUT_WEBGL is WebGLContext.h's, and the two
#: ..._WEBGL names of the draw-buffers extension are the numbers of MAX_COLOR_ATTACHMENTS and MAX_DRAW_BUFFERS).
GL_ENUMS = {
    "MAX_3D_TEXTURE_SIZE": 32883, "MAX_ARRAY_TEXTURE_LAYERS": 35071, "MAX_CLIENT_WAIT_TIMEOUT_WEBGL": 37447,
    "MAX_COLOR_ATTACHMENTS": 36063, "MAX_COLOR_ATTACHMENTS_WEBGL": 36063,
    "MAX_COMBINED_FRAGMENT_UNIFORM_COMPONENTS": 35379, "MAX_COMBINED_TEXTURE_IMAGE_UNITS": 35661,
    "MAX_COMBINED_UNIFORM_BLOCKS": 35374, "MAX_COMBINED_VERTEX_UNIFORM_COMPONENTS": 35377, "MAX_CUBE_MAP_TEXTURE_SIZE": 34076,
    "MAX_DRAW_BUFFERS": 34852, "MAX_DRAW_BUFFERS_WEBGL": 34852, "MAX_ELEMENTS_INDICES": 33001, "MAX_ELEMENTS_VERTICES": 33000,
    "MAX_ELEMENT_INDEX": 36203, "MAX_FRAGMENT_INPUT_COMPONENTS": 37157, "MAX_FRAGMENT_UNIFORM_BLOCKS": 35373,
    "MAX_FRAGMENT_UNIFORM_COMPONENTS": 35657, "MAX_FRAGMENT_UNIFORM_VECTORS": 36349, "MAX_PROGRAM_TEXEL_OFFSET": 35077,
    "MAX_RENDERBUFFER_SIZE": 34024, "MAX_SAMPLES": 36183, "MAX_SERVER_WAIT_TIMEOUT": 37137, "MAX_TEXTURE_IMAGE_UNITS": 34930,
    "MAX_TEXTURE_LOD_BIAS": 34045, "MAX_TEXTURE_MAX_ANISOTROPY_EXT": 34047, "MAX_TEXTURE_SIZE": 3379,
    "MAX_TRANSFORM_FEEDBACK_INTERLEAVED_COMPONENTS": 35978, "MAX_TRANSFORM_FEEDBACK_SEPARATE_ATTRIBS": 35979,
    "MAX_TRANSFORM_FEEDBACK_SEPARATE_COMPONENTS": 35968, "MAX_UNIFORM_BLOCK_SIZE": 35376, "MAX_UNIFORM_BUFFER_BINDINGS": 35375,
    "MAX_VARYING_COMPONENTS": 35659, "MAX_VARYING_VECTORS": 36348, "MAX_VERTEX_ATTRIBS": 34921, "MAX_VERTEX_OUTPUT_COMPONENTS": 37154,
    "MAX_VERTEX_TEXTURE_IMAGE_UNITS": 35660, "MAX_VERTEX_UNIFORM_BLOCKS": 35371, "MAX_VERTEX_UNIFORM_COMPONENTS": 35658,
    "MAX_VERTEX_UNIFORM_VECTORS": 36347, "MAX_VIEWPORT_DIMS": 3386, "MAX_VIEWS_OVR": 38449,
}


def _webgl_limit(leaf, prefs):
    """A GL limit: what int_params and int2_params declare under its number. A number neither declares is answered by the host's ANGLE
    (None for every seed): a constant of Firefox on Windows."""
    enum = GL_ENUMS.get(leaf.rsplit(".", 1)[1])
    if enum is None:
        return None
    ints, pairs = _table(prefs, "zoom.stealth.webgl.int_params"), _table(prefs, "zoom.stealth.webgl.int2_params")
    declared = (ints.get(enum), pairs.get(enum))
    return None if declared == (None, None) else declared


def _webgpu(leaf, prefs):
    """A WebGPU limit or the feature list: the core declares one table for every Direct3D 12 persona."""
    m = re.match(r"surface\.webgpu\.adapters\.[a-z_]+\.(limits\.(?P<limit>\w+)|(?P<features>features))$", leaf)
    if not m:
        return None  # the adapter's info and its device: no pref carries them
    if m.group("features"):
        return prefs.get("zoom.stealth.webgpu.features")
    limits = dict(line.split("=", 1) for line in prefs.get("zoom.stealth.webgpu.limits", "").split("\n") if "=" in line)
    return limits.get(m.group("limit"))


DECLARED_FROM = (
    ("surface.screen.*", _geometry),
    ("surface.window.*", _geometry),
    ("surface.visualViewport.*", _geometry),
    ("surface.computedStyle.body.*", _geometry),
    ("state.*", _geometry),
    ("surface.navigator.hardwareConcurrency", _prefs("zoom.stealth.hw_concurrency")),
    ("surface.worker.hc", _prefs("zoom.stealth.hw_concurrency")),
    ("surface.voices", _prefs("zoom.stealth.voices.list")),
    ("surface.webgl*.unmasked", _prefs("zoom.stealth.webgl.vendor", "zoom.stealth.webgl.renderer")),
    ("surface.webgl*.parameters.RENDERER", _prefs("zoom.stealth.webgl.renderer")),
    ("surface.webgl*.extension_parameters.WEBGL_debug_renderer_info.UNMASKED_RENDERER_WEBGL", _prefs("zoom.stealth.webgl.renderer")),
    ("surface.webgl*.extension_parameters.WEBGL_debug_renderer_info.UNMASKED_VENDOR_WEBGL", _prefs("zoom.stealth.webgl.vendor")),
    ("surface.webgl*.parameters.MAX_*", _webgl_limit),
    ("surface.webgl*.extension_parameters.*.MAX_*", _webgl_limit),
    ("surface.webgpu.adapters.*", _webgpu),
)


def declared(leaf, prefs):
    """What the core declared for `leaf` in these prefs; None when the profile declares nothing for it (a constant of Firefox)."""
    for pattern, how in DECLARED_FROM:
        if fnmatch.fnmatchcase(leaf, pattern):
            return how(leaf, prefs)
    return None


# -
# What the engine answers for a reference_machine leaf, to hold it against the retail reference at each scale. A key here is the only way to
# be a reference_machine leaf: a leaf the engine's answer is not known for is red, since nothing would say it differs from retail.

def _voices_answer(prefs):
    """The (name, language) of each declared speech voice, in order."""
    return [item.split("|")[:2] for item in prefs["zoom.stealth.voices.list"].split(",") if item]


#: A maximized window's content starts at x = 0 on every persona (gfx::StealthDeclaredOriginShift, TestStealthWindowGeometry.cpp: the main
#: window is declared at (0, UI height) from the screen's origin; only y moves with the frame).
ENGINE_ANSWER = {
    "surface.window.mozInnerScreenX": lambda prefs: 0,
    "state.normal.mozInnerScreenX": lambda prefs: 0,
    "state.after_fullscreen.mozInnerScreenX": lambda prefs: 0,
    "surface.voices": _voices_answer,
}


def _retail_as_answered(leaf, value):
    """The retail value in the shape ENGINE_ANSWER gives."""
    return [v[:2] for v in value] if leaf == "surface.voices" else value


def equal_to_retail(leaf, prefs):
    """The scales (dpr) at which the engine's answer for `leaf` equals the retail reference's, or None when the engine's answer is not known."""
    answer = ENGINE_ANSWER.get(leaf)
    if answer is None:
        return None
    mine = answer(prefs)
    return [dpr for dpr in gate.DPRS
            if json.dumps(_retail_as_answered(leaf, gate.leaves(gate.reference_items(gate.REFERENCE, dpr))[leaf])) == json.dumps(mine)]


# -
# The seeds: the personas the declared values come from.

@pytest.fixture(scope="module")
def declared_prefs():
    """The prefs the core declares for each seed, in the gate's own session (en-US)."""
    return [translate_profile_to_prefs(core_profile.generate_profile(seed), locale="en-US", timezone="UTC") for seed in SEEDS]


@functools.lru_cache(maxsize=None)
def reference_leaves():
    """The leaves of the retail references, both scales: a frozenset."""
    out = set()
    for dpr in gate.DPRS:
        items = gate.reference_items(gate.REFERENCE, dpr)
        out |= {k for k in gate.leaves(items) if not gate.is_diag(k) and not gate.is_ab(k)}
    return frozenset(out)


def varies(leaf, all_prefs):
    return len({json.dumps(declared(leaf, p), sort_keys=True, default=str) for p in all_prefs}) > 1


def findings(exceptions, all_prefs, leaves=None):
    """Every line the guard is red on for these exceptions: an identity pattern that covers a leaf the seeds do not move, or covers no
    leaf; a reference_machine key with a glob, with no reason, that varies, or that equals retail."""
    leaves = reference_leaves() if leaves is None else leaves
    out = []
    for entry in exceptions.get("identity", []):
        for pattern in entry["keys"]:
            covered = sorted(k for k in leaves if fnmatch.fnmatchcase(k, pattern))
            if not covered:
                out.append("pattern %r covers no leaf of the retail references: it would swallow a leaf nobody looked at" % pattern)
            for leaf in covered:
                if not varies(leaf, all_prefs):
                    out.append("%s is exempt by %r but the same across the seeds' declared values (%s)"
                               % (leaf, pattern, json.dumps(declared(leaf, all_prefs[0]), default=str)[:80]))
    for entry in exceptions.get("reference_machine", []):
        for key in entry["keys"]:
            if re.search(r"[*?\[]", key):
                out.append("reference_machine key %r has a wildcard: name each leaf" % key)
            elif key not in leaves:
                out.append("reference_machine key %r is not a leaf of the retail references" % key)
            elif varies(key, all_prefs):
                out.append("%s is under reference_machine but varies with the seed: it belongs under identity" % key)
            else:
                same = equal_to_retail(key, all_prefs[0])
                if same is None:
                    out.append("%s is under reference_machine but ENGINE_ANSWER has no answer for it: nothing says it differs from retail" % key)
                elif same:
                    out.append("%s is under reference_machine but the engine answers what retail does at dpr %s: compare it, do not exempt it"
                               % (key, ", ".join(map(str, same))))
        if not isinstance(entry.get("why"), str) or not entry["why"].strip():
            out.append("reference_machine entry %s has no reason" % entry["keys"])
    return out


# -
# The tests.

def test_the_spread_of_seeds_meets_every_scale_and_many_panels(declared_prefs):
    scales = {float(p[DPR]) for p in declared_prefs}
    # 2.0 has a frame (a pinned persona) but the screen table never draws it: 0 of 3000 seeds
    assert {1.0, 1.25, 1.5} <= scales <= set(WINDOW_FRAME_BY_DPR), "the seeds meet the scales %s" % sorted(scales)
    panels = {(p[W], p[H]) for p in declared_prefs}
    assert len(panels) >= 8, "only %d panels in %d seeds" % (len(panels), len(SEEDS))
    assert len({p["zoom.stealth.hw_concurrency"] for p in declared_prefs}) >= 3
    assert len({p["zoom.stealth.webgl.renderer"] for p in declared_prefs}) >= 5


def test_the_identity_exceptions_of_the_gate_cover_only_what_the_seeds_move(declared_prefs):
    assert findings(gate.load_exceptions(), declared_prefs) == []


#: The exceptions the gate carried before this guard: a glob over the whole screen and over the whole screen of every state, the speech
#: voices, the GPU's every MAX_ limit and WebGPU adapter. Each swallowed leaves that no seed moves.
OLD_IDENTITY = {"identity": [
    {"keys": ["surface.screen.*", "state.*.screen.*"], "why": "the declared screen"},
    {"keys": ["surface.voices"], "why": "the declared speech voices"},
    {"keys": ["surface.webgl*.parameters.MAX_*", "surface.webgpu.adapters.*"], "why": "the declared GPU"},
]}


@pytest.fixture(scope="module")
def old_findings(declared_prefs):
    return findings(OLD_IDENTITY, declared_prefs)


@pytest.mark.parametrize("leaf", [
    "surface.screen.colorDepth", "surface.screen.pixelDepth", "surface.screen.availTop", "surface.screen.availLeft",
    "surface.screen.top", "surface.screen.left", "surface.screen.orientation.type", "surface.screen.orientation.angle",
    "surface.screen.mozOrientation", "surface.screen.addEventListener",
    "surface.voices", "surface.webgl1.parameters.MAX_TEXTURE_SIZE", "surface.webgpu.adapters.none.limits.maxBindGroups",
])
def test_the_guard_is_red_on_a_constant_the_old_exceptions_swallowed(old_findings, leaf):
    lines = [f for f in old_findings if f.startswith(leaf + " ")]
    assert lines, "%s is swallowed by the old exceptions and the guard does not say so" % leaf


def test_the_guard_does_not_flag_a_leaf_the_seeds_move(declared_prefs):
    moved = ["surface.screen.width", "surface.screen.availHeight", "surface.window.outerWidth", "surface.window.screenX",
             "state.normal.events.click", "state.fullscreen.innerHeight", "surface.navigator.hardwareConcurrency"]
    lines = findings({"identity": [{"keys": moved, "why": "x"}]}, declared_prefs, leaves=set(moved))
    assert lines == []


def test_a_pattern_that_covers_no_leaf_is_red(declared_prefs):
    lines = findings({"identity": [{"keys": ["surface.screen.noSuchLeaf*"], "why": "x"}]}, declared_prefs)
    assert any("covers no leaf" in line for line in lines)


def test_a_reference_machine_key_must_name_a_leaf_that_does_not_vary(declared_prefs):
    lines = findings({"reference_machine": [{"keys": ["surface.screen.*", "surface.screen.width", "surface.nope"], "why": "x"}]},
                     declared_prefs)
    assert any("wildcard" in line for line in lines)
    assert any("surface.screen.width is under reference_machine but varies" in line for line in lines)
    assert any("not a leaf of the retail references" in line for line in lines)


def test_a_reference_machine_leaf_that_equals_retail_is_red(declared_prefs, monkeypatch):
    # the engine answers 16 where the reference's window sat at 16 (dpr 1.0), and the voices the reference had: both are retail's, so compared
    monkeypatch.setitem(ENGINE_ANSWER, "surface.window.mozInnerScreenX", lambda prefs: 16)
    monkeypatch.setitem(ENGINE_ANSWER, "surface.voices", lambda prefs: [v[:2] for v in gate.leaves(
        gate.reference_items(gate.REFERENCE, 1.5))["surface.voices"]])
    exceptions = {"reference_machine": [{"keys": ["surface.window.mozInnerScreenX", "surface.voices"], "why": "x"}]}
    lines = findings(exceptions, declared_prefs)
    assert any(line.startswith("surface.window.mozInnerScreenX is under reference_machine but the engine answers what retail does at dpr 1.0")
               for line in lines), lines
    assert any(line.startswith("surface.voices is under reference_machine but the engine answers what retail does at dpr 1.0, 1.5")
               for line in lines), lines


def test_a_reference_machine_leaf_that_equals_retail_at_one_scale_only_is_red(declared_prefs, monkeypatch):
    # 10.666666984558105 is the reference's answer at dpr 1.5 only
    monkeypatch.setitem(ENGINE_ANSWER, "state.normal.mozInnerScreenX", lambda prefs: 10.666666984558105)
    lines = findings({"reference_machine": [{"keys": ["state.normal.mozInnerScreenX"], "why": "x"}]}, declared_prefs)
    assert any("at dpr 1.5" in line and "at dpr 1.0" not in line for line in lines), lines


def test_a_reference_machine_leaf_the_engine_answer_is_not_known_for_is_red(declared_prefs):
    lines = findings({"reference_machine": [{"keys": ["surface.screen.colorDepth"], "why": "x"}]}, declared_prefs)
    assert any("ENGINE_ANSWER has no answer" in line for line in lines), lines


@pytest.mark.parametrize("why", ["", "   ", None])
def test_a_reference_machine_entry_without_a_reason_is_red(declared_prefs, why):
    lines = findings({"reference_machine": [{"keys": ["surface.voices"], "why": why}]}, declared_prefs)
    assert any("has no reason" in line for line in lines), lines


def test_every_reference_machine_entry_of_the_gate_has_a_reason_and_differs_from_retail(declared_prefs):
    exceptions = gate.load_exceptions()
    assert exceptions["reference_machine"], "the gate has no reference_machine entry: this test would hold nothing"
    assert all(e["why"].strip() for e in exceptions["reference_machine"])
    for entry in exceptions["reference_machine"]:
        for key in entry["keys"]:
            assert equal_to_retail(key, declared_prefs[0]) == [], key

