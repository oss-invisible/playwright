"""``@font-face { src: local(<name>) }`` finds the faces of the bundle by their PostScript name, and never a font that only the host has (e2e, every OS, firefox-68).

What the engine fixes. A page can ask for an installed font by name with ``src: local("ArialMT")`` (the PostScript name or the full name of a face)
and learn from ``FontFace.status`` whether the browser has it. The font list of the engine is the bundle and nothing else
(``gfxPlatformFontList::mStealthBundleOnly``, set in the constructor, on every platform and whether or not the engine is declared): the face
names the ``local()`` lookup searches are read from the ``name`` tables of the bundle's files (``RecordFaceNamesFromTable``,
``ReadFaceNamesForFamily``; folders ``fingerprint/fonts/font-list`` and ``fingerprint/fonts/bundle`` of the series), never from DirectWrite
or fontconfig. So the PostScript name of a bundled face resolves, a name that only the host's fonts carry does not, and the answer is the same
on every machine: a page that probes ``local()`` learns the bundle, not the machine.

What the test reads. One page declares, for each name, ``@font-face { font-family: <probe>; src: local("<name>") }`` and loads it
(``document.fonts.load``), then reads the ``status`` of that face (``loaded`` or ``error``, an event of the page: no timer) and the width of one
string through the probe family. Four PostScript names of the bundle come from the manifest the core hands to the engine (``ArialMT``,
``TimesNewRomanPSMT``, and the faces ``MS-PGothic`` and ``NSimSun``, which are NOT the first face of their ``.ttc`` file): each must be ``loaded``
and draw with the same width as the family it belongs to (the same face, not another one that happened to load). The host-only face is read from
the host's own font files (``tests/_font_names.py``: the ``name`` table of every file of the folders the system installs fonts in, minus every
name the bundle knows): its PostScript name AND its full name must be ``error``. On Linux the face asked for is DejaVu Sans when the host has
it, on Windows the first font of ``%WINDIR%\\Fonts`` the bundle does not carry; no value is typed in the test, and a host that has no such font
fails the test (there would be nothing the control could find).

The negative control, in the same test. The names the host has are the ones that must NOT resolve, so the same page runs a second time with the
engine not declared (``zoom.stealth.fpp.hw_seed`` = 0, the pref the core uses to tell the engine to stay stock). On firefox-68 bundle-only
does NOT depend on the engine being declared (the member is set unconditionally in the constructor), so the control reads the same: the bundled
names resolve and the host-only names do not, engine on or off. The test asserts that equality; it is the characterization of what 68 does, and
the day the engine changes it (the host fonts reaching ``local()`` with the engine off) this test is red. A name that exists nowhere
(``NoSuchFace-ZZ``) is read in both runs as the control of the probe itself: it is ``error``, so the probe can tell a missing face from a
loaded one.

Why a page reads it: ``FontFace.status`` after ``document.fonts.load()`` is plain web API; fingerprinting scripts probe installed fonts this
way as well as by width.

Run: pytest tests/test_bundle_font_names_e2e.py -m e2e -v
"""
from __future__ import annotations

import sys

import pytest

import _font_names as fn

pytestmark = pytest.mark.e2e

#: PostScript name -> the family it belongs to in the bundle manifest. The last two are faces of a collection that are not face 0 of their file.
BUNDLED = [("ArialMT", "Arial"), ("TimesNewRomanPSMT", "Times New Roman"), ("MS-PGothic", "MS PGothic"), ("NSimSun", "NSimSun")]

#: Declared by no machine: the control of the probe.
NOWHERE = "NoSuchFace-ZZ"

TEXT = "mmmmmmmmmmlliWWW0Oq@"

