"""A port of the ICE socket that another process holds on loopback must not blind the checks of two connections on one page (red on firefox-68 on Windows).

The engine draws the host port of each connection at random in 49152-65535 and binds the WILDCARD address on it (``nr_stealth_network.cpp``), and a
connection that talks to its own peer is sent to ``127.0.0.1:<that port>``. On Windows a wildcard bind and a bind of ``127.0.0.1`` on the same port
coexist, and a datagram sent to ``127.0.0.1`` goes to the specific one: another program of the machine (here the way the lab's SOCKS5 server binds its
relay sockets, ``tests/_socks_servers.py``) that binds ``127.0.0.1:<port>`` takes every check and every answer of the pair, and the connection does not
open until it lets go and the checks that follow find the road again. Linux refuses the second bind. The engine from firefox-69 binds exclusively
(``SO_EXCLUSIVEADDRUSE``): whoever holds the port first makes the other bind fail, so a program that wants the port is refused and the engine's socket
is never blind.

The reproduction does not wait for a lucky draw. The page makes the two connections and sets the offer on the first (it knows its host port then); the
test binds ``127.0.0.1:<that port>`` the way the lab's server binds its sockets; the page goes on (answer, candidates) and waits for the pair to open.

  * Windows, engine before firefox-69: the bind succeeds, the pair does not open while the socket is held, and, the socket let go 7 s after the page
    started, it opens between 9 and 10 s (the seventh check of the connection that asks last leaves at about 9.4 s: ``stun_client_ctx.cpp``);
  * Windows from firefox-69, and Linux whatever the engine: the bind is refused and the pair opens at once.

Nothing here changes the road: the held socket only reads (it never answers, never sends), the page reads ``getStats`` and never writes, and the lines
the engine logs are the ones the other launch tests keep (``tests/_evidence_with_files.py``).
"""
from __future__ import annotations

import json
import shutil
import socket
import sys
import threading
import time

import pytest

from _e2e_verdicts import MDNS_RETRY_MS, STALL_MS, opened_at_the_retry_problem
from _evidence_with_files import FailureEvidenceWithFiles, probe_machine
from _mdns_listener import MdnsListener
from test_declared_host_network_e2e import MDNS_MOZ_LOG, V4_ONLY, _session

pytestmark = [pytest.mark.e2e, pytest.mark.usefixtures("the_udp_line")]

#: The socket is let go this long after the page started (ms): the checks of a connection fail for good at 7.9 s, and the ones of the connection that asks
#: last go on to 9.4 s, so the pair that was blind opens after this, not at once.
RELEASE_AT_MS = 7000
#: Where the pair of an engine whose port is held opens (ms from the start of the page): the retry that follows the release. The window is of the
#: schedule of the checks, which the machine does not change; it is judged only on a run whose page was not stopped by the machine.
BLIND_OPENS_BETWEEN_MS = (9000, 10000)
#: A statistics snapshot of the held phase, this long before the socket is let go (ms): the checks sent with no response.
SNAPSHOT_BEFORE_RELEASE_MS = 500

