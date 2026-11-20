"""HTTP/3 behind a proxy: visits of one page read the protocols a retail Firefox reads, road by road.

⛔ THE KNOWN-BAD INPUT: an engine that refuses HTTP/3 whenever a proxy is in use reads `h2` on every visit behind a SOCKS5 gateway
that relays UDP where the same Firefox reads `h3` after the first visits; one that reads `h3` at the FIRST visit of THIS
fixture (see below: it is not a retail fact, it is a property of the fixture); and one that tries h3 over a UDP that goes nowhere
makes the page wait for a timeout before it falls back to h2. That wait is NOT measured (how long a fallback takes is the speed of the
machine, and a loaded runner takes seconds for nothing): the attempt is, where it is born, as a datagram that reaches the UDP port of
the server (``H3Server.udp_datagrams``, ``_udp_line.h3_attempts``), which is empty on every road where UDP is dropped.

THE REFERENCE is a retail Firefox 159 on Windows, measured with DoH forced like our core (15 sessions per condition, one
profile per session). With UDP free the first runs read h2/h2/h3 in 9 sessions, h2/h2/h2 in 4 and h2/h3/h3 in 2, and h3 does
not have to come at the second visit. THE THIRD RUN (Windows, DoH and UDP free) READ h3 AT THE FIRST VISIT IN 3 SESSIONS OF 15
(h3, h2, h2): behind a proxy "visit 1 is always h2" is NOT a retail fact. With UDP blocked it is h2/h2/h2 in 15 of 15, with no
attempt at h3: the only outcome of the retail that repeats every time. With no
proxy and the native DNS (measured in loopback) it is h2, h3, h3. The method is the retail's: one context, the same page again
and again (``?v=N`` keeps each visit off the HTTP cache), after each load a wait (with a deadline) until the server has seen the QUIC connection that the
Alt-Svc it carries makes, on the roads where UDP is free, and a pause of two seconds, and the ``nextHopProtocol`` of the navigation entry of the document,
``performance.getEntriesByType('navigation')[0].nextHopProtocol``. What each road reads is in ``_udp_line.expected_protocols``
and is judged by ``_udp_line.judge_visits``.

WHY VISIT 1 IS h2 HERE. The retail reads h3 at the first visit when the DNS answer carries the HTTPS record (type 65, with
``alpn=h3``): the browser then knows before it connects. The server of these tests is reached by its ADDRESS, so no name is
looked up and no HTTPS record exists; and the FakeDoh of the tests (tests/_doh_servers.py) never serves a type 65 with data
(``FakeDoh.https_served`` stays empty; each test below asserts it, or that the page was reached by an address). That is what
makes the first visit h2 in a hermetic test, and only that: a FakeDoh that learns to serve type 65 changes the expectation
for the name it serves, and this test says so before it goes red on a protocol.

THE VISITS. No proxy: three, exactly h2, h3, h3. Behind a proxy: FIVE, on every road (the same number for all, for symmetry), and
all five are done (no early stop: the judge is a pure function of the list, so it is deterministic). On the road with UDP free
the first visit must be h2 (the fixture has no HTTPS record), at least one visit must be h3 within the five, and once a visit is
h3 the next ones are h3 too; the second visit is not required to be h3. On the roads without usable UDP every one of the five is h2 and not one datagram reaches the UDP port of the server. Time: about 3 s a visit (load plus the pause), so about 15 s of browsing per
proxy test and 9 s for the one with no proxy.

THE ROADS. A proxy whose UDP the core MEASURED ``coherent`` (a STUN Binding through its ASSOCIATE came back from the address of
its TCP exit) carries QUIC through the relay, the server sees the relay and never this host, and the page ends up on h3 as the
retail does with UDP free. Every other proxy is "a router that drops UDP", and the engine acts on it: the SOCKS5 proxy
that refuses UDP (``no``), whose UDP leaves from another address (``no``) or that never answers (``unmeasured``), and an HTTP
proxy (which has no UDP at all): h2 on every visit, nothing QUIC reaches the server (not even a datagram of an attempt that fails), no datagram of the page goes to a relay,
so no visit has an attempt to wait on. The core's measurement is not replaced: it asks the relay of each gateway for a Binding for real,
at the server the launch names (``udp_stun_servers``), and the test says what the launch declared.

The server is local, reached by its address (an address of this host that is not loopback: a loopback destination goes direct
whatever the proxy), with a self-signed certificate (the context ignores certificate errors); its TCP side speaks HTTP/2 and
carries ``Alt-Svc: h3`` (tests/_h3_server.py).
"""
from __future__ import annotations

