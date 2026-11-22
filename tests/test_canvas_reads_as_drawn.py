"""A 2D canvas reads back exactly as it was drawn, as on retail Firefox 151.

The canvas noise (the engine's projection) is a WebGL/WebGPU readback only from
firefox-52 (test_webgl_noise_active.py keeps WebGL's side). A 2D canvas, on the
page or offscreen, gives the page the pixels it drew. Each property here has its
known-bad:

* The random-pixel test public fingerprinting pages run: 64 random opaque 1x1
  fills on an 8x8 canvas must read back byte for byte. invisible 0.41 changed
  6 of 64; retail 151 changes 0.
* The same grid on an OffscreenCanvas 2D context: its context type is not the
  page canvas's, and a gate that only knew the page's let it through.
* A canvas nobody drew on reads all zeros.
* Two seeds that declare the same display scale and ClearType tuning draw the
  same text canvas: what separates two machines is how they draw, never a
  per-seed change at readback.
* A toDataURL PNG carries the deBG chunk retail 151 writes.
* A WebGL canvas that is not premultiplied (premultipliedAlpha:false, half
  transparent) reads the same pixels through toDataURL and toBlob, noised with the
  engine on (hw_seed > 0); the control is the engine off (hw_seed 0: as drawn).

Runs against about:blank and a local http page, no network/proxy. Part of the
e2e release gate.
Run: pytest tests/test_canvas_reads_as_drawn.py -m e2e -v
"""
from __future__ import annotations

import hashlib

import pytest

from invisible_playwright import InvisiblePlaywright

_GRID_JS = """(offscreen) => {
  const c = offscreen ? new OffscreenCanvas(8, 8) : document.createElement('canvas');
  c.width = 8; c.height = 8;
  const x = c.getContext('2d');
  const want = []; let s = 7;
  const rnd = () => (s = (s * 1103515245 + 12345) & 0x7fffffff) % 256;
  for (let y = 0; y < 8; y++) for (let i = 0; i < 8; i++) {
    const p = [rnd(), rnd(), rnd()]; want.push(p);
    x.fillStyle = `rgba(${p[0]},${p[1]},${p[2]},1)`; x.fillRect(i, y, 1, 1);
  }
  const got = x.getImageData(0, 0, 8, 8).data; const bad = [];
  for (let i = 0; i < 64; i++)
    if (got[4*i] !== want[i][0] || got[4*i+1] !== want[i][1] || got[4*i+2] !== want[i][2])
      bad.push([i, want[i], [got[4*i], got[4*i+1], got[4*i+2]]]);
  return bad;
}"""

_BLANK_JS = """() => {
  const c = document.createElement('canvas'); c.width = 8; c.height = 8;
  return Math.max(...c.getContext('2d').getImageData(0, 0, 8, 8).data);
}"""

_TEXT_JS = """() => {
  const c = document.createElement('canvas'); c.width = 240; c.height = 60;
  const x = c.getContext('2d');
  x.textBaseline = 'alphabetic'; x.fillStyle = '#f60'; x.fillRect(100, 1, 62, 20);
  x.fillStyle = '#069'; x.font = '11pt "Times New Roman"';
  x.fillText('Cwm fjordbank gly', 2, 15);
  x.fillStyle = 'rgba(102, 204, 0, 0.2)'; x.font = '18pt Arial';
  x.fillText('Cwm fjordbank gly', 4, 45);
  return Array.from(x.getImageData(0, 0, 240, 60).data);
}"""

#: One display scale and one ClearType key for both seeds, so the only thing
#: left to differ between them is what this file forbids.
_SAME_MACHINE = {"screen.dpr": 1.0, "font.cleartype_gamma": 1800,
                 "font.cleartype_contrast": 100}


def _eval(firefox_binary, seed, js, arg=None, pin=None):
    with InvisiblePlaywright(seed=seed, binary_path=firefox_binary,
                             headless=True, pin=pin) as b:
        p = b.new_context().new_page()
        p.goto("about:blank", timeout=30_000)
        return p.evaluate(js, arg) if arg is not None else p.evaluate(js)


@pytest.mark.e2e
@pytest.mark.parametrize("offscreen", [False, True], ids=["canvas", "offscreen"])
@pytest.mark.parametrize("seed", [1, 2])
def test_random_pixels_read_back_as_drawn(firefox_binary, seed, offscreen):
    bad = _eval(firefox_binary, seed, _GRID_JS, offscreen)
    assert not bad, (
        "%d of 64 pixels read back differently from what was drawn (index, "
        "drawn, read): %s. Retail Firefox never changes a 2D pixel at readback, "
        "and public fingerprinting tests draw exactly this grid" % (len(bad), bad[:5]))


@pytest.mark.e2e
def test_a_canvas_nobody_drew_on_reads_zero(firefox_binary):
    assert _eval(firefox_binary, 3, _BLANK_JS) == 0


_PNG_CHUNKS_JS = """() => {
  const c = document.createElement('canvas'); c.width = 16; c.height = 16;
  const x = c.getContext('2d'); x.fillStyle = '#069'; x.fillRect(2, 2, 9, 7);
  const bin = atob(c.toDataURL().split(',')[1]); const out = []; let i = 8;
  while (i < bin.length) {
    const n = (bin.charCodeAt(i) << 24 | bin.charCodeAt(i + 1) << 16 |
               bin.charCodeAt(i + 2) << 8 | bin.charCodeAt(i + 3)) >>> 0;
    out.push([bin.substr(i + 4, 4), n]); i += 12 + n;
  }
  return out;
}"""


