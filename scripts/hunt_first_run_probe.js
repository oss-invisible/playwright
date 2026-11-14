// HUNT ONLY (branch hunt/cpu-render-path, never merged). Served by scripts/hunt_first_run.py as /probe.js, loaded by the page /probe.
// What can a page read of the state a profile builds up in its first minutes (downloaded components: Widevine, OpenH264, the lists)?
// At each moment (seconds after the LAUNCH, read from /t0 and the shared clock) every probe below runs, concurrently, and posts its own result to
// /result as soon as it is done, so that a probe that waits (requestMediaKeySystemAccess waits for the CDM download, up to 60 s: see
// dom/media/eme/MediaKeySystemAccessManager.cpp AwaitInstall) does not delay the others. Each result: {moment, probe, started_ms (after launch), ms (how
// long the probe took), status: resolved | rejected | timeout, value}.
(async () => {
  const MOMENTS = (new URLSearchParams(location.search).get("moments") || "5,20,40,90").split(",").map(Number);
  const t0 = Number(await (await fetch("/t0", { cache: "no-store" })).text());
  const post = (o) => fetch("/result", { method: "POST", body: JSON.stringify(o), keepalive: true }).catch(() => {});
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const EME_CFG = [{
    initDataTypes: ["cenc", "webm"],
    audioCapabilities: [{ contentType: 'audio/mp4;codecs="mp4a.40.2"' }, { contentType: 'audio/webm;codecs="opus"' }],
    videoCapabilities: [{ contentType: 'video/mp4;codecs="avc1.42E01E"' }, { contentType: 'video/webm;codecs="vp8"' }],
    distinctiveIdentifier: "optional", persistentState: "optional", sessionTypes: ["temporary"],
  }];
  const H264 = "video/H264;level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f";

  async function timed(fn, limit) {
    const s = performance.now();
    try {
      const v = await Promise.race([fn(), sleep(limit).then(() => { throw { __timeout: true }; })]);
      return { status: "resolved", ms: Math.round(performance.now() - s), value: v };
    } catch (e) {
      if (e && e.__timeout) return { status: "timeout", ms: Math.round(performance.now() - s), value: "no answer in " + limit + " ms" };
      return { status: "rejected", ms: Math.round(performance.now() - s), value: (e && e.name ? e.name + ": " : "") + String((e && e.message) || e).slice(0, 200) };
    }
  }
  const eme = (ks, cfg) => async () => {
    const a = await navigator.requestMediaKeySystemAccess(ks, cfg || EME_CFG);
    return { keySystem: a.keySystem, config: a.getConfiguration() };
  };
  const mcEncrypted = (ks) => async () => {
    const r = await navigator.mediaCapabilities.decodingInfo({
      type: "media-source",
      video: { contentType: 'video/mp4; codecs="avc1.42E01E"', width: 640, height: 360, bitrate: 1000000, framerate: 30 },
      keySystemConfiguration: { keySystem: ks, initDataType: "cenc", video: { robustness: "" } },
    });
    return { supported: r.supported, smooth: r.smooth, powerEfficient: r.powerEfficient, keySystemAccess: r.keySystemAccess ? r.keySystemAccess.keySystem : null };
  };
  const mcPlain = (kind, cfg) => async () => {
    const r = await navigator.mediaCapabilities[kind](cfg);
    return { supported: r.supported, smooth: r.smooth, powerEfficient: r.powerEfficient };
  };
  const track = (url) => async () => {
    const c = new AbortController(); const t = setTimeout(() => c.abort(), 8000);
    try { await fetch(url, { mode: "no-cors", cache: "no-store", signal: c.signal }); return "fetch resolved"; }
    finally { clearTimeout(t); }
  };
  const probes = {
    "eme.widevine": () => timed(eme("com.widevine.alpha"), 80000),
    "eme.clearkey": () => timed(eme("org.w3.clearkey"), 80000),
    "eme.playready": () => timed(eme("com.microsoft.playready"), 80000),
    "eme.widevine.hwdrm": () => timed(eme("com.widevine.alpha", [{ initDataTypes: ["cenc"], videoCapabilities: [{ contentType: 'video/mp4;codecs="avc1.42E01E"', robustness: "HW_SECURE_ALL" }] }]), 80000),
    "mc.decoding.widevine": () => timed(mcEncrypted("com.widevine.alpha"), 80000),
    "mc.decoding.clearkey": () => timed(mcEncrypted("org.w3.clearkey"), 80000),
    "mc.decoding.plain": () => timed(mcPlain("decodingInfo", { type: "file", video: { contentType: 'video/mp4; codecs="avc1.42E01E"', width: 640, height: 360, bitrate: 1000000, framerate: 30 } }), 20000),
    "mc.decoding.webrtc.h264": () => timed(mcPlain("decodingInfo", { type: "webrtc", video: { contentType: H264, width: 640, height: 480, bitrate: 1000000, framerate: 30 } }), 20000),
    "mc.encoding.webrtc.h264": () => timed(mcPlain("encodingInfo", { type: "webrtc", video: { contentType: H264, width: 640, height: 480, bitrate: 1000000, framerate: 30 } }), 20000),
    "mc.encoding.webrtc.vp8": () => timed(mcPlain("encodingInfo", { type: "webrtc", video: { contentType: "video/VP8", width: 640, height: 480, bitrate: 1000000, framerate: 30 } }), 20000),
    "rtc.capabilities": () => timed(async () => {
      const f = (x) => x ? x.codecs.map((c) => c.mimeType + (c.sdpFmtpLine ? ";" + c.sdpFmtpLine : "")) : null;
      return { senderVideo: f(RTCRtpSender.getCapabilities("video")), receiverVideo: f(RTCRtpReceiver.getCapabilities("video")),
               senderAudio: f(RTCRtpSender.getCapabilities("audio")), receiverAudio: f(RTCRtpReceiver.getCapabilities("audio")) };
    }, 20000),
    "rtc.offer.codecs": () => timed(async () => {
      const pc = new RTCPeerConnection({ iceServers: [] });
      pc.addTransceiver("video"); pc.addTransceiver("audio");
      const o = await pc.createOffer(); pc.close();
      return o.sdp.split(/\r?\n/).filter((l) => /^a=(rtpmap|fmtp)/.test(l) && /H264|VP8|VP9|AV1|red|ulpfec|rtx/i.test(l)).slice(0, 60);
    }, 20000),
    "media.canplay": () => timed(async () => {
      const v = document.createElement("video");
      const types = ['video/mp4; codecs="avc1.42E01E"', 'audio/mp4; codecs="mp4a.40.2"', 'video/webm; codecs="vp9"', 'video/mp4; codecs="hev1.1.6.L93.B0"', 'video/mp4; codecs="av01.0.05M.08"'];
      return { canPlayType: types.map((t) => v.canPlayType(t)), isTypeSupported: types.map((t) => MediaSource.isTypeSupported(t)) };
    }, 20000),
    "media.devices": () => timed(async () => {
      const d = await navigator.mediaDevices.enumerateDevices();
      return { keys: Object.getOwnPropertyNames(Object.getPrototypeOf(navigator.mediaDevices)).sort(), devices: d.map((x) => [x.kind, x.label ? "labelled" : "no label"]) };
    }, 10000),
    "nav.plugins": () => timed(async () => ({ plugins: Array.from(navigator.plugins).map((p) => p.name), mimeTypes: Array.from(navigator.mimeTypes).map((m) => m.type), pdf: navigator.pdfViewerEnabled }), 5000),
    "storage.estimate": () => timed(async () => { const e = await navigator.storage.estimate(); return { quota: e.quota, usage: e.usage }; }, 10000),
    "permissions": () => timed(async () => {
      const out = {};
      for (const n of ["notifications", "geolocation", "camera", "microphone", "persistent-storage", "midi"]) {
        try { out[n] = (await navigator.permissions.query({ name: n })).state; } catch (e) { out[n] = "ERR " + e.name; }
      }
      return out;
    }, 10000),
    "net.control.same_origin": () => timed(async () => { await fetch("/t0", { cache: "no-store" }); return "ok"; }, 8000),
    "net.cryptomining_host": () => timed(track("https://coinhive.com/lib/coinhive.min.js"), 10000),
    "net.analytics_host": () => timed(track("https://www.google-analytics.com/analytics.js"), 10000),
  };

  async function run(moment) {
    const wait = t0 + moment * 1000 - Date.now();
    if (wait > 0) await sleep(wait);
    const startedMs = Date.now() - t0;
    await Promise.all(Object.entries(probes).map(async ([name, fn]) => {
      const r = await fn();
      await post({ moment, probe: name, started_ms: startedMs, ...r });
    }));
  }
  await post({ moment: -1, probe: "page.loaded", started_ms: Date.now() - t0, status: "resolved", ms: 0, value: navigator.userAgent });
  await Promise.all(MOMENTS.map(run));
  await post({ moment: -2, probe: "page.done", started_ms: Date.now() - t0, status: "resolved", ms: 0, value: "all moments done" });
})();
