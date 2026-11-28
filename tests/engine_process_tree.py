"""The engine processes that belong to THIS test, found from the tree and not from the machine.

An e2e run opens several browsers at once (pytest-xdist workers, each with its own). A test that
wants "the engine process that does X" and looks for it over the whole of /proc can be handed the
process of another worker's browser, which has not done X yet: that is how
test_webgl_draws_on_the_engine_s_own_gl failed on one run of the Linux gate and passed on the
next with the same engine and the same core.

The tree is the answer: a worker's browsers are its descendants, and a process that is not below
the worker is not the test's business. `maps_of_the_engine_below` takes the root, the engine's
folder and a `view` of the machine (the real one, or a stand-in in the unit test).
"""
from __future__ import annotations

import os
import sys
from typing import Dict, List, Optional


class Machine:
    """What the selector asks of the machine: the real one, through psutil and /proc."""

    def descendants(self, pid: int) -> List[int]:
        import psutil

        try:
            return [p.pid for p in psutil.Process(pid).children(recursive=True)]
        except psutil.Error:
            return []

    def exe(self, pid: int) -> Optional[str]:
        import os

        try:
            return os.readlink("/proc/%d/exe" % pid)
        except OSError:
            return None

    def maps(self, pid: int) -> List[str]:
        try:
            with open("/proc/%d/maps" % pid) as f:
                return sorted({line.split()[-1] for line in f if len(line.split()) > 5})
        except OSError:
            return []


def maps_of_the_engine_below(root: int, binary_dir: str, machine: Optional[Machine] = None) -> Dict[int, List[str]]:
    """{pid: mapped files} of every engine process (executable inside `binary_dir`) that is a
    descendant of `root`. Nothing outside the tree is looked at."""
    machine = machine or Machine()
    found: Dict[int, List[str]] = {}
    for pid in machine.descendants(root):
        exe = machine.exe(pid)
        if exe and exe.startswith(binary_dir):
            found[pid] = machine.maps(pid)
    return found


def ships_its_own_gl(binary_dir: str) -> bool:
    """The engine's own GL is in the package from firefox-59: a test follows the package under
    test, not the core's version (the gate runs a candidate under the published core)."""
    return os.path.isfile(os.path.join(binary_dir, "llvmpipe", "libEGL.so.1"))


def gl_maps_of_this_test(firefox_binary: str) -> Optional[Dict[int, List[str]]]:
    """The maps of the engine's processes below this test, to be read while the browser is up and a
    page has had its WebGL context; None where the verdict on the GL does not apply (not Linux,
    or an engine with no GL of its own)."""
    if not sys.platform.startswith("linux"):
        return None
    binary_dir = os.path.dirname(os.path.realpath(firefox_binary))
    if not ships_its_own_gl(binary_dir):
        return None
    return maps_of_the_engine_below(os.getpid(), binary_dir)


def assert_the_gl_is_the_engines_own(firefox_binary: str, below: Optional[Dict[int, List[str]]]) -> None:
    """The rule of the verdict on the GL, written once: some engine process maps the engine's own
    libEGL.so.1 and its own libgallium-*, and none maps a libEGL that is not under the engine's
    `llvmpipe/` folder. (GTK still opens the host's GLX at startup to choose its X11 visuals;
    nothing of it reaches WebGL or a page, so only libEGL is held against the host's.) With
    `below` None the verdict does not apply."""
    if below is None:
        return
    own = os.path.join(os.path.dirname(os.path.realpath(firefox_binary)), "llvmpipe") + os.sep
    assert below, "no engine process below this test"
    drew = [pid for pid, maps in below.items()
            if own + "libEGL.so.1" in maps and any(m.startswith(own + "libgallium-") for m in maps)]
    assert drew, {pid: [m for m in maps if "EGL" in m or "gallium" in m] for pid, maps in below.items()}
    foreign = {pid: [m for m in maps if "libEGL" in os.path.basename(m) and not m.startswith(own)]
               for pid, maps in below.items()}
    assert not any(foreign.values()), foreign
