#!/usr/bin/env python3
"""A MEASUREMENT, not a gate: who wins in the REAL Firefox when a second navigation starts while the first changes process.

The reference of scripts/hunt_second_goto_vs_stock.py. Stock Playwright's Firefox turns Fission off, so it is no reference for this
question; the reference here is Firefox 159.0 RETAIL, the build Mozilla publishes on archive.mozilla.org, with Fission ON, driven from
the PARENT process over Marionette in the chrome context: no Juggler, no page, no automation layer between the two navigations.

The scenario is the one of hunt_second_goto_vs_stock.py:

  * a local server on a thread, with a.test, b.test and c.test on its one port (`network.dns.localDomains`);
  * the page is at a.test/plain; load 1 goes to b.test/partial, whose headers and first bytes the server sends and whose rest it HOLDS;
  * as soon as the SERVER has sent the start of /partial (a Marionette client does not hear responses: the signal is the server's),
    load 2 goes to c.test/second;
  * the server lets /partial go only at the end of the measurement: 15 s after load 2, or as soon as the parent says c.test/second
    has committed (`currentURI` of the browser is its URL and the document is not loading).

Both loads are `gBrowser.selectedBrowser.browsingContext.loadURI(Services.io.newURI(url), {triggeringPrincipal: system})` run in the
chrome context (`--api browser` runs `selectedBrowser.loadURI(...)` instead; `--api bc-string` gives the URL as a string).

Per repetition it records:

  * which page stays loaded, read by the parent: `selectedBrowser.currentURI` (the page of the last commit), the URL of the document
    of the current window global, the title, whether the browser is still loading;
  * what the server saw: did /second arrive, and at how many ms after load 2 started (server and client read the same `perf_counter`);
  * how long the final page took to be loaded, in ms after load 2 started, and what released the held response;
  * that Fission is on: `Services.appinfo.fissionAutostart`, the isolation strategy, and the number and kinds of content processes.

A new BROWSER and a new PROFILE per repetition (the state a user's first navigations have; the spare process, the one of a.test and the
ones of b.test and c.test are what the outcome depends on). Nothing here decides: the exit code is non-zero only when the script itself
could not measure (no repetition measured, or Fission never on).

    python scripts/hunt_second_goto_retail.py --fetch-dir "$RUNNER_TEMP/retail" --out-dir out      # download 159.0, 20 repetitions
    python scripts/hunt_second_goto_retail.py --binary /path/to/firefox --repetitions 3 --out-dir out

The only network use is the download of the build and of its SHA256SUMS from archive.mozilla.org; the measurement is local.
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import platform
import shutil
import signal
import statistics
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingTCPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))   # the Marionette client of the repo (tests/webfont_probe.py), as the fonts e2e uses it

VERSION = "159.0"
ARCHIVE = "https://archive.mozilla.org/pub/firefox/releases/%s" % VERSION
#: What is downloaded, per system: the path under the release (the key of its line in SHA256SUMS) and how it is made a folder.
DOWNLOADS = {
    "linux": "linux-x86_64/en-US/firefox-%s.tar.xz" % VERSION,
    "windows": "win64/en-US/Firefox Setup %s.exe" % VERSION,   # a 7z archive with a stub: 7z opens it, nothing is installed
}

#: The prefs of the profile (user.js). Retail defaults for everything else: Fission is `fission.autostart`, the isolation strategy is Mozilla's.
PREFS = {
    "fission.autostart": True,
    "network.dns.localDomains": "a.test,b.test,c.test",
    # nothing asks the internet and nothing updates or reports
    "app.update.auto": False,
    "app.update.enabled": False,
    "app.update.service.enabled": False,
    "app.update.staging.enabled": False,
    "app.update.checkInstallTime": False,
    "datareporting.policy.dataSubmissionEnabled": False,
    "datareporting.healthreport.uploadEnabled": False,
    "toolkit.telemetry.enabled": False,
    "toolkit.telemetry.unified": False,
    "toolkit.telemetry.reportingpolicy.firstRun": False,
    "app.normandy.enabled": False,
    "app.shield.optoutstudies.enabled": False,
    "messaging-system.rsexperimentloader.enabled": False,
    "browser.region.update.enabled": False,
    "browser.search.update": False,
    "browser.discovery.enabled": False,
    "browser.safebrowsing.malware.enabled": False,
    "browser.safebrowsing.phishing.enabled": False,
    "browser.safebrowsing.downloads.enabled": False,
    "network.captive-portal-service.enabled": False,
    "network.connectivity-service.enabled": False,
    "extensions.getAddons.cache.enabled": False,
    "extensions.update.enabled": False,
    "dom.security.https_first": False,
    "network.proxy.type": 0,
    # a window with nothing in it, and no first-run page
    "browser.shell.checkDefaultBrowser": False,
    "browser.startup.page": 0,
    "browser.startup.homepage": "about:blank",
    "startup.homepage_welcome_url": "",
    "startup.homepage_welcome_url.additional": "",
    "browser.aboutwelcome.enabled": False,
    "browser.newtabpage.enabled": False,
    "datareporting.policy.firstRunURL": "",
    "browser.tabs.warnOnClose": False,
}

GOTO2_BUDGET_S = 15.0          # as asked: the held response is let go 15 s after load 2 if c.test/second has not committed
PARTIAL_SENT_S = 20.0          # how long the client waits for the server to send the start of /partial
SETTLE_S = 1.0                 # after the release: a /second that comes late is still counted
AFTER_RELEASE_S = 3.0          # how long the parent keeps reading after the release, for a page that loads only then
POLL_S = 0.05
LAUNCH_S = 90.0
REPETITION_S = 150.0
REPETITIONS = 20


def _ms(seconds):
    return None if seconds is None else round(seconds * 1000.0, 1)


def _system():
    return {"win32": "windows", "linux": "linux", "darwin": "macos"}.get(sys.platform, sys.platform)


# ---------------------------------------------------------------- the server

class _Server(ThreadingTCPServer):
    """A thread per connection and daemon threads: a connection the browser opens and leaves cannot hold the server or its shutdown."""
    daemon_threads = True
    allow_reuse_address = True


class Sites:
    """The local server of one repetition. `arrived` has the time (perf_counter) each path was first asked for."""

    def __init__(self):
        self.arrived = {}                    # path -> perf_counter of the first request
        self.partial_sent = threading.Event()  # set once the headers and the first bytes of /partial are on the wire
        self.sent_at = None
        self.release = threading.Event()     # what the server holds until the measurement is over
        owner = self

        class Handler(BaseHTTPRequestHandler):
            timeout = 10

            def log_message(self, *args):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                owner.arrived.setdefault(path, time.perf_counter())
                try:
                    if path == "/partial":
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html")
                        self.send_header("Content-Length", "200")
                        self.end_headers()
                        self.wfile.write(b"<!doctype html><title>partial</title><p>start")
                        self.wfile.flush()
                        owner.sent_at = time.perf_counter()
                        owner.partial_sent.set()
                        owner.release.wait()
                        self.wfile.write(b" " * 156)
                        return
                    body = ("<!doctype html><title>%s</title><p>%s" % (path, path)).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def at(self, host, path):
        return "http://%s:%d%s" % (host, self.port, path)

    def close(self):
        self.release.set()
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
        self.thread.join(timeout=10)


# ---------------------------------------------------------------- the retail build

def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(url, target, attempts=3):
    last = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "second-goto-retail-hunt"})
            with urllib.request.urlopen(request, timeout=60) as response, open(target, "wb") as out:
                shutil.copyfileobj(response, out, 1 << 20)
            return
        except Exception as e:  # noqa: BLE001 - a network failure of any kind is retried, then reported
            last = e
            print("download of %s failed (attempt %d/%d): %r" % (url, attempt, attempts, e), flush=True)
            time.sleep(3 * attempt)
    raise RuntimeError("could not download %s: %r" % (url, last))


def _seven_zip():
    found = shutil.which("7z") or shutil.which("7za")
    if found:
        return found
    for candidate in (r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError("7z is needed to open the Windows installer and was not found")


def fetch_retail(folder):
    """Download Firefox VERSION retail into `folder`, check it against Mozilla's SHA256SUMS, extract it. Returns (binary, info)."""
    system = _system()
    if system not in DOWNLOADS:
        raise RuntimeError("no retail download for %s" % system)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    key = DOWNLOADS[system]
    archive = folder / Path(key).name
    sums = folder / "SHA256SUMS"
    _download(ARCHIVE + "/SHA256SUMS", sums)
    _download(ARCHIVE + "/" + key.replace(" ", "%20"), archive)
    actual = _sha256(archive)
    listed = None
    for line in sums.read_text(encoding="utf-8", errors="replace").splitlines():
        digest, _, name = line.partition("  ")
        if name.strip() == key:
            listed = digest.strip().lower()
    if listed is not None and listed != actual:
        raise RuntimeError("sha256 of %s is %s, Mozilla's SHA256SUMS says %s" % (archive.name, actual, listed))
    print("downloaded %s sha256 %s (SHA256SUMS: %s)" % (archive.name, actual, "same" if listed else "no line for it"), flush=True)
    extracted = folder / "extracted"
    if extracted.exists():
        shutil.rmtree(extracted)
    extracted.mkdir()
    if system == "linux":
        with tarfile.open(archive, "r:xz") as tar:
            try:
                tar.extractall(extracted, filter="data")
            except TypeError:                       # a Python before the extraction filters
                tar.extractall(extracted)
        binary = extracted / "firefox" / "firefox"
    else:
        subprocess.run([_seven_zip(), "x", str(archive), "-o" + str(extracted), "-y"], check=True, stdout=subprocess.DEVNULL)
        binary = extracted / "core" / "firefox.exe"
    if not binary.is_file():
        raise RuntimeError("the build was extracted and %s is not there" % binary)
    info = {"version": VERSION, "url": ARCHIVE + "/" + key, "file": archive.name, "sha256": actual,
            "sha256sums_line": listed, "sha256sums_matches": (listed == actual) if listed else None}
    return str(binary), info


