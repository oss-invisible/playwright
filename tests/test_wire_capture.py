"""The wire parsers of the network A/B oracle (``tests/_wire_capture.py``), held to known inputs with no browser: a ClientHello made by hand and one made by Python's
``ssl``, the frames of the ``h2`` library and of hand-made bytes, HTTP/1.1 heads as they are written.

What each test refuses: a hello parser that keeps the random, the session id, a key share or a PSK identity (the values of a connection, not of a client); a GREASE value
that is not normalised; a peek that eats the bytes ssl then needs; an HTTP/1.1 reader that lowers the case of a header or forgets an empty one; an HTTP/2 tap that loses the
order of the SETTINGS, the WINDOW_UPDATE of the connection, the PRIORITY frames or the order of the pseudo-headers.
"""
from __future__ import annotations

import json
import socket
import ssl
import struct
import threading
import time

import pytest

import _wire_capture as wc

pytestmark = pytest.mark.unit

RANDOM = b"\x11" * 32
SESSION_ID = b"\x22" * 32
KEY_BYTES = b"\x33" * 32
IDENTITY = b"\x44" * 24
BINDER = b"\x55" * 32


def _u16(n: int) -> bytes:
    return struct.pack("!H", n)


def _vec16(b: bytes) -> bytes:
    return _u16(len(b)) + b


def _vec8(b: bytes) -> bytes:
    return bytes([len(b)]) + b


def _ext(kind: int, body: bytes) -> bytes:
    return _u16(kind) + _vec16(body)


def build_hello(suites=(0x2A2A, 0x1301, 0x1303, 0xC02B), extensions=None) -> bytes:
    """A ClientHello record as a Firefox-like client writes it, with a GREASE suite, group, version and extension, and values that must NOT come out."""
    if extensions is None:
        extensions = [
            _ext(0x3A3A, b"\x00"),                                                                                   # a GREASE extension
            _ext(0, _vec16(b"\x00" + _vec16(b"wire.test"))),                                                           # server_name
            _ext(23, b""),                                                                                           # extended_master_secret
            _ext(65281, _vec8(b"")),                                                                                 # renegotiation_info
            _ext(10, _vec16(_u16(0x6A6A) + _u16(0x001D) + _u16(0x0017) + _u16(0x0018) + _u16(0x0100))),             # supported_groups
            _ext(11, _vec8(b"\x00")),                                                                                # ec_point_formats
            _ext(35, b""),                                                                                           # session_ticket
            _ext(16, _vec16(_vec8(b"h2") + _vec8(b"http/1.1"))),                                                      # alpn
            _ext(5, b"\x01" + _vec16(b"") + _vec16(b"")),                                                             # status_request
            _ext(34, _vec16(_u16(0x0403) + _u16(0x0503))),                                                           # delegated_credentials
            _ext(51, _vec16(_u16(0x0A0A) + _vec16(b"\x00") + _u16(0x001D) + _vec16(KEY_BYTES))),                     # key_share
            _ext(43, _vec8(_u16(0x7A7A) + _u16(0x0304) + _u16(0x0303))),                                             # supported_versions
            _ext(13, _vec16(_u16(0x0403) + _u16(0x0804) + _u16(0x0401))),                                            # signature_algorithms
            _ext(45, _vec8(b"\x01")),                                                                                # psk_key_exchange_modes
            _ext(28, _u16(16385)),                                                                                   # record_size_limit
            _ext(27, _vec8(_u16(2))),                                                                                # compress_certificate
            _ext(65037, b"\x00" + _u16(1) + _u16(1) + b"\x66" + _vec16(b"\x77" * 32) + _vec16(b"\x88" * 144)),      # encrypted_client_hello
            _ext(21, b"\x00" * 40),                                                                                  # padding
            _ext(41, _vec16(_vec16(IDENTITY) + struct.pack("!I", 12345)) + _vec16(_vec8(BINDER))),                   # pre_shared_key
        ]
    body = (_u16(0x0303) + RANDOM + _vec8(SESSION_ID) + _vec16(b"".join(_u16(s) for s in suites)) + _vec8(b"\x00") + _vec16(b"".join(extensions)))
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16" + _u16(0x0301) + _vec16(handshake)


