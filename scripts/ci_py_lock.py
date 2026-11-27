#!/usr/bin/env python3
"""Writes ci/py-lock/gate.in from its sources and compiles ci/py-lock/gate.txt, the lock of the `gate` environment (ci-03).

The engine series' gate installs the wrapper's environment from a wheelhouse resolved once per image, Python and lock, offline: no unpinned `pip install`
anywhere. The lock of that environment lives HERE, because what it must cover is declared here and is read at the wrapper_ref the gate selects. It holds every
THIRD-PARTY distribution the gate installs from the wrapper:

  * the wrapper's dependencies (`[project].dependencies` of pyproject.toml, without `invisible_core`, which is built as a wheel of ours) and its `dev` extra;
  * the dependencies of the core the wrapper pins (`invisible_core==X`: the `[project].dependencies` of the core's pyproject.toml at the tag vX);
  * playwright at scripts/playwright_pin.txt (the stock client the gate drives the engine with) and cryptography at CRYPTOGRAPHY (the lab's PKI);
  * the installer the install-from-index job needs beside pytest (HARNESS: `uv`, which the release test installs the package with and the wait asks), so that
    job installs its test harness from this lock and from nothing else (tests/test_the_install_job_has_every_module_its_tests_import.py).

One command regenerates both files after a dependency change or a bump of the core pin:

    python scripts/ci_py_lock.py --core-pyproject <pyproject.toml of the core at the tag vX> [--exclude-newer 2026-11-25T00:00:00Z] [--uv uv]

The resolver is the series' (`uv pip compile --universal --generate-hashes --no-build`, every file of every version hashed), so the same input gives the same
bytes there and here. tests/test_the_gate_lock_follows_the_sources.py reads the committed files with this module, offline.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK_DIR = ROOT / "ci" / "py-lock"
IN_PATH = LOCK_DIR / "gate.in"
TXT_PATH = LOCK_DIR / "gate.txt"
PYPROJECT = ROOT / "pyproject.toml"
PLAYWRIGHT_PIN = ROOT / "scripts" / "playwright_pin.txt"

#: What the lab's PKI needs, as the series' gate installs it today.
CRYPTOGRAPHY = "cryptography==44.0.3"
#: Installers the install-from-index job (user-install.yml) needs and no dependency of the wrapper brings: the README's way in (`uv`) and the wait on the index.
HARNESS = ["uv>=0.5"]
#: The floor of the lock: the Python the unit job judges; `--universal` covers every Linux and Windows wheel of every Python from it.
PYTHON_VERSION = "3.11"
DEFAULT_EXCLUDE_NEWER = "2026-11-25T00:00:00Z"
#: The separator of the core's requirements in a header line (a requirement can contain commas).
SEP = " | "

HASH_RE = re.compile(r"^--hash=sha256:[0-9a-f]{64}$")
PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,._-]+\])?)==([A-Za-z0-9][A-Za-z0-9.!+_-]*)(?: ; (.+))?$")
CORE_PIN_RE = re.compile(r"^invisible[-_]core==([0-9][0-9A-Za-z.!+_-]*)$")


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def base_name(requirement: str) -> str:
    return norm(re.split(r"[\[<>=!~ ;]", requirement, maxsplit=1)[0])


def sha256_text(text: str) -> str:
    """sha256 of the text with LF line ends: the bytes the index stores, whatever a Windows checkout did to them."""
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def read_in(text: str) -> dict:
    """The input file: `# key: value` header lines, then one requirement per line."""
    meta, reqs = {}, []
    for line in text.replace("\r\n", "\n").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^#\s*([a-z-]+):\s*(.*)$", line)
        if m:
            meta[m.group(1)] = m.group(2).strip()
        elif not line.startswith("#"):
            reqs.append(line)
    return {"meta": meta, "requirements": reqs}


def parse_lock(text: str) -> list:
    """[(name, version, marker, [hashes])] of a lock. A line that is not `name==version [; marker]` with hashes under it is an error: a range, a URL, an
    editable, an index option, an entry with no hash."""
    entries, current = [], None
    for lineno, raw in enumerate(text.replace("\r\n", "\n").splitlines(), 1):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        body = line.strip()
        if body.endswith("\\"):
            body = body[:-1].rstrip()
        if body.startswith("--hash="):
            if current is None or not HASH_RE.match(body):
                raise ValueError("line %d: a hash that is not sha256:<64 hex>, or none to hold it: %r" % (lineno, body))
            current[3].append(body.split(":", 1)[1])
            continue
        m = PIN_RE.match(body)
        if not m or line[0] in " \t":
            raise ValueError("line %d: not `name==version`: %r" % (lineno, body))
        current = (m.group(1), m.group(2), m.group(3) or "", [])
        entries.append(current)
    for name, version, _, hashes in entries:
        if not hashes:
            raise ValueError("%s==%s has no --hash" % (name, version))
    return entries


# ── the sources ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def wrapper_sources(pyproject_text: str) -> dict:
    """What pyproject.toml declares: the core pin, the other dependencies, the dev extra."""
    project = tomllib.loads(pyproject_text)["project"]
    pins = [d for d in project["dependencies"] if CORE_PIN_RE.match(d.strip())]
    if len(pins) != 1:
        raise SystemExit("pyproject.toml must carry exactly one `invisible_core==X` pin, found %r" % pins)
    return {
        "core_version": CORE_PIN_RE.match(pins[0].strip()).group(1),
        "dependencies": [d for d in project["dependencies"] if d != pins[0]],
        "dev": list(project["optional-dependencies"]["dev"]),
    }


def core_dependencies(core_pyproject_text: str) -> list:
    return list(tomllib.loads(core_pyproject_text)["project"]["dependencies"])


def expected_requirements(pyproject_text: str, core_requires: list, playwright_pin: str) -> list:
    """The requirements the lock must cover, in the order the input prints them."""
    src = wrapper_sources(pyproject_text)
    reqs = set(src["dependencies"]) | set(src["dev"]) | set(core_requires) | set(HARNESS) | {"playwright==%s" % playwright_pin.strip(), CRYPTOGRAPHY}
    return sorted(reqs, key=lambda r: (base_name(r), r))


def make_in(pyproject_text: str, core_pyproject_text: str, playwright_pin: str, exclude_newer: str) -> str:
    src = wrapper_sources(pyproject_text)
    core_requires = core_dependencies(core_pyproject_text)
    head = [
        "# Input of the gate lock; `python scripts/ci_py_lock.py` writes it and gate.txt from the sources below. Wrapper-owned (ci-03), read at the wrapper_ref.",
        "# python-version: %s" % PYTHON_VERSION,
        "# exclude-newer: %s" % exclude_newer,
        "# wrapper: pyproject.toml sha256 %s (at generation); its dependencies without invisible_core, and its `dev` extra" % sha256_text(pyproject_text),
        "# core: invisible_core==%s, tag v%s, pyproject.toml sha256 %s (at generation); the core is a wheel of ours, only what it depends on is locked" % (
            src["core_version"], src["core_version"], sha256_text(core_pyproject_text)),
        "# core-requires: %s" % SEP.join(core_requires),
        "# playwright: %s from scripts/playwright_pin.txt (the stock client the gate drives the engine with)" % playwright_pin.strip(),
        "# cryptography: %s (the lab's PKI)" % CRYPTOGRAPHY.split("==")[1],
        "# harness: %s (the installers of the install-from-index job)" % SEP.join(HARNESS),
    ]
    return "\n".join(head + expected_requirements(pyproject_text, core_requires, playwright_pin)) + "\n"


# ── the lock ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def lock_header(in_text: str, meta: dict) -> str:
    return "\n".join([
        "# Generated by `python scripts/ci_py_lock.py`: do not edit. Wrapper-owned (ci-03).",
        "# input: ci/py-lock/gate.in sha256 %s" % sha256_text(in_text),
        "# resolver: uv pip compile --universal --generate-hashes --no-build --python-version %s --exclude-newer %s" % (meta["python-version"], meta["exclude-newer"]),
        "# Every file of each version is hashed (Linux and Windows wheels of every Python from %s, and the sdist): one lock for every OS." % meta["python-version"],
        "# A consumer installs it with: pip install --no-index --find-links <wheelhouse> --require-hashes -r <this file>",
    ]) + "\n"


def normalise(raw: str, header: str) -> str:
    """What uv printed (no header, no annotations) as a lock: entries by name, hashes sorted, the given header. Same input, same bytes."""
    entries = sorted(parse_lock(raw), key=lambda e: (norm(e[0]), e[1], e[2]))
    lines = [header.rstrip("\n")]
    for name, version, marker, hashes in entries:
        lines.append("%s==%s%s \\" % (name, version, (" ; " + marker) if marker else ""))
        hs = sorted(set(hashes))
        lines += ["    --hash=sha256:%s%s" % (h, " \\" if i < len(hs) - 1 else "") for i, h in enumerate(hs)]
    return "\n".join(lines) + "\n"


def compile_lock(in_text: str, uv: str = "uv") -> str:
    parsed = read_in(in_text)
    meta = parsed["meta"]
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.txt"
        src.write_text("\n".join(parsed["requirements"]) + "\n", encoding="utf-8")
        out = subprocess.run([uv, "pip", "compile", str(src), "--universal", "--generate-hashes", "--no-build", "--no-header", "--no-annotate", "--quiet",
                              "--python-version", meta["python-version"], "--exclude-newer", meta["exclude-newer"]],
                             check=True, capture_output=True, text=True).stdout
    return normalise(out, lock_header(in_text, meta))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--core-pyproject", required=True, help="pyproject.toml of the core at the tag of the pin (invisible_core==X -> tag vX)")
    ap.add_argument("--exclude-newer", default=None, help="ignore releases newer than this (default: the one of the committed gate.in, else %s)" % DEFAULT_EXCLUDE_NEWER)
    ap.add_argument("--uv", default="uv", help="the uv executable")
    a = ap.parse_args(argv)
    exclude = a.exclude_newer
    if exclude is None:
        exclude = read_in(IN_PATH.read_text(encoding="utf-8"))["meta"].get("exclude-newer") if IN_PATH.exists() else None
    in_text = make_in(PYPROJECT.read_text(encoding="utf-8"), Path(a.core_pyproject).read_text(encoding="utf-8"),
                      PLAYWRIGHT_PIN.read_text(encoding="utf-8"), exclude or DEFAULT_EXCLUDE_NEWER)
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    IN_PATH.write_text(in_text, encoding="utf-8", newline="\n")
    txt = compile_lock(in_text, a.uv)
    TXT_PATH.write_text(txt, encoding="utf-8", newline="\n")
    print("ci/py-lock/gate.in and gate.txt written (%d direct requirements, %d locked distributions)" % (len(read_in(in_text)["requirements"]), len(parse_lock(txt))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
