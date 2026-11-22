#!/usr/bin/env python3
"""The poisoned host of the page A/B oracle.

The page oracle compares two engines on one machine, so a value both read from that machine passes the comparison. This puts values on the
machine that the engine must never read, for the run of the page oracle only (the job of that leg: the host oracle's parsers read
tracerpt and strace output and are not in it), and proves that they are there with a NEGATIVE CONTROL: Playwright's own Firefox on the
same runner reads them. The engine's dumps are then held to the identity the core declares, never to the poison.

  poison          where Firefox reads it                                      what the engine declares instead
  time zone       Lima (UTC-5, no DST): Windows `tzutil`, Linux `TZ`          the zone of the host gate's reference (Asia/Bangkok), by the launch
  language        ja-JP: Windows `Set-Culture` and the user language list,   en-US, by the launch
                  Linux `LANG`
  colour scheme   dark: Windows AppsUseLightTheme=0, Linux `GTK_THEME`        the scheme of the seed's persona (dark for 40% of them)
  fonts           a user-installed CANARY font, family `ZZHostCanary`:        no such family: a user font is not the declared machine's
                  Windows per-user fonts, Linux ~/.local/share/fonts
  audio (Linux)   a null sink at 96000 Hz and 6 channels, the default sink    the persona's sample rate (44100 or 48000) and channel count

The canary is generated here, byte for byte the same every time (`canary_font()`: a TrueType font of three glyphs, written from the
specification, no third-party font and so no licence): every letter and digit is a box 1.5 em wide, so a page that reads the family
measures a text far wider than any fallback. Its sha256 is CANARY_SHA256 and the tests hold the two together.

    python ci_poison_host.py apply  [--state FILE] [--no-audio]    put the poison on this machine (and verify it); writes the undo state
    python ci_poison_host.py verify [--expect-env]                 the poison is in force (--expect-env: this process also inherited the variables)
    python ci_poison_host.py undo   --state FILE                   put back what apply found
    python ci_poison_host.py control --out FILE                    the negative control: stock Firefox of Playwright reads the poison, or exit 2 (broken control)
    python ci_poison_host.py font   --out FILE                     write the canary font

Windows warning. `Set-Culture` changes the number and date formats of every process started after it. Every PowerShell this script runs
starts by forcing the invariant culture on its own thread, and the state it keeps is text it read before the change: nothing here
parses a formatted number or a date. The poison belongs to the page oracle's job and `undo` runs before that job's judge.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
PAGE = HERE / "host_gate" / "host_gate_page.html"

FAMILY = "ZZHostCanary"
FONT_FILE = FAMILY + ".ttf"
#: Where Firefox must read Lima from, in the two spellings: the Windows id and the IANA name (the offsets are what a page tells apart).
WINDOWS_ZONE, IANA_ZONE = "SA Pacific Standard Time", "America/Lima"
LIMA_OFFSETS = [300, 300]
CULTURE, LINUX_LANG = "ja-JP", "ja_JP.UTF-8"
GTK_THEME = "Adwaita:dark"
SINK, SINK_RATE, SINK_CHANNELS = "hostcanary", 96000, 6
SINK_MAP = "front-left,front-right,front-center,lfe,rear-left,rear-right"
#: The variables a Linux poison puts in the environment of the later steps ($GITHUB_ENV), and the one that says it is on.
LINUX_ENV = {"TZ": IANA_ZONE, "LANG": LINUX_LANG, "LANGUAGE": "ja", "GTK_THEME": GTK_THEME}
#: A text of 12 'm' at 12px in the canary: 12 boxes of 1.5 em. A generic family that measures this is the canary.
CANARY_TEXT_WIDTH = 216.0
GENERICS = ("sans-serif", "serif", "monospace")
SWITCH = "AB_ORACLE_POISON"
#: The variable that names the file `apply` wrote its undo state to.
STATE_SWITCH = "AB_ORACLE_POISON_STATE"
THEME_KEY = r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
FONTS_KEY = r"HKCU:\Software\Microsoft\Windows NT\CurrentVersion\Fonts"
FONT_VALUE = FAMILY + " (TrueType)"

Run = Callable[[list], tuple]


# -
# The canary font.

def _checksum(data: bytes) -> int:
    data += b"\0" * (-len(data) % 4)
    return sum(struct.unpack(">%dI" % (len(data) // 4), data)) & 0xFFFFFFFF


def _pad(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) % 4)


def canary_font() -> bytes:
    """A TrueType font, family ZZHostCanary, of three glyphs: .notdef and every character of U+0021..U+007E are boxes, U+0020 is blank.
    The advance of a box is 1500 units of 1000 per em, the space's 500. Deterministic: no clock, no randomness."""
    upem, box_advance, space_advance = 1000, 1500, 500
    box = struct.pack(">hhhhh", 1, 100, 0, 1400, 700) + struct.pack(">H", 3) + struct.pack(">H", 0) + bytes([1, 1, 1, 1]) \
        + struct.pack(">4h", 100, 0, 1300, 0) + struct.pack(">4h", 0, 700, 0, -700)
    glyf = _pad(box) + _pad(box)  # .notdef and the box: the same drawing
    loca = struct.pack(">4I", 0, len(_pad(box)), 2 * len(_pad(box)), 2 * len(_pad(box)))  # .notdef, box, space (empty)
    hmtx = struct.pack(">HhHhHh", box_advance, 100, box_advance, 100, space_advance, 0)
    head = struct.pack(">IIIIHHqqhhhhHHhhh", 0x00010000, 0x00010000, 0, 0x5F0F3CF5, 0x000B, upem,
                       3_700_000_000, 3_700_000_000, 0, 0, 1400, 700, 0, 8, 2, 1, 0)
    hhea = struct.pack(">IhhhHhhhhhhhhhhhH", 0x00010000, 800, -200, 0, box_advance, 100, 100, 1400, 1, 0, 0, 0, 0, 0, 0, 0, 3)
    maxp = struct.pack(">IHHHHHHHHHHHHHH", 0x00010000, 3, 4, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0)
    os2 = struct.pack(">HhHHH11h", 4, 1500, 400, 5, 0, 650, 600, 0, 75, 650, 600, 0, 350, 50, 250, 0)
    os2 += bytes(10) + struct.pack(">IIII", 1, 0, 0, 0) + b"NONE"
    os2 += struct.pack(">HHHhhhHHII", 0x40, 0x20, 0x7E, 800, -200, 0, 800, 200, 1, 0) + struct.pack(">hhHHH", 500, 700, 0, 0x20, 1)
    post = struct.pack(">IIhhIIIII", 0x00030000, 0, -100, 50, 0, 0, 0, 0, 0)
    # cmap: format 4, one segment per character (a constant glyph needs no glyph-index array)
    chars = [(0x20, 2)] + [(c, 1) for c in range(0x21, 0x7F)]
    ends = [c for c, _ in chars] + [0xFFFF]
    deltas = [(g - c) & 0xFFFF for c, g in chars] + [1]
    seg = len(ends)
    sub = (struct.pack(">HHHHHHH", 4, 16 + 8 * seg, 0, seg * 2, 128, 6, 2 * seg - 128)
           + struct.pack(">%dH" % seg, *ends) + struct.pack(">H", 0) + struct.pack(">%dH" % seg, *ends)
           + struct.pack(">%dH" % seg, *deltas) + struct.pack(">%dH" % seg, *([0] * seg)))
    cmap = struct.pack(">HHHHI", 0, 1, 3, 1, 12) + sub
    names = {1: FAMILY, 2: "Regular", 3: FAMILY + "-Regular-1.000", 4: FAMILY + " Regular", 5: "Version 1.000", 6: FAMILY + "-Regular"}
    strings, records = b"", b""
    for nid in sorted(names):
        text = names[nid].encode("utf-16-be")
        records += struct.pack(">HHHHHH", 3, 1, 0x409, nid, len(text), len(strings))
        strings += text
    name = struct.pack(">HHH", 0, len(names), 6 + 12 * len(names)) + records + strings
    tables = {b"OS/2": os2, b"cmap": cmap, b"glyf": glyf, b"head": head, b"hhea": hhea, b"hmtx": hmtx,
              b"loca": loca, b"maxp": maxp, b"name": name, b"post": post}
    n = len(tables)
    out = struct.pack(">IHHHH", 0x00010000, n, 128, 3, n * 16 - 128)
    offset = 12 + 16 * n
    body = b""
    for tag in sorted(tables):
        data = tables[tag]
        out += struct.pack(">4sIII", tag, _checksum(data), offset + len(body), len(data))
        body += _pad(data)
    font = bytearray(out + body)
    head_at = offset + sum(len(_pad(tables[t])) for t in sorted(tables) if t < b"head") + 8
    struct.pack_into(">I", font, head_at, (0xB1B0AFBA - _checksum(bytes(font))) & 0xFFFFFFFF)
    return bytes(font)


