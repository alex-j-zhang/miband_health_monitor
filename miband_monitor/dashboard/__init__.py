"""Live web dashboard: in-memory hub, HTTP/SSE API and static page assets.

Public API re-exported for backward compatibility with the historical
dashboard module. New code may import from the focused submodules instead:

* miband_monitor.dashboard.constants -- METRIC_KINDS, limits, DB column map
* miband_monitor.dashboard.history -- make_db_history_provider (?source=db)
* miband_monitor.dashboard.hub -- TelemetryHub ring buffers and SSE fan-out
* miband_monitor.dashboard.http -- HTTP/SSE response framing helpers
* miband_monitor.dashboard.page -- single-file page composition and fallback
* miband_monitor.dashboard.server -- DashboardServer (asyncio HTTP/SSE server)
"""

import logging

from ..storage.readers import utc_iso
from .constants import (
    DEFAULT_MAX_POINTS,
    DEFAULT_SNAPSHOT_POINTS,
    HTTP_HEADER_TIMEOUT,
    HTTP_READ_TIMEOUT,
    HTTP_STOP_TIMEOUT,
    MAX_POINTS,
    METRIC_KINDS,
    SNAPSHOT_POINTS,
    SSE_KEEPALIVE_SECONDS,
    SSE_QUEUE_SIZE,
    _DB_HISTORY_COLUMNS,
)
from .history import make_db_history_provider
from .hub import TelemetryHub
from .page import FALLBACK_INDEX, _FALLBACK_INDEX, compose_page, page_bytes
from .server import DashboardServer

#: Historical module-level logger (the legacy dashboard module exposed it).
logger = logging.getLogger("MiBandDashboard")

__all__ = [
    "DEFAULT_MAX_POINTS",
    "DEFAULT_SNAPSHOT_POINTS",
    "DashboardServer",
    "FALLBACK_INDEX",
    "HTTP_HEADER_TIMEOUT",
    "HTTP_READ_TIMEOUT",
    "HTTP_STOP_TIMEOUT",
    "MAX_POINTS",
    "METRIC_KINDS",
    "SNAPSHOT_POINTS",
    "SSE_KEEPALIVE_SECONDS",
    "SSE_QUEUE_SIZE",
    "TelemetryHub",
    "_DB_HISTORY_COLUMNS",
    "_FALLBACK_INDEX",
    "compose_page",
    "logger",
    "make_db_history_provider",
    "page_bytes",
    "utc_iso",
]
