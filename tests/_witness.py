"""A witness: a STUN server (UDP) and a TCP listener on ONE local address and port, that writes down who contacted it.

What it is for: a datagram or a connection a browser sends DIRECT, around its proxy, is invisible in a laboratory whose servers sit at
documentation addresses that nothing routes (a leak and a drop look the same: nothing arrives). The witness stands at an address the
browser can reach directly (a documentation address held on ``lo``, see ``_turn_chain.lab_exit_address``), so a leak ARRIVES, from a
source that is not the exit of the proxy, and a test can tell a leak from a drop. It answers a STUN Binding Request with the source it
saw, so a road that works also gives the page a server-reflexive candidate.

``udp`` is every datagram as (source ip, source port, length); ``tcp`` is every connection as (source ip, source port), both of a source that
is an address of THIS host. The browser runs on this host, so a direct send of it comes from an address of this host; a source that is not
one (a runner's address is reachable from the internet, and noise arrives) cannot be the browser. Those arrivals are written down apart,
in ``foreign_udp`` and ``foreign_tcp``, and are never answered. ``heard()`` and ``heard_from_other_than()`` speak of the host's only.
"""
from __future__ import annotations

import ipaddress
import socket
import sys
import struct
import threading
from typing import Dict, Iterable, List, Set, Tuple

_MAGIC = b"\x21\x12\xa4\x42"
_IP_FAMILIES = (socket.AF_INET, socket.AF_INET6)

#: How many port numbers the witness tries before it gives up, and where it draws them from after the system's own pick: the sequence of
#: tests/_ports.py, which the HTTP/3 server and the resolver trap walk too. Below the start of Windows' dynamic range (49152), where its excluded
#: ranges are reserved in blocks of a hundred, and below Linux's ephemeral range (32768): a number drawn there is not the next of a sequence the
#: system hands out.
from _ports import DRAW_FROM, TRIES, candidates  # noqa: E402


def _normal(ip: str) -> str:
    """``ip`` as the one spelling a set compares on (an IPv4-mapped IPv6 address is its IPv4 address; a zone id is dropped)."""
    try:
        parsed = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return ip
    mapped = getattr(parsed, "ipv4_mapped", None)
    return (mapped or parsed).compressed


class HostInterfacesUnreadable(RuntimeError):
    """The interfaces of this host could not be listed. Whatever decides by them (is a source the browser's? is a destination the lab's?) must fail
    with this, never decide on an empty list in its place."""


def how_host_interfaces_are_read() -> str:
    """How :func:`read_host_interfaces` lists the interfaces, said in the record of a reader (the version of ``psutil`` included)."""
    try:
        import psutil

        version = psutil.__version__
    except Exception as exc:  # the reading itself raises it, with its cause: here it is only said
        version = "not importable: %s: %s" % (type(exc).__name__, exc)
    return "psutil.net_if_addrs() (psutil %s): the AF_INET and AF_INET6 entries of every interface, normalised (zone id dropped, " \
           "IPv4-mapped read as IPv4, lower case)" % version


def read_host_interfaces() -> Dict[str, List[str]]:
    """Every IPv4 and IPv6 address assigned to an interface of this machine (loopback and link-local included), normalised, by interface name.
    ``psutil`` is a dependency of the package and answers the same on Linux and on Windows.

    Raises :class:`HostInterfacesUnreadable` when the list cannot be read: ``psutil`` does not import, ``net_if_addrs()`` raises, an entry of an IP
    family is not an address, or no interface has any IP address at all (every host has loopback: an empty list is a failed reading)."""
    try:
        import psutil

        table = psutil.net_if_addrs()
    except Exception as exc:
        raise HostInterfacesUnreadable("the interfaces of this host could not be read (%s): %s: %s"
                                       % (how_host_interfaces_are_read(), type(exc).__name__, exc)) from exc
    interfaces: Dict[str, List[str]] = {}
    for name, entries in table.items():
        for entry in entries:
            if entry.family not in _IP_FAMILIES:
                continue
            normal = _normal(entry.address)
            try:
                ipaddress.ip_address(normal)
            except ValueError:
                raise HostInterfacesUnreadable("interface %r lists %r as an address of family %s, and it is not one"
                                               % (name, entry.address, entry.family)) from None
            listed = interfaces.setdefault(name, [])
            if normal not in listed:
                listed.append(normal)
    if not interfaces:
        raise HostInterfacesUnreadable("psutil.net_if_addrs() listed no IPv4 or IPv6 address on any interface (not even loopback): %r"
                                       % sorted(table))
    return interfaces


def host_addresses() -> Set[str]:
    """Every address of :func:`read_host_interfaces`, of every interface (raises :class:`HostInterfacesUnreadable` as it does)."""
    return {address for addresses in read_host_interfaces().values() for address in addresses}


def is_host_source(ip: str, known: Set[str]) -> bool:
    """Whether a source ip is an address of this host: any 127.0.0.0/8, ::1, or one of ``known`` (the host's addresses)."""
    ip = _normal(ip)
    try:
        if ipaddress.ip_address(ip).is_loopback:
            return True
    except ValueError:
        return False
    return ip in known