import os
import socket

import pytest

from _host_address import this_host_address
from _waits import wait_until
from _udp_line import (declared_udp, egress_by_proxy, expected_protocols, h3_attempts, judge_visits, measure_servers,
                       no_https_record, relay_activity, visits_for)

pytestmark = [pytest.mark.e2e, pytest.mark.usefixtures("the_udp_line")]

#: What the product's own probe of the egress is told when there is no proxy: RFC 5737 TEST-NET-3.
EXIT_DIRECT = "203.0.113.7"
#: What the core is expected to have measured, by road.
_DECLARED = {"socks5_coherent": "coherent", "socks5_no_udp": "no", "socks5_mismatched": "no", "socks5_unmeasured": "unmeasured"}
#: The pause after each load (the retail method), and the nextHopProtocol of the document.
PAUSE_MS = 2000
#: The roads where QUIC can reach the server, so a QUIC connection of the browser is what its Alt-Svc turns into.
_UDP_FREE_ROADS = ("direct", "socks5_coherent")
_NAV_JS = "() => performance.getEntriesByType('navigation')[0].nextHopProtocol"


def _binary():
    cand = os.environ.get("STEALTHFOX_E2E_BINARY") or os.environ.get("INVPW_BINARY_PATH")
    if not cand or not os.path.exists(cand):
        pytest.skip("no patched binary (set STEALTHFOX_E2E_BINARY)")
    return cand


def _gateway(road: str, host: str):
    from _socks_servers import coherent, mismatched, silent_udp, tcp_only
    make = {"socks5_coherent": coherent, "socks5_no_udp": tcp_only, "socks5_mismatched": mismatched,
            "socks5_unmeasured": silent_udp}[road]
    # The relay sits on the address of this host, as a real gateway's does: the engine's UDP sockets are bound to an interface.
    # Its datagrams leave from that address too, on every system: Windows cannot send from 127.0.0.1 to an address of its own host
    # (WinError 10051, which this test once read as "the server saw QUIC from []"). The relay is told from a client that came direct
    # by the PORT it sends from (``gateway.exit_ports``), and it forwards only to this host: the browser's own QUIC to the Internet
    # stays in the lab.
    gateway = make(user="alice", password="s3cret", relay_host=host, exit_host=host)
    gateway.forward_only_to = {host}
    return gateway


def _visits(road, server, monkeypatch, discover, relay=None, trace=None, **launch):
    """Visit the page of ``server`` ``visits_for(road)`` times in one context; the nextHopProtocol of the document of each visit.
    With a SOCKS5 gateway as ``relay``, ``trace["before"]`` is what it had been asked when the first visit began (after the core's own
    measurement of it): the base of ``h3_attempts``."""
    from invisible_core import _geo
    from invisible_playwright import InvisiblePlaywright

    monkeypatch.setattr(_geo, "discover_egress_ip", discover)
    protocols = []
    # Firefox turns HTTP/3 off when the certificate chain ends in a root that is not one of its own (a self-signed test
    # server always does): measured, the Alt-Svc is learned, the QUIC handshake of validation succeeds with the override and
    # Firefox then closes it itself (hasThirdPartyRoots), so the visits stay h2. A test with a certificate of its own turns
    # that check off; the product does not (it would change what Firefox does with a root it does not know).
    prefs = {"network.http.http3.disable_when_third_party_roots_found": False, **launch.pop("extra_prefs", {})}
    with InvisiblePlaywright(headless=True, binary_path=_binary(), timezone="America/New_York", locale="en-US",
                             extra_prefs=prefs, **launch) as browser:
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        if relay is not None:
            trace["before"] = relay_activity(relay)
        for visit in range(1, visits_for(road) + 1):
            url = server.url if visit == 1 else "%s?v=%d" % (server.url, visit)
            page.goto(url, wait_until="load", timeout=60000)
            if road in _UDP_FREE_ROADS:
                # The Alt-Svc of the document is absorbed when the browser has made its own QUIC connection to the server (the
                # server sees it): the next visit reads h3 from then on, and not before. Waited for as a condition with a
                # deadline, not slept for: on a loaded machine that connection comes later than any pause. A road whose UDP
                # leads nowhere has nothing to wait for (every visit is h2 and no QUIC arrives): it keeps the pause.
                wait_until(lambda: server.quic_peers, 30, lambda: page.wait_for_timeout(100))
            page.wait_for_timeout(PAUSE_MS)           # let the later resources settle
            protocols.append(page.evaluate(_NAV_JS))
    return protocols


