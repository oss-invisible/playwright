"""`speechSynthesis.getVoices()` answers what retail Windows Firefox answers.

Measured on retail Windows Firefox 151 on 2026-10-13: David, Mark, Zira, then
David Desktop and Zira Desktop, all `en-US`, every one `localService` true and
none of them `default` (Gecko's SAPI backend never calls SetDefaultVoice).
Until invisible-core 44.39.0 the profile made David the default and listed Zira
before Mark. The list reaches the engine through `zoom.stealth.voices.list`,
so this reads it where a page reads it.

The session DECLARES the locale and the time zone that list belongs to. It
inherited them from the egress IP, so the test passed on US runners and failed
from anywhere else: from Italy the session was it-IT, and the list began with
Microsoft Cosimo, which is the right answer for that session and not the one
measured here ([B364]).
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

RETAIL = [
    ["Microsoft David - English (United States)", "en-US", False, True],
    ["Microsoft Mark - English (United States)", "en-US", False, True],
    ["Microsoft Zira - English (United States)", "en-US", False, True],
    ["Microsoft David Desktop - English (United States)", "en-US", False, True],
    ["Microsoft Zira Desktop - English (United States)", "en-US", False, True],
]

READ = """() => new Promise(resolve => {
  const read = () => speechSynthesis.getVoices().map(
      v => [v.name, v.lang, v.default, v.localService]);
  if (read().length) return resolve(read());
  speechSynthesis.addEventListener('voiceschanged', () => resolve(read()),
                                   {once: true});
  setTimeout(() => resolve(read()), 30000);
})"""


def test_the_voices_are_retail_windows_in_its_order_and_none_is_the_default(
        firefox_binary):
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=42, binary_path=firefox_binary, headless=True,
                             locale="en-US", timezone="America/New_York") as browser:
        page = browser.new_page()
        page.goto("about:blank")
        assert page.evaluate(READ) == RETAIL


# --- voiceURI: the retail form, and what an empty declaration falls to ------
# Retail Windows Firefox's SAPI backend names a voice `urn:moz-tts:sapi:<name>?<lang>`;
# the engine builds that string from the declared list (the hook of
# dom/media/webspeech/synth: StealthSpeechVoices, StealthSpeechVoice). With the
# engine off (`zoom.stealth.fpp.hw_seed` 0: an active engine refuses an empty
# declaration) the engine does not answer: Firefox's own path reads the host's
# registry (Windows) or speech-dispatcher (Linux).

READ_URIS = """() => speechSynthesis.getVoices().map(v => [v.name, v.voiceURI])"""

#: A voice no host has: the declaration, not a coincidence of the machine.
_ONLY = "Zaphod Quux - English (United States)"


def _uris(firefox_binary, extra_prefs):
    """The voices a page reads: asked until the list is not empty (the engine's
    answer is there at once; the host's comes when the host's service has loaded
    and may be empty, so the deadline is the end of the wait, not a failure)."""
    from _waits import wait_until
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=42, binary_path=firefox_binary, headless=True,
                             locale="en-US", timezone="America/New_York",
                             extra_prefs=extra_prefs) as browser:
        page = browser.new_page()
        page.goto("about:blank")

        def pump():
            page.wait_for_timeout(100)    # the events of a sync client are delivered while it calls the browser

        wait_until(lambda: page.evaluate(READ_URIS), 30, pump)
        return page.evaluate(READ_URIS)


def test_the_voice_uri_is_the_retail_form_and_an_empty_list_falls_to_the_registry(
        firefox_binary):
    declared = _uris(firefox_binary, None)
    assert declared == [[name, "urn:moz-tts:sapi:%s?en-US" % name] for name, *_ in RETAIL], declared
    assert not any(uri.startswith("stealth") for _n, uri in declared), declared

    # The declaration is what answers: a one-voice list is the whole answer.
    one = _uris(firefox_binary, {"zoom.stealth.voices.list": "%s|en-US|0|1" % _ONLY})
    assert one == [[_ONLY, "urn:moz-tts:sapi:%s?en-US" % _ONLY]], one

    # Negative control: the same launch with the engine off (stock Firefox) is not
    # answered by the engine. Whatever the host's registry (Windows) or speech-dispatcher
    # (Linux) lists, it is not the declared voice, and no URI is of the engine's
    # internal `stealth` form.
    empty = _uris(firefox_binary, {"zoom.stealth.fpp.hw_seed": 0})
    assert all(name != _ONLY for name, _uri in empty), empty
    assert not any(uri.startswith("stealth") for _n, uri in empty), empty
    assert empty != one, (empty, one)
