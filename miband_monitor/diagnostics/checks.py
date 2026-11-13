"""Small pure helpers used by individual doctor checks.

Nothing here touches the band: they only read the installed bleak version, the
permissions of the local .env file and format identifiers for display.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

try:  # Python 3.8+
    from importlib.metadata import PackageNotFoundError, version as _pkg_version
except Exception:  # pragma: no cover - very old Python
    _pkg_version = None  # type: ignore[assignment]

    class PackageNotFoundError(Exception):  # type: ignore[no-redef]
        pass


def _bleak_version() -> str:
    if _pkg_version is None:
        return "unknown"
    try:
        return _pkg_version("bleak")
    except PackageNotFoundError:
        return "not installed"
    except Exception:  # pragma: no cover - defensive
        return "unknown"


def _env_mode_hint() -> Optional[str]:
    """Warn when .env is readable by other local users."""
    if os.name == "nt":
        return None
    env_path = _env_path()
    if not env_path.exists():
        return None
    try:
        mode = env_path.stat().st_mode & 0o777
    except OSError:
        return None
    if mode & 0o077:
        return f"chmod 600 {env_path.name}"
    return None


def _env_path() -> Path:
    """Project-local .env shown by the permission check."""
    from miband_monitor.config.paths import default_env_path

    return default_env_path()


def _mask(identifier: str) -> str:
    """Show only the last two octets of a MAC / last 4 chars of a UUID."""
    text = identifier.strip()
    if ":" in text and len(text) == 17:
        return "XX:XX:XX:XX:" + text[-5:]
    return text[-4:].rjust(len(text), "*") if text else ""