# The same two connections as ``_LOOPBACK_JS`` of ``test_declared_host_network_e2e.py``, in two steps. The first makes them, sets the offer on pc1 and
# returns pc1's host UDP candidate (the address is a mDNS name, the port is real); the second goes on from there. A candidate waits until the other
# connection has the remote description (a real signaling channel delivers it then), because the second step starts a moment after the first.
# ``events`` is the page's timeline (``at`` since the start of the page), ``snapshots`` is what ``getStats`` says of both connections (every pair with the
# checks sent and answered); it only reads.
_START_JS = r"""async () => {
  const t0 = performance.now();
  const events = [];
  const now = () => Math.round((performance.now() - t0) * 10) / 10;
  const at = (what, extra) => { events.push(Object.assign({at: now(), what}, extra || {})); };
  const steps = {};
  const step = name => { steps[name] = now(); at(name); };
  const tick = () => new Promise(r => setTimeout(r, 5));
  const pc1 = new RTCPeerConnection(), pc2 = new RTCPeerConnection();
  for (const [name, pc] of [['pc1', pc1], ['pc2', pc2]]) {
    pc.addEventListener('icecandidate', e => at('icecandidate', {pc: name, candidate: e.candidate ? e.candidate.candidate : null}));
    pc.addEventListener('iceconnectionstatechange', () => at('iceconnectionstatechange', {pc: name, state: pc.iceConnectionState}));
    pc.addEventListener('connectionstatechange', () => at('connectionstatechange', {pc: name, state: pc.connectionState}));
    pc.addEventListener('icegatheringstatechange', () => at('icegatheringstatechange', {pc: name, state: pc.iceGatheringState}));
    pc.addEventListener('signalingstatechange', () => at('signalingstatechange', {pc: name, state: pc.signalingState}));
  }
  const add = (target, targetName, candidate) => {
    at('addIceCandidate called', {to: targetName, candidate: candidate.candidate});
    return target.addIceCandidate(candidate).then(
      () => at('addIceCandidate resolved', {to: targetName, candidate: candidate.candidate}),
      err => at('addIceCandidate rejected', {to: targetName, candidate: candidate.candidate, error: String(err)}));
  };
  const ready = {pc1: false, pc2: false};
  const waiting = {pc1: [], pc2: []};
  const deliver = (target, targetName, candidate) => { if (ready[targetName]) add(target, targetName, candidate); else waiting[targetName].push(candidate); };
  const open = (target, targetName) => { ready[targetName] = true; for (const c of waiting[targetName].splice(0)) add(target, targetName, c); };
  pc1.onicecandidate = e => { if (e.candidate) deliver(pc2, 'pc2', e.candidate); };
  pc2.onicecandidate = e => { if (e.candidate) deliver(pc1, 'pc1', e.candidate); };
  const gathered = {pc1: [], pc2: []};
  const keep = (name, e) => { if (e.candidate && e.candidate.candidate) gathered[name].push(e.candidate.candidate); };
  pc1.addEventListener('icecandidate', e => keep('pc1', e));
  pc2.addEventListener('icecandidate', e => keep('pc2', e));
  const dc = pc1.createDataChannel('x');
  let opened = null, echoed = false, error = null;
  dc.onopen = () => { opened = Math.round(performance.now() - t0); at('datachannel open'); dc.send('ping'); };
  pc2.ondatachannel = ev => { at('datachannel announced on pc2'); ev.channel.onmessage = m => ev.channel.send('pong:' + m.data); };
  dc.onmessage = m => { echoed = (m.data === 'pong:ping'); at('datachannel echo', {echoed}); };
  const readStats = async pc => {
    const stats = await pc.getStats();
    const byId = {};
    stats.forEach(r => { byId[r.id] = r; });
    const view = r => r ? {id: r.id, candidateType: r.candidateType, address: r.address, port: r.port, protocol: r.protocol, priority: r.priority} : null;
    const out = {local: [], remote: [], pairs: []};
    stats.forEach(r => {
      if (r.type === 'local-candidate') out.local.push(view(r));
      if (r.type === 'remote-candidate') out.remote.push(view(r));
      if (r.type === 'candidate-pair') {
        out.pairs.push({id: r.id, state: r.state, nominated: r.nominated, requestsSent: r.requestsSent, responsesReceived: r.responsesReceived,
                        requestsReceived: r.requestsReceived, responsesSent: r.responsesSent, localCandidateId: r.localCandidateId,
                        remoteCandidateId: r.remoteCandidateId, local: view(byId[r.localCandidateId]), remote: view(byId[r.remoteCandidateId])});
      }
    });
    return out;
  };
  const snapshot = async when => {
    const one = {when, at: now()};
    for (const [name, pc] of [['pc1', pc1], ['pc2', pc2]]) {
      try { one[name] = await readStats(pc); } catch (e) { one[name] = {error: String(e)}; }
    }
    return one;
  };
  // The second step: from the holder's bind to the end of the wait. ``snapshotAt`` is the page time of the snapshot of the held phase (null: none).
  const finish = async snapshotAt => {
    let stalled = 0;
    const snapshots = [];
    try {
      await pc2.setRemoteDescription(pc1.localDescription);
      step('pc2 remote description set');
      open(pc2, 'pc2');
      await pc2.setLocalDescription(await pc2.createAnswer());
      step('pc2 local description set');
      await pc1.setRemoteDescription(pc2.localDescription);
      step('pc1 remote description set');
      open(pc1, 'pc1');
      // Until the channel has opened and echoed, for 30 s (the deadline of the wait), keeping the longest time the loop did not run: a page that was
      // stopped by the machine says so, and the time the connection took is not judged then (`_e2e_verdicts.opened_at_the_retry_problem`).
      const deadline = performance.now() + 30000;
      let last = performance.now();
      let snapped = snapshotAt === null;
      while (performance.now() < deadline && !(opened !== null && echoed)) {
        await tick();
        if (!snapped && now() >= snapshotAt) {
          snapped = true;
          snapshots.push(await snapshot('held'));
          last = performance.now();
          continue;
        }
        const t = performance.now();
        stalled = Math.max(stalled, t - last);
        last = t;
      }
      at('wait over', {opened, echoed});
    } catch (e) {
      error = String(e);
      at('error', {error});
    }
    let pair = null;
    try {
      const final = await snapshot(error ? 'error' : 'wait over');
      snapshots.push(final);
      for (const p of (final.pc1 && final.pc1.pairs) || []) {
        if (p.state === 'succeeded' && (p.nominated || !pair)) {
          const l = p.local || {}, rm = p.remote || {};
          pair = {local: l.protocol, remote: rm.protocol, localType: l.candidateType, remoteType: rm.candidateType, remoteAddress: rm.address || null};
        }
      }
    } catch (e) {}
    // Closed before the browser is: the engine prints a connection's timecard when the connection is destroyed.
    try { pc1.close(); } catch (e) {}
    try { pc2.close(); } catch (e) {}
    return {opened, echoed, pair, gathered, stalled: Math.round(stalled), error, events, steps, snapshots, origin: performance.timeOrigin, started_at: Math.round(t0 * 10) / 10};
  };
  let first = null;
  try {
    await pc1.setLocalDescription(await pc1.createOffer());
    step('pc1 local description set');
    // The host UDP candidate of pc1: its port is the one the engine drew and bound.
    const deadline = performance.now() + 30000;
    while (performance.now() < deadline && !first) {
      first = gathered.pc1.find(c => c.split(' ')[2].toLowerCase() === 'udp' && c.indexOf(' typ host') >= 0) || null;
      if (!first) await tick();
    }
    at('first host udp candidate of pc1', {candidate: first});
  } catch (e) {
    error = String(e);
    at('error', {error});
  }
  window.__heldPort = {finish};
  return {candidate: first, port: first ? Number(first.split(' ')[5]) : null, error, started_wall_ms: performance.timeOrigin + t0};
}"""

