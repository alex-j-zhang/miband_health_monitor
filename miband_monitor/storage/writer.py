"""Background queue writer: one thread owns every file/SQLite handle.

BLE notification callbacks must never block on disk I/O, so TelemetryStore only
enqueues (kind, ts, iso_time, payload) tuples and this loop consumes them. The
loop commits in small batches and always drains the queue on failure so a dead
writer can never deadlock a producer.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ..config.security import secure_path
from .sqlite import (
    COMMIT_BATCH_SIZE,
    insert_record,
    open_database,
    secure_database_files,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from .store import TelemetryStore

logger = logging.getLogger("MiBandStorage")

#: How long the writer blocks on the queue before flushing pending SQLite rows.
QUEUE_POLL_SECONDS = 0.5

#: Marks the end of the queue; identity comparison only, never serialized.
_SENTINEL = object()


def run_writer_loop(store: "TelemetryStore") -> None:
    """Consume the store queue until the close sentinel is received.

    Expected TelemetryStore attributes (the writer thread's single argument):
    _queue, jsonl_paths, db_path, _use_jsonl, _use_sqlite and _failed. Files and
    the SQLite connection are created here, on the writer thread, so the
    constructor never performs blocking I/O beyond creating the output
    directory.
    """
    jsonl_handles: dict[str, object] = {}
    conn: Optional[sqlite3.Connection] = None
    pending_commits = 0
    try:
        # -- JSONL sinks: append-only, one handle per stream ------------------
        if store._use_jsonl:
            for kind, path in store.jsonl_paths.items():
                if not path.exists():
                    path.touch(mode=0o600, exist_ok=True)
                secure_path(path, is_dir=False)
                jsonl_handles[kind] = open(path, "a", encoding="utf-8", buffering=1)

        # -- SQLite sink: single WAL database with one table per stream ------
        if store._use_sqlite:
            conn = open_database(store.db_path)

        while True:
            try:
                item = store._queue.get(timeout=QUEUE_POLL_SECONDS)
            except queue.Empty:
                # Idle tick: flush whatever the last batch accumulated.
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
                insert_record(conn, kind, event_ts, iso_time, payload)
                pending_commits += 1
                if pending_commits >= COMMIT_BATCH_SIZE:
                    conn.commit()
                    pending_commits = 0

        if conn is not None:
            conn.commit()
    except Exception as exc:  # pragma: no cover - defensive: disk full etc.
        store._failed = True
        logger.error("Storage writer failed; further records will be dropped: %s", exc)
        # Drain the queue so producers never block on a dead writer.
        while True:
            if store._queue.get() is _SENTINEL:
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
        secure_database_files(store.db_path)


def sentinel() -> object:
    """Return the shared close marker used by TelemetryStore.close()."""
    return _SENTINEL


__all__ = ["QUEUE_POLL_SECONDS", "run_writer_loop", "sentinel"]
