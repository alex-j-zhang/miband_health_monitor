"""Backward-compatible facade for miband_monitor.dashboard.

The hub, HTTP/SSE server, SQLite history bridge and page assets now live in the
miband_monitor.dashboard package. This module keeps the historical import path
for the CLI, tests and any external tooling.
"""

from __future__ import annotations

from miband_monitor.dashboard import *  # noqa: F401,F403
from miband_monitor.dashboard import __all__ as _package_all
from miband_monitor.dashboard.server import logger

__all__ = list(_package_all) + ["logger"]
