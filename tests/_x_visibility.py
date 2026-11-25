"""What X itself says about two toplevels of a private display: which one is above, where they are, what visibility X sends the lower one, and whether the
pointer or the focus moved while it was covered.

The occlusion e2e test (``test_window_occlusion_e2e.py``) judges a page's ``document.visibilityState`` after a window has covered the first session's one. On
Linux the engine turns a window ``OCCLUDED`` only on a ``VisibilityNotify`` with ``FullyObscured`` of its toplevel (``nsWindow::OnVisibilityNotifyEvent``,
connected to the shell's ``visibility-notify-event``, ``widget/gtk/nsWindow.cpp``): whether X sent that event is the ground truth the verdict rests on, and the
test reads it without the engine, through a connection of its own to the same display (libX11, as ``_os_focus.XFocus``, whose connection and window lookup this
reuses).

The cover on Linux is a window of the test's own (``cover_window``): an override-redirect ``InputOutput`` window on the private display, larger than the covered
toplevel on every side (``COVER_MARGIN``, inside the screen when it fits), mapped and raised, and never given the input focus (nothing calls ``XSetInputFocus``).
It is not a second Firefox session's window, as it was before. Why the arrangement must not move the pointer: the private Xvfb has no window manager, so the
focus is ``PointerRoot`` and GDK treats the toplevel under the pointer as the focused one. Any window mapped over the pointer makes the pointer leave the covered
toplevel; GDK turns that crossing into a GTK focus-out (``OnContainerFocusOutEvent``, then a ``leave notify``) and GTK draws a window that loses the focus
(``nsWindow::OnExposeEvent``, ``NotifyOcclusionState(VISIBLE)``). When X delivered ``PartiallyObscured`` and ``FullyObscured`` close to that crossing, the draw
ran 0.4 to 15 ms AFTER the ``FullyObscured`` and turned the window ``VISIBLE`` again before the posted visibility update ran, so the page never recorded
``hidden`` (hunt run 45603211231, 58 reds of 1294; shard 1 it-0014: ``VisibilityNotify state 1`` at 54.355670, ``OnContainerFocusOutEvent`` at 54.355752,
``state 2`` at 54.356290, ``OnExposeEvent`` with ``mIsFullyOccluded 0`` at 54.359166). That is Mozilla's behaviour, in retail too. The first reading, that the
focus moved to a second session's window and X sent a ``FocusOut``, was refuted by run 45620136865: 458 reds of 463 with an X cover over the pointer, and no
``FocusOut`` in 40 iterations of the old arrangement; the trigger is the pointer crossing. So the test warps the pointer to the bottom-right pixel of the
root (computed from the root's geometry) before any session starts, the cover keeps the pointer outside it, and ``pointer_or_focus_moved`` judges that no
crossing and no focus event reached the covered toplevel and that the pointer, read back once, is outside both rectangles.

* ``warp_pointer_to_the_corner``: ``XWarpPointer`` through this connection to ``(width - 1, height - 1)`` of the root, then ``XSync``;
* ``watch``: ``XSelectInput(VisibilityChangeMask | FocusChangeMask | EnterWindowMask | LeaveWindowMask)`` on the covered toplevel, before the cover is mapped.
  Every client that selects a mask gets its own copy of the events, so the engine's own selection is untouched. The ``EnterNotify``, ``LeaveNotify``,
  ``FocusIn`` and ``FocusOut`` events are kept with their time, mode and detail;
* ``cover_window``: reads the pointer (``XQueryPointer`` of the root), creates, maps and raises the cover (``cover_rectangle`` gives its rectangle, the pointer
  outside it); ``destroy_window`` destroys it;
* ``raise_and_read``: ``XRaiseWindow`` of the covering toplevel, then ONE read-back of the stacking (``XQueryTree`` of the root, bottom to top: no window
  manager reparents the toplevels) and of the two geometries (``XGetWindowAttributes``, relative to the root);
* ``read_pointer_back``: ONE ``XQueryPointer`` right after the cover is up, kept in the record with whether the pointer lies outside both rectangles;
* ``drain``: the visibility, crossing and focus events that arrived, with the time since ``watch`` (evidence, and the precondition);
* ``shrink_by_one``: ``XResizeWindow`` of the covered toplevel, one pixel narrower, the cover left above it and larger: a draw of a window that stays
  covered (see ``control_sees_hidden``).

The pure parts (``visibility_of``, ``focus_of``, ``crossing_of``, ``cover_rectangle``, ``arrangement``, ``VisibilityRecord``, ``pointer_or_focus_moved``,
``control_sees_hidden``, ``product_stays_visible``) are tested without a display in ``test_the_x_record_of_the_occlusion_test.py``.
"""
from __future__ import annotations

