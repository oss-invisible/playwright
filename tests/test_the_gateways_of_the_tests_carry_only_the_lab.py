"""The proxies of the tests carry only the lab's destinations: any other is refused and recorded, never forwarded.

The e2e tests run in the gate of the candidate, where no test may depend on a real service. A proxy of the tests that relayed a CONNECT or a datagram for
real would carry the background requests of the browser behind it (a name of the vendor, a captive-portal check, a telemetry endpoint) to the internet, and
a verdict would then depend on whether the runner reaches them. What this holds, on the helpers themselves (no browser):

* :func:`_socks_servers.is_lab_destination`: loopback, the documentation ranges, the addresses of this host's own interfaces and ``localhost`` are the lab's;
  a public address, a name of the internet and a public IPv6 address are not (an IPv4-mapped one is judged as the IPv4 address it carries);
* the base :class:`_socks_servers.Gateway` (and with it ``Gateway(tcp_from_exit=True)``): a CONNECT to a public address or to a name is answered with reply 2
  and never opened upstream, a CONNECT to a loopback server is carried; a datagram of the UDP relay to a public address or a name is dropped, one to a lab
  address is sent; each refusal is recorded (time since the gateway started, address type, destination as received, port, protocol ``tcp`` or ``udp``);
* ``Socks5TcpOnly`` and the SOCKS5 server of ``test_proxy_socks_auth_e2e.py`` (``_Socks5AuthRecorder``) do the same for CONNECT;
* the record is written, always, when the gateway is closed, under ``$RUNNER_TEMP/invpw-evidence/<gateway>/`` (the folder the e2e jobs upload), once even
  if the gateway is closed twice; off a runner (no ``RUNNER_TEMP``) nothing is written;
* the gateway of ``test_ipv6_literal_connect_e2e.py``, which narrows the rule to the listener it carries to, still carries the mapped destination and refuses
  a loopback server that is not the one mapped.

Known-bad inputs: a CONNECT to ``8.8.8.8:443`` (a public address), to ``detectportal.firefox.com:80`` (a name) and to a public IPv6 address; a datagram to
``8.8.8.8:443`` and to a name; each must be refused and recorded, and nothing must be opened or sent for it.
"""
from __future__ import annotations

import json
import socket
import struct
import threading
import time

import pytest

import _socks_servers as servers
from _socks_servers import Socks5TcpOnly, coherent, is_lab_destination

pytestmark = pytest.mark.unit

PUBLIC_V4 = "8.8.8.8"
PUBLIC_V6 = "2606:4700:4700::1111"
NAME = "detectportal.firefox.com"

#: Address types of a SOCKS5 request (RFC 1928).
V4, NAMED, V6 = 1, 3, 4


@pytest.fixture(autouse=True)
def _no_own_addresses(monkeypatch):
    """The addresses of this machine's interfaces are not a fact of the test (a runner may have a public one): none, unless a test names them."""
    monkeypatch.setattr(servers, "_own_addresses", lambda: set())


def _address(host: str, atyp: int) -> bytes:
    if atyp == V4:
        return b"\x01" + socket.inet_aton(host)
    if atyp == V6:
        return b"\x04" + socket.inet_pton(socket.AF_INET6, host)
    return b"\x03" + bytes([len(host)]) + host.encode()


def _connect(port: int, host: str, dport: int, atyp: int, credentials=None):
    """A SOCKS5 CONNECT to ``port`` of 127.0.0.1: the connected socket and the reply code."""
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    if credentials is None:
        s.sendall(b"\x05\x01\x00")
        assert s.recv(2) == b"\x05\x00"
    else:
        user, password = (c.encode() for c in credentials)
        s.sendall(b"\x05\x01\x02")
        assert s.recv(2) == b"\x05\x02"
        s.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
        assert s.recv(2) == b"\x01\x00"
    s.sendall(b"\x05\x01\x00" + _address(host, atyp) + struct.pack("!H", dport))
    reply = b""
    while len(reply) < 10:
        chunk = s.recv(10 - len(reply))
        if not chunk:
            break
        reply += chunk
    return s, (reply[1] if len(reply) > 1 else None)


