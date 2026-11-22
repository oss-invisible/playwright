"""Native line endings are Windows' on every host.

On retail Windows Firefox a `Blob` made with `endings: 'native'` converts every
"\n" to "\r\n" (dom/file/BlobSet.cpp, under `XP_WIN`), and the plain-text
serializer used by `getSelection().toString()` puts "\r\n" between blocks
(dom/serializers/nsPlainTextSerializer.cpp, `DetermineLineBreak`); on retail
Linux both stay "\n". A page reads both, so the engine gives the lines of
Windows everywhere, with no pref (hook `fingerprint/js-surface/line-endings`:
`StealthNativeEOL.h`, `StealthPlainTextLineBreak.h`). The test is the same on
Linux and Windows, where the engine's answer is the same.

Control: the conversion is the one of `native`, not a global change of every
line break: the default `endings` (transparent) leaves a Blob's "\n" alone
(size 1), and `innerText` of the same two blocks keeps "\n". The stock
Firefox of the same candidate (1 and "\n" on Linux) cannot be asked from the
candidate: the hook is not behind the engine's gate, as it is not in the
retail.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

#: Raw: the backslash-n of the JS source is an escape the page reads, not a line break written into the string literal.
PROBE = r"""() => {
  document.body.innerHTML = '<div>one</div><div>two</div>';
  const sel = getSelection();
  sel.removeAllRanges();
  sel.selectAllChildren(document.body);
  return {
    native: new Blob(['\n'], {endings: 'native'}).size,
    native_text: new Blob(['a\nb'], {endings: 'native'}).size,
    transparent: new Blob(['\n'], {endings: 'transparent'}).size,
    default: new Blob(['\n']).size,
    selection: sel.toString(),
    inner_text: document.body.innerText,
  };
}"""


def test_native_line_endings_are_windows(firefox_binary):
    from invisible_playwright import InvisiblePlaywright

    with InvisiblePlaywright(seed=42, binary_path=firefox_binary, headless=True) as browser:
        page = browser.new_page()
        page.goto("about:blank")
        got = page.evaluate(PROBE)
    assert got["native"] == 2, got
    assert got["native_text"] == 4, got
    # The text between two blocks is "\r\n" and nothing is a bare "\n".
    assert "\r\n" in got["selection"], got
    assert got["selection"].replace("\r\n", "\n") == "one\ntwo", got
    assert "\n" not in got["selection"].replace("\r\n", ""), got
    # Control: the conversion belongs to `native` and to the serializer.
    assert got["transparent"] == 1 and got["default"] == 1, got
    assert "\r" not in got["inner_text"], got
