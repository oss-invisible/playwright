"""HUNT ONLY, never merged: the published packages against stock Playwright, per browser: launch, evaluate round trip, goto -> the
request at the server, close, and CPU. Runs with the python of a venv that has the published invisible-playwright installed (not this
checkout).

    python hunt_published_latency.py ARM N_BROWSERS ROUND [binary]

  inproc     our wrapper in-process, N browsers in this Python process
  remote     launch_server, one Python process per browser, stock Playwright's client connects
  stock      Playwright's own Firefox and client, N browsers in this process
(the "one browser per process" arm is `inproc 1`, started four times at once by the workflow)

Each browser prints one HUNTROW JSON line: launch_ms, close_ms, eval_ms (100 round trips of evaluate("1+1")), goto_req_ms (10 gotos:
the client's goto call -> the request arriving at the local server), cpu_s (CPU seconds of the browser's processes during the work).
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import psutil

ARRIVED = {}
EVALS = 100
GOTOS = 10


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        q = parse_qs(urlsplit(self.path).query)
        key = (q.get("b", [""])[0], q.get("i", [""])[0])
        ARRIVED.setdefault(key, time.time() * 1000)
        body = b"<!doctype html><title>p</title><body>p</body>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def engine_cpu():
    """CPU seconds of every firefox process under this one, by pid."""
    out = {}
    for p in psutil.Process().children(recursive=True):
        try:
            if "firefox" in p.name().lower() or "plugin-container" in p.name().lower():
                t = p.cpu_times()
                out[p.pid] = t.user + t.system
        except psutil.Error:
            pass
    return out


async def work(page, port, tag):
    goto = []
    for i in range(GOTOS):
        t0 = time.time() * 1000
        await page.goto("http://127.0.0.1:%d/p?b=%s&i=%d" % (port, tag, i), wait_until="load", timeout=30000)
        arrived = ARRIVED.get((tag, str(i)))
        goto.append(round(arrived - t0, 1) if arrived else None)
    ev = []
    for _ in range(EVALS):
        t0 = time.perf_counter()
        await page.evaluate("1 + 1")
        ev.append(round((time.perf_counter() - t0) * 1000, 2))
    return goto, ev


async def serve_endpoint(binary):
    proc = await asyncio.create_subprocess_exec(sys.executable, __file__, "serve", binary,
                                                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    while True:
        line = (await asyncio.wait_for(proc.stdout.readline(), 120)).decode().strip()
        if not line:
            err = (await proc.stderr.read()).decode(errors="replace")[-600:]
            raise RuntimeError("the server process ended before it served: " + err)
        if line.startswith("ENDPOINT "):
            return proc, line.split(" ", 1)[1]


def serve_one(binary):
    from invisible_playwright._juggler.remote import launch_server
    server = launch_server(headless=True, binary_path=binary, seed=42, humanize=False)
    print("ENDPOINT " + server.ws_endpoint, flush=True)
    try:
        sys.stdin.read()
    finally:
        server.close()


async def one_browser(arm, binary, port, tag):
    row = {"arm": arm, "tag": tag}
    t0 = time.perf_counter()
    if arm == "inproc":
        from invisible_playwright.async_api import InvisiblePlaywright
        cm = InvisiblePlaywright(seed=42, binary_path=binary, humanize=False, headless=True)
        browser = await cm.__aenter__()
        close = lambda: cm.__aexit__(None, None, None)
    elif arm == "remote":
        from playwright.async_api import async_playwright
        proc, endpoint = await serve_endpoint(binary)
        pw = await async_playwright().start()
        browser = await pw.firefox.connect(endpoint)

        async def close(browser=browser, pw=pw, proc=proc):
            await browser.close()
            await pw.stop()
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), 60)
    else:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        browser = await pw.firefox.launch(headless=True)

        async def close(browser=browser, pw=pw):
            await browser.close()
            await pw.stop()
    page = await browser.new_page()
    row["launch_ms"] = round((time.perf_counter() - t0) * 1000)
    row["goto_req_ms"], row["eval_ms"] = await work(page, port, tag)
    t1 = time.perf_counter()
    await close()
    row["close_ms"] = round((time.perf_counter() - t1) * 1000)
    return row


class CpuSampler:
    """The last CPU seconds seen of every engine process under this one, sampled every 0.2 s for the whole round: a process that
    ends between two samples loses at most 0.2 s of its CPU."""

    def __init__(self):
        self.last, self.stop = {}, threading.Event()
        self.t = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.is_set():
            self.last.update(engine_cpu())
            time.sleep(0.2)

    def total(self):
        self.stop.set()
        self.t.join(2)
        return sum(self.last.values())


async def main(arm, n, rnd, binary):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    sampler = CpuSampler()
    sampler.t.start()
    t0 = time.time()
    rows = await asyncio.gather(*(one_browser(arm, binary, port, "%s-%s-%d" % (arm, rnd, i)) for i in range(n)), return_exceptions=True)
    cpu = sampler.total()
    for r in rows:
        if isinstance(r, Exception):
            print("HUNTROW " + json.dumps({"arm": arm, "round": rnd, "crash": repr(r)[:800]}), flush=True)
            continue
        r.update({"round": rnd, "n": n, "os": sys.platform, "wall_s": round(time.time() - t0, 1), "cpu_s_per_browser": round(cpu / n, 2)})
        print("HUNTROW " + json.dumps(r), flush=True)
    httpd.shutdown()


if __name__ == "__main__":
    if sys.argv[1] == "serve":
        serve_one(sys.argv[2])
        sys.exit(0)
    asyncio.run(main(sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None))
