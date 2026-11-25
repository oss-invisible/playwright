"""The guard of tests/_no_network.py: a destination on this host passes, anything else is refused and recorded, and the record fails the test.

Each case installs its own guard on top of the one the conftest gives every test that is not e2e; the inner guard refuses first, so nothing
reaches the outer one and nothing leaves the machine: a refused connect never sends a packet.
"""
from __future__ import annotations

import socket
import threading

import pytest

import _no_network

pytestmark = pytest.mark.unit

OUTSIDE = ("1.1.1.1", 443)  # never contacted: the guard refuses before the system call


@pytest.mark.parametrize("host", ["127.0.0.1", "127.8.9.10", "::1", "0.0.0.0", "::", "localhost", "LocalHost", "a.localhost", None, "",
                                  "198.51.100.9", "192.0.2.1", "203.0.113.7", "2001:db8::1", "::ffff:127.0.0.1", "[::1]"])
def test_this_host_is_this_host(host):
    assert _no_network.is_on_this_host(host)


@pytest.mark.parametrize("host", ["1.1.1.1", "8.8.8.8", "10.0.0.1", "192.168.1.1", "2606:4700::1111", "api.ipify.org", "example.com",
                                  b"icanhazip.com", "::ffff:8.8.8.8", "fe80::1%lo"])
def test_outside_is_outside(host):
    assert not _no_network.is_on_this_host(host)


def test_a_connect_outside_is_refused_named_and_fails_the_test(monkeypatch):
    guard = _no_network.install(monkeypatch, "tests/test_x.py::test_y")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        with pytest.raises(ConnectionRefusedError) as refused:
            s.connect(OUTSIDE)
    message = str(refused.value)
    for part in ("WHERE: tests/test_x.py::test_y", "WHAT: connect to 1.1.1.1:443", "FIX:", "RULE:"):
        assert part in message, message
    assert guard.verdict() is not None and "connect to 1.1.1.1:443" in guard.verdict()


def test_a_refusal_the_code_swallowed_still_fails_the_test(monkeypatch):
    """The code under test may catch the refusal and carry on (an offline fallback): the record is what fails the test."""
    guard = _no_network.install(monkeypatch, "t")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.connect_ex(OUTSIDE)
        except OSError:
            pass
    assert guard.verdict() is not None


def test_a_datagram_and_a_name_lookup_outside_are_refused(monkeypatch):
    guard = _no_network.install(monkeypatch, "t")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        with pytest.raises(ConnectionRefusedError):
            s.sendto(b"x", ("8.8.8.8", 53))
    with pytest.raises(ConnectionRefusedError):
        socket.getaddrinfo("api.ipify.org", 443)
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("icanhazip.com", 443), timeout=1)
    assert guard.attempts == ["datagram to 8.8.8.8:53", "name lookup of api.ipify.org", "name lookup of icanhazip.com"]


def test_a_client_thread_of_the_test_is_recorded(monkeypatch):
    guard = _no_network.install(monkeypatch, "t")

    def client():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.connect(OUTSIDE)
            except OSError:
                pass

    worker = threading.Thread(target=client)
    worker.start()
    worker.join(10)
    assert guard.attempts == ["connect to 1.1.1.1:443"]


def test_this_host_passes_and_leaves_no_record(monkeypatch):
    """The control: a server on loopback is reached through the same doors, by address and by name, and the test stays green."""
    guard = _no_network.install(monkeypatch, "t")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        with socket.create_connection(("localhost", port), timeout=5) as client:
            accepted, _ = server.accept()
            accepted.close()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as other:
            assert other.connect_ex(("127.0.0.1", port)) == 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        receiver.bind(("127.0.0.1", 0))
        sender.sendto(b"x", receiver.getsockname())
    assert guard.verdict() is None


def test_the_address_this_host_sends_from_is_this_host(monkeypatch):
    """A relay of the tests that leaves from the address of the host (tests/_host_address.py) is on this host: a datagram to it stays here."""
    own = _no_network.own_addresses()
    if not own:
        pytest.skip("this host has no default route, so no address of its own beside loopback")
    guard = _no_network.install(monkeypatch, "t")
    with socket.socket(socket.AF_INET if ":" not in own[0] else socket.AF_INET6, socket.SOCK_DGRAM) as receiver:
        receiver.bind((own[0], 0))
        with socket.socket(receiver.family, socket.SOCK_DGRAM) as sender:
            sender.sendto(b"x", receiver.getsockname()[:2])
    assert guard.verdict() is None
    assert all(_no_network.is_on_this_host(a, own) for a in own)


# The asyncio doors, for every loop kind this system has: the selector loop everywhere, and on Windows the Proactor loop too (the default
# there), which connects with ConnectEx and sends with WSASendTo, never through socket.socket: the unit tier of the round runs on both systems.
import asyncio  # noqa: E402
import sys  # noqa: E402

LOOPS = ["selector"] + (["proactor"] if sys.platform == "win32" else [])