#: The sha256 of canary_font(): a font that changed is a different poison, said here and held by a test.
CANARY_SHA256 = "fc1a766e6253f2791fb422f241db7e14a9793a8975ebd867c13e1257110e4045"


def canary_sha256() -> str:
    return hashlib.sha256(canary_font()).hexdigest()


def read_tables(font: bytes) -> dict:
    """{tag: bytes} of an sfnt, after checking the directory's checksums and the whole-file checksum (head.checkSumAdjustment)."""
    n = struct.unpack_from(">H", font, 4)[0]
    tables = {}
    for i in range(n):
        tag, csum, off, length = struct.unpack_from(">4sIII", font, 12 + 16 * i)
        data = font[off:off + length]
        if tag != b"head" and _checksum(data) != csum:
            raise ValueError("table %r: bad checksum" % tag)
        tables[tag.decode("latin-1")] = data
    if _checksum(font) != 0xB1B0AFBA:
        raise ValueError("the whole-file checksum is not the one head.checkSumAdjustment makes")
    return tables


# -
# What the page reads: the probe lives in the page (host_gate_page.html, between the two markers) and the control evaluates the same text.

PROBE_BEGIN, PROBE_END = "// ab.poison:begin", "// ab.poison:end"


def probe_source(page: Path = PAGE) -> str:
    """The probe function of the page, `const abPoisonProbe = async () => {...};`, exactly as the page runs it."""
    text = page.read_text(encoding="utf-8")
    if text.count(PROBE_BEGIN) != 1 or text.count(PROBE_END) != 1 or text.index(PROBE_BEGIN) > text.index(PROBE_END):
        raise SystemExit("%s must hold the probe between one %r and one %r" % (page.name, PROBE_BEGIN, PROBE_END))
    return text.split(PROBE_BEGIN, 1)[1].split(PROBE_END, 1)[0]


