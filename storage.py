"""Timestamped telemetry persistence for Mi Band 6 data streams.

Every sample received from the band is stamped with a UTC timestamp and sent
to one or more append-only sinks:

* **JSONL** — one JSON object per line (``heart_rate.jsonl``, ``steps.jsonl``,
  ``imu.jsonl``). Easy to stream into pandas/awk/jq.
* **SQLite** — a single ``mi_band.db`` (WAL mode) with one table per stream,
  indexed by timestamp. Easy to query and to export to CSV.

All disk I/O happens on one background writer thread, so BLE notification
callbacks never block on the filesystem. Records enqueued after :meth:`close`
are dropped (and counted) instead of raising inside a BLE callback.

Typical use::

    store = TelemetryStore("data", backend="both")
    store.record_heart_rate(72)
    store.record_steps({"steps": 1250, "distance_meters": 850, "calories": 45})
    store.record_imu(parsed_sensor_packet)   # one row per accelerometer sample
    store.close()
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Optional

logger = logging.getLogger("MiBandStorage")

STORAGE_BACKENDS = ("jsonl", "sqlite", "both", "none")

JSONL_FILENAMES = {
    "heart_rate": "heart_rate.jsonl",
    "steps": "steps.jsonl",
    "imu": "imu.jsonl",
    "spo2": "spo2.jsonl",
    "activity_history": "activity_history.jsonl",
}

DB_FILENAME = "mi_band.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS heart_rate (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    iso_time TEXT NOT NULL,
    bpm INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    iso_time TEXT NOT NULL,
    steps INTEGER NOT NULL,
    distance_m INTEGER NOT NULL,
    calories INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS imu (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    iso_time TEXT NOT NULL,
    sensor_type INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    sample_index INTEGER NOT NULL,
    x_raw INTEGER NOT NULL,
    y_raw INTEGER NOT NULL,
    z_raw INTEGER NOT NULL,
    ax_g REAL NOT NULL,
    ay_g REAL NOT NULL,
    az_g REAL NOT NULL,
    ax_ms2 REAL NOT NULL,
    ay_ms2 REAL NOT NULL,
    az_ms2 REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_heart_rate_ts ON heart_rate(ts);
CREATE INDEX IF NOT EXISTS idx_steps_ts ON steps(ts);
CREATE INDEX IF NOT EXISTS idx_imu_ts ON imu(ts);

CREATE TABLE IF NOT EXISTS spo2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    iso_time TEXT NOT NULL,
    spo2 INTEGER NOT NULL,
    auto_measure INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS activity_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    iso_time TEXT NOT NULL,
    kind INTEGER NOT NULL,
    intensity INTEGER NOT NULL,
    steps INTEGER NOT NULL,
    heart_rate INTEGER NOT NULL,
    unknown1 INTEGER NOT NULL DEFAULT 0,
    sleep INTEGER NOT NULL DEFAULT 0,
    deep_sleep INTEGER NOT NULL DEFAULT 0,
    rem_sleep INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_spo2_ts ON spo2(ts);
CREATE INDEX IF NOT EXISTS idx_activity_history_ts ON activity_history(ts);
"""

