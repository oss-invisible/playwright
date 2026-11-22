"""`navigator.maxTouchPoints`, `navigator.hardwareConcurrency` and `navigator.storage.estimate().quota` are the values the session declares.

What it fixes, in the published engine (firefox-68), one test per hook:

* `fingerprint/hardware/profile`, `Navigator::MaxTouchPoints` (`dom/base/Navigator.cpp`): while the engine is active
  (`zoom.stealth.fpp.hw_seed` > 0) a page's `navigator.maxTouchPoints` is `zoom.stealth.max_touch_points`; the core derives it from the
  persona (0 for every persona today: a desktop) and a pin (`hardware.max_touch_points`) changes it.
* `fingerprint/hardware/profile` and `fingerprint/hardware/concurrency`, `Navigator::HardwareConcurrency` (`dom/base/Navigator.cpp`) and
  `WorkerNavigator::HardwareConcurrency` (`dom/workers/WorkerNavigator.cpp`): `zoom.stealth.hw_concurrency` when it is > 0, in the page, in a
  worker and in an iframe, so the three agree. The core declares the count of the persona; the host's count is the runner's CPUs.
* `fingerprint/hardware/storage-quota`, `EstimateOp::GetResponse` (`dom/quota/OriginOperations.cpp`): `zoom.stealth.storage.quota_mb` MiB when
  it is > 0, otherwise the quota Mozilla computed from the disk of the host.

A page reads all three with no permission (the storage estimate needs a secure context: the page is served from 127.0.0.1).
The values declared here (5 touch points, 7 cores, 123 MiB) are ones no runner has.

Negative controls, in each test: touch points with no pin (the persona's own value, 0) and with the engine off (`hw_seed` 0, the host's
answer, which is not 5); cores and quota with the engine off (`hw_seed` 0, stock Firefox: the engine refuses a launch that is active with a declaration pref cleared),
which are not 7 and not 123 MiB.
The prefs are set through `extra_prefs` (the overlay the core applies last), the touch points through the pin the wrapper exposes.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

HW_SEED = "zoom.stealth.fpp.hw_seed"
CORES = "zoom.stealth.hw_concurrency"
QUOTA_MB = "zoom.stealth.storage.quota_mb"
PAGE = "/from-network"

TOUCH = "() => navigator.maxTouchPoints"

CORE_COUNTS = """async () => {
  const code = 'self.onmessage = () => self.postMessage(self.navigator.hardwareConcurrency);';
  const worker = new Worker(URL.createObjectURL(new Blob([code], {type: 'application/javascript'})));
  const inWorker = await new Promise((resolve, reject) => {
    worker.onmessage = (e) => resolve(e.data);
    worker.onerror = (e) => reject(new Error(e.message));
    worker.postMessage(0);
  });
  worker.terminate();
  const frame = document.createElement('iframe');
  document.body.appendChild(frame);
  const inFrame = frame.contentWindow.navigator.hardwareConcurrency;
  frame.remove();
  return {main: navigator.hardwareConcurrency, worker: inWorker, iframe: inFrame};
}"""

QUOTA = """async () => {
  try {
    return {secure: isSecureContext, quota: (await navigator.storage.estimate()).quota};
  } catch (e) {
    return {error: String(e)};
  }
}"""


def read(binary, url, expression, extra_prefs=None, pin=None):
    from invisible_playwright import InvisiblePlaywright

    options = {}
    if pin:
        options["pin"] = pin
    with InvisiblePlaywright(seed=42, binary_path=binary, headless=True, locale="en-US",
                             timezone="America/New_York", extra_prefs=extra_prefs, **options) as browser:
        page = browser.new_page()
        page.goto(url, timeout=60_000)
        return page.evaluate(expression)


def test_max_touch_points_is_the_pinned_count_while_the_engine_is_active(firefox_binary, fixture_server):
    url = fixture_server + PAGE
    pinned = read(firefox_binary, url, TOUCH, pin={"hardware.max_touch_points": 5})
    assert pinned == 5

    # Control 1: no pin, the persona's own count (a desktop: 0).
    unpinned = read(firefox_binary, url, TOUCH)
    assert unpinned == 0
    assert unpinned != pinned

    # Control 2: the same pin with the engine off (hw_seed 0): the page reads what the host answers, not the declaration.
    engine_off = read(firefox_binary, url, TOUCH, extra_prefs={HW_SEED: 0}, pin={"hardware.max_touch_points": 5})
    assert engine_off != 5, engine_off


def test_hardware_concurrency_is_the_declared_count_in_the_page_the_worker_and_the_iframe(firefox_binary, fixture_server):
    url = fixture_server + PAGE
    declared = read(firefox_binary, url, CORE_COUNTS, extra_prefs={CORES: 7})
    assert declared == {"main": 7, "worker": 7, "iframe": 7}, declared

    # Control: the engine off (hw_seed 0), the three read the host's count (the same one, and not 7).
    host = read(firefox_binary, url, CORE_COUNTS, extra_prefs={HW_SEED: 0})
    assert host["main"] != 7, host
    assert host["worker"] == host["main"], host
    assert host["iframe"] == host["main"], host


def test_the_storage_quota_is_the_declared_megabytes(firefox_binary, fixture_server):
    url = fixture_server + PAGE
    declared = read(firefox_binary, url, QUOTA, extra_prefs={QUOTA_MB: 123})
    assert declared == {"secure": True, "quota": 123 * 1024 * 1024}, declared

    # Control: the engine off (hw_seed 0), the quota Mozilla computed from the host's disk.
    host = read(firefox_binary, url, QUOTA, extra_prefs={HW_SEED: 0})
    assert host.get("secure") is True, host
    assert host.get("quota") != 123 * 1024 * 1024, host
