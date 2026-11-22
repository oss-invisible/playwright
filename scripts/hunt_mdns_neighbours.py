"""Neighbour engines for the mDNS hunt (.github/workflows/intermittent-68-mdns-hunt.yml). HUNT ONLY: never a gate, never merged to main.

The experiment: does `test_two_connections_on_one_page_connect_in_the_time_of_retail` go red only when OTHER engines are alive on the machine? Every engine
holds a responder socket on 0.0.0.0:5353 joined to 224.0.0.251 on 127.0.0.1 (dom/media/webrtc/transport/mdns_service/src/lib.rs), so the engines share one
loopback multicast group. This script keeps N such engines alive while the test runs.

    python scripts/hunt_mdns_neighbours.py --neighbours N --out DIR [--stop-file FILE]

Each neighbour is a child process (the sync Playwright API is one browser per process) that launches the engine the way the test does (the same
`_launch`, the same declared network, the same prefs and proxy, `tests/test_declared_host_network_e2e._session`), opens a page, creates an RTCPeerConnection with
a data channel, calls setLocalDescription and waits for the gathering to be complete with a host candidate whose address ends with ".local". The
connection stays open.

HOW IT PROVES A NEIGHBOUR'S NAME IS REGISTERED AND ANSWERED. Two proofs, written to DIR/neighbours.json and DIR/probe-summary.md:
  1. The page got a candidate carrying the name. The engine registers a name with the responder BEFORE it hands the candidate to the page (one IPC channel, one
     main thread of the parent, one channel to the responder thread), so a candidate with the name is a registered name.
  2. The responder answers for it on the wire. A passive listener (`tests/_mdns_listener.MdnsListener`) runs from before the first neighbour until after the last
     one has closed. Once all neighbours are up this script asks for each name in three forms: (a) a socket with an ephemeral source port, the plain question (QM);
     (b) a socket bound to 0.0.0.0:5353 with SO_REUSEADDR, multicast interface 127.0.0.1, loop on, the plain question and then the same with the unicast-response
     (QU) bit set. For each form it records whether the listener heard the question itself on the group, and whether a response with the name came, from where and when.
     A name that is not answered is written `"answered": false` and reported; it does not stop the hunt (not answering would be a finding).
At the end DIR/ear-wire.jsonl is every packet the listener heard in the whole job and DIR/ear-summary.md says which came from the helper, the neighbours and the
test's engines (attributed by content: the listener cannot see a process).

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
NL = chr(10)

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

#: The three forms of the probe: (key, how the question is sent). `a` is a socket with an ephemeral source port and the plain (QM) question; `b-qm` and `b-qu` come
#: from a socket bound to 0.0.0.0:5353 with SO_REUSEADDR, as the engine's own, the second with the unicast-response (QU) bit set in the question class.
FORMS = [
    ("a", "source port ephemeral, QM", None, 0x0001),
    ("b-qm", "source 0.0.0.0:5353 (SO_REUSEADDR), QM", 5353, 0x0001),
    ("b-qu", "source 0.0.0.0:5353 (SO_REUSEADDR), QU bit (class 0x8001)", 5353, 0x8001),
]


def _question(name: str, ident: int, qclass: int = 0x0001) -> bytes:
    labels = b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.rstrip(".").split("."))
    return struct.pack("!6H", ident, 0, 1, 0, 0, 0) + labels + b"\x00" + struct.pack("!HH", 1, qclass)


def _sender(port):
    from _mdns_listener import JOIN_INTERFACE

    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if port:
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sender.bind(("0.0.0.0", port))
    sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(JOIN_INTERFACE))
    sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    return sender


def _name_of(answer) -> str:
    return str(answer.get("name", "")).lower().rstrip(".")


def _probe(listener, names: list[str]) -> dict:
    """Ask the group for each name in each form and record, for each, whether the passive listener heard the QUESTION itself and whether a response with the name came
    (from where, when). The listener runs the whole job; it is only read here."""
    sys.path.insert(0, str(TESTS))
    sent, forms = [], {}
    for form_number, (key, how, port, qclass) in enumerate(FORMS, start=1):
        per_name = {}
        try:
            sender = _sender(port)
            sender_error = None
        except OSError as exc:
            sender, sender_error = None, repr(exc)
        for number, name in enumerate(names):
            wanted = name.lower().rstrip(".")
            result = {"how": how, "attempts": [], "heard_question": 0, "answered": False, "response": None, "ms": None, "sender_error": sender_error}
            first_m = None
            for attempt in range(PROBE_ATTEMPTS):
                if sender is None or result["answered"]:
                    break
                ident = (form_number << 12) | (number << 6) | attempt
                m, t = time.perf_counter(), time.time()
                first_m = m if first_m is None else first_m
                try:
                    sender.sendto(_question(name, ident, qclass), GROUP)
                except OSError as exc:
                    result["send_error"] = repr(exc)
                note = {"id": ident, "m": round(m, 6), "t": round(t, 4), "heard_m": None}
                result["attempts"].append(note)
                sent.append({"form": key, "name": name, "id": ident})
                until = m + PROBE_WAIT_S
                while time.perf_counter() < until and not result["answered"]:
                    for packet in list(listener.packets):
                        if packet.get("kind") == "response" and packet["m"] >= first_m:
                            for answer in packet.get("answers", []):
                                if _name_of(answer) == wanted:
                                    result.update(answered=True, response={"src": packet["src"], "t": packet["t"], "answer": answer},
                                                  ms=round((packet["m"] - first_m) * 1000, 1))
                    time.sleep(0.05)
            time.sleep(0.2)   # the last datagram of an attempt is heard before the count
            for note in result["attempts"]:
                note["heard_m"] = next((p["m"] for p in list(listener.packets) if p.get("kind") == "query" and p.get("id") == note["id"]), None)
            result["heard_question"] = sum(1 for note in result["attempts"] if note["heard_m"] is not None)
            per_name[name] = result
        if sender is not None:
            sender.close()
        forms[key] = per_name
    return {"forms": forms, "sent": sent}


def _probe_lines(probe: dict) -> list[str]:
    """What the probe found, plainly, for each form: was the question heard on the group, did the neighbour answer."""
    lines = ["Probe of the neighbour names, three forms (a question is heard by the passive listener of the whole job, joined on 127.0.0.1 and bound to 5353):", ""]
    for key, how, _port, _qclass in FORMS:
        per_name = probe["forms"].get(key, {})
        sent = sum(len(r["attempts"]) for r in per_name.values())
        heard = sum(r["heard_question"] for r in per_name.values())
        answered = [r for r in per_name.values() if r["answered"]]
        lines.append("- form `%s` (%s): question heard on the group: %s (%d of %d sent); the neighbour answered: %s (%d of %d names)%s"
                     % (key, how, "YES" if heard else "NO", heard, sent, "YES" if answered else "NO", len(answered), len(per_name),
                        "".join("; %s after %s ms from %s:%s" % (_name_of(r["response"]["answer"]), r["ms"], r["response"]["src"][0], r["response"]["src"][1])
                                for r in answered)))
        for name, r in per_name.items():
            for extra in ("sender_error", "send_error"):
                if r.get(extra):
                    lines.append("  - %s: %s: %s" % (name, extra, r[extra]))
    return lines


def _stamp(t) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(t)) if t else "-"


def _ear_lines(listener, probe: dict, names: list[str]) -> list[str]:
    """Who the packets heard in the whole job came from. The listener cannot see a process, so a packet is attributed by what it carries: a question with an id the
    helper sent is the helper's; a response for a name of a neighbour is the neighbour's; the rest (questions and responses for other names) is the test's engines."""
    helper_ids = {s["id"] for s in probe["sent"]}
    neighbour_names = {n.lower().rstrip(".") for n in names}
    classes = {"helper (its probe questions)": [], "neighbour engines (responses for their names)": [], "the test's engines (everything else)": []}
    for packet in list(listener.packets):
        if packet.get("kind") == "query" and packet.get("id") in helper_ids:
            classes["helper (its probe questions)"].append(packet)
        elif packet.get("kind") == "response" and any(_name_of(a) in neighbour_names for a in packet.get("answers", [])):
            classes["neighbour engines (responses for their names)"].append(packet)
        else:
            classes["the test's engines (everything else)"].append(packet)
    lines = ["Packets the passive listener heard in the whole job (%d), by whom (attributed by content):" % len(listener.packets), "",
             "| from | questions | responses | other | source ports | first (UTC) | last (UTC) |", "|---|---|---|---|---|---|---|"]
    for label, packets in classes.items():
        ports = sorted({p["src"][1] for p in packets if p.get("src")})
        lines.append("| %s | %d | %d | %d | %s | %s | %s |" % (
            label, sum(p.get("kind") == "query" for p in packets), sum(p.get("kind") == "response" for p in packets),
            sum(p.get("kind") not in ("query", "response") for p in packets), ",".join(map(str, ports)) or "-",
            _stamp(packets[0]["t"]) if packets else "-", _stamp(packets[-1]["t"]) if packets else "-"))
    lines += ["", "The listener itself: %s" % json.dumps(listener.status)]
    return lines


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

    sys.path.insert(0, str(TESTS))
    from _mdns_listener import MdnsListener

    children, records = [], []
    failed = 0
    probe = {"forms": {}, "sent": []}
    # The passive ear of the whole job, started BEFORE the first neighbour (so every engine's socket is a later one, as in the test) and stopped after the last
    # neighbour has closed. It only listens.
    ear = MdnsListener().start()

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
        names = [r["name"] for r in records]
        probe = _probe(ear, names)
        for record in records:
            record["probe"] = {key: per_name[record["name"]] for key, per_name in probe["forms"].items()}
        lines = _probe_lines(probe)
        for line in lines:
            _say(line)
        (args.out / "neighbours.json").write_text(json.dumps({"neighbours": records, "sent": probe["sent"]}, indent=1), encoding="utf-8")
        (args.out / "probe-summary.md").write_text(NL.join(lines) + NL, encoding="utf-8")
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
        ear.stop()
        try:
            ear.write(args.out / "ear-wire.jsonl")
            names = [r["name"] for r in records]
            (args.out / "ear-summary.md").write_text(NL.join(_ear_lines(ear, probe, names)) + NL, encoding="utf-8")
        except Exception as exc:
            _say("the ear's record could not be written: %r" % (exc,))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
