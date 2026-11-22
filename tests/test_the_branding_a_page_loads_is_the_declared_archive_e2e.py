"""A page loads the branding from the archive the core declares, with the
content sandbox on.

`chrome://branding/content/` is content-accessible, and what a page reads there
tells a build from the retail one (`resource://usercontext-content/branding/`,
the other place the engine serves the archive, is not readable by a page: it
answers `error` to an image load, so it is not asserted). The core declares
where the files are (`zoom.stealth.branding.archive`, the zip of a set it ships
or one of the caller's own, `pin={"branding": <archive>}`); the engine serves them as `jar:` at the places
that resolve those URIs (netwerk/protocol/res, `fingerprint/js-surface/
branding`) and grants the content processes read access to exactly that file
(Windows: `sandboxBroker.cpp`; Linux: `SandboxBrokerPolicyFactory.cpp`), for the
archive lives wherever the package is installed, outside what the sandbox opens.
So a page that loads the files shows both the redirect (the declared files, not
the build's) and the sandbox grant (the load works at all). Both are read here
as the page reads them: the size of the image a page loads, from a page on
127.0.0.1, on Linux and on Windows.

Negative control: the same page with the declaration pointed at another archive
sees that archive's file, and the dimensions differ from the shipped retail
set's, so a launch that ignored the declaration, or whose content process could
not read it, fails here. (An undeclared archive with the engine on is refused
at launch by the declaration gate, which the core's own tests hold: the browser
does not start to be read.) The archive of the caller's own is the shipped
`official` set with `about-logo.png` replaced by a 7x5 image.
"""
from __future__ import annotations

import struct
import zlib
import zipfile

import pytest

pytestmark = pytest.mark.e2e

#: An image no brand has: the declaration, not the build's file.
_MINE = (7, 5)
_FILE = "about-logo.png"

URLS = {
    "chrome": "chrome://branding/content/" + _FILE,
}

LOAD = """async (urls) => {
  const out = {};
  for (const [way, url] of Object.entries(urls)) {
    out[way] = await new Promise(resolve => {
      const img = new Image();
      img.onload = () => resolve([img.naturalWidth, img.naturalHeight]);
      img.onerror = () => resolve('error');
      img.src = url;
    });
  }
  return out;
}"""


def _png(width, height):
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + b"\xff\x00\x00\xff" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def _size_in(archive):
    with zipfile.ZipFile(archive) as z:
        head = z.read(_FILE)[:24]
    return list(struct.unpack(">II", head[16:24]))


def _own_archive(tmp_path):
    from invisible_core._fpforge.profile import branding_archive

    with zipfile.ZipFile(branding_archive("official")) as z:
        entries = {n: z.read(n) for n in z.namelist()}
    entries[_FILE] = _png(*_MINE)
    path = tmp_path / "mine.zip"
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return path


def _load(firefox_binary, url, pin):
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=42, binary_path=firefox_binary, headless=True, pin=pin) as browser:
        page = browser.new_page()
        page.goto(url)
        return page.evaluate(LOAD, URLS)


def test_the_branding_a_page_loads_is_the_declared_archive(firefox_binary, fixture_server, tmp_path):
    from invisible_core._fpforge.profile import branding_archive

    retail = _size_in(branding_archive("official"))
    assert retail != list(_MINE), retail
    shipped = _load(firefox_binary, fixture_server + "/from-network", None)
    assert shipped == {"chrome": retail}, (shipped, retail)

    # Control: another declared archive is the one a page sees.
    own = _load(firefox_binary, fixture_server + "/from-network", {"branding": str(_own_archive(tmp_path))})
    assert own == {"chrome": list(_MINE)}, own
    assert own != shipped, (own, shipped)
