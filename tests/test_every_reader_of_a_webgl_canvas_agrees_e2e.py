"""Every road by which a page reads a WebGL canvas gives it the same bytes: the element, an OffscreenCanvas, the placeholder of
``transferControlToOffscreen`` (e2e, every OS; written on firefox-68, which the engine of the series must keep).

The engine applies its canvas noise (``gfx::StealthProjectPixels``, a pure function of the seed and of what the canvas holds) when a page
reads a WebGL or WebGPU canvas, and does not touch a 2D canvas, as on retail. Until firefox-50 the noise was bolted onto five read methods
and every other road read the real pixels: a page saw it by reading the canvas twice. The hooks that keep it one function are the page snapshot of
``HTMLCanvasElement`` (``GetSurfaceSnapshot``, whose Mozilla body is the as-drawn reader), the snapshot of ``OffscreenCanvas``, and the snapshot of
the display helper behind a transferred placeholder (``fingerprint/canvas/noise-projection``: dom/html/HTMLCanvasElement.cpp,
dom/canvas/OffscreenCanvas.cpp, dom/canvas/OffscreenCanvasDisplayHelper.cpp). A page reads each of these roads with its own API and compares
the answers.

Three properties, each with its control IN THE SAME TEST (the canvas noise declared off, ``zoom.stealth.canvas.substitute_pixels`` false, every
other declaration the same: a second launch of the same page):

* ``test_every_reader_of_a_webgl_canvas_element_reads_the_same_bytes``: ``readPixels``, ``drawImage``, ``createImageBitmap``, the PNG of
  ``toDataURL`` and of ``toBlob`` read the same bytes, which are not the bytes of the un-noised canvas (the noise is there) and differ from them
  only in the low bit of R, G and B. A 2D canvas drawn with the same triangles, translucent fills and a shadow reads as drawn through every reader,
  noised or not: its bytes are equal in the two launches.
* ``test_an_offscreen_webgl_canvas_reads_as_its_element_does``: an ``OffscreenCanvas`` on the page's thread and one in a worker, each with a WebGL
  context, read through every reader (``drawImage`` onto a 2D canvas, ``createImageBitmap``, ``convertToBlob``, ``transferToImageBitmap``,
  ``readPixels``) the bytes the element of the same scene reads.
* ``test_a_transferred_placeholder_reads_noised_like_its_worker``: the worker draws WebGL on the offscreen canvas it was given by
  ``transferControlToOffscreen``; the page reads the placeholder element (``drawImage``, ``createImageBitmap``) once the frame reached it, and reads
  what the worker reads from its own canvas.

The scene and the readers are in ``tests/_canvas_readers.py``. The noise seed (12345) is one no runner has, so a passing run is not the host's.
The page ends its readers on events (a promise, a message of the worker, a frame of the page); the one wait that polls (the frame reaching the
placeholder) ends on the placeholder holding the frame, and the deadline is the test's, not a measure of speed.

Kept apart from other canvas modules: each test launches two short-lived browsers one after the other (the sync API does not nest).
Run: pytest tests/test_every_reader_of_a_webgl_canvas_agrees_e2e.py -m e2e -v
"""
from __future__ import annotations

import pytest

import _canvas_readers as cr

pytestmark = pytest.mark.e2e

PAGE_JS = r"""
async function theElement() {
  const g = document.createElement('canvas'); g.width = W; g.height = H;
  const gl = drawGL(g);
  const c = document.createElement('canvas'); c.width = W; c.height = H;
  const x = c.getContext('2d'); draw2D(x);
  return {webgl: await readersOf(g, () => readGL(gl), makeElement, false),
          flat2d: await readersOf(c, () => readCtx(x), makeElement, false)};
}

const WORKER_OFFSCREEN = `
onmessage = async () => {
  try {
    const c = new OffscreenCanvas(W, H); const gl = drawGL(c);
    postMessage({readers: await readersOf(c, () => readGL(gl), makeOffscreen, true)});
  } catch (e) { postMessage({err: String((e && e.stack) || e)}); }
};`;

async function theOffscreens() {
  const g = document.createElement('canvas'); g.width = W; g.height = H;
  const gl = drawGL(g);
  const element = await readersOf(g, () => readGL(gl), makeElement, false);
  const o = new OffscreenCanvas(W, H);
  const ogl = drawGL(o);
  const main = await readersOf(o, () => readGL(ogl), makeOffscreen, true);
  const w = new Worker(URL.createObjectURL(new Blob([WORKER_LIB + WORKER_OFFSCREEN])));
  const worker = await new Promise((ok, bad) => {
    w.onmessage = e => (e.data.err ? bad(new Error(e.data.err)) : ok(e.data.readers));
    w.onerror = e => bad(new Error('worker: ' + e.message));
    w.postMessage(0);
  });
  w.terminate();
  return {element, main, worker};
}

const WORKER_PLACEHOLDER = `
onmessage = async (e) => {
  try {
    const c = e.data.canvas; const gl = drawGL(c);
    postMessage({readers: await readersOf(c, () => readGL(gl), makeOffscreen, false)});
  } catch (err) { postMessage({err: String((err && err.stack) || err)}); }
};`;

async function thePlaceholder() {
  const el = document.createElement('canvas'); el.width = W; el.height = H;
  document.body.appendChild(el);
  const off = el.transferControlToOffscreen();
  const w = new Worker(URL.createObjectURL(new Blob([WORKER_LIB + WORKER_PLACEHOLDER])));
  const worker = await new Promise((ok, bad) => {
    w.onmessage = e => (e.data.err ? bad(new Error(e.data.err)) : ok(e.data.readers));
    w.onerror = e => bad(new Error('worker: ' + e.message));
    w.postMessage({canvas: off}, [off]);
  });
  // the frame the worker drew reaches the placeholder after the worker's task: the page waits on what it can read (the alpha of
  // the middle pixel, 255 in the whole scene, 0 before the frame), one frame of the page at a time
  const seen = () => pixelsOf(makeElement, x => x.drawImage(el, 0, 0));
  const middle = ((H / 2) * W + W / 2) * 4 + 3;
  while (seen()[middle] !== 255) await new Promise(r => requestAnimationFrame(r));
  const placeholder = {drawImage: b64(seen())};
  const bm = await createImageBitmap(el);
  placeholder.createImageBitmap = b64(pixelsOf(makeElement, x => x.drawImage(bm, 0, 0)));
  bm.close();
  w.terminate();
  return {placeholder, worker};
}
"""

