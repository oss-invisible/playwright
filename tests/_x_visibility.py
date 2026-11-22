"""What X itself says about two toplevels of a private display: which one is above, where they are, and what visibility X sends the lower one.

The occlusion e2e test (``test_window_occlusion_e2e.py``) judges a page's ``document.visibilityState`` after a second session's window has covered the first
one. On Linux the engine turns a window ``OCCLUDED`` only on a ``VisibilityNotify`` with ``FullyObscured`` of its toplevel (``nsWindow::OnVisibilityNotifyEvent``,
connected to the shell's ``visibility-notify-event``, ``widget/gtk/nsWindow.cpp``): whether X sent that event is the ground truth the verdict rests on, and the
test reads it without the engine, through a connection of its own to the same display (libX11, as ``_os_focus.XFocus``, whose connection and window lookup this
reuses):

* ``watch``: ``XSelectInput(VisibilityChangeMask)`` on the covered toplevel, before the cover is mapped. Every client that selects the mask gets its own copy of
  the event, so the engine's own selection is untouched;
* ``raise_and_read``: ``XRaiseWindow`` of the covering toplevel, then ONE read-back of the stacking (``XQueryTree`` of the root, bottom to top: no window
  manager reparents the toplevels) and of the two geometries (``XGetWindowAttributes``, relative to the root);
* ``drain``: the ``VisibilityNotify`` events that arrived, with the time since ``watch`` (evidence only, never a verdict).

The pure parts (``visibility_of``, ``arrangement``, ``VisibilityRecord``) are tested without a display in ``test_the_x_record_of_the_occlusion_test.py``.
"""
from __future__ import annotations

import ctypes
import time
from typing import Dict, List, Optional, Sequence, Tuple

from _os_focus import XFocus, _XWindowAttributes

#: ``VisibilityNotify`` (``X.h``) and ``VisibilityChangeMask`` (bit 16 of the event mask).
VISIBILITY_NOTIFY = 15
VISIBILITY_CHANGE_MASK = 1 << 16
#: The ``state`` of a ``VisibilityNotify``.
STATES = {0: "Unobscured", 1: "PartiallyObscured", 2: "FullyObscured"}
FULLY_OBSCURED = "FullyObscured"
#: ``map_state`` of ``XGetWindowAttributes``.
MAP_STATES = {0: "IsUnmapped", 1: "IsUnviewable", 2: "IsViewable"}


class XVisibilityEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
                ("window", ctypes.c_ulong), ("state", ctypes.c_int)]


class XEvent(ctypes.Union):
    """Xlib's ``XEvent``: the type, the visibility member, and the padding of 24 longs that gives the union its size."""
    _fields_ = [("type", ctypes.c_int), ("xvisibility", XVisibilityEvent), ("pad", ctypes.c_long * 24)]


def visibility_of(event: XEvent) -> Optional[Tuple[int, str]]:
    """(window, state name) of a ``VisibilityNotify``; None for any other event."""
    if event.type != VISIBILITY_NOTIFY:
        return None
    state = event.xvisibility.state
    return int(event.xvisibility.window), STATES.get(state, "state %d" % state)


def outer(geometry: Dict[str, int]) -> Tuple[int, int, int, int]:
    """(left, top, right, bottom) of a window with its border, from ``x``, ``y``, ``width``, ``height``, ``border``."""
    border = geometry.get("border", 0)
    return (geometry["x"], geometry["y"], geometry["x"] + geometry["width"] + 2 * border, geometry["y"] + geometry["height"] + 2 * border)


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
    """The ``VisibilityNotify`` events of one watched window, each with the seconds since the watch began; events of other windows are not kept."""

    def __init__(self, window: int) -> None:
        self.window = window
        self.events: List[Dict[str, object]] = []

    def add(self, window: int, state: str, since: float) -> None:
        if window == self.window:
            self.events.append({"state": state, "t": round(since, 4)})

    def first(self, state: str = FULLY_OBSCURED) -> Optional[Dict[str, object]]:
        return next((event for event in self.events if event["state"] == state), None)

    def line(self) -> str:
        """One line: the states X sent, in order, with their times."""
        if not self.events:
            return "X sent window 0x%x no VisibilityNotify" % self.window
        return "X sent window 0x%x: %s" % (self.window, ", ".join("%s at +%.3f s" % (event["state"], event["t"]) for event in self.events))

    def as_dict(self) -> Dict[str, object]:
        first = self.first()
        return {"window": "0x%x" % self.window, "events": list(self.events), "fully obscured arrived": first is not None,
                "fully obscured at": first["t"] if first else None}


class XVisibility(XFocus):
    """A test's own connection to the display: watches the visibility of one toplevel, raises another and reads the two back."""

    def __init__(self, display: str) -> None:
        super().__init__(display)
        x11 = self.x11
        x11.XSelectInput.argtypes = (ctypes.c_void_p, ctypes.c_ulong, ctypes.c_long)
        x11.XRaiseWindow.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        x11.XPending.argtypes = (ctypes.c_void_p,)
        x11.XNextEvent.argtypes = (ctypes.c_void_p, ctypes.POINTER(XEvent))
        self.record: Optional[VisibilityRecord] = None
        self.started = 0.0

    def drain(self) -> Optional[VisibilityRecord]:
        """Reads every event that arrived (none blocks: ``XPending`` first) into the record of the watched window."""
        self.x11.XSync(self.connection, False)
        event = XEvent()
        while self.x11.XPending(self.connection) > 0:
            self.x11.XNextEvent(self.connection, ctypes.byref(event))
            seen = visibility_of(event)
            if seen and self.record is not None:
                self.record.add(seen[0], seen[1], time.monotonic() - self.started)
        return self.record

    def watch(self, window: int) -> VisibilityRecord:
        """From now on, the ``VisibilityNotify`` events of ``window``; what was queued before (an earlier window's) is read and dropped first."""
        self.record = None
        self.drain()
        self.record = VisibilityRecord(window)
        self.started = time.monotonic()
        self.x11.XSelectInput(self.connection, window, VISIBILITY_CHANGE_MASK)
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
