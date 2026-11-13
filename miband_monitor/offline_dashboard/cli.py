"""Command-line entry point for the single-file offline dashboard.

``build()`` collects the payload, renders the HTML and writes it with private
POSIX permissions; ``main()`` is the historical CLI (same flags, same output).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Optional, Sequence

from miband_monitor.config.defaults import DEFAULT_OFFLINE_DAYS
from miband_monitor.config.security import secure_directory, secure_file

from .collect import collect
from .render import render
from .strings import CLI_ERROR, CLI_SUMMARY, CLI_WROTE


def build(
    db_path: str | Path,
    output: str | Path,
    *,
    days: int = DEFAULT_OFFLINE_DAYS,
    spo2_sleep_path: str | Path | None = None,
) -> Path:
    payload = collect(db_path, days=days, spo2_sleep_path=spo2_sleep_path)
    html = render(payload)
    target = Path(output)
    # ``--output data/reports/dash.html`` is a natural thing to type; don't make
    # the user mkdir first.
    secure_directory(target.parent)
    target.write_text(html, encoding="utf-8")
    secure_file(target)
    return target


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a self-contained historical health dashboard (HTML)."
    )
    parser.add_argument("--db", required=True, help="path to mi_band.db")
    parser.add_argument("-o", "--output", default="history_dashboard.html", help="output HTML file")
    parser.add_argument(
        "--days", type=int, default=DEFAULT_OFFLINE_DAYS,
        help=f"window in days (default {DEFAULT_OFFLINE_DAYS})",
    )
    parser.add_argument(
        "--spo2-sleep-jsonl",
        default=None,
        help=(
            "explicit path to spo2_sleep.jsonl; by default the file next to "
            "--db is used, and the dedicated spo2_sleep SQLite table wins"
        ),
    )
    args = parser.parse_args(argv)

    try:
        target = build(
            args.db, args.output, days=args.days,
            spo2_sleep_path=args.spo2_sleep_jsonl,
        )
        payload = collect(
            args.db, days=args.days, spo2_sleep_path=args.spo2_sleep_jsonl
        )
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(CLI_ERROR.format(message=exc), file=sys.stderr)
        return 1

    print(CLI_WROTE.format(target=target, size=target.stat().st_size // 1024))
    print(
        CLI_SUMMARY.format(
            activity=payload["meta"]["activity_samples"],
            sessions=len(payload["sleep"]["sessions"]),
            spo2=payload["meta"]["spo2_samples"],
            spo2_sleep=payload["meta"]["spo2_sleep_samples"],
            source=payload["meta"]["spo2_sleep_source"],
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
