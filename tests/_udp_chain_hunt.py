"""HUNT ONLY (branch hunt/udp-chain-post, never merged): what the whole-UDP-chain e2e measures about itself, kept in memory and printed
at the end of each run as `HUNT` lines (to stdout and, appended, to the file named by INVPW_HUNT_FILE, which the workflow prints).

The test fails on Windows CI with page status TIMEOUT: the srflx arrives at ~1031 ms, but the tick of 2000 ms fires before the reply to
the page's POST clears it. Three questions, none of which changes the tick or anything the test asserts:

1. WHEN, on both sides: the page stamps performance.now() and Date.now() when it sends the POST and when the response arrives
   (`window.__result.timeline`, tests/_turn_chain.py PAGE_HTML); the page server stamps time.time() when the POST request line arrives,
   when its body is read and when its response is written (`Page.requests`).
2. THE GATEWAY'S VIEW of the connection that carried the POST: new CONNECT or an older connection, and the time.time() of every step
   (`Chain.trace_events`: accepted, greeted, connect-request, replies sent, upstream open, every chunk each way, end).
3. WHO opens the TLS handshakes the FakeDoh rejects: the time, the SNI, the client's port (`FakeDoh.handshakes`). No process lookup:
   psutil.net_connections is costly on Windows and holds the GIL, so it could stall the process it is meant to observe.
4. STALLS of the pytest process itself: a sentinel thread sleeps 10 ms in a loop and records every gap over 50 ms (`Sentinel`).
5. EVERY gateway connection: accepted, first bytes read in the greeting, greeted (`Chain.trace_events`), as a histogram of
   accepted -> first bytes and the list of those over 500 ms.
"""
from __future__ import annotations

import json
import os
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


class Sentinel:
    """A thread that sleeps 10 ms in a loop and records every gap over 50 ms between two of its wake-ups: (start time.time(), length s).
    A gap is the process not running Python code (the GIL held elsewhere, a descheduled process, a long call that does not release it)."""

    STEP = 0.010
    OVER = 0.050

    def __init__(self) -> None:
        self.gaps: List[Tuple[float, float]] = []
        self._halt = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._halt.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        last_wall, last = time.time(), time.perf_counter()
        while not self._halt.is_set():
            time.sleep(self.STEP)
            now_wall, now = time.time(), time.perf_counter()
            if now - last > self.OVER:
                self.gaps.append((last_wall, now - last))
            last_wall, last = now_wall, now


