"""`GPUDevice.prototype.importExternalTexture` exists on every operating system (firefox-68, Linux and Windows).

Hooks of `fingerprint/webgpu/adapter-declaration` (`modules/libpref/init/StaticPrefList.yaml`: `dom.webgpu.external-texture.enabled` is true on every OS;
`gfx/thebes/gfxPlatform.cpp`: the force-disable for a system that is not Windows or macOS is off) and of `fingerprint/webgpu/external-texture-vulkan` (the
Vulkan backend has external textures, so the API is real on Linux): upstream Firefox has the interface on Windows and macOS only, so a page could tell
a Linux engine from a Windows one by `typeof GPUDevice.prototype.importExternalTexture`. The method is `[Func=ExternalTexturePrefEnabled]`: present
when the pref (read once, at start) is true. A page reads it with no permission.

Negative control: the same read with the pref `dom.webgpu.external-texture.enabled` false (the stock value on Linux) has no such method.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

PREF = "dom.webgpu.external-texture.enabled"
PAGE = "/from-network"

READ = """() => ({
  interface: typeof GPUDevice,
  method: typeof GPUDevice === 'undefined' ? 'no GPUDevice' : typeof GPUDevice.prototype.importExternalTexture,
})"""


def read(binary, url, extra_prefs=None):
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=42, binary_path=binary, headless=True, humanize=False, locale="en-US",
                             timezone="America/New_York", extra_prefs=extra_prefs) as browser:
        page = browser.new_page()
        page.goto(url, timeout=60_000)
        return page.evaluate(READ)


def test_importexternaltexture_exists_on_every_os_e2e(firefox_binary, fixture_server):
    url = fixture_server + PAGE
    on = read(firefox_binary, url)
    assert on["interface"] == "function", on
    assert on["method"] == "function", on

    # Control: the pref false takes the method away, so the read above depends on the declaration and not on the page.
    off = read(firefox_binary, url, extra_prefs={PREF: False})
    assert off["interface"] == "function", off
    assert off["method"] == "undefined", off
    assert off["method"] != on["method"]
