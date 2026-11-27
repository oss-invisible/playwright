"""E2E: behind a gateway that DECLARES its exit, every value a page can read says that exit and nothing says this machine.

This replaces a check that was made by hand through a paid proxy against pages on the internet. No test here needs a real proxy
or a site on the internet: the proxy is a local SOCKS5 gateway (``ExitGateway``, a ``coherent`` UDP road) that answers "what is my
address" with the exit it declares, the names of the pages are resolved by a FakeDoh through that gateway, and the pages are a
local site that reads the same values a page on the internet reads. The truth for the zone and the country of an exit is the
core's own offline geo database (``ip_to_timezone`` and ``ip_to_country``, asked at test time), not a second service: the tests
ask whether the pages say what the path ``prepare_session_geo`` takes says, for six exits in six countries. The session geo is
NOT stubbed: the only replaced step is the HTTPS question "what is my address" (``egress_by_proxy``), which no local gateway can
answer as itself; the zone, the language and the srflx are decided by the core from that answer.

One launch per exit (``observed``, six of them) is read by the tests below; each test judges one thing:

* the address a site sees, and that nothing reached the site, or the network, past the gateway;
* WebRTC: the candidates and the SDP carry nothing of this machine;
* DNS: the names of the pages reach the gateway (as DoH) and are not asked of the system resolver;
* the time zone of the page, of its offsets read two ways, and of a worker;
* the language of the page, of a worker and of the Accept-Language the site receives, and that it fits the exit's country.

The four states of the gateway (coherent, TCP only, mismatched, silent) are tested by ``test_webrtc_realness.py``; here only the
"nothing of the host" part of a coherent gather is added.

Hermetic on a real binary; the resolver trap runs where it can (Linux, root; ``INVPW_REQUIRE_TRAP=1`` makes a trap that cannot
run a failure, as in the DoH suite), and the other assertions do not depend on it. On Windows the trap does not exist, and the
Watcher still sees a connection to a DNS port.
"""
from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest

from _session_end import EndingPlaywright

import _realness_support as rs
import _trap_policy
from _doh_servers import DOH_PROVIDER_PREFS, QTYPE_A, FakeDoh
from _host_address import this_host_address
from _leaks import ResolverTrap, Watcher
from _socks_servers import STUN_NUMBER, STUN_PORT
from _udp_line import egress_by_proxy, measure_servers
from test_webrtc_realness import _RELAY_PROBE_JS

pytestmark = [pytest.mark.e2e, pytest.mark.usefixtures("the_udp_line")]

#: Loopback goes through the gateway too (the site is on loopback), and the DoH provider is the FakeDoh's (a preloaded host's
#: certificate cannot be overridden): the same two things the DoH suite declares.
PREFS = {"network.proxy.allow_hijacking_localhost": True, "network.proxy.no_proxies_on": "", **DOH_PROVIDER_PREFS,
         "media.peerconnection.ice.obfuscate_host_addresses": True}
STUN = "stun:%s:%d" % (STUN_NUMBER, STUN_PORT)


def _start_trap():
    """The resolver trap, or None where it cannot run and the gate does not require it (see the ``trap`` fixture)."""
    if not ResolverTrap.available:
        why = _trap_policy.failure(None, False, ResolverTrap.reason)
        if why:
            pytest.fail(why)
        return None
    trap = ResolverTrap()
    try:
        trap.__enter__()
    except OSError as exc:
        why = _trap_policy.failure(None, True, bind_error=exc)
        if why:
            pytest.fail(why)
        return None
    return trap


def _truth_of(exit_ip):
    """The zone and the country the core's geo database gives the exit: the truth of these tests."""
    from invisible_core._geo import ip_to_country, ip_to_timezone
    from invisible_core import ensure_geoip_mmdb

    database = ensure_geoip_mmdb()
    return ip_to_timezone(exit_ip, database), ip_to_country(exit_ip, database)


