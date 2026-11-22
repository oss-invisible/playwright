"""The WebGL readback noise is where a device varies, and nowhere else.

Two properties, each with its known-bad:

* A realistic render - a triangle with per-vertex colours (a gradient) and
  antialiased edges, what WebGL fingerprinters draw - reads back differently
  for two seeds. That is where a real device varies (edges, gradients,
  blending), and where the per-seed entropy belongs.
* A flat clear reads back EXACTLY as cleared. A clear has no device variation:
  the value is the clear colour quantised to 8 bits, the same on every GPU, and
  a page that knows what it cleared can compare. Noise there is a tell. The
  per-seed gamma remap of firefox-50 moved those values, and the bits form B
  put along a column before 17140065 did too ([B363]); both fail here, the
  retail passes by nature.

The premise this file used to state, "a high-entropy render must vary per
seed", was wrong: distinct colours are not device entropy, and a column of
identical cleared pixels has none. It drew 64 scissor-cleared stripes and
asserted they differ between seeds, i.e. it asserted the tell.

The clear colours are v/255 for integer v, so the expected byte is exactly v
whatever the GPU's rounding: a k/64 colour lands on 127.5 at k=32, where two
correct drivers may disagree.

Three more tests characterise the roads by which a page reads a WebGL canvas (the
engine's WebGL readback hooks: ClientWebGLContext.cpp and StealthWebGLReadback.h
of the fingerprint/canvas/noise-projection folder). Each reads with the engine on
(zoom.stealth.fpp.hw_seed > 0) and, as the negative control in the same test, with the
canvas noise declared off (zoom.stealth.canvas.substitute_pixels false, the engine still
on: the bytes as drawn), and asserts the two differ, or no noise reached
the road the test compares:

* readPixels into a PIXEL_PACK_BUFFER (WebGL2), read back with getBufferSubData,
  gives the bytes of readPixels into an array.
* A frame of canvas.captureStream() has the bytes of toDataURL of the same canvas.
* readPixels of a rectangle partly outside the framebuffer, and one with
  PACK_SKIP_PIXELS / PACK_SKIP_ROWS / PACK_ROW_LENGTH, change only the bytes the
  read wrote: the rest of the page's array stays what the page passed in, and the
  bytes written are those of a plain read of the same pixels.

Kept in its own file: it launches its own short-lived browsers, so it must not
run alongside another module's shared browser (the Playwright sync API cannot
nest). Run: pytest tests/test_webgl_noise_active.py -m e2e -v
"""
from __future__ import annotations

import contextlib
import hashlib

import pytest

from _page_bytes import unfiltered
from _waits import wait_until
from invisible_playwright import InvisiblePlaywright

_GRADIENT_JS = """() => {
  const c = document.createElement('canvas'); c.width = 128; c.height = 128;
  const gl = c.getContext('webgl', {preserveDrawingBuffer: true, antialias: true});
  if (!gl) return null;
  const vs = gl.createShader(gl.VERTEX_SHADER);
  gl.shaderSource(vs, 'attribute vec2 p; attribute vec3 c; varying vec3 v;'
    + ' void main(){v=c;gl_Position=vec4(p,0.,1.);}');
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
  const buf = new Uint8Array(128 * 128 * 4); gl.finish();
  gl.readPixels(0, 0, 128, 128, gl.RGBA, gl.UNSIGNED_BYTE, buf);
  return Array.from(buf);
}"""

#: Column k of the flat render, as (R, G, B): exact 8-bit values.
_STRIPES = [(4 * k, 255 - 4 * k, (37 * k) % 256) for k in range(64)]

_STRIPES_JS = """(cols) => {
  const c = document.createElement('canvas'); c.width = 64; c.height = 64;
  const gl = c.getContext('webgl', {preserveDrawingBuffer: true});
  if (!gl) return null;
  gl.enable(gl.SCISSOR_TEST);
  for (let k = 0; k < 64; k++) {
    gl.scissor(k, 0, 1, 64);
    gl.clearColor(cols[k][0] / 255, cols[k][1] / 255, cols[k][2] / 255, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
  }
  const buf = new Uint8Array(64 * 64 * 4); gl.finish();
  gl.readPixels(0, 0, 64, 64, gl.RGBA, gl.UNSIGNED_BYTE, buf);
  return Array.from(buf);
}"""


