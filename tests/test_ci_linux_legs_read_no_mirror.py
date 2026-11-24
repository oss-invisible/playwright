"""No Linux leg of the wrapper's workflows reads Ubuntu's mirror: their apt reads only the run's linux-deps repository.

A run lost a green leg to its time limit with every step green, because `playwright install-deps firefox` fetched 48.8 MB from the archive's mirror at 102 kB/s
("Fetched 48.8 MB in 7min 59s"). The speed of a mirror is a third party's, so .github/workflows/linux-deps.yml reads Ubuntu's archive once per runner image and
list (a cache entry, written and read on GitHub's runners only) and hands the jobs of the run a REPOSITORY, the artifact linux-deps-<name>: the roots, their
whole closure of Depends and Pre-Depends (what the image already has included, because GitHub mixes builds of one image in one run, and a first form that handed
over only the missing files had legs fail with "E: Unable to fetch some archives"), a Packages index and roots.txt. The same file is in the series. What the
workflows promise, checked on the files themselves:

  1. In a leg the only apt lines are `sudo apt-get $APT update` and `sudo apt-get $APT install -y --no-download --no-install-recommends $(cat
     /tmp/linux-deps/roots.txt)`, where APT names a sources list, a sources folder and a lists folder under /tmp/linux-deps-apt (and Acquire::Languages=none):
     apt reads the repository and nothing else. The sources list holds the one line `deb [trusted=yes] file:/tmp/linux-deps ./`, and the files are copied into
     apt's archives first. No leg runs `playwright install-deps` (not even `--dry-run`: linux-deps.yml reads the table of the pin itself).
  2. A job that installs names a job that calls linux-deps.yml in its `needs`, downloads that job's artifact (`needs.<job>.outputs.artifact`) into /tmp/linux-deps,
     and does so BEFORE its install.
  3. A job that starts a virtual display (`xvfb-run`) installs from the artifact: a leg with no package step would run on whatever the image has.
  4. A workflow calls linux-deps.yml once per name, and each call names what the artifact is for.
  5. linux-deps.yml builds the repository (closure, `apt-get download`, Packages index, roots.txt, every root checked), never with --download-only, and reads
     the archive only in the steps that run when the cache entry is missing.

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
APT_LOCAL = ("-o Dir::Etc::SourceList=/tmp/linux-deps-apt/sources.list -o Dir::Etc::SourceParts=/tmp/linux-deps-apt/parts "
             "-o Dir::State::Lists=/tmp/linux-deps-apt/lists -o Acquire::Languages=none")
UPDATE = "sudo apt-get $APT update"
INSTALL = "sudo apt-get $APT install -y --no-download --no-install-recommends $(cat /tmp/linux-deps/roots.txt)"
ALLOWED = (UPDATE, INSTALL)
SOURCES_LINE = 'echo "deb [trusted=yes] file:/tmp/linux-deps ./" | sudo tee /tmp/linux-deps-apt/sources.list >/dev/null'
COPY = "sudo cp /tmp/linux-deps/*.deb /var/cache/apt/archives/"
MISS = "if: steps.restore.outputs.cache-hit != 'true'"
APT_WORDS = re.compile(r"apt-get|apt install|apt update|apt upgrade|apt-cache")
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


def deps_violations(text):
    """What linux-deps.yml breaks: the repository of the closure, built from the archive only when the entry is missing."""
    bad = []
    code = jobs_of(text).get("debs", "")
    for need in ("apt-cache depends --recurse --no-recommends", "apt-get download", "apt-ftparchive packages . > Packages", "roots.txt",
                 "is not in the repository", 'for group in ("tools", "firefox")'):
        if need not in code:
            bad.append("%s: the repository is not built: `%s` is missing" % (LINUX_DEPS, need))
    # PR #585: libgtk-3-0 is a name libgtk-3-0t64 provides (a root check on Package: alone refused it); a key per image build missed the entry run after
    # run (the builds of one release are mixed in a run); the closure read one package at a time took 13 minutes from a slow mirror.
    for need, why in (("Provides: (.*, )?$e", "a root may be a name a package of the repository provides"),
                      ('RELEASE="${ImageVersion%%.*}"', "the entry is per release of the image, not per build"),
                      ("xargs -n 20 -P 8 apt-get download", "the closure is downloaded 8 at a time"),
                      ("apt-get download -qq -o Acquire::Retries=5", "each fetch of the closure retries 5 times"),
                      ("MB in $((T1 - T0)) s", "the MB and seconds of the download are logged"),
                      ("Reverse Provides:", "a root only provided is resolved to its provider (apt-cache depends does not walk from a virtual root)"),
                      ("not by one package", "a root provided by more than one package is refused"),
                      ("of the closure was not downloaded", "every package of the closure is checked")):
        if need not in code:
            bad.append("%s: %s: `%s` is missing" % (LINUX_DEPS, why, need))
    if "$ImageVersion-$SHA" in code:
        bad.append("%s: the entry is keyed by the image build ($ImageVersion), so it misses whenever the builds of a release are mixed" % LINUX_DEPS)
    if "--download-only" in code:
        bad.append("%s: `--download-only` fetches only what the image lacks; the repository holds the whole closure" % LINUX_DEPS)
    for step in re.split(r"(?m)^      - ", code)[1:]:
        if (APT_WORDS.search(step) or "actions/cache/save" in step) and MISS not in step:
            bad.append("%s: a step reads the archive or saves the entry without `%s`: %s" % (LINUX_DEPS, MISS, step.splitlines()[0].strip()))
    return bad


def violations(name, text):
    """What `text` (the workflow `name`) breaks of the rules, one line each."""
    if name == LINUX_DEPS:
        return deps_violations(text)
    bad = []
    jobs = jobs_of(text)
    callers = {j: c for j, c in jobs.items() if CALL in c}
    names = []
    for job, code in jobs.items():
        lines = code.splitlines()
        apt_lines = [l.strip() for l in lines if APT_WORDS.search(l)]
        for line in lines:
            if re.search(INSTALL_DEPS, line):
                bad.append("%s %s: `playwright install-deps` installs from the archive: %s" % (name, job, line.strip()))
        for line in apt_lines:
            if line not in ALLOWED:
                bad.append("%s %s: an apt line that is not one of the two the repository allows: %s" % (name, job, line))
        installs = [m.start() for m in re.finditer(re.escape(INSTALL), code)]
        if apt_lines or installs:
            for need in ALLOWED:
                if need not in apt_lines:
                    bad.append("%s %s: lacks `%s`" % (name, job, need))
            if ('APT="%s"' % APT_LOCAL) not in code:
                bad.append("%s %s: APT is not the local sources list, sources folder and lists folder under /tmp/linux-deps-apt, with Acquire::Languages=none" % (name, job))
            if SOURCES_LINE not in code:
                bad.append("%s %s: the sources list is not the one line `deb [trusted=yes] file:/tmp/linux-deps ./`" % (name, job))
            if [l.strip() for l in lines if "sources.list" in l and not l.lstrip().startswith("APT=")] != [SOURCES_LINE]:
                bad.append("%s %s: something else writes or names the sources list" % (name, job))
            if COPY not in code:
                bad.append("%s %s: lacks `%s`" % (name, job, COPY))
            elif installs and code.index(COPY) > installs[0]:
                bad.append("%s %s: copies the files into apt's archives after the install" % (name, job))
        if installs:
            wanted = DOWNLOAD.search(code)
            if not wanted:
                bad.append("%s %s: installs from the repository but does not download the artifact of a linux-deps job into /tmp/linux-deps" % (name, job))
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
            if re.search(r"(?m)^      playwright:", code):
                bad.append("%s %s: the input `playwright` is gone (the pin is read from `playwright_wrapper_ref`)" % (name, job))
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
          set -euo pipefail
          read -r OS VER < /tmp/linux-deps/image.txt
          echo "the repository was read on $OS $VER; this runner is $ImageOS $ImageVersion"
          sudo mkdir -p /tmp/linux-deps-apt/lists/partial /tmp/linux-deps-apt/parts
          echo "deb [trusted=yes] file:/tmp/linux-deps ./" | sudo tee /tmp/linux-deps-apt/sources.list >/dev/null
          APT="-o Dir::Etc::SourceList=/tmp/linux-deps-apt/sources.list -o Dir::Etc::SourceParts=/tmp/linux-deps-apt/parts -o Dir::State::Lists=/tmp/linux-deps-apt/lists -o Acquire::Languages=none"
          sudo apt-get $APT update
          sudo cp /tmp/linux-deps/*.deb /var/cache/apt/archives/
          sudo apt-get $APT install -y --no-download --no-install-recommends $(cat /tmp/linux-deps/roots.txt)
          xvfb-run -a true
"""
APT_UPDATE = "          sudo apt-get $APT update\n"
APT_INSTALL = "          sudo apt-get $APT install -y --no-download --no-install-recommends $(cat /tmp/linux-deps/roots.txt)\n"
FIRST_FORM = ('          set -- /tmp/linux-deps/*.deb\n'
              '          sudo apt-get install -y -qq --no-download --no-install-recommends "$@"\n')


