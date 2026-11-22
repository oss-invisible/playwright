"""Do the HOST's fontconfig rules change the pixels of the text a page reads? (e2e, Linux, firefox-68; a measurement that asserts the principle).

The question. On Linux the engine declares how FreeType loads a glyph (``zoom.stealth.text.freetype_hintstyle`` and ``..._antialias``, from the
core), but four more parameters of the face still come out of the fontconfig pattern of the host: ``rgba``, ``lcdfilter``, ``autohint`` and
``embeddedbitmap``. They are a rule of the machine (``/etc/fonts``, ``~/.config/fontconfig``): a value a page reads (``getImageData`` of a canvas
with text) must not depend on them. The comment of the core that declares the first two says the other four "measured identical in every
configuration tried", and that was measured on a canvas that is NOT opaque. Skia turns subpixel anti-aliasing on only for an OPAQUE canvas
(``getContext('2d', {alpha: false})``: ``SetPermitSubpixelAA(IsOpaque)``), which is where ``rgba`` and ``lcdfilter`` would show, and an embedded
bitmap (``embeddedbitmap``) only exists in some faces at some sizes. So this test reads both kinds of canvas, with faces that carry embedded
bitmaps, at the sizes where they exist.

What is drawn. One local page (http://127.0.0.1, a secure context, so ``crypto.subtle`` answers) draws ``fillText`` with the same seed in every
launch:

  * on two canvases per row: (a) OPAQUE, ``getContext('2d', {alpha: false})``, white fill first; (b) the DEFAULT one, white fill first too, so that
    the only difference between the two is the opacity of the canvas;
  * with fonts of the engine's bundle (the manifest of the core: ``tests/_font_names.py``): Arial (Latin), SimSun and MS Gothic (CJK). The two CJK
    faces are the ones of the bundle that ship embedded bitmaps on a retail Windows (SimSun at 12 to 16 px, MS Gothic at its small sizes), so
    they are where ``embeddedbitmap`` can show; Arial and its outlines are the control that has none;
  * at 9, 11, 12, 13 and 16 px, one row per (font, size) and canvas.

Each row returns the sha256 of the ``getImageData`` bytes, the number of pixels with R != G or G != B (subpixel anti-aliasing: grayscale text has
none), the number of pixels that are not white (the text is there) and the width ``measureText`` gives.

Six launches, one engine, one seed, each with its own ``FONTCONFIG_FILE``:

  1. nothing: ``/etc/fonts/fonts.conf`` alone (the rules of the runner, whatever they are);
  2. ``rgba=rgb`` and ``lcdfilter=lcddefault``;
  3. ``autohint=true``;
  4. ``embeddedbitmap=false``;
  4b. ``embeddedbitmap=true``;
  5. launch 1 and an ``<alias>`` that makes the generic ``system-ui`` (and ``Sans``, ``sans-serif``, which the toolkit's default font goes
     through) prefer a face of another family (DejaVu Serif, copied into a folder of its own that fontconfig is told to scan first).

Every config includes the base file first, then adds ``<match target="font">`` (and the same under ``target="pattern"``, so a rule that fontconfig
applies at either stage is there) with ``<edit mode="assign">``. In launches 1 and 5 (all of them, the cost is nothing) the page reads
``getComputedStyle(...).fontFamily`` of an element in ``system-ui`` and, by canvas and by layout, the width of a Latin string and of one CJK
character in ``system-ui``: a width that moves is a face of the host that reached the page.

How the environment reaches the browser. ``FONTCONFIG_FILE`` is set in the environment of the test process (``os.environ``) before each launch,
which is how a user's shell would carry it: ``InvisiblePlaywright`` composes the engine's environment from ``os.environ`` (the wrapper's
``build_env``, then the core's ``build_launch_env`` and ``_engine_env.compose``), and the core removes only the host families
``invisible_core.environment.HOST_FAMILIES`` (``MOZ_*``, ``MESA_*``, ``GTK_*``, ``GDK_*``, ``LC_*``...) and ``HOST_NAMES`` (``TZ``, ``LANG``,
``LANGUAGE``). ``FONTCONFIG_FILE`` is in neither, and neither the core nor the wrapper writes a ``FONTCONFIG_FILE`` or ``FONTCONFIG_PATH`` of its
own. The test reads ``/proc/<pid>/environ`` of the engine's processes to show the variable did arrive, and lists every ``*.conf`` the engine's
folder holds (a configuration of the bundle, if there were one, is in the record).

What is recorded, whatever the verdict. ``result.json`` in ``$FCRULES_RESULT_DIR`` (else the test's tmp folder): per launch, the config, the
per-row hashes and subpixel counts, the system-ui reads, what fontconfig itself says for the config (``fc-match``) and the environment seen in the
engine's processes. A table is printed (launch by canvas: same or different from launch 1, subpixel pixels in total). A failure to record is
swallowed: the record never changes the outcome.

The assertions (the principle: same seed, same bytes):
  * every row drew text (a blank canvas would make the comparison empty);
  * the configs bite in fontconfig itself (``fc-match`` says the rule), or the equality below would be vacuous;
  * launches 1, 2, 3, 4 and 4b give the same bytes as launch 1, per canvas, row by row;
  * the ``system-ui`` reads of launch 5 equal those of launch 1.

Not in any gate: this is a measurement (``.github/workflows/fontconfig-host-rules-hunt.yml``, a temporary workflow on a branch of its own).

Run: pytest tests/test_the_host_fontconfig_rules_do_not_reach_the_text_pixels_linux_e2e.py -m e2e -v -s
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="fontconfig is the Linux font system"),
]

SEED = 21

#: (font, text, why). The text of each is in the writing the face draws.
FONTS = [
    ("Arial", "Hamburgefonstiv 0123456789 éèàü", "Latin outlines: the control, no embedded bitmaps"),
    ("SimSun", "中文汉字测试 ABC abc 0123", "CJK, embedded bitmaps at 12-16 px on a retail Windows"),
    ("MS Gothic", "日本語のテキスト ABC abc ｱｲｳｴｵ", "CJK, embedded bitmaps at its small sizes"),
]
SIZES = [9, 11, 12, 13, 16]
CANVASES = ["opaque", "default"]

#: A face a runner has, copied into a folder of its own; its family differs from the one fontconfig answers to by default.
HOST_FACE_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
]
BASE_CONF = "/etc/fonts/fonts.conf"

#: name of the launch -> (rules, what the rules are). The rules are ``(property, kind, value)``.
LAUNCHES = [
    ("1_nothing", []),
    ("2_rgba_rgb_lcddefault", [("rgba", "const", "rgb"), ("lcdfilter", "const", "lcddefault")]),
    ("3_autohint", [("autohint", "bool", "true")]),
    ("4_embeddedbitmap_off", [("embeddedbitmap", "bool", "false")]),
    ("4b_embeddedbitmap_on", [("embeddedbitmap", "bool", "true")]),
    ("5_system_ui_alias", []),
]
#: What fontconfig itself must say for the config of each launch (``fc-match -f``): the proof that the rule is in force.
EXPECTED_FC = {
    "2_rgba_rgb_lcddefault": {"rgba": "1", "lcdfilter": "1"},
    "3_autohint": {"autohint": "True"},
    "4_embeddedbitmap_off": {"embeddedbitmap": "False"},
    "4b_embeddedbitmap_on": {"embeddedbitmap": "True"},
}
SYSTEM_UI_ALIASES = ("system-ui", "Sans", "sans-serif")

#: Draws every row. ``args`` = [fonts, sizes]. Returns [{canvas, font, size, sha256, subpixel, ink, width, fallback_width}].
ROWS_JS = """async (args) => {
  const [fonts, sizes] = args;
  const hex = (buf) => Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('');
  const fnv = (bytes) => { let h = 0x811c9dc5; for (let i = 0; i < bytes.length; i++) { h ^= bytes[i]; h = Math.imul(h, 0x01000193) >>> 0; } return 'fnv:' + h.toString(16); };
  const out = [];
  for (const kind of ['opaque', 'default']) {
    for (const [family, text] of fonts) {
      for (const size of sizes) {
        const c = document.createElement('canvas'); c.width = 400; c.height = size * 2 + 12;
        const x = kind === 'opaque' ? c.getContext('2d', {alpha: false}) : c.getContext('2d');
        x.fillStyle = '#fff'; x.fillRect(0, 0, c.width, c.height);
        x.fillStyle = '#000'; x.textBaseline = 'alphabetic';
        x.font = size + 'px "' + family + '"';
        x.fillText(text, 4, size + 4);
        const width = x.measureText(text).width;
        x.font = size + 'px "NoSuchFamilyForTheFallback"';
        const fallback_width = x.measureText(text).width;
        const data = x.getImageData(0, 0, c.width, c.height).data;
        let subpixel = 0, ink = 0;
        for (let i = 0; i < data.length; i += 4) {
          if (data[i] !== data[i + 1] || data[i + 1] !== data[i + 2]) subpixel++;
          if (data[i] !== 255 || data[i + 1] !== 255 || data[i + 2] !== 255) ink++;
        }
        const sha256 = (window.crypto && crypto.subtle) ? hex(await crypto.subtle.digest('SHA-256', data)) : fnv(data);
        out.push({canvas: kind, font: family, size, sha256, subpixel, ink, width, fallback_width});
      }
    }
  }
  return out;
}"""

#: What a page reads of ``system-ui``.
SYSTEM_UI_JS = """() => {
  const latin = 'Hamburgefonstiv 0123456789', cjk = '\\u4e2d';
  const span = (text) => { const s = document.createElement('span'); s.style.cssText = 'position:absolute;left:-9999px;white-space:pre;font:16px system-ui'; s.textContent = text; document.body.appendChild(s); return s; };
  const a = span(latin), b = span(cjk);
  const x = document.createElement('canvas').getContext('2d');
  x.font = '16px system-ui';
  const out = {
    computed_font_family: getComputedStyle(a).fontFamily,
    canvas_font: x.font,
    canvas_width_latin: x.measureText(latin).width,
    canvas_width_cjk: x.measureText(cjk).width,
    dom_width_latin: a.getBoundingClientRect().width,
    dom_width_cjk: b.getBoundingClientRect().width,
  };
  a.remove(); b.remove();
  return out;
}"""

_PAGE = b"<!doctype html><meta charset=utf-8><title>fcrules</title><body style='margin:0;background:#fff'>"


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_PAGE)))
        self.end_headers()
        self.wfile.write(_PAGE)

    def log_message(self, *args, **kwargs):
        pass


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _config_text(name: str, rules, base: str, host_dir, host_family: str) -> str:
    """The fonts.conf of a launch: the base file, then the rules."""
    parts = ['<?xml version="1.0"?>', '<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">', "<fontconfig>"]
    if name == "5_system_ui_alias":
        parts.append("  <dir>%s</dir>" % host_dir)
        for family in SYSTEM_UI_ALIASES:
            parts.append('  <alias binding="same"><family>%s</family><prefer><family>%s</family></prefer></alias>' % (family, host_family))
    parts.append('  <include ignore_missing="no">%s</include>' % base)
    for target in ("pattern", "font"):
        if rules:
            parts.append('  <match target="%s">' % target)
            for prop, kind, value in rules:
                parts.append('    <edit name="%s" mode="assign"><%s>%s</%s></edit>' % (prop, kind, value, kind))
            parts.append("  </match>")
    parts.append("</fontconfig>")
    return "\n".join(parts) + "\n"


def _fc_match(conf: Path, pattern: str, fmt: str) -> str:
    env = dict(os.environ, FONTCONFIG_FILE=str(conf))
    done = subprocess.run(["fc-match", "-f", fmt, pattern], env=env, capture_output=True, text=True, timeout=60)
    return done.stdout.strip()


def _what_fontconfig_says(name: str, conf: Path) -> dict:
    """What fontconfig itself says for this config (the host tool, not the browser)."""
    out = {}
    try:
        for key in ("rgba", "lcdfilter", "autohint", "embeddedbitmap"):
            out[key] = _fc_match(conf, "sans", "%%{%s}" % key)
        out["system-ui_family"] = _fc_match(conf, "system-ui", "%{family}")
        out["sans_family"] = _fc_match(conf, "sans", "%{family}")
    except Exception as exc:  # recorded, never decides
        out["error"] = repr(exc)
    return out


def _engine_environ(binary: str, conf: Path) -> dict:
    """``FONTCONFIG_FILE`` and ``FONTCONFIG_PATH`` in the environment of every process of the engine (``/proc/<pid>/environ``)."""
    seen = {"processes": 0, "with_the_file": 0, "values": [], "path_values": []}
    try:
        real = os.path.realpath(binary)
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                argv0 = Path(entry.path, "cmdline").read_bytes().split(b"\0")[0].decode("utf-8", "replace")
                if os.path.realpath(argv0) != real:
                    continue
                env = dict(item.partition(b"=")[::2] for item in Path(entry.path, "environ").read_bytes().split(b"\0") if item)
            except OSError:
                continue
            seen["processes"] += 1
            value = env.get(b"FONTCONFIG_FILE")
            if value is not None:
                seen["values"].append(value.decode("utf-8", "replace"))
                if value.decode("utf-8", "replace") == str(conf):
                    seen["with_the_file"] += 1
            if env.get(b"FONTCONFIG_PATH") is not None:
                seen["path_values"].append(env[b"FONTCONFIG_PATH"].decode("utf-8", "replace"))
    except Exception as exc:
        seen["error"] = repr(exc)
    return seen


def _confs_of_the_engine(binary: str) -> list:
    """Every ``*.conf`` file in the engine's folder (a configuration of the bundle, if it carried one)."""
    try:
        root = Path(os.path.realpath(binary)).parent
        return sorted(str(p.relative_to(root)) for p in root.rglob("*.conf"))[:50]
    except Exception:
        return []


