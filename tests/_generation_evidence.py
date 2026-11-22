"""TEMPORARY: what a generation of the security state that fails leaves for a reader, whoever started the engine. Remove it once the exit by SIGSEGV
(-11) of the engine in generate mode on the Linux e2e job (run 45014369018) is understood and fixed.

Two launches of the engine in generate mode run in tests/test_security_state_engine_e2e.py: the test's own (`-profile <p>` with the four prefs) and the
CORE's (`invisible_core._security_state_generate.generate`, which `_ready` calls once per module and whose outcome the three tests that need a ready
directory share). The core sends the engine's stdout and stderr to the null device, deletes its work profile before it raises `GenerationError`, and
starts the engine with the core size limit of the test process (0 on the runners): a crash of its launch left nothing at all.

* `save_what_the_failed_generation_left` saves, in `$RUNNER_TEMP/invpw-evidence/<name>/` (the folder the e2e jobs upload), the engine's stdout and stderr,
  the files of the profile with their sizes (the result, `user.js` and `security_state/` copied), and on Linux gdb's backtrace of the core the crash left
  (`kernel.core_pattern` points at `$INVPW_CORE_DIR` on the e2e job, which installs gdb);
* `lift_the_core_limit` is the `preexec_fn` of a launch: the child's soft RLIMIT_CORE raised to its hard limit, so a crash leaves a core;
* `the_core_s_generation_keeps_its_evidence(work, name)` wraps the core's own launch while it is in force: its Popen writes stdout and stderr to files of
  `work` and lifts the core limit, the files of the work profile are noted when the engine exits (before the core deletes them), and when `generate`
  raises, everything is saved under `name` and the error is raised unchanged. The core's launch line, environment and outcome are not touched.

Saving is best effort: it never raises and never decides a test (the assertion of the test, or the core's error, does).
"""
from __future__ import annotations

import contextlib
import importlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

#: The folder under `$RUNNER_TEMP` the e2e workflows upload (`invpw-evidence-*`).
EVIDENCE_FOLDER = "invpw-evidence"
RESULT_NAME = "securitystate-result.json"
STATE_DIR = "security_state"
#: How long gdb may take to read a core of the engine.
GDB_TIMEOUT_S = 600


def evidence_folder(name, fallback):
    """`$RUNNER_TEMP/invpw-evidence/<name>` on a runner, `<fallback>/<name>` elsewhere; made here."""
    base = os.environ.get("RUNNER_TEMP")
    folder = (Path(base) / EVIDENCE_FOLDER if base else Path(fallback)) / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def lift_the_core_limit():
    """In the child, before exec (`preexec_fn`, Linux): the soft RLIMIT_CORE raised to the hard one. A failure is left silent: the child starts as it would have."""
    try:
        import resource

        _soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
        resource.setrlimit(resource.RLIMIT_CORE, (hard, hard))
    except (ImportError, OSError, ValueError):
        pass


def _profile_of(argv):
    argv = [str(part) for part in argv]
    return Path(argv[argv.index("-profile") + 1]) if "-profile" in argv[:-1] else None


def snapshot_the_profile(profile, into):
    """The files of `profile` (path, size) in `into/profile-files.tsv`, with the result, `user.js` and `security_state/` copied: what the engine left, kept before
    anybody deletes the profile. One line saying what was done."""
    into.mkdir(parents=True, exist_ok=True)
    if profile is None or not Path(profile).is_dir():
        return "no profile directory (%s)" % (profile,)
    profile = Path(profile)
    listing = ["%s\t%d" % (path.relative_to(profile).as_posix(), path.stat().st_size) for path in sorted(profile.rglob("*")) if path.is_file()]
    (into / "profile-files.tsv").write_text("\n".join(listing) + "\n", encoding="utf-8")
    for name in (RESULT_NAME, "user.js"):
        if (profile / name).is_file():
            shutil.copyfile(str(profile / name), str(into / name))
    if (profile / STATE_DIR).is_dir():
        shutil.copytree(str(profile / STATE_DIR), str(into / STATE_DIR), dirs_exist_ok=True)
    return "profile files: %d" % len(listing)


def save_what_the_failed_generation_left(binary, profile, output, started_at, code, why, folder, *, snapshot=None, cwd=None):
    """ONLY after a generation that failed: `<output>.stdout` and `<output>.stderr` copied as `engine.stdout`/`engine.stderr`, the profile's files (from `profile`,
    or from a `snapshot` folder made by `snapshot_the_profile` when the profile is gone), and on Linux the backtrace of the core the crash left; `notes.txt` says
    what was found. Never raises."""
    notes = ["exit code %r; %s" % (code, why or "the engine exited")]
    try:
        for suffix in (".stdout", ".stderr"):
            source = Path(str(output) + suffix)
            if source.is_file():
                shutil.copyfile(str(source), str(folder / ("engine" + suffix)))
                notes.append("%s: %d bytes" % (source.name, source.stat().st_size))
            else:
                notes.append("%s: not written" % source.name)
        if snapshot is not None and Path(snapshot).is_dir():
            shutil.copytree(str(snapshot), str(folder), dirs_exist_ok=True)
            notes.append("profile: as the engine left it at its exit (the core deleted it afterwards)")
        else:
            notes.append(snapshot_the_profile(profile, folder))
        if sys.platform.startswith("linux"):
            notes.append(core_backtrace(binary, cwd or os.getcwd(), folder, started_at))
    except Exception as exc:  # the evidence is best effort: the test reports the failure
        notes.append("saving the evidence failed: %r" % (exc,))
    with open(str(folder / "notes.txt"), "a", encoding="utf-8") as sink:
        sink.write("\n".join(notes) + "\n")