import ctypes
import time
from typing import Dict, List, Optional, Sequence, Tuple

from _os_focus import XFocus, _XWindowAttributes

#: ``VisibilityNotify`` (``X.h``) and ``VisibilityChangeMask`` (bit 16 of the event mask).
VISIBILITY_NOTIFY = 15
VISIBILITY_CHANGE_MASK = 1 << 16
#: ``FocusIn`` and ``FocusOut`` (``X.h``) and ``FocusChangeMask`` (bit 21 of the event mask).
FOCUS_IN, FOCUS_OUT = 9, 10
FOCUS_CHANGE_MASK = 1 << 21
#: The ``mode`` and the ``detail`` of a focus event (``X.h``).
FOCUS_MODES = {0: "NotifyNormal", 1: "NotifyGrab", 2: "NotifyUngrab", 3: "NotifyWhileGrabbed"}
FOCUS_DETAILS = {0: "NotifyAncestor", 1: "NotifyVirtual", 2: "NotifyInferior", 3: "NotifyNonlinear", 4: "NotifyNonlinearVirtual", 5: "NotifyPointer",
                 6: "NotifyPointerRoot", 7: "NotifyDetailNone"}
FOCUS_NAMES = {FOCUS_IN: "FocusIn", FOCUS_OUT: "FocusOut"}
#: ``EnterNotify`` and ``LeaveNotify`` (``X.h``), ``EnterWindowMask`` (bit 4) and ``LeaveWindowMask`` (bit 5); their ``mode`` and ``detail`` read as the focus ones.
ENTER_NOTIFY, LEAVE_NOTIFY = 7, 8
ENTER_WINDOW_MASK, LEAVE_WINDOW_MASK = 1 << 4, 1 << 5
CROSSING_NAMES = {ENTER_NOTIFY: "EnterNotify", LEAVE_NOTIFY: "LeaveNotify"}
#: What ``watch`` selects on the covered toplevel.
WATCH_MASK = VISIBILITY_CHANGE_MASK | FOCUS_CHANGE_MASK | ENTER_WINDOW_MASK | LEAVE_WINDOW_MASK
#: ``CWBackPixel`` and ``CWOverrideRedirect`` of ``XChangeWindowAttributes``, the margin the cover has on every side of the covered toplevel (pixels) and its
#: background pixel.
CW_BACK_PIXEL, CW_OVERRIDE_REDIRECT = 1 << 1, 1 << 9
COVER_MARGIN = 32
COVER_BACKGROUND = 0x303030
#: The ``state`` of a ``VisibilityNotify``.
STATES = {0: "Unobscured", 1: "PartiallyObscured", 2: "FullyObscured"}
FULLY_OBSCURED = "FullyObscured"
#: ``map_state`` of ``XGetWindowAttributes``.
MAP_STATES = {0: "IsUnmapped", 1: "IsUnviewable", 2: "IsViewable"}


def control_sees_hidden(changes: Sequence[str], not_before: int = 1) -> bool:
    """The verdict of the control half (tracking enabled): the page recorded ``hidden`` among its ``visibilitychange`` events at or after index ``not_before``.

    ``changes`` is the list the page records (``document.visibilityState`` at the start, then one entry per ``visibilitychange``); ``not_before`` is the length
    that list had at the last look made BEFORE X's ``FullyObscured`` showed up (1: the start), so a ``hidden`` at an index below it cannot be its effect.

    Not the final state: Firefox's ``nsWindow::OnExposeEvent`` calls ``NotifyOcclusionState(VISIBLE)`` on every draw of the window's container, and a
    draw that comes after the ``FullyObscured`` turns the window ``VISIBLE`` again while X, whose state has not changed, says nothing more. The page then
    reads ``visible`` at the end of a run in which the engine did make it ``hidden``: ``visible, hidden, visible``.
    """
    return "hidden" in list(changes)[max(not_before, 0):]


