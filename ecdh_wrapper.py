"""Backward-compatible facade for miband_monitor.crypto.

The native/pure-Python ECDH backends and the backend-selection logic now live in
miband_monitor.crypto. This module keeps the historical import path and the
module-level ctypes/subprocess/sys hooks used by the backend tests.
"""

from __future__ import annotations

from miband_monitor.crypto import *  # noqa: F401,F403
from miband_monitor.crypto import __all__ as _package_all
from miband_monitor.crypto import _BACKEND, _NATIVE_LIBRARY_NAMES, logger

__all__ = list(_package_all) + ["_BACKEND", "_NATIVE_LIBRARY_NAMES", "logger"]
