"""The families a page can detect are those of the bundle and nothing else; no font of the host is in the list (e2e, every OS, firefox-68).

What the engine fixes. A page learns which fonts a machine has by asking for a family by name and seeing whether the text changes: the width of a
string through ``"<family>", <generic>`` against the width through the generic alone. The font list of the engine is the manifest the core hands it
(``zoom.stealth.fonts.manifest``, the text of ``bundle-fonts.list``: the families of the Windows fonts the package ships, plus the Windows font
substitutes) and NOTHING of the host: DirectWrite, fontconfig and the registry substitutes are never enumerated
(``gfxPlatformFontList::mStealthBundleOnly``, always true on firefox-68; folder ``fingerprint/fonts/font-list`` of the series, with
``fingerprint/fonts/bundle`` for the files). So the detectable set is the same on every machine, and it is not the machine's.

What the test reads. One page probes a list of names with the width method (three generics as baselines: a family is detected when the text is a
different width through it than through ANY of the generics) and returns the detected ones:

  * every family of the bundle manifest and every Windows font-substitute name of it (``A|`` rows): the names the engine may answer to;
  * every family name of the host that the bundle does not know: read from the font files of this machine (``tests/_font_names.py``: the
    ``name`` table of each file of the system's font folders, minus every name of the manifest and of the bundle's own files). Nothing is typed:
    on Linux these are DejaVu, Liberation, Noto and whatever the image holds, on Windows the fonts of the runner that the bundle does not carry.

The assertions: the detected names are all names of the bundle (no host family is among them); the host has such families at all (a host with
none would make the test say nothing, so it fails); twenty families of the bundle every Windows has (Arial, Times New Roman, Consolas, Segoe UI,
...; the test first checks that the manifest lists them) ARE detected, which is the proof that the probe can detect an installed font.

The negative control, in the same test. The same page runs again with the engine not declared (``zoom.stealth.fpp.hw_seed`` = 0, the pref the core
uses to tell the engine to stay stock). On firefox-68 bundle-only does not depend on the engine being declared (the member is set unconditionally),
so the control reads the same set: the test asserts that the two detected sets are equal, and that no host family is detected with either. The day
the engine gives the host's fonts to a page when it is not declared, or the detectable set moves with the declaration, this test is red.

Not covered here: a family that has no Latin glyph (the emoji, symbol and icon fonts) cannot be told by the width of a Latin string; the assertions
are about the host names being absent and the standard Latin families present, not about the whole bundle being detected.

Run: pytest tests/test_the_font_list_is_the_bundle_e2e.py -m e2e -v
"""
from __future__ import annotations

import pytest

import _font_names as fn

pytestmark = pytest.mark.e2e

#: Families every retail Windows has, with Latin glyphs: the test checks that the manifest lists each of them.
CORE = ["Arial", "Calibri", "Cambria", "Candara", "Comic Sans MS", "Consolas", "Constantia", "Corbel", "Courier New", "Georgia", "Impact",
        "Lucida Console", "Lucida Sans Unicode", "Microsoft Sans Serif", "Palatino Linotype", "Segoe UI", "Tahoma", "Times New Roman",
        "Trebuchet MS", "Verdana"]

TEXT = "mmmmmmmmmmlliWWW0Oq@ Hamburgefonstiv 0123456789"

#: The page: which of the names change the width of TEXT against each of three generics.
PROBE = """(args) => {
  const [names, text] = args;
  const span = document.createElement('span');
  span.style.cssText = 'position:absolute;left:-9999px;top:0;white-space:pre;font-size:48px;font-style:normal;font-weight:400;';
  span.textContent = text;
  document.body.appendChild(span);
  const width = (family) => { span.style.fontFamily = family; return span.getBoundingClientRect().width; };
  const generics = ['monospace', 'sans-serif', 'serif'];
  const base = generics.map(width);
  const detected = names.filter(name => generics.some((g, i) => width('"' + name + '", ' + g) !== base[i]));
  span.remove();
  return detected;
}"""


def _detected(firefox_binary, names, engine_declared):
    from invisible_playwright import InvisiblePlaywright

    prefs = {} if engine_declared else {"zoom.stealth.fpp.hw_seed": 0}
    with InvisiblePlaywright(seed=21, binary_path=firefox_binary, headless=True, extra_prefs=prefs) as browser:
        page = browser.new_context().new_page()
        page.goto("about:blank", timeout=30_000)
        return set(page.evaluate(PROBE, [names, TEXT]))


def test_the_font_list_is_the_bundle_and_nothing_else(firefox_binary):
    manifest = fn.bundle_manifest()
    missing = [family for family in CORE if family not in manifest.families]
    assert not missing, "the manifest of the core does not list %r: the families this test expects the bundle to hold are not its families" % missing

    known = fn.bundle_file_names(manifest, firefox_binary)
    host_only = fn.host_only_families(manifest, extra_known=known)
    assert host_only, ("the host has no font family the bundle does not know: there is no name here that only the host can answer to, "
                       "so the test could not tell a list of the host's fonts from the bundle")

    allowed = set(manifest.families) | set(manifest.aliases)
    names = list(manifest.families) + sorted(manifest.aliases) + host_only

    for declared in (True, False):
        mode = "engine declared" if declared else "engine not declared (hw_seed 0)"
        detected = _detected(firefox_binary, names, declared)
        host_seen = sorted(detected & set(host_only))
        assert not host_seen, (
            "%s: the page detects %d family name(s) of the host that the bundle does not have: %r (of %d host-only names probed). The font list is "
            "not the bundle" % (mode, len(host_seen), host_seen, len(host_only)))
        outside = sorted(detected - allowed)
        assert not outside, "%s: the page detects %r, which are neither a family nor a substitute name of the bundle" % (mode, outside)
        not_found = [family for family in CORE if family not in detected]
        assert not not_found, (
            "%s: the page does not detect %r, standard Windows families of the bundle: the probe cannot see an installed font here (detected "
            "%d of %d bundle names)" % (mode, not_found, len(detected), len(manifest.families) + len(manifest.aliases)))
        if declared:
            with_engine = detected
        else:
            assert detected == with_engine, (
                "the detectable set differs between the engine declared and not declared: only with the engine %r, only without %r"
                % (sorted(with_engine - detected), sorted(detected - with_engine)))