def product_stays_visible(changes: Sequence[str]) -> bool:
    """The verdict of the product half (tracking off): the page never turned ``hidden`` and got no ``visibilitychange`` at all."""
    return list(changes) == ["visible"]


class XVisibilityEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
                ("window", ctypes.c_ulong), ("state", ctypes.c_int)]


class XFocusChangeEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
                ("window", ctypes.c_ulong), ("mode", ctypes.c_int), ("detail", ctypes.c_int)]


class XCrossingEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
                ("window", ctypes.c_ulong), ("root", ctypes.c_ulong), ("subwindow", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("x", ctypes.c_int), ("y", ctypes.c_int), ("x_root", ctypes.c_int), ("y_root", ctypes.c_int), ("mode", ctypes.c_int),
                ("detail", ctypes.c_int), ("same_screen", ctypes.c_int), ("focus", ctypes.c_int), ("state", ctypes.c_uint)]


class XSetWindowAttributes(ctypes.Structure):
    _fields_ = [("background_pixmap", ctypes.c_ulong), ("background_pixel", ctypes.c_ulong), ("border_pixmap", ctypes.c_ulong), ("border_pixel", ctypes.c_ulong),
                ("bit_gravity", ctypes.c_int), ("win_gravity", ctypes.c_int), ("backing_store", ctypes.c_int), ("backing_planes", ctypes.c_ulong),
                ("backing_pixel", ctypes.c_ulong), ("save_under", ctypes.c_int), ("event_mask", ctypes.c_long), ("do_not_propagate_mask", ctypes.c_long),
                ("override_redirect", ctypes.c_int), ("colormap", ctypes.c_ulong), ("cursor", ctypes.c_ulong)]


class XEvent(ctypes.Union):
    """Xlib's ``XEvent``: the type, the visibility, focus and crossing members, and the padding of 24 longs that gives the union its size."""
    _fields_ = [("type", ctypes.c_int), ("xvisibility", XVisibilityEvent), ("xfocus", XFocusChangeEvent), ("xcrossing", XCrossingEvent),
                ("pad", ctypes.c_long * 24)]


def visibility_of(event: XEvent) -> Optional[Tuple[int, str]]:
    """(window, state name) of a ``VisibilityNotify``; None for any other event."""
    if event.type != VISIBILITY_NOTIFY:
        return None
    state = event.xvisibility.state
    return int(event.xvisibility.window), STATES.get(state, "state %d" % state)


def focus_of(event: XEvent) -> Optional[Tuple[int, str, str, str]]:
    """(window, ``FocusIn`` or ``FocusOut``, mode name, detail name) of a focus event; None for any other event."""
    if event.type not in FOCUS_NAMES:
        return None
    focus = event.xfocus
    return (int(focus.window), FOCUS_NAMES[event.type], FOCUS_MODES.get(focus.mode, "mode %d" % focus.mode),
            FOCUS_DETAILS.get(focus.detail, "detail %d" % focus.detail))


def crossing_of(event: XEvent) -> Optional[Tuple[int, str, str, str, int, int]]:
    """(window, ``EnterNotify`` or ``LeaveNotify``, mode name, detail name, x_root, y_root) of a crossing event; None for any other event."""
    if event.type not in CROSSING_NAMES:
        return None
    crossing = event.xcrossing
    return (int(crossing.window), CROSSING_NAMES[event.type], FOCUS_MODES.get(crossing.mode, "mode %d" % crossing.mode),
            FOCUS_DETAILS.get(crossing.detail, "detail %d" % crossing.detail), int(crossing.x_root), int(crossing.y_root))


def corner_of(screen: Dict[str, int]) -> Tuple[int, int]:
    """The point the pointer is warped to: the bottom-right pixel of the root, from its read-back geometry (never a fixed number)."""
    return (int(screen["width"]) - 1, int(screen["height"]) - 1)


def point_in(point: Tuple[int, int], rectangle: Tuple[int, int, int, int]) -> bool:
    """Whether ``point`` is a pixel of ``rectangle`` (left, top, right, bottom; the right and bottom edges are outside, as in X)."""
    return rectangle[0] <= point[0] < rectangle[2] and rectangle[1] <= point[1] < rectangle[3]


