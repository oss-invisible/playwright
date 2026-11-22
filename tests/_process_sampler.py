"""A sampler of the processes below a pid, from before a launch to after it, and what proves it missed none: the e2e of the host GL probe.

Firefox on Linux probes the host's GL in a child of the parent: ``<GRE>/gfxtest glx -f <fd>`` (``GfxInfo::FireGLXTestProcess``, widget/gtk/GfxInfo.cpp;
Mozilla's code and logs call it ``glxtest``, the binary is ``gfxtest`` and ``glx`` is the probe it runs). It lives as long as it takes to load the host's GL,
answer through the pipe and be reaped by the parent, and a poll of the process tree can fall between two of its samples. So the sampler alone never says
"absent". The complete list is the kernel's: under the strace stand-in (``tests/_strace_stand_in.py``) every ``execve`` of every process of the browser's tree is
written to the log, because ptrace stops the process AT the call and it cannot run past it unseen. Every exec of a probe in that log must have been seen by
the sampler too (same pid, named as a probe), and the sampler must have run from before the launch to after the page had its WebGL context. A probe that
strace saw and the sampler did not, a sampler that did not cover the launch, a log that does not hold the browser's own exec, or a sampler that died on an
error is a PROBLEM that ``reconcile`` returns, and a test that asserts ``problems == []`` first is red then: a miss is reported, never read as "no probe".

``TreeSampler`` takes ``read`` (``root -> {pid: (name, ppid, cmdline)}``, psutil by default) and ``clock``, so the unit tests drive it with a fake tree and a
counter (tests/test_the_process_sampler_cannot_miss_a_short_lived_probe.py). No browser here.
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

#: The names of the probe's executable: ``gfxtest`` since Mozilla merged its probes into one binary, ``glxtest`` before.
PROBE_NAMES = ("gfxtest", "glxtest")

#: What strace has to write for ``read_witness``: the execs, and the clones that make the tree.
TRACE = "execve,clone,clone3,fork,vfork"

Tree = Dict[int, Tuple[str, Optional[int], List[str]]]


@dataclass
class Seen:
    """A process the sampler saw: every name and command line it had (a child is first seen before its exec, with its parent's name), its parent, and the
    clock at the first and the last sample that held it."""
    pid: int
    ppid: Optional[int]
    names: List[str]
    cmds: List[List[str]]
    first: float
    last: float

    def is_probe(self) -> bool:
        return any(n in PROBE_NAMES for n in self.names) or any(c and os.path.basename(c[0]) in PROBE_NAMES for c in self.cmds)


def psutil_tree(root: int) -> Tree:
    """``{pid: (name, ppid, cmdline)}`` of every process below ``root`` now. A zombie keeps its name and parent and has no command line."""
    import psutil

    out: Tree = {}
    try:
        children = psutil.Process(root).children(recursive=True)
    except psutil.Error:
        return out
    for proc in children:
        try:
            with proc.oneshot():
                name, ppid = proc.name(), proc.ppid()
                try:
                    cmd = proc.cmdline()
                except psutil.Error:
                    cmd = []
        except psutil.Error:
            continue
        out[proc.pid] = (name, ppid, list(cmd))
    return out


class TreeSampler:
    """Samples the tree below ``root`` in a thread, as fast as it can (a step of ``step`` seconds between two reads, so the test's own client keeps the
    interpreter), from ``start`` to ``stop``. ``start`` takes the first sample before it returns and ``stop`` takes a last one after the thread has ended,
    so what happens between the two calls is inside the samples. ``first_sample`` is the clock when the first read ENDED, ``last_sample`` the clock when
    the last read BEGAN: the window the sampler covers."""

    def __init__(self, root: int, read: Callable[[int], Tree] = psutil_tree, clock: Callable[[], float] = time.monotonic, step: float = 0.001) -> None:
        self.root = root
        self._read = read
        self._clock = clock
        self._step = step
        self.seen: Dict[int, Seen] = {}
        self.samples = 0
        self.first_sample: Optional[float] = None
        self.last_sample: Optional[float] = None
        self.error: Optional[BaseException] = None
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "TreeSampler":
        self._sample()
        self._thread = threading.Thread(target=self._poll, name="tree-sampler", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stopped.set()
        if self._thread is not None:
            self._thread.join()
        if self.error is None:
            self._sample()

    def _poll(self) -> None:
        try:
            while not self._stopped.is_set():
                self._sample()
                self._stopped.wait(self._step)
        except BaseException as exc:  # the verdict reports it: a sampler that died covered nothing after it
            self.error = exc

    def _sample(self) -> None:
        began = self._clock()
        tree = self._read(self.root)
        ended = self._clock()
        with self._lock:
            for pid, (name, ppid, cmd) in tree.items():
                seen = self.seen.get(pid)
                if seen is None:
                    self.seen[pid] = Seen(pid, ppid, [name] if name else [], [list(cmd)] if cmd else [], began, began)
                    continue
                seen.last = began
                if name and name not in seen.names:
                    seen.names.append(name)
                if cmd and list(cmd) not in seen.cmds:
                    seen.cmds.append(list(cmd))
            if self.first_sample is None:
                self.first_sample = ended
            self.last_sample = began
            self.samples += 1

    def view(self) -> Tuple[Dict[int, Seen], Optional[float], Optional[float], Optional[BaseException]]:
        with self._lock:
            return dict(self.seen), self.first_sample, self.last_sample, self.error


# --------------------------------------------------------------------------------------------------------------------------------- the witness: strace
@dataclass
class Exec:
    """One ``execve`` of the strace log: the pid, the path, the arguments, and the result (``None`` when the log ends before it)."""
    pid: int
    path: str
    argv: List[str] = field(default_factory=list)
    result: Optional[int] = None

    def ran(self) -> bool:
        """The exec replaced the process (0), or the log stops before saying (counted as run: it can only make a test red)."""
        return self.result is None or self.result == 0


_EXECVE = re.compile(r'^(\d+)\s+execve\("((?:[^"\\]|\\.)*)",\s*\[(.*?)\]')
_RESUMED_EXECVE = re.compile(r"^(\d+)\s+<\.\.\. execve resumed>")
_RESULT = re.compile(r"\)\s+=\s+(-?\d+)")
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')


def read_execs(lines: Iterable[str]) -> List[Exec]:
    """Every ``execve`` of a ``strace -f`` log (no timestamps), with its result: on its line, or on the ``<... execve resumed>`` line of its pid when
    another thread interrupted it."""
    out: List[Exec] = []
    pending: Dict[int, Exec] = {}
    for line in lines:
        m = _EXECVE.match(line)
        if m:
            found = Exec(int(m.group(1)), m.group(2), _QUOTED.findall(m.group(3)))
            out.append(found)
            if "<unfinished ...>" in line:
                pending[found.pid] = found
            else:
                r = _RESULT.search(line[m.end():])
                found.result = int(r.group(1)) if r else None
            continue
        m = _RESUMED_EXECVE.match(line)
        if m and int(m.group(1)) in pending:
            r = _RESULT.search(line)
            pending.pop(int(m.group(1))).result = int(r.group(1)) if r else None
    return out


def is_probe_exec(found: Exec) -> bool:
    return os.path.basename(found.path) in PROBE_NAMES


def is_glx_probe(found: Exec) -> bool:
    """The GLX probe: the probe's executable with ``glx`` as its command (``gfxtest glx -f <fd>``; ``vaapi``, ``v4l2`` and ``vulkan`` are other probes)."""
    return is_probe_exec(found) and found.argv[1:2] == ["glx"]


def browser_parents(execs: Iterable[Exec], real_binary: str) -> List[int]:
    """The pids that executed the browser itself (the real binary, not a content process): the parent the probe is a child of."""
    return [e.pid for e in execs if e.ran() and e.path == real_binary and "-contentproc" not in e.argv]


def reconcile(seen: Dict[int, Seen], first_sample: Optional[float], last_sample: Optional[float], error: Optional[BaseException],
              execs: List[Exec], real_binary: str, launched_at: float, ready_at: Optional[float]) -> Tuple[List[Exec], List[str]]:
    """``(probes, problems)``: the execs of a probe that ran, and every reason the observation cannot be trusted. ``problems`` is empty only when strace
    saw the browser start, the sampler ran from before ``launched_at`` to after ``ready_at`` (the page has its WebGL context), and every probe strace saw
    the kernel exec was seen by the sampler under the same pid as a probe, and the other way round."""
    problems = []
    if error is not None:
        problems.append("the sampler stopped on an error: %r" % (error,))
    if first_sample is None or first_sample > launched_at:
        problems.append("the sampler's first sample (%s) is not before the launch (%s): what the launch started first is not in it" % (first_sample, launched_at))
    if ready_at is None:
        problems.append("the page never had its WebGL context: the window of the launch has no end")
    elif last_sample is None or last_sample < ready_at:
        problems.append("the sampler's last sample (%s) is before the page had its WebGL context (%s): it stopped early" % (last_sample, ready_at))
    if not browser_parents(execs, real_binary):
        problems.append("strace wrote no execve of the browser (%s): the witness is empty, and an empty witness proves no absence" % real_binary)
    probes = [e for e in execs if is_probe_exec(e) and e.ran()]
    for e in probes:
        s = seen.get(e.pid)
        if s is None or not s.is_probe():
            problems.append("strace saw pid %d exec %s and the sampler %s: the sampler missed a probe"
                            % (e.pid, " ".join(e.argv[:3]) or e.path, "never saw the pid" if s is None else "saw it only as %s" % (s.names or s.cmds)))
    witnessed = {e.pid for e in probes}
    for s in seen.values():
        if s.is_probe() and s.pid not in witnessed:
            problems.append("the sampler saw a probe (pid %d, %s) that strace never saw exec: the two do not watch the same tree" % (s.pid, s.names))
    return probes, problems


def describe(seen: Dict[int, Seen], execs: List[Exec], origin: float) -> str:
    """Every process the sampler saw (seconds from ``origin``, names, pid, ppid, head of the command line) and every exec strace wrote."""
    rows = ["processes seen below the test (first..last sample, from the launch):"]
    for s in sorted(seen.values(), key=lambda s: (s.first, s.pid)):
        head = " ".join(a[:80] for a in (s.cmds[-1] if s.cmds else [])[:4])
        rows.append("  +%.3fs..+%.3fs pid=%d ppid=%s names=%s cmd=%s" % (s.first - origin, s.last - origin, s.pid, s.ppid, "/".join(s.names), head))
    rows.append("execs strace wrote:")
    for e in execs:
        rows.append("  pid=%d result=%s %s" % (e.pid, e.result, " ".join(a[:80] for a in e.argv[:4]) or e.path))
    return "\n".join(rows)
