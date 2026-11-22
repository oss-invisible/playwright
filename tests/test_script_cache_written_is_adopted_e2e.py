"""A fresh profile keeps the startup script cache it wrote as a file mapping, not as a second copy on the heap (firefox-68, Linux and Windows).

What it fixes, in the published engine (firefox-68), for the hooks of `platform/script-cache` (`js/xpconnect/loader/ScriptPreloader.cpp` and `.h`, the own
file `StealthScriptCache-inl.h`): the script preloader writes `<profile>/startupCache/scriptCache.bin` once the startup is over. On a WARM profile the
cache is a file mapping (`explicit/script-preloader/non-heap/memmapped-cache`) and costs no heap. On a FRESH profile the same bytes were built in memory
for the write and stayed on the heap for the whole session (16.5 MB measured in the parent, with the 13.3 MB file already on disk): the engine now maps the
file it wrote and points every entry into the mapping (`AdoptWrittenCache`), and reports the mapping as
`explicit/script-preloader/non-heap/memmapped-written-cache`, so the megabytes that left the heap appear somewhere. It is a memory property, not a
fingerprint one: a page cannot read it. The preloader exists TWICE in the parent: `gScriptPreloader` (`scriptCache.bin`, the parent's scripts, about 14 MB) and `gChildScriptPreloader` (`scriptCache-child.bin`,
the scripts for content processes, about 1.2 MB; `ScriptPreloader::GetChildSingleton` runs `InitCache(u"scriptCache-child")` in the parent too). Both register the
same weak memory reporter, so the parent's report carries every path TWICE under the process name `""`, once per instance, in an order the engine does not fix; the
test keeps ALL the amounts of a path (a dict that kept the last one made the warm session look as if it mapped 1.2 MB while 14.3 MB was on disk: run 44985244053,
Linux 1207724 and Windows 1207732 reported, 14296776 and 14475288 on disk). The test also records, in every assertion message, the size of every file in
`startupCache/` (when the cache was adopted, when the fresh session was over, at the warm start and when the warm report was read). The test reads the memory report of the parent (the engine's own reporters, `nsIMemoryReporterManager.getReports`,
in chrome scope through Marionette: the route of `tests/webfont_probe.py`; `MOZ_REMOTE_ALLOW_SYSTEM_ACCESS` is set with `monkeypatch`).

What the test does. A NEW profile directory, a session on it. It waits on the CONDITION "`startupCache/scriptCache.bin` exists" (not on a timer), then on the
condition "the parent reports a non-zero `memmapped-written-cache`" (the file appears when the write is moved into place and is adopted a moment after).
It asserts, for that session: `memmapped-written-cache` equals the size of the file (non-zero), and `heap/saved-scripts` is below the size of the file (the
heap copies are gone: without the hook it is about the size of the file). Then the SAME profile is opened again (warm): `memmapped-cache` equals the size of
the file left on disk when the fresh session ended (it can be a rewrite of the shutdown, smaller than the adopted one) (the file is mapped under the name `scriptCache-current.bin`: a warm session moves `scriptCache.bin` aside when it opens it), and
`memmapped-written-cache` is 0 unless the session wrote the cache again, in which case `scriptCache.bin` is back and it equals the new size.

Negative control: the warm session of the same profile. The fresh and the warm session differ in the two reporters in the way described (written is the size
of the file in one and 0 in the other, read is 0 in one and the size in the other), so the reading cannot give the same number whatever the engine does.
"""
from __future__ import annotations

import json

import pytest

from _waits import wait_until

pytestmark = pytest.mark.e2e

MARIONETTE_ARGS = ["-marionette", "-remote-allow-system-access"]
WRITTEN = "explicit/script-preloader/non-heap/memmapped-written-cache"
READ_CACHE = "explicit/script-preloader/non-heap/memmapped-cache"
SAVED = "explicit/script-preloader/heap/saved-scripts"
LAUNCH = dict(seed=42, headless=True, humanize=False, locale="en-US", timezone="America/New_York")

#: The reports of the parent process (the process name of a reporter of this process is the empty string) under the script preloader.
REPORT_JS = """
const done = arguments[arguments.length - 1];
const out = {};
const manager = Cc["@mozilla.org/memory-reporter-manager;1"].getService(Ci.nsIMemoryReporterManager);
manager.getReports(
  {callback(process, path, kind, units, amount, description) {
    if (process === "" && path.startsWith("explicit/script-preloader/")) { (out[path] = out[path] || []).push(amount); }
  }},
  null,
  {callback() { done(JSON.stringify(out)); }},
  null,
  false);
"""


def _reports(marionette):
    """path -> sorted list of every amount reported under it (one per preloader instance: the parent's and the child's)."""
    answer = marionette.call("WebDriver:ExecuteAsyncScript", {"script": REPORT_JS, "args": []})
    return {path: sorted(amounts) for path, amounts in json.loads(answer["value"]).items()}


def _files(profile):
    """name -> size of every file in `<profile>/startupCache/` (empty if the folder is not there)."""
    folder = profile / "startupCache"
    if not folder.is_dir():
        return {}
    return {entry.name: entry.stat().st_size for entry in sorted(folder.iterdir()) if entry.is_file()}


def _amounts(reports, path):
    return reports.get(path, [])


def _session(binary, profile):
    from invisible_playwright import InvisiblePlaywright

    return InvisiblePlaywright(binary_path=binary, profile_dir=profile, extra_args=MARIONETTE_ARGS,
                               extra_prefs={"marionette.port": 0}, **LAUNCH)


