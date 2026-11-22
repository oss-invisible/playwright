"""A page keeps the focus when another session takes the display, and loses it only to its own session.

What it fixes: with ``zoom.stealth.focus.ignore_os_deactivation`` (the core declares it true, one value for every session) the window does not give up its
activation when the SYSTEM takes it: another application, or another session's window on the same display. A page reads ``document.hasFocus()`` and the
``blur`` event; without the pref a second visible session made the first one read ``hasFocus()`` false (4 times in 4 on one display) while nothing in its own
session had moved. When the session opens a window of its own the window it replaces is lowered with the blur any Firefox gives.

The hooks: ``AppWindow::WindowDeactivated`` (the early return) and ``AppWindow::WindowActivated`` (``StealthLowerActiveWindow``, which lowers the active window
with its own action id before Mozilla's raise) in ``xpfe/appshell/AppWindow.cpp`` of the series, folder ``fingerprint/screen-window/focus``.

The steps: session A opens a page that records ``focus`` and ``blur``; session B, headed on the same display, opens a window and takes the focus. A's page
still reads ``hasFocus()`` true and has recorded no ``blur``. Then A opens a second page of its own: the first page records its ``blur`` and reads
``hasFocus()`` false, the new one reads it true.

The control, in the same test: ``extra_prefs={"zoom.stealth.focus.ignore_os_deactivation": False}``, the same steps up to B taking the display, and A's page
reads ``hasFocus()`` false. It runs first, so a runner where the second session does not take the focus is red there and not a green that means nothing.

Both sessions are headed on one display, and the test owns it: on Linux a private Xvfb started for the test (the ``xvfb-run`` display that the four workers
share is never used, so no neighbour's headed window moves the focus); on Windows the interactive desktop of the runner, which only this test touches because
it is ``wall_clock`` (the e2e run gives such a test the machine alone, after the parallel tests).

What the verdict waits for is the operating system, not a pause: before anything is judged the test reads from the OS that the focus really is where the step
says (the X input focus on Linux, ``GetForegroundWindow`` on Windows: first the first session's window, then the second session's window). "No blur" is judged
after that and after a round trip to the first page's own process (two painted frames), which handles everything the parent sent it before: the deactivation
that would cause a blur has been delivered by then, so a late one cannot give a false green.
"""
from __future__ import annotations

import contextlib
import sys

import pytest

from _os_focus import ForegroundWindow, XFocus, a_display_of_its_own
from _window_sessions import in_a_thread_of_its_own, launch, wait_for_condition

pytestmark = pytest.mark.e2e

PREF = "zoom.stealth.focus.ignore_os_deactivation"

RECORD = """() => {
  window.__events = [];
  for (const type of ['focus', 'blur']) window.addEventListener(type, () => window.__events.push(type));
  return document.hasFocus();
}"""
HAS_FOCUS = "() => document.hasFocus()"
EVENTS = "() => window.__events"
#: A round trip to the page's own process: it has handled what the parent sent it before this ran (the deactivation, if the parent had one to send).
ROUND_TRIP = "() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))"
#: Names the page, so that on Windows its top-level window is found by its title.
NAME = "(name) => { document.title = name; return document.title; }"
FIRST_TITLE, SECOND_TITLE = "focus-first-session", "focus-second-session"


@contextlib.contextmanager
def the_os_focus(monkeypatch):
    """What tells where the OS has put the focus, on a display this test owns (see the module docstring)."""
    if sys.platform == "win32":
        yield ForegroundWindow()
        return
    with a_display_of_its_own(monkeypatch) as display:
        reader = XFocus(display)
        try:
            yield reader
        finally:
            reader.close()


def takes_the_focus(b):
    """Runs in the second session's own thread: opens a window, names it, and waits until its page has the focus."""
    taker = b.new_page()
    taker.goto("about:blank")
    taker.evaluate(NAME, SECOND_TITLE)
    wait_for_condition(taker, lambda: taker.evaluate(HAS_FOCUS), lambda focused: focused is True, "the second session's page never took the focus")


