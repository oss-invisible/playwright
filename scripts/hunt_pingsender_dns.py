"""HUNT ONLY, never merged: a watcher for the shutdown-ping sender (`firefox --backgroundtask pingsender ...`), run as root beside the
wrapper's own e2e lab (tests/test_nothing_leaves_but_the_proxy_e2e.py).

    python hunt_pingsender_dns.py watch OUT_JSONL PREFS_DIR

Every 20 ms it looks for a `firefox` process whose command line has `--backgroundtask` and `pingsender`. At first sight, per sender:
  * its profile dir, taken from the saved-telemetry-pings path in its command line (`<profile>/saved-telemetry-pings/<file>`);
  * a copy of that profile's prefs.js (kept in PREFS_DIR), with network.trr.stealth.contexts, network.trr.uri (set yes/no),
    network.trr.mode, network.trr.excluded-domains and network.proxy.type read from the copy, and the copy's mtime against the sender's birth;
  * the route it received: STEALTHFOX_PINGSENDER_ROUTE from its environment (present, proxy record present, type/scheme/host/port;
    credentials are never printed) and whether MOZ_LOG / MOZ_LOG_FILE reached it;
and when it ends (its lifetime). One JSON line per event in OUT_JSONL: "seen" and "end".

Also, for the attribution of every native DNS query to the process that sent it (the table step joins the kernel's view, bpftrace on
udp_sendmsg and tcpdump, with these records by pid and by time), the same loop snapshots EVERY process of the engine (its exe under
HUNT_ENGINE_DIR, /opt/ff): one "proc" event at first sight (pid, ppid, birth, comm, the head and the tail of the command line, the ancestors,
the process type, the profile: its own -profile or the saved-telemetry-pings path, else its parent's, the route it received), one "pupd" ~0.5 s
later (the comm and the command line a child sets after it starts), one "pend" when it ends; per profile one "prof" event (its birth, the
prefs of interest read from prefs.js and user.js, re-read until they appear) and one "pgone" when the directory disappears; every 2 s an
"anchor" (wall clock ms against CLOCK_MONOTONIC ns, the clock bpftrace's nsecs uses).

    python hunt_pingsender_dns.py sockpoll OUT_JSONL

The fallback when bpftrace cannot attach: polls `ss -H -u -a -n -p` and records (local port, pid, comm, peer port 53) the first time it sees each.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback

import psutil

INTEREST = ("network.trr.stealth.contexts", "network.trr.uri", "network.trr.mode", "network.trr.excluded-domains", "network.proxy.type")
ROUTE_VAR = "STEALTHFOX_PINGSENDER_ROUTE"
SECRET = re.compile(r"user|pass|auth|cred|secret|token|key", re.I)
LOCK = threading.Lock()
ENGINE = os.environ.get("HUNT_ENGINE_DIR", "/opt/ff")


def now_ms():
    return time.time() * 1000


def emit(path, rec):
    with LOCK, open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def read_prefs(path):
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    out = {}
    for name in INTEREST:
        m = re.search(r'user_pref\("%s", (.*)\);' % re.escape(name), text)
        out[name] = m.group(1)[:160] if m else None
    return out


def sanitize_route(raw):
    """What the sender received, without secrets: the record is there or not, its keys, and its type/scheme/host/port style fields."""
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
    prefs = route.get("prefs") if isinstance(route, dict) else None
    if isinstance(prefs, dict):
        out["prefs_n"] = len(prefs)
        out["prefs_of_interest"] = {k: prefs.get(k) for k in INTEREST if k in prefs}
    else:
        out["prefs_n"] = None
    return out


def profile_from(cmd):
    """The profile dir from the path of a saved telemetry ping in the command line: `<profile>/saved-telemetry-pings/<file>`."""
    for a in cmd:
        m = re.search(r"^(.*?)[/\\]saved-telemetry-pings[/\\]", a)
        if m:
            return m.group(1)
    for i, a in enumerate(cmd):
        if a in ("-profile", "--profile") and i + 1 < len(cmd):
            return cmd[i + 1]
    return None




def read_profile_prefs(profile):
    """The prefs of interest of a profile: prefs.js, then user.js on top of it (the file Firefox applies last wins)."""
    merged = {}
    for name in ("prefs.js", "user.js"):
        got = read_prefs(os.path.join(profile, name))
        for k, v in (got or {}).items():
            if v is not None or k not in merged:
                merged[k] = v
    return merged


def oldest_mtime(profile):
    try:
        return round(min(e.stat().st_mtime for e in os.scandir(profile) if e.is_file()) * 1000, 1)
    except (OSError, ValueError, TypeError):
        return None


def brief(cmd):
    out = []
    for a in cmd:
        a = re.sub(r"\?.*", "", a)
        a = re.sub(r"//[^/@\s]*@", "//", a)
        out.append(a[:110])
    return out


CHILD_ARG = {"tab": "content", "socket": "socket", "gpu": "gpu", "rdd": "rdd", "utility": "utility", "forkserver": "forkserver", "vr": "vr"}
COMM_TYPE = (("isolated web", "content"), ("web content", "content"), ("privileged cont", "content"), ("webextensions", "content"),
             ("isolated servic", "content"), ("socket process", "socket"), ("gpu process", "gpu"), ("rdd process", "rdd"),
             ("utility process", "utility"), ("forkserver", "forkserver"))


def classify(cmd, comm):
    """The process type: pingsender and other background tasks, the Gecko child types (from the thread name the child sets, else from the
    last argument of -contentproc), the main browser (it carries -profile)."""
    low = [a.lstrip("-").lower() for a in cmd[1:]]
    if "backgroundtask" in low:
        i = low.index("backgroundtask")
        name = low[i + 1] if i + 1 < len(low) else "?"
        return "pingsender" if name == "pingsender" else "backgroundtask:" + name
    c = (comm or "").lower()
    for k, v in COMM_TYPE:
        if c.startswith(k):
            return v
    if "contentproc" in low:
        return CHILD_ARG.get(low[-1], "child:" + low[-1][:20])
    if "forkserver" in low:
        return "forkserver"
    if "profile" in low or "no-remote" in low or "juggler-pipe" in low or "headless" in low:
        return "main"
    return "firefox:" + (comm or "?")


def follow(p, rec, out):
    while True:
        try:
            if not p.is_running() or p.status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.Error:
            break
        time.sleep(0.01)
    ended = now_ms()
    emit(out, {"event": "end", "pid": rec["pid"], "ended": round(ended, 1), "lifetime_ms": round(ended - rec["born"], 1)})


def capture_sender(p, cmd, out, prefs_dir):
    """The record of a pingsender at first sight (as in the earlier hunts: its prefs copy, its route, its profile's birth)."""
    try:
        born = p.create_time() * 1000
    except psutil.Error:
        return
    seen = now_ms()
    rec = {"event": "seen", "pid": p.pid, "ppid": p.ppid(), "born": round(born, 1), "seen": round(seen, 1),
           "cmd": [re.sub(r"[?&=].*", "", a)[:90] for a in cmd[:5]]}
    try:
        env = p.environ()
        rec["route"] = sanitize_route(env.get(ROUTE_VAR))
        rec["env_moz_log"] = env.get("MOZ_LOG")
        rec["env_moz_log_file"] = bool(env.get("MOZ_LOG_FILE"))
    except psutil.Error as exc:
        rec["route"] = {"error": type(exc).__name__}
    profile = profile_from(cmd)
    rec["profile"] = profile
    src = os.path.join(profile, "prefs.js") if profile else None
    try:
        st = os.stat(src)
        dst = os.path.join(prefs_dir, "prefs-sender-%d.js" % p.pid)
        shutil.copy2(src, dst)
        rec["prefs_copy_mtime_vs_birth_ms"] = round(st.st_mtime * 1000 - born, 1)
        rec["prefs_copy_taken_after_birth_ms"] = round(now_ms() - born, 1)
        rec["prefs"] = read_prefs(dst)
    except (OSError, TypeError) as exc:
        rec["prefs"] = None
        rec["prefs_error"] = "%s %s" % (type(exc).__name__, src)
    rec["profile_oldest_mtime_ms"] = oldest_mtime(profile) if profile else None
    emit(out, rec)
    threading.Thread(target=follow, args=(p, rec, out), daemon=True).start()


def chain_of(ppid, live):
    """The ancestors, nearest first (pid, comm, process type), from what the watcher already knows, else from /proc."""
    chain, pid = [], ppid
    for _ in range(6):
        if not pid or pid <= 1:
            break
        r = live.get(pid)
        if r is not None:
            chain.append({"pid": pid, "comm": r["comm"], "ptype": r["ptype"]})
            pid = r["ppid"]
            continue
        try:
            q = psutil.Process(pid)
            chain.append({"pid": pid, "comm": q.name(), "ptype": "outside:" + os.path.basename(q.exe() or "")})
            pid = q.ppid()
        except psutil.Error:
            break
    return chain


def snapshot(p, live):
    """One engine process at first sight: who it is, its parents, its profile (its own, else the nearest ancestor's), the route it received."""
    born = p.create_time() * 1000
    cmd = p.cmdline()
    comm = p.name()
    ppid = p.ppid()
    rec = {"event": "proc", "pid": p.pid, "ppid": ppid, "born": round(born, 1), "seen": round(now_ms(), 1), "comm": comm,
           "exe": os.path.basename(p.exe() or ""), "argc": len(cmd), "head": brief(cmd[:6]), "tail": brief(cmd[-3:]),
           "ptype": classify(cmd, comm)}
    rec["chain"] = chain_of(ppid, live)
    profile = profile_from(cmd)
    via = "own" if profile else None
    if profile is None:
        for c in rec["chain"]:
            r = live.get(c["pid"])
            if r is not None and r.get("profile"):
                profile, via = r["profile"], "ancestor %d" % c["pid"]
                break
    rec["profile"], rec["profile_via"] = profile, via
    try:
        route = sanitize_route(p.environ().get(ROUTE_VAR))
        rec["route_present"] = route.get("present")
        rec["route_proxy"] = route.get("proxy_present")
    except psutil.Error:
        rec["route_present"] = None
    return rec, cmd


class Profiles:
    """Per profile: its birth, the prefs of interest (re-read every 0.5 s for 4 s, or until the stealth contexts pref shows), its disappearance."""

    def __init__(self, out):
        self.out, self.items = out, {}

    def see(self, path, born_hint):
        if not path or path in self.items:
            return
        self.items[path] = {"born": oldest_mtime(path) or born_hint, "first": now_ms(), "next": 0.0, "vals": None, "done": False, "gone": False}
        self.poll(now_ms())

    def poll(self, now):
        for path, st in self.items.items():
            if st["gone"]:
                continue
            if not os.path.isdir(path):
                st["gone"] = True
                emit(self.out, {"event": "pgone", "profile": path, "t": round(now, 1)})
                continue
            if st["done"] or now < st["next"]:
                continue
            st["next"] = now + 500
            vals = read_profile_prefs(path)
            if vals != st["vals"]:
                st["vals"] = vals
                emit(self.out, {"event": "prof", "profile": path, "born": st["born"], "first": round(st["first"], 1), "t": round(now, 1), "prefs": vals})
            if (vals or {}).get("network.trr.stealth.contexts") is not None or now - st["first"] > 4000:
                st["done"] = True


def anchor(out):
    a = time.monotonic_ns()
    w = time.time() * 1000
    b = time.monotonic_ns()
    emit(out, {"event": "anchor", "wall": round(w, 3), "mono": (a + b) // 2, "err_ns": b - a})


def watch(out, prefs_dir):
    state = {"live": {}, "procs": {}, "ignore": set(), "last_anchor": 0.0}
    profiles = Profiles(out)
    os.makedirs(prefs_dir, exist_ok=True)
    anchor(out)
    print("watching the engine's processes under %s (pingsender senders in full)" % ENGINE, flush=True)
    while True:
        try:
            sweep(out, prefs_dir, state, profiles)
        except Exception:
            traceback.print_exc()
            sys.stdout.flush()
            sys.stderr.flush()
        time.sleep(0.02)


def sweep(out, prefs_dir, state, profiles):
    """One pass of the watcher (the state outlives it): ends, the second look, the new engine processes, the profiles."""
    live, procs, ignore = state["live"], state["procs"], state["ignore"]
    now = now_ms()
    if now - state["last_anchor"] > 2000:
        anchor(out)
        state["last_anchor"] = now
    try:
        pids = set(psutil.pids())
    except psutil.Error:
        pids = set()
    # Ends, and the one look at the comm and the command line a child sets after it starts.
    for pid in list(live):
        r, p = live[pid], procs[pid]
        try:
            gone = pid not in pids or not p.is_running() or p.status() == psutil.STATUS_ZOMBIE
        except psutil.Error:
            gone = True
        if gone:
            emit(out, {"event": "pend", "pid": pid, "born": r["born"], "ended": round(now_ms(), 1)})
            del live[pid], procs[pid]
        elif not r.get("refreshed") and now - r["seen"] > 500:
            r["refreshed"] = True
            try:
                cmd, comm = p.cmdline(), p.name()
                ptype = classify(cmd, comm)
            except psutil.Error:
                continue
            if comm != r["comm"] or ptype != r["ptype"]:
                r["comm"], r["ptype"] = comm, ptype
                emit(out, {"event": "pupd", "pid": pid, "born": r["born"], "comm": comm, "ptype": ptype, "head": brief(cmd[:6]), "tail": brief(cmd[-3:])})
    ignore &= pids
    # New engine processes, parents first (a child's profile is its parent's).
    for pid in sorted(pids - set(live) - ignore):
        try:
            p = psutil.Process(pid)
            exe = p.exe()
            age = now / 1000 - p.create_time()
        except psutil.Error:
            continue
        if not exe.startswith(ENGINE):
            # A process between fork and exec still shows its parent's exe: look again for a second before dismissing it.
            if age > 1.0:
                ignore.add(pid)
            continue
        try:
            rec, cmd = snapshot(p, live)
        except psutil.Error:
            continue
        live[pid], procs[pid] = rec, p
        profiles.see(rec["profile"], rec["born"])
        emit(out, rec)
        if rec["ptype"] == "pingsender":
            try:
                capture_sender(p, cmd, out, prefs_dir)
            except psutil.Error:
                pass
    profiles.poll(now_ms())


SS_RE = re.compile(r"^\S+\s+\d+\s+\d+\s+(\S+):(\d+)\s+(\S+):(\d+)\s+users:\(\((.*)\)\)\s*$")
SS_USER = re.compile(r'"([^"]*)",pid=(\d+),fd=(\d+)')


def sockpoll(out):
    """The fallback of the bpftrace attribution: the owner of every UDP socket whose peer port is 53, from `ss`, as fast as ss runs."""
    seen = set()
    print("polling ss -H -u -a -n -p", flush=True)
    while True:
        try:
            text = subprocess.run(["ss", "-H", "-u", "-a", "-n", "-p"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            time.sleep(0.05)
            continue
        t = now_ms()
        for line in text.splitlines():
            m = SS_RE.match(line.strip())
            if not m or m.group(4) != "53":
                continue
            for comm, pid, fd in SS_USER.findall(m.group(5)):
                key = (m.group(2), pid, fd)
                if key in seen:
                    continue
                seen.add(key)
                emit(out, {"event": "ssq", "t": round(t, 1), "sport": int(m.group(2)), "pid": int(pid), "comm": comm, "fd": int(fd), "peer": m.group(3)})
        time.sleep(0.002)


if __name__ == "__main__":
    if sys.argv[1] == "sockpoll":
        sockpoll(sys.argv[2])
    else:
        watch(sys.argv[2], sys.argv[3])
