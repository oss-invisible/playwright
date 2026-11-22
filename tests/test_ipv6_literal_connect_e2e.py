"""An IPv6 literal a page asks for is judged by the declared network, and a destination behind a SOCKS proxy is left to the proxy.

What this fixes in place (characterisation of the published engine firefox-68; the engine hook is `nsSocketTransport::InitiateSocket` in
`netwerk/base/nsSocketTransport2.cpp`, the decision is `StealthConnect` / `StealthConnectError` in `netwerk/base/StealthIPv6Literal.cpp`, series folder
`network/ipv6-literal`):

* The session declares its host network in `STEALTHFOX_WEBRTC_NETWORK` (`v6=0`: no IPv6). A hostname then never resolves to AAAA, but a LITERAL
  (`http://[2001:db8::1]/`) is handed to the socket by Firefox without any lookup, and the route table of the machine under the browser would decide.
  The engine decides instead: a direct connection to a routable IPv6 literal fails before `connect()` is called, the way a Windows machine that has the
  IPv6 stack and no IPv6 route fails it, and nothing is sent. The client reads it as `NS_ERROR_CONNECTION_REFUSED` (a failed `fetch` is a `TypeError`,
  and `requestfailed` carries that text). A page reads this: whether `fetch("http://[<global IPv6>]/")` fails at once or hangs, and with which error, is
  the first thing that tells a machine with an IPv6 route from one without.
* An IPv4-mapped literal (`::ffff:127.0.0.1`) is refused on every system and whatever the declaration says: Windows refuses `connect()` to a mapped
  address on a socket that is IPv6-only (Firefox's are), and a dual-stack Linux socket would connect over IPv4. The listener below never sees it.
* A destination behind a SOCKS proxy is never judged: the proxy reaches it, not this host. The same literal that is refused with no proxy reaches the
  proxy as a CONNECT whose destination is that address and port, as an IPv6 address (SOCKS5 address type 4), never as a name. How many times the
  browser tries it (Mozilla retries a connection that did not answer at once) is Mozilla's code and is not fixed here: every CONNECT of the
  navigation to the documentation destination has that shape, and there is at least one.

Not clocks: "refused by the engine" and "timed out" are told apart by the error (`TypeError` with the failure text `NS_ERROR_CONNECTION_REFUSED`, against a
`TimeoutError` of the 30 s deadline of the fetch), and by the listener having heard nothing; no duration is read anywhere.

Negative controls, in the same test:

* the listener is real: the same address by its IPv4 literal (`127.0.0.1`) reaches it, once, in the launches where the mapped literal does not;
* the mapped literal is refused with IPv6 declared on as well as off (a rule of its own: the declaration decides the other literals, not this one);
* behind the proxy the documentation-prefix literal that is refused direct IS carried: an engine that judged it behind SOCKS (or that always passed
  `kDirect`) would make the proxy record no CONNECT, and the fetch would fail; and an engine that handed the proxy the literal in another shape (a
  name, an IPv4 address) fails the shape assertion.

What this test cannot tell, by design: with IPv6 declared on, a global IPv6 literal goes to the OS, whose answer depends on the machine (a runner with no
IPv6 route answers `ENETUNREACH`, the same `NS_ERROR_CONNECTION_REFUSED` as the engine's), so the documentation-prefix fetch is judged only in the
declared-off launch and behind the proxy. The port is 80: Firefox bans port 9 (discard) before any connect, so a fetch to it never reaches the gate.

Hermetic: a local listener, a local SOCKS5 gateway of `tests/_socks_servers.py` that maps the documentation address to the listener and forwards
NOTHING else: the browser's own background traffic also arrives at the gateway, and every CONNECT that is not the one carried to the listener is refused
(reply 2) and recorded. Every gateway of that module is hermetic the same way (it carries only the lab's, see its docstring); this one is narrower still:
the lab for it is the listener alone. Every request, refused or carried, is recorded with its time since the launch, its SOCKS5 address type, its destination
as received and its port. The refused list is written, always, when the gateway is closed, under `$RUNNER_TEMP/invpw-evidence/` (the folder the e2e jobs
upload), so that a reader sees what the browser asked for besides the literal.
"""
from __future__ import annotations