# -
# The engine under the poison: what its dumps must say instead.

def declared(seed: int, reference=None, dpr: float = 1.0) -> dict:
    """What the identity of `seed` declares for each poisoned value: from the core's prefs of the seed (the dark scheme, the audio chain) and
    from the retail reference of the host gate (the zone and the language the launch declares, the strings they make)."""
    import ci_host_gate as gate
    import persona_cover as personas

    prefs = {k: json.loads(v) for k, v in personas.seed_features(seed)[1].items()}
    intl = gate.reference_items(reference or gate.REFERENCE, dpr)["surface.intl"]
    return {"timezone": intl["DateTimeFormat"]["timeZone"], "offsets": intl["offsets"], "locale": intl["DateTimeFormat"]["locale"],
            "language": intl["languages"][0], "date_full": intl["strings"][4], "number": intl["strings"][3],
            "dark": bool(prefs["ui.systemUsesDarkTheme"]),
            "sample_rate": prefs[personas.P + "audio.sample_rate"], "max_channels": prefs[personas.P + "audio.max_channel_count"]}


def _is_canary_width(width) -> bool:
    return isinstance(width, (int, float)) and abs(width - CANARY_TEXT_WIDTH) < 0.5


def findings(probe, want: dict) -> list:
    """What an engine's `ab.poison` leaves say about the host, against `want` (declared()): every line is a value that is the machine's
    (or that is not what the persona declares), empty when the page read none of the poison. A page that dumped no probe is a finding too:
    it would pass anything."""
    if not isinstance(probe, dict) or not probe:
        return ["the dump has no ab.poison leaves: the page did not read the poison, so nothing was checked"]
    out = []

    def expect(key, value, what):
        if probe.get(key) != value:
            out.append("%s: the page read %s, the identity declares %s" % (what, json.dumps(probe.get(key)), json.dumps(value)))

    expect("timezone", want["timezone"], "time zone")
    expect("offsets", want["offsets"], "UTC offsets of January and July")
    if probe.get("offsets") == LIMA_OFFSETS and want["offsets"] != LIMA_OFFSETS:
        out.append("time zone: the offsets are the host's Lima (UTC-5, no DST)")
    expect("language", want["language"], "navigator.language")
    expect("intl_locale", want["locale"], "the default Intl locale")
    expect("date_full", want["date_full"], "a full date")
    expect("number", want["number"], "a formatted number")
    expect("dark", want["dark"], "prefers-color-scheme dark")
    canary = probe.get("canary")
    if not isinstance(canary, dict) or "present" not in canary:
        out.append("canary font: the probe did not measure it")
    elif canary["present"]:
        out.append("canary font: a page measures the user-installed family %s (its text is %s wide, a fallback's %s)"
                   % (FAMILY, json.dumps(canary.get("with")), json.dumps(canary.get("without"))))
    generic = probe.get("generic")
    if not isinstance(generic, list) or len(generic) != len(GENERICS):
        out.append("generic families: the probe did not measure them: %s" % json.dumps(generic))
    else:
        out += ["generic family %s: a page measures %s wide, the canary's width: the host's fontconfig chose it" % (g, w)
                for g, w in zip(GENERICS, generic) if _is_canary_width(w)]
    audio = probe.get("audio")
    if not isinstance(audio, dict):
        out.append("audio: the probe did not read the audio context: %s" % json.dumps(audio))
    else:
        if audio.get("sampleRate") != want["sample_rate"]:
            out.append("audio: sampleRate %s, the persona declares %s" % (audio.get("sampleRate"), want["sample_rate"]))
        if audio.get("maxChannelCount") != want["max_channels"]:
            out.append("audio: maxChannelCount %s, the persona declares %s" % (audio.get("maxChannelCount"), want["max_channels"]))
    return out


def dump_findings(dump: dict, wants: Callable = declared) -> list:
    """findings() of every visit of a packed dump (ci_ab_oracle.pack), as 'seed N dprD: line'."""
    out = []
    for visit in dump.get("visits", []):
        probe = visit["items"].get("ab.poison")
        out += ["seed %d dpr %s: %s" % (visit["seed"], visit["dpr"], line) for line in findings(probe, wants(visit["seed"]))]
    return out


# -
# The by-product: what the published engine reads from the host that the poison changes.
#
# The same job dumps the published engine on the CLEAN runner (before the poison is applied) and, in the oracle's own run, on the POISONED one,
# the same seeds. Every leaf that differs is a host dependency the published engine ALREADY has: not a failure of the candidate and never red,
# a finding, kept in a file of its own and summarised in the job's summary.

CLEAN_FILE = "poison-clean-68.json"
DEPENDENCE_FILE = "poison-host-dependence-68.json"
DEPENDENCE_FORMAT = "poison-host-dependence/1"
CLIP = 300


def _clip(value):
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return value if len(text) <= CLIP else {"clipped": text[:CLIP], "len": len(text)}