class Hunt:
    """The three measurements of one run of the walk."""

    def __init__(self, gateway, rep: int = 0, binary: Optional[str] = None) -> None:
        self.gateway = gateway
        self.rep = rep
        self.tag = "HUNT[%d.%d.rep%s]" % (os.getpid(), int(time.time()), rep)
        self.sentinel = Sentinel()

    def start(self) -> None:
        self.sentinel.start()

    def stop(self) -> None:
        try:
            self.sentinel.stop()
        except Exception:   # noqa: BLE001
            pass

    # ------------------------------------------------------------------------------------------------------------
    def report(self, result: Optional[dict], explain: bool) -> None:
        """Print everything. Never raises: a hunt must not change the verdict of the test."""
        lines: List[str] = []
        for part in (self._page_and_server, self._gateway_view, self._connections, self._stalls, self._tls):
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

    # --- 3: who opens the handshakes the FakeDoh rejects ---------------------------------------------------------------
    def _tls(self, result: dict) -> List[str]:
        doh = self.gateway.doh
        shakes = list(doh.handshakes)
        bad = [h for h in shakes if not h["ok"]]
        out = ["%s 3. FakeDoh: %d TLS handshake(s) served, %d rejected (tls_failures %d)" % (self.tag, len(shakes), len(bad), len(doh.tls_failures))]
        snis: Counter = Counter()
        ports = set()
        for h in bad:
            port = h["client"][1] if h["client"] else None
            snis[h["sni"]] += 1
            ports.add(port)
            out.append("%s 3.   rejected t=%.3f (handshake started %.3f, %.0f ms) sni=%s client_port=%s via=%s error=%s" % (
                self.tag, h["t_end"], h["t_start"], (h["t_end"] - h["t_start"]) * 1000.0, h["sni"], port, h["via"], (h["error"] or "")[:90]))
        if bad:
            out.append("%s 3. SUMMARY rejected: %d on %d client port(s); by SNI %s; first t=%.3f last t=%.3f (span %.0f ms)" % (
                self.tag, len(bad), len(ports), dict(snis), min(h["t_end"] for h in bad), max(h["t_end"] for h in bad),
                (max(h["t_end"] for h in bad) - min(h["t_end"] for h in bad)) * 1000.0))
        return out

    # --- 4: stalls of this process --------------------------------------------------------------------------------
    def _stalls(self, result: dict) -> List[str]:
        tl = result.get("timeline") or {}
        send, resp, tick = tl.get("post_send_date"), tl.get("post_response_date"), tl.get("tick_fired_date")
        base = (send / 1000.0) if send is not None else None
        gaps = list(self.sentinel.gaps)

        def rel(t: float) -> str:
            return "%+.1f ms" % ((t - base) * 1000.0) if base is not None else "%.3f" % t

        out = ["%s 4. sentinel (sleep %d ms, gaps over %d ms): %d gap(s), total %.0f ms" % (
            self.tag, self.sentinel.STEP * 1000, self.sentinel.OVER * 1000, len(gaps), sum(g for _t, g in gaps) * 1000.0)]
        windows = []
        if send is not None and resp is not None:
            windows.append(("post_send -> post_response", send / 1000.0, resp / 1000.0))
        if send is not None and tick is not None:
            windows.append(("post_send -> tick_fired", send / 1000.0, tick / 1000.0))
        for name, a, b in windows:
            hit = [(t, g) for t, g in gaps if t < b and t + g > a]
            out.append("%s 4. gaps overlapping %s (%.0f ms): %s" % (
                self.tag, name, (b - a) * 1000.0,
                "; ".join("start %s length %.0f ms" % (rel(t), g * 1000.0) for t, g in hit) or "none"))
        for t, g in sorted(gaps, key=lambda x: -x[1])[:10]:
            out.append("%s 4.   largest: start t=%.3f (%s) length %.0f ms" % (self.tag, t, rel(t), g * 1000.0))
        return out

    # --- 5: every gateway connection: accepted -> first bytes -> greeted --------------------------------------------
    def _connections(self, result: dict) -> List[str]:
        tl = result.get("timeline") or {}
        send = tl.get("post_send_date")
        base = (send / 1000.0) if send is not None else None

        def rel(t: Optional[float]) -> str:
            if t is None:
                return "n/a"
            return "%+.1f ms" % ((t - base) * 1000.0) if base is not None else "%.3f" % t

        rows = []
        for seg in self._segments():
            first = {}
            for t, _p, event, detail in seg:
                first.setdefault(event, t)
            connect = next((d for _t, _p, e, d in seg if e == "connect-request"), "-")
            rows.append({"port": seg[0][1], "accepted": first.get("accepted"), "first": first.get("first-bytes"),
                         "greeted": first.get("greeted"), "connect": connect})
        wait = [(r["first"] - r["accepted"]) * 1000.0 for r in rows if r["first"] is not None and r["accepted"] is not None]
        edges = [10, 50, 100, 200, 500, 1000]
        labels = ["< 10 ms"] + ["%d-%d ms" % (a, b) for a, b in zip(edges, edges[1:])] + [">= %d ms" % edges[-1]]
        counts = [0] * len(labels)
        for w in wait:
            counts[sum(1 for e in edges if w >= e)] += 1
        out = ["%s 5. gateway: %d connection(s) accepted, %d with first bytes read; accepted -> first bytes histogram: %s" % (
            self.tag, len(rows), len(wait), ", ".join("%s: %d" % (l, c) for l, c in zip(labels, counts)))]
        if wait:
            out.append("%s 5. accepted -> first bytes: min %.1f, median %.1f, max %.1f ms" % (self.tag, min(wait), sorted(wait)[len(wait) // 2], max(wait)))
        for r in rows:
            gap = (r["first"] - r["accepted"]) * 1000.0 if r["first"] is not None and r["accepted"] is not None else None
            if gap is None:
                out.append("%s 5.   NO first bytes: client port %s accepted %s, greeted %s, connect %s" % (
                    self.tag, r["port"], rel(r["accepted"]), rel(r["greeted"]), r["connect"]))
            elif gap > 500:
                out.append("%s 5.   OVER 500 ms: client port %s accepted %s, first bytes %s (+%.0f ms), greeted %s, connect %s" % (
                    self.tag, r["port"], rel(r["accepted"]), rel(r["first"]), gap, rel(r["greeted"]), r["connect"]))
        return out