class Witness:
    def __init__(self, address: str, port: int = 0) -> None:
        self.address = address
        self.udp: List[Tuple[str, int, int]] = []
        self.tcp: List[Tuple[str, int]] = []
        self.foreign_udp: List[Tuple[str, int, int]] = []
        self.foreign_tcp: List[Tuple[str, int]] = []
        #: Every failed reading of the interfaces after the start (the first one raises here): :meth:`heard` raises while any is listed.
        self.read_errors: List[str] = []
        self._host: Set[str] = host_addresses() | {_normal(address)}
        self._lock = threading.Lock()
        self._closed = False
        # One port number for both, TCP and UDP. Each try binds TCP and then UDP on the same number; if either is refused, both
        # are released and another number is tried. The first try is the system's own pick (TCP, port 0). The others are drawn
        # at random: Windows hands out ephemeral TCP ports in sequence and keeps excluded ranges of a hundred ports for UDP
        # inside the same zone, so "the next pick" lands in the same refused range again and again (WinError 10013, twenty
        # tries in a row on a work machine). A given ``port`` is tried once and its refusal is raised as it is.
        self._udp, self._tcp, self.port = self._bind_both(address, port)
        threading.Thread(target=self._serve_udp, daemon=True).start()
        threading.Thread(target=self._serve_tcp, daemon=True).start()

    @staticmethod
    def _bind_both(address: str, port: int):
        refusals: List[str] = []
        for wanted in ([port] if port else candidates(TRIES)):
            tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            udp = None
            try:
                if sys.platform != "win32":    # on Windows SO_REUSEADDR is "share the port": it breaks bind
                    tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                tcp.bind((address, wanted))
                chosen = tcp.getsockname()[1]
                udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                udp.bind((address, chosen))
                tcp.listen(16)
                return udp, tcp, chosen
            except OSError as exc:
                if port:
                    tcp.close()
                    if udp is not None:
                        udp.close()
                    raise
                refusals.append("%s: %s" % (wanted or "system pick", exc))
                tcp.close()
                if udp is not None:
                    udp.close()
        raise OSError("the witness found no port number on %s where both TCP and UDP bind, after %d tries; the last refusals: %s"
                      % (address, TRIES, "; ".join(refusals[-3:])))

    # --- what a test reads ------------------------------------------------------------------------------------------
    def heard(self) -> Dict[str, list]:
        self._raise_if_unread()
        with self._lock:
            return {"udp": list(self.udp), "tcp": list(self.tcp)}

    def heard_foreign(self) -> Dict[str, list]:
        """What reached the witness from a source that is NOT an address of this host: not the browser, noise from the network."""
        self._raise_if_unread()
        with self._lock:
            return {"udp": list(self.foreign_udp), "tcp": list(self.foreign_tcp)}

    def _is_host(self, ip: str) -> bool:
        """Whether ``ip`` is an address of this host; a miss looks at the interfaces again (an address added since) before it is foreign. A reading
        that fails is never a silent "foreign": the arrival is kept as the host's (a direct send is not hidden) and every reader of the witness raises."""
        if is_host_source(ip, self._host):
            return True
        try:
            self._host |= host_addresses()
        except HostInterfacesUnreadable as exc:
            with self._lock:
                self.read_errors.append(str(exc))
            return True
        return is_host_source(ip, self._host)

    def _raise_if_unread(self) -> None:
        with self._lock:
            errors = list(self.read_errors)
        if errors:
            raise HostInterfacesUnreadable("the witness could not read the interfaces of this host again: %s" % "; ".join(errors))

    def heard_from_other_than(self, allowed: Iterable[str]) -> Dict[str, list]:
        """What the witness heard from a source that is not one of ``allowed`` (the exit of the proxy): a direct send."""
        ok = set(allowed)
        heard = self.heard()
        return {"udp": [h for h in heard["udp"] if h[0] not in ok], "tcp": [h for h in heard["tcp"] if h[0] not in ok]}

    @property
    def target(self) -> str:
        return "%s:%d" % (self.address, self.port)

    # --- serving ----------------------------------------------------------------------------------------------------
    def close(self) -> None:
        self._closed = True
        for s in (self._udp, self._tcp):
            try:
                s.close()
            except OSError:
                pass

    def __enter__(self) -> "Witness":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _serve_udp(self) -> None:
        while not self._closed:
            try:
                data, peer = self._udp.recvfrom(2048)
            except ConnectionResetError:
                continue
            except OSError:
                return
            if not self._is_host(peer[0]):
                with self._lock:
                    self.foreign_udp.append((peer[0], peer[1], len(data)))
                continue
            with self._lock:
                self.udp.append((peer[0], peer[1], len(data)))
            if len(data) >= 20 and data[0:2] == b"\x00\x01" and data[4:8] == _MAGIC:
                xport = struct.pack("!H", peer[1] ^ 0x2112)
                xaddr = bytes(a ^ b for a, b in zip(socket.inet_aton(peer[0]), _MAGIC))
                attr = struct.pack("!HH", 0x0020, 8) + b"\x00\x01" + xport + xaddr
                try:
                    self._udp.sendto(b"\x01\x01" + struct.pack("!H", len(attr)) + _MAGIC + data[8:20] + attr, peer)
                except OSError:
                    pass

    def _serve_tcp(self) -> None:
        while not self._closed:
            try:
                conn, peer = self._tcp.accept()
            except OSError:
                return
            with self._lock:
                (self.tcp if self._is_host(peer[0]) else self.foreign_tcp).append((peer[0], peer[1]))
            try:
                conn.close()
            except OSError:
                pass
