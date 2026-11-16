"""HUNT ONLY, never merged: does the shutdown-ping sender (`firefox --backgroundtask pingsender ...`) ask the MACHINE's resolver for the
telemetry host in a session that has a launch proxy?

    sudo python hunt_pingsender_dns.py trap LOG          the resolver trap (root): 127.0.0.1:53, every query logged with the pid
    python hunt_pingsender_dns.py run ARM N ROUND BINARY   N sessions (launch with a proxy, load a page, close) and what the sender did

ARM is `cand`, `pub` (a SOCKS5 launch proxy) or `cand_http`, `pub_http` (an HTTP CONNECT launch proxy). Per close it captures:
  1. the route the sender received: STEALTHFOX_PINGSENDER_ROUTE from the sender's environment (psutil environ()), without credentials;
  2. a copy of the profile's prefs.js taken the moment the sender is seen, and one taken at the close: network.trr.stealth.contexts,
     network.trr.uri, network.trr.mode, network.trr.excluded-domains, network.proxy.type, the copy's mtime against the sender's birth;
  3. MOZ_LOG=nsHostResolver:5,TRR:5 with a MOZ_LOG_FILE per pid, inherited by the sender: the lines of the telemetry host's lookup;
  4. the trap's view: every UDP query that reached 127.0.0.1:53 (resolv.conf points there), with the pid that sent it.
Lines: HUNTPING (one per close), HUNTSENDER (one per sender), HUNTPROXY (what reached the proxy for the telemetry host).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import socket
import socketserver
import struct
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import psutil

TELEMETRY = "incoming.telemetry.mozilla.org"
INTEREST = ("network.trr.stealth.contexts", "network.trr.uri", "network.trr.mode", "network.trr.excluded-domains", "network.proxy.type")
ROUTE_VAR = "STEALTHFOX_PINGSENDER_ROUTE"
SECRET = re.compile(r"user|pass|auth|cred|secret|token|key", re.I)
RESOLV = "/etc/resolv.conf"


def now_ms():
    return time.time() * 1000


# ---- the resolver trap (root) -------------------------------------------------------------------------------------------------------
def dns_question(pkt):
    """(name, qtype) of the first question of a DNS packet."""
    try:
        i, labels = 12, []
        while pkt[i]:
            n = pkt[i]
            labels.append(pkt[i + 1:i + 1 + n].decode("ascii", "replace"))
            i += n + 1
        return ".".join(labels).lower(), struct.unpack(">H", pkt[i + 1:i + 3])[0]
    except Exception:   # noqa: BLE001
        return "?", 0


def udp_owner(port):
    """The pid whose UDP socket has this local port (the query's source port), at the moment the query arrives."""
    try:
        for c in psutil.net_connections(kind="udp"):
            if c.laddr and c.laddr.port == port and c.pid:
                return c.pid
    except psutil.Error:
        pass
    return None


def trap_main(log_path):
    """Point resolv.conf at 127.0.0.1:53, answer by forwarding to the machine's own upstream, log every query; restore on exit."""
    was_link = os.path.islink(RESOLV)
    original = os.readlink(RESOLV) if was_link else open(RESOLV, encoding="utf-8").read()
    upstream = "127.0.0.53"
    if not was_link:
        for m in re.finditer(r"^nameserver\s+(\S+)", original, re.M):
            if m.group(1) != "127.0.0.1":
                upstream = m.group(1)
                break
    else:
        try:
            text = open(RESOLV, encoding="utf-8").read()
            m = re.search(r"^nameserver\s+(\S+)", text, re.M)
            upstream = m.group(1) if m else upstream
        except OSError:
            pass

    def restore(*_a):
        try:
            if os.path.lexists(RESOLV):
                os.remove(RESOLV)
            if was_link:
                os.symlink(original, RESOLV)
            else:
                with open(RESOLV, "w", encoding="utf-8") as f:
                    f.write(original)
        except OSError as exc:
            print("trap: could not restore resolv.conf: %r" % exc, flush=True)
        if _a:
            os._exit(0)

    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 53))
    signal.signal(signal.SIGTERM, restore)
    signal.signal(signal.SIGINT, restore)
    os.remove(RESOLV) if os.path.lexists(RESOLV) else None
    with open(RESOLV, "w", encoding="utf-8") as f:
        f.write("nameserver 127.0.0.1\noptions timeout:2 attempts:1\n")
    log = open(log_path, "a", encoding="utf-8", buffering=1)
    os.chmod(log_path, 0o644)
    print("trap: listening on 127.0.0.1:53, upstream %s" % upstream, flush=True)
    lock = threading.Lock()

    def forward(data, addr):
        up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        up.settimeout(3)
        try:
            up.sendto(data, (upstream, 53))
            reply, _ = up.recvfrom(4096)
            srv.sendto(reply, addr)
        except OSError:
            pass
        finally:
            up.close()
    try:
        while True:
            data, addr = srv.recvfrom(4096)
            t = now_ms()
            name, qtype = dns_question(data)
            pid = udp_owner(addr[1])         # before the reply: the asker's socket is still open, waiting for it
            who = {}
            if pid:
                try:
                    p = psutil.Process(pid)
                    cmd = p.cmdline()
                    who = {"proc": p.name(), "ppid": p.ppid(), "cmd": [a[:60] for a in cmd[:5]]}
                except psutil.Error:
                    pass
            with lock:
                log.write(json.dumps({"t": round(t, 1), "name": name, "qtype": qtype, "sport": addr[1], "pid": pid, **who}) + "\n")
            threading.Thread(target=forward, args=(data, addr), daemon=True).start()
    finally:
        restore()


