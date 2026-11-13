"""SQLite connection setup and row insertion for the telemetry writer.

The database is opened once per writer thread and kept in WAL mode so readers
(the live dashboard history provider, CSV export, analysis scripts) can query
concurrently while BLE samples keep streaming in. All permission fixes go
through miband_monitor.config.security; this module never defines ad-hoc chmod
calls of its own.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from ..config.security import secure_path
from .schema import _INSERT_SQL, _SCHEMA

logger = logging.getLogger("MiBandStorage")

#: Files SQLite may create next to the main database in WAL mode.
WAL_SIDECAR_SUFFIXES = ("-wal", "-shm")

#: Commit at most once per this many inserted rows (durability/throughput
#: trade-off). A timeout tick or close always flushes the remainder.
COMMIT_BATCH_SIZE = 200


def secure_database_files(db_path: Path) -> None:
    """Tighten the database and any WAL/SHM sidecar file to 0600.

    Called after opening and again at shutdown, because SQLite creates the
    sidecars lazily with the process umask rather than the mode of the main
    database file.
    """
    secure_path(db_path, is_dir=False)
    for suffix in WAL_SIDECAR_SUFFIXES:
        sidecar = Path(f"{db_path}{suffix}")
        if sidecar.exists():
            secure_path(sidecar, is_dir=False)


def open_database(db_path: Path) -> sqlite3.Connection:
    """Create/tighten the database file and return a WAL-mode connection."""
    if not db_path.exists():
        db_path.touch(mode=0o600, exist_ok=True)
    secure_path(db_path, is_dir=False)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    secure_database_files(db_path)
    return conn


def insert_record(
    conn: sqlite3.Connection,
    kind: str,
    event_ts: float,
    iso_time: str,
    payload: dict,
) -> None:
    """Insert one queued record into its table.

    The branch-per-kind layout keeps the payload tuple order explicit; the
    matching INSERT statements live in miband_monitor.storage.schema.
    """
    if kind == "heart_rate":
        conn.execute(_INSERT_SQL[kind], (event_ts, iso_time, payload["bpm"]))
    elif kind == "steps":
        conn.execute(
            _INSERT_SQL[kind],
            (
                event_ts,
                iso_time,
                payload["steps"],
                payload["distance_m"],
                payload["calories"],
            ),
        )
    elif kind == "imu":
        conn.execute(
            _INSERT_SQL[kind],
            (
                event_ts,
                iso_time,
                payload["sensor_type"],
                payload["sequence"],
                payload["sample_index"],
                payload["x_raw"],
                payload["y_raw"],
                payload["z_raw"],
                payload["ax_g"],
                payload["ay_g"],
                payload["az_g"],
                payload["ax_ms2"],
                payload["ay_ms2"],
                payload["az_ms2"],
            ),
        )
    elif kind == "spo2":
        conn.execute(
            _INSERT_SQL[kind],
            (event_ts, iso_time, payload["spo2"], payload["auto_measure"]),
        )
    elif kind == "activity_history":
        conn.execute(
            _INSERT_SQL[kind],
            (
                event_ts,
                iso_time,
                payload["kind"],
                payload["intensity"],
                payload["steps"],
                payload["heart_rate"],
                payload["unknown1"],
                payload["sleep"],
                payload["deep_sleep"],
                payload["rem_sleep"],
            ),
        )
    elif kind == "spo2_sleep":
        conn.execute(
            _INSERT_SQL[kind],
            (
                event_ts,
                iso_time,
                payload["spo2"],
                payload["duration"],
                json.dumps(payload["spo2_high"]),
                json.dumps(payload["spo2_low"]),
                json.dumps(payload["signal_quality"]),
                json.dumps(payload["extend"]),
            ),
        )
    else:  # pragma: no cover - guarded by TelemetryStore callers
        raise ValueError(f"Unknown record kind: {kind}")