class _Echo:
    """A TCP echo server on 127.0.0.1: the lab's server."""

    def __init__(self):
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    @staticmethod
    def _echo(conn):
        try:
            data = conn.recv(4096)
            conn.sendall(data)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self._listener.close()


def _the_echo_is_carried(port: int, echo_port: int, credentials=None) -> None:
    s, code = _connect(port, "127.0.0.1", echo_port, V4, credentials)
    try:
        assert code == 0, "a CONNECT to a loopback server was refused (reply %s)" % code
        s.sendall(b"ping")
        assert s.recv(4) == b"ping"
    finally:
        s.close()


def _fields(record: dict) -> tuple:
    return record["atyp"], record["destination"], record["port"], record["protocol"]


# -- what is the lab's -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("host", [
    "127.0.0.1", "127.9.9.9", "::1", "::ffff:127.0.0.1", "[::1]", "198.51.100.9", "198.51.100.5", "203.0.113.80", "203.0.113.7", "192.0.2.1",
    "2001:db8::1", "[2001:db8::1]", "localhost", "LOCALHOST", "localhost.", "page.localhost",
])
def test_these_are_the_labs(host):
    assert is_lab_destination(host), host


@pytest.mark.parametrize("host", [
    PUBLIC_V4, "1.1.1.1", PUBLIC_V6, "::ffff:8.8.8.8", "10.9.8.7", "192.168.1.20", "fe80::1", NAME, "api.ipify.org", "stun.l.google.com",
    "example.com", "page.test", "localhost.example.com", "",
])
def test_these_are_not_the_labs(host):
    assert not is_lab_destination(host), host


def test_an_address_of_an_interface_of_this_host_is_the_labs_and_no_other_private_one(monkeypatch):
    monkeypatch.setattr(servers, "_own_addresses", lambda: {"10.9.8.7", "fd00::7"})
    assert is_lab_destination("10.9.8.7") and is_lab_destination("fd00::7") and is_lab_destination("::ffff:10.9.8.7")
    assert not is_lab_destination("10.9.8.8") and not is_lab_destination("fd00::8")


# -- the TCP side of the base gateway --------------------------------------------------------------------------------------
@pytest.mark.parametrize("host, atyp", [(PUBLIC_V4, V4), (NAME, NAMED), (PUBLIC_V6, V6)], ids=["public_ipv4", "name", "public_ipv6"])
def test_a_connect_to_a_public_destination_is_refused_recorded_and_never_opened(host, atyp):
    with coherent() as gateway:
        opened = []
        gateway._open_upstream = lambda target: opened.append(target) or pytest.fail("the gateway opened a connection to %s" % (target,))
        s, code = _connect(gateway.port, host, 443, atyp)
        s.close()
        assert code == 2, "the reply to a CONNECT outside the lab is 2 (not allowed): %s" % code
        assert opened == []
        assert len(gateway.guard.records) == 1
        record = gateway.guard.records[0]
        assert _fields(record) == (atyp, host, 443, "tcp"), record
        assert record["t"] >= 0
        assert gateway.guard.refused("tcp") == [(host, 443)] and gateway.guard.refused("udp") == []
        assert (host, 443) in gateway.connects, "the CONNECT is still listed: the test sees what the browser asked for"


def test_a_connect_to_a_loopback_server_is_carried_and_leaves_no_refusal():
    echo = _Echo()
    try:
        with coherent() as gateway:
            _the_echo_is_carried(gateway.port, echo.port)
            assert gateway.guard.records == []
    finally:
        echo.close()


def test_a_gateway_with_credentials_refuses_the_same_way():
    with coherent(user="alice", password="s3cret") as gateway:
        s, code = _connect(gateway.port, PUBLIC_V4, 80, V4, ("alice", "s3cret"))
        s.close()
        assert code == 2 and gateway.guard.refused("tcp") == [(PUBLIC_V4, 80)]


