"""The pieces the e2e tests of the language and the zone a context declares share.

* ``serve(routes)``: a local HTTP server on a port the OS gives, answering a table of paths (the query is ignored), each with its bytes, its type
  and, if the test needs them, extra headers. Nothing leaves the machine.
* ``open_chrome(port)`` / ``chrome(...)`` / ``set_pref(...)``: the engine's own chrome scope through Marionette, for what no protocol command of
  the wrapper does: writing ``intl.accept_languages`` while a session runs, and ``content_process_prefs(...)``: what a pref holds in the
  content process of each tab asked for (the proof that a process has taken a change, read from the process itself).
* ``read_in_page_realm(page, body)``: runs ``body`` as an inline ``<script>`` of the page, so in the page's own realm, and returns what it
  wrote into the DOM. ``page.evaluate`` is not the page's realm on this engine; an inline script is what a site runs.
* the measuring scripts of the canvas tests (``MEASURE_JS``): the width of a few strings as a canvas of one language group draws them.

No clock, no port written as a number, no environment written here.
"""
from __future__ import annotations

import http.server
import json
import threading
from typing import Any, Dict, Tuple, Union

import webfont_probe

#: What the launch of a browser whose chrome scope is read through Marionette is given (the env var MOZ_REMOTE_ALLOW_SYSTEM_ACCESS
#: is set by the test with ``monkeypatch``).
MARIONETTE_ARGS = ["-marionette", "-remote-allow-system-access"]

Route = Union[bytes, Tuple[bytes, str], Tuple[bytes, str, Dict[str, str]]]


def serve(routes: Dict[str, Route]):
    """A server for ``routes``; returns ``(server, base_url)``. Stop it with :func:`stop`."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            route = routes.get(path)
            if route is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if isinstance(route, bytes):
                route = (route, "text/html; charset=utf-8")
            body, kind = route[0], route[1]
            extra = route[2] if len(route) > 2 else {}
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in extra.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d" % server.server_address[1]


def stop(server) -> None:
    server.shutdown()
    server.server_close()


PAGE = b"<!doctype html><html><head><meta charset=utf-8><title>t</title></head><body><p id=p>t</p></body></html>"


# ----------------------------------------------------------------------------------------------------------------- the chrome scope

def open_chrome(port: int):
    """A Marionette session in chrome scope on the engine that listens on ``port``."""
    marionette = webfont_probe._Marionette(port)
    marionette.call("WebDriver:NewSession", {"capabilities": {"alwaysMatch": {}}})
    marionette.call("WebDriver:SetTimeouts", {"script": 240000})
    marionette.call("Marionette:SetContext", {"value": "chrome"})
    return marionette


def chrome(marionette, script: str, *args: Any) -> Any:
    """Run ``script`` (the body of a function that returns ``JSON.stringify(...)``) in chrome scope; its answer, decoded."""
    return json.loads(marionette.call("WebDriver:ExecuteScript", {"script": script, "args": list(args)})["value"])


SET_STRING_PREF_JS = """
const [name, value] = arguments;
Services.prefs.setStringPref(name, value);
return JSON.stringify(Services.prefs.getStringPref(name));
"""


def set_pref(marionette, name: str, value: str) -> str:
    return chrome(marionette, SET_STRING_PREF_JS, name, value)


#: What the content process of each tab holds for a pref, read in that process. ``arguments``: the pref's name and the labels of the tabs; a tab is
#: found by its address ending in ``?<label>``. A process script (``Services.ppmm.loadProcessScript``, a data: URL, only the processes that exist)
#: runs in every process and sends back its pid and the value ``Services.prefs`` of THAT process gives; the answer comes when each tab's process
#: has answered. The parent sends a pref change to a content process before this message on the same channel, and the process applies it
#: (and runs its observers) before it runs the script, so a process that answers has handled the change.
PROCESS_PREFS_JS = """
const [name, labels] = arguments;
const done = arguments[arguments.length - 1];
const wanted = {};
for (const win of Services.wm.getEnumerator('navigator:browser')) {
  for (const tab of win.gBrowser.tabs) {
    const spec = tab.linkedBrowser.currentURI.spec;
    const wg = tab.linkedBrowser.browsingContext && tab.linkedBrowser.browsingContext.currentWindowGlobal;
    for (const label of labels) {
      if (spec.endsWith('?' + label) && wg && wg.domProcess) { wanted[label] = wg.domProcess.osPid; }
    }
  }
}
const missing = labels.filter((label) => !(label in wanted));
if (missing.length) { done(JSON.stringify({missing})); return; }
const KEY = 'invisible-probe:pref';
const answers = {};
const listener = {
  receiveMessage(message) {
    answers[message.data.pid] = message.data.value;
    if (Object.values(wanted).every((pid) => pid in answers)) {
      Services.ppmm.removeMessageListener(KEY, listener);
      const prefs = {};
      for (const [label, pid] of Object.entries(wanted)) { prefs[label] = answers[pid]; }
      done(JSON.stringify({pids: wanted, prefs}));
    }
  },
};
Services.ppmm.addMessageListener(KEY, listener);
const body = 'sendAsyncMessage(' + JSON.stringify(KEY) + ', {pid: Services.appinfo.processID, value: Services.prefs.getStringPref(' + JSON.stringify(name) + ', "")});';
Services.ppmm.loadProcessScript('data:,' + encodeURIComponent(body), false);
"""


def content_process_prefs(marionette, name: str, labels) -> Dict[str, Any]:
    """``{"pids": {label: pid}, "prefs": {label: value}}``: what pref ``name`` holds in the content process of each tab whose address ends with
    ``?<label>`` (or ``{"missing": [labels]}`` when a tab is not there)."""
    answer = marionette.call("WebDriver:ExecuteAsyncScript", {"script": PROCESS_PREFS_JS, "args": [name, list(labels)]})
    return json.loads(answer["value"])


# -------------------------------------------------------------------------------------------------------------------- the page realm

def read_in_page_realm(page, body: str, name: str = "read") -> Any:
    """What ``body`` (the body of a function that returns a JSON-able value) gives when an inline script of the page runs it."""
    page.add_script_tag(content=("document.documentElement.setAttribute('data-%s', JSON.stringify((() => {%s})()));" % (name, body)))
    return json.loads(page.evaluate("n => document.documentElement.getAttribute('data-' + n)", name))


# ------------------------------------------------------------------------------------------------------------------------ the workers

#: A dedicated worker that answers every message with the language its realm and its navigator speak.
LANG_WORKER = b"""
onmessage = () => postMessage({languages: [...navigator.languages],
                               intl: Intl.DateTimeFormat().resolvedOptions().locale});
