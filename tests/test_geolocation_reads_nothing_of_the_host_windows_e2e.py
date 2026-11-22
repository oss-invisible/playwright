"""The engine never asks the host where it is: a page that asks for a position starts no location process and loads no location module.

What it fixes, in the published engine (firefox-68), for the hooks of `fingerprint/geolocation/no-host-provider`
(`GeolocationService::Init`, `GeolocationSystem::*`, `PermissionObserver`, in `dom/geolocation` and `netwerk/...`): the position a page reads comes only
from the declaration (the standard `geolocation` option of the context, which the core fills for every session). The engine does not ask the
operating system for a position, for a Wi-Fi scan or for the privacy switch of the location. Retail Firefox on Windows does: the first position
request makes the parent start a utility process of the kind `WINDOWS_UTILS` (`-sandboxingKind 3`, which runs the WinRT location provider) and load
`geolocation.dll`, `locationframeworkps.dll`, `wlanapi.dll` and `capabilityaccessmanagerclient.dll`. A page cannot see a process, but it sees what
that road gives (the position of the host, a delay, an error), and a host that has a location service answers differently from one that has not.

What the test does. One session (seed 42, no proxy), a context that declares a position no runner has (12.345, -45.678) and grants the permission, a
page served from 127.0.0.1 (a secure context). The page runs `permissions.query`, `getCurrentPosition` and `watchPosition`. Then the test lists the
engine processes below the test process (the executable is in the engine's folder) and reads, for each one it may read, the files mapped into it
(`psutil` `memory_maps`: the DLLs on Windows, the shared objects on Linux) and its command line.

What it asserts:
* Windows: none of `geolocation.dll`, `locationframeworkps.dll`, `wlanapi.dll`, `capabilityaccessmanagerclient.dll` is mapped in any engine process, and
  no process has `-sandboxingKind 3`;
* Linux (the engine has no `WINDOWS_UTILS` there, so the Windows modules cannot exist; the form that holds on Linux is "no host location library"): none
  of the files mapped in an engine process is a location library of the host (`libgeoclue`, `libgeocode`, `libportal`), and no utility process
  is of a kind other than the generic one (`-sandboxingKind` 0) or the inference one;
* the control, on both systems: the page was told the DECLARED position by `getCurrentPosition` and by `watchPosition` (so the calls ran to the end,
  and the answer came from the declaration, not from the host), `permissions.query` says `granted`, and the reading of the mapped files is not blind
  (the parent was readable and has `xul.dll` / `libxul.so` mapped). A test that listed nothing would pass without this.

The retail Firefox of the same window, which loads these modules, is not in this file (a test never runs another browser in the gate): the
control is the declared answer plus the reading that sees a module that is certainly there.
"""
from __future__ import annotations

import http.server
import os
import sys
import threading

import pytest

pytestmark = pytest.mark.e2e

WINDOWS = sys.platform == "win32"

#: What retail Firefox on Windows maps once a page asks for a position (the WinRT provider and its capability checks).
WINDOWS_LOCATION_MODULES = ("geolocation.dll", "locationframeworkps.dll", "wlanapi.dll", "capabilityaccessmanagerclient.dll")
#: The Linux forms of the same idea: the location services of the desktop (GeoClue) and the portal.
LINUX_LOCATION_LIBRARIES = ("libgeoclue", "libgeocode", "libportal")
#: `SandboxingKind::WINDOWS_UTILS` of the engine's `ipc/glue/UtilityProcessSandboxing.h`: GENERIC_UTILITY 0, UTILITY_AUDIO_DECODING_WMF 1,
#: MF_MEDIA_ENGINE_CDM 2, WINDOWS_UTILS 3.
WINDOWS_UTILS_KIND = "3"
#: Linux: GENERIC_UTILITY 0, HW_INFERENCE 1 (the only kinds the Linux engine has).
LINUX_UTILITY_KINDS = ("0", "1")

#: A position no runner has: the page must read exactly this.
POSITION = {"latitude": 12.345, "longitude": -45.678, "accuracy": 25}

PAGE = b"<!doctype html><html><head><meta charset=utf-8><title>geolocation</title></head><body><p>where</p></body></html>"

