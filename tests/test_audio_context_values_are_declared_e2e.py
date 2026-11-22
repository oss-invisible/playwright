"""`AudioContext.outputLatency` and `destination.maxChannelCount` are the values the session declares.

What it fixes, in the published engine (firefox-68, series folder `fingerprint/audio/context-values`): the hooks at the top of
`AudioContext::OutputLatency` and `AudioContext::MaxChannelCount` (`dom/media/webaudio/AudioContext.cpp`) answer
`zoom.stealth.audio.output_latency_ms` / 1000 and `zoom.stealth.audio.max_channel_count` when the pref is > 0, before the engine
asks the audio device. The core declares both for every persona, because the host's answer is the machine's sound card.

A page reads both on a new `AudioContext()` with no permission. The values declared here (37 ms and 6 channels) are ones no runner
produces: a runner with no audio device answers 0 or 2.

Negative control, in the same test: with the engine off (`hw_seed` 0, stock Firefox: an active engine refuses a declaration pref cleared) the same page reads the host's values,
and they differ from the declared ones. The control reads what the host answers without asserting what it is: a runner with no audio
device may refuse the context, and that refusal is also not the declared value.

The prefs are set through `extra_prefs`, the overlay the core applies last.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

OUTPUT_LATENCY = "zoom.stealth.audio.output_latency_ms"
MAX_CHANNELS = "zoom.stealth.audio.max_channel_count"
HW_SEED = "zoom.stealth.fpp.hw_seed"

READ = """() => {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    return {outputLatency: ctx.outputLatency, maxChannelCount: ctx.destination.maxChannelCount};
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


def test_the_audio_context_answers_the_declared_latency_and_channel_count(firefox_binary):
    declared = read(firefox_binary, {OUTPUT_LATENCY: 37, MAX_CHANNELS: 6})
    assert declared == {"outputLatency": 0.037, "maxChannelCount": 6}, declared

    host = read(firefox_binary, {HW_SEED: 0})
    assert host != declared, (host, declared)
    assert host.get("outputLatency") != 0.037, host
    assert host.get("maxChannelCount") != 6, host
