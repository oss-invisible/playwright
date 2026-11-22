"""Fingerprint surface tests - replicate the checks performed by the canonical
anti-bot detection libraries against an OFFLINE browser session.

Each test asserts the SAME thing the upstream detector would flag. A pass
here means our patched build appears human to that detector; a fail
means a real stealth hole that anti-bot kits would exploit in production.

Detector libraries studied (all FOSS, MIT-licensed):
  - github.com/fingerprintjs/BotD            - 19 detectors, the most
                                                widely deployed client-side
                                                bot detector
  - github.com/abrahamjuliot/creepjs         - headless / stealth / lies
                                                modules
  - github.com/fingerprintjs/fingerprintjs   - canvas / audio / color /
                                                touch consistency
  - github.com/antoinevastel/fpscanner       - UA / platform / oscpu
                                                cross-checks
  - bot.sannysoft.com                        - classic Puppeteer harness

Everything runs against `about:blank` with NO network and NO proxy. The
suite is intended to be part of the release-gate: pre-push hook runs
`pytest -m e2e` and these tests must be green on every release.

Run only this file:
    pytest tests/test_fingerprint_surface.py -m e2e -v
"""
from __future__ import annotations

import re
import sys

import pytest

from invisible_playwright import InvisiblePlaywright


# ────────────────────────────────────────────────────────────────────
# Inline PIN - a coherent mid-range Windows desktop. Not user-config:
# these specific values are what the surface tests assert against.
# Keep PIN small (only fields that JS exposes) and stable across runs.
# ────────────────────────────────────────────────────────────────────

PIN = {
    "screen.width": 1920,
    "screen.height": 1080,
    # `screen.avail_width` / `screen.avail_height` were pinned here until
    # 2026-09-15 and are no longer pin keys: the engine derives the available
    # rect from width, height and taskbar_px, so pinning them moved a label on
    # the profile and nothing a page reads. The assertions below get stronger
    # for it - they now check a value the engine computed rather than one the
    # caller thought it had set.
    "screen.dpr": 1.0,
    "hardware.concurrency": 8,
    "audio.sample_rate": 48000,
    "audio.max_channel_count": 2,
}


@pytest.fixture(scope="module")
def page(firefox_binary):
    """One headless browser shared across the whole module.
    ~20s startup paid once, then every test runs in ~50ms."""
    with InvisiblePlaywright(
        seed=42,
        pin=PIN,
        binary_path=firefox_binary,
        headless=True,
    ) as browser:
        ctx = browser.new_context()
        p = ctx.new_page()
        p.goto("about:blank", timeout=30_000)
        yield p


def _ev(page, expr):
    return page.evaluate(expr)


# ===========================================================================
# sannysoft.com - classic Puppeteer detection harness
# ===========================================================================


@pytest.mark.e2e
def test_sannysoft_chrome_object_consistency(page):
    """Firefox UA + window.chrome present = bot-framework leak."""
    if "Firefox" in _ev(page, "navigator.userAgent"):
        assert not _ev(page, "typeof window.chrome !== 'undefined'")


@pytest.mark.e2e
def test_sannysoft_permissions_query_works(page):
    """navigator.permissions.query() must return a proper PermissionStatus."""
    ok = _ev(page, """async () => {
        if (!navigator.permissions || !navigator.permissions.query) return false;
        try {
            const r = await navigator.permissions.query({name: 'notifications'});
            return r && typeof r.state === 'string';
        } catch (e) { return false; }
    }""")
    assert ok


@pytest.mark.e2e
def test_sannysoft_iframe_chrome_not_leaked(page):
    """iframe.contentWindow.chrome must not leak on Firefox UA."""
    if "Firefox" not in _ev(page, "navigator.userAgent"):
        pytest.skip("Firefox-only invariant")
    leaks = _ev(page, """() => {
        const iframe = document.createElement('iframe');
        iframe.style.display = 'none';
        document.body.appendChild(iframe);
        const is = typeof iframe.contentWindow.chrome !== 'undefined';
        document.body.removeChild(iframe);
        return is;
    }""")
    assert not leaks