_INSERT_SQL = {
    "heart_rate": "INSERT INTO heart_rate (ts, iso_time, bpm) VALUES (?, ?, ?)",
    "steps": (
        "INSERT INTO steps (ts, iso_time, steps, distance_m, calories) "
        "VALUES (?, ?, ?, ?, ?)"
    ),
    "imu": (
        "INSERT INTO imu (ts, iso_time, sensor_type, sequence, sample_index, "
        "x_raw, y_raw, z_raw, ax_g, ay_g, az_g, ax_ms2, ay_ms2, az_ms2) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    ),
    "spo2": (
        "INSERT INTO spo2 (ts, iso_time, spo2, auto_measure) VALUES (?, ?, ?, ?)"
    ),
    "activity_history": (
        "INSERT INTO activity_history (ts, iso_time, kind, intensity, steps, "
        "heart_rate, unknown1, sleep, deep_sleep, rem_sleep) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    ),
}

_SENTINEL = object()


def utc_iso(timestamp: float) -> str:
    """Return an ISO-8601 UTC string with millisecond precision."""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="milliseconds")


def normalize_backend(backend: str) -> str:
    """Validate and normalise a storage backend name."""
    backend = (backend or "").strip().lower()
    if backend not in STORAGE_BACKENDS:
        raise ValueError(
            f"Unknown storage backend {backend!r}; expected one of {STORAGE_BACKENDS}"
        )
    return backend


class TelemetryStore:
    """Thread-safe timestamped sink for heart-rate, steps and IMU records."""

    def __init__(self, output_dir: str | Path = "data", backend: str = "both") -> None:
        self.backend = normalize_backend(backend)
        self.output_dir = Path(output_dir)
        self._use_jsonl = self.backend in ("jsonl", "both")
        self._use_sqlite = self.backend in ("sqlite", "both")
        self._enabled = self.backend != "none"

        self.db_path = self.output_dir / DB_FILENAME
        self.jsonl_paths = {
            name: self.output_dir / filename for name, filename in JSONL_FILENAMES.items()
        }

        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._failed = False
        self._counts = {
            "heart_rate": 0,
            "steps": 0,
            "imu": 0,
            "spo2": 0,
            "activity_history": 0,
        }
        self._dropped = 0
        self._thread: Optional[threading.Thread] = None

        if self._enabled:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self._thread = threading.Thread(
                target=self._writer_loop, name="miband-storage", daemon=True
            )
            self._thread.start()

    # ------------------------------------------------------------------ API
    def record_heart_rate(self, bpm: int, timestamp: Optional[float] = None) -> None:
        """Persist one heart-rate measurement (BPM)."""
        self._enqueue("heart_rate", timestamp, {"bpm": int(bpm)})

    def record_steps(self, info: dict, timestamp: Optional[float] = None) -> None:
        """Persist one real-time steps/calories payload (see parse_realtime_steps)."""
        self._enqueue(
            "steps",
            timestamp,
            {
                "steps": int(info.get("steps", 0)),
                "distance_m": int(info.get("distance_meters", 0)),
                "calories": int(info.get("calories", 0)),
            },
        )

    def record_imu(self, info: dict, timestamp: Optional[float] = None) -> None:
        """Persist one raw IMU packet as one row per accelerometer sample."""
        packet_ts = time.time() if timestamp is None else float(timestamp)
        for index, sample in enumerate(info.get("samples", [])):
            self._enqueue(
                "imu",
                packet_ts,
                {
                    "sensor_type": int(info.get("type", 0)),
                    "sequence": int(info.get("sequence", 0)),
                    "sample_index": index,
                    "x_raw": int(sample.get("x_raw", 0)),
                    "y_raw": int(sample.get("y_raw", 0)),
                    "z_raw": int(sample.get("z_raw", 0)),
                    "ax_g": float(sample.get("ax_g", 0.0)),
                    "ay_g": float(sample.get("ay_g", 0.0)),
                    "az_g": float(sample.get("az_g", 0.0)),
                    "ax_ms2": float(sample.get("ax_ms2", 0.0)),
                    "ay_ms2": float(sample.get("ay_ms2", 0.0)),
                    "az_ms2": float(sample.get("az_ms2", 0.0)),
                },
            )

    def record_spo2(
        self,
        spo2: int,
        auto_measure: bool = False,
        timestamp: Optional[float] = None,
    ) -> None:
        """Persist one blood-oxygen sample (percent, 0-100)."""
        self._enqueue(
            "spo2",
            timestamp,
            {"spo2": int(spo2), "auto_measure": 1 if auto_measure else 0},
        )

    def record_activity_history(
        self, sample: dict, timestamp: Optional[float] = None
    ) -> None:
        """Persist one per-minute historical activity sample fetched from the band."""
        self._enqueue(
            "activity_history",
            timestamp,
            {
                "kind": int(sample.get("kind", 0)),
                "intensity": int(sample.get("intensity", 0)),
                "steps": int(sample.get("steps", 0)),
                "heart_rate": int(sample.get("heart_rate", 0)),
                "unknown1": int(sample.get("unknown1", 0)),
                "sleep": int(sample.get("sleep", 0)),
                "deep_sleep": int(sample.get("deep_sleep", 0)),
                "rem_sleep": int(sample.get("rem_sleep", 0)),
            },
        )

    def stats(self) -> dict:
        """Return a snapshot of enqueue counters (useful for shutdown logs)."""
        with self._lock:
            return {
                "backend": self.backend,
                "counts": dict(self._counts),
                "dropped": self._dropped,
                "failed": self._failed,
                "closed": self._closed,
            }

    def close(self) -> None:
        """Flush all queued records and release file handles. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True

        if self._thread is None:
            return

        self._queue.put(_SENTINEL)
        self._thread.join(timeout=10.0)
        if self._thread.is_alive():
            logger.error("Storage writer thread did not flush within 10s; some rows may be lost.")

    def __enter__(self) -> "TelemetryStore":
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()

    # -------------------------------------------------------------- internals
    def _enqueue(self, kind: str, timestamp: Optional[float], payload: dict) -> None:
        if not self._enabled:
            return
        with self._lock:
            if self._closed:
                self._dropped += 1
                return
            self._counts[kind] += 1
        event_ts = time.time() if timestamp is None else float(timestamp)
        self._queue.put((kind, event_ts, utc_iso(event_ts), payload))

    def _writer_loop(self) -> None:
        jsonl_handles: dict[str, object] = {}
        conn: Optional[sqlite3.Connection] = None
        pending_commits = 0
        try:
            if self._use_jsonl:
                for kind, path in self.jsonl_paths.items():
                    jsonl_handles[kind] = open(path, "a", encoding="utf-8", buffering=1)
            if self._use_sqlite:
                conn = sqlite3.connect(self.db_path, check_same_thread=False)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.executescript(_SCHEMA)
                conn.commit()

            while True:
                try:
                    item = self._queue.get(timeout=0.5)
                except queue.Empty:
                    if conn is not None:
                        conn.commit()
                        pending_commits = 0
                    continue

                if item is _SENTINEL:
                    break

                kind, event_ts, iso_time, payload = item
                if jsonl_handles:
                    handle = jsonl_handles.get(kind)
                    if handle is not None:
                        row = {"ts": round(event_ts, 6), "iso_time": iso_time, **payload}
                        handle.write(json.dumps(row, ensure_ascii=False) + "\n")  # type: ignore[attr-defined]
                if conn is not None:
                    self._insert_sqlite(conn, kind, event_ts, iso_time, payload)
                    pending_commits += 1
                    if pending_commits >= 200:
                        conn.commit()
                        pending_commits = 0

            if conn is not None:
                conn.commit()
        except Exception as exc:  # pragma: no cover - defensive: disk full etc.
            self._failed = True
            logger.error("Storage writer failed; further records will be dropped: %s", exc)
            # Drain the queue so producers never block on a dead writer.
            while True:
                if self._queue.get() is _SENTINEL:
                    break
        finally:
            for handle in jsonl_handles.values():
                try:
                    handle.close()  # type: ignore[attr-defined]
                except OSError:
                    pass
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    @staticmethod
    def _insert_sqlite(
        conn: sqlite3.Connection,
        kind: str,
        event_ts: float,
        iso_time: str,
        payload: dict,
    ) -> None:
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
        else:  # pragma: no cover - guarded by callers
            raise ValueError(f"Unknown record kind: {kind}")


def read_jsonl(path: str | Path) -> list[dict]:
    """Small helper used by tests/diagnostics to load a JSONL file."""
    rows = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows
