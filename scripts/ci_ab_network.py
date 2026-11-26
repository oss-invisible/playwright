#!/usr/bin/env python3
"""Network A/B oracle: the same deterministic journey on the local lab with two engines, and every leaf of what the lab saw compared.

What the A/B oracle of the page (scripts/ci_ab_oracle.py) cannot see: what the browser PUTS ON THE WIRE. The same journey is walked on the lab with the engine
published as firefox-68 and with a candidate binary, with the same seeds, the same core and the same wrapper; what each side of the lab saw is written as a
transcript, normalised, and compared leaf by leaf. A leaf that differs is red, with its path and both values.

The lab (127.0.0.1 and the address of this host, nothing else; no internet): a SOCKS5 gateway in coherent mode that carries TCP and UDP (the CONNECT log and the
datagram log are the transcript's hosts and UDP path), the fake DoH behind it (every question: name, type, order), a witness and the watcher of direct connections
(and the resolver trap where it can run: Linux, root), and servers that write down what they were sent:

  * the P4 journey (tests/fakeweb: 17 steps a person makes, driven by events, never by a timer) on two origins, behind a tee that keeps every HTTP/1.1 request line
    and header in the order they were written;
  * one HTTPS site with two servers (HTTP/1.1 and HTTP/2 by the ALPN they offer): the ClientHello, the HTTP/2 SETTINGS, WINDOW_UPDATE, PRIORITY frames and the
    pseudo-header order, the header order of each request (tests/_wire_capture.py);
  * one HTTP/3 site (QUIC through the relay): the transport parameters, the SETTINGS frame and the header order;
  * one WebRTC page (STUN and TURN by address through the relay): the candidates, normalised, the SDP, the datagrams the relay carried;
  * IDLE launches (no action): the destinations of a browser that does nothing, each up to an event the lab drives (see IDLE below).

THE NORMALISATION. A port of the lab becomes the role of its server (``A``, ``O``, ``page``, ``h1``, ``h2``, ``h3``, ``witness``), written plainly (``:41039``) or inside a URL
parameter (``%3A41039``); the random token of a page of the journey (``tok=``, the ids ``lp``, ``park``, ``kapark`` and ``sf``, which the page draws at every load) and the values of a
cookie are dropped. ci/ab-network-volatile.json lists the leaves that differ between two runs of ONE engine: each entry has the pattern of the leaf
path, the reason, and the A/A run that proved it. The list is born from the A/A (``ab --a-a``, ``compare --a-a``), never written ahead of it, and it may only shrink: an
entry that matches no leaf of a run is red as stale. An entry that is ``optional`` names a thing that may or may not have happened in a run (a resumption): when one of the two runs
has nothing under it, what the other has is excused; when both have something, it is compared; and a run that never has it on any seed while the other has it on some is red.

THE POOL. What a connection and a request SAY is the engine's and is compared; how many connections there are, which one a request rode, its stream, the frames after the first
request, a connection opened and never used, how many datagrams QUIC sent, and where in the journey a connection landed are the pool's and the timers', and are not compared as such
(``fold_wire``, ``journey_data``, the h3 ``udp``): one summary per role over the whole walk, the distinct fingerprints of the connections that carried a request, and every request
with how many times it was made.

THE CONTROL. ``--a-a`` runs the published engine twice and compares: a difference there is a BROKEN CONTROL (exit 2), not a finding about the candidate. With a candidate,
the A/A comes first; if it is broken the A/B is not run (exit 2) unless ``--measure``, which runs everything and still exits 2.

IDLE. The engine fires the observer ``browser-idle-startup-tasks-finished`` when its startup tasks are done. The only channel that could read it in this product is
Marionette in chrome scope (tests/webfont_probe.py, ``-marionette -remote-allow-system-access`` and MOZ_REMOTE_ALLOW_SYSTEM_ACCESS=1), which changes the launch of the browser
that is measured (an extra remote agent, extra arguments and environment); it is not used. The window of an idle launch ends instead at an EVENT THE LAB DRIVES, with no
clock: ``c1`` ends at the first completed round trip of a sentinel request the lab asked of the browser (everything the lab saw before it is the idle traffic); ``c2``
repeats the round trip until two consecutive rounds leave the lab's count of what is not the sentinel unchanged (at most MAX_ROUNDS rounds; ``converged`` says
whether it stopped by itself). Both record the destinations they saw; the A/A says which one is stable.

    python ci_ab_network.py run --binary B --label L --out transcript.json [--seeds 11,12]
    python ci_ab_network.py compare A.json B.json [--floor N] [--out diff.json] [--a-a]
    python ci_ab_network.py ab --out DIR [--candidate BIN --candidate-seal SEAL] [--published-binary B] [--a-a] [--floor N]

HOW EACH LAUNCH ENDS. Every browser a run launches (the main launch of each seed and the idle and exit-ping launches of the first) is seen by a probe
(scripts/ci_crash_evidence.py: the exit code of the browser's parent after the close the launch already makes, and a watcher on the crash folders): the transcript carries them
under ``exits``, outside the leaves that are compared, and a parent that did not end with 0, or whose code was not seen, or a run that recorded no launch, is a RED line that names
the side, the seed, the launch and the code. What a launch that died left is kept beside the transcript (``transcript-68.json`` -> ``transcript-68.crash/``): the core through gdb with
the build ids, the ``.extra`` and the minidump names, ``said.txt`` for what the collectors did not find. This oracle runs no strace (a trace would change the outcome it observes).

THE WEBRTC EVIDENCE AND THE HUNT. The WebRTC walk keeps, under ``transcript["evidence"]["seeds"][<seed>]["webrtc"]`` (never compared: the comparison reads ``seeds`` and nothing else), every datagram the gateway
routed in it (when, target, message, STUN transaction id), the moments of its stages and of the page's call, and the page's own ``performance.now()`` at each step of the gather. With
``AB_NETWORK_HUNT=webrtc:<seed>:<n>`` the command ``ab`` runs ONLY that walk of that seed, ``n`` times for each engine of the A/A, and writes ``<out>/hunt/<engine>-<i>.json`` and ``<out>/hunt/summary.json``;
it decides nothing and exits 0 (e2e-hunt.yml: the input ``ab_network_webrtc_seed``).

Exit codes: 0 green, 1 red (a finding), 2 broken control (the A/A is red) or a run that could not run.
"""
from __future__ import annotations

import argparse
import collections
import fnmatch
import hashlib
import itertools
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "tests"))
import ci_crash_evidence as crash  # noqa: E402  (how each launch's parent ended: a code other than 0 is red, and what it left is saved)

FORMAT = "ab-network/1"
DIFF_FORMAT = "ab-network-diff/1"
EVIDENCE_FORMAT = "ab-network-evidence/1"
HUNT_FORMAT = "ab-network-hunt/1"
HUNT_SUMMARY_FORMAT = "ab-network-hunt-summary/1"
#: The environment variable that makes ``ab`` repeat one walk of one seed and nothing else (``cmd_hunt``): ``webrtc:<seed>:<n>``.
HUNT_ENV = "AB_NETWORK_HUNT"
#: The engine's own log in a hunt (the launch's environment, in hunt mode only): nICEr's reasons for accepting or rejecting an answer or ending a transaction, and the signaling.
#: ``sync`` makes each line reach the file as it is written, so a process that ends (or is killed) loses none.
HUNT_MOZ_LOG = "sync,timestamp,nicer:5,signaling:5"
VOLATILE_FORMAT = "ab-network-volatile/1"
VOLATILE_FILE = ROOT / "ci" / "ab-network-volatile.json"
SEEDS_FILE = ROOT / "ci" / "persona-seeds.json"
#: The seeds the A/B oracle of the page uses today (scripts/ci_host_gate.py: SEEDS), when ci/persona-seeds.json has no "candidate" key.
FALLBACK_SEEDS = (11, 12, 13)
ENGINE_TAG = "firefox-68"
CANDIDATE = "candidate"
#: The most times the engine sends one STUN request (a transmit and its retransmissions) before it gives the transaction up: the pref
#: ``media.peerconnection.ice.stun_client_maximum_transmits`` (modules/libpref/init/all.js), 7. The first RTO is 100 ms and doubles, so how
#: MANY transactions a walk has is the engine's and is compared, and how many datagrams each took is the network's timing and is not: a
#: transaction over this number is a problem of the run (``udp_summary``'s ``over_limit``, ``_udp_overruns``), never a compared leaf.
STUN_MAX_TRANSMITS = 7
#: What a leaf that one side does not have reads as.
MISSING = "<missing>"
#: A value longer than this in diff.json is replaced by its length and its sha256.
CLIP = 400
PRINT_LIMIT = 200
#: The rounds of a sentinel the converging idle condition (c2) asks for at most. A count, not a time.
MAX_ROUNDS = 8
#: The most visits of the HTTP/3 page: it stops at the first visit read over h3, as a person who visits again does.
H3_VISITS = 5
#: The safety nets of a run, in seconds: a launch that does not come back, a run that does not end. Never a verdict.
RUN_TIMEOUT_S = 40 * 60
WAIT_S = 30
#: The guard of the two waits that end a visit of the HTTPS and HTTP/3 walks (``settle_visit``): the deadline of a HANG, as ``profile_p4.HANG_GUARD_MS`` is for the journey
#: (a test holds the two equal). A healthy browser reaches both far inside it; one that reaches it has stopped, and the visit says so as an error of the transcript.
LEAVE_GUARD_MS = 120000
#: The favicon of an origin: the one request the browser makes on its own, once per origin of a launch, that the pages of the walk do not ask for.
FAVICON = "/favicon.ico"
#: How many times a lab is made again when two of its servers share a port number.
MAX_LAB_TRIES = 50
#: Whose categories a transcript holds under each seed.
CATEGORIES = ("journey", "https", "h3", "webrtc", "idle", "lab")


# ---------------------------------------------------------------------------------------------------------------------------------
#  The comparison: two transcripts, leaf by leaf (no browser in this part)
# ---------------------------------------------------------------------------------------------------------------------------------
def _key(k: Any) -> str:
    """A key as a segment of a path: the path is matched with fnmatch, where [ and ] mean something."""
    return str(k).replace("[", "(").replace("]", ")")


def flatten(value: Any, prefix: str = "seeds") -> Dict[str, Any]:
    """Every scalar of ``value`` as a leaf with a path ``a/b/3/c`` (a list index is a segment). An empty dict or list is one leaf: that it is empty is a fact."""
    out: Dict[str, Any] = {}

    def walk(path: str, v: Any) -> None:
        if isinstance(v, dict) and v:
            for k, item in v.items():
                walk("%s/%s" % (path, _key(k)), item)
        elif isinstance(v, (list, tuple)) and v:
            for i, item in enumerate(v):
                walk("%s/%d" % (path, i), item)
        else:
            out[path] = v if not isinstance(v, tuple) else list(v)

    walk(prefix, value)
    return out


def category_of(path: str) -> str:
    """``seeds/<seed>/<category>/...`` -> the category."""
    parts = path.split("/")
    return parts[2] if len(parts) > 2 else parts[-1]


def _same(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _clip(value: Any) -> Any:
    text = json.dumps(value, sort_keys=True)
    if len(text) <= CLIP:
        return value
    return {"clipped": text[:CLIP // 2], "len": len(text), "sha256": hashlib.sha256(text.encode()).hexdigest()}


def load_volatile(path: Path = VOLATILE_FILE) -> List[Dict[str, str]]:
    """The entries of the volatile list: each ``{"pattern", "reason", "aa_run"}``. A malformed list is an error, not an empty one."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("format") != VOLATILE_FORMAT:
        raise SystemExit("%s: format %r, not %r" % (path, document.get("format"), VOLATILE_FORMAT))
    entries = document.get("entries")
    if not isinstance(entries, list):
        raise SystemExit("%s: `entries` is not a list" % path)
    seen = set()
    for entry in entries:
        for field in ("pattern", "reason", "aa_run"):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                raise SystemExit("%s: every entry needs a non-empty %s (%r)" % (path, field, entry))
        if not isinstance(entry.get("optional", False), bool):
            raise SystemExit("%s: optional is true or false (%r)" % (path, entry))
        if entry["pattern"] in seen:
            raise SystemExit("%s: the pattern %r is listed twice" % (path, entry["pattern"]))
        seen.add(entry["pattern"])
    return entries


def direct_problems(transcript: Dict[str, Any], side: str) -> List[str]:
    """What a transcript says reached the lab, or the network, around the proxy: ``seeds/<seed>/lab/direct/*`` must be empty lists on every side, and so must ``seeds/<seed>/idle/exit_pings/direct/*``."""
    out = []
    for seed, body in sorted((transcript.get("seeds") or {}).items()):
        where = (("", (body.get("lab") or {}).get("direct")), (" (the exit pings)", (((body.get("idle") or {}).get("exit_pings")) or {}).get("direct")))
        for label, lists in where:
            for name, found in sorted((lists or {}).items()):
                if found:
                    out.append("%s, seed %s: %d direct %s%s: %s" % (side, seed, len(found), name, label, json.dumps(found, sort_keys=True)[:300]))
    return out


def _owner(path: str, pattern: str) -> Optional[str]:
    """The shortest prefix of ``path`` (by segments) that ``pattern`` matches: the thing an optional entry is about (``seeds/2/https/h1/wire/resumed_connections``)."""
    parts = path.split("/")
    for i in range(1, len(parts) + 1):
        if fnmatch.fnmatchcase("/".join(parts[:i]), pattern):
            return "/".join(parts[:i])
    return None


def _optional_leaves(patterns: List[str], la: Dict[str, Any], lb: Dict[str, Any]) -> Dict[str, List[Tuple[str, bool]]]:
    """For the entries of the volatile list that are ``optional``: ``{leaf path: [(pattern, one_sided)]}`` for every leaf under an owner of the pattern. An owner is one thing
    that may or may not have happened in a run (a resumed connection of an origin: whether a resumption happens at all follows the ticket and the pool). ``one_sided`` is True when
    one of the two transcripts has nothing under the owner, and then all its leaves are excused; when both have something under it, it is compared like any other leaf."""
    out: Dict[str, List[Tuple[str, bool]]] = {}
    for pattern in patterns:
        owners: Dict[str, Dict[str, bool]] = {}
        for side, leaves in (("a", la), ("b", lb)):
            for path in leaves:
                owner = _owner(path, pattern)
                if owner is not None:
                    owners.setdefault(owner, {"a": False, "b": False})
                    if path != owner:
                        owners[owner][side] = True
        for path in set(la) | set(lb):
            owner = _owner(path, pattern)
            if owner is not None:
                out.setdefault(path, []).append((pattern, not (owners[owner]["a"] and owners[owner]["b"])))
    return out


def _optional_invariants(patterns: List[str], la: Dict[str, Any], lb: Dict[str, Any], label_a: str, label_b: str) -> List[str]:
    """What may not vanish in an optional owner: for each owner generalised over the seed (``https/h1/wire/resumed_connections`` of every seed), a run in which the thing
    never happened on any seed, while the other run has it on some, is a problem: ticket timing decides WHICH launch of an origin resumes a connection, never whether the engine
    resumes at all. An engine that stops resuming (or starts to) is red."""
    problems: List[str] = []
    for pattern in patterns:
        seen: Dict[str, Dict[str, int]] = {}
        for side, leaves in (("a", la), ("b", lb)):
            for path in leaves:
                owner = _owner(path, pattern)
                if owner is None:
                    continue
                parts = owner.split("/")
                general = "/".join(parts[:1] + ["*"] + parts[2:]) if len(parts) > 2 else owner
                row = seen.setdefault(general, {"a": 0, "b": 0})
                if path != owner:
                    row[side] += 1
        for general, row in sorted(seen.items()):
            if (row["a"] > 0) != (row["b"] > 0):
                has, lacks = (label_a, label_b) if row["a"] else (label_b, label_a)
                problems.append("%s has something under %s on %d leaves and %s never has: a thing that may come and go with the timing must still happen in every run"
                                % (has, general, max(row["a"], row["b"]), lacks))
    return problems


_UDP_OPENERS = re.compile(r"^(?P<where>.*/udp)/openers/(?P<target>[^/]+)/\d+$")


def _udp_attribution(la: Dict[str, Any], lb: Dict[str, Any]) -> Tuple[set, List[Dict[str, Any]], int]:
    """The attribution of the datagrams, which loses a race with the life of the socket (A/A run 44993377458: ``browser:parent`` on one side of a destination and nothing on the
    other, on both systems). It is compared as the SET of kinds found per destination over the whole transcript (``seeds/<seed>/.../udp``): a side that found none says nothing,
    two sides that found kinds must have found the same, and two different kinds (``browser:parent`` against ``core``) are a difference. Returns the paths that take part (they
    are not compared leaf by leaf), the differences, and how many datagrams were unattributed (counted, printed, not in the verdict)."""
    groups: Dict[Tuple[str, str], Dict[str, set]] = {}
    paths: set = set()
    unattributed = 0
    for side, leaves in (("a", la), ("b", lb)):
        for path, value in leaves.items():
            m = _UDP_OPENERS.match(path)
            if m:
                paths.add(path)
                groups.setdefault((m.group("where"), m.group("target")), {"a": set(), "b": set()})[side].add(value)
            elif path.endswith("/udp/unattributed") and isinstance(value, int):
                paths.add(path)
                unattributed += value
    differences = []
    for (where, target), kinds in sorted(groups.items()):
        if kinds["a"] and kinds["b"] and kinds["a"] != kinds["b"]:
            differences.append({"path": "%s/openers/%s" % (where, target), "a": sorted(kinds["a"]), "b": sorted(kinds["b"])})
    return paths, differences, unattributed


_UDP_OVER_LIMIT = re.compile(r"^(?P<where>.*/udp)/over_limit/(?P<target>[^/]+)/\d+$")


def _udp_overruns(la: Dict[str, Any], lb: Dict[str, Any], label_a: str, label_b: str) -> Tuple[set, List[str]]:
    """The STUN transactions that were sent more times than the engine's maximum (``udp_summary``'s ``over_limit``: per destination, the number of transmits of each such
    transaction). They are not compared leaf by leaf: how many datagrams a transaction took is the network's timing, but more than the engine's own maximum is not
    timing, it is a client that does not stop. Returns the paths that take part and one problem line for each."""
    paths: set = set()
    problems: List[str] = []
    for label, leaves in ((label_a, la), (label_b, lb)):
        for path, value in sorted(leaves.items()):
            m = _UDP_OVER_LIMIT.match(path)
            if m:
                paths.add(path)
                problems.append("%s: a STUN transaction to %s was sent %s times, more than the engine's maximum of %d transmits (%s)"
                                % (label, m.group("target"), value, STUN_MAX_TRANSMITS, m.group("where")))
    return paths, problems


def turn_path_problems(transcript: Dict[str, Any], label: str) -> List[str]:
    """Whether the TURN path of the lab WORKED in each WebRTC walk of a transcript: a relay candidate was made, no error names the lab's TURN server (701 is what a browser reports when
    every answer it got was thrown away as invalid, as with a wrong credential), and the server refused no request (``webrtc/turn/refused``, written by ``walk_webrtc``). An A/A that
    compared two runs in which the TURN path failed the same way would pass; this is the line that says it did not work. A transcript with no WebRTC walk says nothing."""
    out: List[str] = []
    for seed, body in sorted((transcript.get("seeds") or {}).items()):
        walk = body.get("webrtc")
        if not isinstance(walk, dict):
            continue
        errors = [e for e in walk.get("errors") or [] if TURN_TARGET in str(e)]
        refused = (walk.get("turn") or {}).get("refused") or 0
        if "relay" in (walk.get("candidate_types") or []) and not errors and not refused:
            continue
        seen = (errors + ["the lab TURN refused %d request(s)" % refused] * bool(refused)) or ["no error"]
        out.append("seeds/%s/webrtc: %s made no relay candidate through the lab TURN (%s). Fix: the probe's credential must be the lab TURN's (tests/_turn_chain.py PASSWORD) "
                   "and the lab's answers must reach the browser. Rule: the A/A proves the TURN path it compares" % (seed, label, "; ".join(seen)))
    return out


def turn_deallocation_problems(transcript: Dict[str, Any], label: str) -> List[str]:
    """What the lab's TURN server heard of deallocations (``webrtc/turn/deallocation``, written by ``walk_webrtc`` with :func:`deallocation_facts`) that a walk must not have: an allocation ended
    by more than one deallocation, a deallocation from an address that has no allocation. One deallocation, or none, is the engine's best effort at teardown and is evidence, not a leaf."""
    out: List[str] = []
    for seed, body in sorted((transcript.get("seeds") or {}).items()):
        walk = body.get("webrtc")
        facts = ((walk or {}).get("turn") or {}).get("deallocation") if isinstance(walk, dict) else None
        if not isinstance(facts, dict):
            continue
        for key, what, fix in (("repeated", "%d allocation(s) of the lab TURN were ended by more than one deallocation (a Refresh with LIFETIME 0)",
                                "find what sends the teardown twice; the engine sends it once"),
                               ("unallocated", "the lab TURN heard %d deallocation(s) (a Refresh with LIFETIME 0) from an address that has no allocation",
                                "find what deallocates an allocation it never made; the lab's record is TurnServer.deallocations in tests/_turn_chain.py"),
                               ("early", "%d deallocation(s) (a Refresh with LIFETIME 0) reached the lab TURN before the page began closing its connection",
                                "find what ends the allocation while the page still holds it; the engine sends it only while it shuts the client down, after the close")):
            if facts.get(key):
                out.append("seeds/%s/webrtc: %s: %s. Fix: %s. Rule: the TURN deallocation is one best-effort send at teardown (nICEr nr_turn_client_deallocate)"
                           % (seed, label, what % facts[key], fix))
    return out


def exit_lines(transcript: Dict[str, Any], label: str, side: str) -> List[str]:
    """The red lines of the launches of one side that did not end with 0 (``exits``, which ``run`` writes: scripts/ci_crash_evidence.py). A transcript that has no ``exits``
    was not made by ``run`` and says nothing about it; one that has it and no launch in it is a run whose probe saw no browser, which is red."""
    exits = transcript.get("exits")
    if exits is None:
        return []
    if not exits.get("launches"):
        return ["the run %s recorded no launch (side %s): the probe did not see a browser, so how its browsers ended is not known" % (label, side)]
    return crash.exit_problems(exits["launches"], label, transcript.get("platform") == "win32", side=side)


def compare_transcripts(a: Dict[str, Any], b: Dict[str, Any], volatile: List[Dict[str, str]], floor: int = 0,
                        label_a: str = "A", label_b: str = "B") -> Dict[str, Any]:
    """The verdict on two transcripts (the ``seeds`` of each, as ``run`` writes them).

    Red when a compared leaf differs (``differences``: path and both values), when a side reached the network around the proxy, when the two sides did not
    run the same seeds, when fewer than ``floor`` leaves were compared, or when an entry of the volatile list matches no leaf of either transcript (``stale``)."""
    la, lb = flatten(a.get("seeds") or {}), flatten(b.get("seeds") or {})
    attribution, attribution_differences, unattributed = _udp_attribution(la, lb)
    overrun_paths, overruns = _udp_overruns(la, lb, label_a, label_b)
    turn_problems = (turn_path_problems(a, label_a) + turn_path_problems(b, label_b) +
                     turn_deallocation_problems(a, label_a) + turn_deallocation_problems(b, label_b))
    refused_paths = {p for p in set(la) | set(lb) if re.match(r"^seeds/[^/]+/webrtc/turn/(refused|deallocation/(repeated|unallocated|early))$", p)}
    paths = sorted((set(la) | set(lb)) - attribution - overrun_paths - refused_paths)
    problems: List[str] = []
    if set(a.get("seeds") or {}) != set(b.get("seeds") or {}):
        problems.append("the sides ran different seeds: %s and %s" % (sorted(a.get("seeds") or {}), sorted(b.get("seeds") or {})))
    matched: Dict[str, List[str]] = {e["pattern"]: [] for e in volatile}
    volatile_by_pattern = {e["pattern"]: e for e in volatile}
    excused = 0
    compared: List[str] = []
    invariants = _optional_invariants([e["pattern"] for e in volatile if e.get("optional")], la, lb, label_a, label_b)
    optional = _optional_leaves([e["pattern"] for e in volatile if e.get("optional")], la, lb)
    for path in paths:
        hit = [e["pattern"] for e in volatile if not e.get("optional") and fnmatch.fnmatchcase(path, e["pattern"])]
        for pattern, one_sided in optional.get(path, []):
            matched[pattern].append(path)
            if one_sided:
                hit.append(pattern)
        for pattern in hit:
            if not volatile_by_pattern[pattern].get("optional"):
                matched[pattern].append(path)
        if hit:
            excused += 1
        else:
            compared.append(path)
    differences = list(attribution_differences)
    for path in compared:
        va, vb = la.get(path, MISSING), lb.get(path, MISSING)
        if not _same(va, vb):
            differences.append({"path": path, "a": _clip(va), "b": _clip(vb)})
    stale = [e["pattern"] for e in volatile if not matched[e["pattern"]]]
    used = [{"pattern": e["pattern"], "leaves": len(matched[e["pattern"]]),
             "differing": sum(1 for p in matched[e["pattern"]] if not _same(la.get(p, MISSING), lb.get(p, MISSING)))} for e in volatile]
    direct = direct_problems(a, label_a) + direct_problems(b, label_b)
    by_category = collections.Counter(category_of(p) for p in compared)
    if len(compared) < floor:
        problems.append("only %d leaves were compared, fewer than the floor of %d: a run that saw less than it should passes anything" % (len(compared), floor))
    problems += ["a direct connection: " + d for d in direct]
    problems += exit_lines(a, label_a, "A") + exit_lines(b, label_b, "B")
    problems += invariants
    problems += overruns
    problems += turn_problems
    problems += ["the volatile entry %r matches no leaf of either transcript: it is stale, drop it" % p for p in stale]
    problems += ["%s: %s %s | %s %s" % (d["path"], label_a, json.dumps(d["a"], sort_keys=True)[:160], label_b, json.dumps(d["b"], sort_keys=True)[:160])
                 for d in differences]
    return {"format": DIFF_FORMAT, "verdict": "red" if problems else "green", "problems": problems, "differences": differences, "leaves": len(compared),
            "by_category": dict(sorted(by_category.items())), "excused": excused, "volatile": used, "stale": stale, "direct": direct, "invariants": invariants, "unattributed_datagrams": unattributed, "floor": floor,
            "a": label_a, "b": label_b}


def propose_volatile(report: Dict[str, Any]) -> List[str]:
    """The leaf paths an A/A found different, generalised over the seed and over the step (``seeds/*/journey/steps/*/...``): what the list would need, to be reviewed."""
    out = set()
    for d in report["differences"]:
        parts = d["path"].split("/")
        if len(parts) > 2:
            parts[1] = "*"
        if len(parts) > 4 and parts[2] == "journey" and parts[3] == "steps":
            parts[4] = "*"
        out.add("/".join(parts))
    return sorted(out)


def print_report(report: Dict[str, Any], control: bool) -> None:
    n = len(report["differences"])
    print("compared %d leaves (%s); %d leaves excused as volatile" % (report["leaves"], ", ".join("%s %d" % kv for kv in report["by_category"].items()) or "none",
                                                                       report["excused"]))
    for d in report["differences"][:PRINT_LIMIT]:
        print("DIFF %s: %s %s | %s %s" % (d["path"], report["a"], json.dumps(d["a"], sort_keys=True)[:160], report["b"], json.dumps(d["b"], sort_keys=True)[:160]))
    if n > PRINT_LIMIT:
        print("... and %d more differences in the diff file" % (n - PRINT_LIMIT))
    if report.get("unattributed_datagrams"):
        print("%d datagrams were not attributed to a process (the association was made in a stage that was not looked at, or the socket was gone): counted, not compared" % report["unattributed_datagrams"])
    for problem in report["problems"]:
        if not any(problem.startswith(d["path"] + ":") for d in report["differences"]):
            print("FAIL " + problem)
    print("AB NETWORK %s %s: %d differences, %d problems" % ("A/A" if control else "A/B", "OK" if report["verdict"] == "green" else "FAILED", n, len(report["problems"])))


# ---------------------------------------------------------------------------------------------------------------------------------
#  The normalisation (no browser)
# ---------------------------------------------------------------------------------------------------------------------------------
_PORT = re.compile(r":(\d{2,5})(?!\d)")
_TOKEN = re.compile(r"(?P<p>[?&]tok=)[A-Za-z0-9]+")
_PAGE_ID = re.compile(r"(?P<p>[?&]id=(?:lp|park|kapark))[a-z0-9]{4,8}")
#: The id of the held requests of the slow frame is ``sf`` + the page's random token + the index of the request (tests/fakeweb/journey_site.py, JFRAME_JS "slow"): the token goes, the index stays.
_FRAME_ID = re.compile(r"(?P<p>[?&]id=sf)[a-z0-9]{1,10}(?P<i>\d)(?![a-z0-9])")
#: A port written inside a value that is itself a URL parameter: ``a=http%3A%2F%2Fjourney-a.test%3A41039`` (encodeURIComponent of the origin the ad frame is told to open).
_ENCODED_PORT = re.compile(r"%3[Aa](\d{2,5})(?!\d)")


class Normalizer:
    """Turns what changes from one run of the lab to the next into what does not: the port of a server of the lab into its role, the random token of a page of the
    journey, the value of a cookie. ``roles`` maps a port number to the role of the server that holds it."""

    def __init__(self, roles: Optional[Dict[int, str]] = None) -> None:
        self.roles = roles if roles is not None else {}

    def text(self, value: str) -> str:
        def port(m: "re.Match[str]") -> str:
            role = self.roles.get(int(m.group(1)))
            return ":" + role if role else m.group(0)

        def encoded_port(m: "re.Match[str]") -> str:
            role = self.roles.get(int(m.group(1)))
            return "%3A" + role if role else m.group(0)

        value = _PORT.sub(port, value)
        value = _ENCODED_PORT.sub(encoded_port, value)
        value = _TOKEN.sub(lambda m: m.group("p") + "<TOK>", value)
        value = _FRAME_ID.sub(lambda m: m.group("p") + "<TOK>" + m.group("i"), value)
        return _PAGE_ID.sub(lambda m: m.group("p") + "<TOK>", value)

    def header(self, name: str, value: str) -> str:
        if name.lower() == "cookie":
            return "; ".join(part.split("=", 1)[0].strip() + "=<v>" for part in value.split(";") if part.strip())
        return self.text(value)

    def headers(self, headers: List[List[str]]) -> List[List[str]]:
        return [[n, self.header(n, v)] for n, v in headers]

    def walk(self, value: Any) -> Any:
        """Every string of a structure (keys too), normalised."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(k) if isinstance(k, str) else k: self.walk(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.walk(v) for v in value]
        return value


def request_summary(requests: List[Dict[str, Any]], norm: Normalizer) -> Dict[str, Any]:
    """The HTTP requests of one phase as ``{"<METHOD> <role><target>": {"count", "headers", "name_orders", "body_length"}}``: what was asked, the headers of the first
    ask in the order they were written, and every distinct ORDER of the names that was seen. Which of two parallel requests came first is not kept (a race of
    the page, not a property of the client)."""
    out: Dict[str, Dict[str, Any]] = {}
    for r in requests:
        sig = "%s %s%s" % (r["method"], r.get("role", ""), norm.text(r["target"]))
        names = ",".join(n for n, _v in r["headers"])
        entry = out.setdefault(sig, {"count": 0, "headers": norm.headers(r["headers"]), "name_orders": [], "body_length": r.get("body_length", 0)})
        entry["count"] += 1
        if names not in entry["name_orders"]:
            entry["name_orders"].append(names)
    for entry in out.values():
        entry["name_orders"].sort()
    return dict(sorted(out.items()))


def _openers(targets: List[str], openers: Optional[List[Optional[str]]]) -> Dict[str, List[str]]:
    """``{target: the distinct openers, sorted}`` for the entries whose opener is known (``openers[i]`` is who opened ``targets[i]``, None when it was not looked for)."""
    found: Dict[str, set] = collections.defaultdict(set)
    for target, opener in zip(targets, openers or []):
        if opener is not None:
            found[target].add(opener)
    return {t: sorted(found[t]) for t in sorted(found)}


def connect_summary(targets: List[str], norm: Normalizer, openers: Optional[List[Optional[str]]] = None) -> Dict[str, Any]:
    """The CONNECTs of one phase: the targets in the order they first appeared, and how many of each; and, in the stages where the lab looked, who opened them (``openers``)."""
    ordered = [norm.text(t) for t in targets]
    counts = collections.Counter(ordered)
    out: Dict[str, Any] = {"order": list(dict.fromkeys(ordered)), "counts": dict(sorted(counts.items()))}
    who = _openers(ordered, openers)
    if who:
        out["openers"] = who
    return out


def dns_summary(queries: List[Tuple[str, int, str]], openers: Optional[List[Optional[str]]] = None) -> Dict[str, Any]:
    """The DoH questions of one phase: the names in the order they were first asked, and the types asked of each (the order of A, AAAA and HTTPS of one lookup
    is a race of the resolver)."""
    types: Dict[str, set] = collections.defaultdict(set)
    order: List[str] = []
    for name, qtype, _via in queries:
        if name not in types:
            order.append(name)
        types[name].add(qtype)
    out: Dict[str, Any] = {"order": order, "types": {n: sorted(types[n]) for n in sorted(types)}}
    who = _openers([q[0] for q in queries], openers)
    if who:
        out["openers"] = who
    return out


def udp_summary(events: List[Dict[str, Any]], norm: Normalizer) -> Dict[str, Any]:
    """The datagrams the relay carried in one phase: per destination, the kinds of message and how many.

    ``counts`` is the number of STUN TRANSACTIONS (distinct transaction ids) per destination, plus one for each datagram that is not STUN: a retransmission carries the
    transaction id of the request it repeats, and how many times a request is repeated is the timing of the network (the first RTO is 100 ms and doubles; a late answer
    on a loaded host adds one), so it is not what the engine decides. ``over_limit`` (only when there is one) lists, per destination, the transmits of each transaction
    that has more than ``STUN_MAX_TRANSMITS``: ``compare_transcripts`` reports it as a problem and never compares it."""
    kinds: Dict[str, set] = collections.defaultdict(set)
    transmits: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    plain: collections.Counter = collections.Counter()
    order: List[str] = []
    for e in events:
        target = norm.text(e["target"])
        if target not in kinds:
            order.append(target)
        kinds[target].add(e["message"])
        if e.get("tid") is None:
            plain[target] += 1
        else:
            transmits[target][e["tid"]] += 1
    counts = {t: plain[t] + len(transmits[t]) for t in sorted(kinds)}
    out: Dict[str, Any] = {"order": order, "messages": {t: sorted(kinds[t]) for t in sorted(kinds)}, "counts": counts}
    over = {t: sorted((n for n in transmits[t].values() if n > STUN_MAX_TRANSMITS), reverse=True) for t in sorted(kinds)}
    over = {t: v for t, v in over.items() if v}
    if over:
        out["over_limit"] = over
    who = _openers([norm.text(e["target"]) for e in events], [e.get("opener") for e in events])
    if who:
        out["openers"] = who
        # A datagram is attributed by the association it rode, looked up when the association was made; one on an association made in a stage that was not looked at is
        # unattributed. They are counted for the report (``unattributed_datagrams``), never compared.
        out["unattributed"] = sum(1 for e in events if e.get("opener") is None)
    return out


_CANDIDATE = re.compile(r"^(?:a=)?candidate:\S+ (\d+) (\w+) (\d+) (\S+) (\d+) typ (\w+)(.*)$")


def candidate_summary(line: str, exit_ip: str) -> str:
    """One ICE candidate line as what a client decides, not what it draws at random: the foundation, the address (``<exit>``, ``<mdns>`` or ``<other>``) and the
    port go; the component, protocol, priority and type stay, and the related address with the same rules."""
    m = _CANDIDATE.match(line.strip())
    if not m:
        return "unparsed:" + re.sub(r"\d+", "N", line)[:80]
    component, protocol, priority, address, _port, typ, rest = m.groups()
    kind = "<mdns>" if address.endswith(".local") else "<exit>" if address == exit_ip else "<other>"
    extra = re.sub(r"raddr (\S+)", lambda r: "raddr <exit>" if r.group(1) == exit_ip else "raddr <other>" if r.group(1) not in ("0.0.0.0", "::") else r.group(0), rest)
    extra = re.sub(r"rport \d+", lambda r: "rport 0" if r.group(0) == "rport 0" else "rport <port>", extra)
    return "%s comp%s prio%s %s %s%s" % (protocol.lower(), component, priority, typ, kind, extra)


_SDP_RANDOM = re.compile(r"^(a=(?:ice-ufrag|ice-pwd|fingerprint|msid|ssrc|ssrc-group|identity)[: ]).*$")


def sdp_summary(sdp: str, exit_ip: str) -> List[str]:
    """The lines of an SDP with what is random in each written ``<v>`` and the candidates left to :func:`candidate_summary`."""
    out = []
    for line in sdp.replace("\r", "").split("\n"):
        if not line or line.startswith("a=candidate:"):
            continue
        if line.startswith("o="):
            parts = line.split(" ")
            line = " ".join(parts[:1] + ["<id>", "<ver>"] + parts[3:])
        m = _SDP_RANDOM.match(line)
        if m:
            line = m.group(1) + "<v>"
        line = re.sub(r"c=IN IP4 (\S+)", lambda r: "c=IN IP4 <exit>" if r.group(1) == exit_ip else "c=IN IP4 " + r.group(1), line)
        line = re.sub(r"(m=\w+) \d+", r"\1 <port>", line)
        out.append(line)
    return out


def webrtc_summary(result: Dict[str, Any], exit_ip: str) -> Dict[str, Any]:
    """What the page's probe returned (``candidates``, ``errors``, ``sdp``, the states), without a time and without anything random."""
    cands = sorted(candidate_summary(c, exit_ip) for c in result.get("candidates") or [])
    return {"candidates": cands, "candidate_types": sorted({c.split(" ")[3] for c in cands if len(c.split(" ")) > 3}),
            "errors": sorted("%s %s" % (e.get("code"), e.get("url")) for e in result.get("errors") or []),
            "sdp": sdp_summary(result.get("sdp") or "", exit_ip), "gathering": result.get("gathering")}


def message_kind(payload: bytes) -> str:
    """What a datagram carries, as a word: a STUN or TURN message by its type, QUIC by its first bits, anything else by its length class."""
    if len(payload) >= 20 and payload[4:8] == b"\x21\x12\xa4\x42":
        return "stun-0x%04x" % int.from_bytes(payload[:2], "big")
    if payload and payload[0] & 0x80:
        return "quic-long"
    if payload and payload[0] & 0x40:
        return "quic-short"
    return "other"


def stun_tid(payload: bytes) -> Optional[str]:
    """The transaction id of a STUN message (bytes 8 to 20 of the datagram) in hex: what ties a retransmission to the request it repeats. None when the datagram is not STUN
    (the same test as :func:`message_kind`: the magic cookie at bytes 4 to 8). Written in the evidence of the WebRTC walk, never compared."""
    if len(payload) >= 20 and payload[4:8] == b"\x21\x12\xa4\x42":
        return payload[8:20].hex()
    return None


def stun_attributes(data: bytes) -> Dict[int, bytes]:
    """The attributes of a STUN message by type (RFC 8489: type, length, value padded to 4 bytes); what a truncated tail says is left out."""
    out: Dict[int, bytes] = {}
    i = 20
    while i + 4 <= len(data):
        kind, length = int.from_bytes(data[i:i + 2], "big"), int.from_bytes(data[i + 2:i + 4], "big")
        if i + 4 + length > len(data):
            break
        out[kind] = data[i + 4:i + 4 + length]
        i += 4 + ((length + 3) & ~3)
    return out


def xor_address(value: Optional[bytes]) -> Optional[str]:
    """An XOR-MAPPED-ADDRESS or XOR-RELAYED-ADDRESS value (IPv4) as ``ip:port``; None for a missing value or another family."""
    if value is None or len(value) < 8 or value[1] != 1:
        return None
    cookie = b"\x21\x12\xa4\x42"
    port = int.from_bytes(value[2:4], "big") ^ int.from_bytes(cookie[:2], "big")
    return "%s:%d" % (socket.inet_ntoa(bytes(a ^ b for a, b in zip(value[4:8], cookie))), port)


def turn_request_record(data: bytes, source: Tuple[str, int], t: float) -> Optional[Dict[str, Any]]:
    """What the lab's TURN server writes down of a datagram it receives (None when it is not STUN): when, from where, the transaction id, the message type, whether it carried
    USERNAME and MESSAGE-INTEGRITY, and (``responses``, filled by :func:`turn_response_record`) what the server answered."""
    if stun_tid(data) is None:
        return None
    attrs = stun_attributes(data)
    return {"t": t, "src": "%s:%d" % (source[0], source[1]), "tid": stun_tid(data), "type": "0x%04x" % int.from_bytes(data[:2], "big"),
            "username": 0x0006 in attrs, "integrity": 0x0008 in attrs, "responses": []}


def turn_response_record(data: bytes, destination: Tuple[str, int], t: float) -> Optional[Dict[str, Any]]:
    """What the lab's TURN server sends: when, the type (0x0113 an Allocate error, 0x0103 its success), the address it is sent to and, of a success, the XOR-RELAYED-ADDRESS and the
    XOR-MAPPED-ADDRESS it carries (of an error, its code)."""
    if stun_tid(data) is None:
        return None
    attrs = stun_attributes(data)
    error = attrs.get(0x0009)
    return {"t": t, "tid": stun_tid(data), "type": "0x%04x" % int.from_bytes(data[:2], "big"), "to": "%s:%d" % (destination[0], destination[1]),
            "relayed": xor_address(attrs.get(0x0016)), "mapped": xor_address(attrs.get(0x0020)),
            "error": (error[2] * 100 + error[3]) if error is not None and len(error) >= 4 else None}


class ExchangeSocket:
    """The UDP socket of the lab's TURN server with a record of what it receives and sends (``owner.exchanges``): the server's own code runs on it unchanged, and a failure of the record
    is never a failure of the server. ``owner.clock`` is the moment (the recorder's)."""

    def __init__(self, sock: socket.socket, owner: Any) -> None:
        self._sock, self._owner = sock, owner

    def recvfrom(self, size: int) -> Tuple[bytes, Any]:
        data, source = self._sock.recvfrom(size)
        try:
            record = turn_request_record(data, source, self._owner.clock())
            if record is not None:
                self._owner.exchanges.append(record)
        except Exception:  # noqa: BLE001 - evidence, never the road
            pass
        return data, source

    def sendto(self, data: bytes, address: Any) -> int:
        try:
            record = turn_response_record(data, address, self._owner.clock())
            if record is not None:
                for exchange in reversed(self._owner.exchanges):
                    if exchange.get("tid") == record["tid"]:
                        exchange["responses"].append(record)
                        break
                else:
                    self._owner.exchanges.append({"t": record["t"], "src": None, "tid": record["tid"], "type": None, "username": None, "integrity": None, "responses": [record]})
        except Exception:  # noqa: BLE001 - evidence, never the road
            pass
        return self._sock.sendto(data, address)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sock, name)


# ---------------------------------------------------------------------------------------------------------------------------------
#  The seeds, the transcript and its files
# ---------------------------------------------------------------------------------------------------------------------------------
def default_seeds() -> Tuple[int, ...]:
    """The seeds of the run: ci/persona-seeds.json key "candidate" when the file exists, else those of the A/B oracle of the page."""
    if SEEDS_FILE.is_file():
        data = json.loads(SEEDS_FILE.read_text(encoding="utf-8"))
        if "candidate" in data:
            return tuple(int(s) for s in data["candidate"])
    return FALLBACK_SEEDS


def parse_seeds(text: Optional[str]) -> Tuple[int, ...]:
    return tuple(int(s) for s in text.split(",") if s.strip()) if text else default_seeds()


def transcript_errors(seeds: Dict[str, Any]) -> List[str]:
    """The paths where the run itself failed: a phase that raised (``guarded``), a journey step that did not complete, a connection that broke. The ``extra`` of a journey step is
    what the PAGE answered (the sign-in step has an ``error`` field that holds the text "Wrong user or password.", on purpose): it is content, not a failure of the run."""
    return sorted(p for p in flatten(seeds) if p.endswith("/error") and "/extra/" not in p)


def write_json(path: Path, document: Any) -> None:
    Path(path).write_text(json.dumps(document, sort_keys=True, indent=1), encoding="utf-8", newline="\n")


def read_transcript(path: Path) -> Dict[str, Any]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("format") != FORMAT:
        raise SystemExit("%s is not an %s transcript: format %r" % (path, FORMAT, document.get("format")))
    return document


# ---------------------------------------------------------------------------------------------------------------------------------
#  The lab (needs the engine, the core and the servers of tests/: imported when a run starts, never by the comparison above)
# ---------------------------------------------------------------------------------------------------------------------------------
class Recorder:
    """The stage of the run, the events the lab's own servers write down (a CONNECT, a datagram, an HTTP request head), and the cut of the lists that other servers keep
    (the DoH's questions): each item belongs to the stage that was current when it arrived."""

    def __init__(self) -> None:
        self.phase = "launch"
        self.events: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._sources: Dict[str, list] = {}
        self._marks: List[Tuple[str, Dict[str, int]]] = []
        #: The moment (``t``, as an event's) at which each stage began: the evidence of the WebRTC walk says where its stages sit among the datagrams.
        self.phase_t: Dict[str, float] = {}

    def now(self) -> float:
        """The recorder's clock, the ``t`` of an event: seconds since the recorder started."""
        return round(time.monotonic() - self._t0, 3)

    def attach(self, name: str, items: list) -> None:
        self._sources[name] = items
        self._marks = [(p, dict(m, **{name: 0})) for p, m in self._marks] or [(self.phase, {name: 0})]

    def set_phase(self, name: str) -> None:
        with self._lock:
            self._marks.append((name, {k: len(v) for k, v in self._sources.items()}))
            self.phase = name
            self.phase_t[name] = self.now()

    def event(self, kind: str, **fields: Any) -> None:
        with self._lock:
            self.events.append(dict(fields, kind=kind, phase=self.phase, t=round(time.monotonic() - self._t0, 3)))

    def of(self, phase_prefix: str, kind: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [e for e in self.events if e["kind"] == kind and (e["phase"] == phase_prefix or e["phase"].startswith(phase_prefix + "."))]

    def slice(self, source: str, phase_prefix: str) -> list:
        """The items of an attached list that arrived in the stages named ``phase_prefix`` or under it."""
        with self._lock:
            marks = self._marks + [("<end>", {k: len(v) for k, v in self._sources.items()})]
            items = self._sources[source]
            out: list = []
            for (phase, start), (_next, end) in zip(marks, marks[1:]):
                if phase == phase_prefix or phase.startswith(phase_prefix + "."):
                    out += items[start.get(source, 0):end.get(source, len(items))]
            return out


class RawTee:
    """A TCP server in front of another: it hands every byte on, and writes down the HTTP/1.1 requests the client sends (the head as it was written)."""

    def __init__(self, rec: Recorder, upstream_port: int, role: str) -> None:
        from _wire_capture import Http1Parser

        self.rec, self.upstream, self.role, self._parser = rec, upstream_port, role, Http1Parser
        self._connections = itertools.count()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(64)
        self.port: int = self._listener.getsockname()[1]
        self._closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self._closed:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client, next(self._connections)), daemon=True).start()

    def _serve(self, client: socket.socket, connection: int) -> None:
        try:
            upstream = socket.create_connection(("127.0.0.1", self.upstream), timeout=WAIT_S)
        except OSError:
            client.close()
            return
        parser = self._parser()

        def pump(source: socket.socket, sink: socket.socket, tap: Optional[Any]) -> None:
            try:
                while True:
                    data = source.recv(65536)
                    if not data:
                        break
                    if tap is not None:
                        for request in tap.feed(data):
                            self.rec.event("http", role=self.role, method=request["method"], target=request["target"], headers=request["headers"],
                                           body_length=request["body_length"], connection=connection)
                    sink.sendall(data)
            except (OSError, ValueError):
                pass
            finally:
                for s in (source, sink):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        client.settimeout(None)
        upstream.settimeout(None)
        back = threading.Thread(target=pump, args=(upstream, client, None), daemon=True)
        back.start()
        pump(client, upstream, parser)
        back.join(WAIT_S)
        for s in (client, upstream):
            s.close()

    def close(self) -> None:
        self._closed = True
        try:
            self._listener.close()
        except OSError:
            pass


def host_address() -> str:
    """The address of this host that a relay and a server bind to (tests/_host_address.py), or an error that says why there is none."""
    import _host_address

    try:
        return _host_address.this_host_address()
    except BaseException as exc:  # noqa: BLE001 - pytest's skip is a BaseException; here a host with no route cannot run the lab
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise RuntimeError("this host has no default route: the lab needs the address of its relay (%s)" % exc) from None


def make_lab_classes() -> Tuple[type, type, type]:
    """The gateway of the lab, its TURN server and its page site, built on the servers of tests/ (the classes are made here so that importing this file loads none of them)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import _doh_servers as ds
    from _socks_servers import MEASURE_NUMBER, STUN_NAME, STUN_NUMBER, STUN_PORT, WHOAMI4, WHOAMI6, WEB_NAME
    from _turn_chain import TURN_IP, TURN_PORT, TurnServer, is_deallocation

    class LabGateway(ds.DohGateway):
        """A coherent SOCKS5 gateway with the DoH road. Past loopback it carries only what the lab names: the CONNECTs to the servers of the lab that stand at the
        address of this host (``allowed_tcp``), the datagrams to the HTTP/3 server (``udp_allowed``), the STUN of the lab and its TURN. Every CONNECT and every
        datagram, carried or not, is written down with the stage it belongs to. A CONNECT to the port of a server that has a tee goes to the tee."""

        def __init__(self, rec: Recorder, doh: Any, turn: Any, host: str, exit_ip: str) -> None:
            self.rec, self.turn = rec, turn
            self.reroute: Dict[int, int] = {}
            self.allowed_tcp: set = set()
            self.udp_allowed: set = set()
            #: The stages in which the lab looks who opened a connection (``Lab(attribute_in=...)``), and what it found: by the index of the control connection (what the DoH
            #: says carried a question), and by the index of the association (what a datagram came on).
            self.attribute_in: Tuple[str, ...] = ()
            self._openers: Dict[int, str] = {}
            self._association_openers: Dict[int, str] = {}
            self.opener_lookup: Callable[..., str] = self._look_up_opener
            super().__init__("lab", doh, udp="relay", tcp_exit=exit_ip, udp_exit=exit_ip, relay_host=host, exit_host=host)

        def _deliver(self, relay: socket.socket, to: Any, host: str, port: int, payload: bytes) -> None:
            """Every datagram the relay sends BACK to the browser is written down (``udp_back``: the address it claims in the SOCKS UDP header, where it is sent, from which relay
            socket, the STUN type and transaction id), then sent as it always was."""
            try:
                local = relay.getsockname()
                self.rec.event("udp_back", claimed="%s:%d" % (host, port), to="%s:%d" % (to[0], to[1]), relay="%s:%d" % (local[0], local[1]),
                               message=message_kind(payload), tid=stun_tid(payload))
            except Exception:  # noqa: BLE001 - evidence, never the road
                pass
            super()._deliver(relay, to, host, port, payload)

        def _attributing(self) -> bool:
            phase = self.rec.phase
            return any(phase == p or phase.startswith(p + ".") for p in self.attribute_in)

        def _look_up_opener(self, c: socket.socket) -> str:
            try:
                return classify_pid(pid_of_client(c.getpeername()[1], self.port))
            except OSError:
                return "unknown"

        def opener_of_connection(self, index: Optional[int]) -> Optional[str]:
            """Who opened the control connection ``index`` (None when the lab did not look at it)."""
            return self._openers.get(index) if index is not None else None

        def _note_opener(self, c: socket.socket) -> Optional[str]:
            if not self._attributing():
                return None
            opener = self.opener_lookup(c)
            index = getattr(self._local, "index", None)
            if index is not None:
                self._openers[index] = opener
            return opener

        def _associate(self, c: socket.socket) -> None:
            self._local.opener = self._note_opener(c)
            super()._associate(c)

        def _request(self, kind: str, detail: str) -> None:
            super()._request(kind, detail)
            if kind == "associate" and detail.startswith("association #") and getattr(self._local, "opener", None) is not None:
                self._association_openers[int(detail.split("#")[1])] = self._local.opener

        def _connect(self, c: socket.socket, host: str, port: int) -> None:
            self.rec.event("connect", target="%s:%d" % (host, port), opener=self._note_opener(c))
            special = (host, port) in ds.DOH_TARGETS or (host, port) in (WHOAMI4, WHOAMI6, (WEB_NAME, 80)) or host.lower() in self.whoami_names
            if special or not (ds.is_loopback(host) or (host, port) in self.allowed_tcp):
                super()._connect(c, host, port)
                return
            destination = ("127.0.0.1", self.reroute[port]) if port in self.reroute and ds.is_loopback(host) else (host, port)
            self.connects.append((host, port))
            try:
                upstream = socket.create_connection(destination, timeout=WAIT_S)
            except OSError:
                self._send(c, self._reply(5))
                return
            with self._live_lock:
                self._live.append(upstream)
            self._send(c, self._reply(0))
            try:
                ds.relay(c, upstream, 0.0)
            finally:
                upstream.close()

        def _route(self, host: str, dport: int, atyp: int, payload: bytes, out_port: int, answer: Any) -> Any:
            association = getattr(self._dgram, "index", None)
            # ``deallocation``: the datagram is a TURN deallocation by what it means (``_turn_chain.is_deallocation``, the reading the lab's TURN server uses): marked here, where the payload is.
            self.rec.event("udp", target="%s:%d" % (host, dport), message=message_kind(payload), tid=stun_tid(payload), deallocation=is_deallocation(payload),
                           opener=self._association_openers.get(association) if association is not None else None)
            if atyp == 1 and (host, dport) == (TURN_IP, TURN_PORT):
                return self.turn.address, (host, dport)
            if (host, dport) in ((STUN_NAME, STUN_PORT), (STUN_NUMBER, STUN_PORT), (MEASURE_NUMBER, STUN_PORT)):
                return super()._route(host, dport, atyp, payload, out_port, answer)
            if atyp == 1 and (host, dport) in self.udp_allowed:
                return (host, dport), None
            return None

    class HostTurn(TurnServer):
        """The TURN server of ``tests/_turn_chain.py``, standing at the address of this host: the relay of the gateway sends from that address, and Windows does not send
        from an address of the host to loopback."""

        def __init__(self, address: str) -> None:
            raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            raw.bind((address, 0))
            self.address = raw.getsockname()
            #: Every STUN datagram it received with the answers it sent (``turn_request_record``), by the clock the lab sets (``Lab``): evidence of the WebRTC walk, never compared.
            self.exchanges: list = []
            self.clock: Callable[[], float] = time.monotonic
            self.sock = ExchangeSocket(raw, self)
            self.seen: list = []
            self.allocations: list = []
            self.requests: list = []
            self.deallocations: list = []
            self.refused: list = []
            self._closed = False
            threading.Thread(target=self._serve, daemon=True).start()

    class PageSite:
        """The page of the WebRTC probe (with a canary image at the witness), the blank page, and the sentinel the idle conditions ask for."""

        def __init__(self, canary: str) -> None:
            outer = self
            self.canary = canary
            self.sentinels: List[str] = []

            class Handler(BaseHTTPRequestHandler):
                timeout = 30

                def log_message(self, *args: Any) -> None:
                    pass

                def do_GET(self) -> None:  # noqa: N802
                    path = self.path.split("?")[0]
                    if path.startswith("/s/"):
                        outer.sentinels.append(path)
                    if path.startswith("/s/"):
                        body, kind = b"ok", "text/plain"
                    elif path == "/rtc":
                        body, kind = ("<!doctype html><meta charset=utf-8><title>rtc</title><img src='%s' alt=''>" % outer.canary).encode(), "text/html; charset=utf-8"
                    else:
                        body, kind = b"<!doctype html><title>blank</title>", "text/html; charset=utf-8"
                    self.send_response(200)
                    self.send_header("Content-Type", kind)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)

            self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self._server.daemon_threads = True
            self.port: int = self._server.server_address[1]
            threading.Thread(target=self._server.serve_forever, daemon=True).start()

        def close(self) -> None:
            self._server.shutdown()
            self._server.server_close()

    return LabGateway, HostTurn, PageSite


JOURNEY_A, JOURNEY_O = "journey-a.test", "journey-o.test"
PAGE_NAME, H1_NAME, H2_NAME, H3_NAME = "page.lab.test", "h1.lab.test", "h2.lab.test", "h3.lab.test"
STUN_URL = "stun:198.51.100.5:19302"
TURN_URL = "turn:198.51.100.7:3478?transport=udp"
#: The TURN server of ``TURN_URL`` as the gateway names the target of a datagram (a test holds it equal to ``tests/_turn_chain.py``).
TURN_TARGET = "198.51.100.7:3478"
#: The user name the probe gives the TURN server (the lab's server takes any: the key is made from it and the password of ``tests/_turn_chain.py``, which the walk passes as the credential).
TURN_USERNAME = "lab"
#: The probe of the WebRTC page: one STUN and one TURN server by address (the relay answers both; the TURN's user name and credential are its arguments, never written here), a data channel, and the page waits for the end of the gather (the
#: timer is the safety net of a gather that never ends, not a measure of it).
RTC_JS = r"""async (args) => {
  const [stunUrl, turnUrl, username, credential] = args;
  const T = {start: performance.now(), offer: null, local: null, candidates: [], errors: [], gathered: null, closing: null, closed: null};
  const pc = new RTCPeerConnection({iceServers: [{urls: [stunUrl]}, {urls: [turnUrl], username, credential}]});
  pc.createDataChannel('');
  const cands = [], errors = [];
  pc.addEventListener('icecandidate', e => {
    if (e.candidate && e.candidate.candidate) cands.push(e.candidate.candidate);
    T.candidates.push({t: performance.now(), type: e.candidate ? (e.candidate.type || null) : 'end'});
  });
  pc.addEventListener('icecandidateerror', e => {
    errors.push({code: e.errorCode, url: e.url});
    T.errors.push({t: performance.now(), code: e.errorCode, url: e.url});
  });
  const offer = await pc.createOffer();
  T.offer = performance.now();
  await pc.setLocalDescription(offer);
  T.local = performance.now();
  await new Promise(resolve => {
    const timer = setTimeout(resolve, 30000);
    const done = () => { clearTimeout(timer); if (T.gathered === null) T.gathered = performance.now(); resolve(); };
    if (pc.iceGatheringState === 'complete') done();
    pc.addEventListener('icegatheringstatechange', () => { if (pc.iceGatheringState === 'complete') done(); });
  });
  const sdp = (pc.localDescription && pc.localDescription.sdp) || '';
  const gathering = pc.iceGatheringState;
  T.closing = performance.now();
  try { pc.close(); } catch (e) {}
  T.closed = performance.now();
  return {candidates: cands, errors, sdp, gathering, times: T};
}"""
FETCH_JS = r"""async () => {
  await (await fetch('/api?x=1', {cache: 'no-store'})).text();
  await (await fetch('/api', {method: 'POST', headers: {'content-type': 'application/json', 'x-lab': '1'}, body: JSON.stringify({a: 1})})).text();
  return 1;
}"""
NAV_JS = "() => performance.getEntriesByType('navigation')[0].nextHopProtocol"


def lab_prefs() -> Dict[str, Any]:
    """The lab's prefs on top of the core's declaration (the same as the DoH suite and the HTTP/3 suite declare): loopback goes through the proxy, the DoH is the fake
    one, the answers of the lab are addresses of this host (RFC 1918 on a runner), and a self-signed server may be spoken HTTP/3 to."""
    from _doh_servers import DOH_PROVIDER_PREFS

    return {"network.proxy.allow_hijacking_localhost": True, "network.proxy.no_proxies_on": "", **DOH_PROVIDER_PREFS,
            "media.peerconnection.ice.obfuscate_host_addresses": True, "network.trr.allow-rfc1918": True,
            "network.http.http3.disable_when_third_party_roots_found": False}


# ---------------------------------------------------------------------------------------------------------------------------------
#  Who opens a connection of the lab: the Python of the core and the wrapper (this process), or a process of the engine
# ---------------------------------------------------------------------------------------------------------------------------------
def browser_kind(cmd: List[str]) -> str:
    """What an engine process is, from its command line: ``parent``, the kind of a ``-contentproc`` (the last argument: ``socket``, ``tab``, ``rdd``, ``utility``...), or
    ``backgroundtask:<name>`` (the pingsender of the shutdown)."""
    if "--backgroundtask" in cmd or "-backgroundtask" in cmd:
        flag = "--backgroundtask" if "--backgroundtask" in cmd else "-backgroundtask"
        i = cmd.index(flag)
        return "backgroundtask:" + (cmd[i + 1] if i + 1 < len(cmd) else "?")
    if "-contentproc" in cmd:
        return cmd[-1].strip('"').lower() if cmd else "?"
    return "parent"


def classify_pid(pid: Optional[int], python_pid: Optional[int] = None, process: Optional[Callable[[int], Any]] = None) -> str:
    """``core`` for the Python process that runs the lab (it is the one that imports the core and the wrapper, and makes the core's own requests at launch), ``browser:<kind>`` for
    a process of the engine, ``other:<name>`` for anything else, ``unknown`` when the process was not found."""
    if pid is None:
        return "unknown"
    if pid == (os.getpid() if python_pid is None else python_pid):
        return "core"
    try:
        if process is None:
            import psutil

            process = psutil.Process
        proc = process(pid)
        name = os.path.splitext(proc.name().lower())[0]
        if name.startswith(("firefox", "invisible")):
            return "browser:" + browser_kind([str(a) for a in proc.cmdline()])
        return "other:" + name
    except Exception:  # noqa: BLE001 - a process that is gone, or one that may not be read
        return "unknown"


def pid_of_client(source_port: int, gateway_port: int, connections: Optional[Callable[[], list]] = None) -> Optional[int]:
    """The pid that owns the client end of a connection to the gateway: the TCP connection whose local port is ``source_port`` (what the gateway sees as the port of its client)
    and whose remote port is the gateway's. Linux and Windows both give it with ``psutil.net_connections``; it exists as long as the connection does, and the gateway looks at
    it while it is serving the handshake."""
    if connections is None:
        import psutil

        connections = lambda: psutil.net_connections("tcp")  # noqa: E731
    try:
        for c in connections():
            if c.laddr and c.raddr and c.laddr.port == source_port and c.raddr.port == gateway_port:
                return c.pid
    except Exception:  # noqa: BLE001 - a table that cannot be read says nothing
        return None
    return None


class PortCollision(RuntimeError):
    """Two servers of one lab got the same port number (on different addresses, which the system allows)."""


def open_lab(**options: Any) -> "Lab":
    """A :class:`Lab` none of whose servers shares a port number with another: a lab where two do is closed and made again (a port is normalised into the ROLE of its server, and a
    number two roles share would read as the one that registered last, in some runs and not in others: A/A run 44982522362, seed 4 on Linux, where the port of the origin O and
    that of the HTTP/3 server were the same number and 667 leaves read ``journey-o.test:h3`` in one run)."""
    for _ in range(MAX_LAB_TRIES):
        try:
            return Lab(**options)
        except PortCollision:
            continue
    raise RuntimeError("%d labs in a row had two servers on the same port number" % MAX_LAB_TRIES)


class Lab:
    """Everything the lab is, for ONE launch of the browser: the fake DoH, the gateway, the witness, the servers that write down what they are sent, and the
    recorder that says which stage of the run each thing belongs to. ``journey`` adds the P4 site behind its tees, ``wire`` the HTTPS and HTTP/3 servers."""

    def __init__(self, *, journey: bool = False, wire: bool = False, host: Optional[str] = None, attribute_in: Tuple[str, ...] = ("launch",)) -> None:
        from _doh_servers import FakeDoh
        from _lab_egress import LAB_EGRESS_IP
        from _witness import Witness

        gateway_class, turn_class, page_class = make_lab_classes()
        self.host = host or host_address()
        self.exit_ip = LAB_EGRESS_IP
        self.rec = Recorder()
        self.roles: Dict[int, str] = {}
        self.collisions: List[Tuple[int, str, str]] = []
        self.norm = Normalizer(self.roles)
        #: What the WebRTC walk measured about itself (``walk_webrtc``), for ``webrtc_evidence``: never a leaf.
        self.webrtc: Dict[str, Any] = {}
        self._closers: List[Callable[[], None]] = []
        self.h1 = self.h2 = self.h3 = None
        self.origin_a = self.origin_o = ""
        zone = {PAGE_NAME: "127.0.0.1"}
        if journey:
            zone.update({JOURNEY_A: "127.0.0.1", JOURNEY_O: "127.0.0.1"})
        if wire:
            zone.update({H1_NAME: self.host, H2_NAME: self.host, H3_NAME: self.host})
        self.doh = FakeDoh(zone)
        self._closers.append(self.doh.close)
        self.rec.attach("doh", self.doh.queries)
        self.rec.attach("doh_connections", self.doh.question_connections)
        self.witness = Witness(self.host)
        self._closers.append(self.witness.close)
        self._claim(self.witness.port, "witness")
        self.turn = turn_class(self.host)
        self.turn.clock = self.rec.now
        self._closers.append(self.turn.close)
        self.gateway = gateway_class(self.rec, self.doh, self.turn, self.host, self.exit_ip)
        self.gateway.attribute_in = attribute_in
        self._closers.append(self.gateway.close)
        self.site = page_class("http://%s:%d/c.png" % (self.host, self.witness.port))
        self._closers.append(self.site.close)
        self._tee(self.site.port, "page")
        if journey:
            self._start_journey()
        if wire:
            self._start_wire()
        if self.collisions:
            self.close()
            raise PortCollision(", ".join("%d: %s and %s" % c for c in self.collisions))

    def _claim(self, port: int, role: str) -> None:
        """Gives ``port`` the role of the server that holds it, and remembers a number that already belongs to another role."""
        if port in self.roles and self.roles[port] != role:
            self.collisions.append((port, self.roles[port], role))
        self.roles[port] = role

    def _tee(self, port: int, role: str) -> None:
        tee = RawTee(self.rec, port, role)
        self._closers.append(tee.close)
        self._claim(port, role)
        self.gateway.reroute[port] = tee.port

    def _start_journey(self) -> None:
        from fakeweb import journey_site

        self.origin_a, self.origin_o, stop = journey_site.start((0, 0), public_host=JOURNEY_A, other_host=JOURNEY_O)
        self._closers.append(stop)
        self._tee(int(self.origin_a.rsplit(":", 1)[1]), "A")
        self._tee(int(self.origin_o.rsplit(":", 1)[1]), "O")
        state = journey_site.STATE
        original = state.set_step

        def set_step(n: int) -> None:      # the walk reaches step n: everything that arrives from now on belongs to it
            self.rec.set_phase("journey.%02d" % n)
            original(n)

        state.set_step = set_step
        self._closers.append(lambda: setattr(state, "set_step", original))      # a lab that is gone leaves the walk's step counter as it found it

    def _start_wire(self) -> None:
        from _wire_capture import WireServer

        phase = lambda: self.rec.phase  # noqa: E731
        # The HTTP/1.1 server ends each connection after its answer (``Connection: close``), so every request after the first rides a connection the browser opens when it already has the session
        # ticket the first answer carried: whether a resumed connection happens no longer follows how fast the pool opened a second connection while the first was still busy (A/A run
        # 45025164169, Linux: a resumed h1 connection on one seed of one run and on none of the five seeds of the other).
        self.h1 = WireServer(self.host, alpn=("http/1.1",), phase=phase, dns_names=(H1_NAME,), close_after_response=True)
        self.h2 = WireServer(self.host, alpn=("h2", "http/1.1"), phase=phase, dns_names=(H2_NAME,))
        self.h3 = WireServer(self.host, alpn=("h2", "http/1.1"), h3=True, phase=phase, dns_names=(H3_NAME,))
        for server, role in ((self.h1, "h1"), (self.h2, "h2"), (self.h3, "h3")):
            self._closers.append(server.close)
            self._claim(server.port, role)
            self.gateway.allowed_tcp.add((self.host, server.port))
        self.gateway.udp_allowed.add((self.host, self.h3.port))

    def close(self) -> None:
        for closer in reversed(self._closers):
            try:
                closer()
            except Exception:  # noqa: BLE001 - a server that is already closed is not a finding
                pass

    def __enter__(self) -> "Lab":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return re.sub(r"\d+", "N", "%s: %s" % (type(exc).__name__, text.splitlines()[0] if text else ""))[:200]


def guarded(fn: Callable[[], Any]) -> Any:
    """What a phase returns; a phase that raises is ``{"error": ...}`` in the transcript (the A/A and the A/B say whether both sides raise), and the run goes on."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return {"error": _first_line(exc)}


def phase_data(lab: Lab, phase: str, skip_connects: Callable[[str], bool] = lambda target: False,
               skip_udp: Callable[[Dict[str, Any]], bool] = lambda event: False) -> Dict[str, Any]:
    """What the lab saw in a stage and the stages under it: the CONNECTs, the DoH questions, the datagrams, the HTTP requests of the tees. Where the lab looked who opened an entry
    (``Lab(attribute_in=...)``), each summary says so: ``openers``. ``skip_connects`` leaves out the CONNECTs it is true of, ``skip_udp`` the datagrams (events) it is true of."""
    queries = [(q.name, q.qtype, q.via) for q in lab.rec.slice("doh", phase)]
    gateway = getattr(lab, "gateway", None)
    attributing = bool(gateway is not None and gateway.attribute_in)
    asked_on = [c[2] for c in lab.rec.slice("doh_connections", phase)] if attributing else []
    connects = [e for e in lab.rec.of(phase, "connect") if not skip_connects(e["target"])]
    return {"connects": connect_summary([e["target"] for e in connects], lab.norm, [e.get("opener") for e in connects]),
            "dns": dns_summary(queries, [gateway.opener_of_connection(i) for i in asked_on] if attributing else None),
            "udp": udp_summary([e for e in lab.rec.of(phase, "udp") if not skip_udp(e)], lab.norm), "requests": request_summary(lab.rec.of(phase, "http"), lab.norm)}


def launch_data(lab: Lab) -> Dict[str, Any]:
    """The launch stage (everything before the first page of the walk): the CONNECTs and the DNS questions with the process that opened each, the datagrams and the requests.
    The CONNECTs to the DoH road are not in it: the engine opens one at start-up, and it lands in this stage or in the first step of the journey (A/A run 44960767715, Windows);
    they are counted with the journey's (:func:`journey_data`)."""
    import _doh_servers as ds

    doh = {"%s:%d" % target for target in ds.DOH_TARGETS}
    return phase_data(lab, "launch", skip_connects=lambda target: target in doh)


def step_data(lab: Lab, phase: str) -> Dict[str, Any]:
    """What the lab saw in ONE step of the journey: the HTTP requests of the tees and the datagrams. Not the CONNECTs and not the DNS questions: the lab cuts the journey into
    steps at the moment the driver moves on, and what the browser does on its own (a connection it opens or closes, a name it asks again when the 30 s of an answer are over)
    lands in the step that happens to be current, a different one in each run (A/A run 44946411675: the same CONNECT to ``journey-o.test`` in step 09 of one run and step 12
    of the other). Those two are summarised over the whole journey instead (:func:`journey_data`). A request is the page's own act, driven by an event, and stays in its step."""
    data = phase_data(lab, phase)
    return {"requests": data["requests"], "udp": data["udp"]}


def journey_data(lab: Lab) -> Dict[str, Any]:
    """The CONNECTs and the DNS questions of the whole journey (every stage ``journey.*``): the destinations in the order they were first reached and the names in the order they
    were first asked, which the engine orders (a name is resolved before its first connection), and how many connections each destination got (a count is the pool's, see
    ci/ab-network-volatile.json)."""
    queries = [(q.name, q.qtype, q.via) for q in lab.rec.slice("doh", "journey")]
    # The CONNECTs of the launch stage too: the connection to the DoH that the engine opens at start-up lands before the journey or in its first step (A/A run 44960767715,
    # Windows: one CONNECT in the launch stage of one run and none in the other).
    targets = [e["target"] for e in lab.rec.of("launch", "connect") + lab.rec.of("journey", "connect")]
    return {"connects": connect_summary(targets, lab.norm), "dns": dns_summary(queries)}


def wire_summary(server: Any, phase: str, norm: Normalizer) -> Dict[str, Any]:
    """The connections one capture server saw in a stage: the ClientHello, the HTTP/2 frames or the HTTP/1.1 heads, the QUIC parameters and HTTP/3 settings."""
    snapshot = server.snapshot()
    inside = lambda p: p == phase or p.startswith(phase + ".")  # noqa: E731
    connections = []
    for c in snapshot["connections"]:
        if not inside(c["phase"]):
            continue
        item: Dict[str, Any] = {"alpn": c["alpn"], "tls": c["tls"]}
        if "h2" in c:
            h2 = dict(c["h2"])
            h2["requests"] = [{k: v for k, v in r.items() if k not in ("answered", "written_t")} for r in h2["requests"]]
            item["h2"] = h2
        if "http1" in c:
            item["http1"] = [{"request_line": r["request_line"], "headers": r["headers"], "body_length": r["body_length"]} for r in c["http1"]]
        if "error" in c:
            item["error"] = re.sub(r"\d+", "N", c["error"])[:120]
        connections.append(item)
    quic = [{"alpn": q["alpn"], "transport_parameters": q["transport_parameters"], "h3_settings": q["h3_settings"],
             "requests": [{"pseudo_order": r["pseudo_order"], "header_names": r["header_names"], "headers": r["headers"]} for r in q["requests"]]}
            for q in snapshot["quic"] if inside(q["phase"])]
    return norm.walk({"connections": connections, "quic": quic})


#: What HTTP/2 and HTTP/3 each add to a request on their own (Firefox: ``te: trailers`` over HTTP/2, ``alt-used: <origin>`` over HTTP/3, before the referer).
TRANSPORT_HEADERS = ("te", "alt-used")


def _pseudo(headers: List[List[str]]) -> Tuple[Dict[str, str], List[List[str]]]:
    """The pseudo-headers of a request as a dict (their ORDER is the connection's, see :func:`fold_wire`) and the regular headers in the order they were written."""
    return ({n: v for n, v in headers if n.startswith(":")}, [[n, v] for n, v in headers if not n.startswith(":")])


def _without(value: Any, *names: str) -> Any:
    """``value`` (a JSON structure) without the keys ``names``, at any depth."""
    if isinstance(value, dict):
        return {k: _without(v, *names) for k, v in value.items() if k not in names}
    if isinstance(value, list):
        return [_without(v, *names) for v in value]
    return value


def _resumed(tls: Dict[str, Any]) -> bool:
    return "pre_shared_key" in (tls.get("extensions") or {})


def fold_wire(summary: Dict[str, Any], transport_race: bool = False) -> Dict[str, Any]:
    """What a capture server saw over a whole walk, as what the ENGINE decides and not as what the connection pool did.

    The engine decides what a connection says (the ClientHello, the ALPN, the HTTP/2 SETTINGS and the first WINDOW_UPDATE and PRIORITY frames, the QUIC transport parameters and
    HTTP/3 SETTINGS, the order of the pseudo-headers) and what a request says (its pseudo-headers, its headers in their order and with their values, its HTTP/2 priority, whether it
    ended the stream). The pool decides how many connections there are, which connection a request rides, which stream it gets, how many frames follow the first request, whether a
    connection is opened and never used (a pre-connect), and whether a resumed connection is opened beside the fresh one. A/A run 44960767715 (Linux: a second connection with no
    request, in the stage of the first visit in one run and of the second in the other, 212 leaves; Windows: the page's requests split 4 and 1 between HTTP/2 over TCP and HTTP/3 over
    QUIC in one run, 1 and 4 in the other, with the stream numbers, priorities and frame counts that follow, 280 leaves).

    So the summary of a role is: ``protocols``, the set of protocols that carried a request (h1, h2, h3), not how many requests each carried: an engine that stops using h3 where the
    published one did, or starts h1 where it did not, differs; ``connections``, the distinct fingerprints of the connections that carried at least one request, sorted;
    ``resumed_connections``, the same for the connections whose ClientHello resumes a session (a ``pre_shared_key``), with the number of tickets it offers left out; ``requests``,
    every request with how many times it was made, wherever it was made; ``errors``. Not kept: the number of connections, a connection that carried no request, the stream numbers,
    the frames after the first request. A connection whose ClientHello differs, or a request whose headers differ, is still a difference.

    ``transport_race`` is for the role whose page may be served over HTTP/2 or HTTP/3 (the Alt-Svc of the first answer makes the browser open QUIC, and which of the next requests
    are already on it is a race): the end of the stream and the priority of an HTTP/2 request are then kept for the requests of the page itself (``/?...``, which always opens the
    first connection) and not for the resources, which ride either transport; and the headers a transport adds on its own (``TRANSPORT_HEADERS``) are written once in the fingerprint of
    its connections (``transport_headers``) and not in each request, which is the same request over either (A/A run 44960767715, Windows: ``/wire.css`` with ``te: trailers`` over
    HTTP/2 in one run and with ``alt-used`` before the referer over HTTP/3 in the other)."""
    connections: Dict[str, Dict[str, Any]] = {}
    resumptions: Dict[str, Dict[str, Any]] = {}
    protocols: set = set()
    requests: collections.Counter = collections.Counter()
    shapes: Dict[str, Any] = {}
    errors = set()

    def keep(fingerprint: Dict[str, Any], pseudo_orders: set, carried: int, resumed: bool, protocol: str, marks: set = frozenset()) -> None:
        if not carried:
            return
        protocols.add(protocol)
        fingerprint = dict(fingerprint, pseudo_orders=sorted(list(o) for o in pseudo_orders))
        if transport_race:
            fingerprint["transport_headers"] = sorted(marks)
        if resumed:
            # the number of tickets a resumption offers is the pool's (which ticket was ready); what the ClientHello says around it is the engine's
            fingerprint["tls"] = _without(fingerprint["tls"], "identities")
        # the ECH GREASE AEAD is random per connection, fresh or resumed (ci/ab-network-volatile.json): it does not tell two fingerprints apart
        (resumptions if resumed else connections)[json.dumps(_without(fingerprint, "aead"), sort_keys=True)] = fingerprint

    def regular_headers(headers: List[List[str]], marks: set) -> List[List[str]]:
        """The regular headers of a request; in a race the ones a transport adds on its own (``te: trailers`` on HTTP/2, ``alt-used`` on HTTP/3) are the connection's, not the request's."""
        pseudo, regular = _pseudo(headers)
        if transport_race:
            marks.update("%s: %s" % (n, v) for n, v in regular if n in TRANSPORT_HEADERS)
            regular = [[n, v] for n, v in regular if n not in TRANSPORT_HEADERS]
        return regular

    def count(shape: Dict[str, Any]) -> None:
        key = json.dumps(shape, sort_keys=True)
        shapes[key] = shape
        requests[key] += 1

    for c in summary.get("connections") or []:
        if "error" in c:
            errors.add(c["error"])
        if "h2" in c:
            h2 = c["h2"]
            orders, marks = set(), set()
            priority = {p.get("stream"): {k: v for k, v in p.items() if k != "stream"} for p in h2.get("headers_priority") or []}
            for r in h2.get("requests") or []:
                pseudo, _regular = _pseudo(r["headers"])
                regular = regular_headers(r["headers"], marks)
                orders.add(tuple(r["pseudo_order"]))
                shape = dict(pseudo=dict(sorted(pseudo.items())), headers=regular)
                if not transport_race or pseudo.get(":path", "").startswith("/?"):
                    shape.update(end_stream=r.get("end_stream"), priority=priority.get(r.get("stream")))
                count(shape)
            frames = (h2.get("priority") or []) + (h2.get("priority_update") or [])
            fingerprint = {"transport": "tcp", "alpn": c["alpn"], "tls": c["tls"],
                           "h2": {"settings": h2.get("settings"), "frame_types_before_the_first_request": h2.get("frame_types_before_the_first_request"),
                                  "window_updates_before_the_first_request": h2.get("window_updates_before_the_first_request"),
                                  "settings_ack_sent_by_client": h2.get("settings_ack_sent_by_client"),
                                  "priority_frames": sorted(json.dumps(_without(f, "stream"), sort_keys=True) for f in frames)}}
            keep(fingerprint, orders, len(h2.get("requests") or []), _resumed(c["tls"]), "h2", marks)
        else:
            for r in c.get("http1") or []:
                count({"request_line": r["request_line"], "headers": r["headers"], "body_length": r["body_length"]})
            keep({"transport": "tcp", "alpn": c["alpn"], "tls": c["tls"]}, set(), len(c.get("http1") or []), _resumed(c["tls"]), "h1")
    for q in summary.get("quic") or []:
        orders, marks = set(), set()
        for r in q["requests"]:
            pseudo, _regular = _pseudo(r["headers"])
            regular = regular_headers(r["headers"], marks)
            orders.add(tuple(r["pseudo_order"]))
            count(dict(pseudo=dict(sorted(pseudo.items())), headers=regular))
        keep({"transport": "quic", "alpn": q["alpn"], "transport_parameters": q["transport_parameters"], "h3_settings": q["h3_settings"]}, orders, len(q["requests"]), False, "h3", marks)
    return {"protocols": sorted(protocols), "connections": [connections[k] for k in sorted(connections)],
            "resumed_connections": [resumptions[k] for k in sorted(resumptions)], "requests": [dict(shapes[k], count=requests[k]) for k in sorted(requests)], "errors": sorted(errors)}


class Trusting:
    """A browser whose every new context accepts the certificate of the lab's DoH (``ignore_https_errors``): the lookups of a context carry its user context, and a lookup
    of a context without the exception cannot reach the fake DoH (tests/test_doh_through_the_proxy_e2e.py says why). The journey makes its contexts with ``new_context()``."""

    def __init__(self, browser: Any) -> None:
        self._browser = browser

    def new_context(self, **options: Any) -> Any:
        return self._browser.new_context(**dict(options, ignore_https_errors=True))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._browser, name)


def launch(binary: str, seed: int, lab: Lab, patch: Any) -> Any:
    """The browser of a launch behind the gateway of ``lab``, as the realness suite launches it (the exit is declared, nothing asks the internet). The caller names the launch
    for the probe that sees how its parent ends (``crash.mark``: ``main``, ``idle-c1``, ``exit-pings``)."""
    from invisible_core import _geo
    from _lab_egress import LAB_EGRESS_IP
    from _session_end import EndingPlaywright
    from _udp_line import egress_by_proxy, measure_servers

    patch.setattr(_geo, "discover_egress_ip", egress_by_proxy(lab.gateway, direct=LAB_EGRESS_IP))
    return EndingPlaywright(seed=seed, headless=True, binary_path=binary, proxy=lab.gateway.proxy, udp_stun_servers=measure_servers(lab.gateway),
                            extra_prefs=lab_prefs())


# --- the phases of the main launch -----------------------------------------------------------------------------------------------
def walk_journey(browser: Any, lab: Lab) -> Dict[str, Any]:
    from fakeweb import profile_p4

    lab.rec.set_phase("journey.00")
    records = profile_p4.drive(Trusting(browser), lab.origin_a, lab.origin_o, 1, 15000)
    lab.rec.set_phase("journey.end")
    steps: Dict[str, Any] = {}
    stage = {"%02d" % r["n"]: r for r in records}
    for key in ["00"] + sorted(stage) + ["end"]:
        step: Dict[str, Any] = step_data(lab, "journey.%s" % key)
        record = stage.get(key)
        if record is not None:
            step.update(name=record["name"], ok=record["ok"], idle=record["idle"], url=record["url"], title=record.get("title"), marker=record.get("marker"),
                        cookies=record.get("cookies"), history_length=record.get("history_length"), pages=record.get("pages"),
                        extra=lab.norm.walk(record.get("extra") or {}))
            if not record["ok"]:
                step["error"] = re.sub(r"\d+", "N", record["error"] or "")[:200]
        steps[key] = step
    return dict(journey_data(lab), steps=steps)


