"""Summarize the locally persisted Mi Band 6 telemetry (SQLite).

Usage::

    python history_report.py
    python history_report.py --hours 168 --json
    python history_report.py --db data/demo/mi_band.db --hours 1

Table names and physiological HR bounds are imported from miband_monitor.storage
and miband_monitor.config.health so they never drift from the persistence layer.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from ..config.health import HR_MAX, HR_MIN
from ..config.paths import default_db_path
from ..storage.readers import table_exists
from ..storage.schema import (
    ACTIVITY_HISTORY_TABLE,
    HEART_RATE_TABLE,
    IMU_TABLE,
    SPO2_SLEEP_TABLE,
    SPO2_TABLE,
    STEPS_TABLE,
)

#: Default database path, kept as a public name for backwards compatibility.
DEFAULT_DB = default_db_path()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Historical private alias used by tests/callers."""
    return table_exists(conn, table)


def _scalar(conn: sqlite3.Connection, query: str, params: tuple = ()):
    row = conn.execute(query, params).fetchone()
    return row[0] if row else None


def summarize(db_path: str | Path, hours: float = 24.0, now: float | None = None) -> dict:
    """Build an aggregate summary of the last *hours* of stored telemetry."""
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {path}")

    now_ts = time.time() if now is None else float(now)
    since = now_ts - max(0.0, float(hours)) * 3600.0
    summary: dict = {
        "db": str(path),
        "window_hours": float(hours),
        "since": round(since, 3),
        "since_iso": datetime.fromtimestamp(since).astimezone().isoformat(timespec="seconds"),
        "tables": {},
    }

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = summary["tables"]

        if _table_exists(conn, HEART_RATE_TABLE):
            count, minimum, maximum, average = conn.execute(
                f"SELECT COUNT(*), MIN(bpm), MAX(bpm), AVG(bpm) "
                f"FROM {HEART_RATE_TABLE} WHERE ts >= ?",
                (since,),
            ).fetchone()
            tables[HEART_RATE_TABLE] = {
                "count": int(count or 0),
                "min": minimum,
                "max": maximum,
                "avg": round(average, 2) if average is not None else None,
            }

        if _table_exists(conn, STEPS_TABLE):
            count = _scalar(
                conn, f"SELECT COUNT(*) FROM {STEPS_TABLE} WHERE ts >= ?", (since,)
            )
            latest = conn.execute(
                f"SELECT steps, distance_m, calories FROM {STEPS_TABLE} WHERE ts >= ? "
                "ORDER BY ts DESC LIMIT 1",
                (since,),
            ).fetchone()
            tables[STEPS_TABLE] = {
                "count": int(count or 0),
                "latest": (
                    {"steps": latest[0], "distance_m": latest[1], "calories": latest[2]}
                    if latest
                    else None
                ),
            }

        if _table_exists(conn, IMU_TABLE):
            count = _scalar(conn, f"SELECT COUNT(*) FROM {IMU_TABLE} WHERE ts >= ?", (since,))
            tables[IMU_TABLE] = {"sample_rows": int(count or 0)}

        if _table_exists(conn, SPO2_TABLE):
            count, minimum, maximum, average, auto_count = conn.execute(
                f"SELECT COUNT(*), MIN(spo2), MAX(spo2), AVG(spo2), "
                f"SUM(auto_measure) FROM {SPO2_TABLE} WHERE ts >= ?",
                (since,),
            ).fetchone()
            tables[SPO2_TABLE] = {
                "count": int(count or 0),
                "min": minimum,
                "max": maximum,
                "avg": round(average, 1) if average is not None else None,
                "auto_measurements": int(auto_count or 0),
            }

        if _table_exists(conn, SPO2_SLEEP_TABLE):
            count, minimum, maximum, average = conn.execute(
                f"SELECT COUNT(*), MIN(spo2), MAX(spo2), AVG(spo2) "
                f"FROM {SPO2_SLEEP_TABLE} WHERE ts >= ?",
                (since,),
            ).fetchone()
            tables[SPO2_SLEEP_TABLE] = {
                "count": int(count or 0),
                "min": minimum,
                "max": maximum,
                "avg": round(average, 1) if average is not None else None,
            }

        if _table_exists(conn, ACTIVITY_HISTORY_TABLE):
            # 0 means "no measurement this minute"; 255 (0xFF) is the band's
            # "heart rate unavailable" sentinel and must not be averaged in --
            # on real 2-day data it is present in ~37% of the samples and pushed
            # the reported average from 71 to 172 BPM.
            count, total_steps, avg_hr = conn.execute(
                f"SELECT COUNT(*), SUM(steps), "
                f"AVG(CASE WHEN heart_rate BETWEEN ? AND ? THEN heart_rate END) "
                f"FROM {ACTIVITY_HISTORY_TABLE} WHERE ts >= ?",
                (HR_MIN, HR_MAX, since),
            ).fetchone()
            tables[ACTIVITY_HISTORY_TABLE] = {
                "minutes": int(count or 0),
                "total_steps": int(total_steps or 0),
                "avg_heart_rate": round(avg_hr, 1) if avg_hr is not None else None,
            }
    finally:
        conn.close()

    return summary