def outer(geometry: Dict[str, int]) -> Tuple[int, int, int, int]:
    """(left, top, right, bottom) of a window with its border, from ``x``, ``y``, ``width``, ``height``, ``border``."""
    border = geometry.get("border", 0)
    return (geometry["x"], geometry["y"], geometry["x"] + geometry["width"] + 2 * border, geometry["y"] + geometry["height"] + 2 * border)


def cover_rectangle(covered: Dict[str, int], screen: Dict[str, int], margin: int = COVER_MARGIN,
                    pointer: Optional[Tuple[int, int]] = None) -> Tuple[int, int, int, int]:
    """(x, y, width, height) of the cover: the covered toplevel's rectangle with its border (``outer``) and ``margin`` pixels more on every side, kept inside
    the screen (``screen``: ``width`` and ``height`` of the root) where that fits; a side that the screen cuts is the covered window's own side, so the cover
    always contains the covered rectangle.

    With a ``pointer`` (x, y in root coordinates) the cover also leaves that pixel outside it: on the side where the margin would include the pointer, the
    margin shrinks until the pointer is outside (never below the covered rectangle). A pointer inside the covered rectangle itself has no such side: an
    AssertionError "arrangement: the pointer is inside the covered window, no free point on the screen"."""
    left, top, right, bottom = outer(covered)
    inner = (left, top, right, bottom)
    if pointer is not None and point_in(pointer, inner):
        raise AssertionError("arrangement: the pointer is inside the covered window, no free point on the screen: pointer %r, covered %r" % (tuple(pointer), inner))
    left, top = max(left - margin, min(0, left)), max(top - margin, min(0, top))
    right, bottom = min(right + margin, max(screen["width"], right)), min(bottom + margin, max(screen["height"], bottom))
    if pointer is not None and point_in(pointer, (left, top, right, bottom)):
        px, py = pointer
        il, it, ir, ib = inner
        if px >= ir:
            right = px
        elif px < il:
            left = px + 1
        elif py >= ib:
            bottom = py
        else:
            top = py + 1
    return (left, top, right - left, bottom - top)


def arrangement(stacking: Sequence[int], covered: int, cover: int, geometries: Dict[int, Optional[Dict[str, int]]]) -> Dict[str, object]:
    """What the read-back says: ``ok`` when the cover is above the covered toplevel among the root's children (``stacking``, bottom to top) and the
    covered one's rectangle lies inside the cover's; with the positions and the two rectangles that decided it."""
    position = {window: index for index, window in enumerate(stacking)}
    above = covered in position and cover in position and position[cover] > position[covered]
    inner, wrapping = geometries.get(covered), geometries.get(cover)
    inside = False
    if inner and wrapping:
        (il, it, ir, ib), (ol, ot, orr, ob) = outer(inner), outer(wrapping)
        inside = ol <= il and ot <= it and ir <= orr and ib <= ob
    return {"ok": bool(above and inside), "cover above": above, "inside": inside, "covered position": position.get(covered),
            "cover position": position.get(cover), "covered rect": outer(inner) if inner else None, "cover rect": outer(wrapping) if wrapping else None}


