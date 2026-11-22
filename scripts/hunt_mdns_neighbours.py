"""Neighbour engines for the mDNS hunt (.github/workflows/intermittent-68-mdns-hunt.yml). HUNT ONLY: never a gate, never merged to main.

The experiment: does `test_two_connections_on_one_page_connect_in_the_time_of_retail` go red only when OTHER engines are alive on the machine? Every engine
holds a responder socket on 0.0.0.0:5353 joined to 224.0.0.251 on 127.0.0.1 (dom/media/webrtc/transport/mdns_service/src/lib.rs), so the engines share one
loopback multicast group. This script keeps N such engines alive while the test runs.

    python scripts/hunt_mdns_neighbours.py --neighbours N --out DIR [--stop-file FILE]

Each neighbour is a child process (the sync Playwright API is one browser per process) that launches the engine the way the test does (the same
`_launch`, the same declared network, the same prefs and proxy, `tests/test_declared_host_network_e2e._session`), opens a page, creates an RTCPeerConnection with
a data channel, calls setLocalDescription and waits for the gathering to be complete with a host candidate whose address ends with ".local". The
connection stays open.

HOW IT PROVES A NEIGHBOUR'S NAME IS REGISTERED. Two proofs, both written to DIR/neighbours.json:
  1. The page got a candidate carrying the name. The engine registers a name with the responder BEFORE it hands the candidate to the page (one IPC channel, one
     main thread of the parent, one channel to the responder thread), so a candidate with the name is a registered name.
  2. The responder answers for it on the wire: this script sends an mDNS A question for the name to 224.0.0.251:5353 through 127.0.0.1 and waits for a
     response with that name in the answer section (heard by `tests/_mdns_listener.MdnsListener`, the test's own ear; its packets are DIR/probe-wire.jsonl).
     A name that is not answered after the attempts is written `"answered": false` and warned about on stderr; it does not stop the hunt (not answering would be a finding).
The probe runs once, before "ready N"; its listener is closed before the rounds start, so only engines hold the group during the test.

Protocol: "ready N" on stdout when all N neighbours are up and probed. Then it waits until its stdin is closed (or DIR's --stop-file appears: a fallback for a shell
that cannot close a pipe), closes every neighbour (its stdin closes, the engine is closed by its `with`), and exits 0; non-zero when a neighbour did not
come up or died before the end. A neighbour whose parent dies reads EOF on its stdin and closes itself.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
#: The seeds of the neighbours; the test's own launches use 100-104, so no two engines of the experiment share a seed.
SEED_BASE = 200
#: How long a neighbour has to be ready (launch, gather), in seconds.
READY_S = 120
#: How long a neighbour has to close after its stdin closes, in seconds.
CLOSE_S = 40
PROBE_ATTEMPTS = 6
PROBE_WAIT_S = 1.5
GROUP = ("224.0.0.251", 5353)

_NEIGHBOUR_JS = r"""async () => {
  const pc = new RTCPeerConnection();
  window.__neighbour = pc;
  pc.createDataChannel('neighbour');
  const gathered = [];
  let hostLocal = null;
  pc.addEventListener('icecandidate', e => {
    if (!e.candidate || !e.candidate.candidate) return;
    const c = e.candidate.candidate;
    gathered.push(c);
    const f = c.split(' ');
    if (!hostLocal && c.indexOf(' typ host') >= 0 && f[4] && f[4].endsWith('.local')) hostLocal = c;
  });
  await pc.setLocalDescription(await pc.createOffer());
  // Until the gathering is over and a .local host candidate was seen (a neighbour is quiet when the rounds start); the deadline is far beyond any healthy run.
  await new Promise(resolve => {
    const timer = setTimeout(resolve, 60000);
    const done = () => { if (hostLocal && pc.iceGatheringState === 'complete') { clearTimeout(timer); resolve(); } };
    pc.addEventListener('icecandidate', done);
    pc.addEventListener('icegatheringstatechange', done);
    done();
  });
  return {name: hostLocal ? hostLocal.split(' ')[4] : null, candidate: hostLocal, gathered, gathering: pc.iceGatheringState};
}"""


def _say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


# ------------------------------------------------------------------------------------------------ one neighbour (child process)

def worker(index: int) -> int:
    sys.path.insert(0, str(TESTS))
    # The test's own launch: the declared network, the proxy, the prefs and `_launch` are the ones of `_session` there (one place for the road of the experiment).
    from test_declared_host_network_e2e import NETWORK_ENV, V4_ONLY
    from test_webrtc_realness import _e2e_binary, _launch
    from _socks_servers import Socks5TcpOnly

    binary = _e2e_binary()
    if not binary:
        _say("neighbour %d: no engine binary (INVPW_BINARY_PATH)" % index)
        return 2
    os.environ[NETWORK_ENV] = V4_ONLY

    class Page(BaseHTTPRequestHandler):
        timeout = 5

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>neighbour</body></html>")

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    socks = Socks5TcpOnly()
    closed = threading.Event()

    def watch_stdin():
        try:
            sys.stdin.buffer.read()
        finally:
            closed.set()

    threading.Thread(target=watch_stdin, daemon=True).start()
    prefs = {"network.proxy.allow_hijacking_localhost": True, "network.proxy.no_proxies_on": ""}
    try:
        with _launch(binary_path=binary, proxy={"server": "socks5://127.0.0.1:%d" % socks.port}, extra_prefs=prefs, seed=SEED_BASE + index) as browser:
            page = browser.new_context().new_page()
            page.goto("http://127.0.0.1:%d/" % httpd.server_address[1], wait_until="domcontentloaded", timeout=60000)
            res = page.evaluate(_NEIGHBOUR_JS)
            if not res.get("name"):
                _say("neighbour %d: no host candidate with a .local name: %r" % (index, res))
                return 3
            print("WORKER " + json.dumps(dict(res, index=index, pid=os.getpid(), seed=SEED_BASE + index)), flush=True)
            # Alive until the parent closes our stdin; the page is serviced meanwhile so that the connection is not neglected.
            while not closed.is_set():
                page.wait_for_timeout(250)
            try:
                page.evaluate("() => window.__neighbour.close()")
            except Exception:
                pass
    finally:
        socks.close()
        httpd.shutdown()
    return 0


# ------------------------------------------------------------------------------------------------ the question on the wire

def _question(name: str, ident: int) -> bytes:
    labels = b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.rstrip(".").split("."))
    return struct.pack("!6H", ident, 0, 1, 0, 0, 0) + labels + b"\x00" + struct.pack("!HH", 1, 1)


def _probe(names: list[str], out: Path) -> dict:
    """Ask the group for each name and see whether a responder answers; the answers are heard by the test's own listener."""
    sys.path.insert(0, str(TESTS))
    from _mdns_listener import MdnsListener, JOIN_INTERFACE

    listener = MdnsListener().start()
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(JOIN_INTERFACE))
    sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    results = {}
    try:
        for number, name in enumerate(names):
            wanted = name.lower().rstrip(".")
            result = {"answered": False, "attempts": 0, "answer": None, "ms": None}
            started = time.perf_counter()
            for attempt in range(PROBE_ATTEMPTS):
                result["attempts"] = attempt + 1
                try:
                    sender.sendto(_question(name, 0x4800 + number), GROUP)
                except OSError as exc:
                    result["send_error"] = repr(exc)
                until = time.perf_counter() + PROBE_WAIT_S
                while time.perf_counter() < until and not result["answered"]:
                    for packet in list(listener.packets):
                        if packet.get("kind") != "response":
                            continue
                        for answer in packet.get("answers", []):
                            if str(answer.get("name", "")).lower().rstrip(".") == wanted:
                                result.update(answered=True, answer=answer, ms=round((time.perf_counter() - started) * 1000, 1))
                    time.sleep(0.05)
                if result["answered"]:
                    break
            results[name] = result
    finally:
        sender.close()
        listener.stop()
        listener.write(out / "probe-wire.jsonl")
    return {"listener": listener.summary(), "names": results}


