"""Backward-compatible facade for miband_monitor.offline_dashboard.

The offline single-file historical dashboard now lives in the
miband_monitor.offline_dashboard package (sources, packaging, KPIs, SVG,
collect, render, CLI). This module keeps the historical import path and the
command line: python history_dashboard.py --db data/mi_band.db
"""

from __future__ import annotations

import sys

from miband_monitor.offline_dashboard import *  # noqa: F401,F403
from miband_monitor.offline_dashboard import __all__ as _package_all
from miband_monitor.offline_dashboard import build, collect, main

__all__ = list(_package_all) + ["build", "collect", "main"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
