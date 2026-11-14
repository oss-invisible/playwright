"""HUNT ONLY (branch hunt/cpu-render-path, never merged): find the Breakpad symbols of our engine's xul.dll among the recent runs of the
engine's release.yml (artifact symbols-win-x86_64), and keep the zip whose xul.pdb/<breakpad id>/ matches the binary that was profiled.

    python scripts/hunt_fetch_symbols.py --id ABC123... --out DIR [--run 123] [--repo oss-invisible/firefox_antidetect_patch]

The artifact of a candidate is kept 2 days, that of a release 14; the one that matches may have expired. At most MAX_DOWNLOADS artifacts
(about 400 MB each) are downloaded, newest first, unless --run names one. It needs `gh` and GH_TOKEN. Whatever happens, the exit code is 0
and the last line says `SYMBOLS: <path>` or `SYMBOLS: none (<why>)`.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

MAX_DOWNLOADS = 4
ARTIFACT = "symbols-win-x86_64"


def gh(*args: str, timeout: int = 900) -> str:
    done = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    if done.returncode:
        raise RuntimeError("gh %s: %s" % (" ".join(args[:3]), (done.stderr or done.stdout)[:300]))
    return done.stdout


def candidates(repo: str, run: str) -> list:
    if run:
        return [run]
    runs = json.loads(gh("run", "list", "-R", repo, "--workflow", "release.yml", "-L", "40", "--json", "databaseId,conclusion,createdAt"))
    out = []
    for r in runs:
        try:
            arts = json.loads(gh("api", "repos/%s/actions/runs/%s/artifacts?name=%s" % (repo, r["databaseId"], ARTIFACT)))["artifacts"]
        except Exception as exc:   # noqa: BLE001
            print("run %s: %s" % (r["databaseId"], exc))
            continue
        if any(not a["expired"] for a in arts):
            print("run %s (%s, %s): %s available" % (r["databaseId"], r["createdAt"], r["conclusion"], ARTIFACT))
            out.append(str(r["databaseId"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True, help="breakpad id of xul.dll in the profile")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--run", default="")
    ap.add_argument("--repo", default="oss-invisible/firefox_antidetect_patch")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    try:
        runs = candidates(args.repo, args.run)
    except Exception as exc:   # noqa: BLE001
        print("SYMBOLS: none (listing the runs failed: %s)" % exc)
        return 0
    if not runs:
        print("SYMBOLS: none (no run of release.yml has an unexpired %s)" % ARTIFACT)
        return 0
    for run in runs[:MAX_DOWNLOADS]:
        tmp = args.out / ("dl-" + run)
        try:
            gh("run", "download", run, "-R", args.repo, "-n", ARTIFACT, "-D", str(tmp))
        except Exception as exc:   # noqa: BLE001
            print("run %s: download failed: %s" % (run, exc))
            shutil.rmtree(tmp, ignore_errors=True)
            continue
        for z in sorted(tmp.rglob("*.zip")):
            try:
                names = zipfile.ZipFile(z).namelist()
            except Exception as exc:   # noqa: BLE001
                print("run %s: %s unreadable: %s" % (run, z.name, exc))
                continue
            pdbs = sorted({n.split("/")[1] for n in names if n.startswith("xul.pdb/") and n.count("/") >= 2})
            print("run %s: %s has xul.pdb ids %s" % (run, z.name, pdbs))
            if args.id in pdbs:
                keep = args.out / ("symbols-%s.zip" % run)
                shutil.move(str(z), keep)
                shutil.rmtree(tmp, ignore_errors=True)
                print("SYMBOLS: %s" % keep)
                return 0
        shutil.rmtree(tmp, ignore_errors=True)
    print("SYMBOLS: none (the id %s is in none of the %d artifact(s) tried)" % (args.id, min(len(runs), MAX_DOWNLOADS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