#: How a visit and a walk are named in the error of a visit that cannot be left. In words, because ``guarded`` writes the first line of an error with every digit as ``N`` (a run's own numbers
#: must not tell two runs apart): "v1" and "v2", "h1" and "h2" would come out the same.
ORDINALS = ("first", "second", "third", "fourth", "fifth")
WALK_NAMES = {"h1": "HTTP one", "h2": "HTTP two", "h3": "HTTP three"}


def visit_name(which: str, visit: int) -> str:
    return "the %s visit of the %s walk" % (ORDINALS[visit - 1], WALK_NAMES[which])


class VisitNotSettled(Exception):
    """A visit of the HTTPS or HTTP/3 walk reached its guard before it could be left (``settle_visit``). The first line names the visit and the wait that did not end, and nothing
    that changes from run to run, so that two runs that stop at the same place say the same words."""


def settle_visit(page: Any, server: Any, visit: str, pump: Callable[[], Any], guard_ms: int = LEAVE_GUARD_MS) -> None:
    """The one point from which a visit of the HTTPS and HTTP/3 walks is left: before the next ``goto``, the snapshot of the wire and the close of the context.

    (1) The favicon of the page's ORIGIN (``server``: one server is one origin) has been asked for AND served in full by the lab in this launch (``server.answered_at``: the request
    arrived and its answer was written to the last byte). A second visit of the same origin finds that already true and waits for nothing: the browser asks once per origin of a launch.
    (2) THEN the page reaches ``networkidle``: no request for 500 ms after the last answer, so after the favicon's too.
    Each wait has the guard; one that reaches it raises :class:`VisitNotSettled`, which says the visit (``visit_name``) and which of the two is missing (the walk's ``guarded`` makes it an ``error`` leaf
    of the transcript, red), and the walk does not go on in silence.

    Why. A/A run 45189518824 (e2e, Linux): ``seeds/0/h3/wire/requests/*`` red, the second visit (``?v=2``) of the HTTP/3 page asked for ``/favicon.ico`` in the first run and not in the
    second. 14 transcripts of the day (runs 45118341246, 45121964904 Linux and Windows, 45153612447 Linux and Windows, 45176061220, 45189518824): Firefox asks for the favicon of an
    origin once per launch; in the journey only on the FIRST page of each origin (steps 01 and 10: 140 launches of 140 asked at both), on no page of the other steps (0 of 980) and on
    the last page of the walk (0 of 70). In ``walk_h3`` and ``walk_https`` (h1, h2) ``?v=1`` always asks; ``?v=2``, same origin, did not in 59 launches of 60 over HTTP/3 and in 60 of 60 over h1
    and h2. The one that did ask was the second one: NOT PROVED, the hypothesis is a race: the ``goto`` of ``?v=2`` leaves before the browser has recorded the result of the favicon
    of ``?v=1``, and a favicon that is still in flight is asked for again. If a red falls again with this wait in place the measure to make is, from ``wire_evidence`` (``h3`` and
    ``https``: ``t`` the arrival and ``written`` the end of the answer, seconds from the start of each server), the moment the answer to the favicon of ``?v=1`` was written against the
    moment the ``goto`` of ``?v=2`` began (the ``t`` of its first request): written before it = the hypothesis is wrong and the cause is elsewhere."""
    from _waits import wait_until

    guard_s = guard_ms / 1000
    if server.answered_at(FAVICON) is None and not wait_until(lambda: server.answered_at(FAVICON) is not None, guard_s, pump):
        raise VisitNotSettled("%s: the favicon of its origin was not served in full by the lab within %g s (guard)" % (visit, guard_s))
    try:
        page.wait_for_load_state("networkidle", timeout=guard_ms)
    except Exception as exc:  # noqa: BLE001 - the client's own timeout, or a page that is gone: both mean the visit cannot be left
        raise VisitNotSettled("%s: networkidle was not reached within %g s (guard): %s" % (visit, guard_s, _first_line(exc))) from exc


