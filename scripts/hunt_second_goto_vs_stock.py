#!/usr/bin/env python3
"""A MEASUREMENT, not a gate: one scenario, run on this package's browser and on stock Playwright's Firefox, side by side.

The scenario is the one of tests/test_navigation_across_processes_e2e.py::test_a_second_goto_to_a_third_site_interrupts_the_first_and_lands:

  * sites isolated as in that file (`fission.autostart`, strategy 1), a local server on a thread, three hosts a.test, b.test and c.test
    on its one port, told to the browser by `network.dns.localDomains` (a standard Firefox pref: the same line reaches both browsers);
  * the page is at a.test/plain; goto 1 goes to b.test/partial, whose headers and first bytes the server sends and whose rest it HOLDS
    until the scenario is over; as soon as the client has heard the response of /partial, goto 2 goes to c.test/second
    (domcontentloaded, timeout 15 s).

Per repetition it records what the question "did the second goto leave the browser, and how did the two gotos end" needs:

  * whether the request of /second reached the server, and at how many ms after the start of goto 2 (the server and the client read the
    same clock, `time.perf_counter()`, in this one process), and whether it came before or after goto 2 had ended;
  * how goto 1 ended (the error and its class), and at how many ms;
  * how goto 2 ended (it returned, or the error), at how many ms.

A new BROWSER per repetition, not a new context: what the navigation depends on is the processes the browser has (the spare process it
preallocated, the one that holds a.test, the ones it starts for b.test and c.test); a new context of a warmed browser would inherit them and
every repetition after the first would measure another state. A cold browser per repetition is the state a user's first navigations have,
and it is the same for both. The two browsers alternate (ours, stock, ours, ...) so a runner that slows down in the middle of the run slows both.

Nothing here decides: the exit code is non-zero only when the script itself could not measure (a browser that never launched in any
repetition, a binary that is not there). The result is one JSON per browser and system and a summary printed to the log.

    python scripts/hunt_second_goto_vs_stock.py --binary "$FF" --out-dir out            # both browsers, 20 repetitions each
    python scripts/hunt_second_goto_vs_stock.py --browser stock --repetitions 3 --out-dir out

Stock Firefox is the one `python -m playwright install firefox` downloads for the `playwright` package installed (scripts/playwright_pin.txt).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingTCPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent

#: The prefs of the file the scenario comes from (FISSION_ON there): the same for both browsers.
FISSION_ON = {
    "fission.autostart": True,
    "fission.webContentIsolationStrategy": 1,
    "dom.ipc.processCount.webIsolated": 4,
    "network.dns.localDomains": "a.test,b.test,c.test",
}

SEED = 42                      # the seed of the test this comes from (only ours reads it)
REPETITIONS = 20
GOTO1_TIMEOUT_MS = 30000       # the first goto ends when the second replaces it, or when the held answer is let go
GOTO2_TIMEOUT_MS = 15000       # as asked: domcontentloaded, 15 s
HEAR_RESPONSE_S = 15.0         # how long the client waits to hear the response of /partial before it gives the repetition up
SETTLE_S = 1.0                 # after the end of the scenario and the release: a /second that comes late is still counted
LAUNCH_S = 90.0                # a launch that takes longer is a repetition lost, not a hang
CLOSE_S = 30.0
REPETITION_S = 150.0           # the whole scenario of one repetition: nothing may hold the run


def _ms(seconds):
    return None if seconds is None else round(seconds * 1000.0, 1)


class _Server(ThreadingTCPServer):
    """A thread per connection and daemon threads: a connection the browser opens and leaves cannot hold the server or its shutdown."""
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        ThreadingTCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class Sites:
    """The local server of one repetition. `arrived` has the time (perf_counter) each path was first asked for."""

    def __init__(self):
        self.arrived = {}                 # path -> perf_counter of the first request
        self.release = threading.Event()  # what the server holds until the scenario is over
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
                        # the headers and the start of the body arrive, the rest is held: the response is there, the navigation has
                        # changed process, the document is not complete
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html")
                        self.send_header("Content-Length", "200")
                        self.end_headers()
                        self.wfile.write(b"<!doctype html><title>partial</title><p>start")
                        self.wfile.flush()
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


# ---------------------------------------------------------------- the two browsers

class _Ours:
    """invisible_playwright, the published engine ($FF), the prefs of the scenario."""
    name = "invisible"

    def __init__(self, binary):
        self.binary = binary
        self._cm = None

    async def launch(self):
        from invisible_playwright.async_api import InvisiblePlaywright

        self._cm = InvisiblePlaywright(seed=SEED, binary_path=self.binary, humanize=False, headless=True, extra_prefs=FISSION_ON)
        browser = await self._cm.__aenter__()
        return browser

    async def close(self):
        cm, self._cm = self._cm, None
        if cm is not None:
            await cm.__aexit__(None, None, None)


class _Stock:
    """Stock Playwright with its own Firefox: no engine of ours, no seed, the same prefs."""
    name = "stock"

    def __init__(self, binary=None):
        self._pw = None
        self._browser = None

    async def launch(self):
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.firefox.launch(headless=True, firefox_user_prefs=dict(FISSION_ON))
        return self._browser

    async def close(self):
        browser, self._browser = self._browser, None
        pw, self._pw = self._pw, None
        try:
            if browser is not None:
                await browser.close()
        finally:
            if pw is not None:
                await pw.stop()


# ---------------------------------------------------------------- one repetition

def _error_class(message):
    if message is None:
        return None
    if "is interrupted by another navigation" in message:
        return "interrupted by another navigation"
    if "NS_BINDING_ABORTED" in message:
        return "NS_BINDING_ABORTED"
    if "Timeout" in message and "exceeded" in message:
        return "timeout"
    if "load not reached" in message:
        return "load not reached"
    return "other"


async def _scenario(page, sites):
    """The measurement. Every time is perf_counter; the row says them in ms from the start of goto 1 (`t=0`)."""
    heard = {}                                              # path -> perf_counter the client heard its response
    page.on("response", lambda r: heard.setdefault(urlsplit(r.url).path, time.perf_counter()))

    await page.goto(sites.at("a.test", "/plain"), wait_until="domcontentloaded", timeout=GOTO2_TIMEOUT_MS)

    ends = {}

    async def goto1():
        try:
            await page.goto(sites.at("b.test", "/partial"), wait_until="domcontentloaded", timeout=GOTO1_TIMEOUT_MS)
            return None
        except Exception as e:
            return str(e)
        finally:
            ends["first"] = time.perf_counter()

    t_first = time.perf_counter()
    first = asyncio.ensure_future(goto1())
    deadline = time.monotonic() + HEAR_RESPONSE_S
    while "/partial" not in heard or "/partial" not in sites.arrived:
        if first.done() or time.monotonic() > deadline:
            break
        await asyncio.sleep(0.02)
    row = {"response_heard": "/partial" in heard}
    second_error = None
    second_status = None
    t_second = None
    if row["response_heard"]:
        t_second = time.perf_counter()
        try:
            response = await page.goto(sites.at("c.test", "/second"), wait_until="domcontentloaded", timeout=GOTO2_TIMEOUT_MS)
            second_status = response.status if response is not None else None
        except Exception as e:
            second_error = str(e)
        ends["second"] = time.perf_counter()
    # the rest of the first response is sent only now: it can have ended only by being replaced (or, if it did not, by this)
    sites.release.set()
    try:
        first_error = await asyncio.wait_for(first, 20)
    except asyncio.TimeoutError:
        first.cancel()
        first_error = "(goto 1 still pending 20 s after the held response was let go)"
        ends.setdefault("first", time.perf_counter())
    await asyncio.sleep(SETTLE_S)

    def at(stamp):
        return None if stamp is None else _ms(stamp - t_first)

    second_request = sites.arrived.get("/second")
    row.update({
        "partial_request_ms": at(sites.arrived.get("/partial")),
        "partial_response_heard_ms": at(heard.get("/partial")),
        "goto2_start_ms": at(t_second),
        "second_request_arrived": second_request is not None,
        "second_request_ms": at(second_request),
        "second_request_after_goto2_start_ms": None if second_request is None or t_second is None else _ms(second_request - t_second),
        "second_request_before_goto2_end": None if second_request is None or "second" not in ends else second_request <= ends["second"],
        "goto1_end_ms": at(ends.get("first")),
        "goto1_error": first_error,
        "goto1_error_class": _error_class(first_error) if first_error is not None else "none (it returned)",
        "goto2_outcome": None if t_second is None else ("error" if second_error is not None else "commit"),
        "goto2_ms": None if t_second is None else _ms(ends["second"] - t_second),
        "goto2_error": second_error,
        "goto2_error_class": _error_class(second_error),
        "goto2_status": second_status,
        "url_after": page.url,
        "requests_at_the_server": sorted(sites.arrived),
    })
    return row


async def _repetition(maker, index, binary):
    row = {"repetition": index, "browser": maker.name, "launched": False}
    sites = Sites()
    kind = maker(binary)
    try:
        try:
            browser = await asyncio.wait_for(kind.launch(), LAUNCH_S)
            row["launched"] = True
            try:
                row["browser_version"] = browser.version
            except Exception:
                pass
        except Exception as e:
            row["launch_error"] = "%s: %s" % (type(e).__name__, e)
            return row
        try:
            page = await browser.new_page()
            row.update(await asyncio.wait_for(_scenario(page, sites), REPETITION_S))
        except Exception as e:
            row["scenario_error"] = "%s: %s" % (type(e).__name__, e)
    finally:
        sites.close()
        try:
            await asyncio.wait_for(kind.close(), CLOSE_S)
        except BaseException as e:
            row["close_error"] = "%s: %s" % (type(e).__name__, e)
    return row


# ---------------------------------------------------------------- the summary

def _first_line(message):
    return (message or "").strip().splitlines()[0][:160] if message else None


def summarise(rows):
    measured = [r for r in rows if "second_request_arrived" in r]
    gotos2 = [r for r in measured if r.get("goto2_outcome")]
    ms2 = [r["goto2_ms"] for r in gotos2]
    asked = [r["second_request_after_goto2_start_ms"] for r in measured if r.get("second_request_after_goto2_start_ms") is not None]

    def median_max(values):
        return {"median": round(statistics.median(values), 1), "max": round(max(values), 1)} if values else {"median": None, "max": None}

    return {
        "repetitions": len(rows),
        "launched": sum(1 for r in rows if r.get("launched")),
        "measured": len(measured),
        "never_heard_the_response_of_the_first": sum(1 for r in measured if not r["response_heard"]),
        "second_request_arrived": {"yes": sum(1 for r in measured if r["second_request_arrived"]),
                                   "no": sum(1 for r in measured if not r["second_request_arrived"])},
        "second_request_arrived_after_goto2_ended": sum(1 for r in measured if r.get("second_request_before_goto2_end") is False),
        "goto2": {"commit": sum(1 for r in gotos2 if r["goto2_outcome"] == "commit"),
                  "error": sum(1 for r in gotos2 if r["goto2_outcome"] == "error")},
        "goto2_errors": dict(Counter(_first_line(r["goto2_error"]) for r in gotos2 if r["goto2_error"])),
        "goto1_end": dict(Counter(r["goto1_error_class"] for r in measured if "goto1_error_class" in r)),
        "goto1_errors": dict(Counter(_first_line(r["goto1_error"]) for r in measured if r.get("goto1_error"))),
        "goto2_ms": median_max(ms2),
        "second_request_after_goto2_start_ms": median_max(asked),
        "launch_errors": dict(Counter(_first_line(r["launch_error"]) for r in rows if r.get("launch_error"))),
        "scenario_errors": dict(Counter(_first_line(r["scenario_error"]) for r in rows if r.get("scenario_error"))),
    }


def render(name, summary):
    out = ["== %s: %d repetitions, %d launched, %d measured" % (name, summary["repetitions"], summary["launched"], summary["measured"])]
    s = summary["second_request_arrived"]
    out.append("   /second reached the server: yes %d, no %d (of the yes, %d after goto 2 had ended)"
               % (s["yes"], s["no"], summary["second_request_arrived_after_goto2_ended"]))
    g = summary["goto2"]
    out.append("   goto 2: committed %d, error %d" % (g["commit"], g["error"]))
    out.append("   goto 2 ms: median %(median)s, max %(max)s" % summary["goto2_ms"])
    out.append("   /second request, ms after goto 2 started: median %(median)s, max %(max)s" % summary["second_request_after_goto2_start_ms"])
    out.append("   goto 1 ended as: %s" % (", ".join("%s x%d" % kv for kv in sorted(summary["goto1_end"].items())) or "-"))
    for label, key in (("goto 2 errors", "goto2_errors"), ("goto 1 errors", "goto1_errors"), ("launch errors", "launch_errors"),
                       ("scenario errors", "scenario_errors")):
        for message, count in summary[key].items():
            out.append("   %s: x%d  %s" % (label, count, message))
    if summary["never_heard_the_response_of_the_first"]:
        out.append("   the client never heard the response of the first goto in %d repetition(s)"
                   % summary["never_heard_the_response_of_the_first"])
    return "\n".join(out)


# ---------------------------------------------------------------- the run

def _versions():
    found = {}
    for module, label in (("playwright", "playwright"), ("invisible_playwright", "invisible_playwright"), ("invisible_core", "invisible_core")):
        try:
            found[label] = __import__(module).__version__
        except Exception:
            found[label] = None
    try:
        from importlib.metadata import version
        found["playwright"] = version("playwright")
    except Exception:
        pass
    return found


def _declare_the_lab(directory):
    """Where an e2e launch leaves from is declared, as the e2e suite does (tests/_lab_egress.py): no service of the internet is asked."""
    sys.path.insert(0, str(ROOT / "tests"))
    import pytest

    import _lab_egress

    patch = pytest.MonkeyPatch()
    _lab_egress.install(patch, Path(directory))
    return patch


async def _run(makers, repetitions, binary, out_dir):
    rows = {m.name: [] for m in makers}
    for index in range(1, repetitions + 1):
        for maker in makers:                       # alternate: ours, stock, ours, stock, ...
            row = await _repetition(maker, index, binary)
            rows[maker.name].append(row)
            print("[%s %d/%d] %s" % (maker.name, index, repetitions, json.dumps({
                k: row.get(k) for k in ("launched", "second_request_arrived", "second_request_after_goto2_start_ms", "goto1_error_class",
                                        "goto2_outcome", "goto2_ms", "launch_error", "scenario_error")}, default=str)), flush=True)
    system = {"win32": "windows", "linux": "linux", "darwin": "macos"}.get(sys.platform, sys.platform)
    result = {}
    for maker in makers:
        summary = summarise(rows[maker.name])
        payload = {
            "meta": {"browser": maker.name, "system": system, "platform": platform.platform(), "python": sys.version.split()[0],
                     "versions": _versions(), "binary": binary if maker.name == "invisible" else "the Firefox of `playwright install firefox`",
                     "prefs": FISSION_ON, "hosts": "a.test, b.test, c.test -> the local server, by network.dns.localDomains",
                     "goto1_timeout_ms": GOTO1_TIMEOUT_MS, "goto2_timeout_ms": GOTO2_TIMEOUT_MS,
                     "fresh_browser_per_repetition": True, "browsers_alternate": len(makers) > 1,
                     "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            "summary": summary,
            "rows": rows[maker.name],
        }
        path = Path(out_dir) / ("second-goto-%s-%s.json" % (maker.name, system))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        print("written", path, flush=True)
        result[maker.name] = summary
    print()
    for maker in makers:
        print(render(maker.name, result[maker.name]), flush=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--browser", choices=("both", "invisible", "stock"), default="both",
                        help="which browser: both alternate, repetition by repetition (default)")
    parser.add_argument("--binary", default=os.environ.get("INVPW_BINARY_PATH") or os.environ.get("FF"),
                        help="the engine of invisible_playwright (default: $INVPW_BINARY_PATH, then $FF)")
    parser.add_argument("--repetitions", type=int, default=REPETITIONS, help="per browser (default %d)" % REPETITIONS)
    parser.add_argument("--out-dir", default="second-goto", help="where the JSON of each browser is written")
    args = parser.parse_args(argv)

    makers = {"both": [_Ours, _Stock], "invisible": [_Ours], "stock": [_Stock]}[args.browser]
    if _Ours in makers and not args.binary:
        parser.error("the engine of invisible_playwright is needed: --binary, or $INVPW_BINARY_PATH / $FF")
    with tempfile.TemporaryDirectory(prefix="second-goto-") as lab:
        patch = _declare_the_lab(lab) if _Ours in makers else None
        try:
            result = asyncio.run(_run(makers, args.repetitions, args.binary, args.out_dir))
        finally:
            if patch is not None:
                patch.undo()
    # the exit code says the script could measure, not what it measured
    dead = [name for name, s in result.items() if s["measured"] == 0]
    if dead:
        print("::error::no repetition of %s could be measured" % ", ".join(dead), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
