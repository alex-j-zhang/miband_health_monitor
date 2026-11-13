"""SQLite-backed history provider for the live dashboard (?source=db).

The dashboard page keeps live samples in memory and merges persisted rows on
load, so the provider must reshape SQLite columns into exactly the records that
TelemetryHub.publish_* emits. The database is opened per request (WAL permits
concurrent readers); a missing file or table yields an empty series instead of
an error, which keeps --storage jsonl|none and a fresh install working.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from pathlib import Path
from typing import Callable, Dict, List

from .constants import _DB_HISTORY_COLUMNS

logger = logging.getLogger("MiBandDashboard")


def make_db_history_provider(db_path: str | Path) -> Callable[[str, int], List[Dict]]:
    """Build a DashboardServer.history_provider backed by the SQLite sink."""
    path = Path(db_path)

    def provider(kind: str, limit: int) -> List[Dict]:
        if kind not in _DB_HISTORY_COLUMNS:
            raise ValueError(f"unknown metric kind: {kind!r}")
        if not path.is_file():
            return []
        conn = sqlite3.connect(str(path))
        try:
            rows = conn.execute(
                f"SELECT {_DB_HISTORY_COLUMNS[kind]} FROM {kind} "
                "ORDER BY ts DESC LIMIT ?",
                (max(0, int(limit)),),
            ).fetchall()
        except sqlite3.Error as exc:  # missing table / not a database yet
            logger.debug("No %s history in %s: %s", kind, path, exc)
            return []
        finally:
            conn.close()

        records: List[Dict] = []
        for row in reversed(rows):  # chronological order for plotting
            record: Dict = {
                "kind": kind,
                "ts": round(float(row[0]), 6),
                "iso_time": row[1],
            }
            if kind == "heart_rate":
                record["bpm"] = int(row[2])
            elif kind == "steps":
                record.update(
                    steps=int(row[2]), distance_m=int(row[3]), calories=int(row[4])
                )
            elif kind == "imu":
                record["magnitude_g"] = round(
                    math.hypot(float(row[2]), float(row[3]), float(row[4])), 4
                )
            else:  # spo2 -- dashboard uses "status" for the auto-measure flag
                record.update(spo2=int(row[2]), status=int(row[3]))
            records.append(record)
        return records

    return provider


__all__ = ["make_db_history_provider"]
