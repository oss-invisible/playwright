"""`new AudioContext().sampleRate` is the rate the session declares.

What it fixes, in the published engine (firefox-68, series folder `fingerprint/audio/sample-rate`): the hook in
`mozilla::CubebUtils::PreferredSampleRate` (`dom/media/CubebUtils.cpp`) returns `zoom.stealth.audio.sample_rate` when it is > 0,
before the resist-fingerprinting 44100 and before the audio device is asked. The core declares a rate for every persona, because the
host's rate is the machine's sound card (44100, 48000, 96000...).

A page reads it on a new `AudioContext()` with no permission. The rate declared here, 22050, is one no sound card reports.

Negative control, in the same test: with the engine off (`hw_seed` 0, stock Firefox: an active engine refuses a declaration pref cleared) the same page reads the host's rate,
which is not 22050. The control does not assert what the host answers: a runner with no audio device answers its fallback, and a
refused context is also not the declared rate.

The pref is set through `extra_prefs`, the overlay the core applies last.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

SAMPLE_RATE = "zoom.stealth.audio.sample_rate"
HW_SEED = "zoom.stealth.fpp.hw_seed"

READ = """() => {
  try {
    return {sampleRate: new (window.AudioContext || window.webkitAudioContext)().sampleRate};
  } catch (e) {
    return {error: String(e)};
  }
}"""


def read(binary, prefs):
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=42, binary_path=binary, headless=True, locale="en-US",
                             timezone="America/New_York", extra_prefs=prefs) as browser:
        page = browser.new_page()
        page.goto("about:blank", timeout=60_000)
        return page.evaluate(READ)


def test_the_audio_context_answers_the_declared_sample_rate(firefox_binary):
    declared = read(firefox_binary, {SAMPLE_RATE: 22050})
    assert declared == {"sampleRate": 22050}, declared

    host = read(firefox_binary, {HW_SEED: 0})
    assert host != declared, (host, declared)
    assert host.get("sampleRate") != 22050, host
