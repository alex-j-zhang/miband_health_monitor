"""Shared database fixtures for the offline historical dashboard tests."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from storage import _SCHEMA

BASE = datetime(2026, 11, 7, 23, 30).timestamp()


def make_db(tmp_path, *, activity=(), heart_rate=(), spo2=(), name="mi_band.db"):
    """Create a telemetry DB with the production schema and the given rows."""
    path = tmp_path / name
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA)
        conn.executemany(
            "INSERT INTO activity_history (ts, iso_time, kind, intensity, steps, heart_rate) "
            "VALUES (?, '', ?, ?, ?, ?)",
            [(ts, kind, intensity, steps, hr) for ts, kind, intensity, steps, hr in activity],
        )
        conn.executemany(
            "INSERT INTO heart_rate (ts, iso_time, bpm) VALUES (?, '', ?)",
            list(heart_rate),
        )
        conn.executemany(
            "INSERT INTO spo2 (ts, iso_time, spo2, auto_measure) VALUES (?, '', ?, ?)",
            list(spo2),
        )
        conn.commit()
    finally:
        conn.close()
    return path


def nights(*, start=BASE, minutes=420, hr=52, kind=240):
    """A plain in-bed night: one sample per minute of a single sleep kind."""
    return [(start + i * 60, kind, 0, 0, hr) for i in range(minutes)]


def _insert_sleep_spo2(path, rows):
    conn = sqlite3.connect(str(path))
    try:
        conn.executemany(
            "INSERT INTO spo2_sleep (ts, iso_time, spo2, duration, spo2_high, "
            "spo2_low, signal_quality, extend) VALUES (?, '', ?, ?, '[]', '[]', '[]', '[]')",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _capture_rows(*values):
    return [
        {"ts": BASE + i * 60, "spo2": value, "duration": 32,
         "spo2_high": [100], "spo2_low": [99], "signal_quality": [], "extend": []}
        for i, value in enumerate(values)
    ]
