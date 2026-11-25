"""The lock of the engine series' gate (ci/py-lock/gate.txt) says what the sources say, and is made from the input that says it (ci-03).

The gate installs the wrapper's environment offline, from a wheelhouse made of this lock: a dependency the lock does not hold is red there, by name, and a
dependency the sources dropped but the lock still holds is a package nobody asked for. This test closes the circle without the network:

* ``ci/py-lock/gate.in`` is exactly what the sources declare: the dependencies of pyproject.toml (without ``invisible_core``, which is a wheel of ours) and its
  ``dev`` extra, the dependencies of the core the pin names (recorded in the ``core-requires`` header, and compared with the installed core when the installed
  one is the pinned version), playwright at scripts/playwright_pin.txt, cryptography 44.0.3; and its ``core:`` header names the version the pin names;
* ``ci/py-lock/gate.txt`` names the sha256 of that ``gate.in`` in its header, was resolved with the same Python floor and date, and every line is
  ``name==version`` (a marker allowed) with at least one ``--hash=sha256:``: no range, no URL, no entry without a hash;
* every direct requirement is locked, at a version its specifier accepts, and playwright and cryptography at exactly their pins.

Known-bad inputs: a dependency missing from the input, a dependency the sources do not have, a core pin that moved without the lock, a playwright pin that
moved, a lock made from another input, a range or an entry with no hash in the lock, a direct requirement the lock does not hold or holds at another version.
The green case is the state of the repository. One command regenerates the files: ``python scripts/ci_py_lock.py --core-pyproject <core pyproject at the tag>``.
"""
from __future__ import annotations

import importlib.metadata
import re
import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import ci_py_lock as lock  # noqa: E402

pytestmark = pytest.mark.unit


def _sources() -> dict:
    return {
        "in_text": lock.IN_PATH.read_text(encoding="utf-8"),
        "txt_text": lock.TXT_PATH.read_text(encoding="utf-8"),
        "pyproject": lock.PYPROJECT.read_text(encoding="utf-8"),
        "playwright_pin": lock.PLAYWRIGHT_PIN.read_text(encoding="utf-8").strip(),
    }


def _installed_core():
    """(version, runtime requirements) of the installed core, or None when none is installed."""
    try:
        version = importlib.metadata.version("invisible_core")
    except importlib.metadata.PackageNotFoundError:
        return None
    requires = [r for r in (importlib.metadata.requires("invisible_core") or []) if "extra ==" not in r]
    return version, requires


def problems(in_text: str, txt_text: str, pyproject: str, playwright_pin: str, installed_core=None) -> list:
    out = []
    parsed = lock.read_in(in_text)
    meta, have = parsed["meta"], parsed["requirements"]
    src = lock.wrapper_sources(pyproject)

    # the core pin: the header names the version the pin names
    if "invisible_core==%s," % src["core_version"] not in meta.get("core", ""):
        out.append("gate.in `core:` header is %r but pyproject.toml pins invisible_core==%s: regenerate with scripts/ci_py_lock.py" % (meta.get("core"), src["core_version"]))
    core_requires = [r for r in meta.get("core-requires", "").split(lock.SEP) if r]
    if not core_requires:
        out.append("gate.in has no `core-requires:` header: the dependencies of the core are not recorded")
    if installed_core is not None and installed_core[0] == src["core_version"]:
        if sorted(core_requires) != sorted(installed_core[1]):
            out.append("gate.in `core-requires:` is %s but the core %s declares %s" % (sorted(core_requires), src["core_version"], sorted(installed_core[1])))

    # the input is what the sources say
    want = lock.expected_requirements(pyproject, core_requires, playwright_pin)
    if sorted(have) != sorted(want):
        out.append("gate.in is not what the sources say: missing %s, not in the sources %s" % (sorted(set(want) - set(have)), sorted(set(have) - set(want))))
    if meta.get("playwright", "").split(" ")[0] != playwright_pin:
        out.append("gate.in `playwright:` header is %r but scripts/playwright_pin.txt is %r" % (meta.get("playwright"), playwright_pin))

    # the lock is made from that input
    lines = txt_text.replace("\r\n", "\n").splitlines()
    wanted = "# input: ci/py-lock/gate.in sha256 %s" % lock.sha256_text(in_text)
    if wanted not in lines:
        out.append("gate.txt header does not name the sha256 of gate.in (%s): it was not made from the present gate.in" % lock.sha256_text(in_text))
    resolver = next((line for line in lines if line.startswith("# resolver:")), "")
    for key in ("python-version", "exclude-newer"):
        if "--%s %s" % (key, meta.get(key, "?")) not in resolver:
            out.append("gate.txt resolver line %r does not carry %s %s of gate.in" % (resolver, key, meta.get(key)))

    # every line is name==version with a hash
    try:
        entries = lock.parse_lock(txt_text)
    except ValueError as e:
        return out + [str(e)]
    if not entries:
        return out + ["gate.txt holds no entry"]
    by_name = {}
    for name, version, _marker, _hashes in entries:
        by_name.setdefault(lock.norm(name.split("[")[0]), set()).add(version)

    # every direct requirement is locked, at a version it accepts; the two pins exactly
    for text in have:
        req = Requirement(text)
        versions = by_name.get(lock.norm(req.name))
        if not versions:
            out.append("%s is in gate.in and not in gate.txt" % req.name)
        elif not any(req.specifier.contains(Version(v), prereleases=True) for v in versions):
            out.append("gate.txt locks %s at %s, which %r does not accept" % (req.name, sorted(versions), text))
    for pinned in ("playwright==%s" % playwright_pin, lock.CRYPTOGRAPHY):
        name, version = pinned.split("==")
        if by_name.get(name) != {version}:
            out.append("gate.txt must lock %s exactly at %s, it holds %s" % (name, version, sorted(by_name.get(name, []))))
    return out