# ---- the proxies of the session -----------------------------------------------------------------------------------------------------
PROXY_TARGETS = []        # {"t", "proxy", "atyp", "target", "port"}: what reached the launch proxy
PAGE_PORT = [0]


def recvn(c, n):
    out = b""
    while len(out) < n:
        d = c.recv(n - len(out))
        if not d:
            raise OSError("closed")
        out += d
    return out


def pump(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    finally:
        for x in (a, b):
            try:
                x.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def upstream_for(host, port):
    """The hunt's page host (`*.hunt`) is the local server; everything else is real (a name goes to the machine's resolver: the trap)."""
    if host.endswith(".hunt"):
        return ("127.0.0.1", PAGE_PORT[0])
    return (host.strip("[]"), port)


class Socks5Handler(socketserver.BaseRequestHandler):
    """A minimal SOCKS5 server: no authentication, CONNECT by domain name and by address, relayed for real; every target is recorded."""

    def handle(self):
        c = self.request
        try:
            ver, n = recvn(c, 2)
            recvn(c, n)
            c.sendall(b"\x05\x00")
            ver, cmd, _r, atyp = recvn(c, 4)
            if atyp == 1:
                host = socket.inet_ntoa(recvn(c, 4))
            elif atyp == 3:
                host = recvn(c, recvn(c, 1)[0]).decode("ascii", "replace")
            else:
                host = socket.inet_ntop(socket.AF_INET6, recvn(c, 16))
            port = struct.unpack(">H", recvn(c, 2))[0]
            PROXY_TARGETS.append({"t": round(now_ms(), 1), "proxy": "socks5", "cmd": cmd, "atyp": {1: "ipv4", 3: "name", 4: "ipv6"}.get(atyp), "target": host, "port": port})
            if cmd != 1:        # UDP ASSOCIATE, BIND: not served
                c.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            up = socket.create_connection(upstream_for(host, port), 15)
        except OSError:
            try:
                c.sendall(b"\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00")
            except OSError:
                pass
            return
        c.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        up.settimeout(None)
        c.settimeout(None)
        t = threading.Thread(target=pump, args=(up, c), daemon=True)
        t.start()
        pump(c, up)
        t.join(30)
        up.close()


class HttpProxyHandler(socketserver.StreamRequestHandler):
    """An HTTP proxy: CONNECT and absolute-URI GET, relayed for real; every target is recorded."""

    def handle(self):
        line = self.rfile.readline(65536)
        parts = line.split()
        if len(parts) < 2:
            return
        head = [line]
        while True:
            h = self.rfile.readline(65536)
            head.append(h)
            if h in (b"\r\n", b"\n", b""):
                break
        try:
            if parts[0] == b"CONNECT":
                host, port = parts[1].decode().rsplit(":", 1)
                PROXY_TARGETS.append({"t": round(now_ms(), 1), "proxy": "http", "cmd": "CONNECT", "atyp": "name", "target": host, "port": int(port)})
                up = socket.create_connection(upstream_for(host, int(port)), 15)
                self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                self.wfile.flush()
            else:
                u = urlsplit(parts[1].decode())
                PROXY_TARGETS.append({"t": round(now_ms(), 1), "proxy": "http", "cmd": parts[0].decode(), "atyp": "name", "target": u.hostname, "port": u.port or 80})
                up = socket.create_connection(upstream_for(u.hostname, u.port or 80), 15)
                up.sendall(b"".join(head))
        except OSError:
            return
        up.settimeout(None)
        self.connection.settimeout(None)
        t = threading.Thread(target=pump, args=(up, self.connection), daemon=True)
        t.start()
        pump(self.connection, up)
        t.join(30)
        up.close()


class Threaded(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class PageHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        body = b"<!doctype html><title>p</title><body>p</body>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


# ---- what the sender does ----------------------------------------------------------------------------------------------------------
def read_prefs(path):
    """The five prefs of interest from a prefs.js (or None when there is no file), as the JS literal text."""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    out = {}
    for name in INTEREST:
        m = re.search(r'user_pref\("%s", (.*)\);' % re.escape(name), text)
        out[name] = m.group(1)[:120] if m else None
    return out


def sanitize_route(raw):
    """The route a sender received, without secrets: whether the proxy record is there, its keys, and type/host/port style fields."""
    if raw is None:
        return {"present": False}
    try:
        route = json.loads(raw)
    except ValueError:
        return {"present": True, "json": False, "length": len(raw)}
    proxy = route.get("proxy") if isinstance(route, dict) else None
    out = {"present": True, "json": True, "keys": sorted(route) if isinstance(route, dict) else None, "proxy_present": bool(proxy)}
    if isinstance(proxy, dict):
        out["proxy_keys"] = sorted(proxy)
        safe = {}
        for k, v in proxy.items():
            if SECRET.search(k):
                continue
            if isinstance(v, str):
                v = re.sub(r"//[^/@]*@", "//", v)
            if isinstance(v, (str, int, bool)) or v is None:
                safe[k] = v
        out["proxy"] = safe
    elif proxy:
        out["proxy_type_of_value"] = type(proxy).__name__
    prefs = route.get("prefs") if isinstance(route, dict) else None
    if isinstance(prefs, dict):
        out["prefs_n"] = len(prefs)
        out["prefs_of_interest"] = {k: prefs.get(k) for k in INTEREST if k in prefs}
        out["stealth_contexts_in_route"] = prefs.get("network.trr.stealth.contexts")
    else:
        out["prefs_n"] = None
    return out


class SenderWatcher(threading.Thread):
    """From before the close: every `firefox --backgroundtask pingsender` process on the machine, the moment it is seen (every 10 ms):
    its environment's route, a copy of the profile's prefs.js, its birth; then when it ends."""

    def __init__(self, profile, workdir, since_ms):
        super().__init__(daemon=True, name="hunt-senders")
        self.profile, self.workdir, self.since = profile, workdir, since_ms
        self.senders = {}
        self.stop_ev = threading.Event()

    def run(self):
        while not self.stop_ev.is_set():
            try:
                for p in psutil.process_iter(["name"]):
                    if p.pid in self.senders or "firefox" not in (p.info["name"] or "").lower():
                        continue
                    self.capture(p)
            except psutil.Error:
                pass
            time.sleep(0.01)

    def capture(self, p):
        try:
            born = p.create_time() * 1000
            if born < self.since:
                self.senders[p.pid] = None
                return
            cmd = p.cmdline()
        except psutil.Error:
            return
        if not any(a.lstrip("-").lower() == "backgroundtask" for a in cmd[1:]) or "pingsender" not in cmd:
            self.senders[p.pid] = None
            return
        seen = now_ms()
        rec = {"pid": p.pid, "ppid": p.ppid(), "born": round(born, 1), "seen": round(seen, 1), "cmd": [a[:80] for a in cmd[:6]]}
        try:
            env = p.environ()
            rec["route"] = sanitize_route(env.get(ROUTE_VAR))
            rec["env_moz_log"] = env.get("MOZ_LOG")
            rec["env_moz_log_file"] = env.get("MOZ_LOG_FILE")
        except psutil.Error as exc:
            rec["route"] = {"error": type(exc).__name__}
        src = os.path.join(self.profile, "prefs.js") if self.profile else None
        try:
            st = os.stat(src)
            dst = os.path.join(self.workdir, "prefs-at-sender-%d.js" % p.pid)
            shutil.copy2(src, dst)
            rec["prefs_copy_mtime_vs_birth_ms"] = round(st.st_mtime * 1000 - born, 1)
            rec["prefs_copy_taken_after_birth_ms"] = round(now_ms() - born, 1)
            rec["prefs_at_sender"] = read_prefs(dst)
        except (OSError, TypeError) as exc:
            rec["prefs_at_sender"] = None
            rec["prefs_error"] = type(exc).__name__
        self.senders[p.pid] = rec
        threading.Thread(target=self.follow, args=(p, rec), daemon=True).start()

    @staticmethod
    def follow(p, rec):
        while True:
            try:
                if not p.is_running() or p.status() == psutil.STATUS_ZOMBIE:
                    break
            except psutil.Error:
                break
            time.sleep(0.01)
        rec["ended"] = round(now_ms(), 1)

    def alive(self):
        return [r for r in self.senders.values() if r and "ended" not in r]


TOKENS = re.compile(r"(TRR_ONLY|DISABLE_TRR|RESOLVE_[A-Z_]*TRR[A-Z_]*|TRR_MODE\w*|trr[_ ]?mode\W*\d|NativeLookup|native|Skipping TRR|ResolveHost|AsyncResolve|flags\W*\w+|mode\W*\d)", re.I)


def moz_log_for(logdir, pid):
    """The lines of the telemetry host in the log of one pid (MOZ_LOG_FILE has the pid in its name; a content process adds a suffix)."""
    lines = []
    try:
        names = [n for n in os.listdir(logdir) if re.match(r"hunt-log-%d(\D|$)" % pid, n)]
    except OSError:
        return {"files": 0, "lines": []}
    for n in sorted(names):
        try:
            for ln in open(os.path.join(logdir, n), encoding="utf-8", errors="replace"):
                if TELEMETRY in ln:
                    lines.append(ln.rstrip()[:240])
        except OSError:
            pass
    return {"files": len(names), "lines": lines[:12], "n": len(lines), "tokens": sorted({t.lower() for ln in lines for t in TOKENS.findall(ln)})[:20]}


def engine_of(cm_pid=None):
    for c in psutil.Process().children():
        try:
            if "firefox" in c.name().lower() and "-contentproc" not in c.cmdline():
                return c
        except psutil.Error:
            pass
    return None


def profile_of(proc):
    try:
        cmd = proc.cmdline()
        for i, a in enumerate(cmd):
            if a in ("-profile", "--profile") and i + 1 < len(cmd):
                return cmd[i + 1]
    except psutil.Error:
        pass
    return None


def classify(q, sender_pids, engine_pids):
    pid, proc = q.get("pid"), (q.get("proc") or "")
    if pid in sender_pids:
        return "sender"
    if pid in engine_pids:
        return "engine-tree"
    if pid == os.getpid():
        return "hunt-process (client or proxy)"
    if "firefox" in proc.lower():
        return "firefox-other"
    return "other:%s" % (proc or "?")


async def one_session(arm, binary, proxy_url, logdir, trap_log, i, rnd):
    from invisible_playwright.async_api import InvisiblePlaywright
    t_session = now_ms()
    work = tempfile.mkdtemp(prefix="hunt-ping-")
    row = {"arm": arm, "round": rnd, "session": i, "os": sys.platform, "proxy": proxy_url.split(":")[0]}
    del PROXY_TARGETS[:]
    cm = InvisiblePlaywright(seed=500 + i, binary_path=binary, headless=True, proxy={"server": proxy_url})
    try:
        browser = await cm.__aenter__()
        eng = engine_of()
        profile = profile_of(eng) if eng else None
        row["engine_pid"], row["profile_found"] = eng.pid if eng else None, bool(profile)
        page = await browser.new_page()
        await page.goto("http://page.hunt:%d/p" % PAGE_PORT[0], wait_until="load", timeout=45000)
        await asyncio.sleep(1.5)
        row["prefs_at_close"] = read_prefs(os.path.join(profile, "prefs.js")) if profile else None
        tree = {p.pid for p in ([eng] + eng.children(recursive=True)) if p} if eng else set()
        watcher = SenderWatcher(profile, work, now_ms())
        watcher.start()
        t_close = now_ms()
        try:
            await cm.__aexit__(None, None, None)
        except Exception as exc:   # noqa: BLE001
            row["close_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        row["close_ms"] = round(now_ms() - t_close)
        # The sender lives about a second after the browser: wait until none is alive, and for ones still to come (the reaper waits too).
        end = time.time() + 12
        quiet_since = time.time()
        while time.time() < end:
            if watcher.alive():
                quiet_since = time.time()
            elif time.time() - quiet_since > 2.5:
                break
            await asyncio.sleep(0.1)
        watcher.stop_ev.set()
    except Exception as exc:   # noqa: BLE001
        row["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:300])
        print("HUNTPING " + json.dumps(row), flush=True)
        shutil.rmtree(work, ignore_errors=True)
        try:
            await cm.__aexit__(None, None, None)
        except Exception:   # noqa: BLE001
            pass
        return row
    senders = [r for r in watcher.senders.values() if r]
    sender_pids = {r["pid"] for r in senders}
    queries = []
    try:
        for ln in open(trap_log, encoding="utf-8"):
            try:
                q = json.loads(ln)
            except ValueError:
                continue
            if t_session <= q["t"] <= now_ms() + 100:
                queries.append(q)
    except OSError:
        row["trap_error"] = "no trap log"
    for r in senders:
        r["rel_close_ms"] = round(r["born"] - t_close, 1)
        r["lifetime_ms"] = round(r["ended"] - r["born"], 1) if "ended" in r else None
        r["prefs_at_close"] = row.get("prefs_at_close")
        r["moz_log"] = moz_log_for(logdir, r["pid"])
        r["trap_queries"] = [{"name": q["name"], "t_rel_born_ms": round(q["t"] - r["born"], 1), "qtype": q["qtype"]} for q in queries if q.get("pid") == r["pid"]][:12]
        print("HUNTSENDER " + json.dumps({"arm": arm, "round": rnd, "session": i, "os": sys.platform, **r}), flush=True)
    tel = [q for q in queries if q["name"].rstrip(".") == TELEMETRY]
    row["senders"] = len(senders)
    row["trap_queries_total"] = len(queries)
    row["telemetry_queries"] = [{"who": classify(q, sender_pids, tree), "pid": q.get("pid"), "proc": q.get("proc"), "qtype": q["qtype"],
                                 "t_rel_close_ms": round(q["t"] - t_close, 1)} for q in tel][:12]
    row["native_telemetry_by_firefox"] = sum(1 for q in tel if "firefox" in (q.get("proc") or "").lower())
    row["native_telemetry_by_sender"] = sum(1 for q in tel if q.get("pid") in sender_pids)
    row["other_names_by_firefox"] = sorted({q["name"] for q in queries if "firefox" in (q.get("proc") or "").lower() and q["name"].rstrip(".") != TELEMETRY})[:8]
    row["proxy_saw_telemetry"] = [{"atyp": t["atyp"], "port": t["port"], "t_rel_close_ms": round(t["t"] - t_close, 1), "proxy": t["proxy"]}
                                  for t in PROXY_TARGETS if TELEMETRY in str(t["target"])][:6]
    row["proxy_targets_n"] = len(PROXY_TARGETS)
    row["proxy_targets_by_type"] = {k: sum(1 for t in PROXY_TARGETS if t["atyp"] == k) for k in ("name", "ipv4", "ipv6")}
    # Any firefox of the tree left behind: ended here.
    for p in psutil.Process().children(recursive=True):
        try:
            p.kill()
        except psutil.Error:
            pass
    print("HUNTPING " + json.dumps(row), flush=True)
    for fn in os.listdir(logdir):
        try:
            os.remove(os.path.join(logdir, fn))
        except OSError:
            pass
    shutil.rmtree(work, ignore_errors=True)
    return row


async def run(arm, n, rnd, binary):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), PageHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    PAGE_PORT[0] = httpd.server_address[1]
    kind = "http" if arm.endswith("_http") else "socks5"
    proxy = Threaded(("127.0.0.1", 0), Socks5Handler if kind == "socks5" else HttpProxyHandler)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    proxy_url = "%s://127.0.0.1:%d" % ("socks5" if kind == "socks5" else "http", proxy.server_address[1])
    logdir = os.environ["HUNT_LOGDIR"]
    os.makedirs(logdir, exist_ok=True)
    # The engine's environment: the core passes MOZ_LOG and MOZ_LOG_FILE through (environment.MOZ_DIAGNOSTICS), the sender inherits them.
    os.environ["MOZ_LOG"] = "sync,timestamp,nsHostResolver:5,TRR:5"
    os.environ["MOZ_LOG_FILE"] = os.path.join(logdir, "hunt-log-%PID")
    for i in range(n):
        await one_session(arm, binary, proxy_url, logdir, os.environ["HUNT_TRAP_LOG"], i, rnd)


if __name__ == "__main__":
    if os.environ.get("HUNT_PIDFILE") and sys.argv[1] != "trap":
        with open(os.environ["HUNT_PIDFILE"], "a", encoding="utf-8", newline="\n") as _pf:
            _pf.write("%d\n" % os.getpid())
    if sys.argv[1] == "trap":
        trap_main(sys.argv[2])
        sys.exit(0)
    asyncio.run(run(sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5]))
