"""The e2e tests of the first audit fix judge the browser's own time and what happened, not the speed of the runner.

Each test below (no browser, no sleep that decides anything) runs the judgement of an e2e on the case it must still call
red and the case it must call green:

* the first STUN datagram of a PeerConnection is judged by WHICH association carried it, and the gateway says which;
* a handshake the test holds (`Gateway.gate`) answers nothing until the test lets it go;
* the page of the key-delay test and of the typing-pause test stamp each event with `event.timeStamp` (the browser's own
  creation time), not with `performance.now()` read when the handler happens to run;
* the pages of the `networkidle` cases of `test_a_page_that_moves_on_e2e.py` have no timer: they move from inside `load`
  or from the first bytes of a response the server holds open;
* the rate of a screencast is the rate of the frames that came, and a cap that is not honoured is above its ceiling;
* the screenshot of a frame of another site is taken again until it is painted, and says what it saw if it never is.
"""
from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

from _e2e_verdicts import cold_association_problem
from _screencast_evidence import Stream
from _socks_servers import STUN_NUMBER, STUN_PORT, coherent
from _waits import wait_until

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# the first datagram goes out on an association that was ready before the birth
# ---------------------------------------------------------------------------

BORN = 10.0
#: [start, end] of each association granted: #0 is the core's probe, #1 and #2 are the pool of the engine, #3 is made later
ASSOCIATIONS = [[0.1, 0.3], [9.0, 9.4], [9.1, 9.5], [10.05, 10.5]]


def test_a_first_datagram_on_a_ready_association_is_green_at_any_speed():
    for at in (10.01, 10.4, 25.0, 300.0):      # the same identity whether the machine is quick or three minutes late
        assert cold_association_problem(ASSOCIATIONS, [(STUN_NUMBER, STUN_PORT, 1, at)], STUN_NUMBER, BORN) is None
        assert cold_association_problem(ASSOCIATIONS, [(STUN_NUMBER, STUN_PORT, 2, at)], STUN_NUMBER, BORN) is None


def test_a_first_datagram_on_an_association_made_after_the_birth_is_red_even_when_it_is_fast():
    problem = cold_association_problem(ASSOCIATIONS, [(STUN_NUMBER, STUN_PORT, 3, 10.06)], STUN_NUMBER, BORN)
    assert problem and "association #3" in problem and "a cold association" in problem


def test_an_engine_that_asked_for_nothing_or_whose_pool_was_late_or_that_sent_nothing_is_red():
    assert "never asked" in cold_association_problem([[0.1, 0.3]], [], STUN_NUMBER, BORN)
    assert "no association was completed before" in cold_association_problem(
        [[0.1, 0.3], [10.2, 10.6]], [(STUN_NUMBER, STUN_PORT, 1, 10.7)], STUN_NUMBER, BORN)
    assert "no STUN datagram" in cold_association_problem(ASSOCIATIONS, [("203.0.113.9", 9, 1, 10.1)], STUN_NUMBER, BORN)


def test_the_first_datagram_is_the_earliest_of_the_log_whatever_the_order_it_was_written_in():
    log = [(STUN_NUMBER, STUN_PORT, 1, 10.9), (STUN_NUMBER, STUN_PORT, 3, 10.06)]
    assert "association #3" in cold_association_problem(ASSOCIATIONS, log, STUN_NUMBER, BORN)


def _hello(port):
    t = socket.create_connection(("127.0.0.1", port), timeout=10)
    t.sendall(b"\x05\x01\x00")
    return t


