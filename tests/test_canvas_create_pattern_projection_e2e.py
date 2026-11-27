"""``createPattern`` of a WebGL canvas reads the pixels the page would read from it, with the engine's projection; a 2D source is untouched (e2e, every OS, firefox-68).

What the engine fixes. The engine's per-seed noise (the projection, ``fingerprint/canvas/noise-projection``) is applied where a WebGL or WebGPU
canvas hands its pixels out: readPixels, toDataURL, and the snapshot a canvas gives when another canvas draws it. ``CanvasRenderingContext2D::
CreatePattern`` used to read its source canvas through the pattern's own context, a path the noise does not pass, so
``createPattern(webglCanvas)`` + ``fill`` + ``getImageData`` returned the CLEAN pixels of the render while ``drawImage(webglCanvas)`` returned the
projected ones: a page compared the two and read the difference. On firefox-68 two lines of ``CreatePattern`` (the HTMLCanvasElement and the
OffscreenCanvas overloads; folder ``fingerprint/canvas/text-metrics`` of the series, ``dom/canvas/CanvasRenderingContext2D.cpp``) read the snapshot
from the element, so the pattern carries the projection. A 2D canvas is not projected at all (a 2D canvas reads back exactly as drawn,
``tests/test_canvas_reads_as_drawn.py``), so a pattern of a 2D canvas is the same pixels with the noise on and off.

What the test reads. One page builds a source (a WebGL canvas, a WebGL OffscreenCanvas, or a 2D canvas), fills a 2D canvas with
``createPattern(source, 'no-repeat')`` and reads it back with ``getImageData``, then clears it and draws the source with ``drawImage`` and reads that.
The WebGL render is a triangle with per-vertex colours and antialiased edges on a white clear (what a fingerprinting page draws, and where the
projection acts: ``tests/test_webgl_noise_active.py``); the 2D source is a gradient, an antialiased arc and a rotated rectangle.

The negative control, in the same module. The same page runs with the canvas noise declared off and the engine still on (``zoom.stealth.canvas.
substitute_pixels`` false, the same declared GPU, the engine's own GL: no projection, because the noise is off, not because the engine is):
the WebGL pattern pixels with the noise on must DIFFER from those (the projection is what changed them), and the 2D pattern pixels must be the
SAME bytes with the noise on and off. A control with the engine off (``zoom.stealth.fpp.hw_seed`` 0) would draw the WebGL source on the host's
GL, which a runner may not serve (no context, once in 3503 launches), so no launch of this file has the engine off. On Linux, in both launches,
the maps of the engine's processes hold the engine's own GL and no libEGL of the host's. A render that is not drawn (a blank source) fails the
test: the pixels are checked to hold more than one colour. WebGL being unavailable on the runner is a failure, not a skip.

Run: pytest tests/test_canvas_create_pattern_projection_e2e.py -m e2e -v
"""
from __future__ import annotations

import pytest

from engine_process_tree import assert_the_gl_is_the_engines_own, gl_maps_of_this_test
from invisible_playwright import InvisiblePlaywright

pytestmark = pytest.mark.e2e

SIZE = 64

READ = """(args) => {
  const [kind, size] = args;
  let source;
  if (kind === 'webgl' || kind === 'offscreen-webgl') {
    source = kind === 'webgl' ? document.createElement('canvas') : new OffscreenCanvas(size, size);
    source.width = size; source.height = size;
    const gl = source.getContext('webgl', {preserveDrawingBuffer: true, antialias: true});
    if (!gl) return {error: 'no webgl context for ' + kind};
    const vs = gl.createShader(gl.VERTEX_SHADER);
    gl.shaderSource(vs, 'attribute vec2 p; attribute vec3 c; varying vec3 v; void main(){v=c;gl_Position=vec4(p,0.,1.);}');
    gl.compileShader(vs);
    const fs = gl.createShader(gl.FRAGMENT_SHADER);
    gl.shaderSource(fs, 'precision mediump float; varying vec3 v; void main(){gl_FragColor=vec4(v,1.);}');
    gl.compileShader(fs);
    const pr = gl.createProgram(); gl.attachShader(pr, vs); gl.attachShader(pr, fs);
    gl.linkProgram(pr); gl.useProgram(pr);
    const b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(
      [-0.9, -0.8, 1, 0, 0,  0.85, -0.6, 0, 1, 0,  0.1, 0.9, 0, 0, 1]), gl.STATIC_DRAW);
    const pl = gl.getAttribLocation(pr, 'p'), cl = gl.getAttribLocation(pr, 'c');
    gl.enableVertexAttribArray(pl); gl.vertexAttribPointer(pl, 2, gl.FLOAT, false, 20, 0);
    gl.enableVertexAttribArray(cl); gl.vertexAttribPointer(cl, 3, gl.FLOAT, false, 20, 8);
    gl.clearColor(1, 1, 1, 1); gl.clear(gl.COLOR_BUFFER_BIT); gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.finish();
  } else {
    source = document.createElement('canvas'); source.width = size; source.height = size;
    const s = source.getContext('2d');
    const g = s.createLinearGradient(0, 0, size, size);
    g.addColorStop(0, '#f60'); g.addColorStop(0.5, '#069'); g.addColorStop(1, '#6c0');
    s.fillStyle = g; s.fillRect(0, 0, size, size);
    s.strokeStyle = 'rgba(20, 20, 20, 0.8)'; s.lineWidth = 3.3;
    s.beginPath(); s.arc(size * 0.5, size * 0.45, size * 0.31, 0, 2 * Math.PI * 0.83); s.stroke();
    s.save(); s.translate(size * 0.6, size * 0.6); s.rotate(0.37); s.fillStyle = 'rgba(255, 255, 255, 0.6)';
    s.fillRect(-size * 0.2, -size * 0.1, size * 0.4, size * 0.2); s.restore();
  }
  const dst = document.createElement('canvas'); dst.width = size; dst.height = size;
  const d = dst.getContext('2d');
  d.fillStyle = d.createPattern(source, 'no-repeat');
  d.fillRect(0, 0, size, size);
  const viaPattern = Array.from(d.getImageData(0, 0, size, size).data);
  d.clearRect(0, 0, size, size);
  d.drawImage(source, 0, 0);
  const viaDraw = Array.from(d.getImageData(0, 0, size, size).data);
  return {viaPattern: viaPattern, viaDraw: viaDraw};
}"""

