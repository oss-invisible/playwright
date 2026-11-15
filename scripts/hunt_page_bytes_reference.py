"""HUNT ONLY (branch hunt/page-bytes-reference, never merged): record the reference of what a page reads, per operating system.

    python scripts/hunt_page_bytes_reference.py --binary "$FF" --out DIR [--launches 2]

It does what test 3 of tests/test_software_compositing_is_declared_e2e.py does (the same launch, the same page, the same probe
tests/page_bytes_probe.js), `--launches` times, in the engine and with the core this checkout installed, and instead of comparing with
tests/data/page_bytes_<os>.json it WRITES that file:

  * every key whose entry (tests/_page_bytes.entry_of: the string, or hash and length; for a PNG key the length and the decoded pixels)
    is the same in every launch goes to the reference;
  * every key that is not (an unstable key) is listed apart, with its entries, and left out of the reference: a reference holds nothing
    that changes by itself.

Out, in DIR: page_bytes_<os>.json (the reference, in the form the test reads: copy it to tests/data/), unstable.json, launch-<n>.json.gz
(the raw probe values of each launch) and summary.txt. The log gets the reference between RECORD-BEGIN <os> and RECORD-END.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import _page_bytes as pb  # noqa: E402
import test_software_compositing_is_declared_e2e as t  # noqa: E402  (LAUNCH, the page server, OS_NAME: what test 3 uses)


def one_launch(binary: str) -> dict:
    from invisible_playwright import InvisiblePlaywright

    expression = (ROOT / "tests" / "page_bytes_probe.js").read_text(encoding="utf-8")
    server, url = t._a_page_server("<!doctype html><meta charset=utf-8><title>probe</title><body></body>")
    try:
        with InvisiblePlaywright(binary_path=binary, **t.LAUNCH) as browser:
            page = browser.new_page()
            page.goto(url, wait_until="load", timeout=60000)
            return page.evaluate(expression)
    finally:
        server.shutdown()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default=os.environ.get("INVPW_BINARY_PATH", ""))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--launches", type=int, default=2)
    args = ap.parse_args()
    if not args.binary:
        ap.error("--binary (or INVPW_BINARY_PATH)")
    args.out.mkdir(parents=True, exist_ok=True)

    launches = []
    for n in range(1, args.launches + 1):
        data = one_launch(args.binary)
        with gzip.open(args.out / ("launch-%d.json.gz" % n), "wt", encoding="utf-8") as f:
            json.dump(data, f)
        launches.append(data)
        print("launch %d: %d keys, %d scene error(s)%s" % (n, len(data), len([k for k in data if k.endswith(".error")]),
                                                           (": " + ", ".join("%s=%s" % (k, data[k].get("s")) for k in sorted(data) if k.endswith(".error")))
                                                           if any(k.endswith(".error") for k in data) else ""))
        sys.stdout.flush()

    keys = sorted(set().union(*[set(d) for d in launches]))
    stable, pngs, unstable = {}, {}, {}
    for key in keys:
        if any(key not in d for d in launches):
            unstable[key] = ["missing in launch %d" % (i + 1) for i, d in enumerate(launches) if key not in d]
            continue
        entries = [pb.entry_of(key, d[key]) for d in launches]
        if any(e != entries[0] for e in entries) or "undecodable" in entries[0] or (pb.is_png_key(key) and "sha256" not in entries[0]):
            unstable[key] = entries
            continue
        (pngs if pb.is_png_key(key) else stable)[key] = entries[0]

    engine = re.search(r"firefox-\d+_[\d.]+_\d+", args.binary.replace("\\", "/"))
    reference = {
        "os": t.OS_NAME,
        "source": ("GitHub Actions run %s (workflow page-bytes-reference-hunt, runner image %s %s), wrapper commit %s, invisible-core %s, engine %s, seed %d, "
                   "locale %s, timezone %s, headless=True; %d launches, every key below is equal in all of them. `pngs`: the PNG keys, compared on their "
                   "length and decoded pixels because every PNG carries a per-launch chunk."
                   % (os.environ.get("GITHUB_RUN_ID", "?"), os.environ.get("ImageOS", "?"), os.environ.get("ImageVersion", "?"),
                      os.environ.get("GITHUB_SHA", "?")[:10], metadata.version("invisible-core"), engine.group(0) if engine else "?",
                      t.LAUNCH["seed"], t.LAUNCH["locale"], t.LAUNCH["timezone"], args.launches)),
        "keys": stable,
        "pngs": pngs,
    }
    path = args.out / ("page_bytes_%s.json" % t.OS_NAME)
    path.write_text(json.dumps(reference, indent=1) + "\n", encoding="utf-8", newline="\n")
    (args.out / "unstable.json").write_text(json.dumps(unstable, indent=1) + "\n", encoding="utf-8", newline="\n")

    lines = ["page bytes on %s, %d launches, invisible-core %s: %d keys read, %d stable (%d of them PNG), %d UNSTABLE"
             % (t.OS_NAME, args.launches, metadata.version("invisible-core"), len(keys), len(stable) + len(pngs), len(pngs), len(unstable))]
    lines += ["  unstable: %s %s" % (k, json.dumps(v)[:300]) for k, v in unstable.items()]
    lines += ["  scene error: %s = %s" % (k, stable[k].get("s")) for k in sorted(stable) if k.endswith(".error")]
    summary = "\n".join(lines)
    (args.out / "summary.txt").write_text(summary + "\n", encoding="utf-8", newline="\n")
    print(summary)
    print("RECORD-BEGIN %s" % t.OS_NAME)
    print(json.dumps({"keys": stable, "pngs": pngs}, sort_keys=True))
    print("RECORD-END")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("### Page bytes, %s\n\n```\n%s\n```\n" % (t.OS_NAME, summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