def _launch(binary, gateway, **extra):
    return EndingPlaywright(seed=42, headless=True, binary_path=binary, proxy=gateway.proxy,
                            udp_stun_servers=measure_servers(gateway), extra_prefs=dict(PREFS), **extra)


@pytest.fixture(scope="module", params=list(rs.EXITS), ids=list(rs.EXITS))
def observed(request, firefox_binary):
    """One launch behind the exit ``request.param``, read once: the plain data of everything it showed (the browser and the
    servers are closed before this returns)."""
    from invisible_core import _geo

    label = request.param
    exit_ip = rs.EXITS[label]
    zone, country = _truth_of(exit_ip)
    relay_host = this_host_address()          # before any server: a host with no route skips with nothing left listening
    seen = rs.Observation(label=label, exit_ip=exit_ip, zone=zone, country=country, host=rs.host_addresses() | {relay_host})
    patch = pytest.MonkeyPatch()
    doh = FakeDoh(rs.ZONE_NAMES)
    gateway = rs.ExitGateway("exit", doh, udp="relay", tcp_exit=exit_ip, udp_exit=exit_ip, relay_host=relay_host)
    site = rs.Site(gateway)
    trap = None
    watcher = Watcher(match=("firefox", Path(firefox_binary).stem.lower()))
    try:
        patch.setattr(_geo, "discover_egress_ip", egress_by_proxy(gateway))
        trap = _start_trap()
        watcher.start()
        with _launch(firefox_binary, gateway) as browser:
            context = browser.new_context(ignore_https_errors=True)
            page = context.new_page()
            page.goto(site.url(), wait_until="load", timeout=60000)
            result = page.evaluate(rs.PAGE_JS)
            seen.probe = page.evaluate(_RELAY_PROBE_JS, STUN)
            context.close()
        watcher.stop()
        seen.ip_seen, seen.reads = result["ip"], {"page": result["page"], "worker": result["worker"]}
        seen.arrivals = list(site.arrivals)
        seen.headers = site.headers_of("/", "/fetch", "/worker-fetch")
        seen.connects, seen.site_port = list(gateway.connects), site.port
        # The relay of the gateway is on this host's address (the ICE sockets cannot send to loopback on Windows): the UDP
        # that goes to it is the road, not a leak.
        seen.direct = [c.remote for c in watcher.direct if not (c.protocol == "udp" and c.remote.rsplit(":", 1)[0] == relay_host)]
        seen.watched = len(watcher.watched_pids)
        seen.doh_vias = {name: set(doh.vias_of(name, QTYPE_A)) for name in rs.ZONE_NAMES}
        seen.doh_connects = list(gateway.doh_connects())
        seen.gateway_name = gateway.name
        seen.trap_ran = trap is not None
        seen.trap_queries = [q.name for q in trap.queries] if trap is not None else []
        seen.datagrams = [(h, p) for h, p, _payload in gateway.datagrams]
    finally:
        watcher.stop()
        if trap is not None:
            trap.__exit__(None, None, None)
        patch.undo()
        site.close()
        gateway.close()
        doh.close()
    return seen


# ============================================================================================================ the tests
def test_a_site_sees_the_declared_exit_and_nothing_reaches_it_past_the_gateway(observed):
    """1. A page asks a local "what is my address" endpoint, through the gateway, and is told the exit the gateway declares.
    The site tells a connection the gateway opened from one it did not (``ExitGateway.upstream_ports``): any request on the
    second kind is a direct connection. The watcher, which samples the connections of every browser process from before the
    launch, sees none to a non-loopback address either (other than the gateway's own UDP relay)."""
    assert observed.watched, "the watcher never saw a browser process: it proves nothing"
    problems = rs.ip_problems(observed.ip_seen, observed.arrivals, observed.direct, observed.exit_ip, observed.connects,
                              observed.site_port)
    assert problems == [], "exit %s (%s): %s" % (observed.exit_ip, observed.label, problems)