def test_the_good_input_is_clean():
    assert violations("x.yml", GOOD) == []


@pytest.mark.parametrize("old, new, expected", [
    (APT_UPDATE, "          sudo apt-get update\n", "not one of the two"),
    (APT_UPDATE, APT_UPDATE + "          sudo apt-get upgrade -y\n", "not one of the two"),
    (APT_UPDATE, APT_UPDATE + "          sudo apt-get $APT update -qq\n", "not one of the two"),
    (APT_UPDATE, APT_UPDATE + "          sudo \"$(which python)\" -m playwright install-deps firefox\n", "install-deps"),
    (APT_UPDATE, APT_UPDATE + "          python -m playwright install-deps --dry-run firefox\n", "install-deps"),
    (APT_UPDATE, APT_UPDATE + "          sudo apt-get install -y gdb\n", "not one of the two"),
    (APT_UPDATE, APT_UPDATE + "          sudo apt-get install -y \\\n            gdb\n", "not one of the two"),
    (APT_UPDATE, APT_UPDATE + "          sudo apt install -y gdb\n", "not one of the two"),
    (APT_UPDATE, APT_UPDATE + "          sudo apt-get $APT install -y gdb\n", "not one of the two"),
    (APT_INSTALL, FIRST_FORM, "not one of the two"),
    (APT_INSTALL, APT_INSTALL.replace(" --no-download", ""), "not one of the two"),
    (APT_INSTALL, APT_INSTALL.replace("$(cat /tmp/linux-deps/roots.txt)", "gdb"), "not one of the two"),
    ("-o Acquire::Languages=none", "-o Acquire::Foo=none", "APT is not the local"),
    ("-o Dir::State::Lists=/tmp/linux-deps-apt/lists ", "", "APT is not the local"),
    ("-o Dir::Etc::SourceParts=/tmp/linux-deps-apt/parts ", "", "APT is not the local"),
    ('echo "deb [trusted=yes] file:/tmp/linux-deps ./" | sudo tee /tmp/linux-deps-apt/sources.list >/dev/null\n',
     'echo "deb [trusted=yes] file:/tmp/linux-deps ./" | sudo tee /tmp/linux-deps-apt/sources.list >/dev/null\n'
     '          echo "deb http://archive.ubuntu.com/ubuntu noble main" | sudo tee -a /tmp/linux-deps-apt/sources.list >/dev/null\n', "sources list"),
    ('echo "deb [trusted=yes] file:/tmp/linux-deps ./"', 'echo "deb file:/tmp/linux-deps ./"', "the one line"),
    ("          sudo cp /tmp/linux-deps/*.deb /var/cache/apt/archives/\n", "", "lacks `sudo cp"),
    ("          path: /tmp/linux-deps\n", "          path: /tmp/other\n", "does not download the artifact"),
    ("    needs: [core, linux-deps-x]\n", "    needs: [core]\n", "without `needs`"),
    ("    needs: [core, linux-deps-x]\n", "    needs: core\n", "without `needs`"),
    ("needs.linux-deps-x.outputs.artifact", "needs.core.outputs.artifact", "not a call of linux-deps.yml"),
    ("      packages: xvfb\n", "      packages: xvfb\n      playwright: true\n", "input `playwright` is gone"),
])
def test_the_known_bad_inputs_are_found(old, new, expected):
    assert old in GOOD
    found = violations("x.yml", GOOD.replace(old, new, 1))
    assert found and any(expected in v for v in found), found


