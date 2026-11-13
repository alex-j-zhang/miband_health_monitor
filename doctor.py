"""Backward-compatible facade for miband_monitor.diagnostics.doctor.

The read-only environment self-check now lives in the diagnostics package. This
module keeps the historical import path, the report helpers and the standalone
command line: python doctor.py
"""

from __future__ import annotations

import asyncio
import os
import sys

from miband_monitor.config.defaults import DEFAULT_DATA_DIR, ENV_AUTH_KEY, ENV_OUTPUT_DIR
from miband_monitor.config.env import load_env
from miband_monitor.diagnostics import (
    FAIL,
    INFO,
    OK,
    WARN,
    _MARK,
    _PREFIX,
    _Report,
    _bleak_version,
    _env_mode_hint,
    _mask,
    run_doctor,
)
from miband_monitor.bluez import find_device, is_bluez_platform, list_adapters
from miband_monitor.diagnostics.doctor import _auth_helpers, _check_bluez

__all__ = [
    "OK",
    "INFO",
    "WARN",
    "FAIL",
    "_PREFIX",
    "_MARK",
    "_Report",
    "_bleak_version",
    "_env_mode_hint",
    "_mask",
    "_auth_helpers",
    "_check_bluez",
    "find_device",
    "is_bluez_platform",
    "list_adapters",
    "run_doctor",
]


if __name__ == "__main__":
    load_env()
    from miband_monitor.ble import _env_device_id

    _dev_id = _env_device_id()
    _auth = (os.getenv(ENV_AUTH_KEY) or "").strip()
    _out = (os.getenv(ENV_OUTPUT_DIR) or DEFAULT_DATA_DIR).strip()
    sys.exit(asyncio.run(run_doctor(_dev_id, _auth, _out)))