def build_of(binary):
    """What application.ini next to the binary says: version, build ID, source stamp."""
    ini = Path(binary).resolve().parent / "application.ini"
    out = {}
    if ini.is_file():
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(ini, encoding="utf-8")
        if parser.has_section("App"):
            out = {k: parser.get("App", k, fallback=None) for k in ("Version", "BuildID", "SourceRepository", "SourceStamp")}
    return out


# ---------------------------------------------------------------- the browser

def _write_user_js(profile):
    lines = ["user_pref(%s, %s);" % (json.dumps(name), json.dumps(value)) for name, value in sorted(PREFS.items())]
    (Path(profile) / "user.js").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _kill_tree(proc):
    """The browser and everything it started. On a CI runner (CI=true) every firefox left is killed too: the Windows launcher process
    can hand the browser to a process that is not its child."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            if os.environ.get("CI"):
                subprocess.run(["taskkill", "/F", "/T", "/IM", "firefox.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.kill()
    except Exception:  # noqa: BLE001 - the cleanup never costs the measurement
        pass
    try:
        proc.wait(timeout=30)
    except Exception:  # noqa: BLE001
        pass


class Browser:
    """One retail Firefox with a fresh profile, driven by Marionette in the chrome context."""

    def __init__(self, binary):
        self.binary = binary
        self.profile = tempfile.mkdtemp(prefix="retail-profile-")
        self.log_path = Path(tempfile.mkstemp(prefix="retail-log-", suffix=".txt")[1])
        self.proc = None
        self.m = None

    def launch(self):
        from webfont_probe import _Marionette, marionette_port

        port = marionette_port()
        _write_user_js(self.profile)
        with open(Path(self.profile) / "user.js", "a", encoding="utf-8", newline="\n") as f:
            f.write("user_pref(\"marionette.port\", %d);\n" % port)
        env = dict(os.environ, MOZ_REMOTE_ALLOW_SYSTEM_ACCESS="1", MOZ_CRASHREPORTER_DISABLE="1", MOZ_DISABLE_AUTO_SAFE_MODE="1")
        cmd = [self.binary, "-headless", "-no-remote", "-profile", self.profile, "-marionette", "-remote-allow-system-access"]
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        with open(self.log_path, "wb") as log:
            self.proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kwargs)
        self.m = _Marionette(port, timeout=LAUNCH_S)
        self.m.sock.settimeout(60)
        self.m.call("WebDriver:NewSession", {"capabilities": {"alwaysMatch": {}}})
        try:
            self.m.call("WebDriver:SetContext", {"value": "chrome"})
        except RuntimeError:                      # older name of the same command
            self.m.call("Marionette:SetContext", {"value": "chrome"})
        end = time.time() + 60
        while True:                               # the browser window is there when its startup is done
            try:
                if self.run("const w = Services.wm.getMostRecentWindow('navigator:browser');"
                            "return !!(w && w.gBrowser && w.gBrowser.selectedBrowser && w.gBrowserInit && w.gBrowserInit.delayedStartupFinished);"):
                    break
            except RuntimeError:
                pass
            if time.time() > end:
                raise RuntimeError("no browser window with a selected browser after 60 s")
            time.sleep(0.2)

    def run(self, script, *args):
        reply = self.m.call("WebDriver:ExecuteScript", {"script": script, "args": list(args)})
        return reply.get("value") if isinstance(reply, dict) else reply

    def fission(self):
        """The evidence that Fission is on: the app's own answer, the strategy, and the content processes there are now."""
        return self.run(
            "const kinds = ChromeUtils.getAllDOMProcesses().map(p => p.remoteType).filter(t => t);"
            "return {fission_autostart: Services.appinfo.fissionAutostart, decision: Services.appinfo.fissionDecisionStatusString,"
            " strategy: Services.prefs.getIntPref('fission.webContentIsolationStrategy', -1),"
            " version: Services.appinfo.version, build_id: Services.appinfo.appBuildID,"
            " content_processes: kinds.length, remote_types: kinds.sort()};")

    def load(self, url, api):
        """The navigation, from the parent, with the system principal. Returns the form of the call that was made."""
        return self.run(
            "const [url, api] = arguments;"
            "const b = Services.wm.getMostRecentWindow('navigator:browser').gBrowser.selectedBrowser;"
            "const opts = {triggeringPrincipal: Services.scriptSecurityManager.getSystemPrincipal()};"
            "if (api === 'browser') { b.loadURI(Services.io.newURI(url), opts); return 'selectedBrowser.loadURI(nsIURI)'; }"
            "if (api === 'bc-string') { b.browsingContext.loadURI(url, opts); return 'browsingContext.loadURI(string)'; }"
            "b.browsingContext.loadURI(Services.io.newURI(url), opts); return 'browsingContext.loadURI(nsIURI)';",
            url, api)

    def state(self):
        """Which page the parent says is loaded: the page of the last commit, the document of the window global, the loading flag."""
        return self.run(
            "const b = Services.wm.getMostRecentWindow('navigator:browser').gBrowser.selectedBrowser;"
            "const wg = b.browsingContext && b.browsingContext.currentWindowGlobal;"
            "return {uri: b.currentURI ? b.currentURI.spec : null, doc: wg && wg.documentURI ? wg.documentURI.spec : null,"
            " loading: b.webProgress ? b.webProgress.isLoadingDocument : null, title: b.contentTitle,"
            " remote_type: b.remoteType, pid: wg ? wg.osPid : null};")

    def close(self):
        if self.m is not None:
            self.m.close()
        if self.proc is not None:
            _kill_tree(self.proc)
        tail = None
        try:
            tail = self.log_path.read_text(encoding="utf-8", errors="replace")[-600:]
        except OSError:
            pass
        for _ in range(5):
            shutil.rmtree(self.profile, ignore_errors=True)
            if not os.path.exists(self.profile):
                break
            time.sleep(1)
        try:
            self.log_path.unlink()
        except OSError:
            pass
        return tail