@pytest.fixture
def http_page():
    """A page on a real http origin: the protection keys on the top-level site,
    and about:blank is not a site, so a page there gets no chunk, retail
    included."""
    import http.server
    import threading

    class _Page(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<!doctype html><title>canvas</title><p>x"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()


@pytest.mark.e2e
def test_a_png_carries_the_chunk_retail_151_writes(firefox_binary, http_page):
    """Retail 151 ships baseline fingerprinting protection on, and its canvas
    target appends a 16-byte deBG chunk after IDAT to every toDataURL/toBlob PNG
    (measured on signed 151.0, BuildID 20260516144017). A PNG without it is a
    Firefox 151 PNG no real Firefox 151 writes."""
    with InvisiblePlaywright(seed=4, binary_path=firefox_binary, headless=True) as b:
        p = b.new_context().new_page()
        p.goto(http_page, timeout=30_000)
        chunks = p.evaluate(_PNG_CHUNKS_JS)
    names = [name for name, _ in chunks]
    assert ["deBG", 16] in chunks, chunks
    assert names.index("deBG") > names.index("IDAT"), names


@pytest.mark.e2e
def test_two_seeds_on_the_same_machine_draw_the_same_text(firefox_binary):
    a = _eval(firefox_binary, 11, _TEXT_JS, pin=_SAME_MACHINE)
    b = _eval(firefox_binary, 12, _TEXT_JS, pin=_SAME_MACHINE)
    assert hashlib.sha256(bytes(a)).digest() == hashlib.sha256(bytes(b)).digest(), (
        "two seeds declaring the same display scale and ClearType tuning read "
        "different text pixels: something per-seed still changes a 2D canvas")


_NON_PREMULTIPLIED_JS = """async () => {
  const c = document.createElement('canvas'); c.width = 128; c.height = 128;
  const gl = c.getContext('webgl', {premultipliedAlpha: false, alpha: true,
                                    preserveDrawingBuffer: true, antialias: true});
  if (!gl) return null;
  const alpha = 128 / 255;
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
  const url = c.toDataURL();
  const blob = await new Promise(resolve => c.toBlob(resolve, 'image/png'));
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = '';
  for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
  return {url, blob: btoa(binary), premultipliedAlpha: gl.getContextAttributes().premultipliedAlpha};
}"""


def _non_premultiplied(firefox_binary, hw_seed):
    """(the pixels of toDataURL, the pixels of toBlob) of one half-transparent, non-premultiplied WebGL canvas,
    as 128x128 RGBA bytes, with the engine on (hw_seed > 0) or off (0)."""
    import base64

    from _page_bytes import unfiltered

    with InvisiblePlaywright(
        seed=7, binary_path=firefox_binary, headless=True,
        extra_prefs={"zoom.stealth.fpp.hw_seed": hw_seed},
    ) as b:
        p = b.new_context().new_page()
        p.goto("about:blank", timeout=30_000)
        got = p.evaluate(_NON_PREMULTIPLIED_JS)
    assert got is not None, "webgl is unavailable: a WebGL canvas cannot be read"
    assert got["premultipliedAlpha"] is False, "the context is premultiplied: %r" % got["premultipliedAlpha"]
    images = [unfiltered(base64.b64decode(got["url"].split(",", 1)[1])), unfiltered(base64.b64decode(got["blob"]))]
    for png in images:
        assert (png["w"], png["h"], png["channels"]) == (128, 128, 4), png
    return images[0]["data"], images[1]["data"]


@pytest.mark.e2e
def test_a_non_premultiplied_webgl_canvas_reads_the_same_through_todataurl_and_toblob(firefox_binary):
    """toDataURL and toBlob of a premultipliedAlpha:false WebGL canvas give the same pixels, noised with the engine on.

    The canvas noise is applied to the snapshot with its alpha type: a context that is not premultiplied stores
    the colours as drawn, and a reader that took it for premultiplied would noise (or convert) them wrongly.
    Both PNG readers go through the same hook (GetImageBuffer, GetInputStream in ClientWebGLContext.cpp). The
    canvas is half transparent (alpha 128/255, colours as drawn), so a premultiplied reading changes the bytes.
    Negative control, in the same test: with the engine off the two readers are the bytes as drawn, their alpha is
    the 128 drawn, and the engine-on PNG differs from them."""
    on_url, on_blob = _non_premultiplied(firefox_binary, 12345)
    off_url, off_blob = _non_premultiplied(firefox_binary, 0)
    alphas = {off_url[i] for i in range(3, len(off_url), 4)}
    assert alphas <= set(range(126, 131)), (
        "engine off: the alpha of the PNG is %s, not the 128 that was drawn" % sorted(alphas))
    differing = lambda a, b: sum(1 for x, y in zip(a, b) if x != y)
    assert off_blob == off_url, (
        "engine off: %d of %d bytes of toBlob differ from toDataURL" % (differing(off_blob, off_url), len(off_url)))
    assert differing(on_url, off_url), (
        "toDataURL of a non-premultiplied WebGL canvas is the same with the engine on and off: no noise reaches "
        "the page, so comparing the two readers says nothing")
    assert on_blob == on_url, (
        "engine on: %d of %d bytes of toBlob differ from toDataURL of the same non-premultiplied WebGL canvas"
        % (differing(on_blob, on_url), len(on_url)))
