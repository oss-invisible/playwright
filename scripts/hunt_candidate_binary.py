#!/usr/bin/env python3
"""The engine of an ENGINE CANDIDATE for a hunt, instead of the published one.

A candidate is a run of the `release` workflow of oss-invisible/firefox_antidetect_patch (kind=candidate): it builds both legs and
uploads each as the artifact `asset-<leg>`. A hunt branch names the run in two one-line files, written when the hunt is launched:

    ci/candidate-run.txt        the id of that run
    ci/candidate-core-ref.txt   the ref of oss-invisible/core the candidate was gated with (its `core_ref` input)

Both hold `TODO` until then, and every command here refuses `TODO` first, so a push before the launch fails at once and says why.

    python scripts/hunt_candidate_binary.py check                  # both files are filled in: exit 1 and a message if not
    python scripts/hunt_candidate_binary.py core-ref               # prints the core ref
    python scripts/hunt_candidate_binary.py verify-core            # the installed core IS the git install of that ref (as gate.yml asserts)
    python scripts/hunt_candidate_binary.py seal-tag               # prints the tag the candidate is sealed under, firefox-N-candidate
    python scripts/hunt_candidate_binary.py fetch --leg win-x86_64 --dest DIR
                                                                   # downloads asset-<leg>, extracts it, seals the binary, exports the env

`fetch` does what the gate of the candidate does with its own artifact (gate.yml of the engine repository): the one
`firefox-*-stealth-<leg>.*` archive of the artifact is extracted (zip on Windows, tar.gz on Linux), and
`python -m invisible_core seal --binary <exe> --tag <seal tag>` writes the seal the wrapper's launch is checked under, and DEST/candidate.json
says which run, core ref, seal tag and bytes the hunt ran on. It then writes to
$GITHUB_ENV (when set) and prints: FF and INVPW_BINARY_PATH (the binary) and INVISIBLE_SEAL_FILE (the seal).

The seal tag is the one the plan job of the candidate's run printed (`seal_tag=firefox-N-candidate`); when that log cannot be read it is
the last published `firefox-N` tag plus one, which is how the plan makes it, and the line says so.

Needs the GitHub CLI with GH_TOKEN (the engine repository is public: the token of the workflow reads its artifacts and logs).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE_REPO = "oss-invisible/firefox_antidetect_patch"
CORE_URL = "https://github.com/oss-invisible/core"
RUN_FILE = ROOT / "ci" / "candidate-run.txt"
CORE_FILE = ROOT / "ci" / "candidate-core-ref.txt"
LEGS = ("win-x86_64", "linux-x86_64")


class Refused(Exception):
    """What the hunt cannot start without; the message is the whole explanation."""


def _line(path: Path, what: str) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Refused("%s cannot be read: %s" % (path.relative_to(ROOT).as_posix(), exc))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise Refused("%s must hold exactly one line (%s), it holds %d" % (path.relative_to(ROOT).as_posix(), what, len(lines)))
    if lines[0] == "TODO":
        raise Refused("%s says TODO: this hunt has no candidate yet. Write %s there, commit and push again."
                      % (path.relative_to(ROOT).as_posix(), what))
    return lines[0]


def run_id() -> str:
    value = _line(RUN_FILE, "the id of the candidate's run of the release workflow of %s" % ENGINE_REPO)
    if not re.fullmatch(r"[0-9]{6,}", value):
        raise Refused("ci/candidate-run.txt must be a run id (digits only), it says %r" % value)
    return value


def core_ref() -> str:
    value = _line(CORE_FILE, "the core_ref the candidate was gated with (a branch, a tag or a 40-character sha of oss-invisible/core)")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", value) or value.startswith(("-", "/")):
        raise Refused("ci/candidate-core-ref.txt must be one ref of the core, it says %r" % value)
    return value


def _gh(*args: str) -> str:
    done = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode:
        raise Refused("gh %s failed: %s" % (" ".join(args[:3]), (done.stderr or done.stdout).strip()[:400]))
    return done.stdout


def seal_tag(run: str) -> str:
    try:
        jobs = json.loads(_gh("api", "repos/%s/actions/runs/%s/jobs?per_page=100" % (ENGINE_REPO, run)))["jobs"]
        plan = next(j["id"] for j in jobs if j["name"] == "plan")
        log = _gh("api", "--allow-escape-sequences", "repos/%s/actions/jobs/%s/logs" % (ENGINE_REPO, plan))
        found = re.findall(r"seal_tag=(firefox-[0-9]+-candidate)\b", log)
        if found:
            print("seal tag %s (the plan job of run %s)" % (found[-1], run), file=sys.stderr)
            return found[-1]
        raise Refused("the plan job's log has no seal_tag line")
    except (Refused, StopIteration, KeyError, ValueError) as exc:
        print("::warning::the seal tag could not be read from run %s (%s); using the last published tag plus one" % (run, exc), file=sys.stderr)
    rows = subprocess.run(["git", "ls-remote", "--tags", "--refs", "https://github.com/%s" % ENGINE_REPO, "refs/tags/firefox-*"],
                          capture_output=True, text=True, check=True).stdout.splitlines()
    numbers = [int(m.group(1)) for line in rows for m in [re.search(r"refs/tags/firefox-([0-9]+)$", line)] if m]
    if not numbers:
        raise Refused("no firefox-N tag in %s: the number of the candidate is unknown" % ENGINE_REPO)
    return "firefox-%d-candidate" % (max(numbers) + 1)


def verify_core(ref: str) -> None:
    """The installed core is the git install of exactly `ref` (the assertion of gate.yml), read after importing the wrapper."""
    from importlib.metadata import distribution, version

    if re.fullmatch(r"[0-9a-f]{40}", ref):
        want = ref
    else:
        rows = [line.split() for line in subprocess.run(["git", "ls-remote", CORE_URL, ref, ref + "^{}"], capture_output=True, text=True,
                                                        check=True).stdout.splitlines() if line.strip()]
        peeled = [sha for sha, name in rows if name.endswith("^{}")]    # an annotated tag: its commit, not the tag object
        want = (peeled or [sha for sha, _ in rows] or [""])[0]
    import invisible_playwright  # noqa: F401 - the import-time pin repair runs here, as under pytest

    raw = distribution("invisible-core").read_text("direct_url.json")
    got = (json.loads(raw).get("vcs_info") or {}).get("commit_id") if raw else None
    print("core under test: %s from %s" % (version("invisible-core"), got or "the index"))
    if not want or got != want:
        raise Refused("THE CORE UNDER TEST IS NOT %s: installed %s from %s, the ref is %s"
                      % (ref, version("invisible-core"), got or "the index", want or "not a ref of the core"))


def fetch(leg: str, dest: Path) -> dict:
    run = run_id()
    tag = seal_tag(run)
    dest.mkdir(parents=True, exist_ok=True)
    art = dest / "art"
    _gh("run", "download", run, "-R", ENGINE_REPO, "-n", "asset-%s" % leg, "-D", str(art))
    archives = sorted(art.glob("firefox-*-stealth-%s.*" % leg))
    if len(archives) != 1:
        raise Refused("expected one firefox-*-stealth-%s archive in the artifact asset-%s of run %s, found: %s"
                      % (leg, leg, run, [p.name for p in archives] or sorted(p.name for p in art.iterdir())))
    ff = dest / "ff"
    ff.mkdir(exist_ok=True)
    if leg.startswith("win"):
        with zipfile.ZipFile(archives[0]) as z:
            z.extractall(ff)
        exe = ff / "firefox.exe"
    else:
        subprocess.run(["tar", "xzf", str(archives[0]), "-C", str(ff)], check=True)
        exe = ff / "firefox"
        if exe.exists():
            exe.chmod(exe.stat().st_mode | 0o111)
    if not exe.exists():
        raise Refused("the archive %s has no %s at its root" % (archives[0].name, exe.name))
    seal = dest / "candidate.seal.json"
    subprocess.run([sys.executable, "-m", "invisible_core", "seal", "--binary", str(exe), "--tag", tag, "-o", str(seal)], check=True)
    record = {"engine_repo": ENGINE_REPO, "run_id": run, "core_ref": core_ref(), "seal_tag": tag, "leg": leg, "archive": archives[0].name,
              "archive_sha256": hashlib.sha256(archives[0].read_bytes()).hexdigest(),
              "binary_sha256": hashlib.sha256(exe.read_bytes()).hexdigest()}
    (dest / "candidate.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    env = {"FF": exe.resolve().as_posix(), "INVPW_BINARY_PATH": exe.resolve().as_posix(), "INVISIBLE_SEAL_FILE": seal.resolve().as_posix()}
    target = os.environ.get("GITHUB_ENV")
    if target:
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.writelines("%s=%s\n" % kv for kv in env.items())
    for key, value in env.items():
        print("%s=%s" % (key, value))
    print("candidate run %s, archive %s, sealed as %s" % (run, archives[0].name, tag), file=sys.stderr)
    return env


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("core-ref")
    sub.add_parser("verify-core")
    sub.add_parser("seal-tag")
    f = sub.add_parser("fetch")
    f.add_argument("--leg", choices=LEGS, required=True)
    f.add_argument("--dest", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            print("candidate run %s, core ref %s" % (run_id(), core_ref()))
        elif args.command == "core-ref":
            print(core_ref())
        elif args.command == "verify-core":
            verify_core(core_ref())
        elif args.command == "seal-tag":
            print(seal_tag(run_id()))
        else:
            fetch(args.leg, args.dest)
    except Refused as exc:
        print("::error::%s" % exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
