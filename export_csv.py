"""Export Mi Band 6 telemetry from SQLite to CSV files.

Usage::

    python export_csv.py                       # data/mi_band.db -> data/csv/
    python export_csv.py --db data/mi_band.db --output data/csv
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

# Column order of the exported CSV files (must match storage.py schema).
TABLE_COLUMNS = {
    "heart_rate": ["id", "ts", "iso_time", "bpm"],
    "steps": ["id", "ts", "iso_time", "steps", "distance_m", "calories"],
    "imu": [
        "id",
        "ts",
        "iso_time",
        "sensor_type",
        "sequence",
        "sample_index",
        "x_raw",
        "y_raw",
        "z_raw",
        "ax_g",
        "ay_g",
        "az_g",
        "ax_ms2",
        "ay_ms2",
        "az_ms2",
    ],
}

DEFAULT_DB = Path("data") / "mi_band.db"
DEFAULT_OUTPUT = Path("data") / "csv"


def export_table(conn: sqlite3.Connection, table: str, output_path: Path) -> int:
    """Export one table to CSV; returns the number of written data rows."""
    columns = TABLE_COLUMNS[table]
    query = f"SELECT {', '.join(columns)} FROM {table} ORDER BY ts, id"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    with open(output_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in conn.execute(query):
            writer.writerow(row)
            rows += 1
    return rows


def export_database(db_path: str | Path, output_dir: str | Path) -> dict[str, tuple[Path, int]]:
    """Export every known table; returns ``{table: (csv_path, rows)}``."""
    db_path = Path(db_path)
    output_dir = Path(output_dir)
    if not db_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")

    result: dict[str, tuple[Path, int]] = {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        existing = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for table, columns in TABLE_COLUMNS.items():
            if table not in existing:
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
