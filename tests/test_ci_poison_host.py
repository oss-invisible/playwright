"""The poisoned host of the page A/B oracle (scripts/ci_poison_host.py), no browser and nothing put on this machine.

What is held: the canary font is a valid, deterministic font (and is the one whose hash the script declares); the probe the page and the negative control
share is one text; the findings say, for each poisoned value, that an engine that read it is caught, and that an engine that read the declared identity
is not; the negative control fails when stock Firefox did not read the poison; and apply, verify and undo issue the commands of each system in an order
that puts back what was found (a fake runner records them: nothing here touches the zone, the culture, the registry or the fonts of this machine).
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ci_poison_host as poison  # noqa: E402

pytestmark = pytest.mark.unit

WANT = {"timezone": "Asia/Bangkok", "offsets": [-420, -420], "locale": "en-US", "language": "en-US",
        "date_full": "Thursday, January 15, 2026", "number": "1,234,567.891", "dark": False, "sample_rate": 48000, "max_channels": 2}
OURS = {"timezone": "Asia/Bangkok", "offsets": [-420, -420], "language": "en-US", "languages": ["en-US", "en"], "intl_locale": "en-US",
        "date_full": "Thursday, January 15, 2026", "number": "1,234,567.891", "dark": False, "canary_check": True,
        "canary": {"with": [100, 100], "without": [100, 100], "present": False}, "generic": [30.5, 41.0, 52.0],
        "audio": {"sampleRate": 48000, "maxChannelCount": 2}}
STOCK = {"timezone": "America/Bogota", "offsets": [300, 300], "language": "en-US", "languages": ["en-US", "en"], "intl_locale": "ja-JP",
         "date_full": "2026\u5e741\u670815\u65e5\u6728\u66dc\u65e5", "number": "1,234,567.891",
         "dark": True, "canary_check": True, "canary": {"with": [180, 180], "without": [100, 100], "present": True},
         "generic": [216.0, 216.0, 216.0], "audio": {"sampleRate": 96000, "maxChannelCount": 6}}


# -
# The canary font.

def test_the_canary_is_the_same_bytes_every_time_and_is_the_one_the_script_declares():
    assert poison.canary_font() == poison.canary_font()
    assert poison.canary_sha256() == poison.CANARY_SHA256


def test_the_canary_is_a_well_formed_sfnt_of_a_family_with_wide_letters():
    font = poison.canary_font()
    tables = poison.read_tables(font)  # raises on a bad table checksum or a bad whole-file checksum
    assert sorted(tables) == ["OS/2", "cmap", "glyf", "head", "hhea", "hmtx", "loca", "maxp", "name", "post"]
    assert struct.unpack_from(">I", font)[0] == 0x00010000
    units_per_em = struct.unpack_from(">H", tables["head"], 18)[0]
    number_of_glyphs = struct.unpack_from(">H", tables["maxp"], 4)[0]
    assert (units_per_em, number_of_glyphs) == (1000, 3)
    advances = struct.unpack(">HhHhHh", tables["hmtx"])[::2]
    assert advances == (1500, 1500, 500)  # a box is 1.5 em wide: far from any fallback's letter
    # the family is in the name table, as UTF-16BE of platform 3
    count, offset = struct.unpack_from(">HH", tables["name"], 2)
    records = [struct.unpack_from(">HHHHHH", tables["name"], 6 + 12 * i) for i in range(count)]
    family = next(r for r in records if r[3] == 1)
    text = tables["name"][offset + family[5]:offset + family[5] + family[4]].decode("utf-16-be")
    assert text == poison.FAMILY == "ZZHostCanary"


def test_the_canary_maps_every_printable_ascii_character_to_a_glyph():
    cmap = poison.read_tables(poison.canary_font())["cmap"]
    sub = struct.unpack_from(">I", cmap, 8)[0]
    seg2 = struct.unpack_from(">H", cmap, sub + 6)[0]
    segments = seg2 // 2
    ends = struct.unpack_from(">%dH" % segments, cmap, sub + 14)
    deltas = struct.unpack_from(">%dH" % segments, cmap, sub + 16 + 2 * segments * 2)
    glyph = {end: (end + delta) & 0xFFFF for end, delta in zip(ends, deltas)}
    assert {glyph[c] for c in range(0x21, 0x7F)} == {1}
    assert glyph[0x20] == 2
    assert ends[-1] == 0xFFFF


# -
# The probe, shared by the page and the control.

def test_the_probe_is_one_self_contained_block_of_the_page():
    source = poison.probe_source()
    assert source.count("const abPoisonProbe = async () =>") == 1
    for helper in ("put(", "safe(", "errs(", "plain(", "noise("):
        assert helper not in source, "the control evaluates the block alone: %s is the page's" % helper
    page = (ROOT / "scripts" / "host_gate" / "host_gate_page.html").read_text(encoding="utf-8")
    assert "await safe('ab.poison', abPoisonProbe);" in page


def test_a_page_without_the_markers_is_refused(tmp_path):
    page = tmp_path / "page.html"
    page.write_text("<script>no markers</script>", encoding="utf-8")
    with pytest.raises(SystemExit):
        poison.probe_source(page)


# -
# What the engine must not read.

def test_an_engine_that_reads_the_declared_identity_has_no_finding():
    assert poison.findings(OURS, WANT) == []


@pytest.mark.parametrize("change, says", [
    ({"offsets": [300, 300]}, "Lima"),
    ({"timezone": "America/Lima"}, "time zone"),
    ({"language": "ja-JP"}, "navigator.language"),
    ({"intl_locale": "ja-JP"}, "Intl locale"),
    ({"date_full": "2026年1月15日木曜日"}, "full date"),
    ({"dark": True}, "prefers-color-scheme"),
    ({"canary": {"with": [180, 180], "without": [100, 100], "present": True}}, "ZZHostCanary"),
    ({"audio": {"sampleRate": 96000, "maxChannelCount": 6}}, "sampleRate 96000"),
    ({"generic": [216.0, 41.0, 52.0]}, "generic family sans-serif"),
    ({"generic": [30.5, 216.0, 216.0]}, "generic family monospace"),
    ({"generic": None}, "did not measure"),
    ({"audio": {"sampleRate": 48000, "maxChannelCount": 6}}, "maxChannelCount 6"),
    ({"audio": "ERR NotSupportedError"}, "did not read the audio"),
])
def test_each_poisoned_value_is_caught_when_the_engine_reads_it(change, says):
    lines = poison.findings(dict(OURS, **change), WANT)
    assert lines and any(says in line for line in lines), lines


def test_a_dump_without_the_probe_is_a_finding_not_a_pass():
    assert poison.findings(None, WANT) and poison.findings({}, WANT)
    assert "nothing was checked" in poison.findings({}, WANT)[0]


def test_a_dark_host_is_invisible_on_a_persona_that_declares_dark_and_caught_on_one_that_does_not():
    # why the dark scheme is held against the SEED's declaration and not against 'light': 40% of the personas declare dark
    assert poison.findings(dict(OURS, dark=True), dict(WANT, dark=True)) == []
    assert poison.findings(dict(OURS, dark=True), dict(WANT, dark=False))


def test_document_fonts_check_answering_true_is_not_a_finding_the_width_decides():
    # the specification: check() is true for a family nobody has
    assert OURS["canary_check"] is True and poison.findings(OURS, WANT) == []


def test_a_dump_is_judged_visit_by_visit_with_the_declaration_of_its_seed():
    dump = {"visits": [{"seed": 0, "dpr": 1.0, "items": {"ab.poison": OURS}},
                       {"seed": 63, "dpr": 1.25, "items": {"ab.poison": dict(OURS, dark=True)}}]}
    lines = poison.dump_findings(dump, wants=lambda seed: WANT)
    assert len(lines) == 1 and lines[0].startswith("seed 63 dpr 1.25: ")


def test_the_declaration_of_a_seed_comes_from_the_core_and_the_reference():
    d0, d63 = poison.declared(0), poison.declared(63)
    assert set(d0) == set(WANT)
    assert d0["timezone"] == "Asia/Bangkok" and d0["offsets"] != poison.LIMA_OFFSETS
    assert d0["sample_rate"] in (44100, 48000) and d63["sample_rate"] in (44100, 48000)
    assert isinstance(d0["dark"], bool)


# -
# The negative control.

def test_stock_firefox_that_read_the_poison_is_a_sound_control():
    for system in ("linux", "windows"):
        assert poison.control_failures(STOCK, system) == []


@pytest.mark.parametrize("change, requirement", [
    ({"offsets": [-420, -420]}, "timezone"),
    ({"dark": False}, "dark"),
    ({"canary": {"with": [100, 100], "without": [100, 100], "present": False}}, "canary"),
    ({"audio": {"sampleRate": 48000, "maxChannelCount": 2}}, "audio"),
    ({"generic": [30.5, 216.0, 216.0]}, "generic"),
    ({"generic": None}, "generic"),
    ({"intl_locale": "en-US", "date_full": "Thursday, January 15, 2026"}, "language"),
])
def test_a_poison_that_stock_firefox_did_not_read_is_a_broken_control(change, requirement):
    failures = poison.control_failures(dict(STOCK, **change), "linux")
    assert [name for name, _ in failures] == [requirement]


def test_the_audio_sink_and_the_generic_families_are_linux_s_only():
    assert poison.control_failures(dict(STOCK, audio={"sampleRate": 48000, "maxChannelCount": 2}, generic=[30.5, 41.0, 52.0]), "windows") == []


def test_a_control_that_returned_nothing_is_broken():
    assert poison.control_failures(None, "linux")[0][0] == "probe"


def test_the_control_requires_that_stock_read_a_japanese_language_wherever_it_shows():
    assert "language" in poison.REQUIRED
    assert poison.language_reads(dict(STOCK, intl_locale="en-US", date_full="x"))["japanese"] is False
    assert poison.language_reads(STOCK)["japanese"] is True
    # the language of navigator may stay en-US (the build's own): a Japanese format is the language of the machine, read
    assert poison.control_failures(dict(STOCK, language="en-US"), "windows") == []
    assert poison.language_reads(dict(STOCK, language="ja-JP"))["japanese"] is True
    assert poison.language_reads(dict(STOCK, date_full="2026年1月15日"))["japanese"] is True


# -
# Windows: the commands, their order and the way back.

class Fake:
    """A runner that records what it was asked and answers like a machine that has a zone, a culture, a language list and a theme."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = {"tzutil /g": "Tokyo Standard Time", "(Get-Culture).Name": "en-US", "Get-WinUserLanguageList": "en-US,it-IT",
                        "AppsUseLightTheme": "1", "SystemUsesLightTheme": "1"}
        self.answers.update(answers or {})

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        text = " ".join(cmd)
        for key, value in self.answers.items():
            if key in text:
                return 0, value
        return 0, ""

    def text(self):
        return [" ".join(c) for c in self.calls]