def walk_https(browser: Any, lab: Lab, which: str, server: Any, name: str, guard_ms: int = LEAVE_GUARD_MS) -> Dict[str, Any]:
    context = browser.new_context(ignore_https_errors=True)
    try:
        page = context.new_page()
        pump = lambda: page.wait_for_timeout(100)  # noqa: E731
        out: Dict[str, Any] = {}
        for visit in (1, 2):
            phase = "https.%s.v%d" % (which, visit)
            lab.rec.set_phase(phase)
            page.goto("https://%s:%d/?v=%d" % (name, server.port, visit), wait_until="load", timeout=60000)
            page.evaluate(FETCH_JS)
            settle_visit(page, server, visit_name(which, visit), pump, guard_ms)      # before the next visit enters its stage: the favicon of this one is this stage's
            out["v%d" % visit] = {"dns": phase_data(lab, phase)["dns"]}
        out["wire"] = fold_wire(wire_summary(server, "https.%s" % which, lab.norm))     # the whole walk, read once it is over: see fold_wire
        return out
    finally:
        context.close()


def walk_h3(browser: Any, lab: Lab, guard_ms: int = LEAVE_GUARD_MS) -> Dict[str, Any]:
    from _waits import wait_until

    context = browser.new_context(ignore_https_errors=True)
    try:
        page = context.new_page()
        pump = lambda: page.wait_for_timeout(100)  # noqa: E731
        out: Dict[str, Any] = {"visits": [], "quic_seen": False}
        for visit in range(1, H3_VISITS + 1):
            phase = "h3.v%d" % visit
            lab.rec.set_phase(phase)
            page.goto("https://%s:%d/?v=%d" % (H3_NAME, lab.h3.port, visit), wait_until="load", timeout=60000)
            if not lab.h3.quic:      # the Alt-Svc of the answer makes the browser open QUIC: wait for the server to see it, with a deadline that is a safety net
                wait_until(lambda: bool(lab.h3.quic), WAIT_S, pump)
            out["quic_seen"] = bool(lab.h3.quic)
            protocol = page.evaluate(NAV_JS)
            settle_visit(page, lab.h3, visit_name("h3", visit), pump, guard_ms)      # the favicon rides h2 or QUIC: the server reads it on both
            out["visits"].append(protocol)
            out["v%d" % visit] = {"dns": phase_data(lab, phase)["dns"]}
            if protocol == "h3" or not out["quic_seen"]:
                break
        out["wire"] = fold_wire(wire_summary(lab.h3, "h3", lab.norm), transport_race=True)
        # The datagrams of the whole walk, not of each visit (the packets a visit leaves in flight arrive in the next visit's stage), and not their NUMBER: how many datagrams
        # QUIC sends follows its acknowledgements and timers. A/A run 44946411675, Linux: 12+8 and 13+7; run 44960767715: 23 and 22 on Linux, 16 and 25 on Windows.
        udp = phase_data(lab, "h3")["udp"]
        udp.pop("counts")
        out["udp"] = udp
        return out
    finally:
        context.close()


def walk_webrtc(browser: Any, lab: Lab) -> Dict[str, Any]:
    from _turn_chain import PASSWORD as TURN_PASSWORD      # the one the lab's TURN server signs its answers with: a browser with another finds every success invalid and makes no relay

    lab.rec.set_phase("webrtc")
    context = browser.new_context(ignore_https_errors=True)
    try:
        page = context.new_page()
        page.goto("http://%s:%d/rtc" % (PAGE_NAME, lab.site.port), wait_until="load", timeout=60000)
        lab.webrtc["evaluate"] = {"called": lab.rec.now()}
        result = page.evaluate(RTC_JS, [STUN_URL, TURN_URL, TURN_USERNAME, TURN_PASSWORD])
        lab.webrtc["evaluate"]["returned"] = lab.rec.now()
    finally:
        context.close()
    lab.rec.set_phase("webrtc.end")
    # What the page measured of itself goes to the evidence and to nothing that is compared: ``webrtc_summary`` reads the fields it always read.
    lab.webrtc["page"] = result.pop("times", None)
    # The TURN deallocation (a Refresh with LIFETIME 0) is one best-effort send at teardown, so it is evidence (``webrtc_evidence``) and no leaf. Source: dom/media/webrtc/transport/third_party/
    # nICEr/src/stun/turn_client_ctx.cpp:693-730, ``nr_turn_client_deallocate``: ONE request, no retransmission, sent while the TURN context is destroyed. It reaches the lab in some walks and
    # not in others for the same engine, so it is out of the datagrams counted for the TURN address (told by the mark the gateway wrote on it from its payload, not by when it came) and out
    # of the messages (told by the transaction id the lab's TURN server wrote for it, recorded before the request itself).
    ended = {tid.hex() for tid in lab.turn.deallocation_tids()}
    data = phase_data(lab, "webrtc", skip_udp=lambda event: event["target"] == TURN_TARGET and bool(event.get("deallocation")))
    lab.webrtc["counted"] = len(lab.rec.of("webrtc", "udp"))
    out = webrtc_summary(result, lab.exit_ip)
    marked = [e for e in lab.rec.of("webrtc", "udp") if e["target"] == TURN_TARGET and e.get("deallocation")]
    out.update(connects=data["connects"], dns=data["dns"], udp=data["udp"],
               turn={"messages": sorted({"0x%04x" % t for _when, tid, t, _credentials in lab.turn.requests if tid.hex() not in ended}), "allocated": bool(lab.turn.allocations),
                     "refused": len(lab.turn.refused), "deallocation": deallocation_facts(lab.turn, marked, closing_began(lab.webrtc["evaluate"]["called"], lab.webrtc["page"]))})
    return lab.norm.walk(out)


def closing_began(called: float, page: Optional[Dict[str, Any]]) -> Optional[float]:
    """The moment, on the recorder's clock, from which the page was closing its connection: the recorder's time at which the walk called the page (``evaluate.called``) plus the time the page
    measured from its start to its ``closing`` (``performance.now()`` in milliseconds, of the page's own clock: ``start`` and ``closing`` are both read there, so their difference is a length
    of time and needs no common origin). The script starts at or after the call, so this is never later than the real moment: a deallocation before it was surely sent before the close.
    None when the page did not return its times."""
    if not isinstance(page, dict) or page.get("start") is None or page.get("closing") is None:
        return None
    return called + (page["closing"] - page["start"]) / 1000.0


def deallocation_facts(turn: Any, marked: List[Dict[str, Any]], began_closing: Optional[float]) -> Dict[str, int]:
    """What the lab heard of deallocations that a walk must not have: ``repeated`` allocations that were ended by more than one transaction, ``unallocated`` deallocations from an address
    that has no allocation (both from the lab's TURN server), and ``early`` deallocation transactions that the gateway carried (``marked``, its events of them) before the page began closing
    (``began_closing``, :func:`closing_began`; unknown: none is early). All are 0 in a healthy walk; ``turn_deallocation_problems`` says it when they are not, and they are never compared."""
    ends: collections.Counter = collections.Counter()
    unallocated = 0
    for _ip, _port, _tid, _t, allocation in turn.deallocation_transactions():
        if allocation is None:
            unallocated += 1
        else:
            ends[allocation] += 1
    first: Dict[Any, float] = {}
    for event in marked:
        first.setdefault(event.get("tid"), event["t"])
    early = sum(1 for t in first.values() if began_closing is not None and t < began_closing)
    return {"repeated": sum(1 for n in ends.values() if n > 1), "unallocated": unallocated, "early": early}


def webrtc_evidence(lab: Lab) -> Dict[str, Any]:
    """The WebRTC walk of a seed as it happened, outside the leaves that are compared: every datagram the gateway routed in its stages (when, to whom, the message, the STUN transaction id,
    its stage, and whether it was already there when the summary of the walk was taken: ``counted``), the moments of the stages and of the page's call on the recorder's clock, and the
    page's own ``performance.now()`` at each step of the gather (``page``: the offer, each candidate with its type, each error with its code and url, the end of the gather, the close).
    It exists to tell two readings of a run that differs from itself apart: a close that cuts the retransmissions of the TURN server short at a variable point, and a retransmission of the
    first request whose answer came late."""
    info = getattr(lab, "webrtc", None) or {}
    began = lab.rec.phase_t.get("webrtc")
    counted = info.get("counted")
    datagrams = [{"t": e["t"], "phase": e["phase"], "target": e["target"], "message": e["message"], "tid": e.get("tid"), "counted": counted is None or i < counted}
                 for i, e in enumerate(lab.rec.of("webrtc", "udp"))]
    # The lab's TURN server: each request with what it carried and what it answered; the relay's datagrams back to the browser: the address each claims and the 5-tuple it goes to.
    exchanges = [e for e in getattr(getattr(lab, "turn", None), "exchanges", None) or [] if began is None or e["t"] >= began]
    returned = [{"t": e["t"], "phase": e["phase"], "claimed": e["claimed"], "to": e["to"], "relay": e["relay"], "message": e["message"], "tid": e.get("tid")} for e in lab.rec.of("webrtc", "udp_back")]
    # The deallocation (a Refresh with LIFETIME 0, sent once at teardown): whether the lab's TURN server heard it, and for each transaction when (the recorder's clock, from the exchange of
    # the same transaction id) and which. It is no leaf: the walk leaves it out of what it compares.
    heard = getattr(getattr(lab, "turn", None), "deallocation_transactions", None)
    when = {e["tid"]: e["t"] for e in reversed(exchanges)}
    records = [{"t": when.get(tid.hex()), "tid": tid.hex(), "src": "%s:%d" % (ip, port), "allocation": allocation} for ip, port, tid, _t, allocation in (heard() if heard else [])]
    return {"phase_t": {name: lab.rec.phase_t.get(name) for name in ("webrtc", "webrtc.end")}, "evaluate": info.get("evaluate"), "page": info.get("page"), "datagrams": datagrams,
            "turn": exchanges, "returned": returned, "send_errors": list(getattr(getattr(lab, "gateway", None), "send_errors", None) or []),
            "deallocation": {"seen": bool(records), "records": records}}


