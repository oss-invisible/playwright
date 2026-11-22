"""The audio values a page reads follow the seed only when the audio noise is on.

What it fixes, in the published engine (firefox-68, series folder `fingerprint/audio/noise`): the audio noise hooks of
`dom/media/webaudio/` (`AnalyserNode.cpp` for `getFloatFrequencyData` and `getFloatTimeDomainData`, `AudioDestinationNode.cpp` for the
`OfflineAudioContext` render, `DynamicsCompressorNode.h` for `compressor.reduction`). When `zoom.stealth.audio.fp_noise` is true the
engine adds noise derived from `zoom.stealth.fpp.hw_seed` to those four values; the wrapper ships the pref false, so the values
of a product session are the stock ones.

Four values are read in one rendered graph (oscillator, compressor, analyser, destination of an `OfflineAudioContext(1, 5000, 44100)`):
the rendered samples, the analyser's frequency data, the analyser's time-domain data and the compressor's reduction.
Each is hashed over its float bits in the page, so a one-bit change shows.

With the noise ON: the same `hw_seed` gives the same four values on a fresh browser, and two seeds (11 and 22) give different ones;
the values also differ from the noise-off ones. The negative control is the same two seeds with `fp_noise` false: the four values are equal
across seeds, because the noise is the only thing that carries the seed. A page reads these values with no permission and no device (an
offline context renders without an audio output), so a runner with no sound card gives the same answers.

The seed is the engine's `hw_seed`, set directly (`extra_prefs`): the wrapper maps its own seed to a small pool of render seeds, so two
wrapper seeds can share one.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

NOISE = "zoom.stealth.audio.fp_noise"
HW_SEED = "zoom.stealth.fpp.hw_seed"
VALUES = ("offline", "frequency", "time", "reduction")

# FNV-1a over the 32-bit patterns of a float array, so equality is bit equality and -Infinity (a silent analyser bin) needs no JSON.
RENDER = """async () => {
  const hash = (floats) => {
    const bits = new Uint32Array(new Float32Array(floats).buffer);
    let h = 0x811c9dc5;
    for (let i = 0; i < bits.length; i++) { h ^= bits[i]; h = Math.imul(h, 0x01000193) >>> 0; }
    return h.toString(16) + ':' + bits.length;
  };
  const ctx = new OfflineAudioContext(1, 5000, 44100);
  const osc = ctx.createOscillator();
  osc.type = 'triangle';
  osc.frequency.value = 10000;
  const comp = ctx.createDynamicsCompressor();
  comp.threshold.value = -50;
  comp.knee.value = 40;
  comp.ratio.value = 12;
  comp.attack.value = 0;
  comp.release.value = 0.25;
  const analyser = ctx.createAnalyser();
  osc.connect(comp);
  comp.connect(analyser);
  analyser.connect(ctx.destination);
  osc.start(0);
  const rendered = await ctx.startRendering();
  const freq = new Float32Array(analyser.frequencyBinCount);
  analyser.getFloatFrequencyData(freq);
  const time = new Float32Array(analyser.fftSize);
  analyser.getFloatTimeDomainData(time);
  return {
    offline: hash(rendered.getChannelData(0)),
    frequency: hash(freq),
    time: hash(time),
    reduction: hash([comp.reduction]),
  };
}"""


def read_values(binary, fixture_server, noise, hw_seed):
    from invisible_playwright import InvisiblePlaywright

    prefs = {NOISE: noise, HW_SEED: hw_seed}
    with InvisiblePlaywright(seed=42, binary_path=binary, headless=True, locale="en-US",
                             timezone="America/New_York", extra_prefs=prefs) as browser:
        page = browser.new_page()
        page.goto(fixture_server + "/from-network", timeout=60_000)
        return page.evaluate(RENDER)


def test_audio_values_follow_the_seed_only_when_fp_noise_is_on(firefox_binary, fixture_server):
    on_11 = read_values(firefox_binary, fixture_server, True, 11)
    on_11_again = read_values(firefox_binary, fixture_server, True, 11)
    on_22 = read_values(firefox_binary, fixture_server, True, 22)
    off_11 = read_values(firefox_binary, fixture_server, False, 11)
    off_22 = read_values(firefox_binary, fixture_server, False, 22)

    # Every value was read (a hash of N floats, N > 0).
    for name in VALUES:
        for run in (on_11, on_22, off_11, off_22):
            assert not run[name].endswith(":0"), (name, run)

    # Noise on: the same seed, the same values, on a fresh browser.
    assert on_11 == on_11_again, (on_11, on_11_again)

    # Noise on: two seeds, different values, one by one.
    for name in VALUES:
        assert on_11[name] != on_22[name], (name, on_11, on_22)

    # Noise on is not noise off: the noise is what moves each value.
    for name in VALUES:
        assert on_11[name] != off_11[name], (name, on_11, off_11)

    # Control, noise off: the seed carries nothing, the values are equal across seeds.
    assert off_11 == off_22, (off_11, off_22)
