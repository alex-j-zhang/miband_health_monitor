"""End-to-end integration tests against an in-memory simulated Mi Band 6.

These tests exercise the *real* client code (scan, connect, chunked ECDH auth,
stream subscription, persistence) and need no Bluetooth hardware.
"""

import asyncio
import json
import sqlite3

import pytest

from export_csv import export_database
from miband_client import (
    MiBand6Client,
    StreamCallbacks,
    run_demo,
    run_monitor,
)
from sim_band import (
    FakeBleakScanner,
    SimulatedBand,
    make_simulated_client,
    simulated_factories,
)
from storage import TelemetryStore, read_jsonl

MAC = "C0:FF:EE:C0:FF:EE"
AUTH_KEY = "0123456789abcdef0123456789abcdef"
AUTH_KEY_BYTES = bytes.fromhex(AUTH_KEY)


def _make_scanner(address: str):
    class _Scanner(FakeBleakScanner):
        default_address = address

    return _Scanner


async def _connect(band: SimulatedBand) -> MiBand6Client:
    client = make_simulated_client(MAC, AUTH_KEY, band, scanner_address=MAC)
    await client.connect_and_authenticate(scan_timeout=2.0)
    return client


async def test_simulated_authentication_succeeds():
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES)
    client = await _connect(band)
    try:
        assert client.authenticated is True
        assert band.authenticated is True
        assert band.connect_count == 1
    finally:
        await client.disconnect()


async def test_simulated_authentication_failure_raises():
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES, fail_auth=True)
    client = make_simulated_client(MAC, AUTH_KEY, band, scanner_address=MAC)
    try:
        with pytest.raises(RuntimeError, match="Authentication failed"):
            await client.connect_and_authenticate(scan_timeout=2.0)
        assert client.authenticated is False
    finally:
        await client.disconnect()


async def test_simulated_authentication_gap_is_detected():
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES, drop_auth_seqs=(2,))
    client = make_simulated_client(MAC, AUTH_KEY, band, scanner_address=MAC)
    try:
        with pytest.raises(RuntimeError, match="gap"):
            await client.connect_and_authenticate(scan_timeout=2.0)
    finally:
        await client.disconnect()


async def test_simulated_heart_rate_steps_and_imu_streams():
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES, hr_values=(70, 71, 72))
    client = await _connect(band)
    heart_rate: list[int] = []
    steps: list[dict] = []
    imu: list[dict] = []
    try:
        await client.start_heart_rate_stream(heart_rate.append)
        await client.start_steps_stream(steps.append)
        await client.start_raw_sensor_stream(imu.append)
        await asyncio.sleep(0.3)

        assert heart_rate == [70, 71, 72]
        assert steps and steps[-1]["steps"] == 1263
        assert imu and imu[0]["sample_count"] >= 1
    finally:
        await client.disconnect()


async def test_run_monitor_reconnects_after_link_loss(tmp_path):
    # Drop the link 0.12 s after each connect: after the handshake and at least
    # one heart-rate notification, so reconnects are exercised with real data.
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES, disconnect_after=0.12)
    sim_client_factory, _ = simulated_factories(band)
    scanner_class = _make_scanner(MAC)

    def factory(mac: str, key: str) -> MiBand6Client:
        return MiBand6Client(
            mac, key, client_factory=sim_client_factory, scanner_factory=scanner_class
        )

    store = TelemetryStore(tmp_path, backend="sqlite")
    callbacks = StreamCallbacks(
        heart_rate=store.record_heart_rate,
        steps=store.record_steps,
        imu=store.record_imu,
    )
    try:
        await run_monitor(
            MAC,
            AUTH_KEY,
            duration=0.7,
            imu=True,
            callbacks=callbacks,
            scan_timeout=2.0,
            reconnect=True,
            reconnect_initial_delay=0.01,
            reconnect_max_delay=0.02,
            client_factory=factory,
        )
    finally:
        store.close()

    assert band.connect_count >= 2, "supervisor should reconnect after link loss"
    conn = sqlite3.connect(tmp_path / "mi_band.db")
    try:
        heart_rate_rows = conn.execute("SELECT COUNT(*) FROM heart_rate").fetchone()[0]
    finally:
        conn.close()
    assert heart_rate_rows >= 1


