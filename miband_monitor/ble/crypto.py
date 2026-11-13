"""AES helpers used by the chunked-ECDH handshake.

The Mi Band 6 auth handshake does not use TLS: after an ECDH exchange with the
band's NIST B-163 key it encrypts a 16-byte random challenge with AES and sends
the result back. Two primitives are needed:

* _aes_cbc_encrypt - AES-CBC with the all-zero IV and no padding, used to
  encrypt both the auth key and the derived session key in step 2;
* encrypt_challenge - the AES-128-ECB helper kept for callers/tests that verify
  the challenge encryption in isolation.

Neither function authenticates anything by itself; integrity comes from the
ECDH shared secret mixed into the session key.
"""

from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def _aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """Encrypt data with AES-CBC and no padding.

    The caller guarantees block-aligned input (16 bytes in the handshake). This
    deliberately does not add PKCS#7 padding: the band expects exactly 16 bytes
    per encrypted value.
    """
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    return encryptor.update(data) + encryptor.finalize()


def encrypt_challenge(auth_key: bytes, challenge_bytes: bytes) -> bytes:
    """Encrypt a 16-byte random challenge using AES-128-ECB without padding."""
    if len(auth_key) != 16:
        raise ValueError(f"Auth key must be exactly 16 bytes, got {len(auth_key)}")
    if len(challenge_bytes) != 16:
        raise ValueError(f"Challenge payload must be exactly 16 bytes, got {len(challenge_bytes)}")

    cipher = Cipher(algorithms.AES(auth_key), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(challenge_bytes) + encryptor.finalize()
