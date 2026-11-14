"""HUNT ONLY (branch hunt/cpu-render-path, never merged): why does our Firefox use ~6x the CPU of stock Firefox 159 with a window?

Nothing of the product is changed: this script only launches the two arms the way scripts/hunt_cpu_per_browser.py does and looks.

    python scripts/hunt_cpu_render_path.py support  --arm ours        --binary "$FF" --out DIR     # 1. about:support, prefs, env
    python scripts/hunt_cpu_render_path.py support  --arm stockheaded                --out DIR
    python scripts/hunt_cpu_render_path.py diff     DIR                                            # the Graphics diff
    python scripts/hunt_cpu_render_path.py profile  --arm ours        --binary "$FF" --out DIR     # 2. Gecko profiles of the 10-scene loop
    python scripts/hunt_cpu_render_path.py profile  --arm stockheaded                --out DIR

ARMS (as in hunt_cpu_per_browser.py)
  ours         invisible_playwright, headless=True (on Windows: headed Firefox on a hidden desktop)
  stockheaded  stock Playwright's firefox, firefox.launch(headless=False)

1. `support` launches the arm TWICE, alone:
   A. clean launch (nothing added). Route "juggler": page.goto("about:support") in a page of the browser and save document.body.innerText.
      Then, while the browser is alive, the parent's command line, its environment (MOZ_*, STEALTH*, GFX*, ... only) and the profile's
      prefs.js and user.js are copied (the profile directory comes from `-profile` in the command line), and the process types are counted
      (is there a GPU process?).
   B. launch with `-marionette -remote-allow-system-access` and MOZ_REMOTE_ALLOW_SYSTEM_ACCESS=1 (the route tests/webfont_probe.py uses),
      and in chrome scope: Troubleshoot.snapshot() (the data about:support is made of) and the EFFECTIVE prefs (every pref with a user value,
      plus the whole gfx.*, layers.*, webgl.*, widget.*, media.*, dom.ipc.*, ... family). This is the route "troubleshoot". It is needed even
      when the juggler route works, because in our product the prefs travel over the wire and never reach prefs.js or user.js.
   The report says which route worked, and `diff` prints the Graphics section of both routes side by side, the lines that decide
   compositing (WebRender hardware/software, D3D11/WARP, GPU process, the feature decisions and their reasons) and the differing prefs.

2. `profile` runs the arm of hunt_cpu_per_browser.py (4 browsers at once, 10 rounds of the same scenario each) with the Gecko profiler on
   from startup in EVERY process:
      MOZ_PROFILER_STARTUP=1  MOZ_PROFILER_STARTUP_FEATURES=js,stackwalk,cpu,threads,processcpu  MOZ_PROFILER_STARTUP_FILTERS=*
      MOZ_PROFILER_STARTUP_INTERVAL=2  MOZ_PROFILER_SHUTDOWN=<out>/profile-%p.json
   `threads` and `processcpu` are added to the three features asked for: without `threads` only the main thread of each process is sampled
   and the GPU process's cost is on the Compositor and Renderer threads; STARTUP_FILTERS=* keeps every thread of the parent.
   The profiler slows the browsers: the CPU numbers of this run are printed but are NOT the comparison; part 3 is.
   Our product removes every MOZ_* variable that is not on its pass list from the engine's environment (invisible_core._engine_env.
   without_the_hosts), so MOZ_PROFILER_* would never reach our binary. This script lets MOZ_PROFILER_* through by wrapping that function in
   this process only (a hunt-side switch, not a change of the product); the profile run says whether the variables arrived by counting the
   profile files it got.
   The profiles are written when each process shuts down cleanly; the files are gzipped and left in <out> for the artifact.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import difflib
import gzip
import importlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

MARIONETTE_PORTS = (28431, 2828)
ENV_INTERESTING = re.compile(r"^(MOZ_|STEALTH|INVPW|GFX|WEBRENDER|DXGI|D3D|LIBGL|MESA|GDK_|GTK_|DISPLAY|USERNAME$|SESSIONNAME$)", re.I)
#: the prefs families whose EFFECTIVE value is read (a pref with a user value is read whatever its name)
PREF_FAMILIES = (r"gfx\.|layers\.|webgl\.|widget\.|media\.(hardware|rdd|gpu|ffvpx|wmf|utility)|dom\.ipc\.|dom\.webgpu|fission|browser\.tabs\.remote|"
                 r"apz\.|general\.smoothScroll|privacy\.resistFingerprinting|nglayout\.|security\.sandbox\.(gpu|content)|"
                 r"layout\.(frame_rate|css\.devPixelsPerPx)|toolkit\.cosmeticAnimations|"
                 r"accessibility\.|stealth|juggler|zoom\.|webrender|canvas|gpu|headless|remote\.")
GRAPHICS_KEYS = ("graphics", "features", "processes", "modifiedPreferences", "environmentVariables", "userJS", "experimentalFeatures",
                 "lockedPreferences")
KEY_LINE = re.compile(r"(?i)compositing|webrender|d3d11|d3d9|warp|direct2d|d2d|gpu process|hardware|software|decision|blocklist|blocked|disabled|"
                      r"unavailable|failed|angle|renderer|adapter|driver|device|window protocol|display|advanced layers|opengl|vulkan|"
                      r"remote process|process type|acceler|basic|force|fallback|status|reason|gfx\.|layers\.")


# --------------------------------------------------------------------------------------------------- the profiler switch

def let_profiler_env_through() -> str:
    """Wrap invisible_core._engine_env.without_the_hosts so that MOZ_PROFILER_* (and nothing else) survives. This process only."""
    try:
        eng = importlib.import_module("invisible_core._engine_env")
    except Exception as exc:   # noqa: BLE001
        return "invisible_core._engine_env not importable (%s: %s): our binary will not see MOZ_PROFILER_*" % (type(exc).__name__, exc)
    original = eng.without_the_hosts

    def wrapped(env):
        kept = original(env)
        for k, v in env.items():
            if k.upper().startswith("MOZ_PROFILER_"):
                kept[k] = v
        return kept

    eng.without_the_hosts = wrapped
    return "without_the_hosts wrapped: MOZ_PROFILER_* passes"


# ------------------------------------------------------------------------------------------------------------- process facts

def firefox_processes() -> List[Dict[str, Any]]:
    import psutil
    out = []
    for p in psutil.process_iter(["pid", "ppid", "name"]):
        if not (p.info.get("name") or "").lower().startswith("firefox"):
            continue
        try:
            cmd = p.cmdline()
        except Exception:   # noqa: BLE001
            cmd = []
        kind = "parent"
        if "-contentproc" in cmd:
            last = str(cmd[-1]).strip('"').lower() if cmd else "?"
            kind = last if last in ("tab", "gpu", "socket", "rdd", "utility", "forkserver") else "other"
        out.append({"pid": p.info["pid"], "ppid": p.info["ppid"], "kind": kind, "cmd": cmd, "proc": p})
    return out


def parent_facts() -> Dict[str, Any]:
    """The facts of the running parent: command line, profile dir, interesting environment, the types of every firefox process."""
    procs = firefox_processes()
    kinds: Dict[str, int] = {}
    for p in procs:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    parents = [p for p in procs if p["kind"] == "parent" and "-profile" in p["cmd"]] or [p for p in procs if p["kind"] == "parent"]
    facts: Dict[str, Any] = {"process_kinds": kinds, "parents": len(parents)}
    if not parents:
        return facts
    parent = parents[0]
    cmd = parent["cmd"]
    facts["cmdline"] = cmd
    if "-profile" in cmd:
        facts["profile_dir"] = cmd[cmd.index("-profile") + 1]
    try:
        env = parent["proc"].environ()
        facts["env"] = {k: v for k, v in sorted(env.items()) if ENV_INTERESTING.match(k)}
    except Exception as exc:   # noqa: BLE001
        facts["env_error"] = "%s: %s" % (type(exc).__name__, exc)
    return facts


def copy_profile_files(facts: Dict[str, Any], out: Path) -> None:
    profile = facts.get("profile_dir")
    if not profile:
        return
    for name in ("user.js", "prefs.js"):
        src = Path(profile) / name
        try:
            if src.is_file():
                shutil.copyfile(src, out / name)
                facts[name + "_bytes"] = src.stat().st_size
            else:
                facts[name + "_bytes"] = None
        except Exception as exc:   # noqa: BLE001
            facts[name + "_error"] = "%s: %s" % (type(exc).__name__, exc)


# ---------------------------------------------------------------------------------------------------------------- Marionette

class Marionette:
    def __init__(self, timeout: float = 60.0):
        import socket
        self.n = 0
        self.port = 0
        end = time.time() + timeout
        last: Optional[BaseException] = None
        while time.time() < end:
            for port in MARIONETTE_PORTS:
                try:
                    self.sock = socket.create_connection(("127.0.0.1", port), timeout=3)
                    self.sock.settimeout(120)
                    self._read()
                    self.port = port
                    return
                except (OSError, ValueError) as exc:
                    last = exc
            time.sleep(1)
        raise OSError("no Marionette on %s: %s" % (MARIONETTE_PORTS, last))

    def _read(self):
        head = b""
        while not head.endswith(b":"):
            c = self.sock.recv(1)
            if not c:
                raise OSError("closed")
            head += c
        size = int(head[:-1])
        data = b""
        while len(data) < size:
            data += self.sock.recv(size - len(data))
        return json.loads(data)

    def call(self, name: str, params: Optional[dict] = None):
        self.n += 1
        body = json.dumps([0, self.n, name, params or {}]).encode()
        self.sock.sendall(str(len(body)).encode() + b":" + body)
        while True:
            msg = self._read()
            if msg[0] == 1 and msg[1] == self.n:
                if msg[2]:
                    raise RuntimeError("%s: %s" % (msg[2].get("error"), str(msg[2].get("message"))[:300]))
                return msg[3]

    def close(self):
        with contextlib.suppress(Exception):
            self.sock.close()


TROUBLESHOOT_JS = """
let cb = arguments[arguments.length - 1];
let mod;
try { mod = ChromeUtils.importESModule("resource://gre/modules/Troubleshoot.sys.mjs"); }
catch (e) { try { mod = ChromeUtils.import("resource://gre/modules/Troubleshoot.jsm"); } catch (e2) { cb(JSON.stringify({error: String(e) + " / " + String(e2)})); return; } }
Promise.resolve(mod.Troubleshoot.snapshot()).then(s => cb(JSON.stringify(s)), e => cb(JSON.stringify({error: String(e)})));
"""

PREFS_JS = """
let cb = arguments[arguments.length - 1];
let rel = new RegExp("^(" + arguments[0] + ")", "i");
let out = {};
for (let n of Services.prefs.getChildList("")) {
  let user = Services.prefs.prefHasUserValue(n);
  if (!user && !rel.test(n)) continue;
  let v = null;
  try {
    switch (Services.prefs.getPrefType(n)) {
      case 32: v = Services.prefs.getStringPref(n); break;
      case 64: v = Services.prefs.getIntPref(n); break;
      case 128: v = Services.prefs.getBoolPref(n); break;
    }
  } catch (e) { v = "<unreadable: " + e + ">"; }
  out[n] = [user, v];
}
cb(JSON.stringify(out));
"""


def marionette_value(res):
    return res.get("value") if isinstance(res, dict) and "value" in res else res


def privileged_route(out: Path, report: Dict[str, Any]) -> None:
    """Troubleshoot.snapshot() and the effective prefs, through Marionette in chrome scope."""
    m = None
    try:
        m = Marionette()
        report["marionette_port"] = m.port
        m.call("WebDriver:NewSession", {"capabilities": {"alwaysMatch": {}}})
        m.call("WebDriver:SetTimeouts", {"script": 120000})
        m.call("Marionette:SetContext", {"value": "chrome"})
        snap = marionette_value(m.call("WebDriver:ExecuteAsyncScript", {"script": TROUBLESHOOT_JS, "args": []}))
        (out / "troubleshoot.json").write_text(snap if isinstance(snap, str) else json.dumps(snap), encoding="utf-8")
        report["troubleshoot"] = "ok, %d bytes" % len(snap)
        prefs = marionette_value(m.call("WebDriver:ExecuteAsyncScript", {"script": PREFS_JS, "args": [PREF_FAMILIES]}))
        (out / "effective_prefs.json").write_text(prefs if isinstance(prefs, str) else json.dumps(prefs), encoding="utf-8")
        report["effective_prefs"] = "ok, %d bytes" % len(prefs)
    except Exception as exc:   # noqa: BLE001
        report["privileged_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:400])
    finally:
        if m is not None:
            m.close()


# ------------------------------------------------------------------------------------------------------------- the launches

@contextlib.asynccontextmanager
async def launched(arm: str, binary: str, marionette: bool):
    """Yield one browser of the arm, alone."""
    extra_args = ["-marionette", "-remote-allow-system-access"] if marionette else []
    if marionette:
        os.environ["MOZ_REMOTE_ALLOW_SYSTEM_ACCESS"] = "1"
    if arm == "ours":
        from invisible_playwright.async_api import InvisiblePlaywright
        prefs = {"marionette.port": MARIONETTE_PORTS[0]} if marionette else None
        async with InvisiblePlaywright(seed=100, binary_path=binary, headless=True, locale="en-US", timezone="Europe/Rome",
                                       extra_args=extra_args, extra_prefs=prefs) as browser:
            yield browser
        return
    async_playwright = importlib.import_module("playwright.async_api").async_playwright   # by name: the fork's guard forbids a static import
    pw = await async_playwright().start()
    browser = None
    try:
        prefs = {"fission.autostart": True}
        if marionette:
            prefs["marionette.port"] = MARIONETTE_PORTS[0]
        browser = await pw.firefox.launch(headless=False, args=extra_args, firefox_user_prefs=prefs)
        yield browser
    finally:
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.close()
        with contextlib.suppress(Exception):
            await pw.stop()


async def juggler_route(browser, out: Path, report: Dict[str, Any]) -> None:
    page = None
    try:
        page = await browser.new_page()
        await page.goto("about:support", wait_until="load", timeout=30000)
        with contextlib.suppress(Exception):
            await page.wait_for_function(
                "(() => { const e = document.getElementById('graphics-tbody'); return !!e && e.children.length > 0; })()", timeout=20000)
        await asyncio.sleep(2)   # the page fills its sections one by one
        text = await page.evaluate("document.body.innerText")
        (out / "about_support.txt").write_text(text, encoding="utf-8")
        report["juggler_route"] = "ok, %d chars, %d lines" % (len(text), text.count("\n") + 1)
    except Exception as exc:   # noqa: BLE001
        report["juggler_route"] = "FAILED: %s: %s" % (type(exc).__name__, str(exc)[:400])
    finally:
        if page is not None:
            with contextlib.suppress(Exception):
                await page.close()


async def support_arm(arm: str, binary: str, out: Path) -> Dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    report: Dict[str, Any] = {"arm": arm}
    kill_firefox()
    try:                                                    # A: the clean launch
        async with launched(arm, binary, marionette=False) as browser:
            report["version"] = getattr(browser, "version", None)
            await juggler_route(browser, out, report)
            report["launch_A"] = parent_facts()
            copy_profile_files(report["launch_A"], out)
    except BaseException as exc:   # noqa: BLE001
        report["launch_A_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:400])
    kill_firefox()
    try:                                                    # B: the privileged route
        async with launched(arm, binary, marionette=True) as browser:
            page = await browser.new_page()                 # a window exists, as in launch A
            await page.goto("about:blank")
            report["launch_B"] = parent_facts()
            await asyncio.get_running_loop().run_in_executor(None, privileged_route, out, report)
    except BaseException as exc:   # noqa: BLE001
        report["launch_B_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:400])
    kill_firefox()
    (out / "support_report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    return report


def kill_firefox() -> None:
    for p in firefox_processes():
        with contextlib.suppress(Exception):
            p["proc"].kill()
    time.sleep(1)


def print_support(report: Dict[str, Any]) -> None:
    print("=" * 100)
    print("support, arm %s (browser version %s)" % (report["arm"], report.get("version")))
    print("  route juggler (page.goto about:support, document.body.innerText): %s" % report.get("juggler_route", "not tried"))
    print("  route troubleshoot (Marionette, chrome scope, Troubleshoot.snapshot): %s"
          % (report.get("troubleshoot") or report.get("privileged_error") or report.get("launch_B_error") or "not tried"))
    print("  effective prefs (Services.prefs): %s" % report.get("effective_prefs", "none"))
    for key in ("launch_A", "launch_B"):
        f = report.get(key) or {}
        print("  %s: process kinds %s, parents %s, profile dir %s, user.js %s bytes, prefs.js %s bytes"
              % (key, f.get("process_kinds"), f.get("parents"), f.get("profile_dir"), f.get("user.js_bytes"), f.get("prefs.js_bytes")))
        if f.get("cmdline"):
            print("    command line: %s" % " ".join(str(x) for x in f["cmdline"])[:600])
        for k, v in (f.get("env") or {}).items():
            print("    env %s=%s" % (k, str(v)[:120]))
        if f.get("env_error"):
            print("    env: %s" % f["env_error"])
    for key in ("launch_A_error", "launch_B_error"):
        if report.get(key):
            print("  %s: %s" % (key, report[key]))
    sys.stdout.flush()


# ---------------------------------------------------------------------------------------------------------------- the diff

def graphics_section(text: str) -> List[str]:
    """The lines of the Graphics section of about:support's innerText (from the heading to the next known heading)."""
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in text.splitlines()]
    lines = [l for l in lines if l]
    stops = {"media", "audio", "internationalization & localization", "accessibility", "library versions", "experimental features",
             "sandbox", "safe mode", "startup cache", "winsock lsps", "remote processes", "environment variables", "normandy",
             "mozilla crash reports", "javascript", "place database", "extensions", "security software", "user javascript settings"}
    start = next((i for i, l in enumerate(lines) if l.lower() == "graphics"), None)
    if start is None:
        return ["<no 'Graphics' heading found in about:support>"]
    out = []
    for l in lines[start:]:
        if out and l.lower() in stops:
            break
        out.append(l)
    return out


