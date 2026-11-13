"""Backward-compatible entry point for the interactive auth-key extractor.

The flow now lives in miband_monitor.auth.extractor; this script keeps the
historical command name documented in the README.
"""

from __future__ import annotations

import sys

from miband_monitor.auth.extractor import (
    ENV_PATH,
    TARGET_MAC,
    _preferred_device_env_key,
    run_extractor,
)
from miband_monitor.config.env import update_env_file as _update_env_file

__all__ = [
    "ENV_PATH",
    "TARGET_MAC",
    "_preferred_device_env_key",
    "run_extractor",
    "update_env_file",
]


def update_env_file(key: str, value: str) -> None:
    """Historical two-argument helper (writes to this project's .env)."""
    _update_env_file(ENV_PATH, key, value)


if __name__ == "__main__":
    # Windows redirected output may use a legacy code page; replace instead of
    # raising for the Chinese guidance text.
    for _stream in (sys.stdout, sys.stderr):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if _reconfigure is not None:
            try:
                _reconfigure(errors="replace")
            except (ValueError, OSError):  # pragma: no cover - defensive
                pass
    sys.exit(run_extractor())
