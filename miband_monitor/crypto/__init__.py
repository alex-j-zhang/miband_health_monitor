"""ECDH backends for the Mi Band 6 authentication handshake.

Two interchangeable implementations produce the exact 24/48/48-byte
little-endian format the band expects:

* native - the vendored tiny-ECDH C library through ctypes (fastest, compiled
  on demand into .cache/ecdh when no prebuilt library exists);
* pure Python - the vendored tiny_ecdh implementation used wherever a native
  library cannot be loaded (Windows, macOS, ARM, CI without a compiler).

Backend choice is overridable with MIBAND_ECDH_BACKEND=auto|native|python and
MIBAND_ECDH_LIB=<path>, exactly as before the package split.
"""

from __future__ import annotations

from .constants import (
    BUILD_DIR,
    C_SOURCE_PATH,
    ENV_BACKEND,
    ENV_LIBRARY,
    KEY_GENERATION_ATTEMPTS,
    NATIVE_LIBRARY_NAMES,
    PRIVATE_KEY_SIZE,
    PROJECT_ROOT,
    PUBLIC_KEY_SIZE,
    SHARED_SECRET_SIZE,
    _NATIVE_LIBRARY_NAMES,
    logger,
)
from .facade import (
    _BACKEND,
    backend_name,
    compute_shared_secret,
    generate_ecdh_keys,
    select_backend,
)
from .facade import ctypes, subprocess, sys  # legacy module aliases (see facade.py)
from .native import (
    NativeEcdhBackend,
    _compiler_commands,
    _configure_library,
    compiled_library_path,
    load_native_library,
    native_library_candidates,
)
from .pure_python import PurePythonEcdhBackend

__all__ = [
    # constants
    "PROJECT_ROOT",
    "C_SOURCE_PATH",
    "BUILD_DIR",
    "PRIVATE_KEY_SIZE",
    "PUBLIC_KEY_SIZE",
    "SHARED_SECRET_SIZE",
    "KEY_GENERATION_ATTEMPTS",
    "NATIVE_LIBRARY_NAMES",
    "ENV_BACKEND",
    "ENV_LIBRARY",
    "logger",
    # native backend
    "native_library_candidates",
    "compiled_library_path",
    "load_native_library",
    "NativeEcdhBackend",
    "_compiler_commands",
    "_configure_library",
    # pure-python backend
    "PurePythonEcdhBackend",
    # facade
    "select_backend",
    "backend_name",
    "generate_ecdh_keys",
    "compute_shared_secret",
    # legacy test hooks (same objects as the old module-level imports)
    "ctypes",
    "subprocess",
    "sys",
]