READ = """async () => {
  const position = (p) => ({lat: p.coords.latitude, lon: p.coords.longitude});
  const failure = (e) => ({error: e.code, message: e.message});
  const out = {secure: isSecureContext};
  out.permission = (await navigator.permissions.query({name: 'geolocation'})).state;
  out.current = await new Promise(resolve => {
    navigator.geolocation.getCurrentPosition(p => resolve(position(p)), e => resolve(failure(e)), {timeout: 30000});
  });
  out.watched = await new Promise(resolve => {
    const id = navigator.geolocation.watchPosition(
      p => { navigator.geolocation.clearWatch(id); resolve(position(p)); },
      e => { navigator.geolocation.clearWatch(id); resolve(failure(e)); },
      {timeout: 30000});
  });
  return out;
}"""


def _a_page_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        timeout = 5

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(PAGE)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(PAGE)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d/" % server.server_address[1]


def _engine_processes(binary):
    """The processes below this one whose executable is in the engine's folder (the tree of THIS test, not the machine)."""
    import psutil

    folder = os.path.normcase(os.path.dirname(os.path.realpath(binary)))
    found = []
    for proc in psutil.Process().children(recursive=True):
        try:
            exe = os.path.normcase(os.path.realpath(proc.exe()))
        except psutil.Error:
            continue
        if exe.startswith(folder):
            found.append(proc)
    return found


def _describe(proc):
    """(kind, command line, mapped file names in lower case or None when the process cannot be read)."""
    import psutil

    try:
        cmd = proc.cmdline()
    except psutil.Error:
        cmd = []
    kind = cmd[-1] if "-contentproc" in cmd else "parent"
    try:
        mapped = {os.path.basename(m.path).lower() for m in proc.memory_maps(grouped=True)}
    except (psutil.Error, OSError):
        mapped = None
    return kind, cmd, mapped


def _sandboxing_kind(cmd):
    return cmd[cmd.index("-sandboxingKind") + 1] if "-sandboxingKind" in cmd and cmd.index("-sandboxingKind") + 1 < len(cmd) else None


def test_no_host_location_module_loads_and_no_windows_utils_process_starts(firefox_binary):
    from invisible_playwright import InvisiblePlaywright

    server, url = _a_page_server()
    try:
        with InvisiblePlaywright(seed=42, binary_path=firefox_binary, headless=True, humanize=False,
                                 locale="en-US", timezone="America/New_York") as browser:
            context = browser.new_context(geolocation=POSITION, permissions=["geolocation"])
            page = context.new_page()
            page.goto(url, wait_until="load", timeout=60_000)
            answer = page.evaluate(READ)
            described = [_describe(proc) for proc in _engine_processes(firefox_binary)]
            context.close()
    finally:
        server.shutdown()
        server.server_close()

    # The control: the calls ran to the end and the answer is the declaration.
    declared = {"lat": POSITION["latitude"], "lon": POSITION["longitude"]}
    assert answer["secure"] is True and answer["permission"] == "granted", answer
    assert answer["current"] == declared, "getCurrentPosition did not answer the declared position: %r" % (answer,)
    assert answer["watched"] == declared, "watchPosition did not answer the declared position: %r" % (answer,)

    # The reading is not blind: the parent was readable and has the engine's own library mapped.
    parents = [(cmd, mapped) for kind, cmd, mapped in described if kind == "parent"]
    assert parents, "no engine parent process below the test: %r" % ([(k, m is not None) for k, _c, m in described],)
    engine_library = "xul.dll" if WINDOWS else "libxul.so"
    readable_parent = [mapped for _cmd, mapped in parents if mapped is not None]
    assert readable_parent and any(engine_library in mapped for mapped in readable_parent), (
        "the mapped files of the parent cannot be read, or %s is not among them: the check below would pass with an empty list (%r)"
        % (engine_library, [sorted(m)[:8] if m else m for _c, m in parents]))

    found = []
    kinds = []
    for kind, cmd, mapped in described:
        sandboxing = _sandboxing_kind(cmd)
        if sandboxing is not None:
            kinds.append((kind, sandboxing))
        for name in mapped or ():
            if WINDOWS and name in WINDOWS_LOCATION_MODULES:
                found.append((kind, name))
            if not WINDOWS and any(name.startswith(prefix) or prefix in name for prefix in LINUX_LOCATION_LIBRARIES):
                found.append((kind, name))
    assert found == [], "a process of the engine mapped a host location module: %s" % (found,)
    if WINDOWS:
        assert not [k for k in kinds if k[1] == WINDOWS_UTILS_KIND], "a utility process of the kind WINDOWS_UTILS was started: %s" % (kinds,)
    else:
        assert not [k for k in kinds if k[1] not in LINUX_UTILITY_KINDS], "a utility process of an unexpected kind was started: %s" % (kinds,)
