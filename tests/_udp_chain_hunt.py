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
4. STALLS of the pytest process itself: a sentinel thread sleeps 10 ms in a loop and records every gap over 50 ms (`Sentinel`), with
   the CPU the process used during it and, for a gap over 100 ms, the top frames of every thread; and the same loop in a second, tiny
   process (`ChildSentinel`): both stalling = the CPU is starved, only the first = something in our process holds the GIL.
5. EVERY gateway connection: accepted, first bytes read in the greeting, greeted (`Chain.trace_events`), as a histogram of
   accepted -> first bytes and the list of those over 500 ms.
"""
from __future__ import annotations

import json
import linecache
import os
import re
import subprocess
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
    """A thread that sleeps 10 ms in a loop and records every gap over 50 ms between two of its wake-ups.
    A gap is the process not running Python code (the GIL held elsewhere, a descheduled process, a long call that does not release it).

    Each gap is a dict: ``start`` (time.time()), ``length`` (s), ``cpu`` (s of time.process_time() of THIS process between the two
    wake-ups, all threads: the CPU the process used during the gap), ``cpu_start`` / ``cpu_end`` (the two readings) and, for a gap over
    100 ms, ``frames``: sys._current_frames() taken right after the gap ended, as {thread name: [(file, line, function) x top 6, innermost
    first]}. The thread that held the GIL is usually still in the same call."""

    STEP = 0.010
    OVER = 0.050
    SNAPSHOT_OVER = 0.100
    DEPTH = 6

    def __init__(self) -> None:
        self.gaps: List[dict] = []
        self._halt = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="hunt-sentinel")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._halt.set()
        self._thread.join(timeout=5)

    def _snapshot(self) -> Dict[str, List[Tuple[str, int, str]]]:
        names = {t.ident: t.name for t in threading.enumerate()}
        mine = threading.get_ident()
        out: Dict[str, List[Tuple[str, int, str]]] = {}
        for tid, frame in sys._current_frames().items():
            if tid == mine:
                continue
            stack, f = [], frame
            while f is not None and len(stack) < self.DEPTH:
                stack.append((f.f_code.co_filename, f.f_lineno, f.f_code.co_name))
                f = f.f_back
            key = "%s#%d" % (names.get(tid, "thread"), tid)
            out[key] = stack
        return out

    def _run(self) -> None:
        last_wall, last, last_cpu = time.time(), time.perf_counter(), time.process_time()
        while not self._halt.is_set():
            time.sleep(self.STEP)
            now_wall, now, now_cpu = time.time(), time.perf_counter(), time.process_time()
            if now - last > self.OVER:
                gap = {"start": last_wall, "length": now - last, "cpu": now_cpu - last_cpu, "cpu_start": last_cpu, "cpu_end": now_cpu,
                       "frames": None}
                if now - last > self.SNAPSHOT_OVER:
                    try:
                        gap["frames"] = self._snapshot()
                    except Exception as exc:   # noqa: BLE001
                        gap["frames"] = {"snapshot failed": [("", 0, "%s: %s" % (type(exc).__name__, exc))]}
                self.gaps.append(gap)
                # the snapshot itself took time: the next gap starts after it
                now_wall, now, now_cpu = time.time(), time.perf_counter(), time.process_time()
            last_wall, last, last_cpu = now_wall, now, now_cpu


#: The second sentinel: a tiny process of its own, the same loop (10 ms sleeps, gaps over 50 ms, time.time() stamps). When both stall at
#: once the machine's CPU is starved; when only the one in the pytest process does, something in that process holds the GIL.
CHILD_CODE = r"""
import json, sys, threading, time
halt = threading.Event()
threading.Thread(target=lambda: (sys.stdin.read(), halt.set()), daemon=True).start()
gaps = []
last = time.time()
perf = time.perf_counter()
print("ready", flush=True)
while not halt.is_set():
    time.sleep(0.010)
    now, nperf = time.time(), time.perf_counter()
    if nperf - perf > 0.050:
        gaps.append([last, nperf - perf])
    last, perf = now, nperf
