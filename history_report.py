"""Summarize the locally persisted Mi Band 6 telemetry (SQLite).

Usage::

    python history_report.py                      # default: data/mi_band.db, 24 h
    python history_report.py --hours 168 --json   # last 7 days as JSON
    python history_report.py --db data/demo/mi_band.db --hours 1
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

DEFAULT_DB = Path("data") / "mi_band.db"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


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

        if _table_exists(conn, "heart_rate"):
            count, minimum, maximum, average = conn.execute(
                "SELECT COUNT(*), MIN(bpm), MAX(bpm), AVG(bpm) FROM heart_rate WHERE ts >= ?",
                (since,),
            ).fetchone()
            tables["heart_rate"] = {
                "count": int(count or 0),
                "min": minimum,
                "max": maximum,
                "avg": round(average, 2) if average is not None else None,
            }

        if _table_exists(conn, "steps"):
            count = _scalar(conn, "SELECT COUNT(*) FROM steps WHERE ts >= ?", (since,))
            latest = conn.execute(
                "SELECT steps, distance_m, calories FROM steps WHERE ts >= ? "
                "ORDER BY ts DESC LIMIT 1",
                (since,),
            ).fetchone()
            tables["steps"] = {
                "count": int(count or 0),
                "latest": (
                    {"steps": latest[0], "distance_m": latest[1], "calories": latest[2]}
                    if latest
                    else None
                ),
            }

        if _table_exists(conn, "imu"):
            count = _scalar(conn, "SELECT COUNT(*) FROM imu WHERE ts >= ?", (since,))
            tables["imu"] = {"sample_rows": int(count or 0)}

        if _table_exists(conn, "spo2"):
            count, minimum, maximum, average, auto_count = conn.execute(
                "SELECT COUNT(*), MIN(spo2), MAX(spo2), AVG(spo2), "
                "SUM(auto_measure) FROM spo2 WHERE ts >= ?",
                (since,),
            ).fetchone()
            tables["spo2"] = {
                "count": int(count or 0),
                "min": minimum,
                "max": maximum,
                "avg": round(average, 1) if average is not None else None,
                "auto_measurements": int(auto_count or 0),
            }

        if _table_exists(conn, "activity_history"):
            count, total_steps, avg_hr = conn.execute(
                "SELECT COUNT(*), SUM(steps), AVG(NULLIF(heart_rate, 0)) "
                "FROM activity_history WHERE ts >= ?",
                (since,),
            ).fetchone()
            tables["activity_history"] = {
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
    hr = tables.get("heart_rate")
    if hr and hr["count"]:
        lines.append(
            f"  heart rate : {hr['count']} samples, min {hr['min']}, max {hr['max']}, "
            f"avg {hr['avg']} BPM"
        )
    spo2 = tables.get("spo2")
    if spo2 and spo2["count"]:
        lines.append(
            f"  SpO2       : {spo2['count']} samples, min {spo2['min']}%, max "
            f"{spo2['max']}%, avg {spo2['avg']}%, auto {spo2['auto_measurements']}"
        )
    activity = tables.get("activity_history")
    if activity and activity["minutes"]:
        avg_hr = activity["avg_heart_rate"]
        lines.append(
            f"  activity   : {activity['minutes']} minutes, "
            f"{activity['total_steps']} steps"
            + (f", avg HR {avg_hr} BPM" if avg_hr is not None else "")
        )
    steps = tables.get("steps")
    if steps and steps["count"]:
        latest = steps["latest"] or {}
        lines.append(
            f"  steps live : {steps['count']} updates, latest "
            f"{latest.get('steps', 0)} steps / {latest.get('distance_m', 0)} m / "
            f"{latest.get('calories', 0)} kcal"
        )
    imu = tables.get("imu")
    if imu and imu["sample_rows"]:
        lines.append(f"  IMU        : {imu['sample_rows']} sample rows")
    if len(lines) == 2:
        lines.append("  (no telemetry in this window)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
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


if __name__ == "__main__":
    sys.exit(main())
