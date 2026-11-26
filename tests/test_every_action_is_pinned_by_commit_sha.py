"""Every `uses:` of every workflow is a local path or a full commit SHA.

A tag (`@v4`) or a branch (`@main`, `@release/v1`) is a name the owner of the action can move: the next run of a workflow that publishes to PyPI (or
that holds a token) would run code nobody here read. A commit SHA cannot move. So every `uses:` in `.github/workflows/*.yml` is either

  * a local path (`./.github/workflows/x.yml`), or
  * `owner/repo[/path]@<40 hex>` followed by a trailing comment that names what it was resolved from (`# v4 (v4.4.0)`; for a reusable workflow of another repository, the branch and why).

The SHA is resolved from the official tag through the API (`gh api repos/<owner>/<repo>/git/ref/tags/<tag>`, an annotated tag peeled with
`gh api repos/<owner>/<repo>/git/tags/<sha>`), never typed by hand.

The failure reads `FAIL <file> / <job> / <step>: <what>. Fix: <how>. Rule: <rule>`. The checker is also run on the text of the workflows as they were
(tags, a branch, a cross-repository reusable on `@main`), and it must be red on each.
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

RULE = "actions pinned by commit SHA"
FIX = ("resolve the tag through the API (`gh api repos/<owner>/<repo>/git/ref/tags/<tag>`, peel an annotated tag with "
       "`gh api repos/<owner>/<repo>/git/tags/<sha>`), write `@<40 hex>` and the version in a trailing comment (`# v4 (v4.4.0)`)")

USES = re.compile(r"^(?P<indent>\s*)(?:-\s+)?uses:\s*(?P<ref>[^\s#]+)\s*(?P<comment>#.*)?$")
PINNED = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[^@\s]+)?@[0-9a-f]{40}$")
JOB = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
STEP_START = re.compile(r"^(\s*)-\s")
STEP_NAME = re.compile(r"^\s*(?:-\s+)?name:\s*(.+?)\s*$")


def _where(lines, at):
    """The job and the step of the line `at`, as the failure names them."""
    job = "(no job)"
    for i in range(at, -1, -1):
        m = JOB.match(lines[i])
        if m:
            job = m.group(1)
            break
    indent = len(USES.match(lines[at]).group("indent"))
    if lines[at].lstrip().startswith("- "):
        start = at
    else:
        start = None
        for i in range(at - 1, -1, -1):
            m = STEP_START.match(lines[i])
            if m and len(m.group(1)) == indent - 2:
                start = i
                break
    if start is None:
        return job, "(the job calls a reusable workflow)"
    step = None
    i = start
    while i < len(lines):
        if i > start and STEP_START.match(lines[i]) and len(STEP_START.match(lines[i]).group(1)) <= len(STEP_START.match(lines[start]).group(1)):
            break
        m = STEP_NAME.match(lines[i])
        if m:
            step = m.group(1)
            break
        i += 1
    return job, step or "(unnamed step)"


def unpinned(name, text):
    """One `FAIL ...` line per `uses:` of the workflow text that is not local and not a full SHA with its version comment."""
    out = []
    lines = text.splitlines()
    for at, line in enumerate(lines):
        m = USES.match(line)
        if not m:
            continue
        ref = m.group("ref").strip("'\"")
        if ref.startswith("./"):
            continue
        job, step = _where(lines, at)
        if not PINNED.match(ref):
            what = f"`uses: {ref}` is not pinned to a 40-hex commit SHA"
        elif not m.group("comment") or not m.group("comment").lstrip("#").strip():
            what = f"`uses: {ref}` has no trailing comment that names the version it was resolved from"
        else:
            continue
        out.append(f"FAIL {name} / {job} / {step}: {what}. Fix: {FIX}. Rule: {RULE}")
    return out


def test_every_uses_of_every_workflow_is_a_local_path_or_a_commit_sha():
    assert WORKFLOWS, "no workflow found under .github/workflows"
    failures = []
    for path in WORKFLOWS:
        failures += unpinned(path.name, path.read_text(encoding="utf-8"))
    assert not failures, "\n" + "\n".join(failures)


SHA = "11d5960a326750d5838078e36cf38b85af677262"
OLD_WORKFLOWS = {
    "a tag": f"""
jobs:
  unit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
""",
    "a branch": """
jobs:
  publish:
    steps:
      - uses: pypa/gh-action-pypi-publish@release/v1
""",
    "a cross-repository reusable on main": """
jobs:
  identity:
    uses: oss-invisible/core/.github/workflows/noreply-identity.yml@main
