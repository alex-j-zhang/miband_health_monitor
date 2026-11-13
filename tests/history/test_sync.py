"""sync_history persistence and opt-in sleep SpO2 behaviour."""

from __future__ import annotations

import sqlite3

from band_history import summarize_history_result, sync_history
from sim_band import SimulatedBand
from storage import TelemetryStore, read_jsonl
from tests.support import (
    AUTH_KEY_BYTES,
    connect_simulated_client as _connected_client,
)


async def test_sync_history_spo2_sleep_is_opt_in():
    """Default sync must never request 0x26; the opt-in path delivers samples."""
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES)
    client = await _connected_client(band)
    try:
        baseline = await sync_history(client, days=1, timeout=3.0, max_rounds=1)
        assert baseline.spo2_sleep == []
        assert "spo2_sleep" not in baseline.rounds

        seen: list[dict] = []
        result = await sync_history(
            client,
            days=1,
            include_spo2_sleep=True,
            on_spo2_sleep=seen.append,
            timeout=3.0,
            max_rounds=1,
        )
        assert len(result.spo2_sleep) == len(band.history_spo2_sleep)
        assert seen == result.spo2_sleep
        assert "spo2_sleep" in result.rounds
    finally:
        await client.disconnect()


# ---------------------------------------------------------------------------
# sync_history + storage integration
# ---------------------------------------------------------------------------
async def test_sync_history_persists_spo2_and_activity(tmp_path):
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES)
    client = await _connected_client(band)
    store = TelemetryStore(tmp_path, backend="both")
    try:
        result = await sync_history(
            client,
            days=1,
            max_rounds=2,
            timeout=3.0,
            on_spo2=lambda sample: store.record_spo2(
                sample["spo2"],
                auto_measure=sample["auto_measure"],
                timestamp=sample["ts"],
            ),
            on_activity=lambda sample: store.record_activity_history(
                sample, timestamp=sample["ts"]
            ),
        )
    finally:
        await client.disconnect()
        store.close()

    assert result.errors == []
    assert len(result.spo2) == len(band.history_spo2)
    assert len(result.activity) == len(band.history_activity)
    assert "SpO2" in summarize_history_result(result)

    conn = sqlite3.connect(tmp_path / "mi_band.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM spo2").fetchone()[0] == len(band.history_spo2)
        assert conn.execute("SELECT COUNT(*) FROM activity_history").fetchone()[0] == len(
            band.history_activity
        )
        spo2_rows = conn.execute("SELECT spo2, auto_measure FROM spo2 ORDER BY ts").fetchall()
    finally:
        conn.close()
    assert [row[0] for row in spo2_rows] == [s["spo2"] for s in band.history_spo2]
    assert any(row[1] for row in spo2_rows)

    spo2_jsonl = read_jsonl(tmp_path / "spo2.jsonl")
    assert len(spo2_jsonl) == len(band.history_spo2)
    assert all("ts" in row and "iso_time" in row for row in spo2_jsonl)
    activity_jsonl = read_jsonl(tmp_path / "activity_history.jsonl")
    assert len(activity_jsonl) == len(band.history_activity)


async def test_sync_history_reports_errors_without_raising(tmp_path):
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES, drop_history_packets=(0,))
    client = await _connected_client(band)
    try:
        store = TelemetryStore(tmp_path, backend="sqlite")
        try:
            result = await sync_history(
                client,
                days=1,
                max_rounds=1,
                timeout=2.0,
                on_spo2=lambda sample: store.record_spo2(
                    sample["spo2"], sample["auto_measure"], timestamp=sample["ts"]
                ),
            )
        finally:
            store.close()
    finally:
        await client.disconnect()

    assert result.errors, "packet loss should be reported as an error"
    assert result.spo2 == []