# ---------------------------------------------------------------- one repetition

def _page(uri):
    """Which of the pages of the scenario a URL is."""
    if not uri:
        return "none"
    parts = urlsplit(uri)
    host_path = (parts.hostname or "") + parts.path
    return {"b.test/partial": "b.test/partial", "c.test/second": "c.test/second", "a.test/plain": "a.test/plain"}.get(host_path, "other: " + uri[:80])


def _scenario(browser, sites, api, row):
    """Every time is perf_counter; the row says them in ms after the start of load 2 (`goto2_start`) unless the name says otherwise."""
    row["fission"] = browser.fission()
    row["load_api"] = browser.load(sites.at("a.test", "/plain"), api)
    end = time.monotonic() + 20
    while True:
        s = browser.state()
        if _page(s["uri"]) == "a.test/plain" and not s["loading"]:
            break
        if time.monotonic() > end:
            raise RuntimeError("the first page never loaded (state %r): the load call %r may not take this form, see --api" % (s, row["load_api"]))
        time.sleep(POLL_S)

    t1 = time.perf_counter()
    browser.load(sites.at("b.test", "/partial"), api)                    # load 1
    sent = sites.partial_sent.wait(PARTIAL_SENT_S)
    row["partial_sent"] = sent
    if not sent:
        row["state_at_the_end"] = browser.state()
        return
    t2 = time.perf_counter()
    browser.load(sites.at("c.test", "/second"), api)                     # load 2, at once: the server's signal, no wait
    t2_returned = time.perf_counter()

    history = []                                                           # (ms after load 2, uri, loading): only when it changes
    seen = None
    committed_ms = None
    loaded_ms = {}                                                         # page -> first ms it was the current page and not loading
    released_by = None
    release_at = None
    deadline = t2 + GOTO2_BUDGET_S
    while True:
        now = time.perf_counter()
        s = browser.state()
        at = now - t2
        key = (s["uri"], s["loading"])
        if key != seen:
            history.append({"ms": _ms(at), "uri": s["uri"], "loading": s["loading"]})
            seen = key
        page = _page(s["uri"])
        if page == "c.test/second" and committed_ms is None:
            committed_ms = at
        if page != "none" and not s["loading"]:
            loaded_ms.setdefault(page, at)
        if release_at is None:
            if page == "c.test/second" and not s["loading"]:
                released_by, release_at = "c.test/second committed", now
                sites.release.set()
            elif now > deadline:
                released_by, release_at = "%.0f s after load 2" % GOTO2_BUDGET_S, now
                sites.release.set()
        elif now - release_at > AFTER_RELEASE_S or (page != "none" and not s["loading"] and now - release_at > SETTLE_S):
            break
        time.sleep(POLL_S)
    time.sleep(SETTLE_S)                                                   # a /second that comes late is still counted
    final_state = browser.state()
    final = _page(final_state["uri"])
    second = sites.arrived.get("/second")

    row.update({
        "partial_request_ms_after_load1": _ms(sites.arrived.get("/partial", t1) - t1) if "/partial" in sites.arrived else None,
        "partial_sent_ms_after_load1": _ms(sites.sent_at - t1) if sites.sent_at else None,
        "load2_call_ms": _ms(t2_returned - t2),
        "second_request_arrived": second is not None,
        "second_request_after_load2_ms": None if second is None else _ms(second - t2),
        "second_committed_ms": _ms(committed_ms),
        "released_by": released_by,
        "release_ms": _ms(release_at - t2),
        "final_page": final,
        "final_uri": final_state["uri"],
        "final_document": final_state["doc"],
        "final_title": final_state["title"],
        "final_loading": final_state["loading"],
        "final_remote_type": final_state["remote_type"],
        "final_loaded_ms": _ms(loaded_ms.get(final)) if final in loaded_ms else None,
        "final_loaded_after_release": (final in loaded_ms and loaded_ms[final] > release_at - t2) or None,
        "history": history,
        "requests_at_the_server": sorted(sites.arrived),
    })
    row["fission_end"] = browser.fission()


