"""Native tiny-ECDH backend loaded through ctypes.

The C library (ecdh.c) is the fastest implementation and is used automatically
on Linux whenever a usable libecdh.so exists or can be compiled with gcc/clang.
Every failure path is a no-op: when nothing loads, load_native_library returns
None so callers fall back to the vendored pure-Python implementation.  Nothing
here raises on a broken native library, matching the historical behaviour of
ecdh_wrapper.py.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from .constants import (
    BUILD_DIR,
    C_SOURCE_PATH,
    ENV_LIBRARY,
    KEY_GENERATION_ATTEMPTS,
    NATIVE_LIBRARY_NAMES,
    PRIVATE_KEY_SIZE,
    PROJECT_ROOT,
    PUBLIC_KEY_SIZE,
    SHARED_SECRET_SIZE,
    logger,
)


# ---------------------------------------------------------------------------
# Native library discovery / compilation
# ---------------------------------------------------------------------------
def native_library_candidates() -> List[Path]:
    """Return the native library paths that would be tried on this platform.

    Project-root paths come first (users may drop a prebuilt DLL there), then
    the git-ignored build cache where an auto-compiled library is stored.
    MIBAND_ECDH_LIB is inserted in front of everything else when set.
    """
    names = NATIVE_LIBRARY_NAMES.get(sys.platform, ("libecdh.so",))
    candidates = [PROJECT_ROOT / name for name in names]
    candidates.extend(BUILD_DIR / name for name in names)

    explicit = (os.getenv(ENV_LIBRARY) or "").strip()
    if explicit:
        candidates.insert(0, Path(explicit).expanduser())
    return candidates


def compiled_library_path() -> Path:
    """Return the build-cache path used for a locally compiled native library."""
    names = NATIVE_LIBRARY_NAMES.get(sys.platform, ("libecdh.so",))
    return BUILD_DIR / names[0]


def _compiler_commands(output: Path) -> List[List[str]]:
    """Return best-effort compiler invocations for the current platform."""
    source = str(C_SOURCE_PATH)
    target = str(output)
    if sys.platform == "win32":
        return [
            ["gcc", "-O3", "-shared", "-DECDH_BUILD_DLL", source, "-o", target],
            ["clang", "-O3", "-shared", "-DECDH_BUILD_DLL", source, "-o", target],
        ]
    if sys.platform == "darwin":
        return [
            ["clang", "-O3", "-dynamiclib", source, "-o", target],
            ["gcc", "-O3", "-dynamiclib", source, "-o", target],
        ]
    return [
        ["gcc", "-O3", "-fPIC", "-shared", source, "-o", target],
        ["clang", "-O3", "-fPIC", "-shared", source, "-o", target],
    ]


def _configure_library(lib: ctypes.CDLL) -> ctypes.CDLL:
    lib.ecdh_generate_keys.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    lib.ecdh_generate_keys.restype = ctypes.c_int
    lib.ecdh_shared_secret.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
    lib.ecdh_shared_secret.restype = ctypes.c_int
    return lib


def load_native_library() -> Optional[ctypes.CDLL]:
    """Best-effort load of the native tiny-ECDH library.

    Tries an explicit MIBAND_ECDH_LIB path, the platform's usual file names and
    finally a one-shot compilation from ecdh.c.  Returns None (never raises)
    when nothing usable is found, so callers can fall back to Python.
    """
    for path in native_library_candidates():
        if not path.exists():
            continue
        try:
            return _configure_library(ctypes.CDLL(str(path)))
        except OSError as exc:
            # e.g. the committed x86-64 libecdh.so on ARM/Windows: keep looking
            # (and eventually try to compile a correct-architecture one).
            logger.debug("Cannot load native ECDH library %s: %s", path, exc)

    if not C_SOURCE_PATH.exists():
        logger.debug("No native ECDH library and no %s to compile.", C_SOURCE_PATH.name)
        return None

    output = compiled_library_path()
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - defensive (read-only checkout)
        logger.debug("Cannot create ECDH build directory %s: %s", output.parent, exc)
        return None

    for command in _compiler_commands(output):
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("ECDH compiler %r not available: %s", command[0], exc)
            continue
        if proc.returncode != 0 or not output.exists():
            logger.debug(
                "ECDH compilation with %r failed (rc=%s): %s",
                command[0],
                proc.returncode,
                (proc.stderr or "").strip()[:500],
            )
            continue
        try:
            return _configure_library(ctypes.CDLL(str(output)))
        except OSError as exc:
            logger.debug("Compiled ECDH library %s cannot be loaded: %s", output, exc)
    return None


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------
class NativeEcdhBackend:
    """ECDH backed by the tiny-ECDH C shared library via ctypes."""

    name = "native"

    def __init__(self, lib: Optional[ctypes.CDLL] = None) -> None:
        self._lib = lib if lib is not None else load_native_library()
        if self._lib is None:
            raise RuntimeError("native tiny-ECDH library is not available")

    def generate_keys(self) -> Tuple[bytes, bytes]:
        """Generate a 24-byte private key and 48-byte public key (band format)."""
        for _ in range(KEY_GENERATION_ATTEMPTS):
            prv = bytearray(os.urandom(PRIVATE_KEY_SIZE))
            pub = bytearray(PUBLIC_KEY_SIZE)
            result = self._lib.ecdh_generate_keys(
                (ctypes.c_char * PUBLIC_KEY_SIZE).from_buffer(pub),
                (ctypes.c_char * PRIVATE_KEY_SIZE).from_buffer(prv),
            )
            if result == 1:
                return bytes(prv), bytes(pub)
        raise RuntimeError("ECDH key generation failed after repeated attempts")

    def compute_shared_secret(self, private_key: bytes, peer_public_key: bytes) -> bytes:
        """Compute the 48-byte shared point, raising on an invalid peer point."""
        if len(private_key) != PRIVATE_KEY_SIZE:
            raise ValueError(
                f"Private key must be {PRIVATE_KEY_SIZE} bytes, got {len(private_key)}"
            )
        if len(peer_public_key) != PUBLIC_KEY_SIZE:
            raise ValueError(
                f"Peer public key must be {PUBLIC_KEY_SIZE} bytes, got {len(peer_public_key)}"
            )

        secret = bytearray(SHARED_SECRET_SIZE)
        result = self._lib.ecdh_shared_secret(
            (ctypes.c_char * PRIVATE_KEY_SIZE).from_buffer(bytearray(private_key)),
            (ctypes.c_char * PUBLIC_KEY_SIZE).from_buffer(bytearray(peer_public_key)),
            (ctypes.c_char * SHARED_SECRET_SIZE).from_buffer(secret),
        )
        if result != 1:
            raise ValueError("Peer public key is not a valid point on the NIST B-163 curve")
        return bytes(secret)
