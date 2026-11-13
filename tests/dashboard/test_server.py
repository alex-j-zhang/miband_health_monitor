"""HTTP API, SSE stream and index fallback of DashboardServer."""

from __future__ import annotations

import asyncio
import json

from dashboard import DashboardServer, TelemetryHub


async def _http_get(port: int, path: str, timeout: float = 3.0):
    """Minimal HTTP/1.1 client: returns (status, headers, body)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        request = (
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(request)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), timeout=timeout)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass

    head, _, body = data.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n")[0].split(b" ")[1])
    return status, head.decode("latin-1"), body


async def _read_until(reader: asyncio.StreamReader, predicate, timeout: float = 3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError("timed out waiting for SSE data")
        line = await asyncio.wait_for(reader.readline(), timeout=remaining)
        if predicate(line):
            return line


async def test_http_endpoints():
    hub = TelemetryHub()
    hub.attach_loop(asyncio.get_running_loop())
    hub.publish_heart_rate(70)
    hub.publish_steps({"steps": 100, "distance_meters": 70, "calories": 4})
    server = DashboardServer(hub, host="127.0.0.1", port=0)
    await server.start()
    try:
        assert server.port > 0

        status, headers, body = await _http_get(server.port, "/")
        assert status == 200
        assert "text/html" in headers
        assert b"Mi Band 6" in body

        status, headers, body = await _http_get(server.port, "/healthz")
        assert status == 200
        assert json.loads(body)["status"] == "ok"

        status, _headers, body = await _http_get(server.port, "/api/snapshot")
        assert status == 200
        snapshot = json.loads(body)
        assert snapshot["latest"]["heart_rate"]["bpm"] == 70

        status, _headers, body = await _http_get(
            server.port, "/api/history?kind=heart_rate&limit=10"
        )
        assert status == 200
        payload = json.loads(body)
        assert payload["kind"] == "heart_rate"
        assert payload["rows"][-1]["bpm"] == 70

        status, _headers, body = await _http_get(server.port, "/api/history?kind=bogus")
        assert status == 400
        assert "unknown kind" in json.loads(body)["error"]

        status, _headers, _body = await _http_get(
            server.port, "/api/history?kind=heart_rate&limit=abc"
        )
        assert status == 400

        status, _headers, _body = await _http_get(server.port, "/nope")
        assert status == 404
    finally:
        await server.stop()


async def test_sse_stream_delivers_live_samples():
    hub = TelemetryHub()
    hub.attach_loop(asyncio.get_running_loop())
    server = DashboardServer(hub, host="127.0.0.1", port=0)
    await server.start()
    reader = None
    writer = None
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(
            b"GET /events HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\n\r\n"
        )
        await writer.drain()

        while True:  # skip response headers
            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            if line in (b"\r\n", b"\n", b""):
                break

        first_event = await _read_until(reader, lambda line: line.startswith(b"data:"))
        assert b'"type":"snapshot"' in first_event

        hub.publish_heart_rate(88)
        live_event = await _read_until(
            reader,
            lambda line: line.startswith(b"data:")
            and b'"kind":"heart_rate"' in line
            and b"88" in line,
        )
        payload = json.loads(live_event[len(b"data:") :])
        assert payload["bpm"] == 88
        assert payload["iso_time"].endswith("+00:00")
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        await server.stop()


async def test_index_fallback_when_dashboard_html_missing(tmp_path):
    hub = TelemetryHub()
    server = DashboardServer(
        hub, host="127.0.0.1", port=0, index_path=tmp_path / "missing.html"
    )
    await server.start()
    try:
        status, _headers, body = await _http_get(server.port, "/")
        assert status == 200
        assert b"/api/snapshot" in body
        assert b"/events" in body
    finally:
        await server.stop()


async def test_history_provider_used_for_db_source():
    hub = TelemetryHub()
    calls: list[tuple[str, int]] = []

    def provider(kind: str, limit: int) -> list[dict]:
        calls.append((kind, limit))
        return [{"kind": kind, "ts": 1.0, "iso_time": "1970-01-01T00:00:01.000+00:00", "bpm": 55}]

    server = DashboardServer(hub, host="127.0.0.1", port=0, history_provider=provider)
    await server.start()
    try:
        status, _headers, body = await _http_get(
            server.port, "/api/history?kind=heart_rate&source=db&limit=5"
        )
        assert status == 200
        assert calls == [("heart_rate", 5)]
        assert json.loads(body)["rows"][0]["bpm"] == 55
    finally:
        await server.stop()
