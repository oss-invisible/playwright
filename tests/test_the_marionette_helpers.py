"""The helpers the e2e tests reach Marionette with, without a browser.

A browser's Marionette port is chosen by the browser, never fixed and never freed for it to take: three e2e files shared 28431 and run on
different xdist workers at once, and a port that was free when it was asked for can be taken by a neighbour before the browser binds it. The
browser is launched with port 0 and the port it got is read from `<profile>/MarionetteActivePort`. And a connection that fails says what
the engine says about its Marionette (e2e Windows part 2, run 43704738537: a refused
connection for 120 s and nothing else to read)."""
import pathlib
import re
import socket

import webfont_probe

TESTS = pathlib.Path(__file__).resolve().parent


def test_the_port_a_browser_got_is_read_from_its_profile(tmp_path):
    (tmp_path / "MarionetteActivePort").write_text("43117" + chr(10), encoding="utf-8")
    assert webfont_probe.marionette_active_port(tmp_path, timeout=5) == 43117


def test_a_profile_without_the_active_port_says_what_the_engine_says(tmp_path):
    try:
        webfont_probe.marionette_active_port(tmp_path, timeout=0)
    except OSError as failure:
        text = str(failure)
    else:
        raise AssertionError("a profile with no MarionetteActivePort gave a port")
    assert "never wrote MarionetteActivePort" in text, text
    assert "no engine process below this one" in text, text


def test_no_test_gives_a_browser_a_fixed_marionette_port():
    fixed = []
    for path in TESTS.glob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"""["']marionette\.port["']\s*:\s*[1-9]""", line) or re.search(r"^MARIONETTE_PORT\s*=\s*\d", line):
                fixed.append("%s:%d" % (path.name, n))
    assert not fixed, "a fixed Marionette port: %s" % fixed


def test_a_failed_connection_says_what_the_engine_says_about_its_marionette():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    try:
        webfont_probe._Marionette(port, timeout=0)
    except OSError as failure:
        text = str(failure)
    else:
        raise AssertionError("a connection to a port nobody listens on succeeded")
    assert "Marionette did not answer on port %d" % port in text, text
    assert "no engine process below this one" in text, text
    assert "listening on %d: nobody" % port in text, text