def _read(firefox_binary, seed: int, js: str, arg=None):
    with InvisiblePlaywright(
        seed=seed, binary_path=firefox_binary, headless=True,
        extra_prefs={"zoom.stealth.fpp.hw_seed": 1000 + seed},
    ) as b:
        p = b.new_context().new_page()
        p.goto("about:blank", timeout=30_000)
        arr = p.evaluate(js, arg) if arg is not None else p.evaluate(js)
    return None if arr is None else bytes(arr)


@pytest.mark.e2e
def test_a_realistic_webgl_render_reads_differently_per_seed(firefox_binary):
    """Edges and a gradient are where devices differ: two seeds, two hashes."""
    a = _read(firefox_binary, 1, _GRADIENT_JS)
    b = _read(firefox_binary, 2, _GRADIENT_JS)
    if a is None or b is None:
        pytest.skip("webgl unavailable")
    assert hashlib.sha256(a).digest() != hashlib.sha256(b).digest(), (
        "a gradient with antialiased edges reads back the same for two seeds: "
        "the WebGL readback carries no per-seed noise where devices vary")


@pytest.mark.e2e
@pytest.mark.parametrize("seed", [1, 2])
def test_a_flat_clear_reads_back_exactly_as_cleared(firefox_binary, seed):
    """Every pixel of every cleared column is the cleared 8-bit value."""
    got = _read(firefox_binary, seed, _STRIPES_JS, _STRIPES)
    if got is None:
        pytest.skip("webgl unavailable")
    moved = []
    for y in range(64):
        for k, colour in enumerate(_STRIPES):
            i = (y * 64 + k) * 4
            if tuple(got[i:i + 3]) != colour:
                moved.append(((k, y), colour, tuple(got[i:i + 3])))
    assert not moved, (
        "%d of 4096 cleared pixels do not read back as cleared (column, row, "
        "cleared, read): %s. A clear is the same bytes on every GPU, so noise "
        "on it is a tell" % (len(moved), moved[:5]))


#: A hw_seed no runner has by accident (the engine is on when it is positive). The control of the tests below keeps the engine ON, with the
#: same declared GPU, and declares only the canvas noise off (``zoom.stealth.canvas.substitute_pixels`` false): a launch with hw_seed 0 would
#: change the declared GPU too, and a Windows runner without the engine has no WebGL2 at all, so the comparison would say nothing.
_ENGINE_ON = 12345
_ENGINE_OFF = 0   # "the noise is off": the engine stays on (see _page)

#: Draws what the tests below read: a triangle with per-vertex colours and antialiased edges on a white clear,
#: the render of _GRADIENT_JS, where a device varies and so where the noise lands. ``scene(canvas, kind, attrs, alpha)``
#: returns the context, drawn and finished, or null when there is no such context.
_SCENE_JS = """
const scene = (c, kind, attrs, alpha) => {
  const gl = c.getContext(kind, attrs);
  if (!gl) return null;
  const vs = gl.createShader(gl.VERTEX_SHADER);
  gl.shaderSource(vs, 'attribute vec2 p; attribute vec3 c; varying vec3 v;'
    + ' void main(){v=c;gl_Position=vec4(p,0.,1.);}');
  gl.compileShader(vs);
  const fs = gl.createShader(gl.FRAGMENT_SHADER);
  gl.shaderSource(fs, 'precision mediump float; varying vec3 v; void main(){gl_FragColor=vec4(v,'
    + alpha.toFixed(8) + ');}');
  gl.compileShader(fs);
  const pr = gl.createProgram(); gl.attachShader(pr, vs); gl.attachShader(pr, fs);
  gl.linkProgram(pr); gl.useProgram(pr);
  const b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(
    [-0.9, -0.8, 1, 0, 0,  0.85, -0.6, 0, 1, 0,  0.1, 0.9, 0, 0, 1]), gl.STATIC_DRAW);
  const pl = gl.getAttribLocation(pr, 'p'), cl = gl.getAttribLocation(pr, 'c');
  gl.enableVertexAttribArray(pl); gl.vertexAttribPointer(pl, 2, gl.FLOAT, false, 20, 0);
  gl.enableVertexAttribArray(cl); gl.vertexAttribPointer(cl, 3, gl.FLOAT, false, 20, 8);
  gl.clearColor(1, 1, 1, alpha); gl.clear(gl.COLOR_BUFFER_BIT); gl.drawArrays(gl.TRIANGLES, 0, 3);
  gl.finish();
  return gl;
};
"""


