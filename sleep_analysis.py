"""Backward-compatible facade for miband_monitor.sleep.

The sleep-session detector, per-minute staging, scoring helpers and report now
live in the miband_monitor.sleep package. This module keeps the historical
import path (and the private helpers used by tests/tools) alive.
"""

from __future__ import annotations

from miband_monitor.sleep import *  # noqa: F401,F403
from miband_monitor.sleep import __all__ as _package_all
from miband_monitor.sleep.classification import _rank_within, _rolling_sd, _smooth
from miband_monitor.sleep.report import _format_report
from miband_monitor.sleep.session import _count_awakenings, _longest_sleep_run

__all__ = list(_package_all) + [
    "_format_report",
    "_rank_within",
    "_rolling_sd",
    "_smooth",
    "_count_awakenings",
    "_longest_sleep_run",
]


if __name__ == "__main__":
    raise SystemExit(main())
