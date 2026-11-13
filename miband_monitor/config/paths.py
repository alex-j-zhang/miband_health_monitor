"""Filesystem locations used by the project.

Only locations live here; creating directories and tightening permissions is
the job of miband_monitor.config.security. Runtime output paths stay relative
to the current working directory (the historical behaviour), while source and
asset paths are anchored to the installed package.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from .defaults import DEFAULT_CSV_DIRNAME, DEFAULT_DATA_DIR, DEFAULT_DB_FILENAME

#: .../miband_monitor package directory.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
#: Repository / checkout root that contains miband_monitor/.
PROJECT_ROOT = PACKAGE_ROOT.parent

PathLike = Union[str, Path]


def package_path(*parts: str) -> Path:
    """Return a path inside the installed package (assets, templates, data)."""
    return PACKAGE_ROOT.joinpath(*parts)


def project_path(*parts: str) -> Path:
    """Return a path relative to the project/checkout root."""
    return PROJECT_ROOT.joinpath(*parts)


def default_data_dir() -> Path:
    """Default telemetry directory (relative, for backwards compatibility)."""
    return Path(DEFAULT_DATA_DIR)


def default_db_path() -> Path:
    """Default SQLite database location (data/mi_band.db)."""
    return Path(DEFAULT_DATA_DIR) / DEFAULT_DB_FILENAME


def default_csv_dir(output_dir: PathLike) -> Path:
    """CSV sub-directory used by --export-csv and export_csv.py."""
    return Path(output_dir) / DEFAULT_CSV_DIRNAME


def default_env_path() -> Path:
    """Project-local .env used by the auth-key helper scripts."""
    return PROJECT_ROOT / ".env"


def ecdh_c_source() -> Path:
    """C source of the vendored tiny-ECDH library."""
    return PROJECT_ROOT / "ecdh.c"


def ecdh_build_dir() -> Path:
    """Git-ignored cache directory for a locally compiled native library."""
    return PROJECT_ROOT / ".cache" / "ecdh"