""",
    "a short sha": """
jobs:
  unit:
    steps:
      - name: Check out
        uses: actions/checkout@11d5960
""",
    "a sha without its version": f"""
jobs:
  unit:
    steps:
      - name: Check out
        uses: actions/checkout@{SHA}
""",
}


@pytest.mark.parametrize("kind", sorted(OLD_WORKFLOWS))
def test_the_checker_is_red_on_the_workflows_as_they_were(kind):
    found = unpinned("old.yml", OLD_WORKFLOWS[kind])
    assert found, f"the checker accepted {kind}"
    for line in found:
        assert line.startswith("FAIL old.yml / ") and f"Rule: {RULE}" in line and "Fix: resolve the tag through the API" in line


def test_the_failure_names_file_job_step_and_the_offending_uses():
    found = unpinned("old.yml", OLD_WORKFLOWS["a tag"])
    assert [f.split(": ", 1)[0] for f in found] == ["FAIL old.yml / unit / (unnamed step)", "FAIL old.yml / unit / Set up Python"]
    assert "`uses: actions/checkout@v4`" in found[0] and "`uses: actions/setup-python@v5`" in found[1]
    assert unpinned("old.yml", OLD_WORKFLOWS["a cross-repository reusable on main"])[0].startswith(
        "FAIL old.yml / identity / (the job calls a reusable workflow): `uses: oss-invisible/core/.github/workflows/noreply-identity.yml@main`")


def test_the_checker_accepts_a_local_path_and_a_pinned_action():
    ok = f"""
jobs:
  a:
    uses: ./.github/workflows/linux-deps.yml
  b:
    steps:
      - uses: actions/checkout@{SHA} # v4 (v4.4.0)
      - name: x
        uses: oss-invisible/core/.github/workflows/y.yml@{SHA} # core main, the last change of y.yml
"""
    assert unpinned("ok.yml", ok) == []


# A reusable workflow of another repository runs its own `uses:` inside our run: pinning the caller is not enough if the file at that commit still
# calls a tag. Each one is vendored here, byte for byte, at the commit our callers pin, with its git blob id (what
# `gh api repos/<owner>/<repo>/contents/<path>?ref=<commit> --jq .sha` returns), and the same rule is applied to it.
VENDORED = Path(__file__).resolve().parent / "pinned"
REUSABLE = {
    # uses (without @) -> (commit our callers pin, git blob id of the file at that commit, vendored copy)
    "oss-invisible/core/.github/workflows/noreply-identity.yml": ("1baf9479b288a2563010945ca9233c8b1db91202",
                                                                  "0fb97825b60feae542d90f593c9921b598a55e0c", "core-noreply-identity.yml"),
}


def _blob_id(data):
    import hashlib
    data = data.replace(b"\r\n", b"\n")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def reusable_problems(workflows=WORKFLOWS):
    out = []
    for path in workflows:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = USES.match(line)
            if not m:
                continue
            ref = m.group("ref").strip("'\"")
            if ref.startswith("./") or "/.github/workflows/" not in ref:
                continue
            target, _, commit = ref.partition("@")
            if target not in REUSABLE:
                out.append(f"FAIL {path.name}: `uses: {ref}` is a reusable workflow of another repository with no vendored copy. Fix: vendor the "
                           f"file at that commit in tests/pinned/ and add it to REUSABLE with its blob id. Rule: {RULE}")
                continue
            pinned, blob, copy = REUSABLE[target]
            if commit != pinned:
                out.append(f"FAIL {path.name}: calls {target} at {commit} but the vendored copy is of {pinned}. Fix: vendor the file at the new "
                           f"commit and update REUSABLE. Rule: {RULE}")
            data = (VENDORED / copy).read_bytes()
            if _blob_id(data) != blob:
                out.append(f"FAIL tests/pinned/{copy}: its blob id is {_blob_id(data)}, not {blob}, the file at {pinned}. Fix: copy it again byte "
                           f"for byte from that commit. Rule: {RULE}")
            out += unpinned(f"{target}@{pinned[:10]}", data.decode("utf-8"))
    return out


def test_every_reusable_workflow_we_call_pins_what_it_uses_at_the_commit_we_call():
    found = reusable_problems()
    assert not found, "\n".join(found)


def test_a_reusable_workflow_that_calls_a_tag_inside_is_refused():
    inside = "on: workflow_call\njobs:\n  j:\n    runs-on: ubuntu-24.04\n    steps:\n      - uses: actions/checkout@v4\n"
    assert unpinned("core-noreply-identity.yml", inside)