def test_a_coherent_gather_shows_nothing_of_this_machine(observed):
    """2. The candidates and the SDP of a gather behind a coherent gateway: host candidates are mDNS names, there is no IPv6
    candidate behind an IPv4 exit, the server-reflexive candidate (if one is born) is the exit, and neither the SDP nor the
    candidates write a private address, a loopback one, or any address of this machine's interfaces."""
    problems = rs.webrtc_problems(observed.probe, observed.exit_ip, observed.host)
    assert problems == [], "exit %s (%s): %s; candidates: %s; sdp: %r" % (
        observed.exit_ip, observed.label, problems, observed.probe.get("candidates"), observed.probe.get("sdp"))


def test_the_names_of_the_pages_reach_the_gateway_and_not_the_system_resolver(observed):
    """3. The page was reached by a NAME. The browser asked the FakeDoh for it, through the gateway (a DoH CONNECT), connected
    to the answer by ADDRESS (no CONNECT carries the name), and asked the resolver of the machine nothing: the trap, where it
    can run, counted no question; where it cannot, no connection to port 53 was seen by the watcher."""
    assert observed.doh_vias[rs.SITE_NAME] == {observed.gateway_name}, (
        "%s was asked of the DoH by %s, not by the gateway only" % (rs.SITE_NAME, observed.doh_vias[rs.SITE_NAME]))
    assert observed.doh_connects, "no DoH CONNECT reached the gateway"
    assert [h for h, _p in observed.connects if h.lower() in rs.ZONE_NAMES] == [], (
        "a CONNECT carried the NAME of a page instead of its address: %s" % observed.connects)
    assert [c for c in observed.direct if c.endswith(":53")] == [], "a connection to a DNS port: %s" % observed.direct
    assert [q for q in observed.trap_queries if q.endswith(".test")] == [], (
        "the browser asked the resolver of the machine for %s" % observed.trap_queries)
    if observed.trap_ran:
        assert observed.trap_queries == [], "the browser asked the resolver of the machine: %s" % observed.trap_queries


def test_the_timezone_of_the_page_its_offsets_and_its_worker_are_the_exits(observed):
    """4. ``Intl.DateTimeFormat().resolvedOptions().timeZone``, the UTC offset computed two ways in the page (the clock of the
    page, ``getTimezoneOffset``, and the wall clock ``Intl`` prints for the zone the page names) at a northern-winter and a
    northern-summer instant, and the same reads inside a dedicated Worker, all equal the zone of the exit. The zone expected is
    what the core's own geo database gives the declared exit (``ip_to_timezone``): the truth here is the core's database, not a
    second service. The offsets expected come from the system tz database (``zoneinfo``)."""
    problems = rs.timezone_problems(observed.reads, observed.zone)
    assert problems == [], "exit %s (%s, %s): %s" % (observed.exit_ip, observed.label, observed.zone, problems)


def test_the_language_of_the_page_its_worker_and_the_header_agree_and_fit_the_country(observed):
    """5. ``navigator.language``, ``navigator.languages``, the ``Intl`` locale, the same inside a Worker, and the
    ``Accept-Language`` the site receives (of the navigation, of a fetch and of the fetch of a worker) agree with each other,
    and the primary language is plausible for the country of the exit (a table of this suite, ``PLAUSIBLE_PRIMARY``; a country
    that is not in it is checked for agreement only)."""
    problems = rs.language_problems(observed.reads, observed.headers, observed.country)
    assert problems == [], "exit %s (%s, %s): %s" % (observed.exit_ip, observed.label, observed.country, problems)


def test_the_exits_are_distinct_places():
    """The six exits are six different zones (a test of the table, with no browser: it asks the database only), so a page that
    ignored the exit and kept one zone could not pass them all."""
    zones = {label: _truth_of(ip)[0] for label, ip in rs.EXITS.items()}
    assert len(set(zones.values())) == len(zones), zones
    for ip in rs.EXITS.values():
        assert ipaddress.ip_address(ip).is_global, ip
