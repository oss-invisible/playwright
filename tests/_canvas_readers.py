"""What the canvas-reader e2e tests share: one scene, the readers of a canvas, the page that serves them, and the comparison of bytes.

The engine applies its canvas noise (``gfx::StealthProjectPixels``) to a WebGL or WebGPU canvas when a PAGE reads it, and only then. These
tests ask whether every road by which a page reads such a canvas gives the same bytes, so they need one scene that the noise touches, the same
scene drawn through every API, and the same list of readers for every kind of canvas.

THE SCENE. Ten triangles with random vertex colours (a fixed generator, the same numbers in every launch and in every API: ``starVertices``)
over a white clear. The noise touches only an edge pixel whose 3x3 neighbourhood holds a THIRD colour besides a blend of two, so a flat clear
is never touched; ten overlapping triangles make hundreds of such junctions, and the chance that two reads of a noised and an un-noised canvas
differ nowhere is nil. The 2D twin (``draw2D``) draws the same triangles with translucent fills and a shadow: a canvas that WOULD be noised if
the 2D road were projected, and is not (a 2D canvas reads as drawn).

THE CONTROL. ``noised=False`` declares ``zoom.stealth.canvas.substitute_pixels`` false: the canvas noise is declared off and NOTHING else changes
(same declared GPU, same antialiasing, same raster), so the difference between the two launches is the noise and nothing else, which is why
the tests may ask that it touches only the low bit of R, G and B. (A launch with ``hw_seed`` 0 would also change the declared GPU.)

THE READERS (``readersOf``): the canvas's own read (``readPixels`` / ``getImageData``), ``drawImage`` onto a 2D canvas, ``createImageBitmap``, the
PNG of ``toDataURL`` / ``toBlob`` / ``convertToBlob`` (decoded here, never by the browser, so the browser's image decoding is not a variable)
and, for an OffscreenCanvas, ``transferToImageBitmap``. Every entry is the base64 of RGBA bytes, top row first; a name that ends in ``.png`` is a
PNG file.

Nothing here waits on a clock: the page's async functions end on events, and the one wait that needs polling (``wait_until``) ends on a condition.
"""
from __future__ import annotations

import base64
import contextlib
import json
import threading
from http.server import BaseHTTPRequestHandler
from typing import Dict, List

import _page_bytes as pb
from _servers import LocalServer

#: The side of every canvas of the scene.
SIZE = 128
#: A noise seed that no runner has.
HW_SEED = 12345
#: The persona (identity) seed of every launch: not the noise seed.
SEED = 7

