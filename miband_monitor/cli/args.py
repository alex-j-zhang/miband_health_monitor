"""Argument parser for miband_client.py.

The parser is kept byte-for-byte compatible with the pre-refactor CLI: same
flags, metavars, defaults, help text, validation errors and exit codes.
"""

from __future__ import annotations

import argparse
import os
from typing import Optional

from ..ble.identity import _env_device_id
from ..config.defaults import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_DATA_DIR,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_LOG_LEVEL,
    DEFAULT_SCAN_TIMEOUT,
    DEFAULT_WEB_HOST,
    ENV_AUTH_KEY,
    ENV_BAND_TZ,
    ENV_BLE_MTU,
    ENV_CONNECT_TIMEOUT,
    ENV_DEMO_MODE,
    ENV_DEVICE_NAME,
    ENV_HISTORY_DAYS,
    ENV_LOG_LEVEL,
    ENV_OUTPUT_DIR,
    ENV_SCAN_TIMEOUT,
    ENV_STORAGE_BACKEND,
    ENV_WEB_HOST,
    ENV_WEB_PORT,
)
from ..config.env import env_float, env_optional_int, load_env, secure_env_file
from ..storage.schema import STORAGE_BACKENDS


def _env_float(name: str, default: float) -> float:
    """Historical private helper kept for callers/tests."""
    return env_float(name, default)


def _env_optional_int(name: str) -> Optional[int]:
    """Historical private helper kept for callers/tests."""
    return env_optional_int(name)


def _secure_env_file() -> None:
    """Tighten the working-directory .env file (historical private helper)."""
    secure_env_file()


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser without touching environment paths."""
    parser = argparse.ArgumentParser(
        description="Stream Mi Band 6 heart rate, steps and IMU data to your computer."
    )
    parser.add_argument(
        "duration",
        nargs="?",
        type=int,
        default=None,
        help="collection duration in seconds (default: until Ctrl+C)",
    )
    parser.add_argument("--imu", action="store_true", help="also stream raw IMU accelerometer data")
    parser.add_argument(
        "--device-id",
        default=_env_device_id(),
        metavar="ID",
        help=(
            "Bluetooth MAC address (Linux/Windows) or CoreBluetooth UUID (macOS) "
            "of the band; defaults to DEVICE_ID / legacy DEVICE_MAC from .env"
        ),
    )
    parser.add_argument(
        "--device-name",
        default=os.getenv(ENV_DEVICE_NAME, ""),
        metavar="NAME",
        help=(
            "optional advertised-name substring used as an extra discovery match "
            "(useful on macOS where no MAC address is exposed; default: DEVICE_NAME env)"
        ),
    )
    parser.add_argument(
        "--mtu",
        type=int,
        default=env_optional_int(ENV_BLE_MTU),
        metavar="BYTES",
        help=(
            "override the ATT MTU used to size chunked auth writes "
            "(default: auto-detect, floor 23; env: BLE_MTU)"
        ),
    )
    parser.add_argument(
        "--sync-history",
        nargs="?",
        const=DEFAULT_HISTORY_DAYS,
        type=int,
        default=env_optional_int(ENV_HISTORY_DAYS),
        metavar="DAYS",
        help=(
            "fetch up to DAYS days of band-side history (activity + SpO2) after "
            "connecting, experimental (default: off, or HISTORY_DAYS env)"
        ),
    )
    parser.add_argument(
        "--sync-sleep-spo2",
        action="store_true",
        help=(
            "also fetch experimental sleep SpO2 (type 0x26) for payload capture; "
            "requires --sync-history; 30B layout verified, trailing fields unknown, "
            "samples are written to spo2_sleep.jsonl only"
        ),
    )
    parser.add_argument(
        "--band-tz",
        default=os.getenv(ENV_BAND_TZ, ""),
        metavar="TZ",
        help=(
            "band local timezone for history fetch/timestamps, e.g. Asia/Shanghai "
            "or +08:00 (default: host local timezone, or BAND_TZ env)"
        ),
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="run against the in-memory simulated band (no Bluetooth hardware)",
    )
    parser.add_argument(
        "--output",
        default=os.getenv(ENV_OUTPUT_DIR, DEFAULT_DATA_DIR),
        help="directory for JSONL files and mi_band.db (default: data)",
    )
    parser.add_argument(
        "--storage",
        choices=list(STORAGE_BACKENDS),
        default=os.getenv(ENV_STORAGE_BACKEND, "both").lower(),
        help="storage backend (default: both)",
    )
    parser.add_argument(
        "--export-csv",
        action="store_true",
        help="export the SQLite database to CSV files after the session",
    )
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=env_float(ENV_SCAN_TIMEOUT, DEFAULT_SCAN_TIMEOUT),
        help="seconds to wait for the band to advertise (default: 90)",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=env_float(ENV_CONNECT_TIMEOUT, DEFAULT_CONNECT_TIMEOUT),
        help=(
            "seconds allowed for BLE link establishment and service resolution "
            "(default: 75; on Linux it is raised to at least one full 70 s "
            "advertising period of a sleeping Mi Band 6)"
        ),
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv(ENV_LOG_LEVEL, DEFAULT_LOG_LEVEL),
        help="DEBUG | INFO | WARNING | ERROR (default: INFO)",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=env_optional_int(ENV_WEB_PORT),
        help="serve the live web dashboard on this port (0 = random free port; default: off)",
    )
    parser.add_argument(
        "--web-host",
        default=os.getenv(ENV_WEB_HOST, DEFAULT_WEB_HOST),
        help="dashboard bind address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--no-reconnect",
        action="store_true",
        help="do not automatically reconnect after errors or link loss",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="run an environment self-check and exit (never connects to the band)",
    )
    parser.add_argument(
        "--no-direct-connect",
        action="store_true",
        help=(
            "always scan for the band instead of using the bonded BlueZ device "
            "(Linux only; direct connect is the default when the band is paired)"
        ),
    )
    return parser


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Load .env, tighten its permissions and parse the command line."""
    load_env()
    _secure_env_file()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.sync_sleep_spo2 and args.sync_history is None:
        parser.error("--sync-sleep-spo2 requires --sync-history [DAYS]")
    if args.storage not in STORAGE_BACKENDS:
        args.storage = "both"
    return args


def demo_requested(args: argparse.Namespace) -> bool:
    """Return True when demo mode is enabled by flag or DEMO_MODE env."""
    return bool(args.demo or os.getenv(ENV_DEMO_MODE, "0").lower() in ("1", "true", "yes", "on"))