@contextlib.contextmanager
def _page(firefox_binary, hw_seed: int):
    """One short-lived browser at a blank page, the engine always on: with the noise on (hw_seed > 0) or with only the canvas noise
    declared off (``_ENGINE_OFF``: ``zoom.stealth.canvas.substitute_pixels`` false, the bytes as drawn by the same declared GPU)."""
    prefs = {"zoom.stealth.fpp.hw_seed": _ENGINE_ON}
    if hw_seed == _ENGINE_OFF:
        prefs["zoom.stealth.canvas.substitute_pixels"] = False
    with InvisiblePlaywright(
        seed=7, binary_path=firefox_binary, headless=True,
        extra_prefs=prefs,
    ) as b:
        p = b.new_context().new_page()
        p.goto("about:blank", timeout=30_000)
        yield p


def _differing(a, b) -> int:
    """How many bytes of two equally long sequences differ."""
    assert len(a) == len(b), (len(a), len(b))
    return sum(1 for x, y in zip(a, b) if x != y)


_PBO_JS = "() => {" + _SCENE_JS + """
  const W = 128, H = 128, OFFSET = 16;
  const c = document.createElement('canvas'); c.width = W; c.height = H;
  const gl = scene(c, 'webgl2', {preserveDrawingBuffer: true, antialias: true}, 1);
  if (!gl) return null;
  while (gl.getError()) {}
  const into = new Uint8Array(W * H * 4);
  gl.readPixels(0, 0, W, H, gl.RGBA, gl.UNSIGNED_BYTE, into);
  const pbo = gl.createBuffer();
  gl.bindBuffer(gl.PIXEL_PACK_BUFFER, pbo);
  gl.bufferData(gl.PIXEL_PACK_BUFFER, OFFSET + W * H * 4, gl.STREAM_READ);
  gl.readPixels(0, 0, W, H, gl.RGBA, gl.UNSIGNED_BYTE, OFFSET);
  const error = gl.getError();
  const back = new Uint8Array(OFFSET + W * H * 4);
  gl.getBufferSubData(gl.PIXEL_PACK_BUFFER, 0, back);
  gl.bindBuffer(gl.PIXEL_PACK_BUFFER, null);
  return {error, into: Array.from(into), pbo: Array.from(back.subarray(OFFSET)),
          head: Array.from(back.subarray(0, OFFSET))};
}"""


def _pbo(firefox_binary, hw_seed: int):
    with _page(firefox_binary, hw_seed) as p:
        got = p.evaluate(_PBO_JS)
    assert got is not None, "webgl2 is unavailable: readPixels into a PIXEL_PACK_BUFFER cannot be read"
    assert got["error"] == 0, "readPixels into a PIXEL_PACK_BUFFER raised 0x%x" % got["error"]
    assert got["head"] == [0] * 16, "the 16 bytes before the offset of the read were written: %r" % got["head"]
    return bytes(got["into"]), bytes(got["pbo"])


@pytest.mark.e2e
def test_readpixels_into_a_pixel_pack_buffer_reads_as_into_an_array(firefox_binary):
    """The two roads a page reads WebGL2 pixels by give the same bytes, with the engine on and off.

    With the engine on the PIXEL_PACK_BUFFER road (the host writes the buffer, the engine rewrites it with the noise
    row by row) must give what readPixels into an array gives. With the engine off both are the bytes as drawn, and
    the array differs from the engine-on array: the noise reaches the page on the array road, so the equality above is
    not two untouched reads."""
    on_array, on_pbo = _pbo(firefox_binary, _ENGINE_ON)
    off_array, off_pbo = _pbo(firefox_binary, _ENGINE_OFF)
    assert off_pbo == off_array, (
        "engine off: %d of %d bytes of the buffer read differ from the array read of the same render"
        % (_differing(off_pbo, off_array), len(off_array)))
    assert _differing(on_array, off_array), (
        "the array read is the same with the engine on and off: no noise reaches readPixels, so the comparison "
        "of the two roads says nothing")
    assert on_pbo == on_array, (
        "engine on: %d of %d bytes read from a PIXEL_PACK_BUFFER differ from readPixels into an array; "
        "off, the buffer differs from the array by %d bytes"
        % (_differing(on_pbo, on_array), len(on_array), _differing(off_pbo, off_array)))