def test_a_gateway_that_leaves_from_its_exit_refuses_a_public_connect_too():
    """``Gateway(tcp_from_exit=True)``: the connection of the gateway would leave from ``exit_host``; to a public address it is never made."""
    with servers.Gateway(udp="refuse", tcp_exit="203.0.113.7", udp_exit="203.0.113.7", exit_host="127.0.0.2", tcp_from_exit=True) as gateway:
        opened = []
        gateway._open_upstream = lambda target: opened.append(target) or pytest.fail("the gateway opened a connection to %s" % (target,))
        s, code = _connect(gateway.port, PUBLIC_V4, 443, V4)
        s.close()
        assert code == 2 and opened == [] and gateway.guard.refused("tcp") == [(PUBLIC_V4, 443)]
    echo = _Echo()
    try:
        with servers.Gateway(udp="refuse", tcp_exit="203.0.113.7", udp_exit="203.0.113.7", exit_host="127.0.0.2", tcp_from_exit=True) as gateway:
            _the_echo_is_carried(gateway.port, echo.port)
            assert gateway.guard.records == []
    finally:
        echo.close()


def test_the_questions_the_gateway_answers_itself_are_not_refusals():
    """The exit question (``api.ipify.org:80``) and the page of ``WEB_NAME`` are the lab's own answers, given before the rule."""
    with coherent() as gateway:
        s, code = _connect(gateway.port, "api.ipify.org", 80, NAMED)
        try:
            assert code == 0
            s.sendall(b"GET / HTTP/1.1\r\nHost: api.ipify.org\r\n\r\n")
            assert gateway.tcp_exit.encode() in s.recv(4096)
        finally:
            s.close()
        s, code = _connect(gateway.port, servers.WEB_NAME, 80, NAMED)
        s.close()
        assert code == 0 and gateway.guard.records == []


# -- the UDP side of the base gateway --------------------------------------------------------------------------------------
def _associate(gateway):
    control = socket.create_connection(("127.0.0.1", gateway.port), timeout=5)
    control.sendall(b"\x05\x01\x00")
    assert control.recv(2) == b"\x05\x00"
    control.sendall(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")
    reply = b""
    while len(reply) < 10:
        reply += control.recv(10 - len(reply))
    assert reply[1] == 0, "the association was refused: %r" % reply
    return control, (socket.inet_ntoa(reply[4:8]), int.from_bytes(reply[8:10], "big"))


def _datagram(host: str, port: int, atyp: int, payload: bytes) -> bytes:
    return b"\x00\x00\x00" + _address(host, atyp) + struct.pack("!H", port) + payload


def test_a_datagram_to_a_public_destination_is_dropped_recorded_and_never_sent_and_one_to_a_lab_address_is_sent():
    from _waits import wait_until

    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(10)
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))
    with coherent() as gateway:
        sent = []
        carry = gateway._later
        gateway._later = lambda fn, what="a datagram": (sent.append(what), carry(fn, what))
        control, relay = _associate(gateway)
        try:
            client.sendto(_datagram(PUBLIC_V4, 443, V4, b"quic"), relay)
            client.sendto(_datagram(NAME, 443, NAMED, b"quic"), relay)
            client.sendto(_datagram(PUBLIC_V6, 443, V6, b"quic"), relay)
            # the control: the same relay carries a datagram to a lab address (this one is read by the server below)
            client.sendto(_datagram("127.0.0.1", server.getsockname()[1], V4, b"lab"), relay)
            data, _peer = server.recvfrom(4096)
            assert data == b"lab"
            wait_until(lambda: len(gateway.datagrams) >= 4, 30, lambda: time.sleep(0.01))
            assert len(sent) == 1 and sent[0].startswith("the datagram to 127.0.0.1:"), "only the lab's datagram was sent: %s" % sent
            assert [_fields(r) for r in gateway.guard.records] == [
                (V4, PUBLIC_V4, 443, "udp"), (NAMED, NAME, 443, "udp"), (V6, PUBLIC_V6, 443, "udp")]
            assert gateway.guard.refused("tcp") == []
            assert {(h, p) for h, p, _d in gateway.datagrams} >= {(PUBLIC_V4, 443), (NAME, 443)}, "the datagrams read are still listed"
            assert gateway.send_errors == []
        finally:
            control.close()
            client.close()
            server.close()