@pytest.mark.e2e
def test_sannysoft_iframe_languages_not_empty(page):
    """Iframe-scope navigator.languages must have ≥1 entry."""
    n = _ev(page, """() => {
        const f = document.createElement('iframe');
        f.style.display = 'none';
        document.body.appendChild(f);
        const len = f.contentWindow.navigator.languages.length;
        document.body.removeChild(f);
        return len;
    }""")
    assert n > 0


# ===========================================================================
# FingerprintJS - fingerprint surface coherence
# ===========================================================================


@pytest.mark.e2e
def test_fpjs_canvas_2d_context_returns_valid(page):
    ok = _ev(page, """() => {
        const c = document.createElement('canvas');
        c.width = 100; c.height = 100;
        const ctx = c.getContext('2d');
        if (!ctx) return false;
        ctx.fillText('test', 10, 10);
        const data = c.toDataURL();
        return data.length > 100 && data.startsWith('data:image/png;base64');
    }""")
    assert ok


@pytest.mark.e2e
def test_fpjs_audio_context_works(page):
    ok = _ev(page, """async () => {
        try {
            const ctx = new (window.OfflineAudioContext ||
                              window.webkitOfflineAudioContext)(1, 5000, 44100);
            const osc = ctx.createOscillator();
            osc.connect(ctx.destination);
            osc.start(0);
            const buf = await ctx.startRendering();
            return buf && buf.length > 0;
        } catch (e) { return false; }
    }""")
    assert ok


@pytest.mark.e2e
def test_fpjs_color_gamut_query_works(page):
    """matchMedia('(color-gamut: ...)') must match at least srgb."""
    ok = _ev(page, """matchMedia('(color-gamut: srgb)').matches ||
                      matchMedia('(color-gamut: p3)').matches ||
                      matchMedia('(color-gamut: rec2020)').matches""")
    assert ok


@pytest.mark.e2e
def test_fpjs_screen_color_depth_realistic(page):
    """Atypical color depths are headless-distinctive."""
    cd = _ev(page, "screen.colorDepth")
    assert cd in (24, 30, 32)


# ===========================================================================
# PIN-locked surfaces (the values declared in PIN above)
# ===========================================================================


@pytest.mark.e2e
def test_pin_screen_width_lands_in_screen_object(page):
    assert _ev(page, "screen.width") == PIN["screen.width"]


@pytest.mark.e2e
def test_pin_screen_height_lands_in_screen_object(page):
    assert _ev(page, "screen.height") == PIN["screen.height"]


@pytest.mark.e2e
def test_pin_hardware_concurrency_lands_in_navigator(page):
    assert (_ev(page, "navigator.hardwareConcurrency")
            == PIN["hardware.concurrency"])


@pytest.mark.e2e
def test_pin_audio_sample_rate_lands_in_AudioContext(page):
    assert _ev(page,
        "(new (window.AudioContext||window.webkitAudioContext)()).sampleRate"
    ) == PIN["audio.sample_rate"]


@pytest.mark.e2e
def test_pin_audio_max_channels_lands_in_destination(page):
    assert _ev(page,
        "(new (window.AudioContext||window.webkitAudioContext)())"
        ".destination.maxChannelCount"
    ) == PIN["audio.max_channel_count"]


# ===========================================================================
# fpscanner-style cross-checks
# ===========================================================================


@pytest.mark.e2e
def test_fpscanner_ua_vs_platform_consistent(page):
    """UA OS substring must agree with navigator.platform OS substring."""
    ua = _ev(page, "navigator.userAgent")
    platform = _ev(page, "navigator.platform")
    if "Windows" in ua:
        assert "Win" in platform, f"UA Win but platform={platform!r}"
    elif "Mac" in ua:
        assert "Mac" in platform
    elif "Linux" in ua:
        assert "Linux" in platform or "X11" in platform