def direct_report(lab: Lab, watcher: Any, trap: Any) -> Dict[str, Any]:
    """Everything that reached the lab or the network around the proxy. Each list is empty on a browser that keeps its word; a watcher that never saw the
    browser is a list with that fact in it (a sink that sees nothing proves nothing)."""
    heard = lab.witness.heard()
    ports_kept = {lab.h3.port if lab.h3 else None, lab.witness.port}
    seen = []
    for c in watcher.direct:
        host, _sep, port = c.remote.rpartition(":")
        if c.protocol == "udp" and host == lab.host and int(port) not in ports_kept:
            continue          # the datagrams to the relay of the gateway are the road, not a leak
        seen.append(lab.norm.text("%s %s -> %s" % (c.protocol, os.path.splitext(c.process)[0].lower(), c.remote)))
    return {
        "witness_tcp": sorted({"tcp from " + ip for ip, _p in heard["tcp"]}), "witness_udp": sorted({"udp from " + ip for ip, _p, _n in heard["udp"]}),
        "watcher": sorted(set(seen)), "doh_direct": sorted({q.name for q in lab.doh.queries if q.via == "direct"}),
        "resolver_trap": sorted(trap.names()) if trap is not None else [],
        "blind": [] if watcher.watched_pids else ["the watcher never saw a browser process: it saw %s" % watcher.process_names],
    }


def start_trap() -> Any:
    """The resolver trap where it can run (Linux, root); None elsewhere, which the transcript says (``lab/resolver_trap_ran``)."""
    from _leaks import ResolverTrap

    if os.environ.get("AB_NETWORK_TRAP", "1") == "0" or not ResolverTrap.available:
        return None
    trap = ResolverTrap()
    try:
        trap.__enter__()
    except OSError:
        return None
    return trap


def run_main(binary: str, seed: int, patch: Any) -> Dict[str, Any]:
    """The journey, the HTTPS pages, the HTTP/3 page and the WebRTC page of one seed, in one browser behind one lab."""
    from _leaks import Watcher

    out: Dict[str, Any] = {}
    with open_lab(journey=True, wire=True) as lab:
        trap = start_trap()
        watcher = Watcher(match=("firefox", Path(binary).stem.lower())).start()
        try:
            begin_launch(seed, "main")
            session = launch(binary, seed, lab, patch)
            with session as browser:
                out["journey"] = guarded(lambda: walk_journey(browser, lab))
                out.update(glean_ready(session, seed, "main"))     # the journey opened its own pages: Glean is born; the rest of the work follows, then the close
                out["https"] = {"h2": guarded(lambda: walk_https(browser, lab, "h2", lab.h2, H2_NAME)),
                                "h1": guarded(lambda: walk_https(browser, lab, "h1", lab.h1, H1_NAME))}
                out["h3"] = guarded(lambda: walk_h3(browser, lab))
                out["webrtc"] = guarded(lambda: walk_webrtc(browser, lab))
                save_datareporting(session, seed, "main")
        finally:
            watcher.stop()
            if trap is not None:
                trap.__exit__(None, None, None)
        out["lab"] = {"direct": direct_report(lab, watcher, trap), "resolver_trap_ran": trap is not None,
                      "launch": lab.norm.walk(launch_data(lab))}
        EVIDENCE[str(seed)] = guarded(lambda: wire_evidence(lab))
    return out


#: What each seed of this run saw, request by request, kept OUTSIDE the leaves that are compared (``transcript["evidence"]``, like ``exits``): the transcript folds a request into a count
#: and drops the connection it rode and the moment it arrived, which is what a run that differs from itself needs to be explained (A/A run 45025164169, Windows: a request of step 15
#: counted twice in one run and once in the other, and nothing in the transcript said which page sent it or when).
EVIDENCE: Dict[str, Any] = {}


def wire_evidence(lab: Lab) -> Dict[str, Any]:
    """The requests the tees of the journey saw (when, on which connection of the tee, in which stage, the line and the referer) and the connections of the HTTPS servers (when, in which
    stage, whether the ClientHello resumes a session, and for HTTP/1.1 the requests it carried with their moments). Times are seconds since the lab started."""
    journey = []
    for e in lab.rec.of("journey", "http"):
        referer = next((v for n, v in e["headers"] if n.lower() == "referer"), None)
        journey.append({"t": e["t"], "connection": e["connection"], "phase": e["phase"], "request": "%s %s%s" % (e["method"], e["role"], lab.norm.text(e["target"])),
                        "referer": lab.norm.text(referer) if referer else None})
    https: Dict[str, Any] = {}
    for role, server in (("h1", lab.h1), ("h2", lab.h2)):
        if server is None:
            continue
        connections = []
        for c in server.snapshot()["connections"]:
            item = {"id": c["id"], "t": c.get("t"), "phase": c["phase"], "alpn": c["alpn"], "resumed": _resumed(c["tls"] or {})}
            if "http1" in c:
                item["requests"] = [{"t": r.get("t"), "written": r.get("written_t"), "request": r["request_line"]} for r in c["http1"]]
            if "h2" in c:
                item["requests"] = len(c["h2"].get("requests") or [])
            connections.append(item)
        https[role] = connections
    # The requests of the HTTP/3 site over QUIC, with the moment each arrived and the moment its answer was written to the last datagram (``written``: ``settle_visit`` says what it
    # is for); both seconds since the HTTP/3 server started, as the ``t`` of an HTTP/1.1 request is since its own.
    h3 = []
    if lab.h3 is not None:
        for q in lab.h3.snapshot()["quic"]:
            h3.append({"phase": q["phase"], "requests": [{"t": r.get("t"), "written": r.get("written_t"),
                                                         "request": "%s %s" % (dict(r["headers"]).get(":method"), lab.norm.text(dict(r["headers"]).get(":path", "")))} for r in q["requests"]]})
    return {"journey": journey, "https": https, "h3": h3, "webrtc": webrtc_evidence(lab)}


def sentinel_signature(lab: Lab) -> Dict[str, Any]:
    """What the lab has seen that is not the sentinel: the destinations, the names, the datagram targets."""
    sentinel = lambda t: lab.norm.text(t).endswith(":page")  # noqa: E731
    return {"connects": sorted({lab.norm.text(e["target"]) for e in lab.rec.events if e["kind"] == "connect" and not sentinel(e["target"])}),
            "dns": sorted({q.name for q in lab.doh.queries if q.name != PAGE_NAME}),
            "udp": sorted({lab.norm.text(e["target"]) for e in lab.rec.events if e["kind"] == "udp"})}


def run_idle(binary: str, seed: int, condition: str, patch: Any) -> Dict[str, Any]:
    """A launch with no action, ended at an event the lab drives (see IDLE in the docstring of this file): ``c1`` at the first round trip of a sentinel, ``c2`` when two
    rounds in a row leave what is not the sentinel unchanged."""
    out: Dict[str, Any] = {}
    with open_lab(attribute_in=("launch", "idle", "sentinel")) as lab:
        begin_launch(seed, "idle-" + condition)
        session = launch(binary, seed, lab, patch)
        with session as browser:
            out["launch"] = lab.norm.walk(launch_data(lab))      # who asks what while the browser starts: the STUN Binding at launch is the core's measurement, not the browser's
            lab.rec.set_phase("idle")
            page = browser.new_context(ignore_https_errors=True).new_page()
            out.update(glean_ready(session, seed, "idle-" + condition))     # Glean is born when a window opens: from here to the close there is the work of the launch
            lab.rec.set_phase("sentinel.0")
            page.goto("http://%s:%d/s/0" % (PAGE_NAME, lab.site.port), wait_until="load", timeout=60000)
            rounds, converged, previous = 0, False, sentinel_signature(lab)
            if condition == "c2":
                for k in range(1, MAX_ROUNDS + 1):
                    lab.rec.set_phase("sentinel.%d" % k)
                    page.evaluate("(u) => fetch(u, {mode: 'no-cors', cache: 'no-store'}).then((r) => r.type)", "http://%s:%d/s/%d" % (PAGE_NAME, lab.site.port, k))
                    rounds = k
                    now = sentinel_signature(lab)
                    if now == previous:
                        converged = True
                        break
                    previous = now
            save_datareporting(session, seed, "idle-" + condition)
        out.update(destinations=lab.norm.walk(sentinel_signature(lab)))
        if condition == "c2":
            out.update(rounds=rounds, converged=converged)
    return out


def profile_of(token: Any, find: Optional[Callable[[Any], list]] = None) -> Optional[Path]:
    """The profile folder of a running session: the ``-profile`` argument of its parent process (the one that is not a ``-contentproc``)."""
    if find is None:
        from invisible_core.process import find_processes as find
    for proc in find(token):
        try:
            cmd = [str(a) for a in proc.cmdline()]
        except Exception:  # noqa: BLE001 - a process that is gone
            continue
        if "-contentproc" not in cmd and "-profile" in cmd and cmd.index("-profile") + 1 < len(cmd):
            return Path(cmd[cmd.index("-profile") + 1])
    return None


#: The label of the run in progress (``run``): the line that names a launch says which engine run it belongs to.
RUN_LABEL = ""


def begin_launch(seed: int, launch_name: str) -> None:
    """Names the launch about to be made for the probe (``crash.mark``) and writes the line that says which launch is alive, and since when: the test that runs this oracle reads the
    last one of these lines when the oracle does not end in time, and names the launch it was waiting for (run 45121964904, Windows: a firefox that did not exit held the pipe)."""
    crash.mark(seed=seed, launch=launch_name)
    print(crash.launch_line(seed, launch_name, RUN_LABEL))
    sys.stdout.flush()


def glean_ready(session: Any, seed: int, launch_name: str) -> Dict[str, Any]:
    """Waits (inside the ``with`` of the launch, after a page is open and before the work of the launch) until Glean has initialised, so the close that follows is the one of a
    browser that has finished starting: a close before that makes Glean start inside the shutdown, where its ``glean.init`` thread can die with the panic "No database found"
    and the parent exits with -11 (Mozilla's race, the same in the retail browser: run 45121964904, the engine 68, Linux, launch ``exit-pings``). Glean is born when a window opens,
    so without a page the condition never comes. The condition is read from the host, the engine and its prefs are untouched (``crash.wait_for_glean``). ``{}`` when it is ready;
    ``{"glean": {"error": ...}}`` when it is not, which ``transcript_errors`` sees and which is red: the launch goes on and is closed all the same (the exit of the ``with``), and what
    it saw is kept."""
    try:
        waited = crash.wait_for_glean(profile_of(session._end_token))
    except crash.GleanNotReady as exc:
        return {"glean": {"error": "GleanNotReady: %s" % exc}}
    print("glean ready after %.1f s (seed %d, launch %s)" % (waited, seed, launch_name))
    sys.stdout.flush()
    return {}


def save_datareporting(session: Any, seed: int, launch_name: str) -> None:
    """The last thing a launch does before its close: the list of ``<profile>/datareporting/`` goes to the evidence folder of the run (``crash.save_datareporting``), for the next time
    a launch of the run dies. It changes nothing about the launch and nothing of what is compared, and a failure of it is not one of the launch."""
    try:
        crash.save_datareporting(profile_of(session._end_token))
    except Exception:  # noqa: BLE001 - evidence, never a verdict
        pass


def outside_the_lab(lab: Lab, target: str) -> bool:
    """A CONNECT target that is neither the lab's own (loopback, the DoH, a server it runs, the egress endpoints) nor anything it serves: where an exit ping goes."""
    import _doh_servers as ds
    from _socks_servers import WEB_NAME, WHOAMI4, WHOAMI6

    host, _sep, port = target.rpartition(":")
    known = ((host, int(port)) in ds.DOH_TARGETS or (host, int(port)) in (WHOAMI4, WHOAMI6, (WEB_NAME, 80)) or (host, int(port)) in lab.gateway.allowed_tcp
             or host.lower() in lab.gateway.whoami_names)
    return not (ds.is_loopback(host) or known)


def exit_pings_summary(targets: List[str], norm: Normalizer, openers: Optional[List[Optional[str]]] = None) -> Dict[str, Any]:
    """What the shutdown of a launch with a page sent out of the lab: the CONNECTs towards names outside the lab, in the form the proxy was asked them (a name, or an address), how
    many of each, and in the order they were first asked."""
    return connect_summary(targets, norm, openers)


def run_exit_pings(binary: str, seed: int, patch: Any) -> Dict[str, Any]:
    """A launch behind the lab's proxy with no pref changed: it opens a context and a page, waits until Glean has initialised, navigates to the sentinel page of the idle launches
    until its ``load``, closes, and records until the tree has exited, the pingsender of the shutdown included (``EndingPlaywright`` returns when the reaper has). What went to the proxy towards a name outside the lab is the exit pings; the proxy
    refuses those CONNECTs itself (a SOCKS reply 4, host unreachable: nothing leaves the lab), and a connection from the browser or from its pingsender that did not go to the proxy
    is in ``direct``, which is red on either side.

    The navigation sits between the condition and the close on purpose: the condition says that the database has been written (its file exists), not that the init thread of Glean
    has finished, and that thread can still be half-way when the file appears: that is the window of the race. The page's load is real work of the launch after the condition, and
    the stage ``exit`` begins after it.

    It measures the exit pings of the close of a normal session (one page, Glean initialised). It does not measure the shutdown of a session with no window or one closed in its
    first seconds, where Glean is born inside the shutdown: that is the road of Mozilla's race (run 45121964904, the engine 68, Linux), and it is on the list for after the 69, with
    the sessions closed in the first seconds."""
    from _leaks import Watcher

    with open_lab(attribute_in=("exit",)) as lab:
        trap = start_trap()
        watcher = Watcher(match=("firefox", Path(binary).stem.lower())).start()
        try:
            begin_launch(seed, "exit-pings")
            session = launch(binary, seed, lab, patch)
            with session as browser:
                lab.rec.set_phase("exit.wait")
                page = browser.new_context(ignore_https_errors=True).new_page()
                glean = glean_ready(session, seed, "exit-pings")
                lab.rec.set_phase("sentinel.0")        # not under ``exit``: what the navigation asks of the proxy is not an exit ping
                page.goto("http://%s:%d/s/0" % (PAGE_NAME, lab.site.port), wait_until="load", timeout=60000)
                save_datareporting(session, seed, "exit-pings")
                lab.rec.set_phase("exit")
            lab.rec.set_phase("exit.end")
        finally:
            watcher.stop()
            if trap is not None:
                trap.__exit__(None, None, None)
        pings = [e for e in lab.rec.of("exit", "connect") if outside_the_lab(lab, e["target"])]
        out = exit_pings_summary([e["target"] for e in pings], lab.norm, [e.get("opener") for e in pings])
        out.update(glean)
        out["direct"] = direct_report(lab, watcher, trap)
        return out


