"""Verdicts the e2e tests give from what they recorded, as pure functions of it (so a unit test can feed them the red
case and the green case without a browser).

A verdict here never reads a clock of the machine it runs on: it compares what happened to what happened, or to a
control measured in the same launch.
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence, Tuple


def cold_association_problem(associations: Sequence[Sequence[Optional[float]]],
                             via: Iterable[Tuple[str, int, int, float]], host: str, born: float) -> Optional[str]:
    """What is wrong with the way the first STUN datagram of a PeerConnection left, or None.

    ``associations`` are the UDP associations the gateway granted, ``[start, end]`` in seconds from its start (``end``
    None while unanswered; the first granted one is the core's own probe, made before the browser exists, and is no
    association of the engine); ``via`` are the datagrams the relay carried, ``(host, port, association index, time)``;
    ``born`` is the birth of the PeerConnection on the gateway's clock.

    The promise is that the first datagram leaves on an association that was ready before the PeerConnection was
    born, not that it leaves within some number of milliseconds: a cold association is made AFTER the birth, so the
    datagram that leaves on one is the defect, whatever the speed of the machine."""
    granted = [i for i, a in enumerate(associations) if a[1] is not None][1:]
    if not granted:
        return "the engine never asked the proxy for an association (only the core's probe did)"
    ready = [i for i in granted if associations[i][1] < born]
    if not ready:
        return ("no association was completed before the PeerConnection was born (granted at %s s from the gateway's "
                "start, the PeerConnection at %.3f s): the first ICE socket made its own, from scratch"
                % ([round(associations[i][1], 3) for i in granted], born))
    sent = sorted((t, index) for h, _port, index, t in via if h == host)
    if not sent:
        return "no STUN datagram of the PeerConnection reached the relay"
    at, index = sent[0]
    if index not in ready:
        return ("the first STUN datagram (at %.3f s) left on association #%d, granted at %s s, after the PeerConnection "
                "was born at %.3f s: a cold association, made from scratch (ready before the birth: %s)"
                % (at, index, associations[index][1] and round(associations[index][1], 3), born, ready))
    return None


#: The serial rounds of the gateway that the road of a TURN server written as a NAME is designed to cost, from the first CONNECT of
#: the DoH to the Allocate that is answered with credentials (a ROUND is one round trip of the gateway, ``rtt``): the reply of the
#: CONNECT (1), the TLS flight and the DoH question (2), the first Allocate answered 401 (1) and the second one reaching the TURN
#: server (1/2): four and a half, and one more for a second question asked of the DoH before the allocation. What the gateway adds
#: to every step is the same on every machine, so a count of rounds is the same on every machine: a handshake that waits for another
#: (the associations made one after the other, a DoH connection made for every question) adds rounds, a slow machine does not.
TURN_CHAIN_ROUNDS = 6


def turn_chain_problem(born: float, connect_at: Sequence[float], allocated_at: Optional[float], rtt: float,
                       associations: Sequence[Sequence[Optional[float]]],
                       via: Iterable[Tuple[str, int, int, float]], host: str,
                       rounds: int = TURN_CHAIN_ROUNDS) -> Optional[str]:
    """What is wrong with the road of a TURN server written as a name, or None. Every time is in seconds from the start of the
    gateway, read off its own stamps: the page's tick decides nothing.

    ``born`` is the birth of the PeerConnection; ``connect_at`` the arrival of each CONNECT to the DoH; ``allocated_at`` the moment
    the TURN server answered the first Allocate that carried credentials (None: it never did); ``rtt`` the round trip of the
    gateway; ``associations``, ``via`` and ``host`` as for :func:`cold_association_problem` (``host`` is the address of the TURN
    server: the datagrams of the allocation are the ones judged).

    Two promises. The allocation is answered within ``rounds`` rounds of the gateway and one round more, counted from the first
    CONNECT to the DoH after the birth (from the birth when the DoH connection of the page was kept: nothing before it is a round
    of the gateway, so a machine that is slow to start asking does not move it). And the Allocate leaves on an association that was
    ready before the birth: a handshake made after it costs two rounds more that nobody asked for, and a gateway whose handshakes
    wait for each other costs them over and over."""
    if allocated_at is None:
        return "the TURN server never answered an Allocate that carried credentials"
    began = min((t for t in connect_at if t >= born), default=born)
    spent = allocated_at - began
    budget = (rounds + 1) * rtt
    if spent > budget:
        return ("the allocation was answered %.3f s after the road began (the first CONNECT to the DoH after the birth of the "
                "PeerConnection at %.3f s, or the birth), %.1f rounds of the gateway (%.0f ms each); the design costs %d and one "
                "more is allowed: a handshake waited for another"
                % (spent, began, spent / rtt if rtt else float("inf"), rtt * 1000, rounds))
    return cold_association_problem(associations, via, host, born)
