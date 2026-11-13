"""Mi Band 6 BLE client: chunked-ECDH authentication + real-time telemetry.

Backward-compatible facade. The implementation now lives in:

- miband_monitor.ble: protocol codecs, parsers, identity, chunked transfer and
  the MiBand6Client (discovery/link/auth/streams mixins);
- miband_monitor.cli: argument parsing, logging, callbacks and the session
  supervisor (run_monitor / run_demo / main).

Pipeline
--------
1. Discover the band with a BLE scan, then keep one BlueZ Connect armed across a
   full advertising period (a Mi Band 6 without a phone link emits one isolated
   pulse every 40-60 s).
2. Authenticate with the Huami chunked-transfer protocol on characteristics
   0x0016 (write) / 0x0017 (notify): NIST B-163 ECDH + AES-CBC challenge/response.
3. Stream heart rate (standard BLE SIG service), real-time steps (0x0007) and
   optionally raw IMU accelerometer data (0x0001/0x0002).
4. Persist every sample through miband_monitor.storage, or just log it.

CLI::

    python miband_client.py 60 --imu
    python miband_client.py --demo 10 --imu
    python miband_client.py --export-csv
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from dotenv import load_dotenv

from miband_monitor import bluez as bluez_support
from miband_monitor.ble import *  # noqa: F401,F403
from miband_monitor.ble import (
    __all__ as _ble_all,
)
from miband_monitor.ble.client import BleakClient, BleakScanner
from miband_monitor.cli.app import main
from miband_monitor.cli.args import (
    _env_float,
    _env_optional_int,
    parse_args,
)
from miband_monitor.cli.logging import configure_logging as _configure_logging
from miband_monitor.cli.runner import (
    _sync_band_history,
    run_demo,
    run_monitor,
)
from miband_monitor.config.demo import (
    DEMO_AUTH_KEY_HEX as DEMO_DEFAULT_AUTH_KEY,
    DEMO_DEVICE_ID as DEMO_DEFAULT_MAC,
)
from miband_monitor.config.defaults import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_DIRECT_CONNECT_TIMEOUT as DIRECT_CONNECT_TIMEOUT,
)
from miband_monitor.crypto import backend_name as ecdh_backend_name
from miband_monitor.crypto import compute_shared_secret, generate_ecdh_keys
from miband_monitor.dashboard import (
    DashboardServer,
    TelemetryHub,
    make_db_history_provider,
)
from miband_monitor.history import summarize_history_result, sync_history
from miband_monitor.storage import STORAGE_BACKENDS, TelemetryStore
from miband_monitor.storage.export_csv import export_database

logger = logging.getLogger("MiBand6Client")

__all__ = list(_ble_all) + [
    "BleakClient",
    "BleakScanner",
    "DEFAULT_CONNECT_TIMEOUT",
    "DIRECT_CONNECT_TIMEOUT",
    "DEMO_DEFAULT_MAC",
    "DEMO_DEFAULT_AUTH_KEY",
    "DashboardServer",
    "TelemetryHub",
    "STORAGE_BACKENDS",
    "TelemetryStore",
    "_configure_logging",
    "_env_float",
    "_env_optional_int",
    "_sync_band_history",
    "bluez_support",
    "ecdh_backend_name",
    "export_database",
    "generate_ecdh_keys",
    "compute_shared_secret",
    "logger",
    "main",
    "make_db_history_provider",
    "parse_args",
    "run_demo",
    "run_monitor",
    "summarize_history_result",
    "sync_history",
]


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        sys.exit(130)