def _run(kind, coroutine_function):
    loop = asyncio.SelectorEventLoop() if kind == "selector" else asyncio.ProactorEventLoop()
    try:
        return loop.run_until_complete(coroutine_function())
    finally:
        loop.close()


@pytest.mark.parametrize("kind", LOOPS)
def test_an_asyncio_connection_to_a_numeric_address_outside_is_refused(monkeypatch, kind):
    guard = _no_network.install(monkeypatch, "t")

    async def go():
        with pytest.raises(ConnectionRefusedError):
            await asyncio.open_connection(*OUTSIDE)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setblocking(False)
            with pytest.raises(ConnectionRefusedError):
                await asyncio.get_running_loop().sock_connect(s, OUTSIDE)

    _run(kind, go)
    assert guard.attempts == ["asyncio connect to 1.1.1.1:443"], guard.attempts


@pytest.mark.parametrize("kind", LOOPS)
def test_an_asyncio_datagram_outside_is_refused(monkeypatch, kind):
    guard = _no_network.install(monkeypatch, "t")

    async def go():
        loop = asyncio.get_running_loop()
        with pytest.raises(ConnectionRefusedError):
            await loop.create_datagram_endpoint(asyncio.DatagramProtocol, remote_addr=("8.8.8.8", 53))
        transport, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0))
        try:
            # the selector transport hands a refused send to its protocol (error_received) instead of raising it: the record is what counts
            try:
                transport.sendto(b"x", ("8.8.4.4", 53))
            except ConnectionRefusedError:
                pass
        finally:
            transport.close()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setblocking(False)
            with pytest.raises(ConnectionRefusedError):
                await loop.sock_sendto(s, b"x", ("8.8.8.8", 5353))

    _run(kind, go)
    assert guard.attempts == ["asyncio datagram endpoint to 8.8.8.8:53", "asyncio datagram to 8.8.4.4:53", "asyncio datagram to 8.8.8.8:5353"] \
        or guard.attempts == ["asyncio datagram endpoint to 8.8.8.8:53", "datagram to 8.8.4.4:53", "asyncio datagram to 8.8.8.8:5353"], guard.attempts


@pytest.mark.parametrize("kind", LOOPS)
def test_asyncio_on_this_host_passes(monkeypatch, kind):
    """The control: a server on loopback, reached through asyncio by address and a datagram to a loopback receiver, leave no record."""
    guard = _no_network.install(monkeypatch, "t")

    async def go():
        loop = asyncio.get_running_loop()
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        server.close()
        await server.wait_closed()
        receiver, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0))
        sender, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, remote_addr=receiver.get_extra_info("sockname"))
        sender.sendto(b"x")
        sender.close()
        receiver.close()

    _run(kind, go)
    assert guard.verdict() is None


# The one declared third party: the version gate asks the index. Faked below the guard, so nothing leaves the machine here either.
INDEX_ADDRESS = "8.8.4.4"  # stands for the index: faked, never contacted


def _fake_index(monkeypatch):
    """The system lookup answers the index's address for pypi.org, and the system connect records instead of connecting."""
    reached = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (INDEX_ADDRESS, port))])
    monkeypatch.setattr(socket.socket, "connect", lambda self, address: reached.append(address))
    return reached


def test_the_version_gate_may_ask_the_index_and_only_the_index(monkeypatch):
    reached = _fake_index(monkeypatch)
    guard = _no_network.install(monkeypatch, "tests/test_version_is_not_taken.py::test_the_declared_version_is_not_already_published")
    (family, kind, proto, _name, address), = socket.getaddrinfo("pypi.org", 443)
    with socket.socket(family, kind) as s:
        s.connect(address)
    assert [a for a in reached if a[1] == 443] == [(INDEX_ADDRESS, 443)]  # (the guard's own probe of this host's address is recorded too)
    with pytest.raises(ConnectionRefusedError):
        socket.getaddrinfo("files.example.net", 443)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s, pytest.raises(ConnectionRefusedError):
        s.connect(OUTSIDE)
    assert guard.attempts == ["name lookup of files.example.net", "connect to 1.1.1.1:443"]


def test_no_other_file_may_ask_the_index(monkeypatch):
    reached = _fake_index(monkeypatch)
    guard = _no_network.install(monkeypatch, "tests/test_x.py::test_y")
    with pytest.raises(ConnectionRefusedError):
        socket.getaddrinfo("pypi.org", 443)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s, pytest.raises(ConnectionRefusedError):
        s.connect((INDEX_ADDRESS, 443))
    assert [a for a in reached if a[1] == 443] == [] and guard.attempts == ["name lookup of pypi.org", "connect to %s:443" % INDEX_ADDRESS]


def test_the_declared_third_parties_are_the_version_gate_alone():
    """A new entry is a decision: this test changes with it, in the same diff, where a reviewer sees it."""
    assert _no_network.DECLARED_THIRD_PARTIES == {"tests/test_version_is_not_taken.py": ("pypi.org",)}
