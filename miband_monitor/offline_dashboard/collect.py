"""Gather everything the offline dashboard renders into one payload.

The payload is the whole contract between Python and the bundled JavaScript:
metadata, sleep sessions (with pre-rendered hypnograms), heart rate, activity
bands, steps, point SpO2 and the night-long sleep SpO2 series, plus the KPI
cards. It is a pure function of the SQLite contents and the static template, so
it can be checked without a browser.
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from miband_monitor.config.defaults import DEFAULT_OFFLINE_DAYS
from miband_monitor.sleep import analyze

from .constants import CLOCK_FORMAT, DATE_FORMAT, GENERATED_AT_FORMAT, LOCAL_DATETIME_FORMAT
from .kpis import _kpis
from .packaging import _package_activity, _package_heart_rate, _package_steps
from .sources import (
    _load_activity,
    _load_heart_rate,
    _load_spo2,
    _load_spo2_sleep,
    _local,
)
from .svg import flat_in_bed_svg, hypnogram_svg


def collect(
    db_path: str | Path,
    *,
    days: int = DEFAULT_OFFLINE_DAYS,
    spo2_sleep_path: str | Path | None = None,
) -> dict[str, Any]:
    """Gather everything the dashboard needs into one JSON-serialisable dict."""
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {path}")

    since = (datetime.now() - timedelta(days=days)).replace(second=0, microsecond=0).timestamp()
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        activity = _load_activity(conn, since)
        hr_rows = _load_heart_rate(conn, since)
        spo2_rows = _load_spo2(conn, since)
        spo2_sleep_rows, spo2_sleep_source = _load_spo2_sleep(
            conn, since, db_path, spo2_sleep_path
        )
    finally:
        conn.close()

    spo2_sleep_values = [row["spo2"] for row in spo2_sleep_rows]

    sleep_result = analyze(activity)
    sessions = [
        {
            **session.as_dict(),
            "hypnogram": session.hypnogram(),
            "stages": list(session.stages),
            # Without heart rate there is nothing to stack: show the in-bed span
            # as one flat band instead of four empty lanes.
            "svg": hypnogram_svg(session.stages)
            if session.as_dict()["heart_rate_available"]
            else flat_in_bed_svg(session.minutes),
            "clock": _local(session.start, CLOCK_FORMAT),
            # Labelled by the date the night started, the way Zepp Life does.
            "date": _local(session.start, DATE_FORMAT),
            # A session that begins at the very first sample in the store is not
            # a short night -- it is the tail of a night whose beginning the band
            # has already overwritten.  Say so, or the numbers read as "slept
            # 5.4 h" and get compared against the app in bad faith.
            "truncated_start": bool(activity)
            and abs(session.start - activity[0]["ts"]) <= 60,
        }
        for session in sleep_result["_sessions"]
    ]

    return {
        "meta": {
            "generated_at": datetime.now().strftime(GENERATED_AT_FORMAT),
            "source": str(db_path),
            "days": days,
            "activity_samples": len(activity),
            "hr_samples": len(hr_rows),
            "spo2_samples": len(spo2_rows),
            "spo2_sleep_samples": len(spo2_sleep_rows),
            "spo2_sleep_source": spo2_sleep_source,
            "range": [_local(activity[0]["ts"]) if activity else None,
                      _local(activity[-1]["ts"]) if activity else None],
        },
        "sleep": {
            "sessions": sessions,
            "totals": sleep_result["totals"],
            "not_worn": sleep_result["not_worn"],
        },
        "hr": _package_heart_rate(hr_rows, activity),
        "activity": _package_activity(
            activity,
            {sample["ts"] for session in sleep_result["_sessions"] for sample in session.samples},
        ),
        "steps": _package_steps(activity),
        "spo2": [
            {**item, "time": _local(item["ts"], LOCAL_DATETIME_FORMAT)} for item in spo2_rows
        ],
        "spo2_sleep": {
            "points": [[row["ts"], row["spo2"]] for row in spo2_sleep_rows],
            "count": len(spo2_sleep_rows),
            "min": min(spo2_sleep_values) if spo2_sleep_values else None,
            "max": max(spo2_sleep_values) if spo2_sleep_values else None,
            "avg": (
                round(statistics.mean(spo2_sleep_values), 1)
                if spo2_sleep_values else None
            ),
            "source": spo2_sleep_source,
        },
        "kpi": _kpis(
            sessions, activity, spo2_rows, hr_rows,
            sleep_result["not_worn"], spo2_sleep_rows,
        ),
    }