class VisibilityRecord:
    """The ``VisibilityNotify``, crossing (``EnterNotify``, ``LeaveNotify``) and focus (``FocusIn``, ``FocusOut``) events of one watched window, each with the
    seconds since the watch began; events of other windows are not kept."""

    def __init__(self, window: int) -> None:
        self.window = window
        self.events: List[Dict[str, object]] = []
        self.focus: List[Dict[str, object]] = []
        self.crossings: List[Dict[str, object]] = []
        #: The pointer read back once after the cover was up (``XVisibility.read_pointer_back``), or None.
        self.pointer: Optional[Dict[str, object]] = None

    def add(self, window: int, state: str, since: float) -> None:
        if window == self.window:
            self.events.append({"state": state, "t": round(since, 4)})

    def add_focus(self, window: int, kind: str, mode: str, detail: str, since: float) -> None:
        if window == self.window:
            self.focus.append({"event": kind, "mode": mode, "detail": detail, "t": round(since, 4)})

    def add_crossing(self, window: int, kind: str, mode: str, detail: str, since: float, x_root: int = 0, y_root: int = 0) -> None:
        if window == self.window:
            self.crossings.append({"event": kind, "mode": mode, "detail": detail, "t": round(since, 4), "at": [x_root, y_root]})

    def first(self, state: str = FULLY_OBSCURED) -> Optional[Dict[str, object]]:
        return next((event for event in self.events if event["state"] == state), None)

    def focus_out(self) -> Optional[Dict[str, object]]:
        """The first ``FocusOut`` the watched window got, or None."""
        return next((event for event in self.focus if event["event"] == "FocusOut"), None)

    def line(self) -> str:
        """One line: the states X sent, in order, with their times, then the crossings and the focus events (none: nothing is said of them)."""
        if not self.events:
            seen = "X sent window 0x%x no VisibilityNotify" % self.window
        else:
            seen = "X sent window 0x%x: %s" % (self.window, ", ".join("%s at +%.3f s" % (event["state"], event["t"]) for event in self.events))
        if self.crossings:
            seen += "; pointer: %s" % ", ".join("%s at +%.3f s (%s, %s)" % (event["event"], event["t"], event["mode"], event["detail"]) for event in self.crossings)
        if self.focus:
            seen += "; focus: %s" % ", ".join("%s at +%.3f s (%s, %s)" % (event["event"], event["t"], event["mode"], event["detail"]) for event in self.focus)
        return seen

    def as_dict(self) -> Dict[str, object]:
        first, out = self.first(), self.focus_out()
        return {"window": "0x%x" % self.window, "events": list(self.events), "fully obscured arrived": first is not None,
                "fully obscured at": first["t"] if first else None, "focus events": list(self.focus), "focus out arrived": out is not None,
                "focus out at": out["t"] if out else None, "crossings": list(self.crossings), "pointer read back": self.pointer}


def pointer_or_focus_moved(record: Optional[VisibilityRecord]) -> str:
    """The judgement of the precondition: '' when the covered toplevel got no ``EnterNotify``, ``LeaveNotify``, ``FocusIn`` or ``FocusOut`` since the watch began
    and the pointer, read back once after the cover was up, lies outside both rectangles; else the message of the red, which starts with "arrangement: the
    covering moved the pointer or the focus" and carries the X record. A pointer crossing is turned by GDK into a GTK focus-out and that makes GTK draw the
    window (``nsWindow::OnExposeEvent``, ``NotifyOcclusionState(VISIBLE)``) at a moment the test does not control, so a run that had one proves nothing about
    the verdict. A record with no pointer read back is not judged on the pointer (the test asserts that the read-back was made)."""
    if record is None:
        return ""
    reasons = []
    if record.crossings:
        first = record.crossings[0]
        reasons.append("the covered toplevel got %s at +%.3f s after the watch began (%d crossing event(s))" % (first["event"], first["t"], len(record.crossings)))
    if record.focus:
        first = record.focus[0]
        reasons.append("the covered toplevel got %s at +%.3f s after the watch began (%d focus event(s))" % (first["event"], first["t"], len(record.focus)))
    pointer = record.pointer
    if pointer is not None and not (pointer.get("outside covered") and pointer.get("outside cover")):
        reasons.append("the pointer read back at %r is inside the covered rectangle (outside: %s) or the cover's (outside: %s)" % (
            pointer.get("at"), pointer.get("outside covered"), pointer.get("outside cover")))
    if not reasons:
        return ""
    return ("arrangement: the covering moved the pointer or the focus: %s, so the page may have been drawn again after X's FullyObscured and the run proves "
            "nothing about the engine; %s" % ("; ".join(reasons), record.line()))


