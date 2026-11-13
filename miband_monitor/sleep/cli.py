"""Command-line entry point for the derived sleep analysis.

Usage is unchanged from the historical ``sleep_analysis.py`` CLI:
``--db`` is required, ``--json`` prints the structured payload (without the
internal session objects) and ``--night YYYY-MM-DD`` filters to sessions that
ended on a local date.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Optional, Sequence

from .detection import detect_sessions
from .report import _format_report
from .samples import local_date, load_samples
from .session import analyze


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Derive sleep sessions and stages from band activity samples."
    )
    parser.add_argument("--db", required=True, help="path to mi_band.db (SQLite)")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument(
        "--night",
        metavar="YYYY-MM-DD",
        help="only report sessions that ended on this local date",
    )
    args = parser.parse_args(argv)

    try:
        samples = load_samples(args.db, include_scores=True)
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f"[-] {exc}", file=sys.stderr)
        return 1

    if args.night:
        sessions = detect_sessions(samples)
        samples = [
            sample
            for session in sessions
            if local_date(session[-1]["ts"]) == args.night
            for sample in session
        ]
        # Re-run detection on the filtered samples so the session list matches.
        if not samples:
            print(f"no sleep session ending on {args.night}")
            return 1

    result = analyze(samples)
    if args.json:
        payload = {key: value for key, value in result.items() if key != "_sessions"}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_format_report(result))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
