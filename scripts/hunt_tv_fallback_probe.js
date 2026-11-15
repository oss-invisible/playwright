// HUNT: which face draws a fallback. For each draw that differed from retail, the same text is drawn with each candidate family named
// explicitly, at the same style and size; the candidate whose raster equals the fallback draw is the face the browser fell back to.
async () => {
  const CASES = [
    ["18px", "", "monospace", "cjk"], ["20px", "", "serif", "cjk"], ["28px", "", "monospace", "cjk"], ["34px", "italic", "serif", "cjk"],
    ["16px", "", "'Yu Gothic'", "cjk"], ["16px", "", "'Yu Gothic'", "latin"], ["15px", "italic", "'Segoe UI'", "arabic"], ["34px", "italic", "serif", "arabic"],
    ["16px", "bold", "Arial", "cjk"], ["40px", "bold", "sans-serif", "cjk"]];
  const CJK = ["Microsoft YaHei", "Microsoft JhengHei", "SimSun", "NSimSun", "MS Gothic", "MS Mincho", "Yu Gothic", "Yu Gothic UI", "Yu Mincho", "Meiryo", "Malgun Gothic", "SimHei", "MingLiU", "PMingLiU", "Segoe UI Symbol"];
  const AR = ["Segoe UI", "Arial", "Times New Roman", "Tahoma", "Microsoft Sans Serif", "Segoe UI Historic", "Sakkal Majalla", "Traditional Arabic", "Arabic Typesetting", "Courier New", "Calibri"];
  const LATIN = ["Yu Gothic", "Yu Gothic UI", "Segoe UI", "Arial", "Meiryo"];
  const TEXTS = {latin: "Hamburgefonstiv 0123456789 Ag", cjk: "日本語のテキスト 漢字", arabic: "مرحبا بالعالم"};
  async function digest(data) {
    const h = await crypto.subtle.digest("SHA-256", data);
    return Array.from(new Uint8Array(h)).slice(0, 8).map((b) => b.toString(16).padStart(2, "0")).join("");
  }
  async function draw(font, t) {
    const c = document.createElement("canvas"); c.width = 640; c.height = 64;
    const g = c.getContext("2d"); g.fillStyle = "#fff"; g.fillRect(0, 0, 640, 64);
    g.font = font; g.fillStyle = "#101820"; g.textBaseline = "alphabetic"; g.fillText(t, 4, 44);
    return { raster: await digest(g.getImageData(0, 0, 640, 64).data), width: g.measureText(t).width };
  }
  const out = [];
  for (const [size, style, family, text] of CASES) {
    const spec = [style, size, family].filter(Boolean).join(" ");
    const base = await draw(spec, TEXTS[text]);
    const cands = text === "cjk" ? CJK : text === "arabic" ? AR : LATIN;
    const matches = [], tried = [];
    for (const cand of cands) {
      const s = [style, size, "'" + cand + "'"].filter(Boolean).join(" ");
      const d = await draw(s, TEXTS[text]);
      tried.push([cand, document.fonts.check(s), d.width]);
      if (d.raster === base.raster) matches.push(cand);
    }
    out.push({ spec, text, raster: base.raster, width: base.width, matches, tried });
  }
  return out;
}
