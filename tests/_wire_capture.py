"""What a client puts on the wire, read by a server of the lab: the TLS ClientHello, the HTTP/2 frames, the HTTP/1.1 request heads in wire order, and
what an HTTP/3 client says (QUIC transport parameters, the SETTINGS frame, the header order). No browser in here: every parser takes bytes, and the
unit tests (``test_wire_capture.py``, ``test_wire_capture_quic.py``) feed them a client of Python's ``ssl``, the ``h2`` library and ``aioquic``.

The use: the network A/B oracle (``scripts/ci_ab_network.py``) runs the same journey with two engines and compares what these servers saw, leaf by leaf.

* :func:`parse_client_hello` reads the record of a ClientHello with the standard library only: record and handshake version, the cipher suites in order,
  the compression methods, the extensions in the order they were sent, and what is inside each of the known ones (supported_groups, signature_algorithms,
  ALPN, key_share GROUPS, psk_key_exchange_modes, supported_versions, record_size_limit, ec_point_formats, ...). A GREASE value is written ``"GREASE"``.
  What is random or secret per connection is NOT kept: the random, the session id, the bytes of a key share, the PSK identities and their ages, the
  ECH config id, the encapsulated key. Only their lengths stay (a length is the shape of the hello, not a value of it).
* :func:`peek_client_hello` reads that record off a socket with ``MSG_PEEK`` until the whole record is buffered, so the same socket can then be handed to
  ``ssl`` as if nothing had been read (:func:`accept_with_hello`).
* :class:`FrameTap` reads the HTTP/2 frames of a connection from the raw bytes (``hyperframe``, which ``h2`` is made of): SETTINGS ids and values in order,
  the WINDOW_UPDATEs, PRIORITY and PRIORITY_UPDATE, the priority fields of a HEADERS. :class:`H2Connection` is the server side (``h2``): it answers the
  requests and writes down, per request, the pseudo-header order and every header name and value in the order they arrived.
* :class:`Http1Parser` keeps the request line and the header lines as they were written (the case, the order, the empty values); :func:`serve_http1` is
  the server side, and the parser alone is what a tee in front of another server uses.
* :class:`WireServer` is one address and one port: TCP with TLS (HTTP/2 or HTTP/1.1 by the ALPN it offers) and, when asked, QUIC (HTTP/3 with
  ``aioquic``) on the same number, each connection written down.

Nothing here waits on a clock: a peek that is short of bytes is a poll with a deadline (a safety net, not a verdict), and every other wait is a blocking read.
"""
from __future__ import annotations

import socket
import socketserver
import ssl
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

#: The longest ClientHello record a peek waits for. A hello with a post-quantum key share and an ECH payload is about 2 KB; 16 KB is the limit of a record.
MAX_HELLO_RECORD = 16384 + 5
#: The deadline of a peek, in seconds. A safety net: a client that sent half a hello and stopped, not a measure of anything.
PEEK_DEADLINE_S = 30.0

GREASE = "GREASE"

EXTENSION_NAMES = {
    0: "server_name", 1: "max_fragment_length", 5: "status_request", 10: "supported_groups", 11: "ec_point_formats", 13: "signature_algorithms",
    14: "use_srtp", 15: "heartbeat", 16: "alpn", 18: "signed_certificate_timestamp", 21: "padding", 22: "encrypt_then_mac", 23: "extended_master_secret",
    27: "compress_certificate", 28: "record_size_limit", 34: "delegated_credentials", 35: "session_ticket", 41: "pre_shared_key", 42: "early_data",
    43: "supported_versions", 44: "cookie", 45: "psk_key_exchange_modes", 49: "post_handshake_auth", 50: "signature_algorithms_cert", 51: "key_share",
    17513: "application_settings", 17613: "application_settings_new", 65037: "encrypted_client_hello", 65281: "renegotiation_info",
}

#: The QUIC transport parameters by id (RFC 9000 18.2 and the extensions seen in browsers).
QUIC_PARAMETER_NAMES = {
    0x00: "original_destination_connection_id", 0x01: "max_idle_timeout", 0x02: "stateless_reset_token", 0x03: "max_udp_payload_size",
    0x04: "initial_max_data", 0x05: "initial_max_stream_data_bidi_local", 0x06: "initial_max_stream_data_bidi_remote",
    0x07: "initial_max_stream_data_uni", 0x08: "initial_max_streams_bidi", 0x09: "initial_max_streams_uni", 0x0A: "ack_delay_exponent",
    0x0B: "max_ack_delay", 0x0C: "disable_active_migration", 0x0D: "preferred_address", 0x0E: "active_connection_id_limit",
    0x0F: "initial_source_connection_id", 0x10: "retry_source_connection_id", 0x11: "version_information", 0x20: "max_datagram_frame_size",
    0x2AB2: "grease_quic_bit", 0x2AB1: "reset_stream_at", 0x3127: "initial_rtt", 0x1057: "google_connection_options", 0x4752: "google_version",
}
#: The parameters whose value is an identifier or a secret of the connection: only its length is kept.
QUIC_OPAQUE = {0x00, 0x02, 0x0D, 0x0F, 0x10}

