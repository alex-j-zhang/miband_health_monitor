"""Command-line entry point for the sleep-byte calibration harness.

The candidate-reading logic, night lookup and the report live in
:mod:`miband_monitor.sleep.calibration`; this script keeps the historical
``tools/calibrate_sleep.py`` invocation and re-exports the same names so tests
and ad-hoc imports keep working.

Usage:

    ./.venv/Scripts/python.exe tools/calibrate_sleep.py --zepp data/zepp_nights.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from miband_monitor.sleep import calibration  # noqa: E402
from miband_monitor.sleep.calibration import (  # noqa: E402,F401
    GB_DEEP_MIN,
    GB_REM_MIN,
    MODELS,
    _best_deep_cutoff,
    _clock,
    _local_date,
    _overlap_minutes,
    _scored,
    _window_bounds,
    find_session,
    gb_thresholds,
    heuristic,
    integral,
    nonwear_minutes,
    report,
)

__all__ = [
    "GB_REM_MIN",
    "GB_DEEP_MIN",
    "MODELS",
    "_best_deep_cutoff",
    "_clock",
    "_local_date",
    "_overlap_minutes",
    "_scored",
    "_window_bounds",
    "find_session",
    "gb_thresholds",
    "heuristic",
    "integral",
    "nonwear_minutes",
    "report",
    "main",
]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=calibration.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default="data/final-check/mi_band.db")
    parser.add_argument(
        "--zepp",
        default="data/zepp_nights.json",
        help="JSON list of nights as reported by the app",
    )
    args = parser.parse_args(argv)

    nights = json.loads(Path(args.zepp).read_text(encoding="utf-8"))
    if isinstance(nights, dict):
        nights = nights["nights"]
    return report(args.db, nights)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
