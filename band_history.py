"""Backward-compatible facade for miband_monitor.history.

The fetch protocol, payload parsers and sync loop now live in the
miband_monitor.history package. This module keeps the historical import paths
alive for tools, tests and the simulator.
"""

from __future__ import annotations

from miband_monitor.history import *  # noqa: F401,F403
from miband_monitor.history import __all__ as _package_all

__all__ = list(_package_all)
