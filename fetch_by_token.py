"""Backward-compatible entry point for the Zepp app_token device fetcher.

The request flow and region endpoints now live in miband_monitor.auth; this
script keeps the historical command name documented in the README.
"""

from __future__ import annotations

import sys

from miband_monitor.auth.constants import ENV_PATH, TARGET_MAC, ZEPP_WEB_ENDPOINTS
from miband_monitor.auth.token import fetch_devices, main
from miband_monitor.config.env import update_env_file as _update_env_file

#: Historical alias used by older snippets.
ENDPOINTS = list(ZEPP_WEB_ENDPOINTS)

__all__ = [
    "ENDPOINTS",
    "ENV_PATH",
    "TARGET_MAC",
    "fetch_devices",
    "update_env",
    "main",
]


def update_env(key: str, value: str) -> None:
    """Historical two-argument helper writing to the project .env."""
    _update_env_file(ENV_PATH, key, value)


if __name__ == "__main__":
    sys.exit(main())