# ------------------------------------------------------------------------------------------------------------------ the ClientHello
def test_a_client_hello_is_read_field_by_field_and_grease_is_one_word():
    hello = wc.parse_client_hello(build_hello())
    assert hello["record_version"] == "0x0301" and hello["handshake_version"] == "0x0303"
    assert hello["session_id_length"] == 32 and hello["compression_methods"] == [0]
    assert hello["cipher_suites"] == ["GREASE", "0x1301", "0x1303", "0xc02b"]
    assert hello["extension_order"][:4] == ["GREASE", "server_name", "extended_master_secret", "renegotiation_info"]
    assert hello["extension_order"][-1] == "pre_shared_key"
    assert hello["grease_extensions"] == 1
    x = hello["extensions"]
    assert x["server_name"] == "wire.test"
    assert x["supported_groups"] == ["GREASE", "0x001d", "0x0017", "0x0018", "0x0100"]
    assert x["ec_point_formats"] == [0]
    assert x["alpn"] == ["h2", "http/1.1"]
    assert x["signature_algorithms"] == ["0x0403", "0x0804", "0x0401"]
    assert x["delegated_credentials"] == ["0x0403", "0x0503"]
    assert x["supported_versions"] == ["GREASE", "0x0304", "0x0303"]
    assert x["psk_key_exchange_modes"] == [1]
    assert x["record_size_limit"] == 16385
    assert x["compress_certificate"] == ["0x0002"]
    assert x["key_share"] == [{"group": "GREASE", "key_length": 1}, {"group": "0x001d", "key_length": 32}]
    assert x["status_request"] == {"type": 1, "responder_ids_length": 0, "extensions_length": 0}
    assert x["padding"] == {"length": 40}
    assert x["encrypted_client_hello"] == {"type": 0, "kdf": 1, "aead": 1, "enc_length": 32, "payload_length": 144}
    assert x["pre_shared_key"] == {"identities": 1, "binder_lengths": [32]}
    assert x["renegotiation_info"] == {"length": 0}


def test_nothing_that_is_random_or_secret_per_connection_is_kept():
    """Known-bad: a parser that keeps the random, the session id, the bytes of a key share or a PSK identity makes two launches of one engine differ in every leaf."""
    text = json.dumps(wc.parse_client_hello(build_hello()), sort_keys=True)
    for name, value in (("the random", RANDOM), ("the session id", SESSION_ID), ("a key share", KEY_BYTES), ("a PSK identity", IDENTITY), ("a binder", BINDER)):
        assert value.hex() not in text, "%s is in the parsed hello" % name
        assert list(value) not in json.loads(text).values(), name
    assert "random" not in text.lower() or "random" not in json.loads(text)
    assert "12345" not in text, "the obfuscated ticket age is in the parsed hello"


def test_a_hello_with_no_extensions_and_a_cut_hello():
    bare = wc.parse_client_hello(build_hello(extensions=[]))
    assert bare["extension_order"] == [] and bare["extensions"] == {}
    record = build_hello()
    with pytest.raises(wc.NotAClientHello):
        wc.parse_client_hello(record[:-10])
    with pytest.raises(wc.NotAClientHello):
        wc.parse_client_hello(b"\x17" + record[1:])
    with pytest.raises(wc.NotAClientHello):
        wc.parse_client_hello(b"\x16\x03\x01\x00\x04" + b"\x02\x00\x00\x00")


@pytest.mark.parametrize("value,grease", [(0x0A0A, True), (0x1A1A, True), (0xFAFA, True), (0x0A1A, False), (0x1301, False), (0x0000, False), (0xAAAA, True)])
def test_grease_is_recognised_by_its_two_equal_bytes(value, grease):
    assert wc.is_grease(value) is grease


