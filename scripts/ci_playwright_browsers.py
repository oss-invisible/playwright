#!/usr/bin/env python3
"""Writes ci/py-lock/playwright-browsers.json: the sha256 of every archive of Playwright's own Firefox that the engine series' gate installs (ci-03).

The rows of the gate that compare the engine with the stock Firefox install it from Playwright's CDN. A third-party download is checked by a hash pinned in a
repository, so the archives are pinned HERE, at the wrapper_ref the gate selects, for the Playwright of scripts/playwright_pin.txt: what
`python -m playwright install --dry-run firefox` lists on each platform (Firefox, FFmpeg, and winldd on Windows), each downloaded once and hashed.

    python scripts/ci_playwright_browsers.py [--python python] [--out ci/py-lock/playwright-browsers.json]

The interpreter must have the pinned Playwright installed (the dry-run is what names the archives, so a different version names different ones). The output is
sorted by platform, entries in the order the dry-run lists them, LF, two-space indent, so the same input gives the same bytes.
tests/test_the_playwright_browsers_pin_follows_the_pin.py reads the committed file with `problems`, offline.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "ci" / "py-lock" / "playwright-browsers.json"
PLAYWRIGHT_PIN = ROOT / "scripts" / "playwright_pin.txt"

#: The CDN the dry-run is asked to name (the gate downloads from the same host).
DOWNLOAD_HOST = "https://cdn.playwright.dev/dbazure/download/playwright"
#: platform name in the file -> PLAYWRIGHT_HOST_PLATFORM_OVERRIDE of the dry-run.
PLATFORMS = {"ubuntu24.04-x64": "ubuntu24.04-x64", "win64": "win64"}
#: The archive kinds the dry-run lists for each platform (the first path segment after builds/).
KINDS = {"ubuntu24.04-x64": ["firefox", "ffmpeg"], "win64": ["firefox", "ffmpeg", "winldd"]}

PATH_RE = re.compile(r"^builds/([a-z0-9_-]+)/[0-9]+/[A-Za-z0-9._-]+\.zip$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def problems(data: object, pin: str) -> list:
    """What is wrong with a parsed playwright-browsers.json, given the Playwright pin; an empty list when it is sound."""
    out = []
    if not isinstance(data, dict) or set(data) != {"playwright", "platforms"}:
        return ["the file is not an object with exactly `playwright` and `platforms`"]
    if data["playwright"] != pin.strip():
        out.append("playwright is %r but scripts/playwright_pin.txt pins %r: regenerate with scripts/ci_playwright_browsers.py" % (data["playwright"], pin.strip()))
    platforms = data["platforms"]
    if not isinstance(platforms, dict) or set(platforms) != set(PLATFORMS):
        return out + ["platforms are %r, expected %r" % (sorted(platforms) if isinstance(platforms, dict) else platforms, sorted(PLATFORMS))]
    for name in sorted(PLATFORMS):
        entries = platforms[name]
        if not isinstance(entries, list):
            out.append("%s: not a list" % name)
            continue
        kinds = []
        for e in entries:
            path = e.get("path") if isinstance(e, dict) else None
            m = PATH_RE.match(path) if isinstance(path, str) else None
            if not m:
                out.append("%s: %r is not a relative `builds/<kind>/<revision>/<archive>.zip` path" % (name, path))
                continue
            kinds.append(m.group(1))
            if not isinstance(e.get("sha256"), str) or not SHA_RE.match(e["sha256"]):
                out.append("%s: %s has no 64-hex sha256" % (name, path))
            if "size" in e and not (isinstance(e["size"], int) and not isinstance(e["size"], bool) and e["size"] > 0):
                out.append("%s: %s has a size that is not a positive integer" % (name, path))
            if set(e) - {"path", "sha256", "size"}:
                out.append("%s: %s has unknown fields %s" % (name, path, sorted(set(e) - {"path", "sha256", "size"})))
        if sorted(kinds) != sorted(KINDS[name]):
            out.append("%s: the archives are %s, the dry-run lists %s" % (name, sorted(kinds), sorted(KINDS[name])))
    return out


def installed_version(py: str) -> str:
    return subprocess.run([py, "-c", "import importlib.metadata as m; print(m.version('playwright'))"], capture_output=True, text=True, check=True).stdout.strip()


def dry_run_paths(py: str, platform: str) -> list:
    env = dict(os.environ, PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=PLATFORMS[platform], PLAYWRIGHT_DOWNLOAD_HOST=DOWNLOAD_HOST)
    dry = subprocess.run([py, "-m", "playwright", "install", "--dry-run", "firefox"], capture_output=True, text=True, env=env, check=True).stdout
    urls = re.findall(r"Download url:\s+(\S+)", dry)
    prefix = DOWNLOAD_HOST + "/"
    bad = [u for u in urls if not u.startswith(prefix)]
    if bad or not urls:
        raise SystemExit("%s: the dry-run names %r, not archives of %s" % (platform, bad or urls, DOWNLOAD_HOST))
    return [u[len(prefix):] for u in urls]


def fetch_hash(path: str) -> tuple:
    h, size = hashlib.sha256(), 0
    with urllib.request.urlopen(DOWNLOAD_HOST + "/" + path, timeout=300) as r:
        while chunk := r.read(1 << 20):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def render(data: object) -> str:
    """The bytes of the file for a parsed content: two-space indent, LF, one trailing newline (the same content always gives the same bytes)."""
    return json.dumps(data, indent=2) + "\n"


def build(py: str, pin: str) -> str:
    version = installed_version(py)
    if version != pin.strip():
        raise SystemExit("the interpreter has playwright %s, scripts/playwright_pin.txt pins %s: install the pinned one" % (version, pin.strip()))
    platforms = {}
    for name in sorted(PLATFORMS):
        entries = []
        for path in dry_run_paths(py, name):
            sha, size = fetch_hash(path)
            entries.append({"path": path, "sha256": sha, "size": size})
            print(name, path, size, sha, flush=True)
        platforms[name] = entries
    data = {"playwright": version, "platforms": platforms}
    bad = problems(data, pin)
    if bad:
        raise SystemExit("the generated file is not sound: " + "; ".join(bad))
    return render(data)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--python", default=sys.executable, help="interpreter with the pinned playwright installed (default: this one)")
    ap.add_argument("--out", default=str(OUT_PATH))
    a = ap.parse_args(argv)
    text = build(a.python, PLAYWRIGHT_PIN.read_text(encoding="utf-8"))
    Path(a.out).write_text(text, encoding="utf-8", newline="\n")
    print("%s written" % Path(a.out).name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
