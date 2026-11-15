// HUNT ONLY (branch hunt/cpu-render-path, never merged). Served by scripts/hunt_msaa.py as /probe.js; the page posts ONE JSON result to /result.
// Hypothesis: our forced multisample (webgl.msaa-samples=4, webgl.msaa-force=true; GLContext.cpp:799 turns framebuffer_multisample back on for the Basic Render
// Driver, which GLContext.cpp:783-787 turns off) makes every readPixels of the default framebuffer resolve MSAA in software: ~400 ms for 1024x1024 in ours against ~89 ms in
// stock and retail. Two things are measured IN EACH BROWSER:
//  1. readPixels / toDataURL timing of a 1024x1024 WebGL canvas, once with antialias:true (the default) and once with antialias:false in the same browser: if the
//     multisample resolve is the cost, the antialias:false context is fast in the browser that is slow with antialias:true;
//  2. a 256x256 scene with diagonal edges (a rotated triangle, thin rotated lines, thin rotated slivers) drawn in an antialias:true context and in an antialias:false
//     one, read back with readPixels AND toDataURL. The edge pixels are told from the interior ones (an interior pixel has the same colour as its eight neighbours in
//     the antialias:false image) and hashed separately; the antialiased pixels (colours that are not one of the flat colours) are counted.
// Also: getContextAttributes().antialias, SAMPLES, SAMPLE_BUFFERS, MAX_SAMPLES and the multisample counts of RGBA8 (WebGL 2).
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const now = () => performance.now();
  const q = (a, p) => { if (!a.length) return null; const s = a.slice().sort((x, y) => x - y); return s[Math.min(s.length - 1, Math.floor(p * s.length))]; };
  const r3 = (x) => (x === null || x === undefined ? null : Math.round(x * 1000) / 1000);
  const result = { timing: {}, attributes: {}, scene: {}, errors: [] };
  function hash(u8) { let h1 = 0xdeadbeef ^ u8.length, h2 = 0x41c6ce57 ^ u8.length; for (let i = 0; i < u8.length; i++) { h1 = Math.imul(h1 ^ u8[i], 2654435761); h2 = Math.imul(h2 ^ u8[i], 1597334677); } return (h2 >>> 0).toString(16) + (h1 >>> 0).toString(16); }
  function b64(u8) { let s = ""; for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000)); return btoa(s); }
  async function step(name, fn) { try { await fn(); } catch (e) { result.errors.push(name + ": " + ((e && e.message) || e)); } }

  function context(kind, w, h, antialias) {
    const c = document.createElement("canvas"); c.width = w; c.height = h; c.style.cssText = "position:fixed;left:0;top:0";
    document.body.appendChild(c);
    const gl = c.getContext(kind, { antialias, preserveDrawingBuffer: true, alpha: true });
    if (!gl) { c.remove(); throw new Error("no " + kind + " with antialias:" + antialias); }
    return { c, gl };
  }
  function program(gl, fs) {
    const mk = (t, s) => { const x = gl.createShader(t); gl.shaderSource(x, s); gl.compileShader(x); if (!gl.getShaderParameter(x, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(x)); return x; };
    const p = gl.createProgram();
    gl.attachShader(p, mk(gl.VERTEX_SHADER, "attribute vec2 p; void main(){ gl_Position=vec4(p,0.,1.); }"));
    gl.attachShader(p, mk(gl.FRAGMENT_SHADER, fs));
    gl.bindAttribLocation(p, 0, "p"); gl.linkProgram(p); gl.useProgram(p);
    return p;
  }
  function verts(gl, arr) {
    const b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(arr), gl.STREAM_DRAW);
    gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
  }

  // ------------------------------------------------------------------------------------------------------------- attributes, per context kind
  for (const kind of ["webgl", "webgl2"]) for (const aa of [true, false]) {
    await step("attributes " + kind + " " + aa, async () => {
      const { c, gl } = context(kind, 64, 64, aa);
      const info = gl.getExtension("WEBGL_debug_renderer_info");
      const o = { requested_antialias: aa, attributes_antialias: gl.getContextAttributes().antialias, SAMPLES: gl.getParameter(gl.SAMPLES), SAMPLE_BUFFERS: gl.getParameter(gl.SAMPLE_BUFFERS),
                  RENDERER: gl.getParameter(gl.RENDERER), UNMASKED_RENDERER: info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : null };
      if (kind === "webgl2") {
        o.MAX_SAMPLES = gl.getParameter(gl.MAX_SAMPLES);
        try { o.RGBA8_samples = Array.from(gl.getInternalformatParameter(gl.RENDERBUFFER, gl.RGBA8, gl.SAMPLES)); } catch (e) { o.RGBA8_samples = "ERR " + e.message; }
      }
      result.attributes[kind + ".antialias_" + aa] = o;
      c.remove();
    });
  }

  // ------------------------------------------------------------------------------------------------------------- 1. timing, 1024x1024
  for (const aa of [true, false]) {
    await step("timing " + aa, async () => {
      const { c, gl } = context("webgl", 1024, 1024, aa);
      program(gl, "precision highp float; void main(){ gl_FragColor=vec4(fract(gl_FragCoord.x/97.), fract(gl_FragCoord.y/89.), .5, 1.); }");
      verts(gl, [-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]);
      gl.viewport(0, 0, 1024, 1024);
      const px = new Uint8Array(1024 * 1024 * 4), full = [], one = [], p1 = new Uint8Array(4);
      for (let i = 0; i < 7; i++) { gl.drawArrays(gl.TRIANGLES, 0, 6); const s = now(); gl.readPixels(0, 0, 1024, 1024, gl.RGBA, gl.UNSIGNED_BYTE, px); full.push(now() - s); }
      for (let i = 0; i < 7; i++) { gl.drawArrays(gl.TRIANGLES, 0, 6); const s = now(); gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, p1); one.push(now() - s); }
      gl.drawArrays(gl.TRIANGLES, 0, 6);
      const s2 = now(); c.toDataURL("image/png"); const png = now() - s2;
      result.timing["antialias_" + aa] = { attributes_antialias: gl.getContextAttributes().antialias, first_readPixels_ms: r3(full[0]), repeat_readPixels_ms_p50: r3(q(full.slice(1), 0.5)),
                                           draw_then_1px_readPixels_ms_p50: r3(q(one, 0.5)), toDataURL_png_ms: r3(png), pixels_hash: hash(px) };
      c.remove();
    });
  }

  // ------------------------------------------------------------------------------------------------------------- 2. the scene with diagonal edges
  const W = 256, H = 256;
  function scene(gl) {
    gl.viewport(0, 0, W, H); gl.clearColor(1, 1, 1, 1); gl.clear(gl.COLOR_BUFFER_BIT);
    const col = (r, g, b) => program(gl, "precision mediump float; void main(){ gl_FragColor=vec4(" + r + "," + g + "," + b + ",1.); }");
    const rot = (x, y, a) => [x * Math.cos(a) - y * Math.sin(a), x * Math.sin(a) + y * Math.cos(a)];
    const tri = [[-0.62, -0.5], [0.58, -0.44], [0.02, 0.71]].flatMap(([x, y]) => rot(x, y, 17 * Math.PI / 180));
    col(0.1, 0.3, 0.8); verts(gl, tri); gl.drawArrays(gl.TRIANGLES, 0, 3);
    col(0.8, 0.1, 0.1); gl.lineWidth(1);
    const lines = []; for (let k = 0; k < 12; k++) { const a = (k * 15 + 7) * Math.PI / 180; lines.push(0.05 * Math.cos(a), 0.05 * Math.sin(a), 0.93 * Math.cos(a), 0.93 * Math.sin(a)); }
    verts(gl, lines); gl.drawArrays(gl.LINES, 0, 24);
    col(0.0, 0.55, 0.1);
    const sl = []; for (let k = 0; k < 5; k++) { const a = (k * 31 + 11) * Math.PI / 180; const [x0, y0] = rot(-0.9, -0.85 + k * 0.05, a), [x1, y1] = rot(0.9, -0.85 + k * 0.05, a), [x2, y2] = rot(0.9, -0.845 + k * 0.05, a); sl.push(x0, y0, x1, y1, x2, y2); }
    verts(gl, sl); gl.drawArrays(gl.TRIANGLES, 0, 15);
  }
  function read(gl) { const u = new Uint8Array(W * H * 4); gl.readPixels(0, 0, W, H, gl.RGBA, gl.UNSIGNED_BYTE, u); return u; }
  async function viaDataURL(c) {
    const im = new Image(); im.src = c.toDataURL(); await im.decode();
    const c2 = document.createElement("canvas"); c2.width = W; c2.height = H; const g = c2.getContext("2d"); g.drawImage(im, 0, 0);
    const d = g.getImageData(0, 0, W, H).data;
    // WebGL's readPixels is bottom-up and the PNG top-down: flip the decoded rows to compare like with like
    const out = new Uint8Array(W * H * 4);
    for (let y = 0; y < H; y++) out.set(d.subarray(y * W * 4, (y + 1) * W * 4), (H - 1 - y) * W * 4);
    return out;
  }
  await step("scene", async () => {
    const imgs = {};
    for (const aa of [true, false]) {
      const { c, gl } = context("webgl", W, H, aa);
      scene(gl);
      imgs[aa] = { read: read(gl), url: await viaDataURL(c), attr: gl.getContextAttributes().antialias, samples: gl.getParameter(gl.SAMPLES) };
      c.remove();
    }
    const off = imgs[false].read;
    const same = (i, j) => off[i] === off[j] && off[i + 1] === off[j + 1] && off[i + 2] === off[j + 2] && off[i + 3] === off[j + 3];
    const edge = new Uint8Array(W * H);
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
      const i = (y * W + x) * 4;
      for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
        const xx = Math.min(W - 1, Math.max(0, x + dx)), yy = Math.min(H - 1, Math.max(0, y + dy));
        if (!same(i, (yy * W + xx) * 4)) edge[y * W + x] = 1;
      }
    }
    const flat = new Set();
    for (let i = 0; i < W * H * 4; i += 4) flat.add(off[i] + "," + off[i + 1] + "," + off[i + 2] + "," + off[i + 3]);
    const split = (u8) => {
      const e = [], n = []; let inter = 0;
      for (let p = 0; p < W * H; p++) {
        const i = p * 4, k = u8[i] + "," + u8[i + 1] + "," + u8[i + 2] + "," + u8[i + 3];
        (edge[p] ? e : n).push(u8[i], u8[i + 1], u8[i + 2], u8[i + 3]);
        if (!flat.has(k)) inter++;
      }
      return { edge_hash: hash(Uint8Array.from(e)), interior_hash: hash(Uint8Array.from(n)), antialiased_pixels: inter };
    };
    const diffs = (a, b) => { let n = 0, inEdge = 0, inInterior = 0, mx = 0; for (let p = 0; p < W * H; p++) { const i = p * 4; let d = 0; for (let k = 0; k < 4; k++) d = Math.max(d, Math.abs(a[i + k] - b[i + k])); if (d) { n++; mx = Math.max(mx, d); edge[p] ? inEdge++ : inInterior++; } } return { differing_pixels: n, at_edges: inEdge, in_interior: inInterior, largest_delta: mx }; };
    result.scene = {
      size: [W, H], edge_pixels: edge.reduce((s, x) => s + x, 0), flat_colours: flat.size,
      aa_true: { attributes_antialias: imgs[true].attr, SAMPLES: imgs[true].samples, readPixels: split(imgs[true].read), toDataURL_decoded: split(imgs[true].url),
                 png_equals_readPixels: hash(imgs[true].url) === hash(imgs[true].read) },
      aa_false: { attributes_antialias: imgs[false].attr, SAMPLES: imgs[false].samples, readPixels: split(imgs[false].read), toDataURL_decoded: split(imgs[false].url),
                  png_equals_readPixels: hash(imgs[false].url) === hash(imgs[false].read) },
      aa_true_against_aa_false_readPixels: diffs(imgs[true].read, imgs[false].read),
      aa_true_readPixels_b64: b64(imgs[true].read), aa_false_readPixels_b64: b64(imgs[false].read),
    };
  });
  await fetch("/result", { method: "POST", body: JSON.stringify(result) });
})().catch((e) => fetch("/result", { method: "POST", body: JSON.stringify({ fatal: String((e && e.message) || e) }) }));
