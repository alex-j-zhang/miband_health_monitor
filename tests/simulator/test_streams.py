"""Live data streams and demo-mode persistence/export."""

from __future__ import annotations

import asyncio
import json
import sqlite3

from export_csv import export_database
from miband_client import StreamCallbacks, run_demo
from sim_band import SimulatedBand
from storage import TelemetryStore, read_jsonl
from tests.support import (
    AUTH_KEY,
    AUTH_KEY_BYTES,
    KEY,
    MAC,
    connect_simulated_client as _connect,
)


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
        "spo2_sleep",
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