def core_pattern():
    try:
        return Path("/proc/sys/kernel/core_pattern").read_text(encoding="utf-8").strip()
    except OSError as exc:
        return "unreadable: %r" % (exc,)


def core_backtrace(binary, cwd, folder, started_at):
    """The core the crash left (in `$INVPW_CORE_DIR`, the folder e2e.yml points `kernel.core_pattern` at; else in `cwd`, the working directory of the launch, where
    a relative pattern writes): gdb's backtraces in `gdb-backtrace.txt`. The cores found are deleted (they are hundreds of MB). One line saying what was done."""
    pattern = core_pattern()
    where = Path(os.environ.get("INVPW_CORE_DIR") or cwd)
    cores = sorted((path for path in where.glob("core*") if path.is_file() and path.stat().st_mtime >= started_at - 2), key=lambda path: path.stat().st_mtime)
    gdb = shutil.which("gdb")
    if not cores:
        return "no core file in %s (kernel.core_pattern is %r, INVPW_CORE_DIR is %r, gdb is %s)" % (where, pattern, os.environ.get("INVPW_CORE_DIR"), gdb)
    core = cores[-1]
    size = core.stat().st_size
    try:
        if gdb is None:
            return "core %s (%d bytes, kernel.core_pattern %r) but gdb is not installed" % (core.name, size, pattern)
        try:
            done = subprocess.run([gdb, "-batch", "-nx", "-ex", "set pagination off", "-ex", "bt", "-ex", "info registers", "-ex", "x/8i $pc", "-ex", "info sharedlibrary",
                                   "-ex", "thread apply all bt", str(binary), str(core)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  timeout=GDB_TIMEOUT_S)
            (folder / "gdb-backtrace.txt").write_bytes(done.stdout)
            return "core %s (%d bytes, kernel.core_pattern %r): gdb exited %d, %d bytes of backtrace" % (core.name, size, pattern, done.returncode, len(done.stdout))
        except Exception as exc:
            return "core %s (%d bytes, kernel.core_pattern %r): gdb failed: %r" % (core.name, size, pattern, exc)
    finally:
        for leftover in cores:
            with contextlib.suppress(OSError):
                leftover.unlink()


class _Subprocess:
    """The `subprocess` the core's module sees while the wrapper is in force: the real module, with `Popen` replaced."""

    def __init__(self, popen):
        self.Popen = popen

    def __getattr__(self, name):
        return getattr(subprocess, name)


@contextlib.contextmanager
def the_core_s_generation_keeps_its_evidence(work, name):
    """While in force, every engine the core's `generate` starts writes its stdout and stderr to files of `work`, has its core limit lifted (Linux), and has the
    files of its profile noted when it exits; if the block raises (the core's `GenerationError`), each launch is saved in the evidence folder `name` and the
    exception goes on unchanged. Yields the list of the launches (dicts: argv, output, started_at, code, snapshot)."""
    module = importlib.import_module("invisible_core._security_state_generate")
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    launches = []
    real_popen = subprocess.Popen
    real_module, real_wait = module.subprocess, module._wait_for_exit

    def popen(argv, *args, **kwargs):
        output = work / ("engine-%d" % len(launches))
        launch = {"argv": [str(part) for part in argv], "output": output, "started_at": time.time(), "code": "not exited", "snapshot": None,
                  "cwd": kwargs.get("cwd")}
        launches.append(launch)
        sinks = [open(str(output) + ".stdout", "wb"), open(str(output) + ".stderr", "wb")]
        kwargs["stdout"], kwargs["stderr"] = sinks
        if sys.platform.startswith("linux") and kwargs.get("preexec_fn") is None:
            kwargs["preexec_fn"] = lift_the_core_limit
        try:
            proc = real_popen(argv, *args, **kwargs)
        finally:
            for sink in sinks:
                sink.close()
        launch["proc"] = proc
        return proc

    def wait_for_exit(proc, timeout_s):
        code = real_wait(proc, timeout_s)
        for launch in launches:
            if launch.get("proc") is proc:
                launch["code"] = code
                launch["snapshot"] = work / ("engine-%d-profile" % launches.index(launch))
                try:
                    snapshot_the_profile(_profile_of(launch["argv"]), launch["snapshot"])
                except Exception:  # best effort, as every save here
                    launch["snapshot"] = None
        return code

    module.subprocess, module._wait_for_exit = _Subprocess(popen), wait_for_exit
    try:
        yield launches
    except BaseException as exc:
        folder = evidence_folder(name, work)
        with open(str(folder / "notes.txt"), "a", encoding="utf-8") as sink:
            sink.write("the core's generate raised %s: %s\nlaunches of the engine: %d\n" % (type(exc).__name__, exc, len(launches)))
        for index, launch in enumerate(launches):
            target = folder if len(launches) == 1 else folder / ("launch-%d" % index)
            target.mkdir(parents=True, exist_ok=True)
            with open(str(target / "notes.txt"), "a", encoding="utf-8") as sink:
                sink.write("argv: %r\n" % (launch["argv"],))
            save_what_the_failed_generation_left(launch["argv"][0], _profile_of(launch["argv"]), launch["output"], launch["started_at"], launch["code"], str(exc),
                                                 target, snapshot=launch["snapshot"], cwd=launch["cwd"])
        raise
    finally:
        module.subprocess, module._wait_for_exit = real_module, real_wait