#: The JS of the scene and of the readers. Plain script: it runs in the page and, as the head of a worker's source, in a worker (no ``document`` in it).
SHARED_JS = r"""
const W = 128, H = 128;

// ten triangles, 30 vertices of (x, y, r, g, b), x and y in clip space: a fixed generator, so every launch and every API draws the same scene
function starVertices() {
  const v = []; let s = 12345;
  const rnd = () => (s = (s * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
  for (let i = 0; i < 30; i++) v.push(rnd() * 1.8 - 0.9, rnd() * 1.8 - 0.9, rnd(), rnd(), rnd());
  return v;
}

function drawGL(canvas, kind) {
  const gl = canvas.getContext(kind || 'webgl', {preserveDrawingBuffer: true, antialias: true});
  if (!gl) throw new Error('no ' + (kind || 'webgl') + ' context');
  const shader = (type, src) => {
    const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
    return s;
  };
  const pr = gl.createProgram();
  gl.attachShader(pr, shader(gl.VERTEX_SHADER, 'attribute vec2 p; attribute vec3 c; varying vec3 v; void main(){v=c;gl_Position=vec4(p,0.,1.);}'));
  gl.attachShader(pr, shader(gl.FRAGMENT_SHADER, 'precision mediump float; varying vec3 v; void main(){gl_FragColor=vec4(v,1.);}'));
  gl.linkProgram(pr);
  if (!gl.getProgramParameter(pr, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(pr));
  gl.useProgram(pr);
  const b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(starVertices()), gl.STATIC_DRAW);
  const pl = gl.getAttribLocation(pr, 'p'), cl = gl.getAttribLocation(pr, 'c');
  gl.enableVertexAttribArray(pl); gl.vertexAttribPointer(pl, 2, gl.FLOAT, false, 20, 0);
  gl.enableVertexAttribArray(cl); gl.vertexAttribPointer(cl, 3, gl.FLOAT, false, 20, 8);
  gl.clearColor(1, 1, 1, 1); gl.clear(gl.COLOR_BUFFER_BIT);
  gl.drawArrays(gl.TRIANGLES, 0, 30);
  gl.finish();
  return gl;
}

// the same ten triangles on a 2D canvas, with translucent fills and a shadow
function draw2D(x) {
  x.fillStyle = '#fff'; x.fillRect(0, 0, W, H);
  const v = starVertices();
  for (let t = 0; t < 10; t++) {
    const q = v.slice(t * 15, t * 15 + 15);
    x.fillStyle = 'rgba(' + Math.round(q[2] * 255) + ',' + Math.round(q[3] * 255) + ',' + Math.round(q[4] * 255) + ',' + (t % 2 ? 0.7 : 1) + ')';
    x.beginPath();
    for (let k = 0; k < 3; k++) {
      const px = (q[k * 5] + 1) / 2 * W, py = (1 - q[k * 5 + 1]) / 2 * H;
      if (k) x.lineTo(px, py); else x.moveTo(px, py);
    }
    x.closePath(); x.fill();
  }
  x.shadowColor = 'rgba(0, 0, 0, 0.6)'; x.shadowBlur = 4; x.shadowOffsetX = 3; x.shadowOffsetY = 2;
  x.fillStyle = 'rgb(200, 120, 20)'; x.fillRect(40, 40, 30, 30);
}

// readPixels, top row first (readPixels gives the bottom row first)
function readGL(gl) {
  const buf = new Uint8Array(W * H * 4);
  gl.readPixels(0, 0, W, H, gl.RGBA, gl.UNSIGNED_BYTE, buf);
  const out = new Uint8Array(W * H * 4);
  for (let y = 0; y < H; y++) out.set(buf.subarray((H - 1 - y) * W * 4, (H - y) * W * 4), y * W * 4);
  return out;
}

function readCtx(x) { return new Uint8Array(x.getImageData(0, 0, W, H).data); }

function b64(u8) {
  let s = '';
  for (let i = 0; i < u8.length; i += 8192) s += String.fromCharCode.apply(null, u8.subarray(i, i + 8192));
  return btoa(s);
}

function blobB64(blob) {
  return new Promise((ok, bad) => {
    const r = new FileReader();
    r.onload = () => ok(String(r.result).split(',')[1]);
    r.onerror = () => bad(new Error('FileReader failed'));
    r.readAsDataURL(blob);
  });
}

// what a fresh 2D canvas (made by `make`) holds after `draw(context)`
function pixelsOf(make, draw) {
  const c = make(); c.width = W; c.height = H;
  const x = c.getContext('2d'); draw(x);
  return readCtx(x);
}

// every road by which a page turns `src` into bytes: {name: base64 of RGBA, top row first}; a name ending in .png is a PNG file
async function readersOf(src, direct, make, withTransfer) {
  const out = {direct: b64(direct())};
  out.drawImage = b64(pixelsOf(make, x => x.drawImage(src, 0, 0)));
  const bm = await createImageBitmap(src);
  out.createImageBitmap = b64(pixelsOf(make, x => x.drawImage(bm, 0, 0)));
  bm.close();
  if (src.toDataURL) {
    out['toDataURL.png'] = src.toDataURL('image/png').split(',')[1];
    out['toBlob.png'] = await blobB64(await new Promise(r => src.toBlob(r, 'image/png')));
  } else {
    out['convertToBlob.png'] = await blobB64(await src.convertToBlob({type: 'image/png'}));
  }
  if (withTransfer && src.transferToImageBitmap) {   // last: it empties the canvas
    const tb = src.transferToImageBitmap();
    out.transferToImageBitmap = b64(pixelsOf(make, x => x.drawImage(tb, 0, 0)));
    tb.close();
  }
  return out;
}

const makeElement = () => document.createElement('canvas');
const makeOffscreen = () => new OffscreenCanvas(W, H);
"""


def page_html(page_js: str) -> str:
    """A page whose script holds SHARED_JS, ``WORKER_LIB`` (the same text, for the source of a worker) and the page's own functions."""
    return ("<!doctype html><meta charset=utf-8><title>canvas readers</title><body><script>\n" + SHARED_JS
            + "\nconst WORKER_LIB = " + json.dumps(SHARED_JS) + ";\n" + page_js + "\n</script></body>")


@contextlib.contextmanager
def serve(html: str):
    """The page on a local http origin (a secure context, and a site a canvas has a principal on); port 0, the one the system gave."""
    body = html.encode("utf-8")

    class Page(BaseHTTPRequestHandler):
        timeout = 5

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = LocalServer(("127.0.0.1", 0), Page)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d/" % httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


