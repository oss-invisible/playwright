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
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time

import psutil

INTEREST = ("network.trr.stealth.contexts", "network.trr.uri", "network.trr.mode", "network.trr.excluded-domains", "network.proxy.type")
ROUTE_VAR = "STEALTHFOX_PINGSENDER_ROUTE"
SECRET = re.compile(r"user|pass|auth|cred|secret|token|key", re.I)
LOCK = threading.Lock()


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


def capture(p, out, prefs_dir, seen_pids):
    try:
        born = p.create_time() * 1000
        cmd = p.cmdline()
    except psutil.Error:
        return
    if not any(a.lstrip("-").lower() == "backgroundtask" for a in cmd[1:]) or "pingsender" not in cmd:
        seen_pids[p.pid] = None
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
    seen_pids[p.pid] = rec
    emit(out, rec)
    threading.Thread(target=follow, args=(p, rec, out), daemon=True).start()


def watch(out, prefs_dir):
    os.makedirs(prefs_dir, exist_ok=True)
    seen_pids = {}
    print("watching for pingsender processes", flush=True)
    while True:
        try:
            for p in psutil.process_iter(["name"]):
                if p.pid in seen_pids or "firefox" not in (p.info["name"] or "").lower():
                    continue
                capture(p, out, prefs_dir, seen_pids)
        except psutil.Error:
            pass
        time.sleep(0.02)


if __name__ == "__main__":
    watch(sys.argv[2], sys.argv[3])
