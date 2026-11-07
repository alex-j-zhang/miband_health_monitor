"""ECDH cryptographic wrapper using tiny-ECDH (NIST B-163) for Mi Band 6 auth.

The C library returns ``0`` on failure (e.g. a peer public key that is not a
valid curve point), which the previous revision ignored. Both wrappers now
surface those failures so the client can abort/retry the handshake instead of
silently deriving a garbage session key.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from pathlib import Path
from typing import Tuple

LIB_PATH = Path(__file__).parent / "libecdh.so"
C_SOURCE_PATH = Path(__file__).parent / "ecdh.c"

PRIVATE_KEY_SIZE = 24
PUBLIC_KEY_SIZE = 48
SHARED_SECRET_SIZE = 48
KEY_GENERATION_ATTEMPTS = 16


def ensure_library() -> ctypes.CDLL:
    """Ensure ``libecdh.so`` is compiled and load it."""
    if not LIB_PATH.exists():
        if not C_SOURCE_PATH.exists():
            raise FileNotFoundError(f"Missing ECDH source: {C_SOURCE_PATH}")
        cmd = ["gcc", "-O3", "-fPIC", "-shared", str(C_SOURCE_PATH), "-o", str(LIB_PATH)]
        subprocess.check_call(cmd)

    lib = ctypes.CDLL(str(LIB_PATH))
    lib.ecdh_generate_keys.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    lib.ecdh_generate_keys.restype = ctypes.c_int
    lib.ecdh_shared_secret.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
    lib.ecdh_shared_secret.restype = ctypes.c_int
    return lib


_LIB = ensure_library()


def generate_ecdh_keys() -> Tuple[bytes, bytes]:
    """Generate a 24-byte private key and 48-byte public key on NIST B-163.

    The C routine needs a-priori random private-key bytes and rejects values
    whose bit-degree is too small; retry a few times in that (very unlikely)
    case instead of returning an all-zero public key.
    """
    for _ in range(KEY_GENERATION_ATTEMPTS):
        prv = bytearray(os.urandom(PRIVATE_KEY_SIZE))
        pub = bytearray(PUBLIC_KEY_SIZE)
        result = _LIB.ecdh_generate_keys(
            (ctypes.c_char * PUBLIC_KEY_SIZE).from_buffer(pub),
            (ctypes.c_char * PRIVATE_KEY_SIZE).from_buffer(prv),
        )
        if result == 1:
            return bytes(prv), bytes(pub)
    raise RuntimeError("ECDH key generation failed after repeated attempts")


def compute_shared_secret(private_key: bytes, peer_public_key: bytes) -> bytes:
    """Compute the 48-byte ECDH shared secret.

    Raises :class:`ValueError` if the peer public key is not a valid point on
    the curve (the band would have sent a corrupted/forged key).
    """
    if len(private_key) != PRIVATE_KEY_SIZE:
        raise ValueError(f"Private key must be {PRIVATE_KEY_SIZE} bytes, got {len(private_key)}")
    if len(peer_public_key) != PUBLIC_KEY_SIZE:
        raise ValueError(
            f"Peer public key must be {PUBLIC_KEY_SIZE} bytes, got {len(peer_public_key)}"
        )

    secret = bytearray(SHARED_SECRET_SIZE)
    result = _LIB.ecdh_shared_secret(
        (ctypes.c_char * PRIVATE_KEY_SIZE).from_buffer(bytearray(private_key)),
        (ctypes.c_char * PUBLIC_KEY_SIZE).from_buffer(bytearray(peer_public_key)),
        (ctypes.c_char * SHARED_SECRET_SIZE).from_buffer(secret),
    )
    if result != 1:
        raise ValueError("Peer public key is not a valid point on the NIST B-163 curve")
    return bytes(secret)
