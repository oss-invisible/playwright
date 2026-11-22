"""The telemetry ping sent at shutdown is sent by the engine's own background task, `firefox --backgroundtask pingsender` (firefox-68, Linux and Windows).

What it fixes, in the published engine (firefox-68), for the hooks of `stillborn/telemetry` (`toolkit/components/telemetry/app/TelemetrySend.sys.mjs`,
`pings/BackgroundTask_pingsender.sys.mjs`, the own files `StealthPingsender.sys.mjs` and `StealthPingsenderRoute.sys.mjs`): the shutdown ping
never goes through `pingsender[.exe]` (a separate program that takes its road from the host: WinINet's proxy settings on Windows, libcurl's
environment on Linux) but through the engine itself, with necko, by the road of the session. A server that receives a ping sees who sent it: the
background task is the one whose request says `User-Agent: pingsender/2.0`. The program of the host says another one, and a ping that
left from the host's own address says where the browser is. The server of telemetry is a pref (`toolkit.telemetry.server`), so the test points it at a
server of its own on 127.0.0.1: nothing leaves the machine.

The address the test gives the session is `http://localhost:<port>`, and that is part of what the engine does: the background task keeps the allowlist the
standalone pingsender had (`ALLOWED_ENDPOINTS` in `BackgroundTask_pingsender.sys.mjs`: `localhost` and `incoming.telemetry.mozilla.org`), so a ping addressed to
any other host, `127.0.0.1` included, is refused by the task before a request is made and the ping stays on disk.

What the test does. A session (seed 42, no proxy; the road of the session is decided by the launch) with the telemetry server pref at the fake endpoint
is opened and closed. The session ENDS (`_session_end.EndingPlaywright`: the reaper has finished, so the sender is finished or ended), then the test
reads what the endpoint received.

The grace is the product's own (invisible-core, `juggler/_profile.SENDER_GRACE`, 10 s), not one of the test. `Connection.close` returns when the browser
has exited (the sender is left running: `process.outlives_the_browser`) and starts the launch's reaper, `process.Reaper(tree, grace=SENDER_GRACE)`; from
there the reaper waits up to the grace for the tree to empty by itself, then ends what is left (the process group: SIGTERM, SIGKILL 5 s later; on
Windows the launch's job) and removes the session's profile. A sender still running at the end of the grace is killed and its ping is lost with the
profile: a user's close does the same. The sender is started by the browser during its own shutdown, so the grace counts what is left of its run
after the browser has gone (measured by the core: 0.6 s on Linux, 1.0 s on Windows). So the ping is asserted where the product puts it: on the
endpoint when the session has ended, with no wait of the test after it. The endpoint records a request before it answers, and the sender ends only
after the answer, so a ping that the sender delivered is on the record by then.

What it asserts:
* at least one request reached the endpoint with `User-Agent: pingsender/2.0`, a POST to a path of the telemetry protocol (`/submit/telemetry/...`),
  with a body;
* a second endpoint that the session was NOT pointed at received nothing (the pref is the address, there is no other).

Negative controls, each a session that must send nothing with that user agent: (1) the same session with `datareporting.healthreport.uploadEnabled`
false (the switch retail Firefox has for it); (2) the same session pointed at an endpoint whose host is `127.0.0.1`, which the task's allowlist refuses.
The test therefore fails when the ping leaves by another program (its user agent differs), when the session sends none, and when the allowlist stops
refusing a host that is not on it.
"""
from __future__ import annotations

import http.server
import threading

import pytest

from _session_end import EndingPlaywright

pytestmark = pytest.mark.e2e

SERVER = "toolkit.telemetry.server"
UPLOAD = "datareporting.healthreport.uploadEnabled"
BACKGROUND_TASK_AGENT = "pingsender/2.0"


class Endpoint:
    """A telemetry endpoint on 127.0.0.1 (a port the OS gives) that records every request and answers 200."""

    def __init__(self, host="localhost"):
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            timeout = 10

            def _record(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append({"method": self.command, "path": self.path, "agent": self.headers.get("User-Agent"),
                                       "bytes": len(body)})
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_POST = do_PUT = do_GET = _record

            def log_message(self, *args):
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.url = "http://%s:%d" % (host, self._server.server_address[1])

    def by_agent(self, agent):
        return [r for r in self.requests if r["agent"] == agent]

    def close(self):
        self._server.shutdown()
        self._server.server_close()


def session(binary, endpoint, extra_prefs=None):
    """One session pointed at ``endpoint``, opened and closed; it has ended (the sender included) when this returns."""
    prefs = {SERVER: endpoint.url}
    prefs.update(extra_prefs or {})
    with EndingPlaywright(seed=42, binary_path=binary, headless=True, humanize=False, locale="en-US",
                          timezone="America/New_York", extra_prefs=prefs) as browser:
        page = browser.new_page()
        page.goto("about:blank", timeout=60_000)


def test_the_shutdown_ping_is_sent_by_the_background_task_and_not_by_the_host_program(firefox_binary):
    endpoint, elsewhere = Endpoint(), Endpoint()
    try:
        session(firefox_binary, endpoint)
        # The session has ended: the sender has finished within the product's grace, or the reaper killed it and its ping is lost. Nothing is
        # waited for after this point.
        pings = endpoint.by_agent(BACKGROUND_TASK_AGENT)
        seen = [(r["method"], r["path"][:60], r["agent"], r["bytes"]) for r in endpoint.requests]
        assert pings, ("no request with `User-Agent: %s` had reached the endpoint when the session ended: the background task did not deliver "
                       "the shutdown ping within the core's grace (`SENDER_GRACE`, after which the reaper kills it); what the endpoint received: %s"
                       % (BACKGROUND_TASK_AGENT, seen))
        assert all(r["method"] == "POST" and r["path"].startswith("/submit/telemetry/") and r["bytes"] > 0 for r in pings), pings
        assert elsewhere.requests == [], "an endpoint the session was not pointed at received %s" % (elsewhere.requests,)

        # Control 1: with the upload switched off no ping leaves, so the request above is the product's and not the endpoint's own noise.
        control = Endpoint()
        try:
            session(firefox_binary, control, extra_prefs={UPLOAD: False})
            assert control.by_agent(BACKGROUND_TASK_AGENT) == [], (
                "a session with the upload switched off still sent a ping: %s" % (control.requests,))
        finally:
            control.close()

        # Control 2: the same session, same upload switch, the endpoint named by an address that is not on the task's allowlist: the task refuses it.
        refused = Endpoint(host="127.0.0.1")
        try:
            session(firefox_binary, refused)
            assert refused.requests == [], (
                "a ping addressed to a host that is not on the background task's allowlist reached the endpoint: %s" % (refused.requests,))
        finally:
            refused.close()
    finally:
        endpoint.close()
        elsewhere.close()
