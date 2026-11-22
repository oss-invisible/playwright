// The surface subset one realm reads: the same text is loaded by the page, a cross-site iframe, a srcdoc iframe, a worker, a shared worker, a
// service worker and an audio worklet, so that a value the engine declares for one realm and forgets for another shows as a difference.
// Everything here is a plain read of what the realm exposes; a realm that lacks an API says 'absent'.
(function (root) {
  'use strict';
  const errs = e => 'ERR ' + ((e && e.name) || '') + ': ' + ((e && e.message) || String(e));
  const bytesHash = u8 => {
    let a = 0x811c9dc5, b = 0xdeadbeef;
    for (let i = 0; i < u8.length; i++) { a = Math.imul(a ^ u8[i], 0x01000193) >>> 0; b = Math.imul(b ^ u8[i], 0x5bd1e995) >>> 0; b ^= b >>> 13; }
    return u8.length + ':' + a.toString(16) + b.toString(16);
  };
  const textHash = s => { const u = new Uint8Array(s.length); for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i) & 255; return bytesHash(u); };
  const TEXT = 'Hamburgefonstiv 0123 ZZ';
  const draw = g => {
    g.fillStyle = '#fff'; g.fillRect(0, 0, 160, 40); g.fillStyle = '#036'; g.font = '16px sans-serif'; g.fillText(TEXT, 4, 16);
    g.font = '14px serif'; g.fillText('\u65e5\u672c\u8a9e Stra\u00dfe', 4, 34);
  };
  const measure = g => ['sans-serif', 'serif', 'monospace', 'ZZHostCanary, sans-serif'].map(f => { g.font = '16px ' + f; return g.measureText(TEXT).width; });
  const scene = c => {
    const gl = c.getContext('webgl');
    if (!gl) return 'no webgl';
    const sh = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; };
    const p = gl.createProgram();
    gl.attachShader(p, sh(gl.VERTEX_SHADER, 'attribute vec2 a;void main(){gl_Position=vec4(a,0.,1.);}'));
    gl.attachShader(p, sh(gl.FRAGMENT_SHADER, 'precision mediump float;void main(){gl_FragColor=vec4(.9,.3,.1,1.);}'));
    gl.linkProgram(p); gl.useProgram(p);
    const buf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-0.8, -0.8, 0.8, -0.8, 0, 0.8]), gl.STATIC_DRAW);
    const a = gl.getAttribLocation(p, 'a'); gl.enableVertexAttribArray(a); gl.vertexAttribPointer(a, 2, gl.FLOAT, false, 0, 0);
    gl.viewport(0, 0, 32, 32); gl.clearColor(0.2, 0.4, 0.6, 1); gl.clear(gl.COLOR_BUFFER_BIT); gl.drawArrays(gl.TRIANGLES, 0, 3);
    const px = new Uint8Array(32 * 32 * 4); gl.readPixels(0, 0, 32, 32, gl.RGBA, gl.UNSIGNED_BYTE, px);
    const dbg = gl.getExtension('WEBGL_debug_renderer_info');
    return {pixels: bytesHash(px), vendor: gl.getParameter(gl.VENDOR), renderer: gl.getParameter(gl.RENDERER),
            unmasked: dbg ? [gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL), gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL)] : 'absent'};
  };

  // What needs no await: the only part an audio worklet can read.
  const readSync = () => {
    const o = {};
    const t = (k, f) => { try { o[k] = f(); } catch (e) { o[k] = errs(e); } };
    const nav = root.navigator;
    if (nav) {
      t('language', () => nav.language); t('languages', () => Array.from(nav.languages)); t('platform', () => nav.platform);
      t('hardwareConcurrency', () => nav.hardwareConcurrency); t('userAgent', () => nav.userAgent);
    } else { o.navigator = 'absent'; }
    t('intl', () => { const r = new Intl.DateTimeFormat().resolvedOptions(); return [r.locale, r.timeZone, r.calendar, r.numberingSystem]; });
    const d = new Date(Date.UTC(2026, 0, 15, 12)), s = new Date(Date.UTC(2026, 6, 15, 12));
    t('dates', () => [d.toString(), s.toString(), d.toLocaleString(), d.toLocaleDateString(undefined, {dateStyle: 'full'}), (1234567.891).toLocaleString(),
                      d.getTimezoneOffset(), s.getTimezoneOffset()]);
    t('sampleRate', () => typeof root.sampleRate === 'number' ? root.sampleRate : 'absent');
    return o;
  };

  const read = async () => {
    const o = readSync();
    const ta = async (k, f) => { try { o[k] = await f(); } catch (e) { o[k] = errs(e); } };
    if (root.document && root.document.createElement) {
      await ta('canvas', async () => {
        const c = root.document.createElement('canvas'); c.width = 160; c.height = 40; const g = c.getContext('2d'); draw(g);
        return {toDataURL: textHash(c.toDataURL()), measure: measure(g)};
      });
      await ta('webgl', async () => { const c = root.document.createElement('canvas'); c.width = 32; c.height = 32; return scene(c); });
    } else { o.canvas = 'absent'; }
    if (typeof root.OffscreenCanvas === 'function') {
      await ta('offscreen', async () => {
        const c = new root.OffscreenCanvas(160, 40), g = c.getContext('2d'); draw(g);
        const m = measure(g), blob = await c.convertToBlob();
        return {measure: m, png: bytesHash(new Uint8Array(await blob.arrayBuffer()))};
      });
      await ta('offscreen_webgl', async () => scene(new root.OffscreenCanvas(32, 32)));
    } else { o.offscreen = 'absent'; }
    return o;
  };

  root.abRealm = read;
  root.abRealmSync = readSync;
  // An audio worklet runs this file as a module: its processor reports what the worklet's own realm reads, when it is created.
  if (typeof root.registerProcessor === 'function' && typeof root.AudioWorkletProcessor === 'function') {
    root.registerProcessor('ab-probe', class extends root.AudioWorkletProcessor {
      constructor() { super(); this.port.postMessage(readSync()); }
      process() { return false; }
    });
  }
})(typeof globalThis !== 'undefined' ? globalThis : self);
