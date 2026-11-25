"""The archives of Playwright's own Firefox that the gate installs are pinned by sha256 for the Playwright the repository pins (ci-03).

``ci/py-lock/playwright-browsers.json`` is what ``scripts/ci_playwright_browsers.py`` writes: for ``scripts/playwright_pin.txt``, the archives that
``playwright install --dry-run firefox`` lists on each platform (Firefox and FFmpeg on both, winldd on win64), each with the sha256 of the real download.
A file that names another Playwright version, lacks a platform or an archive kind, carries a hash that is not 64 hex digits, or names an absolute URL instead
of a relative ``builds/...`` path would let the gate install an archive nobody checked, or none. Offline: the file is read, nothing is downloaded.

Known-bad inputs: another version, a missing platform, a missing kind, a bad hash, an absolute URL, an extra field. The green case is the state of the repository.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import ci_playwright_browsers as pins  # noqa: E402

pytestmark = pytest.mark.unit

PIN = pins.PLAYWRIGHT_PIN.read_text(encoding="utf-8").strip()


def _committed():
    return json.loads(pins.OUT_PATH.read_text(encoding="utf-8"))


def test_the_committed_file_follows_the_pin():
    assert pins.problems(_committed(), PIN) == []


def test_the_committed_file_is_what_the_generator_writes():
    text = pins.OUT_PATH.read_bytes().decode("utf-8")
    assert "\r" not in text and text.endswith("}\n")
    assert text == pins.render(json.loads(text))
    assert list(json.loads(text)["platforms"]) == sorted(pins.PLATFORMS)


def test_the_dry_run_kinds_are_on_both_platforms():
    platforms = _committed()["platforms"]
    for name, kinds in pins.KINDS.items():
        assert sorted(e["path"].split("/")[1] for e in platforms[name]) == sorted(kinds)
    assert "winldd" in pins.KINDS["win64"] and "winldd" not in pins.KINDS["ubuntu24.04-x64"]


def _mutated(change):
    data = copy.deepcopy(_committed())
    change(data)
    return pins.problems(data, PIN)


@pytest.mark.parametrize("name,change,needle", [
    ("another version", lambda d: d.update(playwright="1.0.0"), "playwright is"),
    ("a missing platform", lambda d: d["platforms"].pop("win64"), "platforms are"),
    ("an extra platform", lambda d: d["platforms"].update(osx=[]), "platforms are"),
    ("a missing winldd", lambda d: d["platforms"]["win64"].pop(), "win64: the archives are"),
    ("a missing ffmpeg", lambda d: d["platforms"]["ubuntu24.04-x64"].pop(), "ubuntu24.04-x64: the archives are"),
    ("a short hash", lambda d: d["platforms"]["win64"][0].update(sha256="ab" * 31), "no 64-hex sha256"),
    ("an upper-case hash", lambda d: d["platforms"]["win64"][0].update(sha256="AB" * 32), "no 64-hex sha256"),
    ("no hash", lambda d: d["platforms"]["win64"][0].pop("sha256"), "no 64-hex sha256"),
    ("an absolute URL", lambda d: d["platforms"]["win64"][0].update(path="https://example.org/builds/firefox/1/firefox-win64.zip"), "not a relative"),
    ("a rooted path", lambda d: d["platforms"]["win64"][0].update(path="/builds/firefox/1/firefox-win64.zip"), "not a relative"),
    ("a path that climbs", lambda d: d["platforms"]["win64"][0].update(path="builds/../x/1/firefox-win64.zip"), "not a relative"),
    ("an extra field", lambda d: d["platforms"]["win64"][0].update(url="x"), "unknown fields"),
    ("a size that is not a number", lambda d: d["platforms"]["win64"][0].update(size="big"), "size"),
])
def test_known_bad_copies_are_red(name, change, needle):
    out = _mutated(change)
    assert out and any(needle in line for line in out), (name, out)


def test_not_an_object_is_red():
    assert pins.problems([], PIN)


def test_a_non_canonical_copy_is_not_the_generator_bytes():
    text = pins.OUT_PATH.read_bytes().decode("utf-8")
    data = json.loads(text)
    for bad in (text.replace("\n", "\r\n"), json.dumps(data), json.dumps(data, indent=4) + "\n", text.rstrip("\n")):
        assert bad != pins.render(data)