_CAPTURE_START_JS = "() => {" + _SCENE_JS + """
  const c = document.createElement('canvas'); c.width = 64; c.height = 64;
  const gl = scene(c, 'webgl', {preserveDrawingBuffer: true, antialias: true}, 1);
  if (!gl) return false;
  window.__png = c.toDataURL();
  const stream = c.captureStream();
  const v = document.createElement('video');
  v.muted = true; v.autoplay = true; v.srcObject = stream;
  document.body.appendChild(v);
  window.__video = v; window.__track = stream.getVideoTracks()[0]; window.__frame = null;
  v.play().catch(() => {});
  return true;
}"""

#: One look at the video: when it holds a frame with something on it, copy it through a 2D canvas (which reads as
#: drawn) and say so; otherwise ask the track for a frame again.
_CAPTURE_POLL_JS = """() => {
  if (window.__frame) return true;
  const v = window.__video;
  if (v.readyState >= 2 && v.videoWidth === 64 && v.videoHeight === 64) {
    const c = document.createElement('canvas'); c.width = 64; c.height = 64;
    const x = c.getContext('2d'); x.drawImage(v, 0, 0);
    const d = x.getImageData(0, 0, 64, 64).data;
    if (d.some(b => b !== 0)) { window.__frame = Array.from(d); return true; }
  }
  window.__track.requestFrame();
  return false;
}"""


def _capture(firefox_binary, hw_seed: int):
    """(a frame of canvas.captureStream() as 64x64 RGBA bytes, the pixels of toDataURL of the same canvas)."""
    import base64
    with _page(firefox_binary, hw_seed) as p:
        assert p.evaluate(_CAPTURE_START_JS), "webgl is unavailable: captureStream of a WebGL canvas cannot be read"
        got = wait_until(lambda: p.evaluate(_CAPTURE_POLL_JS), 60, lambda: p.wait_for_timeout(100))
        assert got, "the video of canvas.captureStream() never held a frame with something on it"
        frame, url = p.evaluate("() => [window.__frame, window.__png]")
    png = unfiltered(base64.b64decode(url.split(",", 1)[1]))
    assert (png["w"], png["h"], png["channels"]) == (64, 64, 4), png
    return bytes(frame), png["data"]


@pytest.mark.e2e
def test_a_capturestream_frame_reads_as_todataurl(firefox_binary):
    """A frame a page takes from canvas.captureStream() has the bytes toDataURL of the same canvas has.

    captureStream is the one reader of the async snapshot, and a road of its own: the engine must apply the noise
    there too, once. With the engine off the frame is the render as drawn and still equals toDataURL; with it on
    the frame differs from the one of the engine off (the noise reaches this road) and equals toDataURL."""
    on_frame, on_url = _capture(firefox_binary, _ENGINE_ON)
    off_frame, off_url = _capture(firefox_binary, _ENGINE_OFF)
    assert off_frame == off_url, (
        "engine off: %d of %d bytes of a captureStream frame differ from toDataURL of the same canvas"
        % (_differing(off_frame, off_url), len(off_url)))
    assert _differing(on_url, off_url), (
        "toDataURL is the same with the engine on and off: no noise reaches the page, so comparing the two "
        "roads says nothing")
    assert _differing(on_frame, off_frame), (
        "a captureStream frame is the same with the engine on and off: the noise does not reach captureStream")
    assert on_frame == on_url, (
        "engine on: %d of %d bytes of a captureStream frame differ from toDataURL of the same canvas"
        % (_differing(on_frame, on_url), len(on_url)))


_PARTIAL_JS = "() => {" + _SCENE_JS + """
  const W = 128, H = 128;
  const c = document.createElement('canvas'); c.width = W; c.height = H;
  const gl = scene(c, 'webgl2', {preserveDrawingBuffer: true, antialias: true}, 1);
  if (!gl) return null;
  while (gl.getError()) {}
  const errors = [];
  const filled = n => { const a = new Uint8Array(n); for (let i = 0; i < n; i++) a[i] = 1 + (i * 7) % 251; return a; };
  const full = new Uint8Array(W * H * 4);
  gl.readPixels(0, 0, W, H, gl.RGBA, gl.UNSIGNED_BYTE, full);
  errors.push(gl.getError());
  // A rectangle that starts 16 pixels left of and below the framebuffer: its first 16 columns and rows are outside.
  const part = filled(96 * 96 * 4), partPassed = part.slice();
  gl.readPixels(-16, -16, 96, 96, gl.RGBA, gl.UNSIGNED_BYTE, part);
  errors.push(gl.getError());
  // The same pixels as a plain read, put 5 pixels right and 3 rows down in rows of 80 pixels.
  gl.pixelStorei(gl.PACK_ROW_LENGTH, 80);
  gl.pixelStorei(gl.PACK_SKIP_PIXELS, 5);
  gl.pixelStorei(gl.PACK_SKIP_ROWS, 3);
  const skip = filled((3 + 64) * 80 * 4), skipPassed = skip.slice();
  gl.readPixels(0, 0, 64, 64, gl.RGBA, gl.UNSIGNED_BYTE, skip);
  errors.push(gl.getError());
  gl.pixelStorei(gl.PACK_ROW_LENGTH, 0);
  gl.pixelStorei(gl.PACK_SKIP_PIXELS, 0);
  gl.pixelStorei(gl.PACK_SKIP_ROWS, 0);
  return {errors, full: Array.from(full), part: Array.from(part), partPassed: Array.from(partPassed),
          skip: Array.from(skip), skipPassed: Array.from(skipPassed)};
}"""