def test_the_documentation_addresses_and_the_fake_stun_are_the_labs_on_the_relay():
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))
    client.settimeout(10)
    with coherent() as gateway:
        control, relay = _associate(gateway)
        try:
            request = b"\x00\x01\x00\x00\x21\x12\xa4\x42" + b"txid-of-12by"
            for number in (servers.STUN_NUMBER, servers.MEASURE_NUMBER):
                client.sendto(_datagram(number, servers.STUN_PORT, V4, request), relay)
                answer, _peer = client.recvfrom(4096)
                assert answer[10:12] == b"\x01\x01"
            client.sendto(_datagram(servers.STUN_NAME, servers.STUN_PORT, NAMED, request), relay)
            answer, _peer = client.recvfrom(4096)
            assert answer[10:12] == b"\x01\x01"
            assert gateway.guard.records == []
        finally:
            control.close()
            client.close()


def test_forward_only_to_only_narrows():
    """A host the test lists that is not the lab's is not forwarded either: the list never reopens the rule."""
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))
    with coherent() as gateway:
        gateway.forward_only_to = {PUBLIC_V4}
        sent = []
        carry = gateway._later
        gateway._later = lambda fn, what="a datagram": (sent.append(what), carry(fn, what))
        control, relay = _associate(gateway)
        try:
            from _waits import wait_until

            client.sendto(_datagram(PUBLIC_V4, 443, V4, b"quic"), relay)
            wait_until(lambda: gateway.guard.records, 30, lambda: time.sleep(0.01))
            assert sent == [] and gateway.guard.refused("udp") == [(PUBLIC_V4, 443)]
        finally:
            control.close()
            client.close()


# -- the other proxies -----------------------------------------------------------------------------------------------------
def test_the_tcp_only_proxy_refuses_a_public_connect_and_carries_a_loopback_one():
    echo = _Echo()
    proxy = Socks5TcpOnly()
    try:
        for host, atyp in ((PUBLIC_V4, V4), (NAME, NAMED), (PUBLIC_V6, V6)):
            s, code = _connect(proxy.port, host, 443, atyp)
            s.close()
            assert code == 2, (host, code)
        assert [_fields(r) for r in proxy.guard.records] == [(V4, PUBLIC_V4, 443, "tcp"), (NAMED, NAME, 443, "tcp"), (V6, PUBLIC_V6, 443, "tcp")]
        _the_echo_is_carried(proxy.port, echo.port)
        assert len(proxy.guard.records) == 3
    finally:
        proxy.close()
        echo.close()


def test_the_proxy_that_asks_for_the_exit_still_fails_the_test_that_asked_the_world():
    """The question of the exit keeps its own backstop (``asks_the_world``) ahead of the rule, so a proxy that forgot to declare its exit is a red with its name."""
    proxy = Socks5TcpOnly()
    try:
        s, code = _connect(proxy.port, "api.ipify.org", 443, NAMED)
        s.close()
        assert code == 2 and len(servers.nobody_asked_the_world()) == 1
        assert proxy.guard.records == [], "the question of the exit is the backstop's, not a destination the rule refused"
    finally:
        proxy.close()


def test_the_socks5_server_with_authentication_refuses_a_public_connect_and_carries_a_loopback_one():
    from test_proxy_socks_auth_e2e import _USER, _PASS, _Socks5AuthRecorder

    echo = _Echo()
    proxy = _Socks5AuthRecorder()
    try:
        s, code = _connect(proxy.port, PUBLIC_V4, 443, V4, (_USER, _PASS))
        s.close()
        assert code == 2
        s, code = _connect(proxy.port, NAME, 80, NAMED, (_USER, _PASS))
        s.close()
        assert code == 2
        assert [_fields(r) for r in proxy.guard.records] == [(V4, PUBLIC_V4, 443, "tcp"), (NAMED, NAME, 80, "tcp")]
        _the_echo_is_carried(proxy.port, echo.port, (_USER, _PASS))
        assert len(proxy.guard.records) == 2 and (_USER, _PASS) in proxy.seen_creds
    finally:
        proxy.close()
        echo.close()


