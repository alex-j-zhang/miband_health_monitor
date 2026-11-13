"""Pure-Python tiny-ECDH backend (vendored MIT-licensed implementation).

This is the fallback everywhere the native library is unavailable (Windows,
macOS, ARM Linux, CI containers without a compiler).  It produces byte-for-byte
identical output to the C library; tests/test_ecdh_backends.py cross-checks
both backends on Linux.  The vendored package is imported lazily so the native
path does not pay the import cost.
"""

from __future__ import annotations

from typing import Tuple

from .constants import KEY_GENERATION_ATTEMPTS, PRIVATE_KEY_SIZE, PUBLIC_KEY_SIZE


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