def _launch(binary: str, conf: Path, url: str) -> dict:
    from invisible_playwright import InvisiblePlaywright

    result = {"fontconfig_file": str(conf)}
    # The variable of THIS launch, in the environment of the process: the wrapper composes the engine's environment from it.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("FONTCONFIG_FILE", str(conf))
        with InvisiblePlaywright(seed=SEED, binary_path=binary, headless=True) as browser:
            page = browser.new_context().new_page()
            page.goto(url, timeout=30_000)
            result["rows"] = page.evaluate(ROWS_JS, [[[f, t] for f, t, _w in FONTS], SIZES])
            result["system_ui"] = page.evaluate(SYSTEM_UI_JS)
            result["engine_environ"] = _engine_environ(binary, conf)
    return result


def _by_row(rows) -> dict:
    return {(r["canvas"], r["font"], r["size"]): r for r in rows}


def _compare(results: dict, base: str) -> dict:
    """{launch: {canvas: [rows that differ from the base launch]}}."""
    ref = _by_row(results[base]["rows"])
    out = {}
    for name, res in results.items():
        mine = _by_row(res["rows"])
        out[name] = {canvas: sorted("%s %dpx" % (k[1], k[2]) for k, row in mine.items()
                                    if k[0] == canvas and row["sha256"] != ref[k]["sha256"]) for canvas in CANVASES}
    return out


