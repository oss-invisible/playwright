"""Three local SOCKS5 gateways on 127.0.0.1, for tests that need a real wire.

Reusable: nothing here knows about the core. A test (or the wrapper's system
test) starts one with ``with coherent() as gw:`` and points the product at
``gw.server`` (``socks5://127.0.0.1:<port>``), with ``gw.credentials`` when the
gateway asks for a user and password.

The gateways are the things a real one can be:

* :func:`coherent` - TCP CONNECT works, UDP ASSOCIATE is granted, and the relay
  carries a STUN binding request to a fake STUN server that answers with the
  SAME address the TCP side reports as the exit. This is the gateway whose UDP
  can be trusted.
* :func:`tcp_only` - TCP CONNECT works, UDP ASSOCIATE is refused with reply code
  7 ("command not supported"), the way a residential gateway does.
* :func:`mismatched` - UDP ASSOCIATE is granted and STUN answers, but with an
  address DIFFERENT from the TCP exit: the disagreement a detector looks for.
* :func:`silent_udp` - UDP ASSOCIATE is granted and the relay carries datagrams, but
  no STUN server ever answers (a router that drops UDP behind a working handshake): the core's
  measurement ends in ``unmeasured``.

THE CORE MEASURES THE UDP EXIT of a SOCKS5 proxy before it declares it to the browser (``invisible_core._udp_exit``: a STUN
Binding through the proxy's ASSOCIATE, compared with the TCP exit). A test that wants the relay to be used hands the core
this gateway's measurement server, ``udp_stun_servers=gw.measure_servers`` (``MEASURE_NUMBER``: the fake STUN server by a
documentation NUMBER of its own, so the datagrams of the measurement are told apart from those of the page, which asks
``STUN_NUMBER``). The measurement is a UDP ASSOCIATE of its own: :attr:`Gateway.associates` and :attr:`Gateway.datagrams` count it.

Everything is on loopback, so the "exit address" is made observable and
controllable instead of real: the TCP exit is what the gateway declares
(``declare_exit``) and the lab answers when the product asks "what is my address"
through it (``tests/_lab_egress.py``; the product asks HTTPS, which no gateway can
answer as the service; the plain-HTTP endpoint ``api.ipify.org:80`` is still
answered here for the tests that ask it), and the UDP exit is what the fake STUN server puts in its XOR-MAPPED-ADDRESS.
Both are chosen by the test (``tcp_exit`` and ``udp_exit`` arguments).

HERMETIC: a gateway of this module carries a CONNECT or a datagram only to the LAB (:func:`is_lab_destination`: loopback, the
documentation ranges of RFC 5737 and RFC 3849 the tests use as lab exits and servers, the addresses of this host's own interfaces, where a server
of a test may stand, and the name ``localhost``). These tests run in the gate of the candidate, where no test may depend on a real service, and the
browser behind a gateway makes background requests of its own (a name of the vendor, a captive-portal check, a telemetry endpoint): such a
destination is REFUSED, never forwarded (a CONNECT is answered with reply 2, "not allowed"; a datagram is dropped), and RECORDED
(:class:`LabGuard`: the time since the gateway started, the SOCKS5 address type, the destination as it was received - the NAME for type 3 -, the
port, the protocol, ``tcp`` or ``udp``, and the REASON: which rule refused it, the lab's or the narrower one of a subclass). The interfaces of this
host are read when the guard is made (``psutil.net_if_addrs``, written once in the record with how they were read) and again at each decision that
needs them; a reading that fails RAISES (``_witness.HostInterfacesUnreadable``), it is never an empty list. The record is always written, when the gateway is closed, under
``$RUNNER_TEMP/invpw-evidence/<gateway>/<pid>-<ms>/refused-destinations.json`` (the folder the e2e jobs upload; off a runner nothing is
written), so that a reader sees what the browser asked for besides what the test drives. There is no option that reopens it. A subclass can only
narrow it (:meth:`Gateway._admits`); the destinations it serves itself (the fake STUN server, the page of ``WEB_NAME``, the exit questions) are
answered before it, and a name mapped to a local server by :meth:`Gateway._target` is judged by the server it maps to. What a test sees of the
road is :attr:`Gateway.connects`, :attr:`Gateway.datagrams` (every datagram the relay read, carried or dropped) and
:attr:`Gateway.guard`. :class:`Socks5TcpOnly` and the SOCKS5 server of ``test_proxy_socks_auth_e2e.py`` are hermetic the same way (same rule,
same record).

This is the ONE SOCKS5 gateway of the test suite. A road that needs more than the three above (a TURN server, a resolver,
a page, by NAME: ``tests/_turn_chain.py``) subclasses :class:`Gateway` and overrides the small hooks (:meth:`Gateway._route`,
:meth:`Gateway._target`, :attr:`Gateway.whoami_names`) instead of re-implementing the greeting, the authentication, the
ASSOCIATE, the relay or the round trip.
"""
from __future__ import annotations

import ipaddress
import json
import os
import pathlib
import select
import socket
import struct
import threading
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

#: The name the product's STUN probe sends through the relay. The relay maps it
#: to the fake STUN server; every other destination is the lab's to carry or is refused (``LabGuard``).
STUN_NAME = "stun.l.google.com"
STUN_PORT = 19302
#: The same STUN server by NUMBER. A page cannot resolve a name behind the session (the engine forbids local
#: resolution), so the wrapper's system test hands the page this address; like the name it is a documentation
#: address (RFC 5737, TEST-NET-2), unroutable: the only road to it is the relay, which maps it to the fake STUN
#: server. (A copy of invisible_core's tests/_socks_servers.py with this one addition.)
STUN_NUMBER = "198.51.100.5"
#: The same fake STUN server by ANOTHER number: where the core's own measurement of the UDP exit asks (``udp_stun_servers``),
#: so that what the measurement sent through the relay is never mistaken for what the page sent. Documentation address too.
MEASURE_NUMBER = "198.51.100.6"
#: A page the gateway serves itself, for a test that loads one through the proxy (a name nothing else resolves).
WEB_NAME = "page.test"
#: The same page by an address (RFC 5737 TEST-NET-3): since the browser resolves names itself and connects by address, a
#: test that loads the gateway's page through a SOCKS gateway names it by this one.
WEB_ADDRESS = "203.0.113.80"
#: The echo endpoints the product asks for its TCP exit.
WHOAMI4 = ("api.ipify.org", 80)
WHOAMI6 = ("api6.ipify.org", 80)