H2_FRAME_TYPES = {0: "DATA", 1: "HEADERS", 2: "PRIORITY", 3: "RST_STREAM", 4: "SETTINGS", 5: "PUSH_PROMISE", 6: "PING", 7: "GOAWAY",
                  8: "WINDOW_UPDATE", 9: "CONTINUATION", 0x10: "PRIORITY_UPDATE"}
H2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"


class NotAClientHello(ValueError):
    """The bytes are not one TLS record that holds a ClientHello."""


def is_grease(value: int) -> bool:
    """A GREASE value of RFC 8701: both bytes are the same and end in 0xa (0x0a0a, 0x1a1a ... 0xfafa)."""
    return (value & 0x0F0F) == 0x0A0A and (value >> 8) == (value & 0xFF)


def _hex(value: int) -> str:
    return GREASE if is_grease(value) else "0x%04x" % value


def _grease_quic(identifier: int) -> bool:
    """A reserved QUIC transport parameter id (31 * N + 27)."""
    return identifier >= 27 and (identifier - 27) % 31 == 0


class _Reader:
    """A cursor over bytes that refuses to read past the end."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.at = 0

    def left(self) -> int:
        return len(self.data) - self.at

    def take(self, n: int) -> bytes:
        if n < 0 or self.at + n > len(self.data):
            raise NotAClientHello("a field of %d bytes runs past the end (%d left)" % (n, self.left()))
        out = self.data[self.at:self.at + n]
        self.at += n
        return out

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return struct.unpack("!H", self.take(2))[0]

    def u24(self) -> int:
        return int.from_bytes(self.take(3), "big")

    def u32(self) -> int:
        return struct.unpack("!I", self.take(4))[0]

    def vec8(self) -> bytes:
        return self.take(self.u8())

    def vec16(self) -> bytes:
        return self.take(self.u16())


def _u16_list(data: bytes) -> List[str]:
    reader = _Reader(data)
    out = []
    while reader.left() >= 2:
        out.append(_hex(reader.u16()))
    return out


def _parse_extension(ext_type: int, data: bytes) -> Any:
    """What is inside one extension, without anything random: a value, a list, or a dict."""
    reader = _Reader(data)
    if ext_type == 0:                                             # server_name
        names = []
        inner = _Reader(reader.vec16())
        while inner.left():
            kind = inner.u8()
            names.append(inner.vec16().decode("ascii", "replace") if kind == 0 else "type%d" % kind)
        return names[0] if len(names) == 1 else names
    if ext_type == 5:                                             # status_request
        kind = reader.u8()
        return {"type": kind, "responder_ids_length": len(reader.vec16()), "extensions_length": len(reader.vec16())}
    if ext_type == 10:                                            # supported_groups
        return _u16_list(reader.vec16())
    if ext_type == 11:                                            # ec_point_formats
        return list(reader.vec8())
    if ext_type in (13, 34, 50):                                  # signature_algorithms, delegated_credentials, signature_algorithms_cert
        return _u16_list(reader.vec16())
    if ext_type == 16:                                            # alpn
        inner = _Reader(reader.vec16())
        out = []
        while inner.left():
            out.append(inner.vec8().decode("ascii", "replace"))
        return out
    if ext_type == 21:                                            # padding
        return {"length": len(data)}
    if ext_type == 27:                                            # compress_certificate
        return _u16_list(reader.vec8())
    if ext_type == 28:                                            # record_size_limit
        return reader.u16()
    if ext_type == 35:                                            # session_ticket
        return {"length": len(data)}
    if ext_type == 41:                                            # pre_shared_key: how many, never what
        identities = _Reader(reader.vec16())
        count = 0
        while identities.left():
            identities.vec16()
            identities.u32()
            count += 1
        binders = _Reader(reader.vec16())
        lengths = []
        while binders.left():
            lengths.append(len(binders.vec8()))
        return {"identities": count, "binder_lengths": lengths}
    if ext_type == 42:                                            # early_data
        return True
    if ext_type == 43:                                            # supported_versions
        return _u16_list(reader.vec8())
    if ext_type == 45:                                            # psk_key_exchange_modes
        return list(reader.vec8())
    if ext_type == 51:                                            # key_share: the groups and the length of each key, never the key
        shares = _Reader(reader.vec16())
        out = []
        while shares.left():
            group = shares.u16()
            out.append({"group": _hex(group), "key_length": len(shares.vec16())})
        return out
    if ext_type in (17513, 17613):                                # application_settings
        inner = _Reader(reader.vec16())
        out = []
        while inner.left():
            out.append(inner.vec8().decode("ascii", "replace"))
        return out
    if ext_type == 65037:                                         # encrypted_client_hello: the suite and the lengths, never the config or the key
        kind = reader.u8()
        if kind != 0:
            return {"type": kind, "length": len(data)}
        kdf, aead = reader.u16(), reader.u16()
        reader.u8()
        return {"type": 0, "kdf": kdf, "aead": aead, "enc_length": len(reader.vec16()), "payload_length": len(reader.vec16())}
    if ext_type == 65281:                                         # renegotiation_info
        return {"length": len(reader.vec8())}
    return {"length": len(data)}


def parse_client_hello(record: bytes) -> Dict[str, Any]:
    """The ClientHello in ``record`` (a whole TLS record, header included), as the dict described at the top of this file. Raises NotAClientHello."""
    try:
        reader = _Reader(record)
        content_type, record_version, length = reader.u8(), reader.u16(), reader.u16()
        if content_type != 22:
            raise NotAClientHello("the record is of type %d, not a handshake" % content_type)
        if length > reader.left():
            raise NotAClientHello("the record says %d bytes and %d came" % (length, reader.left()))
        body = _Reader(reader.take(length))
        if body.u8() != 1:
            raise NotAClientHello("the handshake message is not a ClientHello")
        hello = _Reader(body.take(body.u24()))
        handshake_version = hello.u16()
        hello.take(32)                                              # the random: dropped
        session_id = hello.vec8()                                   # the session id: only its length
        suites_raw = hello.vec16()
        suites = [_hex(struct.unpack("!H", suites_raw[i:i + 2])[0]) for i in range(0, len(suites_raw) - 1, 2)]
        compression = list(hello.vec8())
        out: Dict[str, Any] = {
            "record_version": "0x%04x" % record_version, "record_length": length, "handshake_version": "0x%04x" % handshake_version,
            "session_id_length": len(session_id), "cipher_suites": suites, "compression_methods": compression,
        }
        order: List[str] = []
        data: Dict[str, Any] = {}
        lengths: Dict[str, int] = {}
        grease = 0
        extensions = _Reader(hello.vec16()) if hello.left() else _Reader(b"")
        while extensions.left():
            ext_type, ext_data = extensions.u16(), extensions.vec16()
            if is_grease(ext_type):
                order.append(GREASE)
                grease += 1
                continue
            name = EXTENSION_NAMES.get(ext_type, "ext_%d" % ext_type)
            order.append(name)
            lengths[name] = len(ext_data)
            try:
                data[name] = _parse_extension(ext_type, ext_data)
            except NotAClientHello:
                data[name] = {"unparsed_length": len(ext_data)}
        out["extension_order"] = order
        out["extensions"] = data
        out["extension_lengths"] = lengths
        out["grease_extensions"] = grease
        return out
    except struct.error as exc:
        raise NotAClientHello("a field is cut short: %s" % exc) from None


# ---------------------------------------------------------------------------------------------------------------- peeking at the socket
def _peek(sock: socket.socket, n: int, deadline_s: float) -> bytes:
    """Exactly ``n`` bytes from the front of the stream, left in it. Blocks until they are there (``MSG_WAITALL`` where the system takes it with
    ``MSG_PEEK``; elsewhere a poll with a deadline: a peek that is short of bytes returns at once, and the only way to wait is to look again)."""
    flags = socket.MSG_PEEK | (0 if sys.platform == "win32" else getattr(socket, "MSG_WAITALL", 0))
    end = time.monotonic() + deadline_s
    sock.settimeout(deadline_s)
    while True:
        data = sock.recv(n, flags)
        if len(data) >= n:
            return data
        if not data:
            raise ConnectionError("the peer closed the connection before the ClientHello was complete")
        if time.monotonic() >= end:
            raise TimeoutError("the ClientHello did not arrive in %.0f s (%d of %d bytes)" % (deadline_s, len(data), n))
        time.sleep(0.002)


def peek_client_hello(sock: socket.socket, deadline_s: float = PEEK_DEADLINE_S) -> bytes:
    """The record of the ClientHello at the front of the stream of ``sock``, with every byte of it still in the socket's buffer."""
    head = _peek(sock, 5, deadline_s)
    if head[0] != 22:
        raise NotAClientHello("the first byte of the connection is %d, not a TLS handshake record" % head[0])
    total = 5 + struct.unpack("!H", head[3:5])[0]
    if total > MAX_HELLO_RECORD:
        raise NotAClientHello("a record of %d bytes" % total)
    return _peek(sock, total, deadline_s)[:total]


