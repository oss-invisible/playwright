"""What an HTTP/3 client says, read by the lab server (``tests/_wire_capture.py``, the QUIC side of ``WireServer``), held to a real ``aioquic`` client with no browser.

The oracle compares what two engines send over QUIC: the transport parameters in the order they come, the SETTINGS frame of HTTP/3 and the order of the headers of a request.
This test makes the server read a client whose values it knows: the transport parameters aioquic sends, its SETTINGS, and a request with its pseudo-headers in the order it was
given. A server that reads none of them (an empty list, no SETTINGS, the headers sorted) is red here.
"""
from __future__ import annotations

import asyncio
import ssl

import pytest

import _wire_capture as wc

pytestmark = pytest.mark.unit


async def _get(port: int) -> int:
    from aioquic.asyncio import connect
    from aioquic.asyncio.protocol import QuicConnectionProtocol
    from aioquic.h3.connection import H3_ALPN, H3Connection
    from aioquic.h3.events import DataReceived, HeadersReceived
    from aioquic.quic.configuration import QuicConfiguration

    class Client(QuicConnectionProtocol):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.http = H3Connection(self._quic)
            self.status = asyncio.get_event_loop().create_future()

        def quic_event_received(self, event) -> None:
            for http_event in self.http.handle_event(event):
                if isinstance(http_event, HeadersReceived):
                    self.headers = dict(http_event.headers)
                elif isinstance(http_event, DataReceived) and http_event.stream_ended and not self.status.done():
                    self.status.set_result(int(self.headers[b":status"]))

    configuration = QuicConfiguration(is_client=True, alpn_protocols=H3_ALPN, verify_mode=ssl.CERT_NONE, server_name="wire.test")
    async with connect("127.0.0.1", port, configuration=configuration, create_protocol=Client) as client:
        stream = client._quic.get_next_available_stream_id()
        client.http.send_headers(stream, [(b":method", b"GET"), (b":scheme", b"https"), (b":authority", b"wire.test"), (b":path", b"/wire.css"),
                                          (b"user-agent", b"a-test"), (b"accept", b"text/css"), (b"x-last", b"z")], end_stream=True)
        client.transmit()
        return await asyncio.wait_for(client.status, 60)


def test_a_quic_client_is_recorded_transport_parameters_settings_and_the_order_of_a_request():
    with wc.WireServer(h3=True) as server:
        assert asyncio.run(_get(server.port)) == 200
        snapshot = server.snapshot()
    (connection,) = snapshot["quic"]
    assert connection["alpn"] == "h3"
    parameters = connection["transport_parameters"]
    names = [p.get("name") or p["id"] for p in parameters]
    assert names, "no transport parameter was read"
    assert "initial_max_data" in names and "initial_max_streams_bidi" in names and "max_idle_timeout" in names
    assert ("initial_source_connection_id" in names) and all("value" not in p for p in parameters if p.get("name") == "initial_source_connection_id")
    by_name = {p["name"]: p for p in parameters if "name" in p}
    assert by_name["initial_max_data"]["value"] > 0
    settings = dict(connection["h3_settings"])
    assert "QPACK_MAX_TABLE_CAPACITY" in settings and "QPACK_BLOCKED_STREAMS" in settings
    (request,) = connection["requests"]
    assert request["pseudo_order"] == [":method", ":scheme", ":authority", ":path"]
    assert request["header_names"] == ["user-agent", "accept", "x-last"]
    assert [n for n, _v in request["headers"]][:4] == [":method", ":scheme", ":authority", ":path"]