@pytest.fixture(autouse=True)
def own_home(tmp_path, monkeypatch):
    """The Linux poison writes under the user's config and data folders: they are this test's own."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


def put_conf(tmp_path):
    conf = poison.fonts_conf_path()
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(poison.fonts_conf(), encoding="utf-8")
    return {"font": str(tmp_path / poison.FONT_FILE), "fonts_conf": {"path": str(conf), "before": None}}


@pytest.fixture()
def no_session(monkeypatch):
    """The two calls that tell the running Windows session about a font are not made from a test."""
    monkeypatch.setattr(poison, "_announce_font", lambda path: None)
    monkeypatch.setattr(poison, "_forget_font", lambda path: None)


def test_windows_apply_reads_everything_before_it_changes_anything(tmp_path, no_session):
    fake = Fake()
    state = poison.apply_windows(fake, font_dir=tmp_path)
    lines = fake.text()
    first_change = next(i for i, line in enumerate(lines) if "tzutil /s" in line)
    assert all("Set-" not in line and "New-ItemProperty" not in line for line in lines[:first_change])
    assert state["timezone"] == "Tokyo Standard Time" and state["culture"] == "en-US" and state["languages"] == "en-US,it-IT"
    assert state["theme"] == {"AppsUseLightTheme": "1", "SystemUsesLightTheme": "1"}
    assert any("tzutil /s %s" % poison.WINDOWS_ZONE in line for line in lines)
    assert any("Set-Culture -CultureInfo 'ja-JP'" in line for line in lines)
    assert any("Set-WinUserLanguageList -LanguageList 'ja-JP' -Force" in line for line in lines)
    assert sum("-Name AppsUseLightTheme -Value 0" in line or "-Name SystemUsesLightTheme -Value 0" in line for line in lines) == 2
    assert (tmp_path / poison.FONT_FILE).read_bytes() == poison.canary_font()
    assert any(poison.FONT_VALUE in line and "-PropertyType String" in line for line in lines)


def test_every_powershell_of_the_script_forces_the_invariant_culture_first(tmp_path, no_session):
    fake = Fake()
    poison.apply_windows(fake, font_dir=tmp_path)
    poison.verify_windows(fake, {"font": str(tmp_path / poison.FONT_FILE)})
    poison.undo_windows({"font": str(tmp_path / poison.FONT_FILE), "theme": {"AppsUseLightTheme": "1"}, "languages": "en-US", "culture": "en-US",
                         "timezone": "Tokyo Standard Time"}, fake)
    shells = [c for c in fake.calls if c[0] == "powershell.exe"]
    assert shells
    for cmd in shells:
        assert "-NoProfile" in cmd and "-NonInteractive" in cmd
        script = cmd[-1]
        assert script.startswith("[System.Threading.Thread]::CurrentThread.CurrentCulture = [System.Globalization.CultureInfo]::InvariantCulture")


def test_windows_undo_puts_back_what_was_found_and_removes_what_was_added(tmp_path, no_session):
    fake = Fake()
    state = poison.apply_windows(fake, font_dir=tmp_path)
    fake.calls.clear()
    poison.undo_windows(state, fake)
    lines = fake.text()
    assert not (tmp_path / poison.FONT_FILE).exists()
    assert any("Remove-ItemProperty" in line and poison.FONT_VALUE in line for line in lines)
    assert any("-Name AppsUseLightTheme -Value 1" in line for line in lines)
    assert any("Set-Culture -CultureInfo 'en-US'" in line for line in lines)
    assert any("Set-WinUserLanguageList -LanguageList 'en-US','it-IT' -Force" in line for line in lines)
    assert any(line.endswith("tzutil /s Tokyo Standard Time") for line in lines)


def test_a_theme_value_that_was_not_there_is_removed_not_set_on_undo(tmp_path, no_session):
    fake = Fake({"AppsUseLightTheme": "", "SystemUsesLightTheme": ""})
    state = poison.apply_windows(fake, font_dir=tmp_path)
    assert state["theme"] == {"AppsUseLightTheme": "", "SystemUsesLightTheme": ""}
    fake.calls.clear()
    poison.undo_windows(state, fake)
    removed = [line for line in fake.text() if "Remove-ItemProperty" in line and "Personalize" in line]
    assert len(removed) == 2


def test_windows_apply_that_a_command_refuses_stops_with_the_command_named(tmp_path, no_session):
    def refuse(cmd):
        return (1, "access denied") if cmd[:2] == ["tzutil", "/s"] else Fake()(cmd)

    with pytest.raises(SystemExit) as stop:
        poison.apply_windows(refuse, font_dir=tmp_path)
    assert "set the time zone" in str(stop.value) and "access denied" in str(stop.value)


def test_windows_verify_names_each_value_that_is_not_in_force(tmp_path):
    font = tmp_path / poison.FONT_FILE
    font.write_bytes(poison.canary_font())
    right = Fake({"tzutil /g": poison.WINDOWS_ZONE, "(Get-Culture).Name": "ja-JP", "Get-WinUserLanguageList": "ja-JP",
                  "AppsUseLightTheme": "0", "SystemUsesLightTheme": "0", "Get-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT": str(font)})
    assert poison.verify_windows(right, {"font": str(font)}) == []
    wrong = Fake()
    problems = poison.verify_windows(wrong, {"font": str(font)})
    assert any("time zone" in p for p in problems) and any("culture" in p for p in problems) and any("AppsUseLightTheme" in p for p in problems)
    assert any("registered" in p for p in problems)
    font.write_bytes(b"not the canary")
    assert any("not the canary" in p for p in poison.verify_windows(right, {"font": str(font)}))


# -
# Linux.

def test_linux_apply_installs_the_locale_the_font_and_the_null_sink(tmp_path):
    answers = {"locale -a": "C\nPOSIX\nen_US.utf8", "load-module": "42", "pactl info": "Server String: /run/user/1001/pulse/native\n"}
    fake = Fake(answers)
    state = poison.apply_linux(fake, font_dir=tmp_path / "fonts")
    lines = fake.text()
    assert any(line.endswith("locale-gen ja_JP.UTF-8") for line in lines)
    assert (tmp_path / "fonts" / poison.FONT_FILE).read_bytes() == poison.canary_font()
    assert any(line.startswith("fc-cache -f") for line in lines)
    sink = next(line for line in lines if "module-null-sink" in line)
    assert "rate=96000" in sink and "channels=6" in sink and "sink_name=hostcanary" in sink
    assert any(line == "pactl set-default-sink hostcanary" for line in lines)
    assert state["module"] == "42"
    assert state["env"]["TZ"] == "America/Lima" and state["env"]["LANG"] == "ja_JP.UTF-8" and state["env"]["GTK_THEME"] == "Adwaita:dark"
    assert state["env"]["PULSE_SERVER"] == "unix:/run/user/1001/pulse/native"


def test_linux_apply_without_audio_makes_no_sink_and_a_present_locale_is_not_generated_again(tmp_path):
    fake = Fake({"locale -a": "C\nja_JP.utf8"})
    state = poison.apply_linux(fake, font_dir=tmp_path, audio=False)
    lines = fake.text()
    assert not any("locale-gen" in line for line in lines) and not any("pactl" in line for line in lines)
    assert "PULSE_SERVER" not in state["env"] and state["module"] == ""


def test_linux_undo_unloads_the_sink_and_removes_the_font(tmp_path):
    fake = Fake({"locale -a": "ja_JP.utf8", "load-module": "7"})
    state = poison.apply_linux(fake, font_dir=tmp_path)
    fake.calls.clear()
    poison.undo_linux(state, fake)
    lines = fake.text()
    assert "pactl unload-module 7" in lines
    assert not (tmp_path / poison.FONT_FILE).exists()


def test_linux_verify_wants_the_sink_to_be_6_channels_at_96000_and_the_default(tmp_path):
    font = tmp_path / poison.FONT_FILE
    font.write_bytes(poison.canary_font())
    state = {"font": str(font)}
    state = dict(state, **put_conf(tmp_path))
    good = Fake({"locale -a": "ja_JP.utf8", "fc-list": "ZZHostCanary", "fc-match": "ZZHostCanary", "list short sinks": "1\thostcanary\tmodule-null-sink.c\ts16le 6ch 96000Hz\tIDLE",
                 "get-default-sink": "hostcanary"})
    assert poison.verify_linux(good, state) == []
    bad = Fake({"locale -a": "ja_JP.utf8", "fc-list": "ZZHostCanary", "fc-match": "ZZHostCanary", "list short sinks": "1\thostcanary\tmodule-null-sink.c\ts16le 2ch 44100Hz\tIDLE",
                "get-default-sink": "alsa_output"})
    problems = poison.verify_linux(bad, state)
    assert any("6 channels at 96000" in p for p in problems) and any("default sink" in p for p in problems)
    assert any("fontconfig" in p for p in poison.verify_linux(Fake({"locale -a": "ja_JP.utf8"}), state, audio=False))


def test_linux_verify_of_the_environment_is_for_the_process_that_inherited_it(tmp_path):
    font = tmp_path / poison.FONT_FILE
    font.write_bytes(poison.canary_font())
    quiet = Fake({"locale -a": "ja_JP.utf8", "fc-list": "ZZHostCanary", "fc-match": "ZZHostCanary"})
    state = dict({"font": str(font)}, **put_conf(tmp_path))
    assert poison.verify_linux(quiet, state, audio=False) == []
    problems = poison.verify_linux(quiet, state, expect_env=True, audio=False, env={"TZ": "UTC"})
    assert len(problems) == 4 and any("TZ" in p for p in problems)
    assert poison.verify_linux(quiet, state, expect_env=True, audio=False, env=dict(poison.LINUX_ENV)) == []


# -
# apply and undo, as the CLI runs them.

def test_apply_tells_the_later_steps_by_github_env_and_undo_gives_the_old_values_back(tmp_path, monkeypatch):
    target = tmp_path / "github_env"
    state_file = tmp_path / "state.json"
    monkeypatch.setenv("GITHUB_ENV", str(target))
    monkeypatch.setattr(poison, "system_name", lambda: "linux")
    monkeypatch.setattr(poison, "apply_linux", lambda run, audio=True: {"system": "linux", "font": str(tmp_path / "f.ttf"), "module": "",
                                                                         "env": {"TZ": "America/Lima"}, "before_env": {"TZ": "UTC"}})
    monkeypatch.setattr(poison, "verify_linux", lambda *a, **k: [])
    monkeypatch.setattr(poison, "undo_linux", lambda state, run: None)
    assert poison.apply(state_file, audio=False) == 0
    written = target.read_text(encoding="utf-8")
    assert "TZ=America/Lima\n" in written and "AB_ORACLE_POISON=1\n" in written
    assert "AB_ORACLE_POISON_STATE=%s\n" % state_file.resolve() in written
    assert json.loads(state_file.read_text(encoding="utf-8"))["system"] == "linux"
    target.write_text("", encoding="utf-8")
    assert poison.undo(state_file) == 0
    back = target.read_text(encoding="utf-8")
    assert "TZ=UTC\n" in back and "AB_ORACLE_POISON=\n" in back


def test_a_poison_that_is_not_in_force_after_apply_is_exit_2_and_tells_nothing_to_the_later_steps(tmp_path, monkeypatch, capsys):
    target = tmp_path / "github_env"
    monkeypatch.setenv("GITHUB_ENV", str(target))
    monkeypatch.setattr(poison, "system_name", lambda: "linux")
    monkeypatch.setattr(poison, "apply_linux", lambda run, audio=True: {"system": "linux", "font": "f", "module": "", "env": {}, "before_env": {}})
    monkeypatch.setattr(poison, "verify_linux", lambda *a, **k: ["the null sink is not the default sink"])
    assert poison.apply(None) == 2
    assert "POISON NOT IN FORCE: the null sink" in capsys.readouterr().out
    assert not target.exists()


def test_verify_here_reads_the_state_the_apply_wrote(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(poison, "system_name", lambda: "linux")
    monkeypatch.setattr(poison, "verify_linux", lambda run, state, expect_env, audio: seen.update(state=state, env=expect_env, audio=audio) or [])
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"font": "x", "audio": True}), encoding="utf-8")
    monkeypatch.setenv(poison.STATE_SWITCH, str(state_file))
    assert poison.verify_here(expect_env=True) == []
    assert seen == {"state": {"font": "x", "audio": True}, "env": True, "audio": True}


# -
# The words and paths a test or a message of the repository must not hold.

def test_the_new_files_name_no_machine_path_and_no_forbidden_word():
    banned = ["C:" + "\\Users", "/home/" + "runner", "camou" + "fox"]
    for path in [ROOT / "scripts" / "ci_poison_host.py", Path(__file__)] + sorted((ROOT / "scripts" / "host_gate" / "ab").iterdir()):
        text = path.read_text(encoding="utf-8")
        for word in banned:
            assert word not in text, "%s holds %r" % (path.name, word)
        assert "\r" not in text, "%s is not LF" % path.name


# -
# The user's fontconfig file of the Linux poison.

def test_the_fonts_conf_makes_the_canary_the_first_choice_of_the_generic_families_and_sets_unusual_rendering():
    conf = poison.fonts_conf()
    for generic in ("sans-serif", "serif", "monospace"):
        assert '<alias binding="strong"><family>%s</family><prefer><family>ZZHostCanary</family></prefer></alias>' % generic in conf
    for edit in ('name="antialias" mode="assign"><bool>false', 'name="hinting" mode="assign"><bool>true', "<const>hintfull</const>",
                 "<const>rgb</const>", "<const>lcdlight</const>"):
        assert edit in conf
    assert conf.startswith('<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n<fontconfig>\n') and conf.endswith("</fontconfig>\n")
    import xml.etree.ElementTree as ET

    assert ET.fromstring(conf.split("\n", 2)[2]).tag == "fontconfig"


def test_linux_apply_writes_the_fonts_conf_and_undo_removes_it_when_there_was_none(tmp_path):
    fake = Fake({"locale -a": "ja_JP.utf8"})
    state = poison.apply_linux(fake, font_dir=tmp_path / "fonts", audio=False)
    conf = Path(state["fonts_conf"]["path"])
    assert conf == poison.fonts_conf_path() and conf.read_text(encoding="utf-8") == poison.fonts_conf() and state["fonts_conf"]["before"] is None
    poison.undo_linux(state, fake)
    assert not conf.exists()


def test_a_fonts_conf_that_was_there_comes_back_on_undo(tmp_path):
    conf = poison.fonts_conf_path()
    conf.parent.mkdir(parents=True)
    conf.write_text("<fontconfig/>\n", encoding="utf-8")
    fake = Fake({"locale -a": "ja_JP.utf8"})
    state = poison.apply_linux(fake, font_dir=tmp_path / "fonts", audio=False)
    assert state["fonts_conf"]["before"] == "<fontconfig/>\n" and conf.read_text(encoding="utf-8") == poison.fonts_conf()
    poison.undo_linux(state, fake)
    assert conf.read_text(encoding="utf-8") == "<fontconfig/>\n"


def test_linux_verify_names_a_fonts_conf_that_is_not_the_poison_s_and_a_generic_family_that_is_not_the_canary(tmp_path):
    font = tmp_path / poison.FONT_FILE
    font.write_bytes(poison.canary_font())
    state = dict({"font": str(font)}, **put_conf(tmp_path))
    answers = {"locale -a": "ja_JP.utf8", "fc-list": "ZZHostCanary", "fc-match": "DejaVu Sans"}
    problems = poison.verify_linux(Fake(answers), state, audio=False)
    assert len([p for p in problems if "not the canary" in p]) == 3 and not any("fonts.conf" in p for p in problems)
    Path(state["fonts_conf"]["path"]).write_text("<fontconfig/>", encoding="utf-8")
    assert any("fonts.conf is not the poison" in p for p in poison.verify_linux(Fake(dict(answers, **{"fc-match": "ZZHostCanary"})), state, audio=False))


# -
# The by-product: what the published engine reads from the host.

def packed(*visits):
    return {"format": "ab-oracle/1", "visits": [{"dpr": dpr, "seed": seed, "items": items} for dpr, seed, items in visits]}


def test_every_leaf_that_differs_between_the_clean_and_the_poisoned_dump_is_a_host_dependence():
    clean = packed((1.0, 0, {"surface.intl": {"timeZone": "Asia/Bangkok", "x": 1}, "text.rect": [1, 2], "diag.webgl": {"a": 1}}),
                   (1.0, 2, {"surface.intl": {"timeZone": "Asia/Bangkok", "x": 1}}))
    poisoned = packed((1.0, 0, {"surface.intl": {"timeZone": "America/Lima", "x": 1}, "text.rect": [1, 3], "diag.webgl": {"a": 2}}))
    report = poison.host_dependence(clean, poisoned)
    assert [(d["seed"], d["leaf"], d["clean"], d["poisoned"]) for d in report["differences"]] == [
        (0, "surface.intl.timeZone", "Asia/Bangkok", "America/Lima"), (0, "text.rect", [1, 2], [1, 3])]
    assert report["visits"] == ["seed 0 dpr 1.0"] and report["missing"] == ["seed 2 dpr 1.0"] and report["leaves_compared"] == 3


def test_a_leaf_one_side_lacks_is_missing_not_equal_and_long_values_are_clipped():
    report = poison.host_dependence(packed((1.0, 0, {"a": "x" * 1000})), packed((1.0, 0, {"b": 1})))
    assert {d["leaf"] for d in report["differences"]} == {"a", "b"}
    assert report["differences"][0]["clean"]["len"] > poison.CLIP and report["differences"][1]["clean"] == "<missing>"


def test_two_equal_dumps_have_no_dependence_and_the_summary_says_so():
    report = poison.host_dependence(packed((1.0, 0, {"a": 1})), packed((1.0, 0, {"a": 1})))
    assert report["differences"] == []
    assert "0 of 1 leaves differ" in poison.dependence_markdown(report)


def test_the_summary_groups_by_family_and_is_a_finding_not_a_failure():
    report = poison.host_dependence(packed((1.0, 0, {"surface.intl": {"a": 1, "b": 1}, "text.x": 1})),
                                    packed((1.0, 0, {"surface.intl": {"a": 2, "b": 2}, "text.x": 2})))
    text = poison.dependence_markdown(report)
    assert "| surface.intl | 2 |" in text and "| text.x | 1 |" in text and "not a failure of the candidate" in text


def test_without_a_clean_dump_the_summary_says_it_and_no_file_is_written(tmp_path):
    (tmp_path / "dump-68.json").write_text(json.dumps(packed((1.0, 0, {"a": 1}))), encoding="utf-8")
    summary = tmp_path / "summary.md"
    assert poison.write_dependence(tmp_path, summary) is None
    assert "No dump of the published engine on the clean runner" in summary.read_text(encoding="utf-8")
    assert not (tmp_path / poison.DEPENDENCE_FILE).exists()


def test_the_dependence_is_written_to_its_own_file_and_to_the_summary(tmp_path):
    (tmp_path / poison.CLEAN_FILE).write_text(json.dumps(packed((1.0, 0, {"a": 1}))), encoding="utf-8")
    (tmp_path / "dump-68.json").write_text(json.dumps(packed((1.0, 0, {"a": 2}))), encoding="utf-8")
    summary = tmp_path / "summary.md"
    report = poison.write_dependence(tmp_path, summary)
    assert json.loads((tmp_path / poison.DEPENDENCE_FILE).read_text(encoding="utf-8")) == report
    assert report["differences"] == [{"seed": 0, "dpr": 1.0, "leaf": "a", "clean": 1, "poisoned": 2}]
    assert "1 of 1 leaves differ" in summary.read_text(encoding="utf-8")


def test_the_files_of_the_by_product_are_poison_files_which_the_oracle_s_listing_ignores():
    assert poison.CLEAN_FILE.startswith("poison-") and poison.DEPENDENCE_FILE.startswith("poison-")
    import ci_ab_oracle as oracle

    assert oracle.clean_name("firefox-68") == poison.CLEAN_FILE


def test_the_clean_dump_is_the_published_engine_dumped_on_this_machine_and_judges_nothing(tmp_path, monkeypatch):
    import ci_ab_oracle as oracle

    seen = {}
    monkeypatch.setattr(oracle, "fetch_published", lambda tag, work: ("published-binary", work / "seal.json"))
    monkeypatch.setattr(oracle, "_dump_visits", lambda name, exe, seal, leg, visits, work, out, file_name:
                        (seen.update(name=name, exe=exe, visits=visits, file=file_name) or ({}, [])))
    assert oracle.run_clean("linux-x86_64", tmp_path, "firefox-68", [(1.0, 0), (1.5, 63)]) == 0
    assert seen == {"name": "firefox-68-clean", "exe": "published-binary", "visits": [(1.0, 0), (1.5, 63)], "file": "poison-clean-68.json"}
    monkeypatch.setattr(oracle, "fetch_published", lambda tag, work: (_ for _ in ()).throw(RuntimeError("no network")))
    assert oracle.run_clean("linux-x86_64", tmp_path, "firefox-68", [(1.0, 0)]) == 1


def test_the_e2e_writes_the_dependence_before_it_asserts_anything_on_a_poisoned_host():
    source = (ROOT / "tests" / "test_ab_oracle_e2e.py").read_text(encoding="utf-8")
    assert source.index("poison.write_dependence(") < source.index("poison.verify_here(") < source.index("poison.dump_findings(")


# -
# Windows keeps 'ja' for the language list that was set from 'ja-JP': the verify reads what the OS reports, and holds it to what the apply recorded.

def windows_answers(font, **override):
    answers = {"tzutil /g": poison.WINDOWS_ZONE, "(Get-Culture).Name": "ja-JP", "Get-WinUserLanguageList": "ja",
               "AppsUseLightTheme": "0", "SystemUsesLightTheme": "0", "Get-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT": str(font)}
    answers.update(override)
    return Fake(answers)


def test_windows_apply_records_what_the_system_reports_for_the_poison(tmp_path, no_session):
    font = tmp_path / poison.FONT_FILE
    state = poison.apply_windows(windows_answers(font), font_dir=tmp_path)
    assert state["after"]["languages"] == "ja" and state["after"]["culture"] == "ja-JP"
    assert state["after"]["theme"] == {"AppsUseLightTheme": "0", "SystemUsesLightTheme": "0"} and state["after"]["timezone"] == poison.WINDOWS_ZONE


@pytest.mark.parametrize("stored", ["ja", "ja-JP", "ja_JP", "JA"])
def test_a_japanese_language_list_is_in_force_whatever_form_the_system_keeps_it_in(tmp_path, stored):
    font = tmp_path / poison.FONT_FILE
    font.write_bytes(poison.canary_font())
    state = {"font": str(font), "after": {"culture": "ja-JP", "languages": stored}}
    assert poison.verify_windows(windows_answers(font, **{"Get-WinUserLanguageList": stored}), state) == []


@pytest.mark.parametrize("culture, languages", [("en-US", "ja"), ("ja-JP", "en-US"), ("ja-JP", "en-US,ja"), ("it-IT", "it-IT")])
def test_a_list_or_a_culture_that_is_not_japanese_is_not_in_force_whatever_was_recorded(tmp_path, culture, languages):
    font = tmp_path / poison.FONT_FILE
    font.write_bytes(poison.canary_font())
    state = {"font": str(font), "after": {"culture": culture, "languages": languages}}
    problems = poison.verify_windows(windows_answers(font, **{"(Get-Culture).Name": culture, "Get-WinUserLanguageList": languages}), state)
    assert any("not Japanese" in p for p in problems), problems


def test_what_the_system_reports_later_must_be_what_it_reported_at_the_apply(tmp_path):
    font = tmp_path / poison.FONT_FILE
    font.write_bytes(poison.canary_font())
    state = {"font": str(font), "after": {"culture": "ja-JP", "languages": "ja"}}
    problems = poison.verify_windows(windows_answers(font, **{"Get-WinUserLanguageList": "ja-JP"}), state)
    assert len(problems) == 1 and "recorded 'ja'" in problems[0]


def test_a_failed_apply_still_tells_the_later_steps_where_the_state_is_so_that_the_undo_runs(tmp_path, monkeypatch):
    target = tmp_path / "github_env"
    state_file = tmp_path / "state.json"
    monkeypatch.setenv("GITHUB_ENV", str(target))
    monkeypatch.setattr(poison, "system_name", lambda: "windows")
    monkeypatch.setattr(poison, "apply_windows", lambda run: {"system": "windows", "font": "f", "theme": {}})
    monkeypatch.setattr(poison, "verify_windows", lambda run, state: ["the first user language is 'en-US', which is not Japanese"])
    assert poison.apply(state_file) == 2
    written = target.read_text(encoding="utf-8")
    assert "AB_ORACLE_POISON_STATE=%s\n" % state_file.resolve() in written and "AB_ORACLE_POISON=1" not in written


# -
# The control follows the system, and the page and the realm keep what two launches of one engine agree on.

def test_the_control_follows_the_system_scheme_not_playwright_s_light_default():
    source = (ROOT / "scripts" / "ci_poison_host.py").read_text(encoding="utf-8")
    assert 'browser.new_context(color_scheme="no-override")' in source


def test_the_canary_is_present_when_a_text_measures_its_own_width_not_when_it_differs_from_a_generic_family():
    page = (ROOT / "scripts" / "host_gate" / "host_gate_page.html").read_text(encoding="utf-8")
    assert "out.present = out.with.some(w => Math.abs(w - 216) < 1)" in page
    assert poison.CANARY_TEXT_WIDTH == 216.0
    # on Linux the user's fonts.conf makes the generic families the canary: the old test ('differs from the fallback') read false there


def test_the_realm_hashes_a_png_without_its_per_launch_chunk_and_the_site_without_its_port():
    realm = (ROOT / "scripts" / "host_gate" / "ab" / "realm.js").read_text(encoding="utf-8")
    assert "t !== 'deBG'" in realm and "toDataURL: dataUrlHash(c.toDataURL())" in realm and "png: pngHash(" in realm and "textHash" not in realm
    surfaces = (ROOT / "scripts" / "host_gate" / "ab" / "surfaces.html").read_text(encoding="utf-8")
    assert "e.origin.replace(/:\\d+$/, ':<port>')" in surfaces


# -
# In trial: measured and told, not required. Nothing is in trial now (the two causes of run 45026082150 are taken away); the mechanism stays, with the old
# values set by the test.

TRIAL = {"dark": ("linux", "windows"), "canary": ("windows",)}


@pytest.fixture()
def trial(monkeypatch):
    monkeypatch.setattr(poison, "IN_TRIAL", dict(TRIAL))


def test_nothing_is_in_trial_now_and_every_requirement_is_required_on_both_systems():
    assert poison.IN_TRIAL == {}
    assert set(poison.REQUIRED) == {"timezone", "dark", "canary", "audio", "generic", "language"}
    light = dict(STOCK, dark=False)
    unseen = dict(STOCK, canary={"with": [72, 122.6], "without": [72, 122.6], "present": False})
    for system in ("linux", "windows"):
        assert [n for n, _ in poison.control_failures(light, system)] == ["dark"] and poison.control_trials(light, system) == []
        assert [n for n, _ in poison.control_failures(unseen, system)] == ["canary"]


def test_the_scheme_and_the_windows_canary_can_be_put_in_trial_and_everything_else_stays_required(trial):
    assert poison.in_trial("dark", "linux") and poison.in_trial("dark", "windows") and poison.in_trial("canary", "windows")
    assert not poison.in_trial("canary", "linux") and not poison.in_trial("timezone", "windows") and not poison.in_trial("language", "linux")


def test_a_scheme_stock_did_not_read_is_not_a_broken_control_but_is_told(trial):
    light = dict(STOCK, dark=False)
    for system in ("linux", "windows"):
        assert poison.control_failures(light, system) == []
        assert [n for n, _ in poison.control_trials(light, system)] == ["dark"]
    assert poison.control_trials(STOCK, "linux") == [] and poison.control_trials(None, "linux") == []


def test_the_windows_canary_is_in_trial_and_the_linux_one_is_required(trial):
    unseen = dict(STOCK, canary={"with": [72, 122.6], "without": [72, 122.6], "present": False})
    assert poison.control_failures(unseen, "windows") == [] and [n for n, _ in poison.control_trials(unseen, "windows")] == ["canary"]
    assert [n for n, _ in poison.control_failures(unseen, "linux")] == ["canary"] and poison.control_trials(unseen, "linux") == []


def test_what_stays_required_stays_red_on_windows_too(trial):
    bad = dict(STOCK, dark=False, offsets=[-420, -420], intl_locale="en-US", date_full="x", canary={"present": False})
    assert sorted(n for n, _ in poison.control_failures(bad, "windows")) == ["language", "timezone"]


def test_the_control_writes_the_trials_to_its_file_and_to_the_summary(tmp_path, monkeypatch, capsys, trial):
    import types

    launched = {}
    make_playwright_firefox(tmp_path)

    class Page:
        def goto(self, url):
            pass

        def evaluate(self, expression):
            return dict(STOCK, dark=False)

    class Context:
        def new_page(self):
            return Page()

    class Browser:
        def new_context(self, **options):
            return Context()

        def close(self):
            pass

    class Firefox:
        executable_path = firefox_path(tmp_path)

        def launch(self, **options):
            launched.update(options)
            return Browser()

    class Manager:
        firefox = Firefox()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: Manager()
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(poison, "system_name", lambda: "linux")
    assert poison.control(tmp_path / "out" / "poison-control.json") == 0
    report = json.loads((tmp_path / "out" / "poison-control.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "green" and report["failures"] == []
    assert [f["requirement"] for f in report["in_trial"]["not_read"]] == ["dark"]
    assert report["in_trial"]["requirements"]["dark"] == ["linux", "windows"]
    text = summary.read_text(encoding="utf-8")
    assert "Verdict: **green**" in text and "not read, in trial (dark)" in text
    assert "IN TRIAL, not required (dark)" in capsys.readouterr().out
    assert launched["executable_path"] != firefox_path(tmp_path) and Path(launched["executable_path"]).name == "firefox"
    assert report["reference"]["taken_out_of_playwright_cfg"] == ['pref("ui.systemUsesDarkTheme", 0);']


# -
# The reference is a Playwright Firefox that reads the machine's scheme: its cfg forces ui.systemUsesDarkTheme to 0 (widget/nsXPLookAndFeel.cpp reads a pref before the system).

CFG = """// A neighbour
// Use light theme by default.
pref("ui.systemUsesDarkTheme", 0);

