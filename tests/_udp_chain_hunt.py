"""HUNT ONLY (branch hunt/udp-chain-post, never merged): what the whole-UDP-chain e2e measures about itself, kept in memory and printed
at the end of each run as `HUNT` lines (to stdout and, appended, to the file named by INVPW_HUNT_FILE, which the workflow prints).

The test fails on Windows CI with page status TIMEOUT: the srflx arrives at ~1031 ms, but the tick of 2000 ms fires before the reply to
the page's POST clears it. Three questions, none of which changes the tick or anything the test asserts:

1. WHEN, on both sides: the page stamps performance.now() and Date.now() when it sends the POST and when the response arrives
   (`window.__result.timeline`, tests/_turn_chain.py PAGE_HTML); the page server stamps time.time() when the POST request line arrives,
   when its body is read and when its response is written (`Page.requests`).
2. THE GATEWAY'S VIEW of the connection that carried the POST: new CONNECT or an older connection, and the time.time() of every step
   (`Chain.trace_events`: accepted, greeted, connect-request, replies sent, upstream open, every chunk each way, end).
3. WHO opens the TLS handshakes the FakeDoh rejects: the time, the SNI, the client's port (`FakeDoh.handshakes`), mapped to a process
   with psutil.net_connections (`ProcessMap`: it resolves the port of every connection the gateway accepts, at the moment it is
   accepted, from a thread of its own, because the connection of a rejected handshake is closed a moment later).
"""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def _upstream_ports(segment: List[tuple]) -> List[int]:
    """The local ports of the gateway's upstream sockets of one connection (what the page server sees as the client port)."""
    found = (re.search(r"from port (\d+)", str(e[3])) for e in segment if e[2] == "upstream-open")
    return [int(m.group(1)) for m in found if m]


def hunt_file() -> Path:
    return Path(os.environ.get("INVPW_HUNT_FILE") or Path(tempfile.gettempdir()) / "udp-chain-hunt.log")


