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
        "canary": {"with": [100, 100], "without": [100, 100], "present": False}, "audio": {"sampleRate": 48000, "maxChannelCount": 2}}
STOCK = {"timezone": "America/Bogota", "offsets": [300, 300], "language": "en-US", "languages": ["en-US", "en"], "intl_locale": "en-US",
         "dark": True, "canary_check": True, "canary": {"with": [180, 180], "without": [100, 100], "present": True},
         "audio": {"sampleRate": 96000, "maxChannelCount": 6}}


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
])
def test_a_poison_that_stock_firefox_did_not_read_is_a_broken_control(change, requirement):
    failures = poison.control_failures(dict(STOCK, **change), "linux")
    assert [name for name, _ in failures] == [requirement]


def test_the_audio_sink_is_linux_s_only():
    assert poison.control_failures(dict(STOCK, audio={"sampleRate": 48000, "maxChannelCount": 2}), "windows") == []


def test_a_control_that_returned_nothing_is_broken():
    assert poison.control_failures(None, "linux")[0][0] == "probe"


def test_the_language_is_reported_and_not_required_and_says_whether_stock_read_japanese():
    assert "language" not in poison.REQUIRED
    assert poison.language_reads(STOCK)["japanese"] is False
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
                  "AppsUseLightTheme": "0", "SystemUsesLightTheme": "0", "Get-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows NT": str(font)})
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
    good = Fake({"locale -a": "ja_JP.utf8", "fc-list": "ZZHostCanary", "list short sinks": "1\thostcanary\tmodule-null-sink.c\ts16le 6ch 96000Hz\tIDLE",
                 "get-default-sink": "hostcanary"})
    assert poison.verify_linux(good, state) == []
    bad = Fake({"locale -a": "ja_JP.utf8", "fc-list": "ZZHostCanary", "list short sinks": "1\thostcanary\tmodule-null-sink.c\ts16le 2ch 44100Hz\tIDLE",
                "get-default-sink": "alsa_output"})
    problems = poison.verify_linux(bad, state)
    assert any("6 channels at 96000" in p for p in problems) and any("default sink" in p for p in problems)
    assert any("fontconfig" in p for p in poison.verify_linux(Fake({"locale -a": "ja_JP.utf8"}), state, audio=False))


def test_linux_verify_of_the_environment_is_for_the_process_that_inherited_it(tmp_path):
    font = tmp_path / poison.FONT_FILE
    font.write_bytes(poison.canary_font())
    quiet = Fake({"locale -a": "ja_JP.utf8", "fc-list": "ZZHostCanary"})
    state = {"font": str(font)}
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
