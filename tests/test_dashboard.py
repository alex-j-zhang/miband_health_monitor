"""Tests for the stdlib-only real-time dashboard (hub, HTTP API, SSE)."""

import asyncio
import json

import pytest

from dashboard import (
    METRIC_KINDS,
    DashboardServer,
    TelemetryHub,
    make_db_history_provider,
)


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


def test_hub_publish_and_snapshot():
    hub = TelemetryHub(max_points=10)
    hub.publish_heart_rate(72)
    hub.publish_steps({"steps": 1250, "distance_meters": 850, "calories": 45})
    hub.publish_imu(
        {
            "type": 1,
            "sequence": 3,
            "samples": [
                {"ax_g": 0.0, "ay_g": 0.0, "az_g": 1.0},
                {"ax_g": 0.0, "ay_g": 0.0, "az_g": 1.0},
            ],
        }
    )
    hub.publish_spo2(98)

    snapshot = hub.snapshot()
    assert set(snapshot["series"]) == set(METRIC_KINDS)
    assert snapshot["latest"]["heart_rate"]["bpm"] == 72
    assert snapshot["latest"]["spo2"]["spo2"] == 98
    assert snapshot["latest"]["steps"]["steps"] == 1250
    assert snapshot["latest"]["imu"]["magnitude_g"] == pytest.approx(1.0)
    assert snapshot["counts"] == {"heart_rate": 1, "steps": 1, "imu": 1, "spo2": 1}
    # Every row carries a UTC timestamp.
    for kind in METRIC_KINDS:
        row = snapshot["series"][kind][0]
        assert isinstance(row["ts"], float)
        assert row["iso_time"].endswith("+00:00")

    history = hub.history("heart_rate", limit=5)
    assert history[-1]["bpm"] == 72
    with pytest.raises(ValueError):
        hub.history("bogus")


def test_hub_ring_buffer_bounds():
    hub = TelemetryHub(max_points=3)
    for bpm in range(5):
        hub.publish_heart_rate(60 + bpm)
    assert len(hub.snapshot()["series"]["heart_rate"]) == 3
    assert hub.snapshot()["counts"]["heart_rate"] == 5


async def test_hub_fanout_to_subscribers():
    hub = TelemetryHub()
    hub.attach_loop(asyncio.get_running_loop())
    queue = hub.subscribe()
    try:
        hub.publish_spo2(97)
        record = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert record["kind"] == "spo2"
        assert record["spo2"] == 97
        assert hub.subscriber_count == 1
    finally:
        hub.unsubscribe(queue)
    assert hub.subscriber_count == 0


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


def test_cli_dashboard_arguments():
    from miband_client import parse_args

    args = parse_args(["--demo", "5", "--web-port", "0", "--web-host", "0.0.0.0"])
    assert args.web_port == 0
    assert args.web_host == "0.0.0.0"

    args = parse_args(["--demo", "5"])
    assert args.web_port is None


# ---------------------------------------------------------------------------
# Persisted-history bridge (?source=db)
# ---------------------------------------------------------------------------
def test_db_history_provider_reshapes_sqlite_rows(tmp_path):
    from storage import TelemetryStore

    store = TelemetryStore(tmp_path, backend="both")
    store.record_heart_rate(71, timestamp=1000.0)
    store.record_heart_rate(75, timestamp=1001.0)
    store.record_steps(
        {"steps": 100, "distance_meters": 70, "calories": 4}, timestamp=1000.0
    )
    store.record_spo2(98, auto_measure=True, timestamp=1000.0)
    store.record_imu(
        {"samples": [{"ax_g": 0.0, "ay_g": 0.0, "az_g": 1.0}]}, timestamp=1000.0
    )
    store.close()

    provider = make_db_history_provider(store.db_path)
    hr = provider("heart_rate", 10)
    assert [row["bpm"] for row in hr] == [71, 75]  # chronological, not DESC
    assert hr[0]["kind"] == "heart_rate" and hr[0]["iso_time"]
    assert provider("heart_rate", 1) == [hr[-1]]

    steps = provider("steps", 10)
    assert (steps[0]["steps"], steps[0]["distance_m"], steps[0]["calories"]) == (
        100, 70, 4,
    )
    spo2 = provider("spo2", 10)
    assert (spo2[0]["spo2"], spo2[0]["status"]) == (98, 1)
    imu = provider("imu", 10)
    assert imu[0]["magnitude_g"] == 1.0


def test_db_history_provider_tolerates_missing_data(tmp_path):
    import sqlite3

    assert make_db_history_provider(tmp_path / "nope.db")("heart_rate", 10) == []

    empty_db = tmp_path / "empty.db"
    sqlite3.connect(empty_db).close()
    assert make_db_history_provider(empty_db)("heart_rate", 10) == []


def test_db_history_provider_rejects_unknown_kind(tmp_path):
    with pytest.raises(ValueError, match="unknown metric kind"):
        make_db_history_provider(tmp_path / "nope.db")("bogus", 10)


def test_dashboard_page_prefills_from_database():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "dashboard.html").read_text(
        encoding="utf-8"
    )
    assert "source=db" in html
    assert "dbSeries" in html
    assert "loadDbHistory" in html


async def test_main_wires_db_history_provider(monkeypatch, tmp_path):
    """The CLI must pass the provider, not merely expose the parameter."""
    import miband_client

    captured = {}

    class FakeServer:
        def __init__(self, hub, **kwargs):
            captured["provider"] = kwargs.get("history_provider")
            self.url = "http://127.0.0.1:0"

        async def start(self):
            return None

        async def stop(self):
            return None

    monkeypatch.setattr(miband_client, "DashboardServer", FakeServer)
    code = await miband_client.main(
        [
            "--demo", "1", "--web-port", "0",
            "--output", str(tmp_path), "--storage", "both",
        ]
    )
    assert code == 0
    provider = captured.get("provider")
    assert callable(provider)
    assert provider("heart_rate", 10)  # demo rows were persisted before close
