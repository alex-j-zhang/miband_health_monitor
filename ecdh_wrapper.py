"""ECDH cryptographic wrapper for the Mi Band 6 authentication handshake.

The band's "chunked transfer" authentication uses ECDH over the NIST B-163
(sect163r2) curve.  Two interchangeable backends implement it:

* **native** – the tiny-ECDH C library (``ecdh.c``) loaded through :mod:`ctypes`.
  This is the fastest option and is used automatically on Linux whenever a
  usable ``libecdh.so`` exists (or can be compiled with ``gcc``/``clang``).
* **pure Python** – the vendored MIT-licensed ``tiny_ecdh`` implementation in
  ``vendor/tiny_ecdh``.  This is the fallback everywhere the native library is
  unavailable (Windows, macOS, ARM Linux, CI containers without a compiler).

Both backends speak the *same byte format* on the wire, which is what the band
expects: a 24-byte little-endian private key, a 48-byte little-endian public key
(``x || y``, 24 bytes each) and a 48-byte little-endian shared point
(``x || y``).  ``tests/test_ecdh_backends.py`` cross-checks the two backends
byte-for-byte on Linux.

Backend selection can be overridden for diagnostics with the environment
variable ``MIBAND_ECDH_BACKEND``:

* ``auto`` (default) – native when available, otherwise pure Python;
* ``native`` – require the native library, raise if it cannot be loaded;
* ``python`` – always use the pure-Python implementation.

``MIBAND_ECDH_LIB`` may point at an explicit native library file, which is handy
on Windows if you built ``ecdh.dll`` yourself and keep it outside the project.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger("MiBandECDH")

PROJECT_ROOT = Path(__file__).resolve().parent
C_SOURCE_PATH = PROJECT_ROOT / "ecdh.c"
#: Where a locally compiled native library is written (git-ignored, `.cache/`).
BUILD_DIR = PROJECT_ROOT / ".cache" / "ecdh"

PRIVATE_KEY_SIZE = 24
PUBLIC_KEY_SIZE = 48
SHARED_SECRET_SIZE = 48
KEY_GENERATION_ATTEMPTS = 16

#: Native library file names tried per platform, in order.
_NATIVE_LIBRARY_NAMES = {
    "win32": ("libecdh.dll", "ecdh.dll"),
    "darwin": ("libecdh.dylib", "libecdh.so"),
}

ENV_BACKEND = "MIBAND_ECDH_BACKEND"
ENV_LIBRARY = "MIBAND_ECDH_LIB"


# ---------------------------------------------------------------------------
# Native library discovery / compilation
# ---------------------------------------------------------------------------
def native_library_candidates() -> List[Path]:
    """Return the native library paths that would be tried on this platform.

    Project-root paths come first (users may drop a prebuilt DLL there), then
    the git-ignored build cache where an auto-compiled library is stored.
    """
    names = _NATIVE_LIBRARY_NAMES.get(sys.platform, ("libecdh.so",))
    candidates = [PROJECT_ROOT / name for name in names]
    candidates.extend(BUILD_DIR / name for name in names)

    explicit = (os.getenv(ENV_LIBRARY) or "").strip()
    if explicit:
        candidates.insert(0, Path(explicit).expanduser())
    return candidates


def compiled_library_path() -> Path:
    """Return the build-cache path used for a locally compiled native library."""
    names = _NATIVE_LIBRARY_NAMES.get(sys.platform, ("libecdh.so",))
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

    Tries an explicit ``MIBAND_ECDH_LIB`` path, the platform's usual file names
    and finally a one-shot compilation from ``ecdh.c``.  Returns ``None`` (never
    raises) when nothing usable is found, so callers can fall back to Python.
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
# Backends
# ---------------------------------------------------------------------------
class NativeEcdhBackend:
    """ECDH backed by the tiny-ECDH C shared library via :mod:`ctypes`."""

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


class PurePythonEcdhBackend:
    """ECDH backed by the vendored pure-Python tiny-ECDH implementation."""

    name = "python"

    def __init__(self) -> None:
        # Imported lazily so the native path does not pay the import cost.
        from vendor.tiny_ecdh import PrivateKey, PublicKey, ecdh_generate_keys
        from vendor.tiny_ecdh.errors import TinyECDHError
        from vendor.tiny_ecdh.utils import CURVE, gf2point_is_zero, gf2point_mul

        self._private_key_cls = PrivateKey
        self._public_key_cls = PublicKey
        self._generate = ecdh_generate_keys
        self._curve = CURVE
        self._point_is_zero = gf2point_is_zero
        self._point_mul = gf2point_mul
        self._error = TinyECDHError

    def generate_keys(self) -> Tuple[bytes, bytes]:
        """Generate a 24-byte private key and 48-byte public key (band format)."""
        for _ in range(KEY_GENERATION_ATTEMPTS):
            private_key, public_key = self._generate()
            scalar = private_key.scalar
            if scalar <= 0 or scalar >= self._curve.order:
                continue
            prv = scalar.to_bytes(PRIVATE_KEY_SIZE, "little")
            pub = public_key.x.to_bytes(PRIVATE_KEY_SIZE, "little") + public_key.y.to_bytes(
                PRIVATE_KEY_SIZE, "little"
            )
            return prv, pub
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

        scalar = int.from_bytes(private_key, "little")
        if scalar <= 0 or scalar >= self._curve.order:
            raise ValueError("Private key is out of range for the NIST B-163 curve")

        # The band sends each coordinate as 24 little-endian bytes; the vendored
        # implementation works with Python integers.
        peer_x = int.from_bytes(peer_public_key[:PRIVATE_KEY_SIZE], "little")
        peer_y = int.from_bytes(peer_public_key[PRIVATE_KEY_SIZE:], "little")
        try:
            peer = self._public_key_cls(peer_x, peer_y)
            own = self._private_key_cls(scalar)
        except self._error as exc:
            raise ValueError(
                "Peer public key is not a valid point on the NIST B-163 curve"
            ) from exc

        secret_x, secret_y = self._point_mul(peer.x, peer.y, own.scalar)
        if self._point_is_zero(secret_x, secret_y):
            raise ValueError("ECDH key agreement produced the point at infinity")
        return secret_x.to_bytes(PRIVATE_KEY_SIZE, "little") + secret_y.to_bytes(
            PRIVATE_KEY_SIZE, "little"
        )


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------
def select_backend(requested: Optional[str] = None) -> object:
    """Select an ECDH backend.

    ``requested`` is one of ``auto`` (default), ``native`` or ``python``; when
    omitted the ``MIBAND_ECDH_BACKEND`` environment variable is used.
    """
    choice = (requested or os.getenv(ENV_BACKEND) or "auto").strip().lower()
    if choice in ("python", "pure", "pure-python"):
        return PurePythonEcdhBackend()
    if choice in ("native", "c", "lib"):
        return NativeEcdhBackend()
    if choice not in ("auto", ""):
        logger.warning("Unknown %s=%r; using auto detection.", ENV_BACKEND, choice)
    try:
        backend = NativeEcdhBackend()
    except RuntimeError as exc:
        logger.debug("Native ECDH backend unavailable (%s); using pure Python.", exc)
        return PurePythonEcdhBackend()
    logger.debug("Using native ECDH backend.")
    return backend


_BACKEND = select_backend()


def backend_name() -> str:
    """Return the active backend name (``native`` or ``python``) for diagnostics."""
    return str(getattr(_BACKEND, "name", type(_BACKEND).__name__))


def generate_ecdh_keys() -> Tuple[bytes, bytes]:
    """Generate a 24-byte private key and 48-byte public key on NIST B-163.

    The C routine needs a-priori random private-key bytes and rejects values
    whose bit-degree is too small; retry a few times in that (very unlikely)
    case instead of returning an all-zero public key.
    """
    return _BACKEND.generate_keys()


def compute_shared_secret(private_key: bytes, peer_public_key: bytes) -> bytes:
    """Compute the 48-byte ECDH shared secret.

    Raises :class:`ValueError` if the peer public key is not a valid point on
    the curve (the band would have sent a corrupted/forged key).
    """
    return _BACKEND.compute_shared_secret(private_key, peer_public_key)