import socketserver
import threading
import time

import pytest

from _servers import LocalServer
from _socks_servers import EXIT, Gateway, _REQUEST
from _udp_line import egress_by_proxy, measure_servers
from _waits import wait_until
from test_declared_host_network_e2e import DUAL, NETWORK_ENV, V4_ONLY

pytestmark = pytest.mark.e2e

#: A global-unicast-looking address that no host routes (RFC 3849 documentation prefix): the gate answers for it, whatever the machine has.
DOCUMENTATION = "2001:db8::1"
DOCUMENTATION_HTTP = 80
#: What a failed connect of the engine is called by the browser (NSPR's PR_NETWORK_UNREACHABLE_ERROR and PR_ADDRESS_NOT_AVAILABLE_ERROR both end here).
REFUSED = "NS_ERROR_CONNECTION_REFUSED"
#: SOCKS5 address type of an IPv6 address (RFC 1928); 1 is IPv4, 3 a domain name.
ATYP_IPV6 = 4
#: The deadline of a fetch that must END (refused, or carried): far beyond a healthy run; what the page reads on a timeout is a `TimeoutError`, never a verdict.
FETCH_SECONDS = 30

_FETCH_JS = """async (url) => {
  try {
    const response = await fetch(url, {mode: 'no-cors', cache: 'no-store', signal: AbortSignal.timeout(%d)});
    return {ok: true, type: response.type};
  } catch (e) {
    return {ok: false, name: e.name, message: String(e.message)};
  }
}""" % (FETCH_SECONDS * 1000)


class _Listener:
    """A local HTTP-ish listener that remembers every connection it accepted and answers each with a tiny 200."""

    def __init__(self) -> None:
        self.connections: list = []
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                outer.connections.append(self.client_address)
                self.request.settimeout(FETCH_SECONDS)
                try:
                    self.request.recv(65536)
                    self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
                except OSError:
                    pass

        self.server = LocalServer(("127.0.0.1", 0), Handler)
        self.port: int = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def __enter__(self) -> "_Listener":
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


class _GatewayToALocalListener(Gateway):
    """The SOCKS5 gateway of the tests (UDP refused) that carries the documentation address to a local listener and forwards nothing else: the destination
    it is asked for is what it records in ``connects`` (with its SOCKS5 address type in ``addresses``), what the listener hears is what it carried, and
    every CONNECT that is not carried to the listener is refused (the base gateway refuses what is not the lab's; here the lab is the listener alone) and
    recorded in ``refused``."""

    evidence_name = "ipv6-literal-refused-connects"

    def __init__(self, carried: dict) -> None:
        self._carried = dict(carried)
        #: Every address of a request the gateway read, as (host, port, address type), in order.
        self.addresses: list = []
        #: Every request, as a dict, for the evidence file: ``t`` (seconds, monotonic, since ``launch_started``), ``atyp`` (SOCKS5 address type: 1 IPv4,
        #: 3 domain name, 4 IPv6), ``destination`` (as received: the NAME when the type is 3), ``port``, ``outcome``.
        self.records: list = []
        self._launch_t0 = time.monotonic()
        self._current = threading.local()
        super().__init__(udp="refuse", tcp_exit=EXIT, udp_exit=EXIT)

    @property
    def refused(self) -> list:
        """Every CONNECT that was refused, as (destination as received, port), in order."""
        return self.guard.refused("tcp")

    def launch_started(self) -> None:
        """Call just before the launch of the browser behind this gateway: the origin of the times of ``records``."""
        self._launch_t0 = time.monotonic()

    def _read_address(self, c):
        host, port = super()._read_address(c)
        atyp = _REQUEST.atyp
        self.addresses.append((host, port, atyp))
        record = {"t": round(time.monotonic() - self._launch_t0, 3), "atyp": atyp, "destination": host, "port": port,
                  "outcome": "answered by the lab (or not a CONNECT)"}
        self.records.append(record)
        self._current.record = record
        return host, port

    def _target(self, host: str, port: int):
        return self._carried.get((host, port), (host, port))

    def _admits(self, host, port, atyp, protocol, target=None):
        if (target or (host, port)) not in self._carried.values():
            self.guard.refuse(host, port, atyp, protocol, reason="not the listener of the lab this gateway maps a destination to")
            self._current.record["outcome"] = "refused"
            return False
        self._current.record["outcome"] = "carried to the listener of the lab"
        return super()._admits(host, port, atyp, protocol, target)

    def _evidence(self) -> dict:
        """The refusals are written by the base gateway at the close, with every request of the launch, so that a reader sees what the browser asked for."""
        return {"requests": self.records}

    def documentation_connects(self) -> list:
        """Every request of the navigation to the documentation destination, in whatever shape it arrived: the host is told by its digits."""
        return [(host, port, atyp) for host, port, atyp in self.addresses if "2001:db8" in host.lower()]