async def test_run_monitor_retries_after_authentication_failure(tmp_path):
    # First connection rejects the challenge response; the supervisor must retry
    # and the second connection must authenticate and stream data.
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES, fail_auth_connections=1)
    sim_client_factory, _ = simulated_factories(band)
    scanner_class = _make_scanner(MAC)

    def factory(mac: str, key: str) -> MiBand6Client:
        return MiBand6Client(
            mac, key, client_factory=sim_client_factory, scanner_factory=scanner_class
        )

    store = TelemetryStore(tmp_path, backend="sqlite")
    callbacks = StreamCallbacks(heart_rate=store.record_heart_rate)
    try:
        await run_monitor(
            MAC,
            AUTH_KEY,
            duration=0.6,
            callbacks=callbacks,
            scan_timeout=2.0,
            reconnect=True,
            reconnect_initial_delay=0.01,
            reconnect_max_delay=0.02,
            client_factory=factory,
        )
    finally:
        store.close()

    assert band.connect_count >= 2, "supervisor should retry after auth failure"
    conn = sqlite3.connect(tmp_path / "mi_band.db")
    try:
        heart_rate_rows = conn.execute("SELECT COUNT(*) FROM heart_rate").fetchone()[0]
    finally:
        conn.close()
    assert heart_rate_rows >= 1


async def test_run_monitor_without_reconnect_stops_after_failure():
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES, fail_auth=True)
    sim_client_factory, _ = simulated_factories(band)
    scanner_class = _make_scanner(MAC)

    def factory(mac: str, key: str) -> MiBand6Client:
        return MiBand6Client(
            mac, key, client_factory=sim_client_factory, scanner_factory=scanner_class
        )

    with pytest.raises(RuntimeError, match="Authentication failed"):
        await run_monitor(
            MAC,
            AUTH_KEY,
            duration=5,
            scan_timeout=2.0,
            reconnect=False,
            client_factory=factory,
        )
    assert band.connect_count == 1


async def test_demo_end_to_end_writes_jsonl_and_sqlite(tmp_path):
    store = TelemetryStore(tmp_path, backend="both")
    callbacks = StreamCallbacks(
        heart_rate=store.record_heart_rate,
        steps=store.record_steps,
        imu=store.record_imu,
    )
    try:
        await run_demo(MAC, AUTH_KEY, duration=0.4, imu=True, callbacks=callbacks)
    finally:
        store.close()

    # JSONL: every record carries a UTC timestamp.
    for name in ("heart_rate", "steps", "imu"):
        rows = read_jsonl(tmp_path / f"{name}.jsonl")
        assert rows, f"{name}.jsonl should not be empty"
        assert all(isinstance(row["ts"], float) for row in rows)
        assert all(row["iso_time"].endswith("+00:00") for row in rows)

    # SQLite: all three tables populated.
    conn = sqlite3.connect(tmp_path / "mi_band.db")
    try:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("heart_rate", "steps", "imu")
        }
    finally:
        conn.close()
    assert counts["heart_rate"] >= 1
    assert counts["steps"] >= 1
    assert counts["imu"] >= 1

    # CSV export from the captured database.
    exported = export_database(tmp_path / "mi_band.db", tmp_path / "csv")
    assert set(exported) == {
        "heart_rate",
        "steps",
        "imu",
        "spo2",
        "activity_history",
    }
    for table, (path, rows) in exported.items():
        assert path.exists(), table
        with open(path, encoding="utf-8") as handle:
            header = handle.readline().strip().split(",")
        assert "ts" in header and "iso_time" in header
    assert exported["heart_rate"][1] >= 1
    assert exported["imu"][1] >= 1


async def test_demo_records_can_be_reloaded(tmp_path):
    store = TelemetryStore(tmp_path, backend="jsonl")
    try:
        await run_demo(
            MAC,
            AUTH_KEY,
            duration=0.3,
            imu=False,
            callbacks=StreamCallbacks(heart_rate=store.record_heart_rate),
        )
    finally:
        store.close()

    rows = read_jsonl(tmp_path / "heart_rate.jsonl")
    assert rows
    parsed = json.loads(json.dumps(rows[0]))
    assert 30 <= parsed["bpm"] <= 220
