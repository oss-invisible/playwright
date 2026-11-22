"""The WebGPU adapter a page sees is the one the core declares, never the host's (firefox-68, Linux and Windows).

Hooks of `fingerprint/webgpu/adapter-declaration` (`dom/webgpu/Adapter.cpp`, `Instance.cpp`, `StealthWebGPUDeclaration.cpp`, `WebGPUParent.cpp`): while the
engine is active (`zoom.stealth.fpp.hw_seed` > 0) the features, the limits and `isFallbackAdapter` of `navigator.gpu.requestAdapter()` come from the
prefs `zoom.stealth.webgpu.features`, `zoom.stealth.webgpu.limits` and `zoom.stealth.webgpu.adapter` (the core declares 12 features and 35 limits of
retail Firefox's Direct3D 12 backend for every persona, and the engine adds `core-features-and-limits` itself); `isFallbackAdapter` is true only for an
adapter asked for with `forceFallbackAdapter`; `RequestDevice` is clamped to the real adapter, so a page that asks for the declared maxima gets a device;
and a declared GPU without Direct3D 12 (`zoom.stealth.webgpu.adapter` = `none`) makes `requestAdapter()` resolve null. A page reads all of it with no
permission (a secure context: the page is served from 127.0.0.1). The adapter of the host is a Vulkan driver on Linux (our own, bundled) and WARP or a
GPU on Windows, and its features and limits differ from retail's.

Nothing is skipped: a runner with no adapter is a red test (the engine must give one: the bundled Vulkan on Linux, WARP on Windows).

Negative controls: in the first test the same read with the engine off (`hw_seed` 0) does not give the declared adapter (the host's, or null); in the second
the same call with the declared adapter `d3d12` gives an adapter where `none` gives null (the pref is what decides), and an unknown `featureLevel` gives
null on both.
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.e2e

HW_SEED = "zoom.stealth.fpp.hw_seed"
ADAPTER = "zoom.stealth.webgpu.adapter"
PAGE = "/from-network"

#: The adapter as a page reads it: its features, its limits, `info.isFallbackAdapter`, whether a device at the declared maxima resolves, and the adapter
#: asked for with `forceFallbackAdapter`. null when the page gets no adapter.
READ_ADAPTER = """async () => {
  if (!navigator.gpu) return {gpu: false};
  const describe = async (adapter) => {
    if (!adapter) return null;
    const limits = {};
    for (const name in adapter.limits) limits[name] = adapter.limits[name];
    return {features: [...adapter.features].sort(), limits, fallback: adapter.info.isFallbackAdapter};
  };
  const adapter = await navigator.gpu.requestAdapter();
  const out = {gpu: true, adapter: await describe(adapter)};
  if (adapter) {
    const maxima = ['maxBufferSize', 'maxStorageBufferBindingSize', 'maxTextureDimension2D', 'maxBindGroups',
                    'maxComputeWorkgroupStorageSize'];
    const required = {};
    for (const name of maxima) required[name] = adapter.limits[name];
    try {
      const device = await adapter.requestDevice({requiredLimits: required});
      const got = {};
      for (const name of maxima) got[name] = device.limits[name];
      out.device = {asked: required, got};
    } catch (e) {
      out.device = {error: String(e)};
    }
  }
  out.forced = await describe(await navigator.gpu.requestAdapter({forceFallbackAdapter: true}));
  return out;
}"""

READ_NO_ADAPTER = """async () => {
  if (!navigator.gpu) return {gpu: false};
  const answer = async (options) => (await navigator.gpu.requestAdapter(options)) === null ? null : 'adapter';
  return {
    gpu: true,
    plain: await answer(),
    unknown_level: await answer({featureLevel: 'no-such-level'}),
    xr: await answer({xrCompatible: true}),
  };
}"""


def read(binary, url, expression, extra_prefs=None):
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=42, binary_path=binary, headless=True, humanize=False, locale="en-US",
                             timezone="America/New_York", extra_prefs=extra_prefs) as browser:
        page = browser.new_page()
        page.goto(url, timeout=60_000)
        # The expression runs in the PAGE's realm, as a script of the page would: `page.evaluate` runs in the caller's world, where Firefox
        # cannot wrap the iterator of `adapter.features` (a set-like): "Xray wrapping of iterators not supported". The answer is written as
        # JSON into the DOM and the test waits on that condition.
        page.add_script_tag(content=(
            "(async () => { let out; try { out = await (%s)(); } catch (e) { out = {scriptError: String(e)}; }"
            " document.documentElement.setAttribute('data-read', JSON.stringify(out)); })();" % expression))
        page.wait_for_function("() => document.documentElement.hasAttribute('data-read')", timeout=60_000)
        answer = json.loads(page.evaluate("() => document.documentElement.getAttribute('data-read')"))
        assert not (isinstance(answer, dict) and "scriptError" in answer), answer
        return answer


def declared():
    """What the core declares, read from the core (the one place that holds it)."""
    from invisible_core.prefs import _WIN_WEBGPU_FEATURES, _WIN_WEBGPU_LIMITS

    features = sorted(_WIN_WEBGPU_FEATURES.split(chr(10)) + ["core-features-and-limits"])
    limits = {name: int(value) for name, value in (line.split("=") for line in _WIN_WEBGPU_LIMITS.split(chr(10)))}
    return features, limits


def test_the_page_sees_the_declared_adapter_e2e(firefox_binary, fixture_server):
    url = fixture_server + PAGE
    features, limits = declared()
    assert len(limits) == 35, len(limits)

    on = read(firefox_binary, url, READ_ADAPTER)
    assert on["gpu"] is True, "navigator.gpu is missing: %r" % (on,)
    adapter = on["adapter"]
    assert adapter is not None, "requestAdapter() gave no adapter with the engine on: the engine must give one on every runner"
    assert adapter["features"] == features, adapter["features"]
    differing = {k: (adapter["limits"].get(k), limits.get(k)) for k in set(limits) | set(adapter["limits"])
                 if adapter["limits"].get(k) != limits.get(k)}
    assert adapter["limits"] == limits, differing
    assert adapter["fallback"] is False
    # RequestDevice at the declared maxima resolves, and the device holds what was asked.
    assert on["device"].get("error") is None and on["device"]["asked"] == on["device"]["got"], on["device"]
    # An adapter asked for with forceFallbackAdapter is the fallback one, with the same declared features and limits.
    forced = on["forced"]
    assert forced is not None, "requestAdapter({forceFallbackAdapter: true}) gave no adapter"
    assert forced["fallback"] is True
    assert forced["features"] == features and forced["limits"] == limits

    # Control: with the engine off the page does not read the declaration (the host's adapter, or none).
    off = read(firefox_binary, url, READ_ADAPTER, extra_prefs={HW_SEED: 0})
    assert off["gpu"] is True, off
    assert off["adapter"] != adapter, "the engine off answered the declared adapter: %r" % (off["adapter"],)


def test_a_gpu_without_d3d12_has_no_adapter_e2e(firefox_binary, fixture_server):
    url = fixture_server + PAGE
    none = read(firefox_binary, url, READ_NO_ADAPTER, extra_prefs={ADAPTER: "none"})
    assert none["gpu"] is True, none
    # A declared GPU without Direct3D 12: requestAdapter resolves null, with or without options.
    assert none["plain"] is None and none["xr"] is None and none["unknown_level"] is None, none

    # Control: the same call with the declared adapter `d3d12` answers an adapter (also with xrCompatible, which warns and goes on), and an
    # unknown featureLevel answers null as it does for stock.
    d3d12 = read(firefox_binary, url, READ_NO_ADAPTER, extra_prefs={ADAPTER: "d3d12"})
    assert d3d12["plain"] == "adapter", d3d12
    assert d3d12["xr"] == "adapter", d3d12
    assert d3d12["unknown_level"] is None, d3d12
    assert d3d12["plain"] != none["plain"]
