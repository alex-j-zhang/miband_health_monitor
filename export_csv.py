"""Backward-compatible facade for miband_monitor.storage.export_csv.

The SQLite-to-CSV exporter now lives in the storage package so its column order
is derived from the same schema used by the writer. This module keeps the
historical command line and import path alive.
"""

from __future__ import annotations

import sys

from miband_monitor.storage.export_csv import *  # noqa: F401,F403
from miband_monitor.storage.export_csv import (
    DEFAULT_DB,
    DEFAULT_OUTPUT,
    TABLE_COLUMNS,
    export_database,
    export_table,
    main,
)

__all__ = [
    "DEFAULT_DB",
    "DEFAULT_OUTPUT",
    "TABLE_COLUMNS",
    "export_database",
    "export_table",
    "main",
]

if __name__ == "__main__":
    sys.exit(main())