def emit(lines: List[str]) -> None:
    """To stdout, and appended to the hunt file in ONE write (the workers of an xdist run share it)."""
    text = "".join(line + "\n" for line in lines)
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:   # noqa: BLE001
        pass
    try:
        fd = os.open(str(hunt_file()), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        try:
            os.write(fd, text.encode("utf-8", "replace"))
        finally:
            os.close(fd)
    except OSError:
        pass


class ProcessMap:
    """port of a client socket of the gateway -> the process that owns it, resolved when the gateway accepts the connection."""

    def __init__(self, gateway_port: int, binary: Optional[str]) -> None:
        self.gateway_port = gateway_port
        self.engine_dir = os.path.normcase(os.path.dirname(binary)) if binary else None
        self._pending: "queue.Queue[Optional[Tuple[float, int]]]" = queue.Queue()
        #: client port -> [(time.time() of the accept, pid or None)]
        self.by_port: Dict[int, List[Tuple[float, Optional[int]]]] = {}
        #: pid -> (kind, cmdline)
        self.info: Dict[int, Tuple[str, str]] = {}
        self.errors: List[str] = []
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._pending.put(None)
        self._thread.join(timeout=10)

    def listen(self, record) -> None:
        """The `trace_listener` of the gateway: only an accept matters."""
        t, port, event, _detail = record
        if event == "accepted" and port > 0:
            self._pending.put((t, port))

    def _run(self) -> None:
        import psutil
        while True:
            item = self._pending.get()
            if item is None:
                return
            batch = [item]
            while True:
                try:
                    more = self._pending.get_nowait()
                except queue.Empty:
                    break
                if more is None:
                    self._pending.put(None)
                    break
                batch.append(more)
            wanted = {port: t for t, port in batch}
            found: Dict[int, int] = {}
            for attempt in range(4):
                try:
                    for c in psutil.net_connections(kind="tcp"):
                        if c.laddr and c.raddr and c.raddr.port == self.gateway_port and c.laddr.port in wanted and c.pid:
                            found[c.laddr.port] = c.pid
                except Exception as exc:   # noqa: BLE001
                    self.errors.append("%s: %s" % (type(exc).__name__, exc))
                    break
                if all(p in found for p in wanted):
                    break
                time.sleep(0.02)
            for port, t in wanted.items():
                pid = found.get(port)
                self.by_port.setdefault(port, []).append((t, pid))
                if pid is not None and pid not in self.info:
                    self.info[pid] = self._describe(pid)

    def _describe(self, pid: int) -> Tuple[str, str]:
        import psutil
        if pid == os.getpid():
            return "python: this test process", "pid %d" % pid
        try:
            proc = psutil.Process(pid)
            name, cmd = proc.name(), proc.cmdline()
            exe = os.path.normcase(proc.exe() or "")
        except Exception as exc:   # noqa: BLE001
            return "gone (%s)" % type(exc).__name__, "pid %d" % pid
        text = " ".join(cmd)
        in_engine = bool(self.engine_dir) and exe.startswith(self.engine_dir)
        if in_engine or "firefox" in name.lower():
            if "-contentproc" in cmd:
                return "firefox -contentproc %s" % (cmd[-1] if cmd else "?"), text[:200]
            return "firefox parent", text[:200]
        if "python" in name.lower():
            return "python: another process", text[:200]
        return "other: %s" % name, text[:200]

    def who(self, port: Optional[int], t: float) -> Tuple[Optional[int], str]:
        """The process of the client socket `port` that was accepted last before `t`."""
        rows = [r for r in self.by_port.get(port or -1, []) if r[0] <= t + 0.05]
        if not rows:
            return None, "unmapped (no accept recorded)"
        pid = rows[-1][1]
        if pid is None:
            return None, "unmapped (not in the TCP table when accepted)"
        return pid, self.info.get(pid, ("?", ""))[0]


class Hunt:
    """The three measurements of one run of the walk."""

    def __init__(self, gateway, rep: int = 0, binary: Optional[str] = None) -> None:
        self.gateway = gateway
        self.rep = rep
        self.tag = "HUNT[%d.%d.rep%s]" % (os.getpid(), int(time.time()), rep)
        self.procs = ProcessMap(gateway.port, binary)

    def start(self) -> None:
        try:
            self.gateway.trace_listener = self.procs.listen
            self.procs.start()
        except Exception:   # noqa: BLE001
            pass

    def stop(self) -> None:
        try:
            self.gateway.trace_listener = None
            self.procs.stop()
        except Exception:   # noqa: BLE001
            pass

    # ------------------------------------------------------------------------------------------------------------
    def report(self, result: Optional[dict], explain: bool) -> None:
        """Print everything. Never raises: a hunt must not change the verdict of the test."""
        lines: List[str] = []
        for part in (self._page_and_server, self._gateway_view, self._tls):
            try:
                lines += part(result or {})
            except Exception as exc:   # noqa: BLE001
                lines.append("%s %s failed: %s: %s" % (self.tag, part.__name__, type(exc).__name__, exc))
        if explain:
            try:
                lines.append("%s the explanation of this failure (gateway.explain):" % self.tag)
                lines += ["%s | %s" % (self.tag, l) for l in self.gateway.explain(result or {}).splitlines()]
            except Exception as exc:   # noqa: BLE001
                lines.append("%s explain failed: %s: %s" % (self.tag, type(exc).__name__, exc))
        emit(lines)

    # --- 1: the POST, on both sides -------------------------------------------------------------------------------
    def _page_and_server(self, result: dict) -> List[str]:
        tl = result.get("timeline") or {}
        out = ["%s 1. status=%s; page timeline (raw, perf in ms since the page's time origin, date in ms since the epoch): %s"
               % (self.tag, result.get("status"), json.dumps(tl, sort_keys=True))]
        send, resp = tl.get("post_send_date"), tl.get("post_response_date")
        if send is None:
            out.append("%s 1. the page never sent the POST (no srflx before the tick, or the page did not run)" % self.tag)
        posts = [r for r in self.gateway.page.requests if r["method"] == "POST"]
        if not posts:
            out.append("%s 1. the page server never saw a POST request line" % self.tag)
        base = (send / 1000.0) if send is not None else None

        def rel(t: Optional[float]) -> str:
            if t is None:
                return "n/a"
            return "%+.1f ms" % ((t - base) * 1000.0) if base is not None else "%.3f s" % t

        def rel_ms(date_ms: Optional[float]) -> str:
            return "n/a" if date_ms is None else rel(date_ms / 1000.0)

        out.append("%s 1. page, relative to its own post_send (Date.now): srflx %s, post_response %s, tick_fired %s; "
                   "performance.now: send %.1f, response %.1f, tick_fired %.1f (response - send = %.1f ms; send - srflx = %.1f ms)" % (
                       self.tag, rel_ms(tl.get("srflx_date")), rel_ms(resp), rel_ms(tl.get("tick_fired_date")),
                       tl.get("post_send_perf", float("nan")), tl.get("post_response_perf", float("nan")),
                       tl.get("tick_fired_perf", float("nan")),
                       tl.get("post_response_perf", float("nan")) - tl.get("post_send_perf", float("nan")),
                       tl.get("post_send_perf", float("nan")) - tl.get("srflx_perf", float("nan"))))
        for r in posts:
            out.append("%s 1. page server, POST on connection #%d (upstream port %d, request %d on it), time.time() as seconds, then "
                       "relative to the page's post_send: accept %.3f (%s), request line %.3f (%s), body %.3f (%s), response written %.3f (%s); "
                       "the page's response arrived %s" % (
                           self.tag, r["conn"], r["client_port"], r["nth_on_conn"], r["t_accept"], rel(r["t_accept"]), r["t_line"],
                           rel(r["t_line"]), r["t_body"] or 0.0, rel(r["t_body"]), r["t_written"] or 0.0, rel(r["t_written"]), rel_ms(resp)))
        for r in self.gateway.page.requests:
            if r["method"] != "POST":
                out.append("%s 1. page server, %s %s on connection #%d (upstream port %d): accept %s, request line %s, written %s" % (
                    self.tag, r["method"], r["path"], r["conn"], r["client_port"], rel(r["t_accept"]), rel(r["t_line"]), rel(r["t_written"])))
        out.append("%s 1. clocks: the page reads Date.now() (whole ms, possibly clamped by the browser), the server time.time(); one host, one wall clock" % self.tag)
        return out

    # --- 2: the gateway's view of the connection of the POST --------------------------------------------------------
    def _segments(self) -> List[List[tuple]]:
        """The trace events split into connections: a new one starts at each `accepted` of a client port."""
        segs: List[List[tuple]] = []
        open_by_port: Dict[int, List[tuple]] = {}
        for rec in list(self.gateway.trace_events):
            _t, port, event, _d = rec
            if event == "accepted" or port not in open_by_port:
                open_by_port[port] = []
                segs.append(open_by_port[port])
            open_by_port[port].append(rec)
        return segs

    def _gateway_view(self, result: dict) -> List[str]:
        tl = result.get("timeline") or {}
        send = tl.get("post_send_date")
        base = (send / 1000.0) if send is not None else None
        page_target = "127.0.0.1:%d" % self.gateway.page.port

        def rel(t: float) -> str:
            return "%+9.1f ms" % ((t - base) * 1000.0) if base is not None else "%.3f" % t

        out: List[str] = []
        segs = self._segments()
        page_segs = [s for s in segs if any(e[2] == "connect-request" and e[3] == page_target for e in s)]
        out.append("%s 2. gateway: %d connection(s) accepted in all, %d of them CONNECT to the page (%s); all wall clock time.time(), "
                   "relative to the page's post_send" % (self.tag, len(segs), len(page_segs), page_target))
        posts = [r for r in self.gateway.page.requests if r["method"] == "POST"]
        upstream_of_post = {r["client_port"] for r in posts}
        for s in page_segs:
            ups = _upstream_ports(s)
            served = [r for r in self.gateway.page.requests if r["client_port"] in ups]
            accepted = s[0][0]
            is_post = bool(set(ups) & upstream_of_post)
            first = {}
            for t, _p, event, detail in s:
                first.setdefault(event, t)
            reply = next((t for t, _p, event, detail in s if event == "sent" and t > first.get("connect-request", 1e18)), None)
            out.append("%s 2. gateway connection of client port %d%s: accepted %s, greeted %s, connect-request %s, connect reply sent %s, "
                       "first request byte in %s / out %s, first response byte in %s / out %s, ended %s; the page server saw: %s" % (
                           self.tag, s[0][1], "  <== THE POST" if is_post else "", rel(accepted),
                           rel(first["greeted"]) if "greeted" in first else "n/a",
                           rel(first["connect-request"]) if "connect-request" in first else "n/a",
                           rel(reply) if reply else "n/a",
                           rel(first["c2u-in"]) if "c2u-in" in first else "n/a", rel(first["c2u-out"]) if "c2u-out" in first else "n/a",
                           rel(first["u2c-in"]) if "u2c-in" in first else "n/a", rel(first["u2c-out"]) if "u2c-out" in first else "n/a",
                           rel(first["tunnel-ended"]) if "tunnel-ended" in first else "n/a",
                           ", ".join("%s #%d" % (r["method"], r["nth_on_conn"]) for r in served) or "no request"))
            if is_post:
                post_req = next(r for r in served if r["method"] == "POST")
                if base is None:
                    verdict = "page send time unknown"
                elif accepted < base:
                    verdict = "an OLDER connection: accepted %.1f ms BEFORE the page sent the POST" % ((base - accepted) * 1000.0)
                else:
                    verdict = "a NEW CONNECT: accepted %.1f ms AFTER the page sent the POST" % ((accepted - base) * 1000.0)
                out.append("%s 2. the connection of the POST is %s; request %d on that connection at the page server%s" % (
                    self.tag, verdict, post_req["nth_on_conn"], " (HTTP/1.0 server: one request per connection)" if post_req["nth_on_conn"] == 1 else ""))
                for t, _p, event, detail in s:
                    out.append("%s 2.   %s  t=%.3f  %-16s %s" % (self.tag, rel(t), t, event, detail))
        matched = {u for s in segs for u in _upstream_ports(s)} & upstream_of_post
        if posts and not matched:
            out.append("%s 2. no gateway connection has an upstream socket on the port the page server saw for the POST (%s)" % (
                self.tag, sorted(upstream_of_post)))
        return out

    # --- 3: who opens the handshakes the FakeDoh rejects --------------------------------------------------------------
    def _tls(self, result: dict) -> List[str]:
        doh = self.gateway.doh
        shakes = list(doh.handshakes)
        bad = [h for h in shakes if not h["ok"]]
        good = [h for h in shakes if h["ok"]]
        out = ["%s 3. FakeDoh: %d TLS handshake(s) served, %d rejected (tls_failures %d); process map errors: %s" % (
            self.tag, len(shakes), len(bad), len(doh.tls_failures), self.procs.errors[:2] or "none")]
        kinds: Counter = Counter()
        times: Dict[str, List[float]] = {}
        snis: Counter = Counter()
        for h in bad:
            port = h["client"][1] if h["client"] else None
            pid, kind = self.procs.who(port, h["t_start"]) if port else (None, "direct to the FakeDoh (no gateway)")
            kinds[kind] += 1
            times.setdefault(kind, []).append(h["t_end"])
            snis[h["sni"]] += 1
            out.append("%s 3.   rejected t=%.3f (handshake started %.3f) sni=%s client_port=%s via=%s pid=%s process=%s error=%s" % (
                self.tag, h["t_end"], h["t_start"], h["sni"], port, h["via"], pid, kind, (h["error"] or "")[:90]))
        for kind, n in kinds.most_common():
            ts = times[kind]
            out.append("%s 3. SUMMARY rejected by process kind: %-34s %3d   first t=%.3f  last t=%.3f  (span %.0f ms)" % (
                self.tag, kind, n, min(ts), max(ts), (max(ts) - min(ts)) * 1000.0))
        if bad:
            out.append("%s 3. SUMMARY rejected by SNI: %s" % (self.tag, dict(snis)))
            out.append("%s 3. SUMMARY rejected, all: first t=%.3f last t=%.3f" % (self.tag, min(h["t_end"] for h in bad), max(h["t_end"] for h in bad)))
        ok_kinds: Counter = Counter()
        for h in good:
            port = h["client"][1] if h["client"] else None
            ok_kinds[self.procs.who(port, h["t_start"])[1] if port else "direct"] += 1
        out.append("%s 3. for comparison, handshakes that SUCCEEDED by process kind: %s" % (self.tag, dict(ok_kinds)))
        for pid, (kind, cmd) in sorted(self.procs.info.items()):
            out.append("%s 3.   pid %d is %s: %s" % (self.tag, pid, kind, cmd))
        return out