_FINISH_JS = "async (snapshotAt) => window.__heldPort.finish(snapshotAt)"


class _Holder:
    """A UDP socket on ``127.0.0.1:<port>`` bound the way the lab's SOCKS5 server binds its relay sockets (``tests/_socks_servers.py``: a plain datagram
    socket, no option, and on Windows the reset of an unreachable port switched off). It only reads: it never sends a datagram, and what it received is
    its record. ``bind_error`` is the refusal, None when the bind succeeded."""

    def __init__(self) -> None:
        self.sock = None
        self.bind_error = None
        self.received = []
        self.released_wall = None
        self._lock = threading.Lock()

    def take(self, port: int) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            sock.close()
            self.bind_error = {"errno": exc.errno, "winerror": getattr(exc, "winerror", None), "message": str(exc)}
            return False
        if hasattr(socket, "SIO_UDP_CONNRESET"):
            sock.ioctl(socket.SIO_UDP_CONNRESET, False)
        self.sock = sock
        threading.Thread(target=self._read, args=(sock,), daemon=True).start()
        return True

    def _read(self, sock: socket.socket) -> None:
        # Until the socket is closed by ``release`` (the read then fails). An error of the body ends the reading, never the test.
        while True:
            try:
                data, peer = sock.recvfrom(65536)
            except ConnectionResetError:
                continue
            except OSError:
                return
            self.received.append({"t": time.time(), "from": list(peer), "bytes": len(data), "stun": data[4:8] == b"\x21\x12\xa4\x42"})

    def release(self) -> None:
        with self._lock:
            if self.sock is None:
                return
            sock, self.sock = self.sock, None
            self.released_wall = time.time()
            sock.close()