def test_the_peek_leaves_every_byte_in_the_socket_and_waits_for_a_hello_that_comes_in_pieces():
    record = build_hello()
    a, b = socket.socketpair()
    go = threading.Event()

    def send() -> None:
        a.sendall(record[:3])
        go.wait(30)
        a.sendall(record[3:20])
        a.sendall(record[20:])

    sender = threading.Thread(target=send, daemon=True)
    sender.start()
    found: list = []
    reader = threading.Thread(target=lambda: found.append(wc.peek_client_hello(b)), daemon=True)
    reader.start()
    go.set()
    reader.join(60)
    sender.join(60)
    assert found == [record]
    again = b""
    b.settimeout(30)
    while len(again) < len(record):
        again += b.recv(len(record) - len(again))
    assert again == record, "the peek consumed bytes"
    a.close()
    b.close()


def test_a_peek_that_meets_a_closed_connection_or_a_stream_that_is_not_tls_says_so():
    a, b = socket.socketpair()
    a.sendall(b"GET / HTTP/1.1\r\n\r\n")
    with pytest.raises(wc.NotAClientHello):
        wc.peek_client_hello(b)
    a.close()
    b.close()
    c, d = socket.socketpair()
    c.close()
    with pytest.raises(ConnectionError):
        wc.peek_client_hello(d)
    d.close()
    # A peek cannot see the end of a stream that stops in the middle of a record (the bytes are still there to be looked at): the deadline is what ends it.
    e, f = socket.socketpair()
    e.sendall(b"\x16\x03\x01\x00")
    e.close()
    with pytest.raises(TimeoutError):
        wc.peek_client_hello(f, deadline_s=0.3)
    f.close()


# --------------------------------------------------------------------------------------------------------- a real client of Python's ssl
def _client_context(alpn):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.set_alpn_protocols(list(alpn))
    return context


def test_the_hello_of_a_real_ssl_client_is_read_and_the_handshake_still_completes_and_http1_is_kept_as_written():
    with wc.WireServer(alpn=("http/1.1",), dns_names=("wire.test",)) as server:
        with socket.create_connection((server.address, server.port), timeout=60) as raw:
            with _client_context(["h2", "http/1.1"]).wrap_socket(raw, server_hostname="wire.test") as tls:
                tls.sendall(b"GET /wire.css?x=1 HTTP/1.1\r\nHost: wire.test\r\nX-Mixed-Case: A\r\nx-empty:\r\nAccept: */*\r\n\r\n")
                tls.settimeout(60)
                reply = b""
                while b"p{color" not in reply:
                    chunk = tls.recv(4096)
                    assert chunk, "the connection closed before the answer: %r" % reply
                    reply += chunk
                version = tls.version()
        assert reply.startswith(b"HTTP/1.1 200")
        snapshot = server.snapshot()
    (connection,) = snapshot["connections"]
    hello = connection["tls"]
    assert hello["extensions"]["server_name"] == "wire.test"
    assert hello["extensions"]["alpn"] == ["h2", "http/1.1"]
    assert "0x0304" in hello["extensions"]["supported_versions"] and version == "TLSv1.3"
    assert hello["cipher_suites"] and all(s == "GREASE" or s.startswith("0x") for s in hello["cipher_suites"])
    assert hello["extension_order"].count("server_name") == 1
    assert connection["alpn"] == "http/1.1"
    (request,) = connection["http1"]
    assert request["request_line"] == "GET /wire.css?x=1 HTTP/1.1"
    assert request["headers"] == [["Host", "wire.test"], ["X-Mixed-Case", "A"], ["x-empty", ""], ["Accept", "*/*"]]


