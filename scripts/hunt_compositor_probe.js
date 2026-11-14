// HUNT ONLY (branch hunt/cpu-render-path, never merged). Served by scripts/hunt_compositor_visible.py as /probe.js and run by every arm in a page of its own for
// about 20 s: what can a PAGE read of the compositor behind it? Each number is measured in the page, with the page's own clocks, and the whole result is POSTed to
// /result once, as one JSON object: {phases: {name: {...}}, values: {...}}. `kind` says what a difference between two browsers would be: "timing" (a measurement
// that varies from run to run and from machine to machine) or "value" (a declared or fixed property: it should be equal in every Firefox on the same machine).
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const now = () => performance.now();
  const q = (a, p) => { if (!a.length) return null; const s = a.slice().sort((x, y) => x - y); return s[Math.min(s.length - 1, Math.floor(p * s.length))]; };
  const r3 = (x) => (x === null || x === undefined ? null : Math.round(x * 1000) / 1000);
  const result = { phases: {}, values: {}, errors: [], started_at: Date.now() };

  // Frames: one rAF loop for `ms`, with the interval between callbacks and what the callback's timestamp says.
  function frames(ms, each) {
    return new Promise((resolve) => {
      const t = [], skew = [], start = now();
      let stop = false;
      const guard = setTimeout(() => { stop = true; resolve({ t, skew, guard: true }); }, ms + 3000);
      function tick(ts) {
        if (stop) return;
        const n = now();
        t.push(n); skew.push(n - ts);
        if (each) each(n);
        if (n - start < ms) requestAnimationFrame(tick);
        else { clearTimeout(guard); stop = true; resolve({ t, skew, guard: false }); }
      }
      requestAnimationFrame(tick);
    });
  }
  function describe(f, ms) {
    const iv = [];
    for (let i = 1; i < f.t.length; i++) iv.push(f.t[i] - f.t[i - 1]);
    const med = q(iv, 0.5);
    const wall = f.t.length > 1 ? f.t[f.t.length - 1] - f.t[0] : ms;
    let dropped = 0;
    if (med) for (const x of iv) if (x > med * 1.5) dropped += Math.max(1, Math.round(x / med) - 1);
    const buckets = { "<14": 0, "14-15.5": 0, "15.5-18": 0, "18-20": 0, "20-25": 0, ">25": 0 };
    for (const x of iv) buckets[x < 14 ? "<14" : x < 15.5 ? "14-15.5" : x < 18 ? "15.5-18" : x < 20 ? "18-20" : x < 25 ? "20-25" : ">25"]++;
    return { kind: "timing", frames: f.t.length, seconds: r3(wall / 1000), fps: r3(iv.length / (wall / 1000)), interval_ms_p5: r3(q(iv, 0.05)), interval_ms_p50: r3(med),
             interval_ms_p95: r3(q(iv, 0.95)), interval_ms_max: r3(Math.max(0, ...iv)), dropped_frames: dropped, buckets, now_minus_timestamp_ms_p50: r3(q(f.skew, 0.5)),
             watchdog: f.guard };
  }
  async function phase(name, fn) {
    try { result.phases[name] = await fn(); } catch (e) { result.errors.push(name + ": " + ((e && e.message) || e)); }
  }

  // ------------------------------------------------------------------------------------------------------------------ values
  result.values = {
    kind: "value",
    update_fast: matchMedia("(update: fast)").matches, update_slow: matchMedia("(update: slow)").matches, update_none: matchMedia("(update: none)").matches,
    prefers_reduced_motion: matchMedia("(prefers-reduced-motion: reduce)").matches, hover: matchMedia("(hover: hover)").matches,
    any_pointer_fine: matchMedia("(any-pointer: fine)").matches, dynamic_range_high: matchMedia("(dynamic-range: high)").matches,
    color_gamut_p3: matchMedia("(color-gamut: p3)").matches, display_mode_browser: matchMedia("(display-mode: browser)").matches,
    screen: [screen.width, screen.height, screen.availWidth, screen.availHeight, screen.colorDepth, screen.pixelDepth], dpr: window.devicePixelRatio,
    visibility: document.visibilityState, hasFocus: document.hasFocus(), hidden: document.hidden,
    scroll_behavior_supported: CSS.supports("scroll-behavior", "smooth"), scrollend_supported: "onscrollend" in window,
    screen_refresh_property: ("refreshRate" in screen) ? screen.refreshRate : "absent", screen_keys_with_rate: Object.keys(Object.getPrototypeOf(screen)).filter((k) => /refresh|rate|hz/i.test(k)),
    hardwareConcurrency: navigator.hardwareConcurrency,
  };
  // performance.now() resolution: the smallest positive step in 300000 calls, and the number of decimals seen
  {
    let min = Infinity, last = now(), decimals = 0;
    for (let i = 0; i < 300000; i++) { const n = now(); const d = n - last; if (d > 0 && d < min) min = d; last = n; if (i < 2000) { const s = String(n); const dot = s.indexOf("."); if (dot >= 0) decimals = Math.max(decimals, s.length - dot - 1); } }
    result.values.performance_now_min_step_ms = r3(min);
    result.values.performance_now_decimals_seen = decimals;
    let dmin = Infinity, dl = Date.now();
    const s0 = now();
    while (now() - s0 < 60 && dmin > 0.5) { const d = Date.now(); if (d > dl) { dmin = Math.min(dmin, d - dl); dl = d; } }
    result.values.date_now_min_step_ms = dmin === Infinity ? null : dmin;
  }

  // ------------------------------------------------------------------------------------------------------------------ 1. rAF alone
  await phase("raf_idle_5s", async () => describe(await frames(5000), 5000));

  // ------------------------------------------------------------------------------------------------------------------ 2. CSS animation
  await phase("raf_with_css_animation_4s", async () => {
    const st = document.createElement("style");
    st.textContent = "@keyframes hspin{from{transform:rotate(0)}to{transform:rotate(360deg)}}@keyframes hcol{from{box-shadow:0 0 0 0 #08f,inset 0 0 20px #f80}to{box-shadow:0 0 60px 20px #0a8,inset 0 0 90px #f08}}" +
      ".hb{position:fixed;left:20px;top:20px;width:300px;height:300px;background:linear-gradient(45deg,#f00,#00f);animation:hspin 2s linear infinite}" +
      ".hc{position:fixed;left:360px;top:20px;width:300px;height:300px;background:#444;animation:hcol 1s ease-in-out infinite alternate}";
    document.head.appendChild(st);
    const a = document.createElement("div"); a.className = "hb"; const b = document.createElement("div"); b.className = "hc";
    document.body.append(a, b);
    const running = document.getAnimations().length;
    const f = await frames(4000);
    a.remove(); b.remove(); st.remove();
    return Object.assign(describe(f, 4000), { running_animations: running });
  });

  // ------------------------------------------------------------------------------------------------------------------ 3. WebGL animation (2 s free, 2 s with a sync readback every frame)
  async function glCanvas(w, h) {
    const c = document.createElement("canvas"); c.width = w; c.height = h; c.style.cssText = "position:fixed;left:0;top:0";
    document.body.appendChild(c);
    const gl = c.getContext("webgl", { preserveDrawingBuffer: false, antialias: true });
    if (!gl) { c.remove(); throw new Error("no webgl"); }
    const mk = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; };
    const p = gl.createProgram();
    gl.attachShader(p, mk(gl.VERTEX_SHADER, "attribute vec2 p; varying vec2 uv; void main(){ uv=p*0.5+0.5; gl_Position=vec4(p,0.,1.); }"));
    gl.attachShader(p, mk(gl.FRAGMENT_SHADER, "precision highp float; varying vec2 uv; uniform float t; void main(){ float a=0.; for(int i=0;i<48;i++){ a+=sin(uv.x*float(i)+t)*cos(uv.y*float(i)-t); } gl_FragColor=vec4(fract(a),uv.x,uv.y,1.); }"));
    gl.linkProgram(p); gl.useProgram(p);
    const b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    return { c, gl, t: gl.getUniformLocation(p, "t") };
  }
  await phase("raf_with_webgl_animation_4s", async () => {
    const { c, gl, t } = await glCanvas(512, 512);
    const px = new Uint8Array(4), syncs = [];
    const f1 = await frames(2000, (n) => { gl.uniform1f(t, n / 500); gl.drawArrays(gl.TRIANGLES, 0, 6); });
    const f2 = await frames(2000, (n) => { gl.uniform1f(t, n / 500); gl.drawArrays(gl.TRIANGLES, 0, 6); const s = now(); gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px); syncs.push(now() - s); });
    c.remove();
    return { free: describe(f1, 2000), with_sync_readback_every_frame: describe(f2, 2000), sync_readback_ms_p50: r3(q(syncs, 0.5)), sync_readback_ms_p95: r3(q(syncs, 0.95)), kind: "timing" };
  });

  // ------------------------------------------------------------------------------------------------------------------ 4. smooth scrolling of a long page
  await phase("smooth_scroll", async () => {
    const box = document.createElement("div");
    box.style.cssText = "position:absolute;left:0;top:0;width:100%;height:30000px;background:repeating-linear-gradient(#fff,#fff 40px,#cde 40px,#cde 80px)";
    for (let i = 0; i < 400; i++) { const d = document.createElement("div"); d.style.cssText = "position:absolute;left:" + ((i * 37) % 600) + "px;top:" + (i * 70) + "px;width:200px;height:50px;background:hsl(" + ((i * 11) % 360) + ",60%,60%);box-shadow:0 2px 8px #0008"; d.textContent = "row " + i; box.appendChild(d); }
    document.body.appendChild(box);
    window.scrollTo(0, 0);
    await sleep(200);
    const ev = [], target = 12000;
    let ended = false, endAt = 0;
    const onScroll = () => ev.push(now());
    window.addEventListener("scroll", onScroll);
    window.addEventListener("scrollend", () => { ended = true; endAt = now(); }, { once: true });
    const t0 = now();
    const frameLoop = frames(4500);
    window.scrollTo({ top: target, behavior: "smooth" });
    let doneAt = null;
    while (now() - t0 < 4500) {
      await sleep(25);
      if (Math.abs(window.scrollY - target) < 1 && doneAt === null) doneAt = now();
      if (ended || (doneAt !== null && now() - doneAt > 400)) break;
    }
    const f = await frameLoop;
    window.removeEventListener("scroll", onScroll);
    const y = window.scrollY;
    window.scrollTo(0, 0); box.remove();
    const lastEv = ev.length ? ev[ev.length - 1] : t0;
    const dur = (ended ? endAt : (doneAt !== null ? doneAt : lastEv)) - t0;
    return { kind: "timing", reached_target: Math.abs(y - target) < 1, final_scrollY: y, scroll_events: ev.length, time_to_finish_ms: r3(dur), scrollend_fired: ended,
             scroll_events_per_second: r3(ev.length / Math.max(0.001, (lastEv - t0) / 1000)), first_event_after_ms: ev.length ? r3(ev[0] - t0) : null, frames_during: describe(f, 4500) };
  });

  // ------------------------------------------------------------------------------------------------------------------ 5. readback timings, 1024x1024
  function hash(u8) { let h1 = 0xdeadbeef, h2 = 0x41c6ce57; for (let i = 0; i < u8.length; i += 7) { h1 = Math.imul(h1 ^ u8[i], 2654435761); h2 = Math.imul(h2 ^ u8[i], 1597334677); } return ((h2 >>> 0).toString(16) + (h1 >>> 0).toString(16)); }
  await phase("canvas2d_getImageData_1024", async () => {
    const times = [], first = {};
    const c = document.createElement("canvas"); c.width = c.height = 1024;
    const g = c.getContext("2d");
    const draw = () => {
      const lg = g.createLinearGradient(0, 0, 1024, 1024); lg.addColorStop(0, "#f40"); lg.addColorStop(1, "#04f");
      g.fillStyle = lg; g.fillRect(0, 0, 1024, 1024);
      for (let i = 0; i < 300; i++) { g.fillStyle = "hsla(" + (i * 13 % 360) + ",70%,50%,.5)"; g.beginPath(); g.arc((i * 97) % 1024, (i * 61) % 1024, 20 + (i % 40), 0, 6.283); g.fill(); }
      g.font = "30px sans-serif"; g.fillStyle = "#000"; g.fillText("compositor-visible probe", 40, 500);
    };
    draw();
    let d = null;
    for (let i = 0; i < 6; i++) { const s = now(); d = g.getImageData(0, 0, 1024, 1024); times.push(now() - s); if (i === 0) first.ms = now() - s; }
    const one = []; for (let i = 0; i < 6; i++) { draw(); const s = now(); g.getImageData(0, 0, 1, 1); one.push(now() - s); }
    const s2 = now(); c.toDataURL("image/png"); const png = now() - s2;
    return { kind: "timing", first_getImageData_ms: r3(first.ms), repeat_getImageData_ms_p50: r3(q(times.slice(1), 0.5)), draw_then_1x1_getImageData_ms_p50: r3(q(one, 0.5)),
             toDataURL_png_ms: r3(png), bytes_hash_value: hash(d.data), willReadFrequently: g.getContextAttributes ? g.getContextAttributes().willReadFrequently : null };
  });
  await phase("webgl_readPixels_1024", async () => {
    const { c, gl, t } = await glCanvas(1024, 1024);
    const px = new Uint8Array(1024 * 1024 * 4), times = [], one = [];
    gl.viewport(0, 0, 1024, 1024);
    for (let i = 0; i < 6; i++) { gl.uniform1f(t, 1.5); gl.drawArrays(gl.TRIANGLES, 0, 6); const s = now(); gl.readPixels(0, 0, 1024, 1024, gl.RGBA, gl.UNSIGNED_BYTE, px); times.push(now() - s); }
    const p1 = new Uint8Array(4);
    for (let i = 0; i < 6; i++) { gl.drawArrays(gl.TRIANGLES, 0, 6); const s = now(); gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, p1); one.push(now() - s); }
    const info = gl.getExtension("WEBGL_debug_renderer_info");
    const out = { kind: "timing", first_readPixels_ms: r3(times[0]), repeat_readPixels_ms_p50: r3(q(times.slice(1), 0.5)), draw_then_1px_readPixels_ms_p50: r3(q(one, 0.5)), bytes_hash_value: hash(px),
                  renderer_value: info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER) };
    c.remove();
    return out;
  });
  result.finished_at = Date.now();
  result.visibility_at_end = document.visibilityState;
  await fetch("/result", { method: "POST", body: JSON.stringify(result) });
})().catch((e) => fetch("/result", { method: "POST", body: JSON.stringify({ fatal: String((e && e.message) || e) }) }));