# ------------------------------------------------------------------------------------------------ the parent

def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.kill()


def _pump(proc: subprocess.Popen, lines: queue.Queue) -> None:
    """The neighbour's stdout, line by line; None when it ends."""
    for line in proc.stdout:
        lines.put(line)
    lines.put(None)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--neighbours", type=int)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--worker", type=int)
    args = parser.parse_args()
    if args.worker is not None:
        return worker(args.worker)
    if args.neighbours is None or args.out is None or args.neighbours < 1:
        parser.error("--neighbours (at least 1) and --out are required")
    args.out.mkdir(parents=True, exist_ok=True)

    children, records = [], []
    failed = 0

    def close_all() -> None:
        for proc, _ in children:
            try:
                proc.stdin.close()
            except Exception:
                pass
        deadline = time.monotonic() + CLOSE_S
        for proc, _ in children:
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                _say("neighbour pid %d did not close in %d s: killed" % (proc.pid, CLOSE_S))
                _kill_tree(proc)

    try:
        for index in range(args.neighbours):
            proc = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()), "--worker", str(index)],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8")
            lines: queue.Queue = queue.Queue()
            threading.Thread(target=_pump, args=(proc, lines), daemon=True).start()
            children.append((proc, lines))
            record = None
            deadline = time.monotonic() + READY_S
            while record is None:
                try:
                    line = lines.get(timeout=max(0.1, deadline - time.monotonic()))
                except queue.Empty:
                    _say("neighbour %d: not ready in %d s" % (index, READY_S))
                    return 1
                if line is None:
                    _say("neighbour %d: exited with %r before it was ready" % (index, proc.poll()))
                    return 1
                if line.startswith("WORKER "):
                    record = json.loads(line[len("WORKER "):])
            records.append(record)
            _say("neighbour %d up: %s (pid %s)" % (index, record["name"], record["pid"]))
        probe = _probe([r["name"] for r in records], args.out)
        for record in records:
            record["probe"] = probe["names"][record["name"]]
            if not record["probe"]["answered"]:
                _say("WARNING neighbour %d: nobody answered the question for %s on the wire" % (record["index"], record["name"]))
        (args.out / "neighbours.json").write_text(json.dumps({"neighbours": records, "listener": probe["listener"]}, indent=1), encoding="utf-8")
        print("ready %d" % args.neighbours, flush=True)

        closed = threading.Event()

        def watch_stdin():
            try:
                sys.stdin.buffer.read()
            except Exception as exc:
                # A stdin that cannot be read is not a closed one: the neighbours must not close on it (the stop file still ends the run).
                _say("stdin is not readable (%r): waiting for the stop file only" % (exc,))
                return
            closed.set()

        threading.Thread(target=watch_stdin, daemon=True).start()
        while not closed.is_set() and not (args.stop_file and args.stop_file.exists()):
            for proc, _ in children:
                if proc.poll() is not None and not getattr(proc, "_reported", False):
                    proc._reported = True
                    failed += 1
                    _say("neighbour pid %d died before the end (exit %r)" % (proc.pid, proc.returncode))
            time.sleep(0.5)
    finally:
        close_all()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