// Do not use system colors - they are affected by themes.
pref("ui.use_standins_for_native_colors", true);
pref("toolkit.shutdown.fastShutdownStage", 3);
"""


def firefox_path(tmp_path):
    return str(tmp_path / "pw" / "firefox" / "firefox")


def make_playwright_firefox(tmp_path, cfg=CFG):
    folder = tmp_path / "pw" / "firefox"
    (folder / "defaults").mkdir(parents=True, exist_ok=True)
    (folder / "firefox").write_bytes(b"binary")
    (folder / "playwright.cfg").write_bytes(cfg.encode("utf-8"))
    (folder / "defaults" / "kept.js").write_bytes(b"kept")
    return folder


def test_the_reference_is_a_copy_without_the_pref_that_forces_the_scheme_and_with_everything_else(tmp_path):
    folder = make_playwright_firefox(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    exe, removed = poison.reference_firefox(str(folder / "firefox"), work)
    copy = Path(exe).parent
    assert copy != folder and Path(exe).name == "firefox" and Path(exe).read_bytes() == b"binary" and (copy / "defaults" / "kept.js").read_bytes() == b"kept"
    cfg = (copy / "playwright.cfg").read_text(encoding="utf-8")
    assert removed == ['pref("ui.systemUsesDarkTheme", 0);'] and "systemUsesDarkTheme" not in cfg
    assert 'pref("ui.use_standins_for_native_colors", true);' in cfg and 'pref("toolkit.shutdown.fastShutdownStage", 3);' in cfg
    assert "systemUsesDarkTheme" in (folder / "playwright.cfg").read_text(encoding="utf-8"), "Playwright's own install is not touched"


@pytest.mark.parametrize("line", ['lockPref("ui.systemUsesDarkTheme", 0);', '  defaultPref("ui.systemUsesDarkTheme", 1);'])
def test_the_pref_is_taken_out_whatever_way_the_cfg_sets_it(tmp_path, line):
    folder = make_playwright_firefox(tmp_path, CFG + line + "\n")
    work = tmp_path / "work"
    work.mkdir()
    exe, removed = poison.reference_firefox(str(folder / "firefox"), work)
    assert len(removed) == 2 and "systemUsesDarkTheme" not in (Path(exe).parent / "playwright.cfg").read_text(encoding="utf-8")


def test_a_reference_that_would_not_differ_from_playwright_s_firefox_is_refused(tmp_path):
    folder = make_playwright_firefox(tmp_path, 'pref("toolkit.shutdown.fastShutdownStage", 3);\n')
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(RuntimeError, match="does not force ui.systemUsesDarkTheme"):
        poison.reference_firefox(str(folder / "firefox"), work)
    (folder / "playwright.cfg").unlink()
    with pytest.raises(RuntimeError, match="no playwright.cfg"):
        poison.reference_firefox(str(folder / "firefox"), tmp_path / "other")


def test_the_control_launches_the_reference_and_not_playwright_s_install():
    source = (ROOT / "scripts" / "ci_poison_host.py").read_text(encoding="utf-8")
    assert "p.firefox.launch(executable_path=executable," in source and "reference_firefox(p.firefox.executable_path" in source
    # the control is headed, and so are the engines of the oracle (a hidden desktop, the core's _headless.py), never `-headless`
    assert "headless=headless" in source and "def control(out: Path, headless: bool = False)" in source


# -
# The canary of Windows is machine-wide: the Fonts folder of the system and HKLM.

def test_the_windows_canary_goes_to_the_fonts_folder_of_the_system_and_to_hklm():
    assert poison.FONTS_KEY.startswith("HKLM:")
    assert poison.font_path_windows({"SystemRoot": "X:" + chr(92) + "Sys"}) == Path("X:" + chr(92) + "Sys") / "Fonts" / poison.FONT_FILE
    assert poison.font_path_windows({"WINDIR": "X:" + chr(92) + "Win"}) == Path("X:" + chr(92) + "Win") / "Fonts" / poison.FONT_FILE


def test_windows_apply_registers_the_canary_under_hklm_and_undo_removes_that_value(tmp_path, no_session):
    fake = Fake()
    state = poison.apply_windows(fake, font_dir=tmp_path)
    assert any("HKLM:" in line and poison.FONT_VALUE in line and "New-ItemProperty" in line for line in fake.text())
    assert not any("HKCU:" in line and poison.FONT_VALUE in line for line in fake.text())
    fake.calls.clear()
    poison.undo_windows(state, fake)
    assert any("Remove-ItemProperty" in line and "HKLM:" in line and poison.FONT_VALUE in line for line in fake.text())
    assert not (tmp_path / poison.FONT_FILE).exists()