class XVisibility(XFocus):
    """A test's own connection to the display: watches the visibility and the focus events of one toplevel, covers it with a window of its own, raises that
    window and reads the two back."""

    def __init__(self, display: str) -> None:
        super().__init__(display)
        x11 = self.x11
        x11.XSelectInput.argtypes = (ctypes.c_void_p, ctypes.c_ulong, ctypes.c_long)
        x11.XCreateSimpleWindow.restype = ctypes.c_ulong
        x11.XCreateSimpleWindow.argtypes = (ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
                                            ctypes.c_ulong, ctypes.c_ulong)
        x11.XChangeWindowAttributes.argtypes = (ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.POINTER(XSetWindowAttributes))
        x11.XMapWindow.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        x11.XDestroyWindow.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        x11.XRaiseWindow.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        x11.XWarpPointer.argtypes = (ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_int,
                                     ctypes.c_int)
        x11.XQueryPointer.argtypes = (ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                                      ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                                      ctypes.POINTER(ctypes.c_uint))
        x11.XResizeWindow.argtypes = (ctypes.c_void_p, ctypes.c_ulong, ctypes.c_uint, ctypes.c_uint)
        x11.XPending.argtypes = (ctypes.c_void_p,)
        x11.XNextEvent.argtypes = (ctypes.c_void_p, ctypes.POINTER(XEvent))
        self.record: Optional[VisibilityRecord] = None
        self.started = 0.0
        #: Where ``warp_pointer_to_the_corner`` put the pointer, and the root geometry it was computed from.
        self.warped: Optional[Dict[str, object]] = None

    def query_pointer(self) -> Tuple[int, int]:
        """The pointer's position in root coordinates (``XQueryPointer`` of the root)."""
        root, child = ctypes.c_ulong(), ctypes.c_ulong()
        root_x, root_y, win_x, win_y, mask = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_uint()
        self.x11.XQueryPointer(self.connection, self.root, ctypes.byref(root), ctypes.byref(child), ctypes.byref(root_x), ctypes.byref(root_y),
                               ctypes.byref(win_x), ctypes.byref(win_y), ctypes.byref(mask))
        return (int(root_x.value), int(root_y.value))

    def warp_pointer_to_the_corner(self) -> Tuple[int, int]:
        """Before any session starts: ``XWarpPointer`` through this connection to the bottom-right pixel of the root (``corner_of`` its read-back geometry), then
        ``XSync``. No toplevel exists yet, so none gets a crossing from it; the pointer is read back and kept in ``warped``."""
        screen = self.geometry(self.root)
        assert screen, "arrangement: the geometry of the root could not be read"
        x, y = corner_of(screen)
        self.x11.XWarpPointer(self.connection, 0, self.root, 0, 0, 0, 0, x, y)
        self.x11.XSync(self.connection, False)
        self.warped = {"to": [x, y], "root": [screen["width"], screen["height"]], "read back": list(self.query_pointer())}
        return (x, y)

    def drain(self) -> Optional[VisibilityRecord]:
        """Reads every event that arrived (none blocks: ``XPending`` first) into the record of the watched window: visibility, crossing and focus events."""
        self.x11.XSync(self.connection, False)
        event = XEvent()
        while self.x11.XPending(self.connection) > 0:
            self.x11.XNextEvent(self.connection, ctypes.byref(event))
            seen, focus, crossing = visibility_of(event), focus_of(event), crossing_of(event)
            if self.record is not None:
                if crossing:
                    self.record.add_crossing(crossing[0], crossing[1], crossing[2], crossing[3], time.monotonic() - self.started, crossing[4], crossing[5])
                if seen:
                    self.record.add(seen[0], seen[1], time.monotonic() - self.started)
                if focus:
                    self.record.add_focus(focus[0], focus[1], focus[2], focus[3], time.monotonic() - self.started)
        return self.record

    def watch(self, window: int) -> VisibilityRecord:
        """From now on, the ``VisibilityNotify``, ``EnterNotify``/``LeaveNotify`` and ``FocusIn``/``FocusOut`` events of ``window``; what was queued before (an
        earlier window's) is read and dropped first."""
        self.record = None
        self.drain()
        self.record = VisibilityRecord(window)
        self.started = time.monotonic()
        self.x11.XSelectInput(self.connection, window, WATCH_MASK)
        self.x11.XSync(self.connection, False)
        return self.record

    def stacking(self) -> List[int]:
        """The root's children, bottom to top (``XQueryTree``)."""
        root, parent, children, count = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.POINTER(ctypes.c_ulong)(), ctypes.c_uint()
        if not self.x11.XQueryTree(self.connection, self.root, ctypes.byref(root), ctypes.byref(parent), ctypes.byref(children), ctypes.byref(count)):
            return []
        try:
            return [int(children[i]) for i in range(count.value)]
        finally:
            if children:
                self.x11.XFree(children)

    def geometry(self, window: int) -> Optional[Dict[str, object]]:
        attributes = _XWindowAttributes()
        if not self.x11.XGetWindowAttributes(self.connection, window, ctypes.byref(attributes)):
            return None
        return {"x": attributes.x, "y": attributes.y, "width": attributes.width, "height": attributes.height, "border": attributes.border_width,
                "map state": MAP_STATES.get(attributes.map_state, attributes.map_state), "override redirect": bool(attributes.override_redirect)}

    def cover_window(self, covered: int, margin: int = COVER_MARGIN) -> int:
        """The cover: an override-redirect ``InputOutput`` window of this connection, covering ``covered`` with ``margin`` pixels more on every side (inside the
        screen where it fits, the pointer, read with ``XQueryPointer``, outside it: ``cover_rectangle``), mapped and raised, the requests flushed. An
        override-redirect window is not managed, and nothing here asks for the input focus (no ``XSetInputFocus``). No event is selected on it. The pointer
        inside the covered window is an AssertionError "arrangement: the pointer is inside the covered window, no free point on the screen"."""
        inner, screen = self.geometry(covered), self.geometry(self.root)
        assert inner and screen, "arrangement: the geometry of the covered toplevel or of the root could not be read: %r, %r" % (inner, screen)
        x, y, width, height = cover_rectangle(inner, screen, margin, self.query_pointer())
        window = int(self.x11.XCreateSimpleWindow(self.connection, self.root, x, y, width, height, 0, 0, COVER_BACKGROUND))
        assert window, "arrangement: XCreateSimpleWindow gave no window"
        attributes = XSetWindowAttributes()
        attributes.override_redirect = 1
        attributes.background_pixel = COVER_BACKGROUND
        self.x11.XChangeWindowAttributes(self.connection, window, CW_OVERRIDE_REDIRECT | CW_BACK_PIXEL, ctypes.byref(attributes))
        self.x11.XMapWindow(self.connection, window)
        self.x11.XRaiseWindow(self.connection, window)
        self.x11.XSync(self.connection, False)
        return window

    def read_pointer_back(self, covered: int, cover: int) -> Dict[str, object]:
        """ONE ``XQueryPointer`` right after the cover is up: where the pointer is and whether it lies outside the covered rectangle and the cover's. Kept in the
        record of the watch (``pointer``) and returned."""
        at = self.query_pointer()
        rectangles = {name: outer(geometry) if geometry else None for name, geometry in (("covered", self.geometry(covered)), ("cover", self.geometry(cover)))}
        seen = {"at": list(at), "covered rect": rectangles["covered"], "cover rect": rectangles["cover"],
                "outside covered": rectangles["covered"] is not None and not point_in(at, rectangles["covered"]),
                "outside cover": rectangles["cover"] is not None and not point_in(at, rectangles["cover"])}
        if self.record is not None:
            self.record.pointer = seen
        return seen

    def destroy_window(self, window: int) -> None:
        """``XDestroyWindow`` of a window this connection made (the cover); nothing else."""
        self.x11.XDestroyWindow(self.connection, window)
        self.x11.XSync(self.connection, False)

    def raise_and_read(self, covered: int, cover: int) -> Dict[str, object]:
        """``XRaiseWindow`` of ``cover``, then the one read-back: the stacking, the geometries of the two, and every viewable child of the root."""
        self.x11.XRaiseWindow(self.connection, cover)
        self.x11.XSync(self.connection, False)
        order = self.stacking()
        geometries = {window: self.geometry(window) for window in (covered, cover)}
        verdict = arrangement(order, covered, cover, geometries)
        viewable = []
        for window in order:
            seen = geometries.get(window) or self.geometry(window)
            if seen and seen["map state"] == "IsViewable":
                viewable.append({"window": "0x%x" % window, "title": self._title(window), **seen})
        return {**verdict, "covered": "0x%x" % covered, "cover": "0x%x" % cover, "covered geometry": geometries[covered],
                "cover geometry": geometries[cover], "viewable children of the root, bottom to top": viewable}

    def shrink_by_one(self, window: int) -> Dict[str, object]:
        """``XResizeWindow``: ``window`` one pixel narrower, its place and its stacking as they are. The window system draws the new size, a draw of a window
        that a larger one still covers. The geometry before and after, read through the same connection."""
        before = self.geometry(window)
        if before and before["width"] > 1:
            self.x11.XResizeWindow(self.connection, window, before["width"] - 1, before["height"])
            self.x11.XSync(self.connection, False)
        return {"before": before, "after": self.geometry(window)}
