"""Read-only helpers shared by diagnostics, exports and analysis code.

Nothing here starts a writer thread or creates files. secure_path is
re-exported from miband_monitor.config.security so historical callers
("from storage import secure_path") keep working without duplicating the
permission logic.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Union

from ..config.security import secure_path
from .schema import STORAGE_BACKENDS

PathLike = Union[str, Path]


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


def read_jsonl(path: PathLike) -> List[Dict]:
    """Load a JSONL file into a list of objects (blank lines are skipped)."""
    rows: List[Dict] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Return True when table exists in the connected SQLite database."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


__all__ = [
    "PathLike",
    "normalize_backend",
    "read_jsonl",
    "secure_path",
    "table_exists",
    "utc_iso",
]
