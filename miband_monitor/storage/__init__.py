"""Timestamped telemetry persistence: JSONL/SQLite schema, writer thread and readers.

Public API re-exported for backward compatibility with the historical
storage module. New code may import from the focused submodules instead:

* miband_monitor.storage.schema -- table/column names, _SCHEMA, _INSERT_SQL
* miband_monitor.storage.store -- TelemetryStore (public sink API)
* miband_monitor.storage.writer -- background queue writer loop
* miband_monitor.storage.sqlite -- connection setup and row insertion
* miband_monitor.storage.readers -- read_jsonl, table_exists, utc_iso, ...
* miband_monitor.storage.export_csv -- SQLite -> CSV export CLI
"""

import logging

from . import writer as _writer
from .export_csv import (
    DEFAULT_DB,
    DEFAULT_OUTPUT,
    export_database,
    export_table,
    main as export_csv_main,
)
from .readers import (
    normalize_backend,
    read_jsonl,
    secure_path,
    table_exists,
    utc_iso,
)
from .schema import (
    _INSERT_SQL,
    _SCHEMA,
    ACTIVITY_HISTORY_COLUMNS,
    ACTIVITY_HISTORY_TABLE,
    DB_FILENAME,
    HEART_RATE_COLUMNS,
    HEART_RATE_TABLE,
    IMU_COLUMNS,
    IMU_TABLE,
    JSONL_FILENAMES,
    RECORD_KINDS,
    SPO2_COLUMNS,
    SPO2_SLEEP_COLUMNS,
    SPO2_SLEEP_TABLE,
    SPO2_TABLE,
    STEPS_COLUMNS,
    STEPS_TABLE,
    STORAGE_BACKENDS,
    TABLE_COLUMNS,
)
from .store import TelemetryStore

#: Historical module-level logger/private names kept for the legacy facade.
logger = logging.getLogger("MiBandStorage")
_SENTINEL = _writer._SENTINEL

__all__ = [
    "ACTIVITY_HISTORY_COLUMNS",
    "ACTIVITY_HISTORY_TABLE",
    "DB_FILENAME",
    "DEFAULT_DB",
    "DEFAULT_OUTPUT",
    "HEART_RATE_COLUMNS",
    "HEART_RATE_TABLE",
    "IMU_COLUMNS",
    "IMU_TABLE",
    "JSONL_FILENAMES",
    "RECORD_KINDS",
    "SPO2_COLUMNS",
    "SPO2_SLEEP_COLUMNS",
    "SPO2_SLEEP_TABLE",
    "SPO2_TABLE",
    "STEPS_COLUMNS",
    "STEPS_TABLE",
    "STORAGE_BACKENDS",
    "TABLE_COLUMNS",
    "TelemetryStore",
    "_INSERT_SQL",
    "_SCHEMA",
    "_SENTINEL",
    "export_csv_main",
    "logger",
    "export_database",
    "export_table",
    "normalize_backend",
    "read_jsonl",
    "secure_path",
    "table_exists",
    "utc_iso",
]
