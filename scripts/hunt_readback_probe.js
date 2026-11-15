// HUNT ONLY (branch hunt/cpu-render-path, never merged). Served by scripts/hunt_readback.py as /probe.js; the page posts ONE JSON result to /result.
// Why does a 1024x1024 WebGL readPixels take ~250-290 ms in ours against 5-9 ms in stock and retail, with or without MSAA?
// In the engine (firefox-64 series, the engine source tree) the WebGL read roads go through the canvas noise, gfx::StealthProjectPixels (gfx/2d/StealthCanvasProjection.cpp):
// a pixel is looked at only if it is an EDGE (a 4-neighbour differs once the low bit of R, G and B is cleared); on such a pixel the function builds the 3x3 neighbourhood and
// runs OnOneSegment (36 pair distances and up to 9 segment tests, integer, per pixel). A flat canvas costs a memcpy; a canvas in which EVERY pixel is an edge costs ~1000 operations
// per pixel. The earlier probes drew exactly that: a per-pixel ramp (fract(gl_FragCoord.x/97.)), whose neighbours differ by ~2.6 levels everywhere. So the content is the variable:
//   mode=timing   per content (flat, halves, triangle, gradient, noise): WebGL readPixels (p50 of 5), WebGL toDataURL (p50 of 3), canvas 2D getImageData (p50 of 5), canvas 2D toDataURL
//                 (p50 of 3), all 1024x1024, antialias:false (MSAA out of the picture); a hash of the bytes read; and the 256x256 diagonal-edge scene (antialias:false) read back, raw
//                 bytes returned, to compare pixel by pixel with retail.
//   mode=profile  20 readPixels and 20 toDataURL of the gradient 1024x1024 WebGL canvas (the worst case), nothing else, for the Gecko profile.
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const now = () => performance.now();
  const mode = new URLSearchParams(location.search).get("mode") || "timing";
  const q = (a, p) => { if (!a.length) return null; const s = a.slice().sort((x, y) => x - y); return s[Math.min(s.length - 1, Math.floor(p * s.length))]; };
  const r3 = (x) => (x === null || x === undefined ? null : Math.round(x * 1000) / 1000);
  const result = { mode, contents: {}, scene: {}, errors: [] };
  function hash(u8) { let h1 = 0xdeadbeef ^ u8.length, h2 = 0x41c6ce57 ^ u8.length; for (let i = 0; i < u8.length; i++) { h1 = Math.imul(h1 ^ u8[i], 2654435761); h2 = Math.imul(h2 ^ u8[i], 1597334677); } return (h2 >>> 0).toString(16) + (h1 >>> 0).toString(16); }
  function b64(u8) { let s = ""; for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000)); return btoa(s); }
  async function step(name, fn) { try { await fn(); } catch (e) { result.errors.push(name + ": " + ((e && e.message) || e)); } }
  const N = 1024;

  function glContext(w, h) {
    const c = document.createElement("canvas"); c.width = w; c.height = h; c.style.cssText = "position:fixed;left:0;top:0";
    document.body.appendChild(c);
    const gl = c.getContext("webgl", { antialias: false, preserveDrawingBuffer: true, alpha: true });
    if (!gl) { c.remove(); throw new Error("no webgl"); }
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
  const QUAD = [-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1];
  // the same per-pixel formulas in JS (for the 2D canvas) and in GLSL (for WebGL)
  const rampPixel = (x, y) => [Math.floor(((x / 97) % 1) * 255), Math.floor(((y / 89) % 1) * 255), 128];
  function noisePixel(x, y) { let h = (x * 374761393 + y * 668265263) | 0; h = Math.imul(h ^ (h >>> 13), 1274126177); h ^= h >>> 16; return [h & 255, (h >>> 8) & 255, (h >>> 16) & 255]; }
  function drawGL(gl, kind) {
    gl.viewport(0, 0, N, N); gl.disable(gl.SCISSOR_TEST);
    if (kind === "flat") { gl.clearColor(0.2, 0.4, 0.6, 1); gl.clear(gl.COLOR_BUFFER_BIT); return; }
    if (kind === "halves") { gl.clearColor(1, 1, 1, 1); gl.clear(gl.COLOR_BUFFER_BIT); gl.enable(gl.SCISSOR_TEST); gl.scissor(0, 0, N / 2, N); gl.clearColor(0.1, 0.5, 0.2, 1); gl.clear(gl.COLOR_BUFFER_BIT); return; }
    if (kind === "triangle") {
      gl.clearColor(1, 1, 1, 1); gl.clear(gl.COLOR_BUFFER_BIT);
      program(gl, "precision mediump float; void main(){ gl_FragColor=vec4(.1,.3,.8,1.); }");
      verts(gl, [-0.9, -0.8, 0.85, -0.6, -0.1, 0.9]); gl.drawArrays(gl.TRIANGLES, 0, 3); return;
    }
    if (kind === "gradient") {
      program(gl, "precision highp float; void main(){ gl_FragColor=vec4(fract(gl_FragCoord.x/97.), fract(gl_FragCoord.y/89.), .5, 1.); }");
      verts(gl, QUAD); gl.drawArrays(gl.TRIANGLES, 0, 6); return;
    }
    if (kind === "noise") {
      program(gl, "precision highp float; float h(vec2 p){ return fract(sin(dot(p, vec2(12.9898,78.233)))*43758.5453); } void main(){ vec2 p=gl_FragCoord.xy; gl_FragColor=vec4(h(p), h(p+17.), h(p+31.), 1.); }");
      verts(gl, QUAD); gl.drawArrays(gl.TRIANGLES, 0, 6); return;
    }
  }
  function draw2D(g, kind) {
    if (kind === "flat") { g.fillStyle = "rgb(51,102,153)"; g.fillRect(0, 0, N, N); return; }
    if (kind === "halves") { g.fillStyle = "#fff"; g.fillRect(0, 0, N, N); g.fillStyle = "rgb(26,128,51)"; g.fillRect(0, 0, N / 2, N); return; }
    if (kind === "triangle") { g.fillStyle = "#fff"; g.fillRect(0, 0, N, N); g.fillStyle = "rgb(26,77,204)"; g.beginPath(); g.moveTo(51, 92); g.lineTo(947, 205); g.lineTo(461, 972); g.closePath(); g.fill(); return; }
    const id = g.createImageData(N, N), d = id.data, f = kind === "gradient" ? rampPixel : noisePixel;
    for (let y = 0; y < N; y++) for (let x = 0; x < N; x++) { const p = f(x, y), i = (y * N + x) * 4; d[i] = p[0]; d[i + 1] = p[1]; d[i + 2] = p[2]; d[i + 3] = 255; }
    g.putImageData(id, 0, 0);
  }
  const reps = async (n, fn) => { const t = []; for (let i = 0; i < n; i++) { const s = now(); const v = fn(); t.push(now() - s); if (i === n - 1) t.last = v; } return t; };

  if (mode === "profile") {
    await step("profile", async () => {
      const { c, gl } = glContext(N, N);
      drawGL(gl, "gradient");
      const px = new Uint8Array(N * N * 4), rp = [], td = [];
      await sleep(300);
      for (let i = 0; i < 20; i++) { const s = now(); gl.readPixels(0, 0, N, N, gl.RGBA, gl.UNSIGNED_BYTE, px); rp.push(now() - s); }
      await sleep(300);
      for (let i = 0; i < 20; i++) { const s = now(); c.toDataURL("image/png"); td.push(now() - s); }
      result.profile = { readPixels_ms_p50: r3(q(rp, 0.5)), toDataURL_ms_p50: r3(q(td, 0.5)), readPixels_ms: rp.map(r3), toDataURL_ms: td.map(r3) };
      c.remove();
    });
    await fetch("/result", { method: "POST", body: JSON.stringify(result) });
    return;
  }

  // ----------------------------------------------------------------------------------------------------------------------- timing, per content
  for (const kind of ["flat", "halves", "triangle", "gradient", "noise"]) {
    await step("webgl " + kind, async () => {
      const { c, gl } = glContext(N, N);
      drawGL(gl, kind);
      const px = new Uint8Array(N * N * 4);
      const rp = await reps(5, () => gl.readPixels(0, 0, N, N, gl.RGBA, gl.UNSIGNED_BYTE, px));
      const td = await reps(3, () => c.toDataURL("image/png"));
      const one = await reps(5, () => gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array(4)));
      (result.contents[kind] = result.contents[kind] || {}).webgl = { readPixels_first_ms: r3(rp[0]), readPixels_ms_p50: r3(q(rp.slice(1), 0.5)), toDataURL_ms_p50: r3(q(td, 0.5)),
        readPixels_1px_ms_p50: r3(q(one, 0.5)), pixels_hash: hash(px) };
      c.remove();
    });
    await step("2d " + kind, async () => {
      const c = document.createElement("canvas"); c.width = c.height = N;
      const g = c.getContext("2d");
      draw2D(g, kind);
      let d = null;
      const gi = await reps(5, () => { d = g.getImageData(0, 0, N, N); });
      const td = await reps(3, () => c.toDataURL("image/png"));
      (result.contents[kind] = result.contents[kind] || {}).canvas2d = { getImageData_first_ms: r3(gi[0]), getImageData_ms_p50: r3(q(gi.slice(1), 0.5)), toDataURL_ms_p50: r3(q(td, 0.5)), pixels_hash: hash(new Uint8Array(d.data.buffer)) };
    });
  }

  // ----------------------------------------------------------------------------------------------------------------------- the diagonal-edge scene, antialias:false, 256x256
  await step("scene", async () => {
    const W = 256, H = 256;
    const { c, gl } = (() => { const c = document.createElement("canvas"); c.width = W; c.height = H; document.body.appendChild(c);
      return { c, gl: c.getContext("webgl", { antialias: false, preserveDrawingBuffer: true }) }; })();
    gl.viewport(0, 0, W, H); gl.clearColor(1, 1, 1, 1); gl.clear(gl.COLOR_BUFFER_BIT);
    const col = (r, g, b) => program(gl, "precision mediump float; void main(){ gl_FragColor=vec4(" + r + "," + g + "," + b + ",1.); }");
    const rot = (x, y, a) => [x * Math.cos(a) - y * Math.sin(a), x * Math.sin(a) + y * Math.cos(a)];
    col(0.1, 0.3, 0.8); verts(gl, [[-0.62, -0.5], [0.58, -0.44], [0.02, 0.71]].flatMap(([x, y]) => rot(x, y, 17 * Math.PI / 180))); gl.drawArrays(gl.TRIANGLES, 0, 3);
    col(0.8, 0.1, 0.1); gl.lineWidth(1);
    const lines = []; for (let k = 0; k < 12; k++) { const a = (k * 15 + 7) * Math.PI / 180; lines.push(0.05 * Math.cos(a), 0.05 * Math.sin(a), 0.93 * Math.cos(a), 0.93 * Math.sin(a)); }
    verts(gl, lines); gl.drawArrays(gl.LINES, 0, 24);
    col(0.0, 0.55, 0.1);
    const sl = []; for (let k = 0; k < 5; k++) { const a = (k * 31 + 11) * Math.PI / 180; const [x0, y0] = rot(-0.9, -0.85 + k * 0.05, a), [x1, y1] = rot(0.9, -0.85 + k * 0.05, a), [x2, y2] = rot(0.9, -0.845 + k * 0.05, a); sl.push(x0, y0, x1, y1, x2, y2); }
    verts(gl, sl); gl.drawArrays(gl.TRIANGLES, 0, 15);
    const u = new Uint8Array(W * H * 4); gl.readPixels(0, 0, W, H, gl.RGBA, gl.UNSIGNED_BYTE, u);
    result.scene = { size: [W, H], attributes_antialias: gl.getContextAttributes().antialias, pixels_hash: hash(u), readPixels_b64: b64(u) };
    c.remove();
  });
  await fetch("/result", { method: "POST", body: JSON.stringify(result) });
})().catch((e) => fetch("/result", { method: "POST", body: JSON.stringify({ fatal: String((e && e.message) || e) }) }));
