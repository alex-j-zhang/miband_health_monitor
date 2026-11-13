"""Read-only SQLite/JSONL data sources for the offline dashboard.

The dashboard tolerates databases written by older versions, so every table is
probed before it is queried and the 0x26 sleep SpO2 stream falls back from its
dedicated table to the historical JSONL capture. Table/column names come from
:mod:`miband_monitor.storage.schema`, so readers cannot drift from the writer.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from miband_monitor.config.health import SPO2_MAX, SPO2_MIN
from miband_monitor.storage.readers import table_exists as _table_exists
from miband_monitor.storage.schema import (
    ACTIVITY_HISTORY_COLUMNS,
    ACTIVITY_HISTORY_TABLE,
    HEART_RATE_COLUMNS,
    HEART_RATE_TABLE,
    SPO2_COLUMNS,
    SPO2_SLEEP_COLUMNS,
    SPO2_SLEEP_TABLE,
    SPO2_TABLE,
)

from .constants import LOCAL_DATETIME_FORMAT, SOURCE_JSONL, SOURCE_NONE, SOURCE_SQLITE, SPO2_SLEEP_JSONL

#: SELECT field order per source, derived from the schema column tuples.
_ACTIVITY_FIELDS = tuple(
    name
    for name in ACTIVITY_HISTORY_COLUMNS
    if name in ("ts", "kind", "intensity", "steps", "heart_rate")
)
_HEART_RATE_FIELDS = tuple(name for name in HEART_RATE_COLUMNS if name in ("ts", "bpm"))
_SPO2_FIELDS = tuple(name for name in SPO2_COLUMNS if name in ("ts", "spo2", "auto_measure"))
_SPO2_SLEEP_FIELDS = tuple(
    name for name in SPO2_SLEEP_COLUMNS if name not in ("id", "iso_time")
)


def _local(timestamp: float, fmt: str = LOCAL_DATETIME_FORMAT) -> str:
    """Local-time label used throughout the payload."""
    return datetime.fromtimestamp(timestamp).strftime(fmt)


def _byte_group(value: Any) -> list[int]:
    """SQLite keeps the four uninterpreted trailing groups as JSON text."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return list(value) if isinstance(value, (list, tuple)) else []


def _load_activity(conn: sqlite3.Connection, since: float) -> list[dict]:
    """Band-side minute history, oldest first (empty when the table is absent)."""
    if not _table_exists(conn, ACTIVITY_HISTORY_TABLE):
        return []
    return [
        {"ts": float(ts), "kind": int(kind), "intensity": int(intensity),
         "steps": int(steps), "heart_rate": int(hr)}
        for ts, kind, intensity, steps, hr in conn.execute(
            f"SELECT {', '.join(_ACTIVITY_FIELDS)} FROM {ACTIVITY_HISTORY_TABLE} "
            "WHERE ts >= ? ORDER BY ts",
            (since,),
        )
    ]


def _load_heart_rate(conn: sqlite3.Connection, since: float) -> list[tuple[float, int]]:
    """Live per-sample heart rate, oldest first (empty when the table is absent)."""
    if not _table_exists(conn, HEART_RATE_TABLE):
        return []
    return [
        (float(ts), int(bpm))
        for ts, bpm in conn.execute(
            f"SELECT {', '.join(_HEART_RATE_FIELDS)} FROM {HEART_RATE_TABLE} "
            "WHERE ts >= ? ORDER BY ts",
            (since,),
        )
    ]


def _load_spo2(conn: sqlite3.Connection, since: float) -> list[dict]:
    """Point-measurement SpO2 (0x25) rows, oldest first."""
    rows: list[dict] = []
    if not _table_exists(conn, SPO2_TABLE):
        return rows
    for ts, spo2, auto in conn.execute(
        f"SELECT {', '.join(_SPO2_FIELDS)} FROM {SPO2_TABLE} WHERE ts >= ? ORDER BY ts",
        (since,),
    ):
        rows.append({"ts": float(ts), "spo2": int(spo2), "auto": bool(auto)})
    return rows


def _load_spo2_sleep(
    conn: sqlite3.Connection,
    since: float,
    db_path: str | Path,
    explicit_path: str | Path | None = None,
) -> tuple[list[dict], str]:
    """Read 0x26 samples: dedicated table first, JSONL capture as fallback.

    Older runs produced only ``spo2_sleep.jsonl``. Keeping that fallback lets
    already-captured evidence be viewed without another sync -- the band may
    clear uploaded data after the ACK, so a second fetch often returns nothing.
    Returns ``(rows, source)`` where source is ``sqlite`` / ``jsonl`` / ``none``.
    """
    if _table_exists(conn, SPO2_SLEEP_TABLE):
        rows = [
            {
                "ts": float(ts),
                "spo2": int(spo2),
                "duration": int(duration),
                "spo2_high": _byte_group(high),
                "spo2_low": _byte_group(low),
                "signal_quality": _byte_group(signal),
                "extend": _byte_group(extend),
            }
            for ts, spo2, duration, high, low, signal, extend in conn.execute(
                f"SELECT {', '.join(_SPO2_SLEEP_FIELDS)} FROM {SPO2_SLEEP_TABLE} "
                "WHERE ts >= ? ORDER BY ts",
                (since,),
            )
        ]
        if rows:
            return rows, SOURCE_SQLITE

    path = Path(explicit_path) if explicit_path else Path(db_path).parent / SPO2_SLEEP_JSONL
    if not path.is_file():
        return [], SOURCE_NONE

    rows = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue  # a half-written line must not break the whole page
            if not isinstance(item, dict) or "ts" not in item or "spo2" not in item:
                continue
            try:
                ts = float(item["ts"])
                spo2 = int(item.get("spo2", 0))
            except (TypeError, ValueError):
                continue
            if ts < since or not SPO2_MIN <= spo2 <= SPO2_MAX:
                continue
            rows.append(
                {
                    "ts": ts,
                    "spo2": spo2,
                    "duration": int(item.get("duration", 0) or 0),
                    "spo2_high": _byte_group(item.get("spo2_high", [])),
                    "spo2_low": _byte_group(item.get("spo2_low", [])),
                    "signal_quality": _byte_group(item.get("signal_quality", [])),
                    "extend": _byte_group(item.get("extend", [])),
                }
            )
    rows.sort(key=lambda item: item["ts"])
    return rows, SOURCE_JSONL if rows else SOURCE_NONE
