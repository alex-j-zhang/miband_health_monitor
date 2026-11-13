"""Chunked-ECDH step 2: derive the session key and answer the challenge.

Once the band's 64-byte step-1 payload has been reassembled (16-byte random
salt + 48-byte NIST B-163 public key) the client:

1. computes the ECDH shared secret with the private key generated for this
   connection;
2. mixes it with the configured 16-byte auth key to derive the session key;
3. AES-CBC-encrypts both the auth key and the session key with the band's salt
   (all-zero IV, no padding) and sends them back as chunked auth step 2.

The raw salt/public-key bytes are deliberately NOT logged: they are protocol
secrets and the old DEBUG lines leaked them into logs and issue reports. Only
sizes/steps are logged, which is enough to diagnose a handshake failure.
"""

from __future__ import annotations

import logging

from ..crypto.facade import compute_shared_secret
from .auth_connect import _write_chunked_payload
from .crypto import _aes_cbc_encrypt
from .protocol import (
    AUTH_STEP1_PUBKEY_SIZE,
    AUTH_STEP1_RANDOM_SIZE,
    CHUNKED_ENDPOINT_AUTH,
    UUID_CHAR_CHUNKED_WRITE,
)

logger = logging.getLogger("MiBand6Client")


class AuthHandshakeMixin:
    """Complete the chunked-ECDH handshake (step 2)."""

    async def _complete_auth_step2(self) -> None:
        """Compute the ECDH shared secret and send the AES-encrypted response."""
        try:
            if self.client is None:
                raise RuntimeError("BLE client is not connected")
            remote_random = bytes(self._reassemble_buf[:AUTH_STEP1_RANDOM_SIZE])
            remote_public = bytes(
                self._reassemble_buf[
                    AUTH_STEP1_RANDOM_SIZE : AUTH_STEP1_RANDOM_SIZE + AUTH_STEP1_PUBKEY_SIZE
                ]
            )

            shared_secret = compute_shared_secret(self._prv_key, remote_public)
            session_key = bytes(
                shared_secret[index + 8] ^ self.auth_key[index] for index in range(16)
            )

            iv = b"\x00" * 16
            encrypted_auth_key = _aes_cbc_encrypt(self.auth_key, iv, remote_random)
            encrypted_session_key = _aes_cbc_encrypt(session_key, iv, remote_random)

            self._handle += 1
            response = b"\x05" + encrypted_auth_key + encrypted_session_key
            logger.info("Sending chunked auth step 2 response (%d bytes)...", len(response))
            await _write_chunked_payload(
                self.client,
                UUID_CHAR_CHUNKED_WRITE,
                CHUNKED_ENDPOINT_AUTH,
                self._handle,
                response,
                mtu=self._session_mtu,
                lock=self._gatt_lock,
            )
        except Exception as exc:
            logger.error("Failed to complete auth step 2: %s", exc)
            self._fail_auth(f"could not complete auth step 2: {exc}")