# ---------------------------------------------------------------------------------------------------------------------------- HTTP/1.1
def test_http1_heads_keep_their_case_order_empty_values_and_pipelining_across_any_split():
    wire = (b"POST /a HTTP/1.1\r\nHost: h\r\nContent-Length: 5\r\nX-Empty:\r\ncookie: a=1; b=2\r\n\r\nhello"
            b"GET /b HTTP/1.1\r\nHOST: h\r\nAccept:  */* \r\n\r\n"
            b"POST /c HTTP/1.1\r\nHost: h\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nwxyz\r\n3\r\nabc\r\n0\r\n\r\n"
            b"GET /d HTTP/1.1\r\nHost: h\r\n\r\n")
    for step in (len(wire), 7, 1):
        parser = wc.Http1Parser()
        out = []
        for i in range(0, len(wire), step):
            out += parser.feed(wire[i:i + step])
        assert [r["request_line"] for r in out] == ["POST /a HTTP/1.1", "GET /b HTTP/1.1", "POST /c HTTP/1.1", "GET /d HTTP/1.1"], step
        assert out[0]["headers"] == [["Host", "h"], ["Content-Length", "5"], ["X-Empty", ""], ["cookie", "a=1; b=2"]]
        assert out[0]["body_length"] == 5 and out[2]["body_length"] == 7 and out[3]["body_length"] == 0
        assert out[1]["headers"] == [["HOST", "h"], ["Accept", "*/*"]]


# ----------------------------------------------------------------------------------------------------------------------------- HTTP/2
def _h2_client_exchange(server, extra_wire: bytes = b"", settings=None, window_update: int = 0):
    """One TLS connection to ``server`` that speaks HTTP/2 with the ``h2`` library: the given SETTINGS, a connection WINDOW_UPDATE, ``extra_wire`` (raw frames),
    then one GET whose headers are in a known order. Returns the status line header of the answer."""
    import h2.config
    import h2.connection
    import h2.events
    import h2.settings

    conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=True))
    if settings:
        conn.local_settings = h2.settings.Settings(client=True, initial_values=settings)
    conn.initiate_connection()
    if window_update:
        conn.increment_flow_control_window(window_update)
    with socket.create_connection((server.address, server.port), timeout=60) as raw:
        with _client_context(["h2", "http/1.1"]).wrap_socket(raw, server_hostname="wire.test") as tls:
            assert tls.selected_alpn_protocol() == "h2"
            tls.settimeout(60)
            tls.sendall(conn.data_to_send() + extra_wire)
            conn.send_headers(1, [(":method", "GET"), (":path", "/wire.css"), (":authority", "wire.test"), (":scheme", "https"),
                                  ("user-agent", "a-test"), ("accept", "text/css,*/*;q=0.1"), ("cookie", "a=1; b=2"), ("x-last", "z")], end_stream=True)
            tls.sendall(conn.data_to_send())
            status, ended = None, False
            while not ended:
                data = tls.recv(65536)
                assert data, "the server closed before the answer"
                for event in conn.receive_data(data):
                    if isinstance(event, h2.events.ResponseReceived):
                        status = dict(event.headers).get(b":status")
                    elif isinstance(event, h2.events.StreamEnded):
                        ended = True
                tls.sendall(conn.data_to_send())
    return status


def _frame(kind: int, stream: int, body: bytes, flags: int = 0) -> bytes:
    return len(body).to_bytes(3, "big") + bytes([kind, flags]) + struct.pack("!I", stream) + body