def format_report(summary: dict) -> str:
    """Render :func:`summarize` output as a compact human-readable report."""
    lines = [
        f"Mi Band 6 history report — {summary['db']}",
        f"window: last {summary['window_hours']:g} h (since {summary['since_iso']})",
    ]
    tables = summary.get("tables", {})
    hr = tables.get(HEART_RATE_TABLE)
    if hr and hr["count"]:
        lines.append(
            f"  heart rate : {hr['count']} samples, min {hr['min']}, max {hr['max']}, "
            f"avg {hr['avg']} BPM"
        )
    spo2 = tables.get(SPO2_TABLE)
    if spo2 and spo2["count"]:
        lines.append(
            f"  SpO2       : {spo2['count']} samples, min {spo2['min']}%, max "
            f"{spo2['max']}%, avg {spo2['avg']}%, auto {spo2['auto_measurements']}"
        )
    spo2_sleep = tables.get(SPO2_SLEEP_TABLE)
    if spo2_sleep and spo2_sleep["count"]:
        lines.append(
            f"  SpO2 sleep : {spo2_sleep['count']} samples, min {spo2_sleep['min']}%, "
            f"max {spo2_sleep['max']}%, avg {spo2_sleep['avg']}%"
        )
    activity = tables.get(ACTIVITY_HISTORY_TABLE)
    if activity and activity["minutes"]:
        avg_hr = activity["avg_heart_rate"]
        lines.append(
            f"  activity   : {activity['minutes']} minutes, "
            f"{activity['total_steps']} steps"
            + (f", avg HR {avg_hr} BPM" if avg_hr is not None else "")
        )
    steps = tables.get(STEPS_TABLE)
    if steps and steps["count"]:
        latest = steps["latest"] or {}
        lines.append(
            f"  steps live : {steps['count']} updates, latest "
            f"{latest.get('steps', 0)} steps / {latest.get('distance_m', 0)} m / "
            f"{latest.get('calories', 0)} kcal"
        )
    imu = tables.get(IMU_TABLE)
    if imu and imu["sample_rows"]:
        lines.append(f"  IMU        : {imu['sample_rows']} sample rows")
    if len(lines) == 2:
        lines.append("  (no telemetry in this window)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for history_report.py."""
    parser = argparse.ArgumentParser(description="Summarize locally stored Mi Band 6 telemetry.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path")
    parser.add_argument("--hours", type=float, default=24.0, help="time window in hours")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args(argv)

    try:
        summary = summarize(args.db, hours=args.hours)
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f"[-] {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(format_report(summary))
    return 0