def test_the_gateway_says_which_association_carried_each_datagram():
    with coherent() as gateway:
        clients = []
        try:
            for _ in range(2):
                t = _hello(gateway.port)
                assert t.recv(2) == b"\x05\x00"
                t.sendall(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")
                reply = t.recv(10)
                assert reply[1] == 0
                clients.append((t, (socket.inet_ntoa(reply[4:8]), struct.unpack("!H", reply[8:10])[0])))
            for _t, relay in clients:
                udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                udp.sendto(b"\x00\x00\x00\x01" + socket.inet_aton(STUN_NUMBER) + struct.pack("!H", STUN_PORT) + b"x", relay)
                udp.close()
            assert wait_until(lambda: len(gateway.datagram_via) == 2, 10, lambda: time.sleep(0.01))
        finally:
            for t, _relay in clients:
                t.close()
        assert sorted((index, host) for host, _port, index, _at in gateway.datagram_via) == [(0, STUN_NUMBER), (1, STUN_NUMBER)]


# ---------------------------------------------------------------------------
# a handshake the test holds
# ---------------------------------------------------------------------------

def test_a_gateway_with_its_gate_closed_answers_nothing_until_the_gate_opens():
    with coherent() as gateway:
        gateway.gate = threading.Event()
        t = _hello(gateway.port)
        t.settimeout(0.4)
        with pytest.raises(socket.timeout):      # a window of observation: it can only hide a failure, never make one
            t.recv(2)
        gateway.gate.set()
        t.settimeout(10)
        assert t.recv(2) == b"\x05\x00"
        t.close()


def test_a_gateway_with_no_gate_holds_nothing():
    with coherent() as gateway:
        assert gateway.gate is None
        t = _hello(gateway.port)
        t.settimeout(10)
        assert t.recv(2) == b"\x05\x00"
        t.close()


# ---------------------------------------------------------------------------
# the pages stamp events with the browser's time
# ---------------------------------------------------------------------------

def test_the_keys_of_the_delay_test_are_stamped_when_the_browser_made_them():
    import test_key_delay_reaches_the_keyboard as module
    assert b"e.timeStamp" in module.PAGE and b"performance.now" not in module.PAGE


def test_the_focus_and_the_first_key_of_the_typing_pause_test_are_stamped_when_the_browser_made_them():
    import test_a_field_is_typed_after_a_pause as module
    page = module.STORE_PAGE.decode()
    script = page[page.index("window.__events"):page.index("const answer")]
    assert "e.timeStamp" in script and "performance.now" not in script


# ---------------------------------------------------------------------------
# a page that moves with no timer
# ---------------------------------------------------------------------------

def test_the_networkidle_pages_of_a_page_that_moves_have_no_timer_in_them():
    import test_a_page_that_moves_on_e2e as module
    for name in ["/ir0", "/ir50", "/ix0", "/ix50"]:
        page = module.PAGES[name].decode()
        assert "setTimeout" not in page and "setInterval" not in page, name
        assert "location.replace" in page, name
    # 0: inside the load handler itself; 50: when the first bytes of a response the server holds open come in
    assert "/hold" not in module.PAGES["/ir0"].decode() and "/hold" in module.PAGES["/ir50"].decode()
    assert "127.0.0.1','localhost'" in module.PAGES["/ix0"].decode()
    # the pages of the read-at-once test keep their timers: they are the stimulus there, and each read accepts either page
    assert "setTimeout" in module.PAGES["/r50"].decode()


# ---------------------------------------------------------------------------
# the rate of the frames that came
# ---------------------------------------------------------------------------

def test_the_rate_of_a_screencast_is_that_of_its_frames_from_the_first_to_the_last():
    stream = Stream("x")
    assert stream.rate() is None
    stream.arrivals = [5.0]
    assert stream.rate() is None
    stream.arrivals = [100.0 + 0.5 * i for i in range(6)]          # a cap of 2 fps honoured: 5 gaps of 0.5 s
    assert stream.rate() == pytest.approx(2.0)
    stream.arrivals = [100.0 + 0.04 * i for i in range(6)]         # a cap that is not honoured: 25 fps
    assert stream.rate() == pytest.approx(25.0) and stream.rate() > 2 * 2
    # a machine that took ten seconds to deliver the first frame does not change the rate of the ones that came
    stream.arrivals = [110.0 + 0.5 * i for i in range(6)]
    assert stream.rate() == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# the screenshot taken again until the frame is painted
# ---------------------------------------------------------------------------

class _Page:
    def wait_for_timeout(self, ms):
        time.sleep(ms / 1000.0)


def _looks(colours):
    """A fake decode: the colour at every point, from the list of looks the page will give (the last one repeats)."""
    looks = iter(colours)
    last = [next(looks)]

    def decode(_data):
        last[0] = next(looks, last[0])
        shade = last[0]
        return (1, 1, lambda x, y: shade)

    return decode


def test_a_frame_that_is_painted_late_is_waited_for_and_one_never_painted_is_red_with_what_was_seen(monkeypatch):
    import test_fission_frames_tree_input_and_pixels_e2e as module
    pink = module.PINK
    monkeypatch.setattr(module, "decode_png", _looks([(255, 255, 255)] * 3 + [pink]))
    taken = []
    found = module.painted_shot(_Page(), lambda: taken.append(1) or b"png", [((5, 5), pink)], seconds=5.0)
    assert found[2](0, 0) == pink and len(taken) == 3   # two looks too early, the third painted
    monkeypatch.setattr(module, "decode_png", _looks([(255, 255, 255)]))
    with pytest.raises(AssertionError) as red:
        module.painted_shot(_Page(), lambda: b"png", [((5, 5), pink)], seconds=0.3)
    assert "never painted" in str(red.value) and "(255, 255, 255)" in str(red.value)