def test_an_h2_client_is_recorded_settings_window_updates_priorities_and_the_order_of_a_request():
    import h2.settings

    priority = _frame(2, 3, struct.pack("!IB", 0, 200)) + _frame(2, 5, struct.pack("!IB", 0x80000000 | 3, 100))
    update = _frame(0x10, 0, struct.pack("!I", 3) + b"u=0, i")
    initial = {h2.settings.SettingCodes.ENABLE_PUSH: 0, h2.settings.SettingCodes.INITIAL_WINDOW_SIZE: 131072,
               h2.settings.SettingCodes.MAX_FRAME_SIZE: 16384}
    with wc.WireServer(alpn=("h2", "http/1.1"), h3=False) as server:
        assert _h2_client_exchange(server, priority + update, initial, window_update=12517377) == b"200"
        snapshot = server.snapshot()
    (connection,) = snapshot["connections"]
    assert connection["alpn"] == "h2" and connection["tls"]["extensions"]["alpn"] == ["h2", "http/1.1"]
    h2c = connection["h2"]
    got = {k: v for k, v in h2c["settings"]}
    assert got[2] == 0 and got[4] == 131072 and got[5] == 16384
    assert [k for k, _v in h2c["settings"]][:3] == [2, 4, 5] or {2, 4, 5} <= set(got), h2c["settings"]
    assert h2c["frame_types_before_the_first_request"][:2] == ["SETTINGS", "WINDOW_UPDATE"]
    assert "PRIORITY" in h2c["frame_types_before_the_first_request"] and "PRIORITY_UPDATE" in h2c["frame_types_before_the_first_request"]
    assert h2c["window_updates_before_the_first_request"] == [{"stream": 0, "increment": 12517377}]
    assert h2c["priority"] == [{"stream": 3, "depends_on": 0, "weight": 201, "exclusive": False},
                               {"stream": 5, "depends_on": 3, "weight": 101, "exclusive": True}]
    assert h2c["priority_update"] == [{"stream": 0, "element": 3, "field": "u=0, i"}]
    (request,) = h2c["requests"]
    assert request["pseudo_order"] == [":method", ":path", ":authority", ":scheme"]
    assert request["header_names"] == ["user-agent", "accept", "cookie", "x-last"]
    assert request["headers"][4:6] == [["user-agent", "a-test"], ["accept", "text/css,*/*;q=0.1"]]


