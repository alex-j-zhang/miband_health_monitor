"""Tests for sleep SpO2 (0x26) reporting in miband_monitor.reports.history."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from miband_monitor.reports.history import format_report, summarize
from miband_monitor.storage.schema import SPO2_SLEEP_TABLE


def test_history_report_includes_spo2_sleep(tmp_path: Path) -> None:
    db_path = tmp_path / "mi_band.db"
    now = int(time.time())
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"""
            CREATE TABLE {SPO2_SLEEP_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                iso_time TEXT NOT NULL,
                spo2 INTEGER NOT NULL,
                duration INTEGER NOT NULL,
                spo2_high INTEGER NOT NULL,
                spo2_low INTEGER NOT NULL,
                signal_quality INTEGER NOT NULL,
                extend TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            f"INSERT INTO {SPO2_SLEEP_TABLE} "
            "(ts, iso_time, spo2, duration, spo2_high, spo2_low, signal_quality, extend) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (now - 600, "2026-11-13T00:00:00Z", 96, 60, 97, 95, 10, "00"),
                (now - 300, "2026-11-13T00:05:00Z", 98, 60, 99, 97, 10, "00"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    summary = summarize(db_path, hours=24.0)
    assert summary["tables"][SPO2_SLEEP_TABLE] == {
        "count": 2,
        "min": 96,
        "max": 98,
        "avg": 97.0,
    }
    text = format_report(summary)
    assert "SpO2 sleep : 2 samples, min 96%, max 98%, avg 97.0%" in text
