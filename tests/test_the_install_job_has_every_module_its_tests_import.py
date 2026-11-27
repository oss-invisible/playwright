r"""The install-from-index job installs its test harness from the wrapper's hashed lock, and the lock holds every third-party module the tests import.

Ledger PR #618 (release 0.60.0) was red on `windows-latest - install from index and launch` with ``ModuleNotFoundError: No module named 'psutil'`` at
tests/_os_cursor.py: the module came with #609, and the step "Test harness only" of ``.github/workflows/user-install.yml`` installed a hand list
(``pip install --upgrade pip pytest packaging uv``). That job runs only on ledger pull requests, so nothing else caught it, and a hand list drifts by construction:
every new import of a test is a line somebody has to remember in a second place.

The harness now comes from ``ci/py-lock/gate.txt`` (ci-03: the index is read only through hashed locks), the lock the other test jobs install, with
``--require-hashes`` and nothing else on the line. The lock holds ``uv`` (the wait on the index asks it, and the release test installs the package with it) and
neither invisible-playwright nor invisible-core: the test installs the package under test from the index into a venv of its own, and a second copy on the path
would make the assertions read the wrong one.

This test closes the circle without the network:

* the step of the harness installs exactly ``--require-hashes -r ci/py-lock/gate.txt`` (no package named on the line, no ``--upgrade``), before the wait and before
  the tests, and no other step of the job runs ``pip install``;
* the lock holds neither of our two packages;
* every third-party top-level module any file under tests/ imports (found with ``ast``; the standard library, the repository's own modules and our two packages are
  not third-party) belongs to a distribution the lock holds. An import guarded by ``try/except ImportError`` is optional: it is listed (so a new optional import is
  a decision made here, in OPTIONAL) and is not required.

Known-bad inputs, run on the parsers: the hand list of #618, a package named beside the lock, no ``--require-hashes``, a second ``pip install`` in the job, a lock
without psutil, an import the lock does not cover. The green case is the state of the repository.
"""
from __future__ import annotations

import ast
import importlib.metadata
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import ci_py_lock as lock  # noqa: E402

WORKFLOW = ROOT / ".github" / "workflows" / "user-install.yml"
LOCK = "ci/py-lock/gate.txt"
HARNESS_STEP = "Test harness only"
JOB = "install"

#: The packages the job must not install in its harness: the test installs them itself, from the index, into its own venv.
UNDER_TEST = {"invisible-playwright", "invisible-core"}
OWN_MODULES = {"invisible_playwright", "invisible_core"}

#: Third-party modules the tests import only inside ``try/except ImportError``: not required in the lock. A new one is added here on purpose.
OPTIONAL = {"playwright"}

#: Module -> distribution for the names whose distribution cannot be read from the environment (a module not installed where this runs, or a private module of
#: a distribution). ``importlib.metadata.packages_distributions()`` is asked first.
KNOWN_DISTRIBUTIONS = {
    "_pytest": "pytest", "pytest": "pytest", "aioquic": "aioquic", "cryptography": "cryptography", "h2": "h2", "hyperframe": "hyperframe",
    "maxminddb": "maxminddb", "packaging": "packaging", "platformdirs": "platformdirs", "playwright": "playwright", "psutil": "psutil",
    "requests": "requests", "websockets": "websockets",
}

PIP_INSTALL = re.compile(r"\bpip3?\s+install\b")


# ── the workflow ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _job_steps(text: str, job: str) -> list[tuple[str, str]]:
    """[(name, the lines of the step without comments)] of the steps of a job, read line by line (PyYAML is not a dependency of this package)."""
    lines = text.replace("\r\n", "\n").split("\n")
    start = next(i for i, line in enumerate(lines) if line == "jobs:")
    inside, steps = False, []
    for line in lines[start + 1:]:
        m = re.match(r"^  ([\w-]+):\s*$", line)
        if m:
            inside = m.group(1) == job
            continue
        if not inside:
            continue
        s = re.match(r"^      - (.*)$", line)
        if s:
            steps.append([s.group(1).strip(), []])
            line = "        " + s.group(1)
        if steps and line.strip() and not line.lstrip().startswith("#"):
            steps[-1][1].append(line.strip())
    out = []
    for first, body in steps:
        name = re.match(r"name:\s*(.*)$", first)
        out.append((name.group(1).strip("\"'") if name else first, "\n".join(body)))
    return out