@pytest.mark.e2e
def test_fpscanner_no_userAgentData_on_firefox(page):
    """navigator.userAgentData is Chromium-only. Presence on Firefox UA = bot."""
    if "Firefox" in _ev(page, "navigator.userAgent"):
        assert not _ev(page, "'userAgentData' in navigator")


# ===========================================================================
# WebGL masking-detector guard (pixelscan getFixedRedBox / webglHash)
#
# pixelscan flags "fingerprint masking" on the WebGL readPixels output. We
# reproduce ITS probe locally (the fingerprintjs gradient triangle) and check
# the structural signature it keys on: our stealth readPixels noise MUST be a
# coherent, monotonic gamma remap (smooth, ~0 spikes), NOT isolated +-1 flips
# (which read as unnatural high-frequency noise and were flagged as masking).
# This is the CI-safe local stand-in for pixelscan's server-side check; it
# guards the gamma fix from ever silently regressing to the +-1 algorithm.
# ===========================================================================

_WEBGL_MASKING_PROBE = """() => {
  const c = document.createElement('canvas');
  const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
  if (!gl) return { error: 'no-webgl' };
  const vs = 'attribute vec2 a;uniform vec2 o;varying vec2 v;' +
             'void main(){v=a+o;gl_Position=vec4(a,0,1);}';
  const fs = 'precision mediump float;varying vec2 v;' +
             'void main(){gl_FragColor=vec4(v,0,1);}';
  const buf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER,
    new Float32Array([-0.2,-0.9,0, 0.4,-0.26,0, 0,0.732134444,0]), gl.STATIC_DRAW);
  const p = gl.createProgram();
  const s1 = gl.createShader(gl.VERTEX_SHADER); gl.shaderSource(s1, vs); gl.compileShader(s1);
  const s2 = gl.createShader(gl.FRAGMENT_SHADER); gl.shaderSource(s2, fs); gl.compileShader(s2);
  gl.attachShader(p, s1); gl.attachShader(p, s2); gl.linkProgram(p); gl.useProgram(p);
  const loc = gl.getAttribLocation(p, 'a'); gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 3, gl.FLOAT, false, 0, 0);
  const off = gl.getUniformLocation(p, 'o'); gl.uniform2f(off, 1, 1);
  gl.drawArrays(gl.TRIANGLE_STRIP, 0, 3);
  const w = gl.drawingBufferWidth, h = gl.drawingBufferHeight;
  const px = new Uint8Array(w * h * 4);
  gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, px);
  // count small local extrema (|delta|<=3 to both horizontal neighbours, same
  // sign) - the +-1-noise signature; a smooth/monotonic render has ~none.
  let spikes = 0;
  for (let y = 0; y < h; y++) {
    for (let x = 1; x < w - 1; x++) {
      for (let ch = 0; ch < 3; ch++) {
        const i = (y * w + x) * 4 + ch; const val = px[i];
        if (val === 0) continue;
        const dl = val - px[i - 4], dr = val - px[i + 4];
        if (dl * dr > 0 && Math.abs(dl) <= 3 && Math.abs(dr) <= 3) spikes++;
      }
    }
  }
  return { spikes: spikes, dims: w + 'x' + h };
}"""


@pytest.mark.e2e
def test_webgl_readpixels_no_masking_signature(page):
    """Stealth WebGL readPixels noise must be a coherent gamma remap (smooth),
    not isolated +-1 flips. +-1 noise on the smooth gradient triangle produced
    ~300+ 'spikes' and pixelscan flagged it as masking; the gamma remap leaves
    the gradient smooth (~0 spikes). Regression guard for the gamma fix."""
    res = _ev(page, _WEBGL_MASKING_PROBE)
    if res.get("error") == "no-webgl" and sys.platform == "darwin":
        pytest.skip(
            "macOS CI runners expose no WebGL (no software-GL fallback); the gamma "
            "readPixels remap is platform-agnostic C++ and is exercised by the Linux "
            "(Xvfb/llvmpipe) and Windows (WARP) gates."
        )
    assert "error" not in res, f"WebGL probe failed: {res}"
    # genuine / gamma -> ~0; the rejected +-1 algorithm produced ~320.
    assert res["spikes"] < 30, (
        f"WebGL readPixels shows {res['spikes']} high-frequency noise spikes "
        f"(pixelscan-maskable); the stealth noise must be a smooth gamma remap."
    )