def accept_with_hello(conn: socket.socket, context: ssl.SSLContext, deadline_s: float = PEEK_DEADLINE_S) -> Tuple[Dict[str, Any], ssl.SSLSocket]:
    """Peek the ClientHello of ``conn``, parse it, then hand the same socket to ``ssl``: ``(hello, tls socket)``. The handshake sees every byte."""
    hello = parse_client_hello(peek_client_hello(conn, deadline_s))
    conn.settimeout(deadline_s)
    return hello, context.wrap_socket(conn, server_side=True)


# ---------------------------------------------------------------------------------------------------------------------------------- HTTP/1.1
class Http1Parser:
    """The requests of one HTTP/1.1 connection, read from the bytes in the order they come. ``feed`` returns the requests that are complete (head and body).

    A request is ``{"request_line": "GET /x HTTP/1.1", "method", "target", "version", "headers": [[name, value], ...], "body_length": n}``: the names with
    the case they were sent with, in the order they were sent, an empty value kept. The body is skipped (``Content-Length`` and ``chunked``) and never kept."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._pending: Optional[Dict[str, Any]] = None
        self._body_left = 0
        self._chunked = False

    def feed(self, data: bytes) -> List[Dict[str, Any]]:
        self._buf += data
        done: List[Dict[str, Any]] = []
        while True:
            if self._pending is None:
                end = self._buf.find(b"\r\n\r\n")
                if end < 0:
                    return done
                head = bytes(self._buf[:end]).decode("latin-1")
                del self._buf[:end + 4]
                lines = head.split("\r\n")
                parts = lines[0].split(" ")
                headers = []
                for line in lines[1:]:
                    name, _sep, value = line.partition(":")
                    headers.append([name, value.strip(" \t")])
                self._pending = {"request_line": lines[0], "method": parts[0], "target": parts[1] if len(parts) > 1 else "",
                                 "version": parts[2] if len(parts) > 2 else "", "headers": headers, "body_length": 0}
                lowered = {n.lower(): v for n, v in headers}
                self._chunked = "chunked" in lowered.get("transfer-encoding", "").lower()
                try:
                    self._body_left = int(lowered.get("content-length", "0") or 0)
                except ValueError:
                    self._body_left = 0
            if self._chunked:
                if not self._skip_chunks():
                    return done
            else:
                take = min(self._body_left, len(self._buf))
                del self._buf[:take]
                self._body_left -= take
                self._pending["body_length"] += take
                if self._body_left:
                    return done
            done.append(self._pending)
            self._pending, self._chunked = None, False

    def _skip_chunks(self) -> bool:
        """Consume chunks while they are whole; True when the terminating chunk and its trailer have been read."""
        while True:
            line_end = self._buf.find(b"\r\n")
            if line_end < 0:
                return False
            try:
                size = int(bytes(self._buf[:line_end]).split(b";")[0], 16)
            except ValueError:
                size = 0
            if size == 0:
                if self._buf.find(b"\r\n\r\n", line_end - 1) < 0 and not self._buf.startswith(b"0\r\n\r\n"):
                    return False
                end = self._buf.find(b"\r\n\r\n")
                del self._buf[:end + 4 if end >= 0 else len(self._buf)]
                return True
            if len(self._buf) < line_end + 2 + size + 2:
                return False
            self._pending["body_length"] += size
            del self._buf[:line_end + 2 + size + 2]


def serve_http1(sock: Any, responder: Callable[[Dict[str, Any]], Tuple[int, List[Tuple[str, str]], bytes]],
                on_request: Callable[[Dict[str, Any]], None], alt_svc: Optional[str] = None, close_after_response: bool = False) -> None:
    """Serve the requests of a connection (a TLS socket, or a plain one) until the peer closes: each request is handed to ``on_request`` and answered by
    ``responder(request) -> (status, headers, body)``. With ``close_after_response`` the answer says ``Connection: close`` and the connection ends after it: the client
    needs a new connection for its next request, and by then it has read the session ticket the server sent with the first answer."""
    parser = Http1Parser()
    while True:
        data = sock.recv(65536)
        if not data:
            return
        for request in parser.feed(data):
            on_request(request)
            status, headers, body = responder(dict(request, scheme="https", protocol="http/1.1"))
            if alt_svc:
                headers = list(headers) + [("alt-svc", alt_svc)]
            lines = ["HTTP/1.1 %d %s" % (status, "OK" if status == 200 else "Status")]
            lines += ["%s: %s" % (n, v) for n, v in headers if n.lower() not in ("content-length", "connection")]
            lines.append("Content-Length: %d" % len(body))
            if close_after_response:
                lines.append("Connection: close")
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
            if close_after_response:
                return


# ---------------------------------------------------------------------------------------------------------------------------------- HTTP/2
class FrameTap:
    """The HTTP/2 frames of one direction of a connection, parsed from the raw bytes as they come (the client's preface first)."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._preface = False
        self.frames: List[Dict[str, Any]] = []

    def feed(self, data: bytes) -> List[Dict[str, Any]]:
        from hyperframe.frame import Frame

        self._buf += data
        if not self._preface:
            if len(self._buf) < len(H2_PREFACE):
                return []
            if bytes(self._buf[:len(H2_PREFACE)]) != H2_PREFACE:
                raise ValueError("the connection does not start with the HTTP/2 preface")
            del self._buf[:len(H2_PREFACE)]
            self._preface = True
        new = []
        while len(self._buf) >= 9:
            frame, length = Frame.parse_frame_header(memoryview(bytes(self._buf[:9])))
            if len(self._buf) < 9 + length:
                break
            frame.parse_body(memoryview(bytes(self._buf[9:9 + length])))
            del self._buf[:9 + length]
            record = _frame_record(frame)
            self.frames.append(record)
            new.append(record)
        return new


def _priority_fields(frame: Any) -> Dict[str, Any]:
    return {"depends_on": frame.depends_on, "weight": frame.stream_weight + 1 if frame.stream_weight is not None else None,
            "exclusive": bool(frame.exclusive)}


def _frame_record(frame: Any) -> Dict[str, Any]:
    kind = type(frame).__name__
    record: Dict[str, Any] = {"stream": frame.stream_id, "flags": sorted(frame.flags)}
    if kind == "SettingsFrame":
        record.update(type="SETTINGS", settings=[[int(k), v] for k, v in frame.settings.items()])
    elif kind == "WindowUpdateFrame":
        record.update(type="WINDOW_UPDATE", increment=frame.window_increment)
    elif kind == "PriorityFrame":
        record.update(type="PRIORITY", **_priority_fields(frame))
    elif kind in ("HeadersFrame", "PushPromiseFrame", "ContinuationFrame"):
        record["type"] = {"HeadersFrame": "HEADERS", "PushPromiseFrame": "PUSH_PROMISE", "ContinuationFrame": "CONTINUATION"}[kind]
        if kind == "HeadersFrame" and "PRIORITY" in frame.flags:
            record["priority"] = _priority_fields(frame)
    elif kind == "DataFrame":
        record.update(type="DATA", length=len(frame.data))
    elif kind == "ExtensionFrame":
        number = int(frame.type)
        record["type"] = H2_FRAME_TYPES.get(number, "TYPE_%d" % number)
        body = bytes(frame.body)
        if number == 0x10 and len(body) >= 4:                       # PRIORITY_UPDATE (RFC 9218): the element and the priority field
            record.update(element=struct.unpack("!I", body[:4])[0] & 0x7FFFFFFF, field=body[4:].decode("ascii", "replace"))
        else:
            record["length"] = len(body)
    else:
        record["type"] = {"PingFrame": "PING", "GoAwayFrame": "GOAWAY", "RstStreamFrame": "RST_STREAM"}.get(kind, kind)
    return record


def summarize_frames(frames: List[Dict[str, Any]]) -> Dict[str, Any]:
    """What of a connection's frames is the shape of the CLIENT, not of the traffic: the first SETTINGS in order, the WINDOW_UPDATEs and the types of the
    frames before the first request, every PRIORITY and PRIORITY_UPDATE, the priority fields of the HEADERS. What follows the first request (DATA, PING,
    later window updates) depends on how much was sent and is only counted."""
    first_headers = next((i for i, f in enumerate(frames) if f["type"] == "HEADERS"), len(frames))
    before = frames[:first_headers]
    settings = next((f["settings"] for f in frames if f["type"] == "SETTINGS" and "ACK" not in f["flags"]), None)
    return {
        "settings": settings,
        "settings_ack_sent_by_client": any(f["type"] == "SETTINGS" and "ACK" in f["flags"] for f in frames),
        "frame_types_before_the_first_request": [f["type"] for f in before],
        "window_updates_before_the_first_request": [{"stream": f["stream"], "increment": f["increment"]} for f in before if f["type"] == "WINDOW_UPDATE"],
        "priority": [{"stream": f["stream"], **{k: f[k] for k in ("depends_on", "weight", "exclusive")}} for f in frames if f["type"] == "PRIORITY"],
        "priority_update": [{"stream": f["stream"], "element": f["element"], "field": f["field"]} for f in frames if f["type"] == "PRIORITY_UPDATE"],
        "headers_priority": [dict(f["priority"], stream=f["stream"]) for f in frames if f["type"] == "HEADERS" and "priority" in f],
        "frames_after_the_first_request": len(frames) - first_headers,
    }


class H2Connection:
    """The server side of one HTTP/2 connection, with a tap on the frames. ``serve(sock)`` blocks until the peer closes. Each request is recorded as
    ``{"stream", "pseudo_order": [":method", ...], "header_names": [...], "headers": [[name, value], ...], "end_stream"}`` and answered by ``responder``."""

    def __init__(self, responder: Callable[[Dict[str, Any]], Tuple[int, List[Tuple[str, str]], bytes]], alt_svc: Optional[str] = None) -> None:
        import h2.config
        import h2.connection

        config = h2.config.H2Configuration(client_side=False, header_encoding=None, validate_inbound_headers=False, normalize_inbound_headers=False,
                                           validate_outbound_headers=False, normalize_outbound_headers=False)
        self.conn = h2.connection.H2Connection(config=config)
        self.responder = responder
        self.alt_svc = alt_svc
        self.tap = FrameTap()
        self.requests: List[Dict[str, Any]] = []
        self._bodies: Dict[int, int] = {}

    def summary(self) -> Dict[str, Any]:
        return dict(summarize_frames(self.tap.frames), requests=list(self.requests))

    def serve(self, sock: Any) -> None:
        import h2.events
        import h2.exceptions

        self.conn.initiate_connection()
        sock.sendall(self.conn.data_to_send())
        while True:
            data = sock.recv(65536)
            if not data:
                return
            try:
                self.tap.feed(data)
                events = self.conn.receive_data(data)
            except (h2.exceptions.ProtocolError, ValueError):
                out = self.conn.data_to_send()
                if out:
                    sock.sendall(out)
                return
            for event in events:
                if isinstance(event, h2.events.RequestReceived):
                    self._request(event)
                    if event.stream_ended is not None:
                        self._answer(event.stream_id)
                elif isinstance(event, h2.events.DataReceived):
                    self._bodies[event.stream_id] = self._bodies.get(event.stream_id, 0) + len(event.data)
                    self.conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                elif isinstance(event, h2.events.StreamEnded):
                    self._answer(event.stream_id)
                elif isinstance(event, h2.events.ConnectionTerminated):
                    return
            out = self.conn.data_to_send()
            if out:
                sock.sendall(out)

    def _request(self, event: Any) -> None:
        headers = [[_text(n), _text(v)] for n, v in event.headers]
        self.requests.append({"stream": event.stream_id, "pseudo_order": [n for n, _v in headers if n.startswith(":")],
                              "header_names": [n for n, _v in headers if not n.startswith(":")], "headers": headers,
                              "end_stream": event.stream_ended is not None})

    def _answer(self, stream_id: int) -> None:
        request = next((r for r in self.requests if r["stream"] == stream_id and not r.get("answered")), None)
        if request is None:
            return
        request["answered"] = True
        fields = {n: v for n, v in request["headers"]}
        status, headers, body = self.responder({"method": fields.get(":method", ""), "target": fields.get(":path", ""), "headers": request["headers"],
                                                "scheme": "https", "protocol": "h2", "body_length": self._bodies.get(stream_id, 0)})
        extra = [("alt-svc", self.alt_svc)] if self.alt_svc else []
        out = [(":status", str(status))] + [(n.lower(), v) for n, v in list(headers) + extra if n.lower() != "content-length"]
        out.append(("content-length", str(len(body))))
        self.conn.send_headers(stream_id, out)
        self.conn.send_data(stream_id, body, end_stream=True)


def _text(value: Any) -> str:
    return value.decode("latin-1") if isinstance(value, (bytes, bytearray)) else str(value)


# ---------------------------------------------------------------------------------------------------------------------------------- QUIC / HTTP/3
def read_varint(data: bytes, at: int) -> Tuple[int, int]:
    """A QUIC variable-length integer at ``data[at:]``: ``(value, the index after it)``."""
    first = data[at]
    size = 1 << (first >> 6)
    if at + size > len(data):
        raise ValueError("a variable-length integer is cut short")
    return int.from_bytes(bytes([first & 0x3F]) + data[at + 1:at + size], "big"), at + size


def parse_quic_transport_parameters(raw: bytes) -> List[Dict[str, Any]]:
    """The transport parameters a QUIC client sent, in the order it sent them. A reserved id (31 * N + 27) is ``"GREASE"`` and loses its value; an
    identifier or a secret (connection ids, the reset token, the preferred address) keeps only its length; any other integer parameter its value."""
    out: List[Dict[str, Any]] = []
    at = 0
    while at < len(raw):
        identifier, at = read_varint(raw, at)
        length, at = read_varint(raw, at)
        value = raw[at:at + length]
        if len(value) < length:
            raise ValueError("a transport parameter is cut short")
        at += length
        if _grease_quic(identifier):
            out.append({"id": GREASE, "length": length})
            continue
        entry: Dict[str, Any] = {"id": identifier, "name": QUIC_PARAMETER_NAMES.get(identifier, "param_%d" % identifier), "length": length}
        if identifier not in QUIC_OPAQUE and 0 < length <= 8 and identifier not in (0x11,):
            try:
                entry["value"], end = read_varint(value, 0)
                if end != length:
                    del entry["value"]
            except ValueError:
                pass
        out.append(entry)
    return out


H3_SETTING_NAMES = {0x01: "QPACK_MAX_TABLE_CAPACITY", 0x06: "MAX_FIELD_SECTION_SIZE", 0x07: "QPACK_BLOCKED_STREAMS", 0x08: "ENABLE_CONNECT_PROTOCOL",
                    0x33: "H3_DATAGRAM", 0x276: "H3_DATAGRAM_DRAFT04", 0x2B603742: "ENABLE_WEBTRANSPORT"}


def h3_settings(settings: Optional[Dict[int, int]]) -> Optional[List[List[Any]]]:
    """The SETTINGS frame of an HTTP/3 client as ``[[name, value], ...]``; a reserved identifier (0x1f * N + 0x21) is ``"GREASE"``."""
    if settings is None:
        return None
    return [["GREASE" if (k >= 0x21 and (k - 0x21) % 0x1F == 0) else H3_SETTING_NAMES.get(int(k), "0x%x" % int(k)), v] for k, v in settings.items()]


# --------------------------------------------------------------------------------------------------------------------------- the lab server
PAGE = (b"<!doctype html><meta charset=utf-8><title>wire</title><link rel=stylesheet href=/wire.css><script src=/wire.js></script>"
        b"<p>wire</p><img src=/wire.svg alt=''>")


def default_responder(request: Dict[str, Any]) -> Tuple[int, List[Tuple[str, str]], bytes]:
    """A small site: a page that loads a style, a script and an image, sets a cookie, and answers a POST."""
    target = request["target"].split("?")[0]
    if target == "/":
        return 200, [("content-type", "text/html; charset=utf-8"), ("set-cookie", "wire=1; Path=/; SameSite=Lax"), ("cache-control", "no-store")], PAGE
    if target == "/wire.css":
        return 200, [("content-type", "text/css"), ("cache-control", "no-store")], b"p{color:#333}"
    if target == "/wire.js":
        return 200, [("content-type", "text/javascript"), ("cache-control", "no-store")], b"window.__wire = 1;"
    if target == "/wire.svg":
        return 200, [("content-type", "image/svg+xml"), ("cache-control", "no-store")], b"<svg xmlns='http://www.w3.org/2000/svg' width='1' height='1'/>"
    return 200, [("content-type", "text/plain"), ("access-control-allow-origin", "*"), ("cache-control", "no-store")], b"ok"


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.server.owner._connection(self.request)  # type: ignore[attr-defined]


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True


class WireServer:
    """One TLS server (TCP) and, with ``h3=True``, a QUIC server on the same port number, each connection written down.

    ``alpn`` is what the server OFFERS (``["h2", "http/1.1"]``, or ``["http/1.1"]`` for a server that only speaks HTTP/1.1): the client's offer is the same
    either way, and what it reads is the answer. ``phase()`` names the stage of the run a connection or a request belongs to, at the time it arrives.

    ``connections`` is the list of the TLS connections, each ``{"id", "phase", "tls": <ClientHello>, "alpn": <selected>, "http1": [requests]}`` or
    ``{..., "h2": <H2Connection>}``; ``quic`` the QUIC connections ``{"phase", "transport_parameters", "h3_settings", "requests"}``. :meth:`snapshot` is the
    JSON of all of it."""

    def __init__(self, address: str = "127.0.0.1", *, alpn: Tuple[str, ...] = ("h2", "http/1.1"), h3: bool = False,
                 responder: Callable[[Dict[str, Any]], Tuple[int, List[Tuple[str, str]], bytes]] = default_responder,
                 phase: Callable[[], str] = lambda: "-", dns_names: Tuple[str, ...] = ("wire.test",), close_after_response: bool = False) -> None:
        from _doh_servers import make_certificate
        from _ports import bind_tcp_and_hold_udp

        self.address, self.alpn, self.responder, self.phase = address, tuple(alpn), responder, phase
        #: HTTP/1.1 only: every answer says ``Connection: close`` and ends its connection (see :func:`serve_http1`).
        self.close_after_response = close_after_response
        self._t0 = time.monotonic()
        self.connections: List[Dict[str, Any]] = []
        self.quic: List[Dict[str, Any]] = []
        self.refused: List[str] = []
        self._lock = threading.Lock()
        self._dir = tempfile.TemporaryDirectory()
        certificate, key = make_certificate(Path(self._dir.name), dns_names=dns_names, ip_addresses=(address,))
        self._tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._tls.load_cert_chain(str(certificate), str(key))
        self._tls.set_alpn_protocols(list(self.alpn))
        self._udp: Optional[socket.socket] = None
        if h3:
            self._tcp, self.port, self._udp = bind_tcp_and_hold_udp(address, lambda host, number: _Server((host, number), _Handler), socket_module=socket)
        else:
            self._tcp = _Server((address, 0), _Handler)
            self.port = self._tcp.server_address[1]
        self.alt_svc = 'h3=":%d"; ma=3600' % self.port if h3 else None
        self._tcp.owner = self  # type: ignore[attr-defined]
        threading.Thread(target=self._tcp.serve_forever, daemon=True).start()
        self._loop = None
        if h3:
            self._start_quic(certificate, key)

    @property
    def url(self) -> str:
        return "https://%s:%d/" % (self.address, self.port)

    # --- TCP ------------------------------------------------------------------------------------------------------------------------------
    def _connection(self, sock: socket.socket) -> None:
        record: Dict[str, Any] = {"phase": self.phase(), "tls": None, "alpn": None, "t": self._now()}
        with self._lock:
            record["id"] = len(self.connections)
            self.connections.append(record)
        tls = None
        try:
            hello, tls = accept_with_hello(sock, self._tls)
            record["tls"] = hello
            record["alpn"] = tls.selected_alpn_protocol() or "http/1.1"
            tls.settimeout(60)
            if record["alpn"] == "h2":
                record["h2"] = connection = H2Connection(self.responder, self.alt_svc)
                connection.serve(tls)
            else:
                record["http1"] = []
                serve_http1(tls, self.responder, lambda r: record["http1"].append(dict(r, phase=self.phase(), t=self._now())), self.alt_svc, self.close_after_response)
        except (ssl.SSLError, OSError, ConnectionError, NotAClientHello, TimeoutError) as exc:
            record["error"] = "%s: %s" % (type(exc).__name__, exc)
        finally:
            for s in (tls, sock):
                try:
                    if s is not None:
                        s.close()
                except OSError:
                    pass

    def _now(self) -> float:
        """Seconds since the server started: when a connection or a request arrived (evidence for a run that differs from itself; never compared)."""
        return round(time.monotonic() - self._t0, 3)

    # --- QUIC -----------------------------------------------------------------------------------------------------------------------------
    def _start_quic(self, certificate: Path, key: Path) -> None:
        import asyncio

        from aioquic.asyncio.protocol import QuicConnectionProtocol
        from aioquic.asyncio.server import QuicServer
        from aioquic.h3.connection import H3_ALPN, H3Connection
        from aioquic.h3.events import HeadersReceived
        from aioquic.quic.configuration import QuicConfiguration
        from aioquic.quic.events import ProtocolNegotiated

        server = self

        class Protocol(QuicConnectionProtocol):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self._http: Optional[Any] = None
                self._record: Dict[str, Any] = {}

            def quic_event_received(self, event: Any) -> None:
                if isinstance(event, ProtocolNegotiated):
                    self._http = H3Connection(self._quic)
                    raw = next((data for kind, data in (self._quic.tls.received_extensions or []) if kind in (0x39, 0xFFA5)), b"")
                    peer = self._quic._network_paths[0].addr
                    self._record = {"phase": server.phase(), "alpn": event.alpn_protocol, "peer": [peer[0], peer[1]],
                                    "transport_parameters": parse_quic_transport_parameters(raw), "h3_settings": None, "requests": []}
                    with server._lock:
                        server.quic.append(self._record)
                if self._http is None:
                    return
                for http_event in self._http.handle_event(event):
                    if isinstance(http_event, HeadersReceived):
                        headers = [[_text(n), _text(v)] for n, v in http_event.headers]
                        self._record["h3_settings"] = h3_settings(self._http.received_settings)
                        self._record["requests"].append({
                            "stream": http_event.stream_id, "pseudo_order": [n for n, _v in headers if n.startswith(":")],
                            "header_names": [n for n, _v in headers if not n.startswith(":")], "headers": headers,
                            "phase": server.phase()})
                        fields = dict(headers)
                        status, out, body = server.responder({"method": fields.get(":method", ""), "target": fields.get(":path", ""), "headers": headers,
                                                              "scheme": "https", "protocol": "h3", "body_length": 0})
                        self._http.send_headers(http_event.stream_id, [(b":status", str(status).encode())] +
                                                [(n.lower().encode(), v.encode()) for n, v in out if n.lower() != "content-length"] +
                                                [(b"content-length", str(len(body)).encode())])
                        self._http.send_data(http_event.stream_id, body, end_stream=True)
                        self.transmit()

        configuration = QuicConfiguration(is_client=False, alpn_protocols=H3_ALPN)
        configuration.load_cert_chain(str(certificate), str(key))
        self._loop = asyncio.new_event_loop()
        started = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(self._loop)
            _transport, self._quic = self._loop.run_until_complete(self._loop.create_datagram_endpoint(
                lambda: QuicServer(configuration=configuration, create_protocol=Protocol), sock=self._udp))
            started.set()
            try:
                self._loop.run_forever()
            finally:
                # The loop is closed HERE, on its own thread, after the QUIC server (as tests/_h3_server.py does): a loop that is only stopped is closed
                # later by the garbage collector on whatever thread it runs in, and on Windows the proactor's close then waits for ever.
                self._quic.close()
                for _ in range(3):      # a few turns of the loop let the closing transport flush
                    self._loop.run_until_complete(asyncio.sleep(0))
                self._loop.close()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        if not started.wait(30):
            raise RuntimeError("the QUIC server did not start")

    # --- what a test or the oracle reads --------------------------------------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            connections = list(self.connections)
            quic = list(self.quic)
        out: List[Dict[str, Any]] = []
        for c in connections:
            item = {"id": c["id"], "phase": c["phase"], "tls": c["tls"], "alpn": c["alpn"], "t": c.get("t")}
            if "h2" in c:
                item["h2"] = c["h2"].summary()
            if "http1" in c:
                item["http1"] = list(c["http1"])
            if "error" in c:
                item["error"] = c["error"]
            out.append(item)
        return {"connections": out, "quic": quic}

    def close(self) -> None:
        self._tcp.shutdown()
        self._tcp.server_close()
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(30)
        self._dir.cleanup()

    def __enter__(self) -> "WireServer":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