def walk_seed(binary: str, seed: int, with_idle: bool, patch: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    if with_idle:
        body["idle"] = {c: guarded(lambda c=c: run_idle(binary, seed, c, patch)) for c in ("c1", "c2")}
        body["idle"]["exit_pings"] = guarded(lambda: run_exit_pings(binary, seed, patch))
    body.update(guarded(lambda: run_main(binary, seed, patch)))
    return body


def run(binary: str, label: str, seeds: Tuple[int, ...], out: Path) -> int:
    """One engine, every seed: the transcript JSON. The idle launches run on the first seed only (what an idle browser asks is the engine's, not the persona's).

    A probe sees how every launch's parent ends (scripts/ci_crash_evidence.py): ``exits`` of the transcript, outside the leaves that are compared; a code other than 0 is red in
    ``compare_transcripts``, and what the launch left (the core through gdb, the build ids, the ``.extra`` and the minidump names; this oracle runs no strace) is saved in the folder
    ``AB_CRASH_DIR`` names (``run_engine`` gives it one beside the transcript)."""
    import pytest

    probe = crash.start_probe(binary, os.environ.get(crash.CRASH_DIR_ENV))
    patch = pytest.MonkeyPatch()
    work = Path(tempfile.mkdtemp(prefix="ab-network-run-"))
    try:
        import _lab_egress

        _lab_egress.install(patch, work)
        transcript: Dict[str, Any] = {"format": FORMAT, "label": label, "platform": sys.platform, "seeds": {}}
        EVIDENCE.clear()
        global RUN_LABEL
        RUN_LABEL = label
        for index, seed in enumerate(seeds):
            print("=== %s: seed %d" % (label, seed))
            sys.stdout.flush()
            transcript["seeds"][str(seed)] = walk_seed(binary, seed, index == 0, patch)
        crash.stop_probe()
        transcript["evidence"] = {"format": EVIDENCE_FORMAT, "seeds": dict(EVIDENCE)}
        transcript["exits"] = probe.document()
        probe.write()
        for line in crash.exit_problems(probe.launches, label, windows=sys.platform == "win32"):
            print("EXIT " + line)
        write_json(out, transcript)
        for path in transcript_errors(transcript["seeds"]):
            print("ERROR in the transcript at %s" % path)
        print("transcript written: %s (%d leaves)" % (out, len(flatten(transcript["seeds"]))))
        return 0
    finally:
        crash.stop_probe()
        patch.undo()
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------------------------------------------------------------
#  The A/A, the A/B, and the command line
# ---------------------------------------------------------------------------------------------------------------------------------
def judge(report: Dict[str, Any], control: bool) -> int:
    """The exit code of a report: 0 green, 1 red, 2 red as a control (an A/A that differs says the oracle is broken, not the candidate)."""
    if report["verdict"] == "green":
        return 0
    return 2 if control else 1


def engine_env(seal: Optional[str]) -> Dict[str, str]:
    """The environment of a process that runs one engine: its seal, no autofix, no pin check, no browser logging."""
    env = dict(os.environ, INVISIBLE_CORE_AUTOFIX="off", INVISIBLE_CORE_PIN="allow")
    if seal:
        env["INVISIBLE_SEAL_FILE"] = str(seal)
    for name in ("MOZ_LOG", "MOZ_LOG_FILE"):
        env.pop(name, None)
    return env


def run_engine(binary: str, seal: Optional[str], label: str, seeds: Tuple[int, ...], out: Path) -> int:
    """``run`` in a process of its own: the seal a process runs under is read at import, so each engine gets its own."""
    env = engine_env(seal)
    env[crash.CRASH_DIR_ENV] = str(Path(out).parent / (Path(out).stem + ".crash"))  # footprint-68.json -> transcript-68.crash/: what a launch that did not end with 0 left
    command = [sys.executable, "-u", str(Path(__file__).resolve()), "run", "--binary", binary, "--label", label, "--seeds", ",".join(map(str, seeds)), "--out", str(out)]
    print("=== run %s: %s (seal %s)" % (label, binary, seal or "the one this process has"))
    sys.stdout.flush()
    from ci_ab_oracle import own_cache_env

    try:
        with tempfile.TemporaryDirectory(prefix="ab-network-cache-") as cache:
            return subprocess.run(command, env=own_cache_env(env, cache), timeout=RUN_TIMEOUT_S).returncode
    except subprocess.TimeoutExpired:
        return 124


# ---------------------------------------------------------------------------------------------------------------------------------
#  The hunt: one walk of one seed, repeated, and the evidence of each repetition (never a verdict)
# ---------------------------------------------------------------------------------------------------------------------------------
def parse_hunt(text: str) -> Tuple[str, int, int]:
    """``webrtc:<seed>:<n>`` -> ``("webrtc", seed, n)``. Anything else is a usage error: the walk is named, the seed and the count are whole numbers, and n is at least 1."""
    parts = (text or "").strip().split(":")
    if len(parts) != 3 or parts[0] != "webrtc":
        raise ValueError("%s is %r: expected webrtc:<seed>:<n>" % (HUNT_ENV, text))
    try:
        seed, n = int(parts[1]), int(parts[2])
    except ValueError:
        raise ValueError("%s is %r: the seed and n are whole numbers" % (HUNT_ENV, text)) from None
    if n < 1:
        raise ValueError("%s is %r: n is at least 1" % (HUNT_ENV, text))
    return parts[0], seed, n


def hunt_counts(evidence: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """Datagrams to the TURN server in a repetition: ``counted`` as the summary of the walk (what the A/A compares) took them, ``all`` with the ones that came after."""
    turn = [d for d in (evidence or {}).get("datagrams") or [] if d.get("target") == TURN_TARGET]
    return {"counted": sum(1 for d in turn if d.get("counted")), "all": len(turn)}


def hunt_repetition(binary: str, label: str, i: int, seed: int, patch: Any, out_dir: Optional[Path] = None) -> Dict[str, Any]:
    """One repetition: a launch behind a lab as the main launch has it, the WebRTC walk only, the close, and what the lab saw of it. A failure is the ``error`` of the record."""
    record: Dict[str, Any] = {"format": HUNT_FORMAT, "engine": label, "i": i, "seed": seed, "error": None, "evidence": None}
    if out_dir is not None:
        # The engine logs to ``<out_dir>/<engine>-<i>-mozlog`` (its child processes to the same name with ``.child-N``): the launch inherits this process's environment.
        record["mozlog"] = "%s-%d-mozlog" % (label, i)
        patch.setenv("MOZ_LOG", HUNT_MOZ_LOG)
        patch.setenv("MOZ_LOG_FILE", str(Path(out_dir) / record["mozlog"]))
    try:
        with open_lab(journey=True, wire=True) as lab:
            begin_launch(seed, "hunt")
            session = launch(binary, seed, lab, patch)
            with session as browser:
                browser.new_context(ignore_https_errors=True).new_page()     # Glean is born when a window opens (``glean_ready``); the walk opens its own context
                record.update(glean_ready(session, seed, "hunt"))
                record["walk"] = guarded(lambda: walk_webrtc(browser, lab))
            record["evidence"] = webrtc_evidence(lab)                        # after the close: the datagrams the close leaves in flight are in it
    except Exception as exc:  # noqa: BLE001 - evidence, never a verdict
        record["error"] = _first_line(exc)
    finally:
        if out_dir is not None:
            patch.delenv("MOZ_LOG", raising=False)
            patch.delenv("MOZ_LOG_FILE", raising=False)
    record["counts"] = hunt_counts(record["evidence"])
    return record


def hunt_run(binary: str, label: str, seed: int, n: int, out_dir: Path) -> int:
    """``n`` repetitions of the WebRTC walk of ``seed`` with one engine, each written to ``<out_dir>/<label>-<i>.json``. Exits 0: it decides nothing."""
    import pytest

    patch = pytest.MonkeyPatch()
    work = Path(tempfile.mkdtemp(prefix="ab-network-hunt-"))
    try:
        import _lab_egress

        _lab_egress.install(patch, work)
        global RUN_LABEL
        RUN_LABEL = label
        out_dir.mkdir(parents=True, exist_ok=True)
        for i in range(1, n + 1):
            record = hunt_repetition(binary, label, i, seed, patch, out_dir)
            write_json(out_dir / ("%s-%d.json" % (label, i)), record)
            print("hunt %s %d/%d: %d datagrams to the TURN server (%d with the late ones)%s" % (
                label, i, n, record["counts"]["counted"], record["counts"]["all"], "" if record["error"] is None else ", ERROR " + record["error"]))
            sys.stdout.flush()
        return 0
    finally:
        patch.undo()
        shutil.rmtree(work, ignore_errors=True)


def hunt_summary(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The summary of the repetition files: per repetition the count of datagrams to the TURN server (``count_3478``: as the A/A counts them; ``count_3478_all``: with the late ones) and, per
    target, the transaction ids in the order they first appeared with the number of datagrams each carried (``[tid, n]``: a retransmission repeats its request's id); per engine, how often
    each count came."""
    repetitions = []
    distribution: Dict[str, collections.Counter] = {}
    for record in records:
        counts = hunt_counts(record.get("evidence"))
        groups: Dict[str, Dict[Any, int]] = {}
        for d in (record.get("evidence") or {}).get("datagrams") or []:
            tids = groups.setdefault(d["target"], {})
            tids[d.get("tid")] = tids.get(d.get("tid"), 0) + 1
        repetitions.append({"engine": record["engine"], "i": record["i"], "count_3478": counts["counted"], "count_3478_all": counts["all"],
                            "targets": {t: [[tid, n] for tid, n in groups[t].items()] for t in sorted(groups)}, "error": record.get("error")})
        distribution.setdefault(record["engine"], collections.Counter())[counts["counted"]] += 1
    return {"format": HUNT_SUMMARY_FORMAT, "target": TURN_TARGET, "repetitions": repetitions,
            "count_3478_by_engine": {engine: {str(k): v for k, v in sorted(c.items())} for engine, c in sorted(distribution.items())}}


def read_hunt_records(folder: Path) -> List[Dict[str, Any]]:
    """The repetition files of a folder, by engine and repetition."""
    records = []
    for path in sorted(Path(folder).glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("format") == HUNT_FORMAT:
            records.append(document)
    return sorted(records, key=lambda r: (r["engine"], r["i"]))


#: The safety net of a hunt's engine run, in seconds: a base and a share per repetition (a launch, Glean, the gather's own 30 s). Never a verdict.
HUNT_BASE_S = 600
HUNT_PER_REPETITION_S = 120


def hunt_engine(binary: str, seal: Optional[str], label: str, seed: int, n: int, out_dir: Path) -> int:
    """``hunt_run`` in a process of its own, as ``run_engine`` runs ``run``: each engine its own seal and its own cache."""
    env = engine_env(seal)
    command = [sys.executable, "-u", str(Path(__file__).resolve()), "hunt-run", "--binary", binary, "--label", label, "--seed", str(seed), "--n", str(n), "--out", str(out_dir)]
    print("=== hunt %s: %s (seal %s), %d repetitions of the WebRTC walk of seed %d" % (label, binary, seal or "the one this process has", n, seed))
    sys.stdout.flush()
    from ci_ab_oracle import own_cache_env

    try:
        with tempfile.TemporaryDirectory(prefix="ab-network-cache-") as cache:
            return subprocess.run(command, env=own_cache_env(env, cache), timeout=HUNT_BASE_S + n * HUNT_PER_REPETITION_S).returncode
    except subprocess.TimeoutExpired:
        return 124


def published_engine(args: argparse.Namespace, work: Path) -> Tuple[str, Optional[str]]:
    """The published engine of an ``ab``: the binary the caller gave, or the one the release holds, with its seal."""
    if args.published_binary:
        return str(args.published_binary), (str(args.published_seal) if args.published_seal else None)
    import ci_ab_oracle as oracle

    binary, seal = oracle.fetch_published(args.engine_tag, work)
    return binary, str(seal)


def cmd_hunt(args: argparse.Namespace, spec: str) -> int:
    """``ab`` with ``AB_NETWORK_HUNT=webrtc:<seed>:<n>``: the WebRTC walk of that seed, ``n`` times, for each engine of the A/A (the published engine twice), written to ``<out>/hunt``:
    ``<engine>-<i>.json`` per repetition and ``summary.json``. It decides nothing and exits 0 (2 only for a spec that does not read)."""
    try:
        _walk, seed, n = parse_hunt(spec)
    except ValueError as exc:
        print("FAIL " + str(exc))
        return 2
    out = Path(args.out) / "hunt"
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="ab-network-"))
    try:
        binary, seal = published_engine(args, work)
        for label in (args.engine_tag, args.engine_tag + "-again"):
            code = hunt_engine(binary, seal, label, seed, n, out)
            if code != 0:
                print("hunt %s ended with %s" % (label, code))
        summary = hunt_summary(read_hunt_records(out))
        write_json(out / "summary.json", summary)
        for r in summary["repetitions"]:
            print("hunt %s %d: %d to the TURN server (%d with the late ones); %s" % (r["engine"], r["i"], r["count_3478"], r["count_3478_all"], r["targets"].get(TURN_TARGET)))
        print("hunt summary written: %s" % (out / "summary.json"))
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def compare_files(a: Path, b: Path, out: Optional[Path], floor: int, volatile_path: Path, control: bool) -> Dict[str, Any]:
    ta, tb = read_transcript(a), read_transcript(b)
    report = compare_transcripts(ta, tb, load_volatile(volatile_path), floor, ta.get("label", "A"), tb.get("label", "B"))
    report["control"] = control
    if out is not None:
        write_json(out, report)
    print_report(report, control)
    return report


def cmd_ab(args: argparse.Namespace) -> int:
    spec = os.environ.get(HUNT_ENV)
    if spec:
        return cmd_hunt(args, spec)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    seeds = parse_seeds(args.seeds)
    if not args.a_a and not args.candidate:
        print("ab needs --candidate (and its seal) or --a-a")
        return 2
    if args.candidate and not args.candidate_seal and not args.a_a:
        print("--candidate-seal (or INVISIBLE_SEAL_FILE) is required: the candidate runs under its own seal")
        return 2
    work = Path(tempfile.mkdtemp(prefix="ab-network-"))
    try:
        binary, seal = published_engine(args, work)
        tag = args.engine_tag.rsplit("-", 1)[-1]
        first, again = out / ("transcript-%s.json" % tag), out / ("transcript-%s-again.json" % tag)
        problems = []
        for path, label in ((first, args.engine_tag), (again, args.engine_tag + "-again")):
            if run_engine(binary, seal, label, seeds, path) != 0 or not path.is_file():
                problems.append("the run %s did not produce a transcript" % label)
        if problems:
            print("\n".join("FAIL " + p for p in problems))
            return 2
        aa = compare_files(first, again, out / "diff-a-a.json", args.floor, args.volatile, control=True)
        proposal = propose_volatile(aa)
        if proposal:
            write_json(out / "volatile-proposal.json", {"format": VOLATILE_FORMAT + "-proposal", "patterns": proposal,
                                                         "note": "leaves that differed between two runs of one engine; each needs a reason before it enters ci/ab-network-volatile.json"})
        broken = aa["verdict"] != "green"
        if args.a_a or not args.candidate or (broken and not args.measure):
            return judge(aa, control=True)
        candidate = out / ("transcript-%s.json" % CANDIDATE)
        if run_engine(args.candidate, args.candidate_seal, CANDIDATE, seeds, candidate) != 0 or not candidate.is_file():
            print("FAIL the run of the candidate did not produce a transcript")
            return 2
        result = compare_files(first, candidate, out / "diff.json", args.floor, args.volatile, control=False)
        return 2 if broken else judge(result, control=False)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="one engine: the transcript")
    r.add_argument("--binary", required=True)
    r.add_argument("--label", default="engine")
    r.add_argument("--seeds", default=None)
    r.add_argument("--out", type=Path, required=True)
    h = sub.add_parser("hunt-run", help="one engine: the WebRTC walk of one seed, n times (what AB_NETWORK_HUNT makes `ab` run)")
    h.add_argument("--binary", required=True)
    h.add_argument("--label", required=True)
    h.add_argument("--seed", type=int, required=True)
    h.add_argument("--n", type=int, required=True)
    h.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("compare", help="two transcripts: the diff and the leaf count")
    c.add_argument("a", type=Path)
    c.add_argument("b", type=Path)
    c.add_argument("--out", type=Path, default=None)
    c.add_argument("--floor", type=int, default=0, help="the fewest leaves a comparison may hold (per operating system: the caller names it)")
    c.add_argument("--volatile", type=Path, default=VOLATILE_FILE)
    c.add_argument("--a-a", action="store_true", help="the two transcripts are of one engine: a difference is a broken control (exit 2)")
    j = sub.add_parser("judge", help="the exit code of a diff file")
    j.add_argument("diff", type=Path)
    j.add_argument("--a-a", action="store_true")
    a = sub.add_parser("ab", help="the A/A (the published engine twice), then, with a candidate, the A/B")
    a.add_argument("--out", type=Path, required=True)
    a.add_argument("--candidate", default=None, help="the candidate's firefox binary")
    a.add_argument("--candidate-seal", default=os.environ.get("INVISIBLE_SEAL_FILE"))
    a.add_argument("--published-binary", type=Path, default=None, help="use this binary as the published engine instead of fetching it")
    a.add_argument("--published-seal", type=Path, default=None)
    a.add_argument("--engine-tag", default=ENGINE_TAG)
    a.add_argument("--seeds", default=None)
    a.add_argument("--floor", type=int, default=0)
    a.add_argument("--volatile", type=Path, default=VOLATILE_FILE)
    a.add_argument("--a-a", action="store_true", help="only the A/A")
    a.add_argument("--measure", action="store_true", help="with a candidate: run the A/B even when the A/A is broken (the exit code is still 2)")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        return run(args.binary, args.label, parse_seeds(args.seeds), args.out)
    if args.cmd == "hunt-run":
        return hunt_run(args.binary, args.label, args.seed, args.n, args.out)
    if args.cmd == "compare":
        return judge(compare_files(args.a, args.b, args.out, args.floor, args.volatile, args.a_a), args.a_a)
    if args.cmd == "judge":
        return judge(json.loads(args.diff.read_text(encoding="utf-8")), args.a_a)
    return cmd_ab(args)


if __name__ == "__main__":
    sys.exit(main())