# ===========================================================================
# The declared screen, and the inner size of the top level (characterisation of
# the engine hooks fingerprint/screen-window/screen: nsScreen.cpp, nsPresContext.cpp)
# ===========================================================================

#: A screen no runner has: 2731x1537, a 41 px taskbar, 30-bit colour, at scale 1.0.
_DECLARED_SCREEN = {"screen.width": 2731, "screen.height": 1537, "screen.taskbar_px": 41,
                    "screen.color_depth": 30, "screen.dpr": 1.0}

#: Another screen no runner has, for the control: the wrapper hands the declared screen to the browser by two roads (the prefs and
#: the screen of the context that Juggler emulates), so a launch with the prefs at 0 still reads the pinned screen.
_OTHER_SCREEN = {"screen.width": 2623, "screen.height": 1411, "screen.taskbar_px": 37,
                 "screen.color_depth": 24, "screen.dpr": 1.0}

_READ_SCREEN = """() => ({width: screen.width, height: screen.height, availWidth: screen.availWidth,
                          availHeight: screen.availHeight, pixelDepth: screen.pixelDepth,
                          colorDepth: screen.colorDepth})"""


def _read_screen(firefox_binary, pin=_DECLARED_SCREEN):
    with InvisiblePlaywright(seed=42, pin=pin, binary_path=firefox_binary, headless=True) as browser:
        page = browser.new_page()
        page.goto("about:blank", timeout=30_000)
        return page.evaluate(_READ_SCREEN)


@pytest.mark.e2e
def test_a_declared_screen_that_no_runner_has_is_what_the_page_reads(firefox_binary):
    """The page reads the screen the core declares (nsScreen::GetRect, GetAvailRect, PixelDepth), not the host's.

    The available height is the declared height less the declared taskbar (2731x1537, taskbar 41, depth 30 -> availWidth
    2731, availHeight 1496). The control is a launch declared with ANOTHER screen (2623x1411, taskbar 37, depth 24: the wrapper
    hands the declared screen to the browser by two roads, so the absent declaration is not a control): the page reads that one, and
    none of the first declaration's numbers.
    """
    from _popup_geometry import in_a_thread_of_its_own

    # The module's `page` fixture keeps a sync client open in this thread: a second browser needs a thread of its own.
    declared = in_a_thread_of_its_own(lambda: _read_screen(firefox_binary))
    assert declared == {"width": 2731, "height": 1537, "availWidth": 2731, "availHeight": 1537 - 41,
                        "pixelDepth": 30, "colorDepth": 30}, "the page reads %r under a declared 2731x1537 screen" % declared

    host = in_a_thread_of_its_own(lambda: _read_screen(firefox_binary, _OTHER_SCREEN))
    assert host == {"width": 2623, "height": 1411, "availWidth": 2623, "availHeight": 1411 - 37,
                    "pixelDepth": 24, "colorDepth": 24}, "the page reads %r under another declared screen" % host
    for key in ("width", "height", "availWidth", "availHeight", "pixelDepth"):
        assert host[key] != declared[key], (
            "under another declared screen screen.%s reads %r, the first declared value: the control reads the first declaration "
            "(%r)" % (key, host[key], host))