def _run_text(body: str) -> str:
    """What a step runs: the `run:` value, inline or as a block."""
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("run:"):
            value = line[4:].strip()
            return "\n".join(lines[i + 1:]) if value in ("|", ">") else value
    return ""


def workflow_problems(text: str) -> list[str]:
    out = []
    steps = _job_steps(text, JOB)
    names = [n for n, _ in steps]
    if names.count(HARNESS_STEP) != 1:
        return ["the job %r has %d steps named %r, expected one" % (JOB, names.count(HARNESS_STEP), HARNESS_STEP)]
    index = names.index(HARNESS_STEP)
    run = " ".join(_run_text(steps[index][1]).split())
    m = PIP_INSTALL.search(run)
    if not m:
        out.append("the harness step does not run `pip install`: %r" % run)
    else:
        tail = run[m.end():].split()
        if sorted(tail) != sorted(["--require-hashes", "-r", LOCK]) or tail[tail.index("-r") + 1:][:1] != [LOCK]:
            out.append("the harness step installs %r, expected exactly `--require-hashes -r %s` (no package named, no --upgrade, no other option)" % (" ".join(tail), LOCK))
    if "python -m pip install" not in run:
        out.append("the harness step must be `python -m pip install ...` (the pip of the interpreter the tests run with): %r" % run)
    # nothing else of the job installs from the index: the wait and the tests run with what the harness put there
    for i, (name, body) in enumerate(steps):
        if i != index and PIP_INSTALL.search(body):
            out.append("step %r also runs `pip install`: the job reads the index only through the lock" % name)
    # the harness is in place before the first step that needs it
    for needs in ("wait for the index to serve this version", "The user path, end to end"):
        if needs not in names:
            out.append("the job has no step %r" % needs)
        elif names.index(needs) < index:
            out.append("step %r runs before the harness is installed" % needs)
    return out


# ── the lock ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def locked(lock_text: str) -> set[str]:
    return {lock.norm(name.split("[")[0]) for name, _v, _m, _h in lock.parse_lock(lock_text)}


def lock_problems(lock_text: str) -> list[str]:
    held = locked(lock_text)
    return ["the lock holds %s, which the test installs from the index itself into its own venv" % n for n in sorted(held & UNDER_TEST)]


# ── the imports of the tests ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def own_modules(root: Path) -> set[str]:
    """Modules and packages the repository itself provides to its tests: anything under tests/, the scripts the tests import, the package under src/."""
    names: set[str] = set(OWN_MODULES) | {"tests"}
    for base in ("tests", "scripts", "src"):
        folder = root / base
        if not folder.is_dir():
            continue
        if base == "tests":
            names |= {p.stem for p in folder.rglob("*.py")} | {p.name for p in folder.rglob("*") if p.is_dir()}
        else:
            names |= {p.stem for p in folder.glob("*.py")} | {p.name for p in folder.iterdir() if p.is_dir()}
    return names


def _guards_import_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(k, ast.Name) and k.id in {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"} for k in kinds)


def third_party_imports(root: Path) -> dict[str, dict[str, bool]]:
    """{top-level module: {file: guarded}} for every import under tests/ that is neither standard library nor ours. ``guarded`` is True when the import
    sits in a ``try`` whose handlers catch ImportError."""
    own = own_modules(root)
    found: dict[str, dict[str, bool]] = {}
    for path in sorted((root / "tests").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        guarded: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Try) and any(_guards_import_error(h) for h in node.handlers):
                guarded |= {id(n) for b in node.body for n in ast.walk(b) if isinstance(n, (ast.Import, ast.ImportFrom))}
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module.split(".")[0]]
            else:
                continue
            for mod in mods:
                if mod in sys.stdlib_module_names or mod in own:
                    continue
                found.setdefault(mod, {})[rel] = found.get(mod, {}).get(rel, True) and id(node) in guarded
    return found


