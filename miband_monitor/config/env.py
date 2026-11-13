"""Environment / .env helpers shared by every entry point.

Keeping these helpers in one module guarantees that all commands parse booleans,
integers and floats the same way and that the credentials file is tightened to
0600 even when it was created by another tool.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from .defaults import DEFAULT_LOG_LEVEL

logger = logging.getLogger("MiBandConfig")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def load_env(path: Optional[Path] = None) -> None:
    """Load .env values into os.environ (best effort, never raises)."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv is a runtime dependency
        return
    target = Path(path) if path is not None else None
    try:
        if target is not None:
            load_dotenv(target)
        else:
            load_dotenv()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not load .env: %s", exc)


def secure_env_file(path: Optional[Path] = None) -> Optional[Path]:
    """Tighten a credentials file to 0600 and return it.

    Defaults to .env in the current working directory, matching the historical
    behaviour of miband_client.py. Non-POSIX platforms are a no-op because they
    do not expose POSIX permission bits this way.
    """
    if os.name == "nt":
        return None
    target = Path(path) if path is not None else Path(".env")
    if not target.is_file():
        return None
    try:
        mode = target.stat().st_mode & 0o777
        if mode & 0o077:
            target.chmod(0o600)
            logger.debug("Tightened %s permissions to 0600", target)
    except OSError:  # pragma: no cover - exotic filesystems
        pass
    return target


def env_str(name: str, default: str = "") -> str:
    """Return a stripped string from the environment."""
    return (os.getenv(name) or default or "").strip()


def env_float(name: str, default: float) -> float:
    """Read a float from the environment, falling back safely on bad values."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return float(default)
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid %s=%r from environment", name, raw)
        return float(default)


def env_int(name: str, default: int) -> int:
    """Read an integer from the environment, falling back safely."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return int(default)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid %s=%r from environment", name, raw)
        return int(default)


def env_optional_int(name: str, default: Optional[int] = None) -> Optional[int]:
    """Read an optional integer from the environment (None when unset)."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        logger.warning("Ignoring invalid %s=%r from environment", name, raw)
        return default


def env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean environment flag (1/true/yes/on are true)."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    value = str(raw).strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    logger.warning("Ignoring invalid boolean %s=%r from environment", name, raw)
    return default


def read_env_file(path: Path) -> list[str]:
    """Return the lines of an .env file, or an empty list when absent."""
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def update_env_file(path: Path, key: str, value: str) -> None:
    """Update/add one KEY=value line and keep the file 0600.

    The implementation deliberately mirrors the previous helpers in
    get_auth_key.py / fetch_by_token.py: existing lines are replaced in place, a
    missing key is appended, and nothing else is reformatted.
    """
    lines = []
    if path.exists():
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()

    found = False
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}\n")

    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(new_lines)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - exotic filesystems
        pass


def logging_level() -> str:
    """Return the configured log level name (defaults to INFO)."""
    return env_str("LOG_LEVEL", DEFAULT_LOG_LEVEL).upper() or DEFAULT_LOG_LEVEL