@pytest.mark.wall_clock
def test_a_held_ice_port_blinds_the_loopback_checks(local_https_page, socks5_tcp_only, monkeypatch, engine_reaches, capsys):
    """See the module. One cold launch; the verdict by system and by engine, none on a clock of the machine but the schedule of the checks.
    It carries ``wall_clock``: the e2e run gives it the machine alone, so that no other worker's socket holds or takes the port it is about."""
    windows = sys.platform == "win32"
    engine_binds_exclusively = engine_reaches("firefox-69")
    blind = windows and not engine_binds_exclusively
    holder = _Holder()
    launch = {"seed": 100, "windows": windows, "engine_binds_exclusively": engine_binds_exclusively, "moz_log": MDNS_MOZ_LOG}
    with FailureEvidenceWithFiles("a-held-ice-port-blinds-the-loopback-checks") as evidence:
        log_dir = evidence.files / "engine-log"
        timer = None

        def script(page):
            nonlocal timer
            started = page.evaluate(_START_JS)
            launch["started"] = started
            assert started["port"], "pc1 gathered no host UDP candidate: %r" % (started,)
            launch["bound"] = holder.take(started["port"])
            launch["bind_error"] = holder.bind_error
            start_wall = started["started_wall_ms"]
            # The release is `RELEASE_AT_MS` after the page started, on the wall clock both sides share.
            delay = max(0.0, (start_wall + RELEASE_AT_MS) / 1000.0 - time.time())
            timer = threading.Timer(delay, holder.release)
            timer.daemon = True
            if launch["bound"]:
                timer.start()
            snapshot_at = RELEASE_AT_MS - SNAPSHOT_BEFORE_RELEASE_MS if launch["bound"] else None
            res = page.evaluate(_FINISH_JS, snapshot_at)
            if (not res["echoed"] or res["opened"] is None or not res["pair"]
                    or opened_at_the_retry_problem(res["opened"], res["stalled"], MDNS_RETRY_MS) is not None):
                probe_machine(evidence.files / "machine-run-0.txt")
            return res

        listener = MdnsListener().start()
        launch.update(started_m=round(time.perf_counter(), 6), started_t=round(time.time(), 4))
        res = None
        try:
            res = _session(V4_ONLY, socks5_tcp_only, local_https_page, monkeypatch, 100, script, engine_log=str(log_dir))
        finally:
            if timer is not None:
                timer.cancel()
            holder.release()
            launch.update(returned_m=round(time.perf_counter(), 6), returned_t=round(time.time(), 4))
            listener.stop()
            listener.write(evidence.files / "mdns-wire-run-0.jsonl")
            launch["mdns_wire"] = listener.summary()
            start_wall = (launch.get("started") or {}).get("started_wall_ms")
            launch["held"] = {
                "released_at_page_ms": None if holder.released_wall is None or start_wall is None else round(holder.released_wall * 1000 - start_wall, 1),
                "received": len(holder.received),
                "received_stun": sum(1 for r in holder.received if r["stun"]),
                "first_at_page_ms": None if not holder.received or start_wall is None else round(holder.received[0]["t"] * 1000 - start_wall, 1),
                "last_at_page_ms": None if not holder.received or start_wall is None else round(holder.received[-1]["t"] * 1000 - start_wall, 1),
                "peers": sorted({tuple(r["from"]) for r in holder.received}),
            }
            if res is not None:
                launch["page"] = {key: res.pop(key, None) for key in ("events", "steps", "snapshots", "error", "origin", "started_at")}
                launch.update(opened=res["opened"], echoed=res["echoed"], stalled=res["stalled"], pair=res["pair"], gathered=res["gathered"])
            (evidence.files / "launches.json").write_text(json.dumps([launch], indent=1, default=str), encoding="utf-8")

        # The bind of the other process: the whole difference between the engines and the systems.
        if blind:
            assert launch["bound"], ("firefox-68 on Windows must let another process bind 127.0.0.1 on the port its wildcard socket holds, "
                                     "this one was refused: %r" % (launch["bind_error"],))
        else:
            assert not launch["bound"], ("the port of the ICE socket was bound a second time on 127.0.0.1 (%s): another process reads the checks "
                                         "of the pair" % ("an engine that binds exclusively" if windows else "Linux"))
        assert res["echoed"] and res["opened"] is not None, "the data channel did not open: %r" % (res,)
        for side in ("pc1", "pc2"):
            udp = [c for c in res["gathered"][side] if " typ host" in c and c.split(" ")[2].lower() == "udp"]
            assert udp, "%s gathered no UDP host candidate: %r" % (side, res["gathered"][side])
        pair = res["pair"]
        assert pair, "the browser reports no nominated pair: %r" % (res,)
        assert pair["local"] == "udp" and pair["remote"] == "udp", "the connection did not take UDP at both ends: %r" % (pair,)
        assert pair["localType"] in ("host", "prflx") and pair["remoteType"] in ("host", "prflx"), "the pair is not direct and local: %r" % (pair,)
        stopped = res["stalled"] >= STALL_MS
        if blind:
            released = launch["held"]["released_at_page_ms"]
            assert released is not None, "the held socket was never let go: %r" % (launch["held"],)
            assert res["opened"] >= released, ("the pair opened at %d ms, while the socket was still held (let go at %.0f ms): the held port does not "
                                               "blind the checks" % (res["opened"], released))
            if not stopped:
                low, high = BLIND_OPENS_BETWEEN_MS
                assert low <= res["opened"] <= high, ("the pair opened at %d ms, not between %d and %d ms after a release at %.0f ms"
                                                      % (res["opened"], low, high, released))
        else:
            problem = opened_at_the_retry_problem(res["opened"], res["stalled"], MDNS_RETRY_MS)
            assert problem is None, problem
        shutil.rmtree(log_dir, ignore_errors=True)
    with capsys.disabled():
        print("\n[measure] a held ICE port (%s, %s): bound=%s %s; the pair opened at %s ms (page stopped at most %s ms); the held socket was let go at %s ms and "
              "received %s datagrams (%s STUN)" % (
                  "Windows" if windows else "Linux", "firefox-69 or later" if engine_binds_exclusively else "before firefox-69", launch["bound"],
                  launch["bind_error"] or "", res["opened"], res["stalled"], launch["held"]["released_at_page_ms"], launch["held"]["received"],
                  launch["held"]["received_stun"]))