def _table(results: dict, base: str) -> str:
    diff = _compare(results, base)
    lines = ["", "launch x canvas -> vs %s (rows that differ / rows), subpixel pixels in total" % base]
    for name, res in results.items():
        cells = []
        for canvas in CANVASES:
            total = sum(r["subpixel"] for r in res["rows"] if r["canvas"] == canvas)
            rows = sum(1 for r in res["rows"] if r["canvas"] == canvas)
            d = diff[name][canvas]
            cells.append("%s: %s (%d/%d), subpixel %d" % (canvas, "same" if not d else "DIFFERENT", len(d), rows, total))
        lines.append("  %-24s %s" % (name, " | ".join(cells)))
    for name in results:
        for canvas in CANVASES:
            if diff[name][canvas]:
                lines.append("  %s %s differs in: %s" % (name, canvas, ", ".join(diff[name][canvas])))
    lines.append("system-ui reads:")
    for name in (base, "5_system_ui_alias"):
        if name in results:
            lines.append("  %-24s %s" % (name, json.dumps(results[name]["system_ui"], sort_keys=True)))
    return "\n".join(lines)


@pytest.fixture(scope="module")
def launches(firefox_binary, tmp_path_factory):
    """Every launch, once per module. -> {name: result}; the record is written before the assertions run."""
    work = tmp_path_factory.mktemp("fcrules")
    face = next((p for p in HOST_FACE_CANDIDATES if os.path.exists(p)), None)
    assert face, "no host face to copy for the system-ui launch: none of %r exists on this runner" % HOST_FACE_CANDIDATES
    host_dir = work / "hostfont"
    host_dir.mkdir()
    shutil.copy(face, host_dir / os.path.basename(face))
    host_family = "DejaVu Serif" if "DejaVu" in face else "Liberation Serif"
    base = os.environ.get("FONTCONFIG_FILE") or BASE_CONF
    assert os.path.exists(base), "the base fontconfig file %s does not exist on this runner" % base

    server = _Server(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/" % server.server_address[1]
    results, fc = {}, {}
    try:
        for name, rules in LAUNCHES:
            conf = work / ("%s.conf" % name)
            conf.write_text(_config_text(name, rules, base, host_dir, host_family), encoding="utf-8")
            fc[name] = _what_fontconfig_says(name, conf)
            results[name] = _launch(firefox_binary, conf, url)
            results[name]["config"] = conf.read_text(encoding="utf-8")
            results[name]["fc_match"] = fc[name]
    finally:
        server.shutdown()
        server.server_close()
        try:
            record = {
                "seed": SEED, "base_config": base, "host_face": face, "host_family": host_family, "fonts": [f for f, _t, _w in FONTS],
                "sizes": SIZES, "engine_confs": _confs_of_the_engine(firefox_binary), "launches": results,
                "differences_vs_1": _compare(results, "1_nothing") if "1_nothing" in results else None,
            }
            folder = Path(os.environ.get("FCRULES_RESULT_DIR") or work)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "result.json").write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
            if "1_nothing" in results:
                print(_table(results, "1_nothing"))
            print("engine *.conf files:", record["engine_confs"])
            print("FONTCONFIG_FILE seen in the engine's processes:",
                  {n: (r.get("engine_environ") or {}).get("with_the_file") for n, r in results.items()})
        except Exception as exc:  # a probe never changes the outcome
            print("the record could not be written:", repr(exc))
    return results