def flatten(prefix: str, value: Any, out: List[str]) -> None:
    if isinstance(value, dict):
        for k in sorted(value):
            flatten("%s.%s" % (prefix, k) if prefix else str(k), value[k], out)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            flatten("%s[%d]" % (prefix, i), v, out)
    else:
        out.append("%s = %s" % (prefix, value))


def snapshot_lines(path: Path, keys: Tuple[str, ...]) -> Dict[str, List[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: (lambda o: (flatten("", data[k], o), o)[1])([]) for k in keys if k in data}


def side(title: str, a: List[str], b: List[str], limit: int = 400) -> None:
    diff = [l for l in difflib.unified_diff(a, b, "ours", "stockheaded", lineterm="", n=0) if not l.startswith(("---", "+++", "@@"))]
    print("-" * 100)
    print("%s: %d lines (ours), %d lines (stockheaded), %d differing lines (- ours only, + stockheaded only)" % (title, len(a), len(b), len(diff)))
    for l in diff[:limit]:
        print(l[:300])
    if len(diff) > limit:
        print("... %d more" % (len(diff) - limit))


def read_prefs_js(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if path.is_file():
        for m in re.finditer(r'user_pref\("([^"]+)",\s*(.*?)\);\s*$', path.read_text(encoding="utf-8", errors="replace"), re.M):
            out[m.group(1)] = m.group(2)
    return out


def diff_dir(root: Path) -> int:
    ours, stock = root / "ours", root / "stockheaded"
    print("=" * 100)
    print("GRAPHICS DIFF, ours (-) against stockheaded (+)")
    for a in (ours, stock):
        r = a / "support_report.json"
        if r.is_file():
            rep = json.loads(r.read_text(encoding="utf-8"))
            print("%s: juggler route: %s; troubleshoot route: %s" % (a.name, rep.get("juggler_route"),
                  rep.get("troubleshoot") or rep.get("privileged_error") or rep.get("launch_B_error")))
    # about:support innerText, Graphics section
    ta, tb = ours / "about_support.txt", stock / "about_support.txt"
    if ta.is_file() and tb.is_file():
        ga, gb = graphics_section(ta.read_text(encoding="utf-8")), graphics_section(tb.read_text(encoding="utf-8"))
        side("about:support innerText, Graphics section", ga, gb)
        for name, g in (("ours", ga), ("stockheaded", gb)):
            print("-" * 100)
            print("key lines of the Graphics section, %s:" % name)
            for l in g:
                if KEY_LINE.search(l):
                    print("  " + l[:240])
        # the rest of the page, for what is not in Graphics (Remote Processes, Environment Variables, Important Modified Preferences ...)
        fa = [re.sub(r"[ \t]+", " ", l).strip() for l in ta.read_text(encoding="utf-8").splitlines() if l.strip()]
        fb = [re.sub(r"[ \t]+", " ", l).strip() for l in tb.read_text(encoding="utf-8").splitlines() if l.strip()]
        side("about:support innerText, WHOLE page", fa, fb, limit=300)
    else:
        print("about:support innerText missing for: %s" % ", ".join(n for n, p in (("ours", ta), ("stockheaded", tb)) if not p.is_file()))
    # Troubleshoot data
    sa, sb = ours / "troubleshoot.json", stock / "troubleshoot.json"
    if sa.is_file() and sb.is_file():
        la, lb = snapshot_lines(sa, GRAPHICS_KEYS), snapshot_lines(sb, GRAPHICS_KEYS)
        for key in GRAPHICS_KEYS:
            if key in la or key in lb:
                side("Troubleshoot.snapshot().%s" % key, la.get(key, []), lb.get(key, []))
        for name, lines in (("ours", la.get("graphics", [])), ("stockheaded", lb.get("graphics", []))):
            print("-" * 100)
            print("key lines of Troubleshoot graphics, %s:" % name)
            for l in lines:
                if KEY_LINE.search(l):
                    print("  " + l[:240])
    else:
        print("troubleshoot.json missing for: %s" % ", ".join(n for n, p in (("ours", sa), ("stockheaded", sb)) if not p.is_file()))
    # effective prefs
    pa, pb = ours / "effective_prefs.json", stock / "effective_prefs.json"
    if pa.is_file() and pb.is_file():
        ea, eb = json.loads(pa.read_text(encoding="utf-8")), json.loads(pb.read_text(encoding="utf-8"))
        names = sorted(set(ea) | set(eb))
        rows = [(n, ea.get(n), eb.get(n)) for n in names if ea.get(n) != eb.get(n)]
        print("-" * 100)
        print("EFFECTIVE prefs that differ (name: ours [user-set, value] | stockheaded [user-set, value]); %d of %d read" % (len(rows), len(names)))
        for family in ("gfx.", "layers.", "webgl.", "widget.", "media.", "dom.", "other"):
            sel = [r for r in rows if (r[0].startswith(family) if family != "other" else not r[0].startswith(
                ("gfx.", "layers.", "webgl.", "widget.", "media.", "dom.")))]
            if sel:
                print("  [%s*] %d" % (family, len(sel)))
                for n, x, y in sel[:250]:
                    print("    %s: %s | %s" % (n, json.dumps(x)[:90] if x is not None else "-", json.dumps(y)[:90] if y is not None else "-"))
    else:
        print("effective_prefs.json missing for: %s" % ", ".join(n for n, p in (("ours", pa), ("stockheaded", pb)) if not p.is_file()))
    # the profile's own files
    for fname in ("user.js", "prefs.js"):
        da, db = read_prefs_js(ours / fname), read_prefs_js(stock / fname)
        rows = [(n, da.get(n), db.get(n)) for n in sorted(set(da) | set(db)) if da.get(n) != db.get(n)]
        rel = [r for r in rows if re.match(PREF_FAMILIES, r[0])]
        print("-" * 100)
        print("%s of the profile: %d prefs in ours, %d in stockheaded, %d differ (%d in the gfx/layers/... families)"
              % (fname, len(da), len(db), len(rows), len(rel)))
        for n, x, y in rel[:200]:
            print("    %s: %s | %s" % (n, x if x is not None else "-", y if y is not None else "-"))
    return 0


# ------------------------------------------------------------------------------------------------------------- the profile

def profile_run(arm: str, binary: str, out: Path, rnd: int) -> int:
    import hunt_cpu_per_browser as hunt
    out.mkdir(parents=True, exist_ok=True)
    pattern = str(out / "profile-%p.json")
    os.environ.update({
        "MOZ_PROFILER_STARTUP": "1",
        "MOZ_PROFILER_STARTUP_FEATURES": "js,stackwalk,cpu,threads,processcpu",
        "MOZ_PROFILER_STARTUP_FILTERS": "*",
        "MOZ_PROFILER_STARTUP_INTERVAL": "2",
        "MOZ_PROFILER_SHUTDOWN": pattern,
    })
    print("profile arm %s: %s" % (arm, pattern))
    if arm == "ours":
        print(let_profiler_env_through())
    kill_firefox()
    result = hunt.run_arm(arm, binary, rnd)
    hunt.print_arm(result)
    kill_firefox()
    time.sleep(3)
    files = sorted(out.glob("profile-*.json"))
    total = 0
    for f in files:
        raw = f.read_bytes()
        total += len(raw)
        with gzip.open(str(f) + ".gz", "wb", 6) as g:
            g.write(raw)
        f.unlink()
    print("profile files written by the processes: %d (%.1f MB raw), gzipped in %s" % (len(files), total / 2 ** 20, out))
    if not files:
        print("NO PROFILE FILES: the variables did not arrive, the profiler is not in this build, or the processes were killed instead of closed")
    (out / "cpu_profiled.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("support", "profile"):
        p = sub.add_parser(name)
        p.add_argument("--arm", choices=("ours", "stockheaded"), required=True)
        p.add_argument("--binary", default=os.environ.get("INVPW_BINARY_PATH", ""))
        p.add_argument("--out", type=Path, required=True, help="the root; the arm's files go in <out>/<arm>/")
        p.add_argument("--round", type=int, default=1)
    d = sub.add_parser("diff")
    d.add_argument("root", type=Path)
    args = ap.parse_args()
    if args.cmd == "diff":
        return diff_dir(args.root)
    if args.arm == "ours" and not args.binary:
        ap.error("the arm ours needs --binary (or INVPW_BINARY_PATH)")
    arm_dir = args.out / args.arm
    if args.cmd == "support":
        report = asyncio.run(support_arm(args.arm, args.binary, arm_dir))
        print_support(report)
        return 0
    return profile_run(args.arm, args.binary, arm_dir, args.round)


if __name__ == "__main__":
    sys.exit(main())