def _partial(firefox_binary, hw_seed: int):
    """Reads of one render: the whole framebuffer, a rectangle partly outside it, and a packed read. Returns the
    bytes of the whole read and the pixels of the other two that are not where they should be (empty when none)."""
    with _page(firefox_binary, hw_seed) as p:
        got = p.evaluate(_PARTIAL_JS)
    assert got is not None, "webgl2 is unavailable: readPixels with PACK_SKIP_* cannot be read"
    assert got["errors"] == [0, 0, 0], "readPixels raised %r" % got["errors"]
    full = got["full"]
    pixel = lambda arr, i: tuple(arr[4 * i:4 * i + 4])
    problems = []
    # The rectangle at (-16, -16), 96x96: request pixel (c, r) is framebuffer pixel (c - 16, r - 16); written when
    # that is inside the 128x128 framebuffer, that is for c, r in [16, 96).
    for r in range(96):
        for c in range(96):
            i = r * 96 + c
            if c >= 16 and r >= 16:
                want, what = pixel(full, (r - 16) * 128 + (c - 16)), "a plain read"
            else:
                want, what = pixel(got["partPassed"], i), "what the page passed in"
            if pixel(got["part"], i) != want:
                problems.append(("rectangle", (c, r), pixel(got["part"], i), want, what))
    # 64x64 at (0, 0) with skip 5 / 3 and rows of 80: request pixel (c, r) is at column c + 5 of row r + 3.
    for row in range(3 + 64):
        for col in range(80):
            i = row * 80 + col
            c, r = col - 5, row - 3
            if 0 <= c < 64 and 0 <= r < 64:
                want, what = pixel(full, r * 128 + c), "a plain read"
            else:
                want, what = pixel(got["skipPassed"], i), "what the page passed in"
            if pixel(got["skip"], i) != want:
                problems.append(("packed", (c, r), pixel(got["skip"], i), want, what))
    return bytes(full), problems


@pytest.mark.e2e
def test_readpixels_of_a_partial_rectangle_projects_only_what_was_written(firefox_binary):
    """readPixels changes the bytes it wrote and no other, and writes what a plain read of those pixels gives.

    Two reads of one render: a 96x96 rectangle that starts 16 pixels outside the framebuffer (only its inside part
    is written), and a 64x64 read with PACK_ROW_LENGTH 80, PACK_SKIP_PIXELS 5 and PACK_SKIP_ROWS 3. The bytes the
    read did not write must be what the page passed in, and the ones it wrote those of a plain read of the same
    framebuffer pixels (noised, with the engine on). Engine off, the same holds with the bytes as drawn; and the
    plain read differs from the engine-on one, so the noise is in what is compared."""
    on_full, on_problems = _partial(firefox_binary, _ENGINE_ON)
    off_full, off_problems = _partial(firefox_binary, _ENGINE_OFF)
    assert not off_problems, "engine off: %d pixels are not where they should be (read, pixel, got, want, from): %s" % (
        len(off_problems), off_problems[:5])
    assert _differing(on_full, off_full), (
        "the plain read is the same with the engine on and off: no noise reaches readPixels, so the comparison "
        "says nothing")
    assert not on_problems, (
        "engine on: %d pixels are not where they should be (read, pixel, got, want, from): %s. A pixel outside "
        "the rectangle that was written must stay what the page passed in, and one inside it must be what a "
        "plain read of the same pixel gives" % (len(on_problems), on_problems[:5]))