def host_dependence(clean: dict, poisoned: dict) -> dict:
    """The leaves of the published engine that differ between its dump on the clean runner and on the poisoned one (both packed dumps,
    ci_ab_oracle.pack), over the visits both have: {"visits", "missing", "leaves_compared", "differences": [{seed, dpr, leaf, clean, poisoned}]}."""
    import ci_host_gate as gate

    def by_visit(dump):
        return {(v["dpr"], v["seed"]): {k: x for k, x in gate.leaves(v["items"]).items() if not gate.is_diag(k)} for v in dump.get("visits", [])}

    a, b = by_visit(clean), by_visit(poisoned)
    both = sorted(set(a) & set(b), key=lambda k: (k[1], k[0]))
    differences, compared = [], 0
    for dpr, seed in both:
        for leaf in sorted(set(a[(dpr, seed)]) | set(b[(dpr, seed)])):
            compared += 1
            x, y = a[(dpr, seed)].get(leaf, "<missing>"), b[(dpr, seed)].get(leaf, "<missing>")
            if x != y:
                differences.append({"seed": seed, "dpr": dpr, "leaf": leaf, "clean": _clip(x), "poisoned": _clip(y)})
    return {"format": DEPENDENCE_FORMAT, "visits": ["seed %d dpr %s" % (s, d) for d, s in both],
            "missing": ["seed %d dpr %s" % (s, d) for d, s in sorted(set(a) ^ set(b), key=lambda k: (k[1], k[0]))],
            "leaves_compared": compared, "differences": differences}


def dependence_markdown(report: dict | None) -> str:
    """The summary section of the job: the count, the leaves by family and the first few with both values."""
    lines = ["### Host dependencies of the published engine (clean runner against poisoned runner)", ""]
    if report is None:
        return "\n".join(lines + ["No dump of the published engine on the clean runner: the step before the poison did not write one, "
                                  "so the by-product is not available in this run.", ""])
    diffs = report["differences"]
    lines.append("%d of %d leaves differ over %d visits (%s). Each is a value the engine read from the host that the poison changed: "
                 "a finding about the published engine, not a failure of the candidate." % (len(diffs), report["leaves_compared"], len(report["visits"]),
                                                                                           ", ".join(report["visits"]) or "none"))
    if report["missing"]:
        lines.append("Visits that only one of the two dumps has: %s." % ", ".join(report["missing"]))
    families = {}
    for d in diffs:
        families[".".join(d["leaf"].split(".")[:2])] = families.get(".".join(d["leaf"].split(".")[:2]), 0) + 1
    if families:
        lines += ["", "| family | leaves |", "|---|---|"] + ["| %s | %d |" % kv for kv in sorted(families.items(), key=lambda kv: (-kv[1], kv[0]))[:30]]
        lines += ["", "| seed | leaf | clean | poisoned |", "|---|---|---|---|"]
        for d in diffs[:40]:
            lines.append("| %s | %s | %s | %s |" % (d["seed"], d["leaf"], json.dumps(d["clean"], ensure_ascii=False)[:80].replace("|", "/"),
                                                     json.dumps(d["poisoned"], ensure_ascii=False)[:80].replace("|", "/")))
    return "\n".join(lines + [""])