def _open_marionette(profile):
    """Marionette of the browser on ``profile``: the port is the one the browser got (it was asked for port 0), read from the profile."""
    import webfont_probe

    marionette = webfont_probe._Marionette(webfont_probe.marionette_active_port(profile))
    marionette.call("WebDriver:NewSession", {"capabilities": {"alwaysMatch": {}}})
    marionette.call("WebDriver:SetTimeouts", {"script": 240000})
    marionette.call("Marionette:SetContext", {"value": "chrome"})
    return marionette


def test_a_fresh_profile_reports_its_written_cache_as_mapped(firefox_binary, monkeypatch, tmp_path):
    import webfont_probe

    monkeypatch.setenv("MOZ_REMOTE_ALLOW_SYSTEM_ACCESS", "1")   # the chrome-scope read of the memory report
    profile = tmp_path / "profile"
    cache = profile / "startupCache" / "scriptCache.bin"

    # The fresh session: the cache is written during it and adopted.
    marionette = None
    try:
        with _session(firefox_binary, profile) as context:
            page = context.new_page()
            page.goto("about:blank", timeout=60_000)
            written_file = wait_until(lambda: cache.exists() and cache.stat().st_size > 0, 180, lambda: page.wait_for_timeout(500))
            assert written_file, "the profile never got %s (the startup script cache is written once the startup is over)" % cache
            marionette = _open_marionette(profile)
            seen = {}
            sampled = []

            def reported():
                # ONE sample per poll: the size of the file read here is the one the report is compared with and the one the test keeps (the
                # file can be written again between two reads).
                seen.clear()
                seen.update(_reports(marionette))
                sampled.append(cache.stat().st_size if cache.exists() else 0)
                return sampled[-1] > 0 and sampled[-1] in _amounts(seen, WRITTEN)

            adopted = wait_until(reported, 60, lambda: page.wait_for_timeout(500))
            size = sampled[-1]
            fresh = dict(seen)
            files_adopted = _files(profile)
    finally:
        if marionette is not None:
            marionette.close()
    # The parent's own file is `scriptCache.bin`; the other amount under a path is the child preloader's (`scriptCache-child.bin`).
    assert adopted, "the parent never reported its written cache (%d bytes) as mapped (%s); reports (all amounts per path): %r; startupCache/ when adopted: %r" % (size, WRITTEN, fresh, files_adopted)
    assert size in _amounts(fresh, WRITTEN), "no mapping reported (%r) is the file written (%d bytes): %r; startupCache/: %r" % (_amounts(fresh, WRITTEN), size, fresh, files_adopted)
    assert max(_amounts(fresh, SAVED), default=0) < size, "the heap still holds the scripts of the cache that is on disk: %r (file %d bytes); startupCache/: %r" % (fresh, size, files_adopted)
    assert not any(_amounts(fresh, READ_CACHE)), "a fresh profile has no cache to read: %r; startupCache/: %r" % (fresh, files_adopted)

    # Control: the same profile, warm. The cache of the file is read as a mapping and nothing written this session is reported unless it was rewritten.
    # The warm session maps the file that is on disk when the fresh session is OVER, not the one that was adopted while it ran: the cache can be written
    # again during the shutdown (`ScriptPreloader::InvalidateCache` starts a new write when the cache is invalidated after it was saved, and the file is
    # replaced), so the size to compare with is read here, after the exit.
    files_left = _files(profile)
    started_with = cache.stat().st_size if cache.exists() else 0
    assert started_with > 0, "the fresh session left no script cache for the warm session to open: %s; startupCache/: %r" % (cache, files_left)
    marionette = None
    try:
        with _session(firefox_binary, profile) as context:
            page = context.new_page()
            page.goto("about:blank", timeout=60_000)
            marionette = _open_marionette(profile)
            warm = _reports(marionette)
            # A warm session MOVES the file it found to `scriptCache-current.bin` when it opens the cache (`ScriptPreloader::OpenCache`) and maps that
            # one; `scriptCache.bin` is there again only once this session has written a cache of its own (about 3 s after the startup, and only if
            # it has something to save). So the file of this session is absent until then, and nothing is reported as written until it exists
            # (`WriteCache` moves the file into place BEFORE it adopts it, so a report that carries a written cache finds the file here).
            now = cache.stat().st_size if cache.exists() else 0
            files_warm = _files(profile)
    finally:
        if marionette is not None:
            marionette.close()
    # What the warm session opens: every file the fresh session left (`scriptCache.bin`, the parent's, and `scriptCache-child.bin`, the child's, when it was
    # written) is moved to `<name>-current.bin` and mapped, and each mapping is reported once under `memmapped-cache`.
    left = sorted(size for name, size in files_left.items() if name in ("scriptCache.bin", "scriptCache-child.bin"))
    details = "startupCache/ at the end of the fresh session %r (%d bytes adopted while it ran), at the warm start %r, when the warm report was read %r; warm reports (all amounts per path): %r" % (files_left, size, files_left, files_warm, warm)
    assert sorted(_amounts(warm, READ_CACHE)) == left, "the warm session did not map the cache files it found (%r bytes on disk when the fresh session ended): %s" % (left, details)
    assert started_with in _amounts(warm, READ_CACHE), "the warm session did not map the parent's cache (%d bytes): %s" % (started_with, details)
    current = sorted(size for name, size in files_warm.items() if name in ("scriptCache.bin", "scriptCache-child.bin"))
    assert all(amount in [0] + current for amount in _amounts(warm, WRITTEN)), "the warm session reports a written cache that is not one of its files (%r): %s" % (current, details)
    assert _amounts(warm, READ_CACHE) != _amounts(fresh, READ_CACHE), details