_MAGIC = b"\x21\x12\xa4\x42"


# ----------------------------------------------------------------------------------------------------------------
#  The exit of a proxy that relays CONNECTs. The question "where do I leave from" is HTTPS (api.ipify.org:443), and no gateway
#  of the tests can answer a TLS request as the service. A gateway of this module is hermetic (a CONNECT outside the lab is refused, see
#  ``LabGuard``), so the question could not reach the internet through it anyway; but the product would then be told no exit at all. So such a
#  proxy DECLARES its exit (``declare_exit``) and ``tests/_lab_egress.DeclaredExit`` answers the question with it; and as a backstop each of
#  them refuses the question itself (``asks_the_world``), which records it and makes the test fail (``nobody_asked_the_world``), so that a
#  proxy that forgot to declare is a red with its name and not a flaky green.
# ----------------------------------------------------------------------------------------------------------------
#: The hosts of the services of the core that tell a caller its address (``invisible_core._geo._IP_ECHO_ENDPOINTS`` and
#: ``_IPV6_ECHO_ENDPOINTS``; ``tests/test_a_launch_asks_nobody_on_the_internet_where_it_leaves_from.py`` checks that this list covers them).
EXIT_SERVICES = ("api.ipify.org", "api6.ipify.org", "icanhazip.com", "ipv6.icanhazip.com", "checkip.amazonaws.com", "ifconfig.me")

#: ``host:port`` of a proxy of the tests that relays CONNECTs -> the exit it declares. Filled by the proxies themselves.
_DECLARED_EXITS: Dict[str, str] = {}
#: The questions of the exit that reached a forwarding proxy and were refused there: one sentence each, until a test reads them.
_WORLD_QUESTIONS: List[str] = []


def _endpoint(server: str) -> str:
    """``host:port`` of a proxy given as ``scheme://host:port`` (or as ``host:port``)."""
    return server.split("://", 1)[-1].rsplit("@", 1)[-1].rstrip("/")


def declare_exit(server: str, exit_ip: str) -> None:
    """The proxy at ``server`` relays CONNECTs and says its exit is ``exit_ip``."""
    _DECLARED_EXITS[_endpoint(server)] = exit_ip


def forget_exit(server: str) -> None:
    _DECLARED_EXITS.pop(_endpoint(server), None)


def declared_exit_of(server: Optional[str]) -> Optional[str]:
    """The exit a forwarding proxy of the tests declared, or None for any other proxy (one that refuses the world, or not a proxy of ours)."""
    return _DECLARED_EXITS.get(_endpoint(server)) if server else None


def asks_the_world(server: str, host: str, port: int) -> bool:
    """True, and the question is recorded, when a CONNECT to ``host:port`` is the question of the exit: the proxy must refuse it (the lab answers it with the declared exit)."""
    if host.lower() not in EXIT_SERVICES:
        return False
    _WORLD_QUESTIONS.append("%s was asked %s:%d: the question of the exit reached a proxy of the lab, which refuses it (its exit is declared, not asked of the internet)" % (
        server, host, port))
    return True


def nobody_asked_the_world() -> List[str]:
    """The questions of the exit refused since the last call (and forgets them): empty when every one was answered by the lab."""
    asked = list(_WORLD_QUESTIONS)
    del _WORLD_QUESTIONS[:]
    return asked


def _lab_exit() -> str:
    """The exit of the lab (``tests/_lab_egress.LAB_EGRESS_IP``), imported when asked."""
    from _lab_egress import LAB_EGRESS_IP

    return LAB_EGRESS_IP