def test_the_host_fontconfig_rules_do_not_move_a_pixel_of_the_text_a_page_reads(launches):
    assert set(launches) == {n for n, _r in LAUNCHES}, "not every launch ran: %s" % sorted(launches)
    base = "1_nothing"
    # The comparison is not empty: every row drew text, and the environment of the launch is the config of the launch.
    blank = [(n, k) for n, res in launches.items() for k, r in _by_row(res["rows"]).items() if r["ink"] == 0]
    assert not blank, "rows with no text drawn (a blank canvas equals any other): %r" % blank[:6]
    for name, res in launches.items():
        seen = res["engine_environ"]
        if seen.get("processes"):
            assert seen["with_the_file"] > 0, (
                "%s: FONTCONFIG_FILE did not reach the engine's processes (%r)" % (name, seen))
    diff = _compare({n: launches[n] for n in launches if n != "5_system_ui_alias"}, base)
    moved = {n: d for n, d in diff.items() if n != base and any(d.values())}
    assert not moved, (
        "the host's fontconfig rules change the pixels of the text a page reads (rows that differ from launch 1, by launch and canvas): %s"
        % json.dumps(moved, sort_keys=True))


def test_the_rules_are_in_force_in_fontconfig_itself(launches):
    """Without this the equality above would say nothing: a config fontconfig ignores is equal to any other."""
    for name, expected in EXPECTED_FC.items():
        said = launches[name]["fc_match"]
        wrong = {k: (said.get(k), v) for k, v in expected.items() if said.get(k) != v}
        assert not wrong, "%s: fontconfig does not say what the config sets (got, wanted): %r (all it said: %r)" % (name, wrong, said)
    said = launches["5_system_ui_alias"]["fc_match"]
    assert said.get("system-ui_family") != launches["1_nothing"]["fc_match"].get("system-ui_family"), (
        "launch 5: fontconfig answers system-ui as it does with no rule: %r against %r" % (said, launches["1_nothing"]["fc_match"]))


def test_a_host_rule_does_not_choose_the_system_ui_face_a_page_reads(launches):
    first, fifth = launches["1_nothing"]["system_ui"], launches["5_system_ui_alias"]["system_ui"]
    assert first == fifth, (
        "a fontconfig alias of system-ui reaches what a page reads of it: launch 1 read %s, launch 5 read %s"
        % (json.dumps(first, sort_keys=True), json.dumps(fifth, sort_keys=True)))