KINDS = ["webgl", "offscreen-webgl", "2d"]


#: A hw_seed no runner has by accident (the engine is on when it is positive), the one both launches of this file declare: the same GPU, so
#: the engine's own GL in both. The control declares only the canvas noise off.
_HW_SEED = 12345


def _launch_prefs(noise_on: bool) -> dict:
    """The prefs of one launch of this file: the engine always on (the same declared GPU, the engine's own GL), with the canvas noise on, or
    with only the canvas noise declared off (``zoom.stealth.canvas.substitute_pixels`` false: no projection, the bytes as that GPU drew them)."""
    prefs = {"zoom.stealth.fpp.hw_seed": _HW_SEED}
    if not noise_on:
        prefs["zoom.stealth.canvas.substitute_pixels"] = False
    return prefs


def _read(firefox_binary, noise_on):
    out = {}
    with InvisiblePlaywright(seed=21, binary_path=firefox_binary, headless=True,
                             extra_prefs=_launch_prefs(noise_on)) as browser:
        page = browser.new_context().new_page()
        page.goto("about:blank", timeout=30_000)
        for kind in KINDS:
            got = page.evaluate(READ, [kind, SIZE])
            assert "error" not in got, got["error"]
            out[kind] = {name: bytes(values) for name, values in got.items()}
        gl_maps = gl_maps_of_this_test(firefox_binary)
    assert_the_gl_is_the_engines_own(firefox_binary, gl_maps)
    return out


@pytest.fixture(scope="module")
def readings(firefox_binary):
    """kind -> {viaPattern, viaDraw} bytes, with the canvas noise on and off (the engine on in both). The browsers are closed before the tests run."""
    noised = _read(firefox_binary, True)
    control = _read(firefox_binary, False)
    for mode, rows in (("noise on", noised), ("noise off", control)):
        for kind, row in rows.items():
            assert len(row["viaPattern"]) == SIZE * SIZE * 4 and len(set(row["viaPattern"])) > 1, (
                "%s: the %s pattern read back is blank or has the wrong size: the source was not drawn" % (mode, kind))
    return {"noised": noised, "control": control}


def test_create_pattern_from_a_webgl_canvas_carries_the_projection(readings):
    noised, control = readings["noised"]["webgl"], readings["control"]["webgl"]
    assert noised["viaPattern"] != control["viaPattern"], (
        "createPattern(webglCanvas) reads the same pixels with the noise on and off: the pattern is the clean render, the "
        "projection does not reach it")


def test_create_pattern_from_an_offscreen_webgl_canvas_carries_the_projection(readings):
    noised, control = readings["noised"]["offscreen-webgl"], readings["control"]["offscreen-webgl"]
    assert noised["viaPattern"] != control["viaPattern"], (
        "createPattern(offscreenWebglCanvas) reads the same pixels with the noise on and off: the pattern is the clean render, "
        "the projection does not reach it")


@pytest.mark.parametrize("kind", ["webgl", "offscreen-webgl"])
def test_a_pattern_of_a_webgl_canvas_reads_what_drawimage_of_it_reads(readings, kind):
    noised = readings["noised"][kind]
    assert noised["viaPattern"] == noised["viaDraw"], (
        "%s: the pixels of createPattern + fill and of drawImage differ with the noise on: the two reads of one canvas carry "
        "different projections" % kind)


def test_create_pattern_from_a_2d_canvas_reads_the_same_with_the_engine_on_and_off(readings):
    """A 2D source is untouched whether the canvas noise is on or off (the engine is on in both launches): it is read as drawn."""
    noised, control = readings["noised"]["2d"], readings["control"]["2d"]
    assert noised["viaPattern"] == control["viaPattern"], (
        "createPattern(2dCanvas) differs between the noise on and off: a 2D source must be read as drawn")
    assert noised["viaDraw"] == control["viaDraw"], "drawImage(2dCanvas) differs between the noise on and off"
    assert noised["viaPattern"] == noised["viaDraw"], "a pattern of a 2D canvas is not the pixels drawImage reads"