def _launch(firefox_binary, gateway=None):
    from invisible_playwright import InvisiblePlaywright

    kw = {"seed": 42, "binary_path": firefox_binary, "headless": True, "timezone": "America/New_York"}
    if gateway is not None:
        kw.update(proxy=gateway.proxy, udp_stun_servers=measure_servers(gateway))
    return InvisiblePlaywright(**kw)


def _try(page, failures, url, needle):
    """One fetch of ``url`` from the page: what the page read, and the failure texts the client reported for the request whose URL holds ``needle``."""
    result = page.evaluate(_FETCH_JS, url)
    if not result["ok"]:
        wait_until(lambda: [text for seen, text in failures if needle in seen], FETCH_SECONDS, lambda: page.wait_for_timeout(50))
    return result, [text for seen, text in failures if needle in seen]


def _page_of(browser):
    page = browser.new_context().new_page()
    failures: list = []
    page.on("requestfailed", lambda request: failures.append((request.url, request.failure)))
    page.goto("about:blank")
    return page, failures


def _direct_launch(firefox_binary, monkeypatch, declaration, listener, with_documentation_prefix):
    """One launch with no proxy and ``declaration``: the IPv4 literal of the listener (control), the mapped literal of the same listener, and, when
    asked, the documentation-prefix literal. Returns what was read and the connections the listener heard after each step."""
    monkeypatch.setenv(NETWORK_ENV, declaration)
    seen = {}
    with _launch(firefox_binary) as browser:
        page, failures = _page_of(browser)
        before = len(listener.connections)
        seen["control"] = _try(page, failures, "http://127.0.0.1:%d/" % listener.port, "127.0.0.1")
        wait_until(lambda: len(listener.connections) > before, FETCH_SECONDS, lambda: page.wait_for_timeout(50))
        seen["after_control"] = len(listener.connections) - before
        seen["mapped"] = _try(page, failures, "http://[::ffff:127.0.0.1]:%d/" % listener.port, "[::ffff:")
        seen["after_mapped"] = len(listener.connections) - before
        if with_documentation_prefix:
            seen["documentation"] = _try(page, failures, "http://[%s]:%d/" % (DOCUMENTATION, DOCUMENTATION_HTTP), "[%s]" % DOCUMENTATION)
    return seen


