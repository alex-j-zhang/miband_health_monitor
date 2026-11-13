"""Thread-safe timestamped telemetry sink: JSONL and/or SQLite.

Every sample received from the band is stamped with a UTC timestamp and handed
to one background writer thread, so BLE notification callbacks never block on
the filesystem. Records enqueued after close() are dropped (and counted) instead
of raising inside a BLE callback.

Typical use::

    store = TelemetryStore("data", backend="both")
    store.record_heart_rate(72)
    store.record_steps({"steps": 1250, "distance_meters": 850, "calories": 45})
    store.record_imu(parsed_sensor_packet)   # one row per accelerometer sample
    store.close()

Sinks (selected by the backend argument):

* JSONL -- one JSON object per line, one file per stream.
* SQLite -- a single mi_band.db (WAL mode) with one table per stream, indexed
  by timestamp. See miband_monitor.storage.schema for the exact format.
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import Optional

from ..config.security import secure_path
from . import writer
from .readers import normalize_backend, utc_iso
from .schema import DB_FILENAME, JSONL_FILENAMES
from .sqlite import insert_record

#: How long close() waits for the writer thread to flush before logging an error.
CLOSE_JOIN_TIMEOUT = 10.0


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
            "spo2_sleep": 0,
        }
        self._dropped = 0
        self._thread: Optional[threading.Thread] = None

        if self._enabled:
            # Create the directory eagerly so a bad --output path fails at
            # startup, but leave file creation to the writer thread.
            self.output_dir.mkdir(parents=True, exist_ok=True)
            secure_path(self.output_dir, is_dir=True)
            for path in [self.db_path, *self.jsonl_paths.values()]:
                if path.exists():
                    secure_path(path, is_dir=False)
            self._thread = threading.Thread(
                target=writer.run_writer_loop, args=(self,), name="miband-storage", daemon=True
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

    def record_spo2_sleep(
        self, sample: dict, timestamp: Optional[float] = None
    ) -> None:
        """Persist one sleep-SpO2 (0x26) record to its own table and JSONL.

        The 30-byte layout was verified on a Mi Band 6 (2020-01-08), but
        duration and the trailing byte groups have no confirmed meaning, so rows
        go to the dedicated spo2_sleep table -- never the point measurement
        spo2 table. Both sinks receive the record according to the configured
        backend.
        """
        self._enqueue(
            "spo2_sleep",
            timestamp if timestamp is not None else sample.get("ts"),
            {
                "spo2": int(sample.get("spo2", 0)),
                "duration": int(sample.get("duration", 0)),
                "spo2_high": list(sample.get("spo2_high", [])),
                "spo2_low": list(sample.get("spo2_low", [])),
                "signal_quality": list(sample.get("signal_quality", [])),
                "extend": list(sample.get("extend", [])),
                "source": "sleep",
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

        self._queue.put(writer.sentinel())
        self._thread.join(timeout=CLOSE_JOIN_TIMEOUT)
        if self._thread.is_alive():
            writer.logger.error(
                "Storage writer thread did not flush within 10s; some rows may be lost."
            )

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

    @staticmethod
    def _insert_sqlite(
        conn, kind: str, event_ts: float, iso_time: str, payload: dict
    ) -> None:
        """Historical static helper; delegates to storage.sqlite.insert_record."""
        insert_record(conn, kind, event_ts, iso_time, payload)


__all__ = ["CLOSE_JOIN_TIMEOUT", "TelemetryStore"]