def os_focus_is_on_the_first_window(first, os_focus):
    """Waits until the OS says the first session's window holds the focus; gives what holds it."""
    if sys.platform == "win32":
        window = wait_for_condition(first, lambda: os_focus.window_titled(FIRST_TITLE), lambda hwnd: hwnd is not None,
                                    "no single visible top-level window is titled %r" % FIRST_TITLE).history[-1][1]
        wait_for_condition(first, lambda: os_focus.ask_for_the_foreground(window), lambda holder: holder == window,
                           "the first session's window never became the foreground window")
        return window
    seen = wait_for_condition(first, os_focus.holder, os_focus.is_a_window, "no window of the display ever held the X input focus")
    return seen.history[-1][1]


def os_focus_is_on_the_second_window(first, os_focus, before):
    """Waits until the OS says a window other than the first one holds the focus (on Windows: the second session's window, by its title)."""
    if sys.platform == "win32":
        window = wait_for_condition(first, lambda: os_focus.window_titled(SECOND_TITLE), lambda hwnd: hwnd is not None,
                                    "no single visible top-level window is titled %r" % SECOND_TITLE).history[-1][1]
        assert window != before, "the second session's window is the first one's"
        wait_for_condition(first, lambda: os_focus.ask_for_the_foreground(window), lambda holder: holder == window,
                           "the second session's window never became the foreground window")
        return
    wait_for_condition(first, os_focus.holder, lambda holder: os_focus.is_a_window(holder) and holder != before,
                       "the X input focus never left the first session's window %r" % before)


def another_session_takes_the_display(firefox_binary, os_focus, keeps, with_own_page=False):
    """What A's page reads once B's window has the focus, and (``with_own_page``) once A has opened a page of its own."""
    extra = None if keeps else {PREF: False}
    with launch(firefox_binary, extra_prefs=extra) as a:
        first = a.new_page()
        first.goto("about:blank")
        first.evaluate(NAME, FIRST_TITLE)
        first.evaluate(RECORD)
        wait_for_condition(first, lambda: first.evaluate(HAS_FOCUS), lambda focused: focused is True, "the page never had the focus of its own window")
        before = os_focus_is_on_the_first_window(first, os_focus)
        with in_a_thread_of_its_own(firefox_binary) as b:
            b.run(takes_the_focus)
            os_focus_is_on_the_second_window(first, os_focus, before)
            if keeps:
                # the OS has moved the focus; one round trip to the page's process orders the read after the deactivation that would have followed
                first.evaluate(ROUND_TRIP)
            else:
                wait_for_condition(first, lambda: first.evaluate(HAS_FOCUS), lambda focused: focused is False,
                                   "the first page still has the focus although another session's window took it")
            seen = {"has_focus": first.evaluate(HAS_FOCUS), "events": first.evaluate(EVENTS)}
            if with_own_page:
                own = a.new_page()
                own.goto("about:blank")
                wait_for_condition(own, lambda: (own.evaluate(HAS_FOCUS), first.evaluate(EVENTS), first.evaluate(HAS_FOCUS)),
                                   lambda state: state[0] is True and state[1].count("blur") == 1 and state[2] is False,
                                   "a page of the session's own did not take the focus from the first with one blur")
                seen["after_own_page"] = (own.evaluate(HAS_FOCUS), first.evaluate(EVENTS), first.evaluate(HAS_FOCUS))
        return seen


@pytest.mark.wall_clock
def test_a_page_keeps_focus_when_another_session_takes_the_display(firefox_binary, monkeypatch):
    with the_os_focus(monkeypatch) as os_focus:
        control = another_session_takes_the_display(firefox_binary, os_focus, keeps=False)
        assert control["has_focus"] is False, "control: without the pref the first page still has the focus: %r" % control

        kept = another_session_takes_the_display(firefox_binary, os_focus, keeps=True, with_own_page=True)
    assert kept["has_focus"] is True, "with the pref the first page lost the focus to another session: %r" % kept
    assert "blur" not in kept["events"], "with the pref the first page recorded a blur when another session took the display: %r" % kept
    assert kept["has_focus"] != control["has_focus"]
    own_focused, events, first_focused = kept["after_own_page"]
    assert own_focused is True and events.count("blur") == 1 and first_focused is False, "a page of the session's own did not replace the first one with one blur: %r" % kept
