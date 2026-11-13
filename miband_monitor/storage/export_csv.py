"""Export Mi Band 6 telemetry from SQLite to CSV files.

Usage::

    python export_csv.py                       # data/mi_band.db -> data/csv/
    python export_csv.py --db data/mi_band.db --output data/csv

Column order comes from miband_monitor.storage.schema.TABLE_COLUMNS so the CSV
header can never drift from the database schema. Default paths are derived from
miband_monitor.config.paths instead of duplicating the literal "data" layout.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

from ..config.paths import default_csv_dir, default_data_dir, default_db_path
from ..config.security import secure_directory, secure_file
from .readers import table_exists
from .schema import TABLE_COLUMNS

#: Default database and output directory (kept relative to the working
#: directory, matching the historical command-line behaviour).
DEFAULT_DB = default_db_path()
DEFAULT_OUTPUT = default_csv_dir(default_data_dir())


def export_table(conn: sqlite3.Connection, table: str, output_path: Path) -> int:
    """Export one table to CSV; returns the number of written data rows."""
    columns = TABLE_COLUMNS[table]
    query = f"SELECT {', '.join(columns)} FROM {table} ORDER BY ts, id"
    secure_directory(output_path.parent)
    rows = 0
    with open(output_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in conn.execute(query):
            writer.writerow(row)
            rows += 1
    secure_file(output_path)
    return rows


def export_database(
    db_path: str | Path, output_dir: str | Path
) -> dict[str, tuple[Path, int]]:
    """Export every known table; returns {table: (csv_path, rows)}."""
    db_path = Path(db_path)
    output_dir = Path(output_dir)
    if not db_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")

    secure_directory(output_dir)

    result: dict[str, tuple[Path, int]] = {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for table in TABLE_COLUMNS:
            if not table_exists(conn, table):
                continue
            path = output_dir / f"{table}.csv"
            result[table] = (path, export_table(conn, table, path))
    finally:
        conn.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export Mi Band 6 SQLite telemetry to CSV.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="CSV output directory")
    args = parser.parse_args(argv)

    try:
        exported = export_database(args.db, args.output)
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f"[-] Export failed: {exc}", file=sys.stderr)
        return 1

    if not exported:
        print(f"[-] No telemetry tables found in {args.db}", file=sys.stderr)
        return 1

    print(f"[+] Exported {len(exported)} table(s) from {args.db}:")
    for table, (path, rows) in exported.items():
        print(f"    {table:<12} {rows:>8} rows -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DEFAULT_DB",
    "DEFAULT_OUTPUT",
    "TABLE_COLUMNS",
    "export_database",
    "export_table",
    "main",
]
