"""Backend selection and the public key-agreement helpers.

Both backends speak the same wire format, so the rest of the project only calls
generate_ecdh_keys and compute_shared_secret.  Backend selection is cached at
import time exactly like the legacy ecdh_wrapper.py, keeping the
MIBAND_ECDH_BACKEND / MIBAND_ECDH_LIB behaviour byte-for-byte identical.
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

from .constants import ENV_BACKEND, logger
from .native import NativeEcdhBackend
from .pure_python import PurePythonEcdhBackend

# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------
def select_backend(requested: Optional[str] = None) -> object:
    """Select an ECDH backend.

    requested is one of auto (default), native or python; when omitted the
    MIBAND_ECDH_BACKEND environment variable is used.
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
    """Return the active backend name (native or python) for diagnostics."""
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

    Raises ValueError if the peer public key is not a valid point on the curve
    (the band would have sent a corrupted/forged key).
    """
    return _BACKEND.compute_shared_secret(private_key, peer_public_key)


# ---------------------------------------------------------------------------
# Legacy module aliases
# ---------------------------------------------------------------------------
# The historical ecdh_wrapper module exposed its ctypes, subprocess and sys
# imports, and the older tests monkeypatch e.g. ecdh_wrapper.ctypes.CDLL or
# ecdh_wrapper.sys.platform.  Re-exporting the same module objects keeps those
# hooks working through a thin root facade.
import ctypes  # noqa: E402,F401
import subprocess  # noqa: E402,F401
import sys  # noqa: E402,F401
