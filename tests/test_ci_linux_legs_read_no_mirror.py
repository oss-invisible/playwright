"""No Linux leg of the wrapper's workflows reads Ubuntu's mirror: the packages come from the run's linux-deps job.

A run lost a green leg to its time limit with every step green, because `playwright install-deps firefox` fetched 48.8 MB from the archive's mirror at 102 kB/s
("Fetched 48.8 MB in 7min 59s"): every Linux leg of that run spent 450-600 s on that step, 20 s in the run before. The speed of a mirror is a third party's, so
.github/workflows/linux-deps.yml fetches the .deb files once per runner image and list (a cache entry, written and read on GitHub's runners only), hands them to
the jobs of the run as the artifact linux-deps-<name>, and the legs install the local files. What the workflows promise, checked on the files themselves:

  1. No job runs `apt-get update` (or `apt`, upgrade, dist-upgrade), `playwright install-deps` (the one use that is allowed is `--dry-run`, in linux-deps.yml, which
     asks which packages the Playwright pin names), or an `apt-get install` without `--no-download`. Only linux-deps.yml reads the archive.
  2. A job that installs the packages names a job that calls linux-deps.yml in its `needs`, downloads that job's artifact (`needs.<job>.outputs.artifact`) into
     /tmp/linux-deps, and does so BEFORE its first install.
  3. A job that starts a virtual display (`xvfb-run`) installs from the artifact: a leg with no package step would run on whatever the image has.
  4. A workflow calls linux-deps.yml once per name, and each call names what the artifact is for.

The checker is tested on known-bad inputs first, because a check that has only ever said "fine" is not known to be able to say anything else. PyYAML is not a
dependency of this package, so the files are read as text: jobs by their header at two spaces, comments dropped, a line continued by a backslash joined.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
LINUX_DEPS = "linux-deps.yml"
INSTALL_DEPS = r"(?<![-\w])install-deps(?![-\w])"
CALL = "uses: ./.github/workflows/linux-deps.yml"
APT = re.compile(r"\bapt(?:-get)?\s+(?:-\S+\s+)*(update|upgrade|dist-upgrade|install)\b")
DOWNLOAD = re.compile(
    r"uses: actions/download-artifact@\S+[^\n]*\n\s+with:\s*\n\s+name: \$\{\{ needs\.([A-Za-z0-9_-]+)\.outputs\.artifact \}\}\n\s+path: /tmp/linux-deps\b")


def jobs_of(text):
    """{name: code of the job}, from the header `  <name>:` to the next one; comments dropped, a line that ends with a backslash joined to the next."""
    head, sep, body = text.partition("\njobs:\n")
    assert sep, "no jobs: in the workflow"
    parts = re.split(r"(?m)^  ([A-Za-z0-9_-]+):[ ]*$", body)
    out = {}
    for name, chunk in zip(parts[1::2], parts[2::2]):
        lines = [l for l in chunk.splitlines() if not l.lstrip().startswith("#")]
        out[name] = re.sub(r"\\\n\s*", " ", "\n".join(lines))
    return out


def needs_of(code):
    inline = re.search(r"(?m)^    needs:[ ]*(.+)$", code)
    if inline:
        return set(re.findall(r"[A-Za-z0-9_-]+", inline.group(1)))
    block = re.search(r"(?m)^    needs:[ ]*\n((?:      - .+\n?)+)", code)
    return set(re.findall(r"[A-Za-z0-9_-]+", block.group(1))) if block else set()


def violations(name, text):
    """What `text` (the workflow `name`) breaks of the four rules, one line each."""
    bad = []
    jobs = jobs_of(text)
    callers = {j: c for j, c in jobs.items() if CALL in c}
    names = []
    for job, code in jobs.items():
        for line in code.splitlines():
            m = APT.search(line)
            if name != LINUX_DEPS and m:
                if m.group(1) != "install":
                    bad.append("%s %s: `apt-get %s` reads the archive; the packages come from the run's linux-deps job" % (name, job, m.group(1)))
                elif "--no-download" not in line:
                    bad.append("%s %s: an `apt-get install` without --no-download reads the archive: %s" % (name, job, line.strip()))
            if re.search(INSTALL_DEPS, line) and "--dry-run" not in line:
                bad.append("%s %s: `playwright install-deps` installs from the archive (only `--dry-run` in linux-deps.yml): %s" % (name, job, line.strip()))
            if re.search(INSTALL_DEPS, line) and "--dry-run" in line and name != LINUX_DEPS:
                bad.append("%s %s: `install-deps --dry-run` belongs to linux-deps.yml" % (name, job))
        installs = [m.start() for m in re.finditer(r"apt-get install[^\n]*--no-download", code)]
        if installs:
            wanted = DOWNLOAD.search(code)
            if not wanted:
                bad.append("%s %s: installs with --no-download but does not download the artifact of a linux-deps job into /tmp/linux-deps" % (name, job))
            else:
                source = wanted.group(1)
                if source not in callers:
                    bad.append("%s %s: downloads the artifact of %s, which is not a call of linux-deps.yml" % (name, job, source))
                if source not in needs_of(code):
                    bad.append("%s %s: downloads the artifact of %s without `needs` on it" % (name, job, source))
                if wanted.start() > installs[0]:
                    bad.append("%s %s: installs before it downloads the artifact" % (name, job))
        elif "xvfb-run" in code and CALL not in code:
            bad.append("%s %s: runs `xvfb-run` and installs no package from a linux-deps job" % (name, job))
        if CALL in code:
            m = re.search(r"\n      name: (\S+)", code)
            if not m:
                bad.append("%s %s: a call of linux-deps.yml without a name" % (name, job))
            else:
                names.append(m.group(1))
    if len(names) != len(set(names)):
        bad.append("%s: two calls of linux-deps.yml with the same name (one artifact for both)" % name)
    return bad


def repository_workflows():
    return sorted(WORKFLOWS.glob("*.yml"))


# ---- known-bad inputs ------------------------------------------------------------------------------------------------------------------------
GOOD = """name: x
jobs:
  linux-deps-x:
    name: linux-deps-x
    uses: ./.github/workflows/linux-deps.yml
    with:
      name: x
      packages: xvfb
  leg:
    needs: [core, linux-deps-x]
    runs-on: ubuntu-24.04
    steps:
      - name: The Linux packages (the run's linux-deps-x job, no mirror)
        uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4
        with:
          name: ${{ needs.linux-deps-x.outputs.artifact }}
          path: /tmp/linux-deps
      - name: System deps
        run: |
          set -- /tmp/linux-deps/*.deb
          sudo apt-get install -y -qq --no-download --no-install-recommends "$@"
          xvfb-run -a true
"""


def test_the_good_input_is_clean():
    assert violations("x.yml", GOOD) == []


@pytest.mark.parametrize("old, new, expected", [
    ("          set -- /tmp/linux-deps/*.deb\n", "          sudo apt-get update\n          set -- /tmp/linux-deps/*.deb\n", "apt-get update"),
    ("          set -- /tmp/linux-deps/*.deb\n", "          sudo apt-get upgrade -y\n          set -- /tmp/linux-deps/*.deb\n", "apt-get upgrade"),
    ("          set -- /tmp/linux-deps/*.deb\n", "          sudo \"$(which python)\" -m playwright install-deps firefox\n          set -- /tmp/linux-deps/*.deb\n", "install-deps"),
    ("          set -- /tmp/linux-deps/*.deb\n", "          sudo apt-get install -y gdb\n          set -- /tmp/linux-deps/*.deb\n", "without --no-download"),
    ("          set -- /tmp/linux-deps/*.deb\n", "          sudo apt-get install -y \\\n            gdb\n          set -- /tmp/linux-deps/*.deb\n", "without --no-download"),
    ("          set -- /tmp/linux-deps/*.deb\n", "          sudo apt install -y gdb\n          set -- /tmp/linux-deps/*.deb\n", "without --no-download"),
    ("          set -- /tmp/linux-deps/*.deb\n", "          python -m playwright install-deps --dry-run firefox\n          set -- /tmp/linux-deps/*.deb\n", "belongs to linux-deps.yml"),
    ("          path: /tmp/linux-deps\n", "          path: /tmp/other\n", "does not download the artifact"),
    ("    needs: [core, linux-deps-x]\n", "    needs: [core]\n", "without `needs`"),
    ("    needs: [core, linux-deps-x]\n", "    needs: core\n", "without `needs`"),
    ("needs.linux-deps-x.outputs.artifact", "needs.core.outputs.artifact", "not a call of linux-deps.yml"),
])
def test_the_known_bad_inputs_are_found(old, new, expected):
    assert old in GOOD
    found = violations("x.yml", GOOD.replace(old, new, 1))
    assert found and any(expected in v for v in found), found


def test_a_leg_that_installs_before_it_downloads_is_found():
    step = GOOD[GOOD.index("      - name: The Linux packages"):GOOD.index("      - name: System deps")]
    swapped = GOOD.replace(step, "").replace("          xvfb-run -a true\n", "          xvfb-run -a true\n" + step)
    assert any("before it downloads" in v for v in violations("x.yml", swapped)), violations("x.yml", swapped)


def test_a_leg_with_a_virtual_display_and_no_package_step_is_found():
    bare = GOOD[:GOOD.index("    steps:\n")] + "    steps:\n      - run: xvfb-run -a true\n"
    assert any("xvfb-run" in v for v in violations("x.yml", bare)), violations("x.yml", bare)


def test_two_calls_of_one_name_are_found():
    twice = GOOD + GOOD[GOOD.index("  linux-deps-x:"):GOOD.index("  leg:")].replace("linux-deps-x", "linux-deps-y")
    assert any("same name" in v for v in violations("x.yml", twice)), violations("x.yml", twice)


def test_a_comment_that_names_the_old_command_is_not_a_violation():
    assert violations("x.yml", GOOD.replace("      - name: System deps\n", "      # it was `sudo apt-get update` and `playwright install-deps firefox`\n      - name: System deps\n")) == []


def test_linux_deps_yml_may_read_the_archive_but_install_deps_only_as_a_dry_run():
    text = (WORKFLOWS / LINUX_DEPS).read_text(encoding="utf-8")
    assert "apt-get update" in text and "--download-only" in text, "the one place that reads the archive"
    assert violations(LINUX_DEPS, text) == []
    assert any("install-deps" in v for v in violations(LINUX_DEPS, text.replace("install-deps --dry-run firefox", "install-deps firefox")))


# ---- the workflows of the repository -----------------------------------------------------------------------------------------------------------
def test_the_workflows_of_the_repository_are_found():
    names = {p.name for p in repository_workflows()}
    assert {"e2e.yml", "fakeweb.yml", "e2e-hunt.yml", "real-sites.yml", "user-install.yml", LINUX_DEPS} <= names


@pytest.mark.parametrize("path", repository_workflows(), ids=lambda p: p.name)
def test_no_linux_leg_reads_the_mirror_and_each_installs_the_files_of_its_linux_deps_job(path):
    assert violations(path.name, path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("workflow, calls", [
    ("e2e.yml", {"linux-deps-e2e"}),
    ("fakeweb.yml", {"linux-deps-fakeweb"}),
    ("e2e-hunt.yml", {"linux-deps-hunt"}),
    ("real-sites.yml", {"linux-deps-real-sites"}),
    ("user-install.yml", {"linux-deps-user-install"}),
])
def test_the_workflows_that_had_apt_in_their_legs_call_linux_deps(workflow, calls):
    jobs = jobs_of((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    assert {j for j, c in jobs.items() if CALL in c} == calls


def test_the_legs_of_e2e_yml_that_start_a_display_install_the_files_of_the_one_list():
    jobs = jobs_of((WORKFLOWS / "e2e.yml").read_text(encoding="utf-8"))
    legs = {j for j, c in jobs.items() if "xvfb-run" in c}
    assert legs == {"e2e-linux", "e2e-network", "e2e-host", "e2e-page", "e2e-page-final"}, legs
    for leg in legs:
        assert "linux-deps-e2e" in needs_of(jobs[leg]), leg
    pkgs = re.search(r"(?m)^      packages: (.+)$", jobs["linux-deps-e2e"]).group(1).split()
    assert {"xvfb", "strace", "gdb", "elfutils"} <= set(pkgs), "what the legs installed one by one, as one list"
    assert re.search(r"(?m)^      playwright: true$", jobs["linux-deps-e2e"]), "and what install-deps names for Firefox"