def write_dependence(out: Path, summary: Path | None = None) -> dict | None:
    """Write DEPENDENCE_FILE in `out` from CLEAN_FILE and dump-68.json there, and the markdown to `summary` (the job's $GITHUB_STEP_SUMMARY).
    Returns the report, or None when there is no clean dump. Never a verdict."""
    clean, poisoned = out / CLEAN_FILE, out / "dump-68.json"
    report = None
    if clean.is_file() and poisoned.is_file():
        report = host_dependence(json.loads(clean.read_text(encoding="utf-8")), json.loads(poisoned.read_text(encoding="utf-8")))
        (out / DEPENDENCE_FILE).write_text(json.dumps(report, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    text = dependence_markdown(report)
    if summary:
        with open(summary, "a", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
    print(text)
    return report


# -
# The negative control: what stock Firefox must read on the poisoned runner.

#: Each requirement of the control, with what it means. Language is reported and not required: Playwright's Firefox is an en-US build, which
#: takes its language from its own locale, not from the machine; the control prints what it read, and the requirement is one line away
#: (REQUIRED) once a run has shown that stock reads it.
REQUIRED = ("timezone", "dark", "canary", "audio", "generic", "language")


def control_failures(probe, system: str) -> list:
    """[(requirement, message)] of what stock Firefox did NOT read of the poison. `system` is 'linux' or 'windows' (audio is Linux's)."""
    if not isinstance(probe, dict):
        return [("probe", "the probe returned %s" % json.dumps(probe))]
    out = []
    if probe.get("offsets") != LIMA_OFFSETS:
        out.append(("timezone", "stock Firefox read the offsets %s, not Lima's %s" % (json.dumps(probe.get("offsets")), LIMA_OFFSETS)))
    if not language_reads(probe)["japanese"]:
        out.append(("language", "stock Firefox read no Japanese language, date or number format: %s" % json.dumps(language_reads(probe)["reads"], ensure_ascii=False)))
    if probe.get("dark") is not True:
        out.append(("dark", "stock Firefox read prefers-color-scheme dark = %s" % json.dumps(probe.get("dark"))))
    canary = probe.get("canary")
    if not isinstance(canary, dict) or canary.get("present") is not True:
        out.append(("canary", "stock Firefox did not measure the user-installed family %s: %s" % (FAMILY, json.dumps(canary))))
    if system == "linux":
        generic = probe.get("generic")
        if not isinstance(generic, list) or not generic or not all(_is_canary_width(w) for w in generic):
            out.append(("generic", "stock Firefox measured the generic families %s, not the canary's %s: the user's fonts.conf was not read"
                        % (json.dumps(generic), CANARY_TEXT_WIDTH)))
        audio = probe.get("audio")
        if not isinstance(audio, dict) or audio.get("sampleRate") != SINK_RATE:
            out.append(("audio", "stock Firefox read the audio %s, not the null sink's %d Hz" % (json.dumps(audio), SINK_RATE)))
    return [(name, message) for name, message in out if name in REQUIRED]


def language_reads(probe) -> dict:
    """What stock read of the language, for the log: the leaves and whether any of them is Japanese."""
    keys = ("language", "languages", "intl_locale", "date_full", "number")
    reads = {k: probe.get(k) for k in keys} if isinstance(probe, dict) else {}
    text = json.dumps(reads, ensure_ascii=False)
    return {"reads": reads, "japanese": bool(re.search(r'"ja\b|"ja-|[぀-ヿ一-鿿]', text))}


def system_name() -> str:
    return "windows" if sys.platform == "win32" else "linux"


def control(out: Path, headless: bool = False) -> int:
    """Launch Playwright's own Firefox (headed: a headless Firefox has no theme to read) on this runner, read the probe, write `out`.
    0 when it read the poison, 2 BROKEN CONTROL (the poison is not in force or the control cannot see it)."""
    from playwright.sync_api import sync_playwright

    out.parent.mkdir(parents=True, exist_ok=True)
    probe = None
    error = None
    try:
        with sync_playwright() as p:
            browser = p.firefox.launch(headless=headless, firefox_user_prefs={"intl.regional_prefs.use_os_locales": True})
            try:
                # Playwright's default is a LIGHT scheme (an emulation): the control must follow the system, as a user's browser does
                page = browser.new_context(color_scheme="no-override").new_page()
                page.goto("about:blank")
                probe = page.evaluate("(async () => {%s; return await abPoisonProbe(); })()" % probe_source())
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001 - a control that could not run is a broken control, with the reason
        error = "%s: %s" % (type(e).__name__, str(e)[:500])
    failures = [("run", error)] if error else control_failures(probe, system_name())
    report = {"format": "poison-control/1", "system": system_name(), "probe": probe, "language": language_reads(probe),
              "failures": [{"requirement": n, "message": m} for n, m in failures], "verdict": "green" if not failures else "broken"}
    out.write_text(json.dumps(report, sort_keys=True, indent=1, ensure_ascii=False), encoding="utf-8")
    print("stock Firefox read: %s" % json.dumps(probe, sort_keys=True, ensure_ascii=False))
    print("language, informational: %s" % json.dumps(report["language"], sort_keys=True, ensure_ascii=False))
    for name, message in failures:
        print("BROKEN CONTROL (%s): %s" % (name, message))
    print("POISON CONTROL %s" % ("OK" if not failures else "BROKEN"))
    return 0 if not failures else 2


# -
# Putting the poison on a machine, and taking it off.

def _run(cmd: list) -> tuple:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    except (OSError, subprocess.SubprocessError) as e:  # the program is not on this machine, or did not answer
        return 127, "%s: %s" % (type(e).__name__, e)
    return done.returncode, (done.stdout or "") + (done.stderr if done.returncode else "")


#: Every PowerShell of this script: the invariant culture on its own thread first (the culture of the machine is what the poison changes).
PS_PRELUDE = ("[System.Threading.Thread]::CurrentThread.CurrentCulture = [System.Globalization.CultureInfo]::InvariantCulture; "
              "$ErrorActionPreference = 'Stop'; ")


def ps(script: str) -> list:
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", PS_PRELUDE + script]


def _ok(run: Run, cmd: list, what: str) -> str:
    rc, text = run(cmd)
    if rc != 0:
        raise SystemExit("the poison could not %s (exit %s): %s" % (what, rc, text.strip()[:400]))
    return text.strip()


def _try(run: Run, cmd: list) -> str:
    rc, text = run(cmd)
    return text.strip() if rc == 0 else ""


def font_path_windows(env=None) -> Path:
    return Path((env or os.environ)["LOCALAPPDATA"]) / "Microsoft" / "Windows" / "Fonts" / FONT_FILE


def font_dir_linux(env=None) -> Path:
    env = env or os.environ
    base = env.get("XDG_DATA_HOME") or str(Path(env.get("HOME") or Path.home()) / ".local" / "share")
    return Path(base) / "fonts"


def _reg_get(name: str) -> list:
    return ps("$v = (Get-ItemProperty -Path '%s' -Name %s -ErrorAction SilentlyContinue).%s; if ($null -ne $v) { $v }" % (THEME_KEY, name, name))


def _reg_set(name: str, value) -> list:
    if value in ("", None):
        return ps("Remove-ItemProperty -Path '%s' -Name %s -ErrorAction SilentlyContinue" % (THEME_KEY, name))
    return ps("New-ItemProperty -Path '%s' -Name %s -Value %s -PropertyType DWord -Force | Out-Null" % (THEME_KEY, name, int(value)))


def apply_windows(run: Run = _run, font_dir: Path | None = None) -> dict:
    """Put the poison on this Windows machine; returns the state `undo_windows` needs (what was there before, as text read before any change)."""
    font = font_path_windows() if font_dir is None else Path(font_dir) / FONT_FILE
    state = {"system": "windows", "font": str(font),
             "timezone": _ok(run, ["tzutil", "/g"], "read the time zone"),
             "culture": _ok(run, ps("(Get-Culture).Name"), "read the culture"),
             "languages": _ok(run, ps("(Get-WinUserLanguageList | ForEach-Object { $_.LanguageTag }) -join ','"), "read the user languages"),
             "theme": {n: _try(run, _reg_get(n)) for n in ("AppsUseLightTheme", "SystemUsesLightTheme")}}
    _ok(run, ["tzutil", "/s", WINDOWS_ZONE], "set the time zone")
    _ok(run, ps("Set-Culture -CultureInfo '%s'" % CULTURE), "set the culture")
    _ok(run, ps("Set-WinUserLanguageList -LanguageList '%s' -Force" % CULTURE), "set the user language list")
    for name in state["theme"]:
        _ok(run, _reg_set(name, 0), "set " + name)
    font.parent.mkdir(parents=True, exist_ok=True)
    font.write_bytes(canary_font())
    _ok(run, ps("New-ItemProperty -Path '%s' -Name '%s' -Value '%s' -PropertyType String -Force | Out-Null" % (FONTS_KEY, FONT_VALUE, font)),
        "register the canary font")
    _announce_font(str(font))
    state["after"] = windows_reads(run)
    return state


def primary_language(tag: str) -> str:
    """The language of a tag, whatever its form: 'ja', 'ja-JP' and 'ja_JP' are Japanese."""
    return re.split(r"[-_]", (tag or "").strip(), maxsplit=1)[0].lower()


def windows_reads(run: Run = _run) -> dict:
    """What this Windows machine reports about the poison, in the form it reports it (Windows keeps the tag 'ja' for the list that was set from
    'ja-JP'): the apply records it and verify holds the machine to it later, besides the semantic check that it is Japanese."""
    return {"timezone": _try(run, ["tzutil", "/g"]),
            "culture": _try(run, ps("(Get-Culture).Name")),
            "languages": _try(run, ps("(Get-WinUserLanguageList | ForEach-Object { $_.LanguageTag }) -join ','")),
            "theme": {n: _try(run, _reg_get(n)) for n in ("AppsUseLightTheme", "SystemUsesLightTheme")}}


def _announce_font(path: str) -> None:
    """Tell the running session about the font (AddFontResourceW and WM_FONTCHANGE); a process started later reads the registry anyway."""
    try:
        import ctypes

        ctypes.windll.gdi32.AddFontResourceW(path)
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001D, 0, 0, 2, 1000, None)
    except Exception:  # noqa: BLE001 - best effort: the registry entry is what a new process reads
        pass


def _forget_font(path: str) -> None:
    try:
        import ctypes

        ctypes.windll.gdi32.RemoveFontResourceW(path)
    except Exception:  # noqa: BLE001 - best effort, as the announcement
        pass


def undo_windows(state: dict, run: Run = _run) -> None:
    font = Path(state["font"])
    _try(run, ps("Remove-ItemProperty -Path '%s' -Name '%s' -ErrorAction SilentlyContinue" % (FONTS_KEY, FONT_VALUE)))
    _forget_font(str(font))
    try:
        font.unlink()
    except OSError:
        pass
    for name, value in state["theme"].items():
        _try(run, _reg_set(name, value))
    if state.get("languages"):
        _try(run, ps("Set-WinUserLanguageList -LanguageList %s -Force" % ",".join("'%s'" % t for t in state["languages"].split(","))))
    if state.get("culture"):
        _try(run, ps("Set-Culture -CultureInfo '%s'" % state["culture"]))
    if state.get("timezone"):
        _try(run, ["tzutil", "/s", state["timezone"]])


def verify_windows(run: Run = _run, state: dict | None = None) -> list:
    problems = []
    font = Path(state["font"]) if state else font_path_windows()
    tz = _try(run, ["tzutil", "/g"])
    if tz != WINDOWS_ZONE:
        problems.append("time zone is %r, not %r" % (tz, WINDOWS_ZONE))
    reads = windows_reads(run)
    recorded = (state or {}).get("after") or {}
    for what, value in (("culture", reads["culture"]), ("first user language", reads["languages"].split(",")[0])):
        if primary_language(value) != primary_language(CULTURE):
            problems.append("the %s is %r, which is not Japanese" % (what, value))
    for key, what in (("culture", "culture"), ("languages", "user language list")):
        if recorded.get(key) and reads[key] != recorded[key]:
            problems.append("the %s is %r, the apply recorded %r" % (what, reads[key], recorded[key]))
    for name in ("AppsUseLightTheme", "SystemUsesLightTheme"):
        value = _try(run, _reg_get(name))
        if value != "0":
            problems.append("%s is %r, not 0 (dark)" % (name, value))
    registered = _try(run, ps("(Get-ItemProperty -Path '%s' -Name '%s' -ErrorAction SilentlyContinue).'%s'" % (FONTS_KEY, FONT_VALUE, FONT_VALUE)))
    if os.path.normcase(registered) != os.path.normcase(str(font)):
        problems.append("the canary font is registered at %r, not %r" % (registered, str(font)))
    problems += _font_file_problems(font)
    return problems


def _font_file_problems(font: Path) -> list:
    if not font.is_file():
        return ["the canary font file %s is missing" % font.name]
    if hashlib.sha256(font.read_bytes()).hexdigest() != CANARY_SHA256:
        return ["the canary font file is not the canary (sha256 differs from %s)" % CANARY_SHA256]
    return []


def _sudo(cmd: list) -> list:
    return cmd if getattr(os, "geteuid", lambda: 0)() == 0 else ["sudo", "-n"] + cmd


def _sink_server(info: str) -> str:
    m = re.search(r"^Server String:\s*(\S+)", info, re.M)
    return m.group(1) if m else ""


def fonts_conf() -> str:
    """The user's fontconfig file of the Linux poison: the canary is the first choice of the three generic families, and the rendering
    settings are ones no desktop has (no antialiasing, full hinting, rgb subpixels, the light LCD filter). The engine's default FcConfig is
    still the host's (FcConfigSubstitute(nullptr, ...) in gfxFcPlatformFontList and ScaledFontFontconfig), so a page that reads what this
    changes read the host."""
    aliases = "".join('  <alias binding="strong"><family>%s</family><prefer><family>%s</family></prefer></alias>\n' % (g, FAMILY) for g in GENERICS)
    return ('<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n<fontconfig>\n' + aliases
            + '  <match target="font">\n'
              '    <edit name="antialias" mode="assign"><bool>false</bool></edit>\n'
              '    <edit name="hinting" mode="assign"><bool>true</bool></edit>\n'
              '    <edit name="hintstyle" mode="assign"><const>hintfull</const></edit>\n'
              '    <edit name="rgba" mode="assign"><const>rgb</const></edit>\n'
              '    <edit name="lcdfilter" mode="assign"><const>lcdlight</const></edit>\n'
              '  </match>\n</fontconfig>\n')


def fonts_conf_path(env=None) -> Path:
    env = env or os.environ
    base = env.get("XDG_CONFIG_HOME") or str(Path(env.get("HOME") or Path.home()) / ".config")
    return Path(base) / "fontconfig" / "fonts.conf"


def apply_linux(run: Run = _run, font_dir: Path | None = None, audio: bool = True, conf_file: Path | None = None) -> dict:
    """Put the poison on this Linux machine; returns the state `undo_linux` needs. The environment part is for the later steps of the job
    (state["env"], appended to $GITHUB_ENV by `apply`)."""
    fonts = font_dir_linux() if font_dir is None else Path(font_dir)
    state = {"system": "linux", "font": str(fonts / FONT_FILE), "env": dict(LINUX_ENV),
             "before_env": {k: os.environ.get(k, "") for k in list(LINUX_ENV) + ["PULSE_SERVER"]}, "module": ""}
    if "ja_jp.utf8" not in _try(run, ["locale", "-a"]).lower().replace("-", ""):
        _ok(run, _sudo(["locale-gen", LINUX_LANG]), "generate the %s locale" % LINUX_LANG)
    fonts.mkdir(parents=True, exist_ok=True)
    (fonts / FONT_FILE).write_bytes(canary_font())
    conf = fonts_conf_path() if conf_file is None else Path(conf_file)
    state["fonts_conf"] = {"path": str(conf), "before": conf.read_text(encoding="utf-8") if conf.is_file() else None}
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(fonts_conf(), encoding="utf-8", newline="\n")
    _ok(run, ["fc-cache", "-f", str(fonts)], "refresh the font cache")
    if audio:
        _try(run, ["pulseaudio", "--daemonize=yes", "--exit-idle-time=-1", "--disallow-exit"])
        module = _ok(run, ["pactl", "load-module", "module-null-sink", "sink_name=" + SINK, "rate=%d" % SINK_RATE,
                           "channels=%d" % SINK_CHANNELS, "channel_map=" + SINK_MAP, "sink_properties=device.description=" + SINK],
                     "load the null sink")
        state["module"] = module
        _ok(run, ["pactl", "set-default-sink", SINK], "make the null sink the default")
        server = _sink_server(_try(run, ["pactl", "info"]))
        if server:
            state["env"]["PULSE_SERVER"] = server if "://" in server or ":" in server.split("/")[0] else "unix:" + server
    return state


def undo_linux(state: dict, run: Run = _run) -> None:
    if state.get("module"):
        _try(run, ["pactl", "unload-module", state["module"]])
        _try(run, ["pulseaudio", "--kill"])
    font = Path(state["font"])
    try:
        font.unlink()
    except OSError:
        pass
    saved = state.get("fonts_conf")
    if saved:  # the file that was there comes back, or the one this wrote goes
        conf = Path(saved["path"])
        try:
            if saved["before"] is None:
                conf.unlink()
            else:
                conf.write_text(saved["before"], encoding="utf-8", newline="\n")
        except OSError:
            pass
    _try(run, ["fc-cache", "-f", str(font.parent)])


def verify_linux(run: Run = _run, state: dict | None = None, expect_env: bool = False, audio: bool = True, env=None) -> list:
    problems = []
    font = Path(state["font"]) if state else font_dir_linux() / FONT_FILE
    if "ja_jp.utf8" not in _try(run, ["locale", "-a"]).lower().replace("-", ""):
        problems.append("the %s locale is not installed" % LINUX_LANG)
    problems += _font_file_problems(font)
    if FAMILY not in _try(run, ["fc-list", ":", "family"]):
        problems.append("fontconfig does not list the family %s" % FAMILY)
    conf = Path(state["fonts_conf"]["path"]) if state and state.get("fonts_conf") else fonts_conf_path()
    if not conf.is_file() or conf.read_text(encoding="utf-8") != fonts_conf():
        problems.append("the user's fonts.conf is not the poison's")
    for generic in GENERICS:
        picked = _try(run, ["fc-match", "-f", "%{family}", generic])
        if FAMILY not in picked:
            problems.append("fontconfig answers %r for %s, not the canary" % (picked, generic))
    if audio:
        sinks = _try(run, ["pactl", "list", "short", "sinks"])
        line = next((ln for ln in sinks.splitlines() if SINK in ln), "")
        if "%dch %dHz" % (SINK_CHANNELS, SINK_RATE) not in line:
            problems.append("the null sink %s is not %d channels at %d Hz: %r" % (SINK, SINK_CHANNELS, SINK_RATE, line))
        if SINK not in _try(run, ["pactl", "get-default-sink"]):
            problems.append("the null sink is not the default sink")
    if expect_env:
        e = os.environ if env is None else env
        problems += ["%s is %r in this process, not %r" % (k, e.get(k), v) for k, v in LINUX_ENV.items() if e.get(k) != v]
        if audio and not e.get("PULSE_SERVER"):
            problems.append("PULSE_SERVER is not set in this process: Firefox would not find the null sink's server")
    return problems


def verify_here(expect_env: bool = False, run: Run = _run) -> list:
    """Is the poison in force on this machine, for a process that wants to know (the page oracle's test): what apply wrote in the state file
    named by AB_ORACLE_POISON_STATE tells where the font is and whether there is a null sink."""
    state_file = os.environ.get(STATE_SWITCH)
    state = json.loads(Path(state_file).read_text(encoding="utf-8")) if state_file and Path(state_file).is_file() else None
    if system_name() == "windows":
        return verify_windows(run, state)
    return verify_linux(run, state, expect_env, audio=bool(state and state.get("audio")))


def github_env_lines(env: dict) -> str:
    return "".join("%s=%s\n" % (k, v) for k, v in env.items())


def apply(state_file: Path | None, audio: bool = True, run: Run = _run) -> int:
    system = system_name()
    state = apply_windows(run) if system == "windows" else apply_linux(run, audio=audio)
    state["audio"] = audio and system == "linux"
    if state_file:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state, sort_keys=True, indent=1), encoding="utf-8")
    target = os.environ.get("GITHUB_ENV")
    if target and state_file:  # what was changed must be undoable even when it is not in force: the undo step reads this
        with open(target, "a", encoding="utf-8", newline="\n") as f:
            f.write(github_env_lines({STATE_SWITCH: str(state_file.resolve())}))
    problems = verify_windows(run, state) if system == "windows" else verify_linux(run, state, audio=state["audio"])
    for p in problems:
        print("POISON NOT IN FORCE: " + p)
    if problems:
        return 2
    env = dict(state.get("env", {}), **{SWITCH: "1"})
    if state_file:
        env[STATE_SWITCH] = str(state_file.resolve())
    if target:
        with open(target, "a", encoding="utf-8", newline="\n") as f:
            f.write(github_env_lines(env))
    print("the host is poisoned (%s): %s" % (system, json.dumps({k: v for k, v in state.items() if k not in ("before_env",)}, sort_keys=True)))
    return 0


