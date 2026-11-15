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