def _report(road, protocols, server, gateway=None):
    said = ("the %d visits of road %s read %s (a retail Firefox reads %s); the server answered TCP with "
            "ALPN %s, saw QUIC from %s and %d UDP datagrams on its h3 port" % (len(protocols), road, protocols, expected_protocols(road),
                                                                              server.tcp_alpn, server.quic_peers, len(server.udp_datagrams)))
    if gateway is not None:
        said += "; datagrams the relay carried: %s" % sorted({(h, p) for h, p, _data in gateway.datagrams})
        if gateway.send_errors:
            said += "; sends of the relay that failed: %s" % sorted(set(gateway.send_errors))
    return said


def test_a_page_with_no_proxy_reads_h2_then_h3_then_h3(monkeypatch):
    import aioquic  # noqa: F401 - the dev extra installs it: a missing one is a failure, never a skip
    from _h3_server import H3Server

    with H3Server(this_host_address()) as server:
        assert not no_https_record(server.address), no_https_record(server.address)
        protocols = _visits("direct", server, monkeypatch, egress_by_proxy(direct=EXIT_DIRECT))
        problems = judge_visits("direct", protocols)
        assert not problems, "; ".join(problems) + ". " + _report("direct", protocols, server)
        assert server.quic_peers, "the h3 visits did not reach the server by QUIC"


def test_a_page_behind_a_gateway_that_relays_udp_starts_on_h2_reaches_h3_within_five_visits_and_the_quic_goes_by_the_relay(monkeypatch):
    import aioquic  # noqa: F401 - the dev extra installs it: a missing one is a failure, never a skip
    from _h3_server import H3Server

    host = this_host_address()
    with H3Server(host) as server, _gateway("socks5_coherent", host) as gateway:
        assert not no_https_record(host), no_https_record(host)
        protocols = _visits("socks5_coherent", server, monkeypatch, egress_by_proxy(gateway), proxy=gateway.proxy,
                                  udp_stun_servers=measure_servers(gateway))
        carried = [(h, p) for h, p, _data in gateway.datagrams if (h, p) == (host, server.port)]
        declared = declared_udp(gateway.proxy)
        said = _report("socks5_coherent", protocols, server, gateway)
    assert declared == "coherent", "the core measured the gateway %s, not coherent: the engine would not use its relay. %s" % (declared, said)
    problems = judge_visits("socks5_coherent", protocols)
    assert not problems, "; ".join(problems) + ". " + said
    assert carried, "the QUIC datagrams did not go by the relay of the gateway. " + said
    assert not gateway.send_errors, "the relay of the gateway could not send. " + said
    assert server.quic_peer_addrs and {a for a, _p in server.quic_peer_addrs} == {host}         and {p for _a, p in server.quic_peer_addrs} <= set(gateway.exit_ports), (
        "the QUIC server saw %s: only the relay of the gateway (%s, from the ports %s) may reach it. %s" % (
            server.quic_peer_addrs, host, gateway.exit_ports, said))