"""

#: Starts that worker in the page and leaves ``window.__ask()`` to ask it again; the worker lives as long as the page.
START_LANG_WORKER_JS = """() => {
  window.__worker = new Worker('/lang-worker.js');
  window.__ask = () => new Promise((ok) => {
    window.__worker.onmessage = (e) => ok(e.data);
    window.__worker.postMessage(0);
  });
}"""

ASK_LANG_WORKER_JS = "() => window.__ask()"


# ----------------------------------------------------------------------------------------------------------------------- the canvases

#: The width of a few strings, in two generic families, as a canvas of ONE language group draws them: Latin text (the generic serif of each
#: group is another font), full-width Han, kana, half-width kana. ``widths(canvas)`` takes the 2D context's canvas, a main-thread or a
#: worker's OffscreenCanvas, or an element: the font is resolved the way each of them resolves it, which is what the tests compare.
MEASURE_JS = """
const STRINGS = ['Hamburgefonstiv', '\\u6f22\\u5b57', '\\u3042\\u3044\\u3046', '\\uff71\\uff72\\uff73', '\\u76f4\\u9aa8'];
function widths(context) {
  const out = {};
  for (const font of ['20px serif', '20px sans-serif']) {
    context.font = font;
    out[font] = STRINGS.map((s) => context.measureText(s).width);
  }
  return out;
}
"""

#: A dedicated worker that measures on its own OffscreenCanvas and answers with the widths.
MEASURE_WORKER = (MEASURE_JS + """
onmessage = () => postMessage(widths(new OffscreenCanvas(300, 60).getContext('2d')));
""").encode()

#: In the page: the worker's widths, the widths of an OffscreenCanvas of the main thread (no element, no ``lang``), and the widths of a canvas
#: ELEMENT whose ``lang`` is each of the two languages asked for.
MEASURE_PAGE_JS = """async (languages) => {
  %s
  const worker = await new Promise((ok) => {
    const w = new Worker('/measure-worker.js');
    w.onmessage = (e) => { ok(e.data); w.terminate(); };
    w.postMessage(0);
  });
  const offscreen = widths(new OffscreenCanvas(300, 60).getContext('2d'));
  const element = {};
  for (const lang of languages) {
    const c = document.createElement('canvas');
    c.setAttribute('lang', lang);
    document.body.appendChild(c);
    element[lang] = widths(c.getContext('2d'));
  }
  return {worker, offscreen, element};
}""" % MEASURE_JS