def distribution_of(module: str) -> str | None:
    dists = importlib.metadata.packages_distributions().get(module)
    if dists:
        return lock.norm(dists[0])
    return lock.norm(KNOWN_DISTRIBUTIONS[module]) if module in KNOWN_DISTRIBUTIONS else None


def import_problems(imports: dict[str, dict[str, bool]], lock_text: str) -> list[str]:
    held = locked(lock_text)
    out = []
    for module, files in sorted(imports.items()):
        optional = all(files.values())
        if optional and module not in OPTIONAL:
            out.append("%s is imported only inside try/except ImportError (%s) but is not in OPTIONAL: add it there on purpose" % (module, sorted(files)[0]))
        if optional:
            continue
        dist = distribution_of(module)
        if dist is None:
            out.append("%s (imported by %s) belongs to no known distribution: add it to KNOWN_DISTRIBUTIONS" % (module, sorted(files)[0]))
        elif dist not in held:
            out.append("%s (distribution %s, imported by %s) is not in %s: the install job would fail with ModuleNotFoundError" % (module, dist, sorted(files)[0], LOCK))
    return out


# ── the state of the repository ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_harness_step_installs_the_hashed_lock_and_nothing_else():
    assert workflow_problems(WORKFLOW.read_text(encoding="utf-8")) == []


def test_the_lock_holds_neither_package_under_test():
    assert lock_problems((ROOT / LOCK).read_text(encoding="utf-8")) == []


def test_the_lock_holds_uv_which_the_wait_and_the_release_test_use():
    assert "uv" in locked((ROOT / LOCK).read_text(encoding="utf-8"))


def test_every_third_party_module_the_tests_import_is_in_the_lock():
    imports = third_party_imports(ROOT)
    assert "psutil" in imports and "pytest" in imports, "the scan found no psutil or pytest: it does not see the tests"
    assert import_problems(imports, (ROOT / LOCK).read_text(encoding="utf-8")) == []


def test_psutil_of_ledger_618_is_covered_by_the_lock_the_step_installs():
    lock_text = (ROOT / LOCK).read_text(encoding="utf-8")
    assert "psutil" in third_party_imports(ROOT)
    assert "psutil" in locked(lock_text)
    assert "psutil" in {distribution_of(m) for m in third_party_imports(ROOT)}


# ── known-bad inputs ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _workflow(harness_run: str, extra: str = "") -> str:
    return f"""name: user-install
jobs:
  linux-deps-user-install:
    uses: ./.github/workflows/linux-deps.yml
  install:
    steps:
      - uses: actions/checkout@abc # v4
      # a comment between the steps
      - name: Test harness only
        run: {harness_run}
{extra}      - name: wait for the index to serve this version
        shell: bash
        run: |
          python - <<'PY'
          print("pip download")
          PY
      - name: The user path, end to end
        run: |
          python -m pytest tests/test_release_e2e.py -m e2e
"""


GOOD_RUN = "python -m pip install --require-hashes -r ci/py-lock/gate.txt"


def test_a_workflow_with_the_lock_step_is_clean():
    assert workflow_problems(_workflow(GOOD_RUN)) == []


@pytest.mark.parametrize("run, what", [
    ("python -m pip install --upgrade pip pytest packaging uv", "the hand list of ledger #618"),
    (GOOD_RUN + " pytest", "a package named beside the lock"),
    (GOOD_RUN + " --upgrade", "an option beside the lock"),
    ("python -m pip install -r ci/py-lock/gate.txt", "no --require-hashes"),
    ("python -m pip install --require-hashes -r requirements.txt", "another file"),
    ("python -m pip install --require-hashes -r ci/py-lock/gate.txt invisible-playwright", "the package under test"),
    ("pip install --require-hashes -r ci/py-lock/gate.txt", "not the pip of the interpreter"),
    ("echo nothing", "no install at all"),
])
def test_a_harness_step_that_is_not_the_lock_alone_is_refused(run, what):
    assert workflow_problems(_workflow(run)), what