def _repetition(binary, index, api):
    row = {"repetition": index, "launched": False}
    sites = Sites()
    browser = Browser(binary)
    started = time.monotonic()
    try:
        try:
            browser.launch()
            row["launched"] = True
        except Exception as e:  # noqa: BLE001
            row["launch_error"] = "%s: %s" % (type(e).__name__, e)
            return row
        try:
            _scenario(browser, sites, api, row)
        except Exception as e:  # noqa: BLE001
            row["scenario_error"] = "%s: %s" % (type(e).__name__, e)
        if time.monotonic() - started > REPETITION_S:
            row["slow_repetition_s"] = round(time.monotonic() - started, 1)
    finally:
        sites.close()
        tail = browser.close()
        if tail is not None and ("launch_error" in row or "scenario_error" in row):
            row["browser_log_tail"] = tail
    return row


# ---------------------------------------------------------------- the summary

def _first_line(message):
    return (message or "").strip().splitlines()[0][:160] if message else None


def summarise(rows):
    measured = [r for r in rows if "final_page" in r]

    def median_max(values):
        return {"median": round(statistics.median(values), 1), "max": round(max(values), 1)} if values else {"median": None, "max": None}

    return {
        "repetitions": len(rows),
        "launched": sum(1 for r in rows if r.get("launched")),
        "measured": len(measured),
        "partial_never_sent": sum(1 for r in rows if r.get("partial_sent") is False),
        "fission_on": sum(1 for r in rows if (r.get("fission") or {}).get("fission_autostart") is True),
        "content_processes_at_the_start": dict(Counter((r["fission"] or {}).get("content_processes") for r in rows if r.get("fission"))),
        "final_page": dict(Counter(r["final_page"] for r in measured)),
        "second_request_arrived": {"yes": sum(1 for r in measured if r["second_request_arrived"]),
                                   "no": sum(1 for r in measured if not r["second_request_arrived"])},
        "released_by": dict(Counter(r["released_by"] for r in measured)),
        "final_loaded_ms": median_max([r["final_loaded_ms"] for r in measured if r.get("final_loaded_ms") is not None]),
        "second_request_after_load2_ms": median_max([r["second_request_after_load2_ms"] for r in measured
                                                     if r.get("second_request_after_load2_ms") is not None]),
        "launch_errors": dict(Counter(_first_line(r["launch_error"]) for r in rows if r.get("launch_error"))),
        "scenario_errors": dict(Counter(_first_line(r["scenario_error"]) for r in rows if r.get("scenario_error"))),
    }