def _recv_exact(s: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = s.recv(n - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


# ----------------------------------------------------------------------------------------------------------------
#  The lab. A gateway of the tests forwards nothing that is not the lab's: the destination is refused and recorded.
# ----------------------------------------------------------------------------------------------------------------
#: The networks that are the lab's whatever the host: loopback, and the documentation ranges (RFC 5737 TEST-NET-1/2/3, RFC 3849) that the tests use
#: for their exits and their servers (nothing routes them: a test holds the ones a server stands at on ``lo``).
LAB_NETWORKS = tuple(ipaddress.ip_network(net) for net in (
    "127.0.0.0/8", "::1/128", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32"))
#: The folder under ``$RUNNER_TEMP`` the e2e workflows upload (``invpw-evidence-*``).
EVIDENCE_FOLDER = "invpw-evidence"


def _own_interfaces() -> Dict[str, List[str]]:
    """The addresses assigned to each interface of this host (a server of a test stands at one of them), by interface name. It RAISES when they
    cannot be listed (``_witness.HostInterfacesUnreadable``, or the ImportError of ``_witness``): never an empty list in place of an unreadable one."""
    from _witness import read_host_interfaces

    return read_host_interfaces()


def _own_addresses() -> Set[str]:
    """Every address of :func:`_own_interfaces` (raises as it does)."""
    return {address for addresses in _own_interfaces().values() for address in addresses}


def _how_own_interfaces_are_read() -> str:
    from _witness import how_host_interfaces_are_read

    return how_host_interfaces_are_read()


def lab_refusal(host: str) -> Optional[str]:
    """None when ``host`` (an address, or a name as a SOCKS5 client sent it) is the lab's: loopback, a documentation range, an address of one of this host's
    own interfaces, or ``localhost``; otherwise WHY it is not, one sentence. Any other NAME is not the lab's: nothing here resolves a name for the internet's
    sake, and the question would leave the machine. The interfaces are read (:func:`_own_addresses`) only for an address outside loopback and the
    documentation ranges, and a reading that fails raises."""
    text = host.strip().strip("[]").split("%", 1)[0].rstrip(".").lower()
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        if text == "localhost" or text.endswith(".localhost"):
            return None
        return "a name that is not localhost: a gateway of the tests resolves no name, the question would leave the machine"
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if any(address in network for network in LAB_NETWORKS):
        return None
    own = _own_addresses()
    if address.compressed in own:
        return None
    return ("%s is not loopback, not in a documentation range and not one of the %d addresses of this host's interfaces read at the decision"
            % (address.compressed, len(own)))


def is_lab_destination(host: str) -> bool:
    """Whether ``host`` is the lab's (:func:`lab_refusal` has no reason to refuse it)."""
    return lab_refusal(host) is None


class LabGuard:
    """What refuses a destination that is not the lab's and writes it down: one per gateway.

    :meth:`admits` is asked with the destination as RECEIVED (``atyp`` is the SOCKS5 address type: 1 IPv4, 3 a name, 4 IPv6; ``protocol`` is ``"tcp"`` or
    ``"udp"``); ``target`` is where it would really go, as (host, port), when that is not the destination itself (a name a gateway maps to a local server): the lab's
    decision is about the target. A refusal is recorded in :attr:`records` as a dict with ``t`` (seconds since ``t0``, the start of the gateway), ``atyp``,
    ``destination`` (as received), ``port``, ``protocol`` and ``reason`` (the rule that refused it). :meth:`write_evidence` leaves them, always, under
    ``$RUNNER_TEMP/invpw-evidence/<name>/``, with the interfaces of this host as the guard read them when it was made (:attr:`host_interfaces`) and how.

    The interfaces are read when the guard is made: a reading that fails raises there (the gateway is not made). A reading at a decision that fails refuses
    the destination with that failure as its reason, is listed in :attr:`read_errors`, and :meth:`write_evidence` (the close of the gateway) raises it."""

    def __init__(self, name: str, t0: Optional[float] = None):
        self.name = name
        self.t0 = time.monotonic() if t0 is None else t0
        #: Every refusal, in order.
        self.records: List[dict] = []
        #: The addresses of each interface of this host, read when the guard was made (raises if they cannot be read), and how they were read.
        self.host_interfaces: Dict[str, List[str]] = _own_interfaces()
        self.host_interfaces_read_by = _how_own_interfaces_are_read()
        #: Every reading of the interfaces at a decision that failed (the destination was refused with it as the reason).
        self.read_errors: List[str] = []
        self._lock = threading.Lock()
        self._written = False

    def admits(self, host: str, port: int, atyp: int, protocol: str, target: Optional[Tuple[str, int]] = None) -> bool:
        try:
            reason = lab_refusal(host if target is None else target[0])
        except Exception as exc:  # the interfaces could not be read at the decision: refused, said, and raised at the close
            error = "the interfaces of this host could not be read at the decision: %s: %s" % (type(exc).__name__, exc)
            with self._lock:
                self.read_errors.append(error)
            self.refuse(host, port, atyp, protocol, reason=error)
            return False
        if reason is None:
            return True
        if target is not None and (target[0], target[1]) != (host, port):
            reason = "mapped to %s:%d, and %s" % (target[0], target[1], reason)
        self.refuse(host, port, atyp, protocol, reason=reason)
        return False

    def refuse(self, host: str, port: int, atyp: int, protocol: str, *, reason: str) -> None:
        """Write down a destination that was refused (by this guard's rule, or by the narrower rule of a subclass), with the ``reason``: which rule, one sentence."""
        record = {"t": round(time.monotonic() - self.t0, 3), "atyp": atyp, "destination": host, "port": port, "protocol": protocol,
                  "reason": reason}
        with self._lock:
            self.records.append(record)

    def refused(self, protocol: Optional[str] = None) -> List[Tuple[str, int]]:
        """Every refused destination as (destination as received, port), in order; of one protocol when asked."""
        with self._lock:
            return [(r["destination"], r["port"]) for r in self.records if protocol is None or r["protocol"] == protocol]

    def evidence(self, extra: Optional[dict] = None) -> dict:
        """The record as it is written: the refusals, the interfaces of this host as read when the guard was made and how, the failed readings."""
        with self._lock:
            records, errors = list(self.records), list(self.read_errors)
        return dict({"gateway": self.name, "refused": records,
                     "host_interfaces": {"read_by": self.host_interfaces_read_by, "read_at": "when the gateway was made",
                                         "interfaces": self.host_interfaces},
                     "host_interfaces_read_errors": errors}, **(extra or {}))

    def write_evidence(self, extra: Optional[dict] = None) -> Optional[pathlib.Path]:
        """Write the record, once (a gateway may be closed twice): the file written, or None off a runner (no ``$RUNNER_TEMP``) or when already written.
        Then, the first time, it RAISES if a reading of the interfaces failed at a decision (:attr:`read_errors`), on a runner or not."""
        runner_temp = os.environ.get("RUNNER_TEMP")
        with self._lock:
            if self._written:
                return None
            self._written = True
        path = None
        if runner_temp:
            folder = pathlib.Path(runner_temp) / EVIDENCE_FOLDER / self.name / ("%d-%d" % (os.getpid(), int(time.time() * 1000)))
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / "refused-destinations.json"
            path.write_text(json.dumps(self.evidence(extra), indent=2), encoding="utf-8")
        if self.read_errors:
            from _witness import HostInterfacesUnreadable

            raise HostInterfacesUnreadable("gateway %s: %s" % (self.name, "; ".join(self.read_errors)))
        return path


class FakeStun:
    """A UDP STUN server that answers every binding request with ``mapped_ip``."""

    def __init__(self, mapped_ip: str, mapped_port: int = 40000, bind_host: str = "127.0.0.1"):
        self.mapped_ip = mapped_ip
        self.mapped_port = mapped_port
        self.requests = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # On the address the relay sends from: Windows sends neither from an address of the host to loopback (WinError 10049) nor
        # from loopback to an address of the host (10051), so a relay that leaves from the host's address needs its STUN there too.
        self._sock.bind((bind_host, 0))
        self._closing = False
        if hasattr(socket, "SIO_UDP_CONNRESET"):
            # Windows: an ICMP "port unreachable" to an answer (a client that already closed its socket) makes the NEXT recvfrom raise
            # WinError 10054; left on, the server that reads would end there and answer nobody afterwards. Off, as a real server has it.
            self._sock.ioctl(socket.SIO_UDP_CONNRESET, False)
        self.address: Tuple[str, int] = self._sock.getsockname()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                data, peer = self._sock.recvfrom(2048)
            except ConnectionResetError:
                continue                # an earlier answer found nobody: that is that client's business, not the server's end
            except OSError:
                return
            if self._closing:
                return                  # the datagram `close` sent to wake this thread, or one that came after it
            if len(data) < 20 or data[0:2] != b"\x00\x01":
                continue
            self.requests += 1
            txid = data[8:20]
            xport = struct.pack("!H", self.mapped_port ^ 0x2112)
            xaddr = bytes(a ^ b for a, b in zip(socket.inet_aton(self.mapped_ip), _MAGIC))
            attr = struct.pack("!HH", 0x0020, 8) + b"\x00\x01" + xport + xaddr
            resp = b"\x01\x01" + struct.pack("!H", len(attr)) + _MAGIC + txid + attr
            try:
                self._sock.sendto(resp, peer)
            except OSError:
                if self._closing:
                    return
                continue                # a send that failed (the peer is gone) does not end the server for the next client

    def close(self) -> None:
        """Ends the server and its thread. Closing the socket wakes a `recvfrom` blocked in another thread on Windows, not on Linux,
        where the thread would stay blocked for the rest of the process: so the server first sends itself one datagram, which wakes the
        thread to see `_closing`, waits for the thread, and only then closes the socket."""
        self._closing = True
        try:
            waker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                waker.sendto(b"", self.address)
            finally:
                waker.close()
        except OSError:
            pass
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._sock.close()


class Gateway:
    """A SOCKS5 server. ``udp`` is ``"relay"`` or ``"refuse"`` (reply code 7)."""

    #: Whether this gateway relays the CONNECTs that are not special (to the lab: it is hermetic): such a gateway declares ``tcp_exit`` as its exit
    #: (``declare_exit``). A subclass that refuses everything but loopback (``tests/_doh_servers.DohGateway``) sets False: it cannot carry the
    #: question of the exit anywhere, and the question is left to what answers it.
    forwards_for_real = True
    #: The name of the folder of the record of what this gateway refused under ``$RUNNER_TEMP/invpw-evidence/`` (a subclass may name its own).
    evidence_name = "socks5-gateway"

    def __init__(self, *, udp: str, tcp_exit: str, udp_exit: str,
                 user: Optional[str] = None, password: Optional[str] = None,
                 relay_host: str = "127.0.0.1", rtt_ms: int = 0,
                 exit_host: str = "127.0.0.1", tcp_data_delay: bool = False, stun_answers: bool = True,
                 tcp_from_exit: bool = False):
        assert udp in ("relay", "refuse")
        self.udp = udp
        #: False: the relay carries the datagrams but the fake STUN server is mute (its requests are dropped).
        self.stun_answers = stun_answers
        #: The round trip to this gateway, in seconds: every answer on the control connection waits for it, and each
        #: datagram the relay carries waits half of it each way. A real gateway is not on loopback.
        self.rtt = rtt_ms / 1000.0
        #: The address of the UDP relay, the BND.ADDR of the association. A real gateway's is a routable address; the
        #: loopback default only suits a client whose UDP socket is not bound to an interface (an engine's ICE
        #: sockets are, and Windows does not send from an interface address to loopback).
        self.relay_host = relay_host
        #: The local address the relay's datagrams leave from (the source a server behind the gateway sees). Another
        #: loopback address makes a client that sent direct (from 127.0.0.1) tell apart from one that used the relay.
        self.exit_host = exit_host
        #: Whether the TCP connections the gateway opens for a CONNECT also leave from ``exit_host`` (as its datagrams do): a server that
        #: is reachable DIRECT then tells a connection of the proxy from a connection of the browser by the source.
        self.tcp_from_exit = tcp_from_exit
        #: While this Event is set to a cleared state, every answer of the gateway on a control connection waits for it (a
        #: handshake the test holds, whatever the speed of the machine): None, the default, holds nothing. A test sets it after
        #: the launch (the core's own probe goes through the gateway before the browser exists) and opens it when it chooses.
        self.gate: Optional[threading.Event] = None
        #: The same hold, one control connection at a time: ``hold(index, kind)`` is asked before each answer of the gateway on a
        #: control connection, with the ``index`` of that connection in ``accepted`` and what it asked for (``kind``: None while only the
        #: greeting is known, then ``"connect"``, ``"associate"`` or ``"other"``), and returns the Event the answer waits for (or None: no
        #: hold). A test holds the handshakes it chooses (the ASSOCIATEs begun from an instant on, one connection by its index) and
        #: lets every other one run, whatever the speed of the machine; None, the default, holds nothing.
        self.hold: Optional[Callable[[int, Optional[str]], Optional[threading.Event]]] = None
        #: Every request the gateway read on a control connection, in order, as (seconds from t0, index of the connection in
        #: ``accepted``, kind, detail): ``"greeting"`` when the connection was accepted (the first bytes of the client), then
        #: ``"connect"`` (detail ``host:port``), ``"associate"`` (detail ``association #n`` of ``associations``, ``refused`` when
        #: UDP is refused) or ``"other"``. The EXCHANGES of a road, counted by what the gateway saw and not by how long they took.
        self.requests: List[Tuple[float, int, str, str]] = []
        self._local = threading.local()
        #: Whether each chunk of a forwarded TCP stream also waits half the round trip (a far gateway), as the datagrams do.
        self.tcp_data_delay = tcp_data_delay
        self.tcp_exit = tcp_exit
        self.user, self.password = user, password
        self.stun = FakeStun(udp_exit, bind_host="127.0.0.1" if exit_host.startswith("127.") else exit_host)
        #: Every CONNECT, as (host, port), in order.
        self.connects: List[Tuple[str, int]] = []
        #: Every UDP ASSOCIATE request, answered or refused.
        self.associates = 0
        #: When the gateway started: the origin of the times below.
        self.t0 = time.monotonic()
        #: What refuses (and records, and writes under ``$RUNNER_TEMP/invpw-evidence/<evidence_name>/`` when the gateway is closed) every destination that is
        #: not the lab's, CONNECT or datagram: ``guard.records``, ``guard.refused()``.
        self.guard = LabGuard(self.evidence_name, self.t0)
        #: The same instant on the wall clock (seconds since the epoch), to compare with what a page reads from Date.now().
        self.wall0 = time.time()
        #: Every TCP connection the gateway accepted (a SOCKS handshake starts with one), as seconds from t0.
        self.accepted: List[float] = []
        #: Every UDP ASSOCIATE that was granted, as [start, end] seconds from t0; end is None until it was answered.
        self.associations: List[List[Optional[float]]] = []
        #: Every datagram the relay carried out, as (host as sent, port, address type, seconds from t0).
        self.datagram_log: List[Tuple[str, int, int, float]] = []
        #: Every datagram the relay carried out, as (host as sent, port, index in ``associations`` of the association it came
        #: on, seconds from t0): which association carried a datagram, not only when it left.
        self.datagram_via: List[Tuple[str, int, int, float]] = []
        #: Every datagram the relay carried out, as (host, port, payload).
        self.datagrams: List[Tuple[str, int, bytes]] = []
        #: The local port of the socket each granted association sends from: what a server behind the gateway sees as the port
        #: of the relay, and what tells a datagram that went by the relay from one that came direct from this host.
        self.exit_ports: List[int] = []
        #: A datagram the relay could not send (a socket that raised): said here and in the report of a test, never mute.
        self.send_errors: List[str] = []
        #: The hosts the relay forwards a datagram to (the fake STUN server aside), among the lab's; None: any host of the lab. It only narrows: a host
        #: that is not the lab's is never forwarded (``guard``).
        self.forward_only_to: Optional[Set[str]] = None
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(32)
        self.port: int = self._listener.getsockname()[1]
        self._closed = False
        if self.forwards_for_real:
            declare_exit(self.server, tcp_exit)
        threading.Thread(target=self._accept, daemon=True).start()

    # --- what the test hands to the product ---------------------------------
    @property
    def server(self) -> str:
        return "socks5://127.0.0.1:%d" % self.port

    @property
    def measure_servers(self) -> List[str]:
        """The ``udp_stun_servers`` of a launch behind this gateway: where the core measures its UDP exit, in the
        form the parameter takes (``"ip:port"``, an IPv4 literal)."""
        return ["%s:%d" % (MEASURE_NUMBER, STUN_PORT)]

    @property
    def credentials(self) -> dict:
        return ({"username": self.user, "password": self.password}
                if self.user is not None else {})

    @property
    def proxy(self) -> dict:
        """The endpoint dict the core takes: ``{"server": ..., [credentials]}``."""
        return dict({"server": self.server}, **self.credentials)

    def close(self) -> None:
        self._closed = True
        forget_exit(self.server)
        for s in (self._listener,):
            try:
                s.close()
            except OSError:
                pass
        self.stun.close()
        self.guard.write_evidence(self._evidence())

    def _evidence(self) -> dict:
        """What a subclass adds to the record of the refusals written at the close (a dict of JSON values)."""
        return {}

    def __enter__(self) -> "Gateway":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- the TCP side --------------------------------------------------------
    def _request(self, kind: str, detail: str = "") -> None:
        """Record a request of the client on the control connection of this thread, and what it asked for (``hold`` is told)."""
        self._local.kind = None if kind == "greeting" else kind
        self.requests.append((time.monotonic() - self.t0, getattr(self._local, "index", -1), kind, detail))

    def _send(self, c: socket.socket, data: bytes) -> None:
        gate = self.gate
        if gate is not None:
            gate.wait(120)
        hold = self.hold
        if hold is not None:
            held = hold(getattr(self._local, "index", -1), getattr(self._local, "kind", None))
            if held is not None:
                held.wait(120)
        if self.rtt:
            time.sleep(self.rtt)
        c.sendall(data)

    def _accept(self) -> None:
        while not self._closed:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            index = len(self.accepted)
            self.accepted.append(time.monotonic() - self.t0)
            threading.Thread(target=self._run, args=(conn, index), daemon=True).start()

    def _run(self, conn: socket.socket, index: int) -> None:
        """One control connection: remembers its index (for ``hold`` and ``requests``), then serves it."""
        self._local.index = index
        self._local.kind = None
        self.requests.append((self.accepted[index], index, "greeting", ""))
        self._handle(conn)

    def _greet(self, c: socket.socket) -> bool:
        ver, n = _recv_exact(c, 2)
        methods = _recv_exact(c, n)
        if ver != 5:
            return False
        if self.user is not None:
            if 2 not in methods:
                self._send(c, b"\x05\xff")
                return False
            self._send(c, b"\x05\x02")
            _recv_exact(c, 1)
            u = _recv_exact(c, _recv_exact(c, 1)[0]).decode()
            p = _recv_exact(c, _recv_exact(c, 1)[0]).decode()
            ok = (u, p) == (self.user, self.password)
            self._send(c, b"\x01" + (b"\x00" if ok else b"\x01"))
            return ok
        self._send(c, b"\x05\x00" if 0 in methods else b"\x05\x02")
        if 0 not in methods:  # a client that only offers user/password: accept any
            _recv_exact(c, 1)
            _recv_exact(c, _recv_exact(c, 1)[0])
            _recv_exact(c, _recv_exact(c, 1)[0])
            self._send(c, b"\x01\x00")
        return True

    def _read_address(self, c: socket.socket) -> Tuple[str, int]:
        """The destination of a request, as received (the NAME for address type 3); its address type is left in ``self._local.atyp``."""
        atyp = _recv_exact(c, 1)[0]
        self._local.atyp = atyp
        if atyp == 1:
            host = socket.inet_ntoa(_recv_exact(c, 4))
        elif atyp == 3:
            host = _recv_exact(c, _recv_exact(c, 1)[0]).decode()
        else:
            host = socket.inet_ntop(socket.AF_INET6, _recv_exact(c, 16))
        return host, struct.unpack("!H", _recv_exact(c, 2))[0]

    @staticmethod
    def _reply(rep: int, host: str = "0.0.0.0", port: int = 0) -> bytes:
        return b"\x05" + bytes([rep]) + b"\x00\x01" + socket.inet_aton(host) + struct.pack("!H", port)

    def _handle(self, c: socket.socket) -> None:
        try:
            c.settimeout(30)
            if not self._greet(c):
                return
            ver, cmd, _rsv = _recv_exact(c, 3)
            host, port = self._read_address(c)
            if cmd == 1:
                self._request("connect", "%s:%d" % (host, port))
                self._connect(c, host, port)
            elif cmd == 3:
                self._associate(c)
            else:
                self._request("other", "command %d" % cmd)
                self._send(c, self._reply(7))
        except (OSError, ConnectionError):
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass

    #: The names the product asks "what is my address" of (on port 80); each is answered with ``tcp_exit``.
    whoami_names: Tuple[str, ...] = (WHOAMI4[0],)

    def _target(self, host: str, port: int) -> Tuple[str, int]:
        """Where a CONNECT that is not a special one really goes. A subclass maps a name to a local server."""
        return host, port

    def _admits(self, host: str, port: int, atyp: int, protocol: str, target: Optional[Tuple[str, int]] = None) -> bool:
        """Whether the gateway carries a CONNECT (``protocol`` ``"tcp"``) or a datagram (``"udp"``) to ``host``:``port`` (as received; ``atyp`` is its SOCKS5
        address type), or to ``target`` (host, port) when it is mapped elsewhere: only the lab's, and the rest is refused and recorded (``guard``). A subclass may narrow
        it (never widen it: call this one first)."""
        return self.guard.admits(host, port, atyp, protocol, target)

    def _open_upstream(self, target: Tuple[str, int]) -> socket.socket:
        """The TCP connection of a CONNECT: from ``exit_host`` when ``tcp_from_exit``, else from wherever the system chooses."""
        if not self.tcp_from_exit:
            return socket.create_connection(target, timeout=30)
        up = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            up.settimeout(30)
            up.bind((self.exit_host, 0))
            up.connect(target)
        except OSError:
            up.close()
            raise
        return up

    def _connect(self, c: socket.socket, host: str, port: int) -> None:
        self.connects.append((host, port))
        if host.lower() in self.whoami_names and port == WHOAMI4[1]:
            self._send(c, self._reply(0))
            _ = c.recv(4096)
            body = self.tcp_exit.encode()
            self._send(c, b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n"
                      b"Connection: close\r\n\r\n%s" % (len(body), body))
            return
        if (host, port) == WHOAMI6:
            self._send(c, self._reply(4))  # no IPv6 exit
            return
        if (host, port) in ((WEB_NAME, 80), (WEB_ADDRESS, 80)):  # a page that only the gateway can reach
            self._send(c, self._reply(0))
            _ = c.recv(4096)
            body = b"<html><body>behind the gateway</body></html>"
            self._send(c, b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\n"
                      b"Connection: close\r\n\r\n%s" % (len(body), body))
            return
        if self.forwards_for_real and asks_the_world(self.server, host, port):
            self._send(c, self._reply(2))      # not allowed: the exit of this gateway is declared, it is not asked of the internet
            return
        target = self._target(host, port)
        if not self._admits(host, port, getattr(self._local, "atyp", 3), "tcp", target):
            self._send(c, self._reply(2))      # not allowed: the gateway carries nothing outside the lab
            return
        try:
            up = self._open_upstream(target)
        except OSError:
            self._send(c, self._reply(5))
            return
        self._send(c, self._reply(0))
        c.settimeout(None)
        up.settimeout(None)

        def pump(a: socket.socket, b: socket.socket) -> None:
            try:
                while True:
                    d = a.recv(65536)
                    if not d:
                        break
                    if self.tcp_data_delay and self.rtt:
                        time.sleep(self.rtt / 2)
                    b.sendall(d)
            except OSError:
                pass
            finally:
                for s in (a, b):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        t = threading.Thread(target=pump, args=(up, c), daemon=True)
        t.start()
        pump(c, up)
        t.join(timeout=2)
        up.close()

    # --- the UDP side --------------------------------------------------------
    def _later(self, fn, what: str = "a datagram") -> None:
        """Run ``fn`` after half the round trip (at once on loopback). A send that fails is RECORDED in ``send_errors`` (a
        silent one hid that Windows cannot send from 127.0.0.1 to an address of its own host: WinError 10051), except on a gateway
        that is already closed."""
        def run() -> None:
            try:
                fn()
            except OSError as e:
                if not self._closed:
                    self.send_errors.append("%s: %s %s" % (what, type(e).__name__, e))
        threading.Timer(self.rtt / 2, run).start() if self.rtt else run()

    def _deliver(self, relay: socket.socket, to: Tuple[str, int], host: str, port: int, payload: bytes) -> None:
        """Wrap ``payload`` as coming from ``host``:``port`` (an IPv4 literal) and send it back to the client."""
        data = b"\x00\x00\x00\x01" + socket.inet_aton(host) + struct.pack("!H", port) + payload
        self._later(lambda: relay.sendto(data, to), "the answer to the client")

    def _route(self, host: str, dport: int, atyp: int, payload: bytes, out_port: int,
               answer) -> Optional[Tuple[Tuple[str, int], Optional[Tuple[str, int]]]]:
        """Where one datagram of the client goes: ``(target, reported_as)``, or None when it is dropped.

        ``target`` is the real address the datagram is sent to from the exit socket (``out_port`` is that socket's port);
        ``reported_as`` is the address the client is told the reply came from (None: the real source). A subclass may also
        answer by itself with ``answer(host, port, data)``. By default the fake STUN server, by name or by number, is
        mapped and everything else is forwarded when it is the lab's (``_admits``); a datagram to any other destination is dropped and recorded."""
        if (host, dport) in ((STUN_NAME, STUN_PORT), (STUN_NUMBER, STUN_PORT), (MEASURE_NUMBER, STUN_PORT)):
            if not self.stun_answers:
                return None
            # A real server answers from the address it was asked at; the fake STUN server is mapped, so its reply is
            # reported as from the asked address (only when that was a literal: a name has no address to report).
            return self.stun.address, ((host, dport) if atyp == 1 else None)
        if not self._admits(host, dport, atyp, "udp"):
            return None                        # not the lab's: dropped, and recorded
        if self.forward_only_to is not None and host not in self.forward_only_to:
            return None
        return (host, dport), None

    def _associate(self, c: socket.socket) -> None:
        self.associates += 1
        if self.udp == "refuse":
            self._request("associate", "refused")
            self._send(c, self._reply(7))
            return
        record: List[Optional[float]] = [time.monotonic() - self.t0, None]
        self.associations.append(record)
        index = next(i for i, r in enumerate(self.associations) if r is record)
        self._request("associate", "association #%d" % index)
        relay = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        relay.bind((self.relay_host, 0))
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        out.bind((self.exit_host, 0))
        self.exit_ports.append(out.getsockname()[1])
        if hasattr(socket, "SIO_UDP_CONNRESET"):
            # Windows: an ICMP "port unreachable" to a send makes the NEXT recvfrom raise WinError 10054 and, uncaught, ends the
            # thread that reads: a relay that stops carrying answers because one datagram found nobody. Off, as a real relay has it.
            for sock in (relay, out):
                sock.ioctl(socket.SIO_UDP_CONNRESET, False)
        client: List[Optional[Tuple[str, int]]] = [None]
        #: Where a mapped destination really is -> the address the client is told it answered from.
        asked_at: dict = {}
        self._send(c, self._reply(0, self.relay_host, relay.getsockname()[1]))
        record[1] = time.monotonic() - self.t0

        def answer(host: str, port: int, data: bytes) -> None:
            self._deliver(relay, client[0], host, port, data)

        def inbound() -> None:  # client -> world
            # The port of the exit socket is read ONCE: asked at every datagram it fell on a socket the teardown had already closed
            # (OSError WinError 10038 in a thread nobody joins). An OSError of the body is the association ending, not a failure.
            try:
                out_port = out.getsockname()[1]
            except OSError:
                return
            while True:
                try:
                    d, peer = relay.recvfrom(65536)
                except ConnectionResetError:
                    continue
                except OSError:
                    return
                try:
                    if len(d) < 10 or d[2] != 0:
                        continue
                    client[0] = peer
                    atyp, i = d[3], 4
                    if atyp == 1:
                        host, i = socket.inet_ntoa(d[i:i + 4]), i + 4
                    elif atyp == 3:
                        n = d[i]
                        host, i = d[i + 1:i + 1 + n].decode(), i + 1 + n
                    else:
                        host, i = socket.inet_ntop(socket.AF_INET6, d[i:i + 16]), i + 16
                    dport = struct.unpack("!H", d[i:i + 2])[0]
                    payload = d[i + 2:]
                    self.datagrams.append((host, dport, payload))
                    self.datagram_log.append((host, dport, atyp, time.monotonic() - self.t0))
                    self.datagram_via.append((host, dport, index, time.monotonic() - self.t0))
                    routed = self._route(host, dport, atyp, payload, out_port, answer)
                    if routed is None:
                        continue
                    target, reported = routed
                    if reported is not None:
                        asked_at[target] = reported
                    self._later(lambda p=payload, t=target: out.sendto(p, t), "the datagram to %s:%d" % target)
                except OSError:
                    return

        def outbound() -> None:  # world -> client
            while True:
                try:
                    d, src = out.recvfrom(65536)
                except ConnectionResetError:
                    continue
                except OSError:
                    return
                if client[0] is None:
                    continue
                host, port = asked_at.get(src, src)
                self._deliver(relay, client[0], host, port, d)

        threading.Thread(target=inbound, daemon=True).start()
        threading.Thread(target=outbound, daemon=True).start()
        try:  # the association lives as long as the control connection
            c.settimeout(None)
            while c.recv(1024):
                pass
        except OSError:
            pass
        finally:
            relay.close()
            out.close()


#: Documentation addresses (RFC 5737): never a real host, so nothing in a test
#: can be mistaken for the machine's own address.
EXIT = "203.0.113.7"
OTHER_EXIT = "198.51.100.4"


def coherent(*, exit_ip: str = EXIT, user: Optional[str] = None,
             password: Optional[str] = None, relay_host: str = "127.0.0.1", rtt_ms: int = 0,
             exit_host: str = "127.0.0.1") -> Gateway:
    """TCP and UDP both work, and both leave from ``exit_ip``."""
    return Gateway(udp="relay", tcp_exit=exit_ip, udp_exit=exit_ip,
                   user=user, password=password, relay_host=relay_host, rtt_ms=rtt_ms, exit_host=exit_host)


def tcp_only(*, exit_ip: str = EXIT, user: Optional[str] = None,
             password: Optional[str] = None, relay_host: str = "127.0.0.1", exit_host: str = "127.0.0.1") -> Gateway:
    """TCP works; UDP ASSOCIATE is refused with reply code 7."""
    return Gateway(udp="refuse", tcp_exit=exit_ip, udp_exit=exit_ip,
                   user=user, password=password, relay_host=relay_host, exit_host=exit_host)


def mismatched(*, tcp_exit: str = EXIT, udp_exit: str = OTHER_EXIT,
               user: Optional[str] = None, password: Optional[str] = None, relay_host: str = "127.0.0.1",
               exit_host: str = "127.0.0.1") -> Gateway:
    """UDP is granted and STUN answers, with an address that is not the TCP exit."""
    assert udp_exit != tcp_exit
    return Gateway(udp="relay", tcp_exit=tcp_exit, udp_exit=udp_exit,
                   user=user, password=password, relay_host=relay_host, exit_host=exit_host)


def silent_udp(*, exit_ip: str = EXIT, user: Optional[str] = None, password: Optional[str] = None,
               relay_host: str = "127.0.0.1", exit_host: str = "127.0.0.1") -> Gateway:
    """TCP works and UDP ASSOCIATE is granted, but no STUN request is ever answered: the core's measurement is
    ``unmeasured``."""
    return Gateway(udp="relay", tcp_exit=exit_ip, udp_exit=exit_ip, user=user, password=password,
                   relay_host=relay_host, exit_host=exit_host, stun_answers=False)


# ----------------------------------------------------------------------------------------------------------------
#  A tiny TCP-only SOCKS5 server for the e2e tests (the fixture `socks5_tcp_only` of conftest.py makes one).
# ----------------------------------------------------------------------------------------------------------------
class Socks5TcpOnly:
    """Minimal SOCKS5: no-auth, CONNECT (TCP) relayed to the lab, UDP ASSOCIATE refused.

    Reproduces a residential TCP-only proxy: pages load over TCP, but WebRTC's
    UDP path is dead - which (for a no-camera page in default_address_only mode)
    is exactly what made the default-route probe fail and ICE return zero
    candidates before Fix C.

    Hermetic like :class:`Gateway`: a CONNECT to a destination that is not the lab's (:func:`is_lab_destination`) is refused with reply 2 and
    recorded in :attr:`guard` (written under ``$RUNNER_TEMP/invpw-evidence/socks5-tcp-only/`` when the server is closed).
    """

    def __init__(self, exit_ip: Optional[str] = None):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self.port = self._srv.getsockname()[1]
        #: It relays a CONNECT (to the lab), so it declares its exit (what a launch behind it is told: the lab's, unless a test gives another).
        self.exit_ip = exit_ip or _lab_exit()
        declare_exit(self.server, self.exit_ip)
        self.udp_associate_attempts = 0
        #: What refuses (and records) every CONNECT that is not the lab's.
        self.guard = LabGuard("socks5-tcp-only")
        self._stop = False
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    @property
    def server(self) -> str:
        return "socks5://127.0.0.1:%d" % self.port

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _recv_exact(self, sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _handle(self, conn):
        try:
            head = self._recv_exact(conn, 2)
            if not head or head[0] != 0x05:
                conn.close()
                return
            nmethods = head[1]
            self._recv_exact(conn, nmethods)
            conn.sendall(b"\x05\x00")  # no-auth
            req = self._recv_exact(conn, 4)
            if not req:
                conn.close()
                return
            ver, cmd, _, atyp = req
            if atyp == 0x01:
                addr = socket.inet_ntoa(self._recv_exact(conn, 4))
            elif atyp == 0x03:
                ln = self._recv_exact(conn, 1)[0]
                addr = self._recv_exact(conn, ln).decode("ascii", "ignore")
            elif atyp == 0x04:
                addr = socket.inet_ntop(socket.AF_INET6, self._recv_exact(conn, 16))
            else:
                conn.close()
                return
            port = struct.unpack("!H", self._recv_exact(conn, 2))[0]
            if cmd != 0x01:  # not CONNECT (e.g. UDP ASSOCIATE) → refuse
                self.udp_associate_attempts += 1
                conn.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")  # cmd not supported
                conn.close()
                return
            if asks_the_world(self.server, addr, port):
                conn.sendall(b"\x05\x02\x00\x01\x00\x00\x00\x00\x00\x00")  # not allowed: the exit is declared, not asked of the internet
                conn.close()
                return
            if not self.guard.admits(addr, port, atyp, "tcp"):
                conn.sendall(b"\x05\x02\x00\x01\x00\x00\x00\x00\x00\x00")  # not allowed: nothing outside the lab is carried
                conn.close()
                return
            try:
                upstream = socket.create_connection((addr, port), timeout=30)
            except OSError:
                conn.sendall(b"\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00")  # host unreachable
                conn.close()
                return
            conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")  # success
            self._relay(conn, upstream)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

    def _relay(self, a, b):
        try:
            while True:
                r, _, _ = select.select([a, b], [], [], 30)
                if not r:
                    break
                for s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    (b if s is a else a).sendall(data)
        finally:
            for s in (a, b):
                try:
                    s.close()
                except Exception:
                    pass

    def close(self):
        self._stop = True
        forget_exit(self.server)
        try:
            self._srv.close()
        except Exception:
            pass
        self.guard.write_evidence()
