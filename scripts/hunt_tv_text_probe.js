// The canvas text of each probe font drawn alone, one string at a time: its raster (sha-256 of the RGBA, first 8 bytes), its ink and its
// measureText boxes. One row per (font, text) says WHICH font and script a difference comes from, where the composite keys of
// page_bytes_probe.js only say that something changed. Used by tests/test_text_bytes_are_the_same_on_every_host_e2e.py.
async () => {
  const FONTS = ["16px sans-serif", "20px serif", "18px monospace", "bold 16px Arial", "italic 15px 'Segoe UI'", "16px 'Times New Roman'",
    "16px Calibri", "14px Consolas", "16px Verdana", "16px Tahoma", "17px Georgia", "16px 'Segoe UI Emoji'", "16px 'Microsoft YaHei'",
    "16px 'Yu Gothic'", "bold 40px sans-serif", "italic 34px serif", "28px monospace", "bold 44px Arial", "36px 'Times New Roman'", "30px 'Segoe UI'"];
  const TEXTS = {latin: "Hamburgefonstiv 0123456789 Ag", cjk: "日本語のテキスト 漢字", emoji: "😀🚀👍🏽", arabic: "مرحبا بالعالم"};
  async function digest(data) {
    const h = await crypto.subtle.digest("SHA-256", data);
    return Array.from(new Uint8Array(h)).slice(0, 8).map((b) => b.toString(16).padStart(2, "0")).join("");
  }
  const out = [];
  for (const f of FONTS) {
    for (const [name, t] of Object.entries(TEXTS)) {
      const c = document.createElement("canvas"); c.width = 640; c.height = 64;
      const g = c.getContext("2d"); g.fillStyle = "#fff"; g.fillRect(0, 0, 640, 64);
      g.font = f; g.fillStyle = "#101820"; g.textBaseline = "alphabetic"; g.fillText(t, 4, 44);
      const m = g.measureText(t);
      const px = g.getImageData(0, 0, 640, 64).data;
      let ink = 0; for (let i = 0; i < px.length; i += 4) if (px[i] < 250) ink++;
      out.push({font: f, text: name, raster: await digest(px), ink, width: m.width, asc: m.actualBoundingBoxAscent,
                desc: m.actualBoundingBoxDescent, fasc: m.fontBoundingBoxAscent, fdesc: m.fontBoundingBoxDescent,
                check: document.fonts.check(f)});
    }
  }
  return out;
}