def render(summary):
    out = ["== Firefox %s retail, Fission on: %d repetitions, %d launched, %d measured, Fission on in %d"
           % (VERSION, summary["repetitions"], summary["launched"], summary["measured"], summary["fission_on"])]
    pages = summary["final_page"]
    out.append("   final page: b.test/partial x%d, c.test/second x%d%s"
               % (pages.get("b.test/partial", 0), pages.get("c.test/second", 0),
                  "".join(", %s x%d" % kv for kv in sorted(pages.items()) if kv[0] not in ("b.test/partial", "c.test/second"))))
    s = summary["second_request_arrived"]
    out.append("   /second reached the server: yes %d, no %d" % (s["yes"], s["no"]))
    out.append("   final page loaded, ms after load 2: median %(median)s, max %(max)s" % summary["final_loaded_ms"])
    out.append("   /second request, ms after load 2: median %(median)s, max %(max)s" % summary["second_request_after_load2_ms"])
    out.append("   held response released by: %s" % (", ".join("%s x%d" % kv for kv in sorted(summary["released_by"].items())) or "-"))
    if summary["partial_never_sent"]:
        out.append("   the server never sent the start of /partial in %d repetition(s)" % summary["partial_never_sent"])
    for label, key in (("launch errors", "launch_errors"), ("scenario errors", "scenario_errors")):
        for message, count in summary[key].items():
            out.append("   %s: x%d  %s" % (label, count, message))
    return "\n".join(out)