def test_the_first_form_is_found_whole():
    """The first form (the .deb files installed against the leg's own image and lists) fails the rules on every count."""
    first = GOOD.replace(
        GOOD[GOOD.index("          echo \"the repository was read"):GOOD.index("          xvfb-run")],
        FIRST_FORM)
    found = violations("x.yml", first)
    assert any("not one of the two" in v for v in found) and any("lacks" in v for v in found), found


def test_a_leg_that_installs_before_it_downloads_is_found():
    step = GOOD[GOOD.index("      - name: The Linux packages"):GOOD.index("      - name: System deps")]
    swapped = GOOD.replace(step, "").replace("          xvfb-run -a true\n", "          xvfb-run -a true\n" + step)
    assert any("before it downloads" in v for v in violations("x.yml", swapped)), violations("x.yml", swapped)


def test_a_leg_that_copies_the_files_after_the_install_is_found():
    cp = "          sudo cp /tmp/linux-deps/*.deb /var/cache/apt/archives/\n"
    late = GOOD.replace(cp, "").replace("          xvfb-run -a true\n", cp + "          xvfb-run -a true\n")
    assert any("after the install" in v for v in violations("x.yml", late)), violations("x.yml", late)


def test_a_leg_with_a_virtual_display_and_no_package_step_is_found():
    bare = GOOD[:GOOD.index("    steps:\n")] + "    steps:\n      - run: xvfb-run -a true\n"
    assert any("xvfb-run" in v for v in violations("x.yml", bare)), violations("x.yml", bare)