@pytest.mark.parametrize("road", ["socks5_no_udp", "socks5_mismatched", "socks5_unmeasured"])
def test_a_page_behind_a_socks5_proxy_that_is_not_coherent_reads_h2_on_every_visit_and_never_tries_h3(monkeypatch, road):
    """A relay that is refused, that leaves from another address or that never answers is a router that drops UDP: every
    visit (five) is h2, nothing QUIC reaches the server, nothing of the page goes to the relay, and no attempt at h3 is made: the
    engine asks the gateway for no UDP ASSOCIATE and sends no datagram into its relay while the page is visited (nothing is measured in time)."""
    import aioquic  # noqa: F401 - the dev extra installs it: a missing one is a failure, never a skip
    from _h3_server import H3Server

    host = this_host_address()
    trace = {}
    with H3Server(host) as server, _gateway(road, host) as gateway:
        assert not no_https_record(host), no_https_record(host)
        protocols = _visits(road, server, monkeypatch, egress_by_proxy(gateway), relay=gateway, trace=trace, proxy=gateway.proxy,
                                  udp_stun_servers=measure_servers(gateway))
        attempts = h3_attempts(server, gateway, trace["before"])
        to_the_server = [(h, p) for h, p, _data in gateway.datagrams if (h, p) == (host, server.port)]
        declared = declared_udp(gateway.proxy)
        said = _report(road, protocols, server, gateway)
    assert declared == _DECLARED[road], "the core declared udp=%s, the road is %s. %s" % (declared, _DECLARED[road], said)
    problems = judge_visits(road, protocols)
    assert not problems, "; ".join(problems) + ". " + said
    assert not attempts, "; ".join(attempts) + ". " + said
    assert not server.quic_peers, "a QUIC connection reached the server: %s. %s" % (server.quic_peers, said)
    assert not to_the_server, "the page's UDP went to a relay that was not measured coherent. " + said


def test_a_page_behind_an_http_proxy_reads_h2_on_every_visit_and_never_tries_h3(monkeypatch):
    """An HTTP proxy has no UDP at all: the engine refuses HTTP/3 behind it and the page is h2 on every visit, with no attempt at h3.
    An engine that tried would send its QUIC Initial to the h3 port the Alt-Svc announces (around the proxy, there being no other
    road) and wait for a timeout before the fallback: the datagram that reaches that port is the attempt, whatever the machine's speed."""
    import aioquic  # noqa: F401 - the dev extra installs it: a missing one is a failure, never a skip
    from _doh_servers import FakeDoh, relay
    from _h3_server import H3Server
    from _http_proxy import HttpProxy

    class ProxyToTheServer(HttpProxy):
        """The HTTP proxy of the tests relays a CONNECT to a loopback address only; this one also relays the one to the server."""
        target = None

        def _connect(self, conn, host, port, authorization):
            if (host, port) != self.target:
                return super()._connect(conn, host, port, authorization)
            upstream = socket.create_connection((host, port), timeout=30)
            self._record("CONNECT", host, port, authorization, 200)
            self._answer(conn, 200)
            try:
                relay(conn, upstream)
            finally:
                upstream.close()

    host = this_host_address()
    doh = FakeDoh({})
    try:
        with H3Server(host) as server:
            proxy = ProxyToTheServer("http1", doh)
            proxy.target = (host, server.port)
            try:
                protocols = _visits("http_proxy", server, monkeypatch, lambda *_a, **_k: EXIT_DIRECT, proxy=proxy.proxy)
                connects = [c for c in proxy.connects if c == (host, server.port)]
            finally:
                proxy.close()
            said = _report("http_proxy", protocols, server) + "; CONNECTs to the server: %d" % len(connects)
        problems = judge_visits("http_proxy", protocols)
        assert not problems, "; ".join(problems) + ". " + said
        assert not h3_attempts(server), "; ".join(h3_attempts(server)) + ". " + said
        assert not no_https_record(host, doh), no_https_record(host, doh)
        assert connects, "the page did not go through the proxy. " + said
        assert not server.quic_peers, "a QUIC connection reached the server direct, around the proxy: %s. %s" % (server.quic_peers, said)
    finally:
        doh.close()
