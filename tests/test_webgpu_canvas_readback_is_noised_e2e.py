"""A WebGPU canvas is read noised through ``toDataURL`` and ``toBlob`` (e2e, every OS; written on firefox-68, which the engine of the series must keep).

The engine applies its canvas noise (``gfx::StealthProjectPixels``) when a page reads a WebGL or WebGPU canvas, never a 2D one. The WebGPU canvas has
two roads that are its own, ``CanvasContext::GetImageBuffer`` (``toDataURL``) and ``CanvasContext::GetInputStream`` (``toBlob``), and in each the
snapshot Mozilla takes goes through ``CanvasUtils::StealthProjectWebGPUSnapshot`` (``fingerprint/canvas/noise-projection``: dom/webgpu/CanvasContext.cpp).
A page that reads a WebGPU canvas with either method reads the noised pixels, and the same ones with both.

The page draws the scene of ``tests/_canvas_readers.py`` (ten triangles with random vertex colours over a white clear) with a render pipeline of
WGSL into a canvas configured ``opaque``, and reads it. The control is in the same test: a second launch with the canvas noise declared off
(``zoom.stealth.canvas.substitute_pixels`` false, every other declaration the same). The test asks that

* ``toDataURL`` and ``toBlob`` read the same pixels with the noise on, and the same with it off;
* the two launches differ (the noise is there), and only in the low bit of R, G and B (nothing else is written).

NO SKIP: the core declares a GPU, so ``navigator.gpu.requestAdapter()`` answers an adapter on every runner. A page that gets ``null`` is a result
of this test (the declaration did not reach WebGPU, or the runner has no way to give one), not a reason to leave it unrun.

The frame of a WebGPU canvas reaches the readers after the submit, in the page's own time: the test reads again, with a deadline, until the PNG
holds the opaque scene (the middle pixel has alpha 255; a canvas not yet presented reads transparent). The pixels it keeps are those of the read
that saw the scene.

Run: pytest tests/test_webgpu_canvas_readback_is_noised_e2e.py -m e2e -v
"""
from __future__ import annotations

import pytest

import _canvas_readers as cr
from _waits import wait_until

pytestmark = pytest.mark.e2e

PAGE_JS = r"""
const WGSL = `
struct VOut { @builtin(position) pos : vec4<f32>, @location(0) color : vec3<f32> };
@vertex fn vs(@location(0) p : vec2<f32>, @location(1) c : vec3<f32>) -> VOut {
  var o : VOut; o.pos = vec4<f32>(p, 0.0, 1.0); o.color = c; return o;
}
@fragment fn fs(@location(0) color : vec3<f32>) -> @location(0) vec4<f32> { return vec4<f32>(color, 1.0); }`;

async function gpuStart() {
  if (!navigator.gpu) return {state: 'navigator.gpu is missing'};
  const adapter = await navigator.gpu.requestAdapter();
  if (!adapter) return {state: 'requestAdapter() answered null'};
  const device = await adapter.requestDevice();
  const canvas = document.createElement('canvas'); canvas.width = W; canvas.height = H;
  document.body.appendChild(canvas);
  const context = canvas.getContext('webgpu');
  if (!context) return {state: 'getContext("webgpu") answered null'};
  const format = navigator.gpu.getPreferredCanvasFormat();
  context.configure({device, format, alphaMode: 'opaque'});
  const data = new Float32Array(starVertices());
  const vertices = device.createBuffer({size: data.byteLength, usage: GPUBufferUsage.VERTEX, mappedAtCreation: true});
  new Float32Array(vertices.getMappedRange()).set(data);
  vertices.unmap();
  const module = device.createShaderModule({code: WGSL});
  const pipeline = device.createRenderPipeline({
    layout: 'auto',
    vertex: {module, entryPoint: 'vs', buffers: [{arrayStride: 20, attributes: [
      {shaderLocation: 0, offset: 0, format: 'float32x2'}, {shaderLocation: 1, offset: 8, format: 'float32x3'}]}]},
    fragment: {module, entryPoint: 'fs', targets: [{format}]},
    primitive: {topology: 'triangle-list'},
  });
  const encoder = device.createCommandEncoder();
  const pass = encoder.beginRenderPass({colorAttachments: [{
    view: context.getCurrentTexture().createView(), clearValue: {r: 1, g: 1, b: 1, a: 1}, loadOp: 'clear', storeOp: 'store'}]});
  pass.setPipeline(pipeline); pass.setVertexBuffer(0, vertices); pass.draw(30); pass.end();
  device.queue.submit([encoder.finish()]);
  await device.queue.onSubmittedWorkDone();
  window.GPU_CANVAS = canvas;
  return {state: 'drawn', format};
}

async function gpuRead() {
  const canvas = window.GPU_CANVAS;
  return {'toDataURL.png': canvas.toDataURL('image/png').split(',')[1],
          'toBlob.png': await blobB64(await new Promise(r => canvas.toBlob(r, 'image/png')))};
}
"""

HTML = cr.page_html(PAGE_JS)
MIDDLE_ALPHA = ((cr.SIZE // 2) * cr.SIZE + cr.SIZE // 2) * 4 + 3


def _draw_and_read(firefox_binary, noised):
    """The two PNGs of the WebGPU canvas, decoded, read again until they hold the scene."""
    with cr.launched(firefox_binary, HTML, noised=noised) as page:
        started = cr.run(page, "gpuStart()")
        assert started["state"] == "drawn", (
            "the page could not draw with WebGPU: %s. The core declares a GPU, so requestAdapter() must answer one on every runner" % started["state"])
        seen = {}

        def holds_the_scene():
            seen.clear()
            seen.update(cr.decode(page.evaluate("() => gpuRead()")))
            return all(pixels[MIDDLE_ALPHA] == 255 for pixels in seen.values())

        assert wait_until(holds_the_scene, 120, lambda: page.wait_for_timeout(200)), (
            "the WebGPU canvas never read as the opaque scene it was drawn with: the middle pixel's alpha is %r through toDataURL and toBlob"
            % {name: pixels[MIDDLE_ALPHA] for name, pixels in seen.items()})
        return dict(seen), started["format"]


@pytest.mark.timeout(300, method="thread")
def test_a_webgpu_canvas_reads_noised_through_todataurl_and_toblob(firefox_binary):
    on, format_on = _draw_and_read(firefox_binary, True)
    off, format_off = _draw_and_read(firefox_binary, False)
    assert format_on == format_off, "the two launches draw into canvases of different formats (%s, %s)" % (format_on, format_off)

    cr.assert_agree("WebGPU canvas, noise on", on, "toDataURL.png")
    cr.assert_agree("WebGPU canvas, noise declared off", off, "toDataURL.png")
    n = cr.differing_pixels(on["toDataURL.png"], off["toDataURL.png"])
    assert n > 0, (
        "the WebGPU canvas reads byte for byte the same with the canvas noise declared on and off: toDataURL and toBlob read the pixels "
        "as drawn, so the page's readback of a WebGPU canvas is not noised")
    beyond = cr.only_the_low_bit_of_rgb(on["toDataURL.png"], off["toDataURL.png"])
    assert not beyond, (
        "%d pixels of the noised WebGPU read differ from the plain one by more than the low bit of R, G and B (first: %r); the noise writes "
        "nothing else" % (len(beyond), beyond[:5]))