def test_the_doh_gateway_records_what_it_refuses_and_its_relay_is_the_labs_only():
    from _doh_servers import DohGateway, FakeDoh

    with FakeDoh({}) as doh, DohGateway("gw", doh, udp="relay") as gateway:
        s, code = _connect(gateway.port, PUBLIC_V4, 443, V4)
        s.close()
        assert code == 2 and gateway.refused == [(PUBLIC_V4, 443)]
        assert [_fields(r) for r in gateway.guard.records] == [(V4, PUBLIC_V4, 443, "tcp")]


def test_the_gateway_of_the_ipv6_literal_test_carries_only_what_it_maps():
    from test_ipv6_literal_connect_e2e import DOCUMENTATION, _GatewayToALocalListener, _Listener

    with _Listener() as listener:
        gateway = _GatewayToALocalListener({(DOCUMENTATION, 80): ("127.0.0.1", listener.port)})
        try:
            s, code = _connect(gateway.port, DOCUMENTATION, 80, V6)
            try:
                assert code == 0, "the mapped destination was not carried"
                s.sendall(b"GET / HTTP/1.1\r\n\r\n")
                assert s.recv(4096).startswith(b"HTTP/1.1 200")
            finally:
                s.close()
            s, code = _connect(gateway.port, "127.0.0.1", listener.port ^ 1, V4)        # loopback, but not the listener it maps to
            s.close()
            assert code == 2
            s, code = _connect(gateway.port, PUBLIC_V4, 443, V4)
            s.close()
            assert code == 2
            assert gateway.refused == [("127.0.0.1", listener.port ^ 1), (PUBLIC_V4, 443)]
            assert gateway.documentation_connects() == [(DOCUMENTATION, 80, V6)]
            assert [r["outcome"] for r in gateway.records] == [
                "carried to the listener of the lab", "refused", "refused"]
        finally:
            gateway.close()


# -- the record ------------------------------------------------------------------------------------------------------------
def test_the_record_is_written_when_the_gateway_is_closed_under_the_evidence_folder_and_once(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    gateway = coherent()
    s, code = _connect(gateway.port, NAME, 80, NAMED)
    s.close()
    assert code == 2
    gateway.close()
    gateway.close()
    files = sorted((tmp_path / "invpw-evidence").rglob("*.json"))
    assert len(files) == 1, files
    assert files[0].parent.parent.name == "socks5-gateway" and files[0].name == "refused-destinations.json"
    written = json.loads(files[0].read_text(encoding="utf-8"))
    assert written["gateway"] == "socks5-gateway"
    assert [_fields(r) for r in written["refused"]] == [(NAMED, NAME, 80, "tcp")] and written["refused"][0]["t"] >= 0


def test_a_gateway_that_refused_nothing_still_leaves_its_record(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    proxy = Socks5TcpOnly()
    proxy.close()
    files = sorted((tmp_path / "invpw-evidence" / "socks5-tcp-only").rglob("refused-destinations.json"))
    assert len(files) == 1 and json.loads(files[0].read_text(encoding="utf-8"))["refused"] == []


def test_off_a_runner_nothing_is_written(tmp_path, monkeypatch):
    monkeypatch.delenv("RUNNER_TEMP", raising=False)
    monkeypatch.chdir(tmp_path)
    gateway = coherent()
    s, _code = _connect(gateway.port, NAME, 80, NAMED)
    s.close()
    gateway.close()
    assert list(tmp_path.rglob("*")) == []
    assert servers.LabGuard("x").write_evidence() is None
