"""Backward-compatible facade for miband_monitor.storage.

The implementation now lives in the miband_monitor.storage package. This module
keeps the historical import paths alive for the CLI scripts, tests and
third-party snippets that do: from storage import TelemetryStore.
"""

from __future__ import annotations

from miband_monitor.storage import *  # noqa: F401,F403
from miband_monitor.storage import __all__ as _package_all
from miband_monitor.storage.writer import _SENTINEL, logger

__all__ = list(_package_all) + ["_SENTINEL", "logger"]
