"""A page cannot reach the three chrome-only methods the parent uses to map a declared screen point back to the real one.

The engine declares the window origin and the chrome offset to the content process. The parent keeps a map from declared to real screen coordinates and exposes it
to the browser's own chrome code, on ``CanonicalBrowsingContext``, as three ``[ChromeOnly]`` methods: ``stealthScreenRectToReal``, ``stealthScreenXDevPxToReal`` and
``stealthScreenYDevPxToReal``. A page that could call them would read the real window geometry from the declared one, so they must not be there for content.
This replaces the engine mochitest of the same point (a mochitest needs a build; this is one the candidate runs).

What it does, per display case (the four of the window tests), in the top document and in a frame of another site (another process), from the page's own script:

  * the roots it can reach are collected: ``window``, ``document``, ``navigator``, ``location``, ``history``, ``screen``, ``visualViewport``, ``top``, ``parent``, ``self``,
    ``frames``, the frame element's ``contentWindow`` where it is reachable, ``window.browsingContext`` and ``window.windowGlobalChild`` (both chrome-only: they must be
    undefined or throw), ``document.defaultView``, ``document.documentElement``, ``window.opener`` and ``window.frameElement``;
  * for each root, every object along its prototype chain is visited, and each of the three names is tried: as a property read (``obj[name]``), with ``in`` and with
    ``Object.getOwnPropertyDescriptor``; and the names of every own property of the chain are searched for the strings ``stealthScreen`` and ``ToReal``;
  * ``CanonicalBrowsingContext`` and ``BrowsingContext`` are tried as globals (``typeof``, ``globalThis[name]``, ``window[name]``).

Every attempt must give ``undefined`` (or ``false``, or no descriptor), or throw on access. A function, a descriptor or a name that matches is a method of the parent a page
can see, and the message names the root, the object and the name.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest
from _fission_frames import Sites, wait_frame
from _window_origin import CASES, DEADLINE_S, launch_case, wait_for

pytestmark = pytest.mark.e2e

METHODS = ["stealthScreenRectToReal", "stealthScreenXDevPxToReal", "stealthScreenYDevPxToReal"]

PAGE = "<!doctype html><meta charset=utf-8><title>exposure</title><body><iframe id=f src=\"%s\"></iframe>"
FRAME = "<!doctype html><meta charset=utf-8><title>exposure in a frame</title><body>x"

#: The page's own probe. Returns every attempt that gave something other than undefined / false / no descriptor / a throw, as strings.
PROBE = """(methods) => {
  const found = [];
  const attempt = (label, fn) => { try { return fn(); } catch (e) { return undefined; } };
  const roots = {};
  const add = (name, get) => { const v = attempt(name, get); if (v !== undefined && v !== null && (typeof v === 'object' || typeof v === 'function')) roots[name] = v; };
  add('window', () => window); add('self', () => self); add('globalThis', () => globalThis);
  add('document', () => document); add('navigator', () => navigator); add('location', () => location);
  add('history', () => history); add('screen', () => screen); add('visualViewport', () => visualViewport);
  add('top', () => top); add('parent', () => parent); add('frames', () => frames);
  add('opener', () => opener); add('frameElement', () => frameElement);
  add('document.defaultView', () => document.defaultView); add('document.documentElement', () => document.documentElement);
  add('document.body', () => document.body); add('iframe', () => document.querySelector('iframe'));
  add('iframe.contentWindow', () => document.querySelector('iframe').contentWindow);
  add('window.browsingContext', () => window.browsingContext); add('window.windowGlobalChild', () => window.windowGlobalChild);
  add('window.docShell', () => window.docShell); add('window.windowUtils', () => window.windowUtils);
  add('window.windowRoot', () => window.windowRoot); add('window.browsingContext.top', () => window.browsingContext.top);
  add('window.browsingContext.parent', () => window.browsingContext.parent);
  add('document.browsingContext', () => document.browsingContext);
  // the chrome-only members of a window must not be there for content at all
  for (const name of ['window.browsingContext', 'window.windowGlobalChild', 'window.docShell', 'window.windowUtils', 'window.windowRoot', 'document.browsingContext'])
    if (roots[name] !== undefined) found.push('a chrome-only root is reachable: ' + name);
  const seen = new Set();
  for (const [rootName, root] of Object.entries(roots)) {
    let obj = root, depth = 0;
    while (obj && depth < 20) {
      depth++;
      if (seen.has(obj)) { obj = attempt('proto', () => Object.getPrototypeOf(obj)); continue; }
      seen.add(obj);
      const own = attempt('names', () => Object.getOwnPropertyNames(obj)) || [];
      const symbols = attempt('symbols', () => Object.getOwnPropertySymbols(obj)) || [];
      for (const n of own) if (/stealthScreen|ToReal/i.test(n)) found.push(rootName + ': own property ' + n + ' on ' + attempt('ctor', () => String(obj.constructor && obj.constructor.name)));
      for (const s of symbols) if (/stealthScreen|ToReal/i.test(String(s))) found.push(rootName + ': own symbol ' + String(s));
      for (const m of methods) {
        const read = attempt('read', () => root[m]);
        if (read !== undefined) found.push(rootName + '[' + m + '] reads ' + typeof read);
        if (attempt('in', () => m in obj) === true) found.push(rootName + ': ' + m + ' in ' + attempt('ctor', () => String(obj.constructor && obj.constructor.name)));
        if (attempt('desc', () => Object.getOwnPropertyDescriptor(obj, m))) found.push(rootName + ': a descriptor of ' + m + ' on ' + attempt('ctor', () => String(obj.constructor && obj.constructor.name)));
      }
      obj = attempt('proto', () => Object.getPrototypeOf(obj));
    }
  }
  for (const g of ['CanonicalBrowsingContext', 'BrowsingContext', 'WindowContext', 'CanonicalWindowContext', 'WindowGlobalParent', 'WindowGlobalChild']) {
    if (typeof attempt(g, () => globalThis[g]) !== 'undefined') found.push('the global ' + g + ' is defined for a page');
    if (attempt(g, () => window[g]) !== undefined) found.push('window.' + g + ' is defined for a page');
    if (attempt(g, () => g in globalThis) === true) found.push(g + ' in globalThis');
  }
  return {found, roots: Object.keys(roots).length, visited: seen.size};
}"""


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_parent_methods_of_the_window_origin_are_not_exposed_to_a_page(firefox_binary, case):
    results: Dict[str, Dict[str, Any]] = {}
    with Sites() as sites:
        sites.route("A", "/page", lambda handler: PAGE % sites.url("B", "/frame"))
        sites.route("B", "/frame", FRAME)
        with launch_case(firefox_binary, case) as browser:
            page = browser.new_page()
            page.goto(sites.url("A", "/page"), wait_until="load", timeout=30000)
            frame = wait_frame(page, sites.origin["B"], "/frame", seconds=DEADLINE_S)
            wait_for(page, lambda: frame.evaluate("() => document.readyState") == "complete")
            for name, doc in (("top document", page), ("frame of another site", frame)):
                results[name] = doc.evaluate(PROBE, METHODS)
    problems: List[str] = []
    for name, result in results.items():
        # a probe that visited nothing proves nothing
        if result["roots"] < 8 or result["visited"] < 20:
            problems.append("%s: the probe reached only %d roots and visited %d objects" % (name, result["roots"], result["visited"]))
        for line in result["found"]:
            problems.append("%s: %s" % (name, line))
    assert not problems, "%s: a page can reach what is chrome-only (%d):\n  %s" % (case, len(problems), "\n  ".join(problems))