HTML = cr.page_html(PAGE_JS)


def _read(firefox_binary, noised, call):
    with cr.launched(firefox_binary, HTML, noised=noised) as page:
        return cr.run(page, call)


def _the_noise_is_a_change_of_low_bits(what, noised, plain):
    """The control: the noise is there (the reads differ), and it is only the low bit of R, G and B."""
    n = cr.differing_pixels(noised, plain)
    assert n > 0, (
        "%s: the canvas read with the noise declared on is byte for byte the canvas read with it declared off: no pixel of the scene was "
        "touched, so this test would pass on an engine that applies no noise at all" % what)
    beyond = cr.only_the_low_bit_of_rgb(noised, plain)
    assert not beyond, (
        "%s: %d pixels of the noised read differ from the plain one by more than the low bit of R, G and B (first: %r); the noise writes "
        "nothing else" % (what, len(beyond), beyond[:5]))
    return n


@pytest.mark.timeout(300, method="thread")
def test_every_reader_of_a_webgl_canvas_element_reads_the_same_bytes(firefox_binary):
    on = _read(firefox_binary, True, "theElement()")
    off = _read(firefox_binary, False, "theElement()")
    webgl_on, webgl_off = cr.decode(on["webgl"]), cr.decode(off["webgl"])
    flat_on, flat_off = cr.decode(on["flat2d"]), cr.decode(off["flat2d"])

    cr.assert_agree("WebGL element, noise on", webgl_on)
    cr.assert_agree("WebGL element, noise declared off", webgl_off)
    _the_noise_is_a_change_of_low_bits("WebGL element", webgl_on["direct"], webgl_off["direct"])

    # the control of the other side: a 2D canvas is not noised by any reader, with the engine on or off
    assert cr.distinct_colours(flat_off["direct"]) > 100, (
        "the 2D scene holds %d colours: too flat to be a canvas the noise would touch" % cr.distinct_colours(flat_off["direct"]))
    cr.assert_agree("2D canvas, noise on", flat_on)
    cr.assert_agree("2D canvas, noise declared off", flat_off)
    assert flat_on["direct"] == flat_off["direct"], (
        "the 2D canvas reads differently with the canvas noise on and off (%d pixels): a 2D canvas reads as drawn, as on retail"
        % cr.differing_pixels(flat_on["direct"], flat_off["direct"]))


@pytest.mark.timeout(300, method="thread")
def test_an_offscreen_webgl_canvas_reads_as_its_element_does(firefox_binary):
    on = _read(firefox_binary, True, "theOffscreens()")
    off = _read(firefox_binary, False, "theOffscreens()")

    def every(group):
        merged = {}
        for place in ("element", "main", "worker"):
            merged.update(cr.decode(group[place], place + "/"))
        return merged

    webgl_on, webgl_off = every(on), every(off)
    cr.assert_agree("an element, an OffscreenCanvas and an OffscreenCanvas in a worker, noise on", webgl_on, "element/direct")
    cr.assert_agree("an element, an OffscreenCanvas and an OffscreenCanvas in a worker, noise declared off", webgl_off, "element/direct")
    _the_noise_is_a_change_of_low_bits("OffscreenCanvas", webgl_on["worker/direct"], webgl_off["worker/direct"])


@pytest.mark.timeout(300, method="thread")
def test_a_transferred_placeholder_reads_noised_like_its_worker(firefox_binary):
    on = _read(firefox_binary, True, "thePlaceholder()")
    off = _read(firefox_binary, False, "thePlaceholder()")

    def every(group):
        merged = cr.decode(group["worker"], "worker/")
        merged.update(cr.decode(group["placeholder"], "placeholder/"))
        return merged

    webgl_on, webgl_off = every(on), every(off)
    cr.assert_agree("the placeholder and its worker, noise on", webgl_on, "worker/direct")
    cr.assert_agree("the placeholder and its worker, noise declared off", webgl_off, "worker/direct")
    _the_noise_is_a_change_of_low_bits("the placeholder", webgl_on["placeholder/drawImage"], webgl_off["placeholder/drawImage"])
