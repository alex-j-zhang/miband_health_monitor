"""Local history report summaries and CLI argument wiring."""

from __future__ import annotations

import pytest

from band_history import HistorySyncResult, summarize_history_result, sync_history
from history_report import format_report, summarize
from miband_client import parse_args
from sim_band import SimulatedBand
from storage import TelemetryStore
from tests.support import (
    AUTH_KEY_BYTES,
    connect_simulated_client as _connected_client,
)


def test_summarize_marks_sleep_spo2_as_experimental():
    default = HistorySyncResult()
    assert "sleep SpO2" not in summarize_history_result(default)

    enabled = HistorySyncResult(spo2_sleep=[{"ts": 1.0, "spo2": 97}])
    summary = summarize_history_result(enabled)
    assert "1 sleep SpO2 sample(s) [experimental]" in summary


# ---------------------------------------------------------------------------
# Local history report
# ---------------------------------------------------------------------------
async def test_history_report_summarizes_database(tmp_path):
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES)
    client = await _connected_client(band)
    store = TelemetryStore(tmp_path, backend="sqlite")
    try:
        await sync_history(
            client,
            days=1,
            max_rounds=1,
            timeout=3.0,
            on_spo2=lambda sample: store.record_spo2(
                sample["spo2"], sample["auto_measure"], timestamp=sample["ts"]
            ),
            on_activity=lambda sample: store.record_activity_history(
                sample, timestamp=sample["ts"]
            ),
        )
        store.record_heart_rate(72)
    finally:
        await client.disconnect()
        store.close()

    summary = summarize(tmp_path / "mi_band.db", hours=24)
    assert summary["tables"]["spo2"]["count"] == len(band.history_spo2)
    assert summary["tables"]["activity_history"]["minutes"] == len(band.history_activity)
    assert summary["tables"]["activity_history"]["total_steps"] > 0
    assert summary["tables"]["heart_rate"]["avg"] == 72.0

    report = format_report(summary)
    assert "SpO2" in report
    assert "activity" in report
    assert "heart rate" in report


def test_activity_average_ignores_unavailable_heart_rate(tmp_path):
    """255 is the band's "measurement unavailable" sentinel, not a BPM value.

    Real 2-day data carries it in roughly a third of the minutes; averaging it
    in reported 172 BPM where the true average was 71.
    """
    def sample(heart_rate: int, steps: int = 0) -> dict:
        return {
            "kind": 1,
            "intensity": 5,
            "steps": steps,
            "heart_rate": heart_rate,
            "unknown1": 0,
            "sleep": 0,
            "deep_sleep": 0,
            "rem_sleep": 0,
        }

    store = TelemetryStore(tmp_path, backend="sqlite")
    try:
        for heart_rate in (60, 80, 255, 255, 0):
            store.record_activity_history(sample(heart_rate))
    finally:
        store.close()

    summary = summarize(tmp_path / "mi_band.db", hours=48)
    activity = summary["tables"]["activity_history"]
    assert activity["minutes"] == 5
    assert activity["avg_heart_rate"] == 70.0


def test_history_report_missing_database(tmp_path):
    with pytest.raises(FileNotFoundError):
        summarize(tmp_path / "missing.db")


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------
def test_cli_sync_history_argument():
    args = parse_args(["--demo", "1", "--sync-history"])
    assert args.sync_history == 7
    args = parse_args(["--demo", "1", "--sync-history", "3"])
    assert args.sync_history == 3
    args = parse_args(["--demo", "1"])
    assert args.sync_history is None
