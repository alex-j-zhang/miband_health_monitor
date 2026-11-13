"""Dashboard-wide constants derived from central configuration and storage schema.

The live hub, the SQLite history provider, the HTTP server and the page assets
all consume METRIC_KINDS and the timing/size limits from this module, so a
threshold or column rename has exactly one place to change. Values that already
exist in miband_monitor.config.defaults are aliased here instead of being
duplicated.
"""

from __future__ import annotations

from typing import Dict, Tuple

from ..config.defaults import (
    DEFAULT_HTTP_HEADER_TIMEOUT,
    DEFAULT_HTTP_READ_TIMEOUT,
    DEFAULT_HTTP_STOP_TIMEOUT,
    DEFAULT_MAX_POINTS,
    DEFAULT_SNAPSHOT_POINTS,
    DEFAULT_SSE_KEEPALIVE_SECONDS,
    DEFAULT_SSE_QUEUE_SIZE,
)
from ..storage.schema import TABLE_COLUMNS

#: Metric streams exposed by the live dashboard, in page/chart order.
#: Derived from the database projection below so both stay in sync.
_HISTORY_FIELDS: Dict[str, Tuple[str, ...]] = {
    "heart_rate": ("bpm",),
    "steps": ("steps", "distance_m", "calories"),
    "imu": ("ax_g", "ay_g", "az_g"),
    "spo2": ("spo2", "auto_measure"),
}

#: Columns read from each SQLite table. Table names come from this fixed map
#: (never from the request), so the query cannot be injected via kind. The
#: per-kind fields are filtered against storage.schema.TABLE_COLUMNS to keep the
#: projection honest if a table is ever reshaped.
_DB_HISTORY_COLUMNS: Dict[str, str] = {
    kind: ", ".join(
        ("ts", "iso_time") + tuple(field for field in fields if field in TABLE_COLUMNS[kind])
    )
    for kind, fields in _HISTORY_FIELDS.items()
}

METRIC_KINDS: Tuple[str, ...] = tuple(_DB_HISTORY_COLUMNS)

#: In-memory ring-buffer size per live series (matches the historical default).
MAX_POINTS = DEFAULT_MAX_POINTS
#: Rows returned by /api/snapshot and used as the /api/history default limit.
SNAPSHOT_POINTS = DEFAULT_SNAPSHOT_POINTS

#: Per-subscriber SSE queue bound; slow subscribers drop events, never block.
SSE_QUEUE_SIZE = DEFAULT_SSE_QUEUE_SIZE
#: Idle time before an SSE stream emits a keep-alive comment.
SSE_KEEPALIVE_SECONDS = DEFAULT_SSE_KEEPALIVE_SECONDS

#: Request deadlines used by the stdlib HTTP server.
HTTP_READ_TIMEOUT = DEFAULT_HTTP_READ_TIMEOUT
HTTP_HEADER_TIMEOUT = DEFAULT_HTTP_HEADER_TIMEOUT
HTTP_STOP_TIMEOUT = DEFAULT_HTTP_STOP_TIMEOUT

__all__ = [
    "DEFAULT_MAX_POINTS",
    "DEFAULT_SNAPSHOT_POINTS",
    "HTTP_HEADER_TIMEOUT",
    "HTTP_READ_TIMEOUT",
    "HTTP_STOP_TIMEOUT",
    "MAX_POINTS",
    "METRIC_KINDS",
    "SNAPSHOT_POINTS",
    "SSE_KEEPALIVE_SECONDS",
    "SSE_QUEUE_SIZE",
    "_DB_HISTORY_COLUMNS",
]
