"""Backward-compatible facade for miband_monitor.reports.history.

The local telemetry summary now lives in the reports package. This module keeps
the historical command line and import path alive.
"""

from __future__ import annotations

import sys

from miband_monitor.reports.history import (
    DEFAULT_DB,
    _scalar,
    _table_exists,
    format_report,
    main,
    summarize,
)

__all__ = [
    "DEFAULT_DB",
    "_scalar",
    "_table_exists",
    "format_report",
    "summarize",
    "main",
]

if __name__ == "__main__":
    sys.exit(main())