def test_the_committed_lock_follows_its_sources():
    assert problems(installed_core=_installed_core(), **_sources()) == []


def test_the_input_is_what_the_sources_say_in_every_dependency():
    """The sets, from the sources themselves: a dependency, the dev extra, the core's, the two pins."""
    s = _sources()
    have = set(lock.read_in(s["in_text"])["requirements"])
    src = lock.wrapper_sources(s["pyproject"])
    assert set(src["dependencies"]) <= have and set(src["dev"]) <= have
    assert "playwright==%s" % s["playwright_pin"] in have and lock.CRYPTOGRAPHY in have
    assert not any(lock.base_name(r) == "invisible-core" for r in have), "the core is a wheel of ours, never a locked distribution"


def test_every_entry_of_the_lock_has_a_hash_and_an_exact_version():
    entries = lock.parse_lock(_sources()["txt_text"])
    assert len(entries) >= 30
    for name, version, _marker, hashes in entries:
        assert re.fullmatch(r"[0-9][0-9A-Za-z.!+_-]*", version), (name, version)
        assert hashes and all(re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes), name


# ── known-bad copies: each is red, by name ───────────────────────────────────────────────────────────────────────────────────────────────────

def _bad(**changes):
    s = _sources()
    s.update(changes)
    return problems(installed_core=None, **s)


def _drop_line(text: str, prefix: str) -> str:
    kept = [line for line in text.splitlines() if not line.startswith(prefix)]
    assert len(kept) == len(text.splitlines()) - 1, prefix
    return "\n".join(kept) + "\n"


def test_a_dependency_missing_from_the_input_is_red():
    bad = _bad(in_text=_drop_line(_sources()["in_text"], "psutil"))
    assert any("psutil" in p for p in bad), bad


def test_a_dependency_the_sources_do_not_have_is_red():
    bad = _bad(in_text=_sources()["in_text"] + "left-over-package>=1\n")
    assert any("left-over-package" in p for p in bad), bad


def test_a_core_dependency_missing_from_the_input_is_red():
    bad = _bad(in_text=_drop_line(_sources()["in_text"], "platformdirs"))
    assert any("platformdirs" in p for p in bad), bad


def test_a_core_pin_that_moved_without_the_lock_is_red():
    pyproject = _sources()["pyproject"]
    moved = re.sub(r'"invisible_core==[^"]+"', '"invisible_core==0.0.1"', pyproject, count=1)
    assert moved != pyproject
    bad = _bad(pyproject=moved)
    assert any("invisible_core==0.0.1" in p for p in bad), bad


def test_a_core_that_declares_something_else_than_the_input_records_is_red():
    s = _sources()
    version = lock.wrapper_sources(s["pyproject"])["core_version"]
    bad = problems(installed_core=(version, ["platformdirs>=4", "a-new-dependency>=1"]), **s)
    assert any("a-new-dependency" in p for p in bad), bad


def test_a_playwright_pin_that_moved_without_the_lock_is_red():
    bad = _bad(playwright_pin="1.99.0")
    assert any("1.99.0" in p for p in bad), bad


def test_a_lock_made_from_another_input_is_red():
    bad = _bad(in_text=_sources()["in_text"].replace("# python-version", "# a-changed-input: x\n# python-version", 1))
    assert any("sha256 of gate.in" in p for p in bad), bad


def test_a_range_in_the_lock_is_red():
    txt = _sources()["txt_text"]
    bad = _bad(txt_text=re.sub(r"^psutil==\S+", "psutil>=5.9", txt, count=1, flags=re.M))
    assert any("not `name==version`" in p for p in bad), bad


def test_an_entry_with_no_hash_in_the_lock_is_red():
    txt = _sources()["txt_text"]
    stripped = re.sub(r"^(psutil==\S+) \\\n(?:    --hash=\S+(?: \\)?\n?)+", r"\1\n", txt, count=1, flags=re.M)
    assert stripped != txt
    bad = _bad(txt_text=stripped)
    assert any("psutil" in p and "no --hash" in p for p in bad), bad


def test_a_direct_requirement_the_lock_does_not_hold_is_red():
    txt = _sources()["txt_text"]
    stripped = re.sub(r"^pyee==\S+ \\\n(?:    --hash=\S+(?: \\)?\n)+", "", txt, count=1, flags=re.M)
    assert stripped != txt
    bad = _bad(txt_text=stripped)
    assert any("pyee" in p and "not in gate.txt" in p for p in bad), bad


def test_a_lock_that_holds_another_version_than_a_pin_is_red():
    txt = _sources()["txt_text"]
    other = re.sub(r"^cryptography==44\.0\.3", "cryptography==44.0.2", txt, count=1, flags=re.M)
    assert other != txt
    bad = _bad(txt_text=other)
    assert any("cryptography" in p and "44.0.3" in p for p in bad), bad
