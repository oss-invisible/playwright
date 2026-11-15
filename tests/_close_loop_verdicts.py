"""The verdicts of `test_closing_one_of_four_browsers_does_not_hold_the_loop_e2e.py`, as pure functions of what its client recorded (so a
unit test feeds them the red case and the green case without a browser).

None of them reads how long something took: a loop that is held is told by WHERE the thread of the loop was (inside a blocking step of
the close), and a browser that keeps answering is told by the ORDER of the events (a call that began and ended while the close was in
flight, and answers that came while the removal of the profile was held until every other browser had answered). A machine that is
slow stretches every duration and changes none of these.

``inside`` is what the watcher saw of the loop's thread, one entry per frame that was ever on its stack while it was running something
(not waiting in the selector): ``{"<file>:<function>": {"n": samples, "stack": [the innermost frames of the first stack it was seen in]}}``.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

#: A blocking step of a close: (a part of the file's path, the functions of it). They WAIT (for the processes of the tree, for the
#: workers, for the directories to go), so none of them may be on the stack of the thread that runs the loop of the user:
#: `InProcessTransport.request_stop`'s `shutdown` and `_join_workers`, `reap` / `reap_session` of the core, `op_close` of the engine's
#: dispatcher, and the blocking calls those make (`psutil.wait_procs`, `shutil.rmtree`, `Thread.join`).
BLOCKING_STEPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("/invisible_playwright/", ("shutdown", "_join_workers", "op_close")),
    ("/invisible_core/", ("shutdown", "reap", "reap_session")),
    ("/psutil/", ("wait_procs",)),
    ("/shutil.py", ("rmtree",)),
    ("/threading.py", ("join",)),
)


def blocked_by(inside: Dict[str, dict], steps: Iterable[Tuple[str, Sequence[str]]] = BLOCKING_STEPS) -> List[str]:
    """The frames of ``steps`` that were on the stack of the loop's thread while it ran something, sorted."""
    found = []
    for frame in inside:
        path, _, function = frame.rpartition(":")
        for part, functions in steps:
            if part in path and function in functions:
                found.append(frame)
                break
    return sorted(found)


def close_on_the_loop_problem(inside: Dict[str, dict]) -> Optional[str]:
    """What is wrong when a step of the close that waits ran on the thread of the loop, or None. Red on a close made on the loop
    (the defect), whatever the speed of the machine; green on one made in threads of their own."""
    found = blocked_by(inside)
    if not found:
        return None
    lines = []
    for frame in found:
        entry = inside[frame]
        lines.append("%s (seen in %d samples of the loop's thread), the innermost frames of the first one:\n    %s"
                     % (frame, entry["n"], "\n    ".join(entry["stack"])))
    return "the loop's own thread was inside a step of the close that waits:\n" + "\n".join(lines)


def blind_instrument_problem(inside: Dict[str, dict], steps: Iterable[Tuple[str, Sequence[str]]]) -> Optional[str]:
    """The control: a loop that was held on purpose, inside the function ``steps`` names, must have been seen there at least once."""
    if blocked_by(inside, steps):
        return None
    return ("the watcher never saw the loop's thread inside %s while the loop was held on purpose: it could not see a close made on "
            "the loop (frames it did see: %s)" % (list(steps), sorted(inside)[:12]))


def charged_more_than_ran_problem(held: float, busy_run: float) -> Optional[str]:
    """The second control: a loop the machine did not schedule waits in its selector, and only the time its thread was seen RUNNING is
    held. ``busy_run`` is the longest stretch the watcher saw the thread run without a pause in the selector, read off the same
    samples as ``held``: what is charged can never be more than that. Charging the whole lateness (the wait in the selector too, as
    the first instrument did: 94 ms with nothing on the loop's thread, candidate 43727128285) is more than that, whatever the machine."""
    if held <= busy_run:
        return None
    return ("the loop was charged %.0f ms of hold and its thread was seen running for at most %.0f ms in a row: the time it waited in "
            "its selector (the machine did not schedule it) was counted as held" % (held * 1000, busy_run * 1000))


def others_answered_problem(others: Sequence[dict], gate: dict) -> Optional[str]:
    """What is wrong with the way the other browsers answered while the first one closed, or None.

    ``others`` has one record per other browser: ``inside`` is the calls that began AFTER the close began and ended BEFORE it ended
    (a call that was answered during the close, in the order of the events, not by how long it took), ``during_removal`` the calls
    that did the same while the removal of the closing browser's profile was held. ``gate`` says how that hold ended: ``reached``
    (the removal ran in the window of the close), ``released_by``: ``answered`` (every other browser had answered a call that began
    after the removal began: the loop was free while the removal ran) or ``deadline`` (they had not, in 30 s: the loop could not run
    while the removal was waiting, which is a removal made on the loop's thread)."""
    if len(others) != 3:
        return "the client followed %d other browsers instead of 3: %r" % (len(others), others)
    silent = [i for i, r in enumerate(others) if r["inside"] < 1]
    if silent:
        return ("the other browsers %s did not answer a single call that began and ended inside the close (calls inside it, per "
                "browser: %s): the loop that serves them did not run during the close" % (silent, [r["inside"] for r in others]))
    if gate.get("reached") and gate.get("released_by") != "answered":
        return ("the removal of the profile was held until every other browser answered, and they did not (%s): the loop that "
                "serves them could not run while the removal waited, so the removal ran on it (answers during the removal, per "
                "browser: %s)" % (gate.get("released_by"), [r["during_removal"] for r in others]))
    return None
