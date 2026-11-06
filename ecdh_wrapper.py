"""ECDH cryptographic wrapper using tiny-ECDH (NIST B-163) for Mi Band 6 authentication."""

import ctypes
import os
import subprocess
from pathlib import Path
from typing import Tuple

LIB_PATH = Path(__file__).parent / "libecdh.so"
C_SOURCE_PATH = Path(__file__).parent / "ecdh.c"


def ensure_library() -> ctypes.CDLL:
    """Ensure libecdh.so is compiled and load it."""
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
    """Generate 24-byte private key and 48-byte public key on NIST B-163."""
    prv = bytearray(os.urandom(24))
    pub = bytearray(48)
    _LIB.ecdh_generate_keys(
        (ctypes.c_char * 48).from_buffer(pub),
        (ctypes.c_char * 24).from_buffer(prv),
    )
    return bytes(prv), bytes(pub)


def compute_shared_secret(private_key: bytes, peer_public_key: bytes) -> bytes:
    """Compute 48-byte ECDH shared secret from own private key and peer's public key."""
    if len(private_key) != 24:
        raise ValueError(f"Private key must be 24 bytes, got {len(private_key)}")
    if len(peer_public_key) != 48:
        raise ValueError(f"Peer public key must be 48 bytes, got {len(peer_public_key)}")

    sec = bytearray(48)
    _LIB.ecdh_shared_secret(
        (ctypes.c_char * 24).from_buffer(bytearray(private_key)),
        (ctypes.c_char * 48).from_buffer(bytearray(peer_public_key)),
        (ctypes.c_char * 48).from_buffer(sec),
    )
    return bytes(sec)