def test_a_direct_ipv6_literal_is_refused_and_one_behind_socks_is_left_to_the_proxy(firefox_binary, monkeypatch):
    from invisible_core import _geo

    with _Listener() as direct, _Listener() as behind:
        off = _direct_launch(firefox_binary, monkeypatch, V4_ONLY, direct, with_documentation_prefix=True)
        on = _direct_launch(firefox_binary, monkeypatch, DUAL, direct, with_documentation_prefix=False)

        # Behind a SOCKS5 gateway, IPv6 declared off as in the first launch: the documentation-prefix literal is the proxy's to reach.
        gateway = _GatewayToALocalListener({(DOCUMENTATION, DOCUMENTATION_HTTP): ("127.0.0.1", behind.port)})
        try:
            monkeypatch.setattr(_geo, "discover_egress_ip", egress_by_proxy(gateway))
            monkeypatch.setenv(NETWORK_ENV, V4_ONLY)
            gateway.launch_started()
            with _launch(firefox_binary, gateway) as browser:
                page, failures = _page_of(browser)
                proxied = _try(page, failures, "http://[%s]:%d/" % (DOCUMENTATION, DOCUMENTATION_HTTP), "[%s]" % DOCUMENTATION)
                wait_until(lambda: behind.connections, FETCH_SECONDS, lambda: page.wait_for_timeout(50))
            documentation = gateway.documentation_connects()
        finally:
            gateway.close()            # writes what it refused and every request, under $RUNNER_TEMP/invpw-evidence/

    # The listener is real: its IPv4 literal reaches it, once per launch.
    for name, seen in (("declared off", off), ("declared on", on)):
        control, _failed = seen["control"]
        assert control["ok"], "%s: the IPv4 literal of the local listener was not reached: %s" % (name, seen)
        assert seen["after_control"] == 1, "%s: the listener heard %d connections for one fetch of its IPv4 literal: %s" % (name, seen["after_control"], seen)

    # An IPv4-mapped literal is refused by the engine, declared off or on, and the listener hears nothing for it.
    for name, seen in (("declared off", off), ("declared on", on)):
        result, failed = seen["mapped"]
        assert result["ok"] is False and result["name"] == "TypeError", (
            "%s: the fetch of the mapped literal did not fail as a refused connection (a TimeoutError would be a connect that was attempted): %s" % (name, seen))
        assert failed == [REFUSED], "%s: the client reported %r for the mapped literal, not the engine's refusal: %s" % (name, failed, seen)
        assert seen["after_mapped"] == seen["after_control"], "%s: the listener heard a connection through the mapped literal: %s" % (name, seen)

    # With IPv6 declared off, a direct documentation-prefix literal is refused by the engine: the same failure as a machine with no IPv6 route.
    result, failed = off["documentation"]
    assert result["ok"] is False and result["name"] == "TypeError", (
        "declared off: the direct fetch of %s did not fail as a refused connection (a TimeoutError means a connect was attempted): %s" % (DOCUMENTATION, off))
    assert failed == [REFUSED], "declared off: the client reported %r for the direct documentation-prefix literal: %s" % (failed, off)

    # The same literal behind the SOCKS5 gateway is carried: every CONNECT of the navigation to the documentation destination is the IPv6 literal and port,
    # as an IPv6 address (how many the browser makes is Mozilla's), and the listener behind it answered.
    assert proxied[0] == {"ok": True, "type": "opaque"}, (
        "the fetch of %s behind the SOCKS5 gateway was not carried (it is refused with no proxy): %s; failures %s; gateway requests %s" % (
            DOCUMENTATION, proxied[0], proxied[1], gateway.addresses))
    assert documentation, "the gateway was asked for no CONNECT to %s:%d: %s" % (DOCUMENTATION, DOCUMENTATION_HTTP, gateway.addresses)
    assert set(documentation) == {(DOCUMENTATION, DOCUMENTATION_HTTP, ATYP_IPV6)}, (
        "every CONNECT to the documentation destination should carry the IPv6 literal %s port %d as an IPv6 address (type %d): %s" % (
            DOCUMENTATION, DOCUMENTATION_HTTP, ATYP_IPV6, documentation))
    assert (DOCUMENTATION, DOCUMENTATION_HTTP) not in gateway.refused, "the gateway refused the destination of the lab: %s" % gateway.refused
    assert len(behind.connections) >= 1, "the listener behind the gateway heard nothing"
