"""Filesystem permission helpers.

The project stores credentials and personal health telemetry locally. On POSIX
those files must not be world-readable, so every writer routes through the
helpers below instead of calling chmod ad hoc.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

#: Directories holding telemetry or credentials.
DIRECTORY_MODE = 0o700
#: JSONL/SQLite/HTML files holding personal data.
FILE_MODE = 0o600


def secure_path(path: Path, is_dir: bool = False) -> None:
    """Ensure private permissions (0700 dirs / 0600 files) on POSIX."""
    if os.name == "nt":
        return
    with contextlib.suppress(OSError):
        path.chmod(DIRECTORY_MODE if is_dir else FILE_MODE)


def secure_directory(path: Path) -> None:
    """Create path if needed and tighten it to 0700."""
    path.mkdir(parents=True, exist_ok=True)
    secure_path(path, is_dir=True)


def secure_file(path: Path) -> None:
    """Tighten an existing file to 0600 (no-op when absent)."""
    if path.exists():
        secure_path(path, is_dir=False)