@pytest.mark.e2e
def test_the_inner_size_of_the_top_is_synced_with_the_engine_active_and_not_without(firefox_binary):
    """A popup's iframe reads the top level's inner size (nsPresContext::UpdateInnerSizeSpoofedForRFP).

    With the engine active the top-level inner size is synced to every process of the browsing context, so an iframe of
    another site in a popup (200x150, in its own process) answers the outer size of the POPUP: the top's inner size plus the
    declared frame, 2 * border wide and popup UI + border high, and not the size of its own 200x150. The popup itself
    answers the same. The control is the same page in a launch declared with a SECOND frame that no runner has (border 6, popup
    UI 53): the iframe then answers the top's inner size plus THAT frame, and none of the first declaration's. The engine off is
    not the control: the declared frame is the one of a retail Windows window at scale 1.0, so a Windows runner's own popup
    reads it with the engine off too (measured: 516x480 for a 500x400 popup) and the test could not fail there.
    """
    from _popup_geometry import (OTHER_BORDER, OTHER_FRAME, OTHER_POPUP_UI, frame_extra, frame_of_the_declaration,
                                 in_a_thread_of_its_own, read_popup)

    frame = frame_of_the_declaration()
    extra = frame_extra(frame.border, frame.popup_ui)
    other_extra = frame_extra(OTHER_BORDER, OTHER_POPUP_UI)
    assert other_extra != extra

    def synced_to(wanted):
        def synced(reading):
            top = reading["popup"]
            return (reading["frame"]["ow"], reading["frame"]["oh"]) == (top["iw"] + wanted[0], top["ih"] + wanted[1])
        return synced

    # The module's `page` fixture keeps a sync client open in this thread: a second browser needs a thread of its own.
    on = in_a_thread_of_its_own(lambda: read_popup(firefox_binary, iframe=True, ready=synced_to(extra)))
    top, inner = on["popup"], on["frame"]
    assert (top["ow"], top["oh"]) == (top["iw"] + extra[0], top["ih"] + extra[1]), (
        "the popup reads outer %dx%d for inner %dx%d, the declared frame adds %r" % (top["ow"], top["oh"], top["iw"],
                                                                                      top["ih"], extra))
    assert (inner["ow"], inner["oh"]) == (top["iw"] + extra[0], top["ih"] + extra[1]), (
        "the iframe of the popup reads outer %dx%d, the top level's inner %dx%d plus the declared frame %r is %dx%d (the "
        "iframe's own inner size is %dx%d)" % (inner["ow"], inner["oh"], top["iw"], top["ih"], extra,
                                               top["iw"] + extra[0], top["ih"] + extra[1], inner["iw"], inner["ih"]))
    assert (inner["iw"], inner["ih"]) != (top["iw"], top["ih"]), "the iframe has the size of the popup: the read proves nothing"

    other = in_a_thread_of_its_own(lambda: read_popup(firefox_binary, extra_prefs=OTHER_FRAME, iframe=True, ready=synced_to(other_extra)))
    other_top, other_inner = other["popup"], other["frame"]
    assert (other_inner["ow"], other_inner["oh"]) == (other_top["iw"] + other_extra[0], other_top["ih"] + other_extra[1]), (
        "declared with another frame the iframe of a popup reads outer %dx%d, the top's inner %dx%d plus the second declared frame %r is %dx%d: %r"
        % (other_inner["ow"], other_inner["oh"], other_top["iw"], other_top["ih"], other_extra,
           other_top["iw"] + other_extra[0], other_top["ih"] + other_extra[1], other))
    assert (other_top["ow"], other_top["oh"]) == (other_top["iw"] + other_extra[0], other_top["ih"] + other_extra[1]), (
        "declared with another frame the popup reads outer %dx%d for inner %dx%d, the second declared frame adds %r"
        % (other_top["ow"], other_top["oh"], other_top["iw"], other_top["ih"], other_extra))
    assert (other_inner["ow"] - other_top["iw"], other_inner["oh"] - other_top["ih"]) != (inner["ow"] - top["iw"], inner["oh"] - top["ih"]), (
        "the iframe reads the same frame under two declarations: it does not follow the declaration: %r vs %r" % (other, on))