def undo(state_file: Path, run: Run = _run) -> int:
    state = json.loads(state_file.read_text(encoding="utf-8"))
    if state["system"] == "windows":
        undo_windows(state, run)
    else:
        undo_linux(state, run)
        target = os.environ.get("GITHUB_ENV")
        if target:  # the later steps of the job read what it was
            with open(target, "a", encoding="utf-8", newline="\n") as f:
                f.write(github_env_lines(dict(state["before_env"], **{SWITCH: "", STATE_SWITCH: ""})))
    print("the poison is taken off (%s)" % state["system"])
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("apply")
    a.add_argument("--state", type=Path)
    a.add_argument("--no-audio", action="store_true", help="Linux: no null audio sink")
    v = sub.add_parser("verify")
    v.add_argument("--state", type=Path)
    v.add_argument("--expect-env", action="store_true")
    v.add_argument("--no-audio", action="store_true")
    u = sub.add_parser("undo")
    u.add_argument("--state", type=Path, required=True)
    c = sub.add_parser("control")
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--headless", action="store_true")
    f = sub.add_parser("font")
    f.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.cmd == "apply":
        return apply(args.state, audio=not args.no_audio)
    if args.cmd == "undo":
        return undo(args.state)
    if args.cmd == "control":
        return control(args.out, args.headless)
    if args.cmd == "font":
        args.out.write_bytes(canary_font())
        print(canary_sha256())
        return 0
    state = json.loads(args.state.read_text(encoding="utf-8")) if args.state else None
    system = system_name()
    problems = verify_windows(_run, state) if system == "windows" else verify_linux(_run, state, args.expect_env, not args.no_audio)
    for p in problems:
        print("POISON NOT IN FORCE: " + p)
    return 2 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
