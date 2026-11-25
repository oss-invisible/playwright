"""A test that is not e2e never leaves the machine: every connection, datagram and name lookup it makes goes to this host or is refused.

The markers say it already (pyproject.toml: ``unit`` is "no network", ``integration`` is "no network"), and nothing enforced it:
``tests/unit/test_config_public.py`` composed the default prefs with no declared exit, the core asked api.ipify.org and icanhazip.com
where it leaves from, and the test failed on a machine whose network refused them. A test cannot fail for the reasons of a third party.

:func:`install` patches the four doors of the standard library that every client in the process goes through (``socket.socket.connect``,
``connect_ex`` and ``sendto``, and ``socket.getaddrinfo``): a destination on this host passes, anything else is refused with
:class:`LeftTheMachine` (a ``ConnectionRefusedError``, so the code under test sees what an offline machine shows it) AND recorded, so the test
fails at its teardown even when the code under test swallowed the refusal. The record names the test, the door and the destination.

asyncio has doors of its own, and on Windows they skip the four above: the Proactor loop connects with ``ConnectEx`` and sends datagrams with
``WSASendTo`` (``_overlapped``), never through ``socket.socket``. So the guard also patches the loop methods every asyncio client goes through, for
both loop kinds: ``BaseEventLoop.create_connection`` and ``create_datagram_endpoint`` (``remote_addr``), ``sock_connect`` and ``sock_sendto`` of the
selector and of the Proactor loop, and the Proactor datagram transport's ``sendto``.

What it cannot see, on any system: a socket made from ``_socket`` directly or used through ``_overlapped`` by hand (no library of this project
does), native code in the same process, and a child process (a subprocess has its own interpreter: the e2e are where a browser runs).

On this host: a loopback address (127/8, ::1), the unspecified address (0.0.0.0, ::, which a connect reaches on this host), a name that is
``localhost`` or ends in ``.localhost``, and an AF_UNIX path. The documentation ranges (192.0.2/24, 198.51.100/24, 203.0.113/24, 2001:db8::/32)
pass too: they are never routed, and the lab gives one of them to an interface of this host (the exit of the lab, tests/_lab_egress.py).
And the address this host sends from (its default route's, tests/_host_address.py): a relay of the tests that leaves from the address of the
host is on this host, and a datagram to it never leaves the machine.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
from typing import Any, List, Optional, Sequence, Tuple

_DOCUMENTATION = tuple(ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32"))

RULE = ("a test that is not e2e never leaves the machine (pyproject.toml markers: unit and integration are 'no network'): "
        "a test cannot fail for the reasons of a third party")
FIX = ("declare the value the code would ask the network for (timezone=, locale=, proxy= with a local gateway), use a server of the tests "
       "on loopback (tests/_servers.py, the local_egress_echo fixture), or mark the test e2e if it is one")


class LeftTheMachine(ConnectionRefusedError):
    """A destination outside this host, refused by the guard of the tests."""


def own_addresses() -> Tuple[str, ...]:
    """The addresses this host sends from, IPv4 and IPv6 (the source its default route picks). A UDP connect sends no packet; a family with
    no route has none. Read BEFORE the doors are patched."""
    found = []
    for family, probe in ((socket.AF_INET, ("192.0.2.1", 9)), (socket.AF_INET6, ("2001:db8::1", 9))):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as s:
                s.connect(probe)
                found.append(s.getsockname()[0].split("%", 1)[0])
        except OSError:
            pass
    return tuple(found)


def is_on_this_host(host: Any, own: Sequence[str] = ()) -> bool:
    """Whether ``host`` (an address, a name, or None) is on this host, the way the guard decides it; ``own`` are the addresses of this host
    (:func:`own_addresses`)."""
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).strip("[]").split("%", 1)[0]
    if host == "" or host.lower() == "localhost" or host.lower().endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False  # a name: resolving it is already a question to the network
    if getattr(address, "ipv4_mapped", None) is not None:
        address = address.ipv4_mapped
    return (address.is_loopback or address.is_unspecified or any(address in net for net in _DOCUMENTATION)
            or any(address == ipaddress.ip_address(mine) for mine in own))


def _destination(address: Any, own: Sequence[str] = ()) -> Optional[str]:
    """``host:port`` of a socket address that leaves the machine, or None when it does not (AF_UNIX paths and this host)."""
    if not isinstance(address, tuple) or not address:
        return None  # an AF_UNIX path or an abstract name: this host
    host = address[0]
    if is_on_this_host(host, own):
        return None
    return "%s:%s" % (host, address[1] if len(address) > 1 else "?")


class Guard:
    """The record of one test: what it tried outside the machine. Thread-safe: a client thread of the test is recorded too."""

    def __init__(self, test: str) -> None:
        self.test = test
        self.attempts: List[str] = []
        self._lock = threading.Lock()

    def refuse(self, door: str, destination: str) -> LeftTheMachine:
        sentence = "%s %s" % (door, destination)
        with self._lock:
            if sentence not in self.attempts:
                self.attempts.append(sentence)
        return LeftTheMachine("WHERE: %s. WHAT: %s, outside this host, refused. FIX: %s. RULE: %s." % (self.test, sentence, FIX, RULE))

    def verdict(self) -> Optional[str]:
        """The failure of the test, or None when it stayed on this host."""
        if not self.attempts:
            return None
        return ("WHERE: %s. WHAT: the test tried to leave the machine: %s (each was refused). FIX: %s. RULE: %s."
                % (self.test, "; ".join(self.attempts), FIX, RULE))


def install(monkeypatch: Any, test: str) -> Guard:
    """Patch the four doors for the duration of ``monkeypatch``; return the record of ``test``."""
    guard = Guard(test)
    own = own_addresses()
    connect, connect_ex, sendto, getaddrinfo = (socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto,
                                                socket.getaddrinfo)

    def guarded_connect(self, address):
        where = _destination(address, own)
        if where is not None:
            raise guard.refuse("connect to", where)
        return connect(self, address)

    def guarded_connect_ex(self, address):
        where = _destination(address, own)
        if where is not None:
            raise guard.refuse("connect to", where)
        return connect_ex(self, address)

    def guarded_sendto(self, data, *rest):
        where = _destination(rest[-1] if rest else None, own)
        if where is not None:
            raise guard.refuse("datagram to", where)
        return sendto(self, data, *rest)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not is_on_this_host(host, own):
            raise guard.refuse("name lookup of", str(host))
        return getaddrinfo(host, *args, **kwargs)

    _install_asyncio(monkeypatch, guard, own)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", guarded_sendto)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    return guard


def _install_asyncio(monkeypatch: Any, guard: Guard, own: Sequence[str]) -> None:
    """The asyncio doors (module docstring), for the selector loop and, where it exists, the Proactor loop."""
    import asyncio.base_events as base_events
    import asyncio.selector_events as selector_events

    loops = [selector_events.BaseSelectorEventLoop]
    datagram_transports = []
    try:
        import asyncio.proactor_events as proactor_events
        loops.append(proactor_events.BaseProactorEventLoop)
        datagram_transports.append(proactor_events._ProactorDatagramTransport)
    except ImportError:  # pragma: no cover - the module exists on every system; its loop runs only on Windows
        pass

    create_connection = base_events.BaseEventLoop.create_connection
    create_datagram_endpoint = base_events.BaseEventLoop.create_datagram_endpoint

    async def guarded_create_connection(self, protocol_factory, host=None, port=None, *args, **kwargs):
        if host is not None and kwargs.get("sock") is None and not is_on_this_host(host, own):
            raise guard.refuse("asyncio connect to", "%s:%s" % (host, port))
        return await create_connection(self, protocol_factory, host, port, *args, **kwargs)

    async def guarded_create_datagram_endpoint(self, protocol_factory, local_addr=None, remote_addr=None, **kwargs):
        where = _destination(remote_addr, own)
        if where is not None:
            raise guard.refuse("asyncio datagram endpoint to", where)
        return await create_datagram_endpoint(self, protocol_factory, local_addr=local_addr, remote_addr=remote_addr, **kwargs)

    monkeypatch.setattr(base_events.BaseEventLoop, "create_connection", guarded_create_connection)
    monkeypatch.setattr(base_events.BaseEventLoop, "create_datagram_endpoint", guarded_create_datagram_endpoint)

    for loop in loops:
        sock_connect, sock_sendto = loop.sock_connect, loop.sock_sendto

        def make(sock_connect=sock_connect, sock_sendto=sock_sendto):
            async def guarded_sock_connect(self, sock, address):
                where = _destination(address, own)
                if where is not None:
                    raise guard.refuse("asyncio connect to", where)
                return await sock_connect(self, sock, address)

            async def guarded_sock_sendto(self, sock, data, address):
                where = _destination(address, own)
                if where is not None:
                    raise guard.refuse("asyncio datagram to", where)
                return await sock_sendto(self, sock, data, address)
            return guarded_sock_connect, guarded_sock_sendto

        guarded_sock_connect, guarded_sock_sendto = make()
        monkeypatch.setattr(loop, "sock_connect", guarded_sock_connect)
        monkeypatch.setattr(loop, "sock_sendto", guarded_sock_sendto)

    for transport in datagram_transports:
        def make_sendto(sendto=transport.sendto):
            def guarded_sendto(self, data, addr=None):
                where = _destination(addr, own)
                if where is not None:
                    raise guard.refuse("asyncio datagram to", where)
                return sendto(self, data, addr)
            return guarded_sendto
        monkeypatch.setattr(transport, "sendto", make_sendto())