print(json.dumps(gaps), flush=True)
"""


class ChildSentinel:
    def __init__(self) -> None:
        self.gaps: List[Tuple[float, float]] = []
        self.note = ""
        self._proc: Optional["subprocess.Popen"] = None

    def start(self) -> None:
        try:
            self._proc = subprocess.Popen([sys.executable, "-I", "-c", CHILD_CODE], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          text=True)
            ready = self._proc.stdout.readline()          # it is running before the browser starts
            if "ready" not in ready:
                self.note = "the second sentinel said %r instead of ready" % ready
        except Exception as exc:   # noqa: BLE001
            self.note = "the second sentinel did not start: %s: %s" % (type(exc).__name__, exc)
            self._proc = None

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
            line = proc.stdout.readline()
            self.gaps = [(float(a), float(b)) for a, b in json.loads(line)]
        except Exception as exc:   # noqa: BLE001
            self.note = "the second sentinel gave no gaps: %s: %s" % (type(exc).__name__, exc)
        finally:
            try:
                proc.wait(timeout=5)
            except Exception:   # noqa: BLE001
                proc.kill()


class Hunt:
    """The three measurements of one run of the walk."""

    def __init__(self, gateway, rep: int = 0, binary: Optional[str] = None) -> None:
        self.gateway = gateway
        self.rep = rep
        self.tag = "HUNT[%d.%d.rep%s]" % (os.getpid(), int(time.time()), rep)
        self.sentinel = Sentinel()
        self.child = ChildSentinel()

    def start(self) -> None:
        self.child.start()
        self.sentinel.start()

    def stop(self) -> None:
        for sentinel in (self.sentinel, self.child):
            try:
                sentinel.stop()
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

    # --- 4: stalls of this process, and of a second process beside it ----------------------------------------------
    def _stalls(self, result: dict) -> List[str]:
        tl = result.get("timeline") or {}
        send, resp, tick = tl.get("post_send_date"), tl.get("post_response_date"), tl.get("tick_fired_date")
        base = (send / 1000.0) if send is not None else None
        gaps = list(self.sentinel.gaps)
        other = list(self.child.gaps)

        def rel(t: float) -> str:
            return "%+.1f ms" % ((t - base) * 1000.0) if base is not None else "%.3f" % t

        def overlaps(t: float, g: float) -> List[Tuple[float, float]]:
            return [(a, b) for a, b in other if a < t + g and a + b > t]

        out = ["%s 4. sentinel IN the pytest process (sleep %d ms, gaps over %d ms): %d gap(s), total %.0f ms, CPU used by the process during them %.0f ms" % (
            self.tag, self.sentinel.STEP * 1000, self.sentinel.OVER * 1000, len(gaps), sum(g["length"] for g in gaps) * 1000.0,
            sum(g["cpu"] for g in gaps) * 1000.0)]
        out.append("%s 4. sentinel in a SEPARATE tiny process (same loop): %d gap(s), total %.0f ms%s" % (
            self.tag, len(other), sum(b for _a, b in other) * 1000.0, ("; " + self.child.note) if self.child.note else ""))
        windows = []
        if send is not None and resp is not None:
            windows.append(("post_send -> post_response", send / 1000.0, resp / 1000.0))
        if send is not None and tick is not None:
            windows.append(("post_send -> tick_fired", send / 1000.0, tick / 1000.0))
        for name, a, b in windows:
            hit = [g for g in gaps if g["start"] < b and g["start"] + g["length"] > a]
            out.append("%s 4. in-process gaps overlapping %s (%.0f ms): %s" % (
                self.tag, name, (b - a) * 1000.0,
                "; ".join("start %s length %.0f ms" % (rel(g["start"]), g["length"] * 1000.0) for g in hit) or "none"))
            hit = [(t, g) for t, g in other if t < b and t + g > a]
            out.append("%s 4. second-process gaps overlapping %s: %s" % (
                self.tag, name, "; ".join("start %s length %.0f ms" % (rel(t), g * 1000.0) for t, g in hit) or "none"))
        out.append("%s 4. every in-process gap, in time order: start relative to post_send, length, CPU of the process during it "
                   "(process_time at start -> end), and whether the second process stalled at the same time" % self.tag)
        for g in gaps:
            both = overlaps(g["start"], g["length"])
            out.append("%s 4.   start %s (t=%.3f) length %6.0f ms  cpu %6.0f ms (%.3f -> %.3f s)  %s" % (
                self.tag, rel(g["start"]), g["start"], g["length"] * 1000.0, g["cpu"] * 1000.0, g["cpu_start"], g["cpu_end"],
                "BOTH STALLED (second process: %s)" % ", ".join("start %s length %.0f ms" % (rel(a), b * 1000.0) for a, b in both)
                if both else "only this process"))
        out.append("%s 4. every second-process gap, in time order (overlap with an in-process gap marked)" % self.tag)
        for t, g in other:
            mine = [x for x in gaps if x["start"] < t + g and x["start"] + x["length"] > t]
            out.append("%s 4.   start %s (t=%.3f) length %6.0f ms  %s" % (
                self.tag, rel(t), t, g * 1000.0,
                "BOTH STALLED (in-process: %s)" % ", ".join("start %s length %.0f ms" % (rel(x["start"]), x["length"] * 1000.0) for x in mine)
                if mine else "only the second process"))
        for rank, g in enumerate(sorted(gaps, key=lambda x: -x["length"])[:15], 1):
            if not g["frames"]:
                continue
            out.append("%s 4.   largest #%d: start %s length %.0f ms cpu %.0f ms; every thread right after the gap, top %d frames, innermost first "
                       "(identical stacks grouped):" % (self.tag, rank, rel(g["start"]), g["length"] * 1000.0, g["cpu"] * 1000.0, Sentinel.DEPTH))
            groups: Dict[tuple, List[str]] = {}
            for thread, stack in g["frames"].items():
                groups.setdefault(tuple(stack), []).append(thread)
            for stack, threads in sorted(groups.items(), key=lambda kv: len(kv[1])):
                names = ", ".join(threads[:3]) + (" +%d more" % (len(threads) - 3) if len(threads) > 3 else "")
                out.append("%s 4.     x%d [%s]" % (self.tag, len(threads), names))
                for filename, lineno, func in stack:
                    text = linecache.getline(filename, lineno).strip()[:70] if filename else ""
                    out.append("%s 4.         %s:%d %s%s" % (self.tag, os.path.basename(filename), lineno, func, ("  | " + text) if text else ""))
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