def test_a_second_install_in_the_job_is_refused():
    extra = "      - name: one more\n        run: python -m pip install pyyaml\n"
    assert any("one more" in p for p in workflow_problems(_workflow(GOOD_RUN, extra)))


def test_a_harness_after_the_step_that_needs_it_is_refused():
    text = _workflow(GOOD_RUN)
    harness = "      - name: Test harness only\n        run: " + GOOD_RUN + "\n"
    moved = text.replace(harness, "") + harness
    assert any("runs before the harness" in p for p in workflow_problems(moved))


def test_a_lock_that_holds_the_package_under_test_is_refused():
    text = (ROOT / LOCK).read_text(encoding="utf-8") + "invisible-core==1.0.0 \\\n    --hash=sha256:" + "0" * 64 + "\n"
    assert lock_problems(text) and "invisible-core" in lock_problems(text)[0]


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tmp_path


def test_the_scan_keeps_third_party_and_drops_standard_library_own_and_relative(tmp_path):
    root = _tree(tmp_path, {
        "tests/test_a.py": "import os\nimport psutil\nfrom helper import x\nfrom . import y\nimport invisible_core\nfrom invisible_playwright.sync_api import z\nimport scriptlib\n",
        "tests/helper.py": "import requests.adapters\n",
        "scripts/scriptlib.py": "",
        "src/invisible_playwright/__init__.py": "",
    })
    assert {m: sorted(f) for m, f in third_party_imports(root).items()} == {"psutil": ["tests/test_a.py"], "requests": ["tests/helper.py"]}


def test_a_guarded_import_is_optional_and_an_unguarded_one_anywhere_is_required(tmp_path):
    root = _tree(tmp_path, {
        "tests/test_a.py": "try:\n    import playwright\nexcept ImportError:\n    playwright = None\ntry:\n    import psutil\nexcept ModuleNotFoundError:\n    psutil = None\n",
        "tests/test_b.py": "import psutil\n",
    })
    found = third_party_imports(root)
    assert found["playwright"] == {"tests/test_a.py": True}
    assert found["psutil"] == {"tests/test_a.py": True, "tests/test_b.py": False}


def test_an_import_the_lock_does_not_cover_is_named_with_its_file(tmp_path):
    root = _tree(tmp_path, {"tests/test_a.py": "import psutil\n"})
    lock_text = (ROOT / LOCK).read_text(encoding="utf-8")
    without = re.sub(r"(?ms)^psutil==.*?(?=^\S)", "", lock_text)
    assert "psutil" not in locked(without) and "psutil" in locked(lock_text)
    problems = import_problems(third_party_imports(root), without)
    assert len(problems) == 1 and "psutil" in problems[0] and "tests/test_a.py" in problems[0]
    assert import_problems(third_party_imports(root), lock_text) == []


def test_a_module_of_no_known_distribution_is_named(tmp_path):
    root = _tree(tmp_path, {"tests/test_a.py": "import a_module_nobody_ships\n"})
    problems = import_problems(third_party_imports(root), (ROOT / LOCK).read_text(encoding="utf-8"))
    assert len(problems) == 1 and "a_module_nobody_ships" in problems[0]


def test_an_optional_import_not_listed_as_optional_is_a_decision_to_make(tmp_path):
    root = _tree(tmp_path, {"tests/test_a.py": "try:\n    import brotli\nexcept ImportError:\n    brotli = None\n"})
    problems = import_problems(third_party_imports(root), (ROOT / LOCK).read_text(encoding="utf-8"))
    assert len(problems) == 1 and "OPTIONAL" in problems[0]
