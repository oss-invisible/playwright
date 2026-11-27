"""tests/vendor/README.md lists every vendored bundle with the sha256 of its bytes, and a bundle changed without its row is red.

The bundles are third-party code kept byte-identical to upstream; the README is where their version, licence and notice live. A bundle
swapped, edited or added without touching that table would leave the provenance claiming something the file no longer is.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

VENDOR = Path(__file__).resolve().parent / "vendor"
ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|.*\|\s*`([0-9a-f]{64})`\s*\|", re.MULTILINE)


def problems(directory: Path) -> list:
    """What differs between the files of `directory` and the rows of its README.md, one line each."""
    rows = {name: digest for name, digest in ROW.findall((directory / "README.md").read_text(encoding="utf-8"))}
    files = {p.name: p for p in directory.iterdir() if p.is_file() and p.name != "README.md"}
    out = ["%s has no row" % n for n in sorted(files) if n not in rows]
    out += ["%s has a row and no file" % n for n in sorted(rows) if n not in files]
    out += ["%s differs from its sha256" % n for n in sorted(rows)
            if n in files and hashlib.sha256(files[n].read_bytes()).hexdigest() != rows[n]]
    return out


def test_every_vendored_bundle_has_a_row_with_the_sha256_of_its_bytes():
    assert problems(VENDOR) == []


def test_a_bundle_changed_without_its_row_is_red(tmp_path):
    (tmp_path / "a.js").write_bytes(b"one")
    (tmp_path / "README.md").write_text("| `a.js` | kind | 1 | MIT | `%s` | n |\n" % hashlib.sha256(b"one").hexdigest(), encoding="utf-8")
    assert problems(tmp_path) == []
    (tmp_path / "a.js").write_bytes(b"two")
    assert problems(tmp_path) == ["a.js differs from its sha256"]


def test_a_bundle_with_no_row_and_a_row_with_no_bundle_are_red(tmp_path):
    (tmp_path / "a.js").write_bytes(b"one")
    (tmp_path / "README.md").write_text("| `b.js` | kind | 1 | MIT | `%s` | n |\n" % ("0" * 64), encoding="utf-8")
    assert problems(tmp_path) == ["a.js has no row", "b.js has a row and no file"]