# ---------------------------------------------------------------- the run

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", default=os.environ.get("FF"),
                        help="the Firefox to drive (default: $FF); without it, --fetch-dir downloads Firefox %s retail" % VERSION)
    parser.add_argument("--fetch-dir", help="download Firefox %s retail from archive.mozilla.org into this folder (sha256 checked against SHA256SUMS)" % VERSION)
    parser.add_argument("--repetitions", type=int, default=REPETITIONS, help="how many (default %d), a new browser and profile each" % REPETITIONS)
    parser.add_argument("--api", choices=("bc-uri", "bc-string", "browser"), default="bc-uri",
                        help="how the loads are given: browsingContext.loadURI(nsIURI) (default), browsingContext.loadURI(string), selectedBrowser.loadURI(nsIURI)")
    parser.add_argument("--out-dir", default="second-goto-retail", help="where the JSON is written")
    args = parser.parse_args(argv)

    retail = None
    binary = args.binary
    if args.fetch_dir:
        binary, retail = fetch_retail(args.fetch_dir)
    if not binary:
        parser.error("a Firefox is needed: --binary, $FF, or --fetch-dir")
    if not Path(binary).is_file():
        parser.error("no such binary: %s" % binary)
    build = build_of(binary)
    print("binary %s, build %s" % (binary, json.dumps(build)), flush=True)

    rows = []
    for index in range(1, args.repetitions + 1):
        row = _repetition(binary, index, args.api)
        rows.append(row)
        shown = ["launched", "final_page", "second_request_arrived", "second_request_after_load2_ms", "final_loaded_ms", "released_by",
                 "launch_error", "scenario_error"]
        print("[%d/%d] %s fission=%s processes=%s" % (index, args.repetitions, json.dumps({k: row.get(k) for k in shown}, default=str),
                                                      (row.get("fission") or {}).get("fission_autostart"),
                                                      (row.get("fission") or {}).get("content_processes")), flush=True)
    summary = summarise(rows)
    system = _system()
    payload = {
        "meta": {"browser": "Firefox %s retail (Mozilla), Fission on, driven by Marionette in the chrome context" % VERSION,
                 "system": system, "platform": platform.platform(), "python": sys.version.split()[0],
                 "binary": binary, "build": build, "download": retail, "prefs": PREFS, "load_api": args.api,
                 "hosts": "a.test, b.test, c.test -> the local server, by network.dns.localDomains",
                 "goto2_budget_s": GOTO2_BUDGET_S, "fresh_browser_and_profile_per_repetition": True,
                 "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        "summary": summary,
        "rows": rows,
    }
    path = Path(args.out_dir) / ("second-goto-retail-%s.json" % system)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")
    print("written", path, flush=True)
    print()
    print(render(summary), flush=True)
    # the exit code says the script could measure, not what it measured
    if summary["measured"] == 0:
        print("::error::no repetition could be measured", flush=True)
        return 1
    if summary["fission_on"] == 0:
        print("::error::Fission was never on: this is not the reference", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