@contextlib.contextmanager
def launched(firefox_binary, html: str, noised: bool = True, pin=None, viewport=None):
    """The page open in a browser of the product: the engine on (a declared noise seed no runner has), and, when ``noised`` is false, the
    canvas noise declared off and nothing else. One browser, closed on leaving."""
    from invisible_playwright import InvisiblePlaywright

    prefs = {"zoom.stealth.fpp.hw_seed": HW_SEED}
    if not noised:
        prefs["zoom.stealth.canvas.substitute_pixels"] = False
    with serve(html) as url:
        with InvisiblePlaywright(seed=SEED, binary_path=firefox_binary, headless=True, extra_prefs=prefs, pin=pin) as browser:
            context = browser.new_context(viewport=viewport) if viewport else browser.new_context()
            page = context.new_page()
            page.goto(url, wait_until="load", timeout=60_000)
            yield page


def run(page, call: str, deadline_s: int = 120):
    """Runs the page's async function ``call`` (a JS expression that gives a promise) to its end and returns what it resolved to. The end is
    the promise's own: the deadline is only how long a lost event is allowed before the test says so."""
    page.evaluate("() => { window.DONE = false; window.ERR = null; window.R = null;"
                  " Promise.resolve().then(() => %s).then(r => { window.R = r; window.DONE = true; },"
                  " e => { window.ERR = String((e && e.stack) || e); window.DONE = true; }); }" % call)
    page.wait_for_function("window.DONE === true", timeout=deadline_s * 1000)
    err = page.evaluate("window.ERR")
    assert err is None, "the page could not run %s: %s" % (call, err)
    return page.evaluate("window.R")


# ------------------------------------------------------------------------------------------------------------------ bytes

def rgba_of_image(image) -> bytes:
    """RGBA bytes of an ``_page_bytes.unfiltered`` image (RGB gets an opaque alpha)."""
    data, channels = image["data"], image["channels"]
    if channels == 4:
        return data
    assert channels == 3, "a PNG of %d channels is not read here" % channels
    out = bytearray()
    for i in range(0, len(data), 3):
        out += data[i:i + 3] + b"\xff"
    return bytes(out)


def rgba(name: str, value: str) -> bytes:
    """The RGBA bytes of a reader's entry: its base64, or, for a PNG, the pixels it decodes to."""
    raw = base64.b64decode(value)
    if name.endswith(".png"):
        image = pb.unfiltered(raw)
        assert (image["w"], image["h"]) == (SIZE, SIZE), "%s is a %dx%d image, not %dx%d" % (name, image["w"], image["h"], SIZE, SIZE)
        return rgba_of_image(image)
    assert len(raw) == SIZE * SIZE * 4, "%s holds %d bytes, not %d" % (name, len(raw), SIZE * SIZE * 4)
    return raw


def decode(readers: Dict[str, str], prefix: str = "") -> Dict[str, bytes]:
    return {prefix + name: rgba(name, value) for name, value in readers.items()}


def differing_pixels(a: bytes, b: bytes) -> int:
    assert len(a) == len(b), "two images of %d and %d bytes" % (len(a), len(b))
    return sum(1 for i in range(0, len(a), 4) if a[i:i + 4] != b[i:i + 4])


def only_the_low_bit_of_rgb(a: bytes, b: bytes) -> List[int]:
    """The pixels where ``a`` and ``b`` differ by more than the low bit of R, G and B (or in alpha): the noise writes nothing else."""
    return [i // 4 for i in range(0, len(a), 4)
            if (a[i] | 1, a[i + 1] | 1, a[i + 2] | 1, a[i + 3]) != (b[i] | 1, b[i + 1] | 1, b[i + 2] | 1, b[i + 3])]


def distinct_colours(a: bytes) -> int:
    return len({a[i:i + 4] for i in range(0, len(a), 4)})


def disagreements(readers: Dict[str, bytes], reference: str) -> List[str]:
    """The readers whose bytes are not those of ``reference``, each with how many pixels differ."""
    want = readers[reference]
    return ["%s (%d pixels differ)" % (name, differing_pixels(want, got)) for name, got in readers.items() if got != want]


def assert_agree(what: str, readers: Dict[str, bytes], reference: str = "direct") -> None:
    bad = disagreements(readers, reference)
    assert not bad, (
        "%s: the readers %s do not read what %s reads (readers: %s). Every road by which a page reads a canvas gives the same bytes "
        "when it is the same canvas" % (what, "; ".join(bad), reference, ", ".join(sorted(readers))))