#: The page. `cases` is [[local name, family it must draw like or null], ...]. It reports, for each: the status of the face once its load settled
#: and the widths of TEXT through the probe family, through the target family, and through the generics (the width of a face that is not there).
PROBE = """async (args) => {
  const [cases, text] = args;
  const span = document.createElement('span');
  span.style.cssText = 'position:absolute;left:-9999px;top:0;white-space:pre;font-size:48px;font-style:normal;font-weight:400;';
  span.textContent = text;
  document.body.appendChild(span);
  const width = (family) => { span.style.fontFamily = family; return span.getBoundingClientRect().width; };
  const generics = ['monospace', 'sans-serif', 'serif'].map(width);
  const out = [];
  let n = 0;
  for (const [name, target] of cases) {
    const probe = 'LocalProbe' + (n++);
    const style = document.createElement('style');
    style.textContent = '@font-face { font-family: ' + probe + '; src: local("' + name + '"); }';
    document.head.appendChild(style);
    await document.fonts.load('48px ' + probe).catch(() => null);
    const face = Array.from(document.fonts).find(f => f.family.replace(/["']/g, '') === probe);
    out.push({
      name: name, status: face ? face.status : 'no-face',
      probe: width(probe + ', monospace'),
      target: target ? width('"' + target + '", monospace') : null,
      generics: generics,
    });
  }
  span.remove();
  return out;
}"""


def _read(firefox_binary, cases, engine_declared):
    from invisible_playwright import InvisiblePlaywright

    prefs = {} if engine_declared else {"zoom.stealth.fpp.hw_seed": 0}
    with InvisiblePlaywright(seed=21, binary_path=firefox_binary, headless=True, extra_prefs=prefs) as browser:
        page = browser.new_context().new_page()
        page.goto("about:blank", timeout=30_000)
        return page.evaluate(PROBE, [cases, TEXT])


def test_src_local_resolves_a_bundled_postscript_name_and_not_a_host_only_name(firefox_binary):
    manifest = fn.bundle_manifest()
    for psname, family in BUNDLED:
        assert manifest.family_of_psname(psname) == family, (
            "the manifest of the core has no face %r in the family %r: the names this test asks for are not the bundle's" % (psname, family))

    known = fn.bundle_file_names(manifest, firefox_binary)
    prefer = ["DejaVu Sans"] if sys.platform != "win32" else []
    host = fn.a_host_only_face(manifest, prefer=prefer, extra_known=known)

    cases = [[psname, family] for psname, family in BUNDLED]
    cases += [[host["psname"], None], [host["full"], None], [NOWHERE, None]]
    host_names = (host["psname"], host["full"])

    runs = {declared: {row["name"]: row for row in _read(firefox_binary, cases, declared)} for declared in (True, False)}

    for declared, rows in runs.items():
        mode = "engine declared" if declared else "engine not declared (hw_seed 0)"
        # the probe tells a missing face from a loaded one
        assert rows[NOWHERE]["status"] == "error", "%s: a name that exists nowhere read as %r, not error: the probe cannot tell a missing face" % (mode, rows[NOWHERE])
        # the bundle: found, and it is the face of its family
        for psname, family in BUNDLED:
            row = rows[psname]
            assert row["status"] == "loaded", "%s: src:local(%s) is %r, not loaded: a PostScript name of the bundle does not resolve (%r)" % (mode, psname, row["status"], row)
            assert row["probe"] == row["target"], (
                "%s: src:local(%s) draws %r wide and the family %s %r wide: it loaded another face than the bundle's" % (mode, psname, row["probe"], family, row["target"]))
            assert any(row["probe"] != g for g in row["generics"]), (
                "%s: src:local(%s) draws as a generic (%r): the face drew nothing of its own" % (mode, psname, row))
        # the host: the file is on this machine's disk, and the engine does not know its names
        for name in host_names:
            row = rows[name]
            assert row["status"] == "error", (
                "%s: src:local(%s) is %r: the name belongs to the host font %s (%s) and the bundle has no such face, so a page learns the machine "
                "from it" % (mode, name, row["status"], host["file"], host["families"]))

    # on 68 the bundle-only list does not depend on the engine being declared: the same answers with it off
    on = {name: runs[True][name]["status"] for name in runs[True]}
    off = {name: runs[False][name]["status"] for name in runs[False]}
    assert on == off, "the statuses of src:local() differ between the engine declared (%r) and not declared (%r)" % (on, off)