def test_two_calls_of_one_name_are_found():
    twice = GOOD + GOOD[GOOD.index("  linux-deps-x:"):GOOD.index("  leg:")].replace("linux-deps-x", "linux-deps-y")
    assert any("same name" in v for v in violations("x.yml", twice)), violations("x.yml", twice)


def test_a_comment_that_names_the_old_command_is_not_a_violation():
    assert violations("x.yml", GOOD.replace("      - name: System deps\n", "      # it was `sudo apt-get update` and `playwright install-deps firefox`\n      - name: System deps\n")) == []


# ---- linux-deps.yml ---------------------------------------------------------------------------------------------------------------------------
def deps_text():
    return (WORKFLOWS / LINUX_DEPS).read_text(encoding="utf-8")


def test_linux_deps_yml_builds_the_repository_and_reads_the_archive_only_when_the_entry_is_missing():
    assert deps_violations(deps_text()) == []


@pytest.mark.parametrize("old, new, expected", [
    ("apt-cache depends --recurse --no-recommends", "apt-cache depends --no-recommends", "apt-cache depends --recurse"),
    ("apt-ftparchive packages . > Packages", "true", "apt-ftparchive packages"),
    ("is not in the repository", "is missing", "is not in the repository"),
    ("apt-get download -qq", "apt-get download --download-only -qq", "--download-only"),
    ('for group in ("tools", "firefox")', 'for group in ("tools",)', "for group"),
    ("Provides: (.*, )?$e", "Provides: none", "provides"),
    ('RELEASE="${ImageVersion%%.*}"', 'RELEASE="$ImageVersion"', "release of the image"),
    ("xargs -n 20 -P 8 apt-get download", "xargs -n 20 -P 1 apt-get download", "8 at a time"),
    ('echo "key=linux-deps-$ImageOS-$RELEASE-$SHA"', 'echo "key=linux-deps-$ImageOS-$ImageVersion-$SHA"', "image build"),
    ("      - name: A new image or list - the repository from Ubuntu's archive, once\n        if: steps.restore.outputs.cache-hit != 'true'\n",
     "      - name: A new image or list - the repository from Ubuntu's archive, once\n", "reads the archive"),
    ("      - name: Keep the entry\n        if: steps.restore.outputs.cache-hit != 'true'\n", "      - name: Keep the entry\n", "saves the entry"),
])
def test_linux_deps_yml_known_bad_inputs_are_found(old, new, expected):
    text = deps_text()
    assert old in text
    found = deps_violations(text.replace(old, new, 1))
    assert found and any(expected in v for v in found), found


def test_linux_deps_yml_takes_the_wrapper_ref_not_a_flag():
    text = deps_text()
    assert re.search(r"(?m)^      playwright_wrapper_ref:$", text) and not re.search(r"(?m)^      playwright:$", text)
    assert "repository: oss-invisible/playwright" in text and "scripts/playwright_pin.txt" in text


# ---- the workflows of the repository -----------------------------------------------------------------------------------------------------------
def test_the_workflows_of_the_repository_are_found():
    names = {p.name for p in repository_workflows()}
    assert {"e2e.yml", "fakeweb.yml", "e2e-hunt.yml", "real-sites.yml", "user-install.yml", LINUX_DEPS} <= names


@pytest.mark.parametrize("path", repository_workflows(), ids=lambda p: p.name)
def test_no_linux_leg_reads_the_mirror_and_each_installs_the_repository_of_its_linux_deps_job(path):
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


def test_the_legs_of_e2e_yml_that_start_a_display_install_the_repository_of_the_one_list():
    jobs = jobs_of((WORKFLOWS / "e2e.yml").read_text(encoding="utf-8"))
    legs = {j for j, c in jobs.items() if "xvfb-run" in c}
    assert legs == {"e2e-linux", "e2e-network", "e2e-host", "e2e-page", "e2e-page-final"}, legs
    for leg in legs:
        assert "linux-deps-e2e" in needs_of(jobs[leg]), leg
    pkgs = re.search(r"(?m)^      packages: (.+)$", jobs["linux-deps-e2e"]).group(1).split()
    assert {"xvfb", "strace", "gdb", "elfutils"} <= set(pkgs), "what the legs installed one by one, as one list"


@pytest.mark.parametrize("workflow, call", [("e2e.yml", "linux-deps-e2e"), ("fakeweb.yml", "linux-deps-fakeweb"), ("e2e-hunt.yml", "linux-deps-hunt")])
def test_the_calls_that_need_install_deps_roots_name_the_pin_of_this_commit(workflow, call):
    """The roots `playwright install-deps firefox` installs come from the Playwright of the wrapper's pin, read at the commit that runs."""
    jobs = jobs_of((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    assert re.search(r"(?m)^      playwright_wrapper_ref: \$\{\{ github\.sha \}\}$", jobs[call]), jobs[call]
