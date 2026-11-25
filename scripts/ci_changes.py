"""Does a change have anything for a heavy workflow to test? The job `changes` of e2e.yml and of fakeweb.yml.

    python scripts/ci_changes.py <e2e|fakeweb> <github.event_name> [BASE_SHA HEAD_SHA]

GitHub applies no `paths` filter to a merge queue run (`merge_group`), so without this a docs-only change would run the whole e2e and the whole fake web in
the queue. In a queue run this lists the files that differ between the base and the head of the entry (`github.event.merge_group.base_sha` and
`head_sha`) and applies the path rules the `push` trigger of the same workflow has, so the queue and main agree on what is worth a run:

    e2e       `paths-ignore`: a change runs it unless EVERY file matches IGNORE (`**.md`, `docs/**`, `LICENSE`);
    fakeweb   `paths`: a change runs it only if SOME file matches ONLY (the fake web, its report, the pin, its workflow).

On any other event (push and workflow_dispatch: a push was already filtered by GitHub, a dispatch is a person asking) the answer is always `run=true`.

Prints `run=true|false` and `files=<n>` (the lines go to $GITHUB_OUTPUT). `run=false` is an explicit answer, "nothing to test": the verdict job of the workflow
(scripts/ci_gate.py) then expects every other job `skipped` and says so in its last line; it is never a skip the verdict waves through. A change that cannot be
listed (a commit that cannot be fetched) is an error: the job is red, and so is the verdict. A change that lists no file at all cannot be told from a broken
diff, so it runs.
"""
from __future__ import annotations

import re
import subprocess
import sys

# e2e.yml `on.push.paths-ignore`: a change of only these files is not a change the browsers can observe (tests/test_the_workflows_are_ready_for_the_merge_queue.py
# holds this table equal to the filter of the workflow).
IGNORE = {
    "e2e": ("**.md", "docs/**", "LICENSE"),
}
# fakeweb.yml `on.push.paths`.
ONLY = {
    "fakeweb": (
        "tests/fakeweb/**",
        "tests/test_fakeweb_*.py",
        "scripts/run_fakeweb.py",
        "scripts/fakeweb_p4_report.py",
        "scripts/playwright_pin.txt",
        ".github/workflows/fakeweb.yml",
    ),
}
WORKFLOWS = tuple(sorted(set(IGNORE) | set(ONLY)))


def glob_regex(pattern: str) -> "re.Pattern[str]":
    """A path filter of a workflow as a regular expression: `**` is any characters, `*` any characters but `/`, `?` one character but `/`."""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        char = pattern[i]
        out.append("[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char))
        i += 1
    return re.compile("".join(out) + r"\Z")


def matches(path: str, patterns) -> bool:
    return any(glob_regex(p).match(path) for p in patterns)


def should_run(workflow: str, files: list) -> bool:
    """Is there something in `files` (the paths a change touches) for `workflow` to test?"""
    if workflow not in WORKFLOWS:
        raise ValueError("unknown workflow %r (one of %s)" % (workflow, ", ".join(WORKFLOWS)))
    if not files:
        return True
    if workflow in IGNORE:
        return any(not matches(f, IGNORE[workflow]) for f in files)
    return any(matches(f, ONLY[workflow]) for f in files)


def changed_files(base: str, head: str, run=subprocess.run) -> list:
    """The files that differ between the base and the head of a queue entry (fetched when the shallow checkout does not have them)."""
    for sha in (base, head):
        if run(["git", "cat-file", "-e", sha + "^{commit}"], capture_output=True, text=True).returncode != 0:
            run(["git", "fetch", "--no-tags", "--depth=1", "origin", sha], check=True, capture_output=True, text=True)
    done = run(["git", "diff", "--name-only", base, head], check=True, capture_output=True, text=True)
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in (2, 4) or args[0] not in WORKFLOWS:
        print("usage: ci_changes.py <%s> EVENT_NAME [BASE_SHA HEAD_SHA]" % "|".join(WORKFLOWS), file=sys.stderr)
        return 2
    workflow, event = args[0], args[1]
    sys.stdout.reconfigure(newline="\n")  # the lines go to $GITHUB_OUTPUT: a carriage return would be part of the value
    if event != "merge_group":
        print("%s: %s is not a merge queue run, it always runs" % (workflow, event or "no event"), file=sys.stderr)
        print("run=true")
        print("files=0")
        return 0
    if len(args) != 4 or not args[2] or not args[3]:
        print("FAIL changes: a merge queue run without the base and the head of its entry. Fix: pass github.event.merge_group.base_sha and head_sha", file=sys.stderr)
        return 2
    files = changed_files(args[2], args[3])
    run = should_run(workflow, files)
    print("%s: %d files changed, %s" % (workflow, len(files), "something to test" if run else "none under its paths: nothing to test"), file=sys.stderr)
    for name in files[:20]:
        print("  " + name, file=sys.stderr)
    print("run=%s" % ("true" if run else "false"))
    print("files=%d" % len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