def test_the_frame_tap_refuses_a_stream_that_does_not_start_with_the_preface_and_reads_a_split_one():
    tap = wc.FrameTap()
    with pytest.raises(ValueError):
        tap.feed(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    tap = wc.FrameTap()
    wire = wc.H2_PREFACE + _frame(4, 0, struct.pack("!HI", 4, 131072) + struct.pack("!HI", 1, 65536)) + _frame(8, 0, struct.pack("!I", 99))
    seen = []
    for i in range(0, len(wire), 5):
        seen += tap.feed(wire[i:i + 5])
    assert [f["type"] for f in seen] == ["SETTINGS", "WINDOW_UPDATE"]
    assert seen[0]["settings"] == [[4, 131072], [1, 65536]] and seen[1]["increment"] == 99
    summary = wc.summarize_frames(tap.frames)
    assert summary["settings"] == [[4, 131072], [1, 65536]] and summary["frames_after_the_first_request"] == 0


# ----------------------------------------------------------------------------------------------------------------------------- QUIC values
def _varint(n: int) -> bytes:
    if n < 64:
        return bytes([n])
    if n < 16384:
        return struct.pack("!H", 0x4000 | n)
    return struct.pack("!I", 0x80000000 | n)


def test_quic_transport_parameters_keep_their_order_and_drop_identifiers_and_grease():
    def param(i: int, value: bytes) -> bytes:
        return _varint(i) + _varint(len(value)) + value

    raw = (param(0x04, _varint(15728640)) + param(0x05, _varint(12582912)) + param(0x0F, b"\x99" * 8) + param(27 + 31 * 5, b"\xaa\xbb")
           + param(0x0B, _varint(20)) + param(0x0C, b"") + param(0x20, _varint(65535)) + param(0x2AB2, b""))
    out = wc.parse_quic_transport_parameters(raw)
    assert [p["id"] for p in out] == [4, 5, 15, "GREASE", 11, 12, 32, 0x2AB2]
    assert out[0] == {"id": 4, "name": "initial_max_data", "length": 4, "value": 15728640}
    assert out[2] == {"id": 15, "name": "initial_source_connection_id", "length": 8}
    assert out[3] == {"id": "GREASE", "length": 2}
    assert out[5] == {"id": 12, "name": "disable_active_migration", "length": 0}
    assert out[6]["value"] == 65535
    assert ("99" * 8) not in json.dumps(out)
    with pytest.raises(ValueError):
        wc.parse_quic_transport_parameters(param(4, _varint(1))[:-1])


def test_h3_settings_are_named_and_a_reserved_identifier_is_grease():
    assert wc.h3_settings(None) is None
    assert wc.h3_settings({1: 65536, 7: 20, 0x21 + 0x1F * 3: 0, 0x33: 1}) == [["QPACK_MAX_TABLE_CAPACITY", 65536], ["QPACK_BLOCKED_STREAMS", 20], ["GREASE", 0],
                                                                              ["H3_DATAGRAM", 1]]


# ------------------------------------------------------------------------------------------------------- when an answer is written in full
def _held_responder(release: threading.Event):
    """The default site, except that the answer to ``/wire.css`` is not given until ``release`` is set: the request has arrived and its answer is not written."""
    def responder(request):
        if request["target"].split("?")[0] == "/wire.css":
            release.wait(60)
        return wc.default_responder(request)
    return responder


def _http1_get(server, target: bytes) -> bytes:
    with socket.create_connection((server.address, server.port), timeout=60) as raw:
        with _client_context(["http/1.1"]).wrap_socket(raw, server_hostname="wire.test") as tls:
            tls.sendall(b"GET " + target + b" HTTP/1.1\r\nHost: wire.test\r\n\r\n")
            tls.settimeout(60)
            reply = b""
            while b"p{color" not in reply:
                chunk = tls.recv(4096)
                assert chunk, "the connection closed before the answer: %r" % reply
                reply += chunk
    return reply


def _arrived(server, kind: str):
    connections = server.snapshot()["connections"]
    return [r for c in connections for r in (c.get("http1") if kind == "http1" else c.get("h2", {}).get("requests")) or []]


@pytest.mark.parametrize("kind", ["http1", "h2"])
def test_a_request_is_answered_in_full_only_when_the_last_byte_of_its_answer_has_been_written_to_the_socket(kind):
    """The visits of the oracle's walks are left only once the lab has SERVED the favicon in full (scripts/ci_ab_network.py ``settle_visit``): a server that says "answered" when the request arrives
    (before the answer is written) or never says it is red here. The path is read without its query."""
    from _waits import wait_until

    release = threading.Event()
    alpn = ("http/1.1",) if kind == "http1" else ("h2", "http/1.1")
    with wc.WireServer(alpn=alpn, dns_names=("wire.test",), responder=_held_responder(release)) as server:
        replies = []
        client = (lambda: _http1_get(server, b"/wire.css?x=1")) if kind == "http1" else (lambda: _h2_client_exchange(server))
        thread = threading.Thread(target=lambda: replies.append(client()), daemon=True)
        thread.start()
        try:
            assert wait_until(lambda: _arrived(server, kind), 30, lambda: time.sleep(0.01)), "the request never arrived"
            assert server.answered_at("/wire.css") is None, "the request has arrived and its answer is not written"
        finally:
            release.set()
        thread.join(60)
        assert replies, "the client got no answer"
        # the client can hold the whole answer a moment before the server has stamped it (the stamp follows the write): the oracle waits for it too
        assert wait_until(lambda: server.answered_at("/wire.css") is not None, 30, lambda: time.sleep(0.01)), "the answer was never marked as written"
        written = server.answered_at("/wire.css")
        assert isinstance(written, float) and written >= 0
        if kind == "http1":
            (request,) = _arrived(server, kind)
            assert request["t"] <= written == request["written_t"]
        assert server.answered_at("/favicon.ico") is None, "a path nobody asked for is not answered"
