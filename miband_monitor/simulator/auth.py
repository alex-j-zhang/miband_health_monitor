"""Chunked-transfer framing and ECDH/AES authentication for the simulator.

The band side of the handshake is implemented here as a mixin consumed by
:class:`miband_monitor.simulator.band.SimulatedBand`:

1. reassemble client chunks written to the 0x0016 characteristic;
2. on the auth endpoint, derive the session key from the ECDH shared secret
   XORed with the stored auth key and send back salt + public key;
3. verify the client's two AES-CBC challenge blocks (auth key + session key)
   and report the result in the 0x0017 notification stream.
"""

from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from miband_monitor.ble import CHUNKED_ENDPOINT_AUTH, UUID_CHAR_CHUNKED_READ
from miband_monitor.crypto import compute_shared_secret

from .constants import (
    _AUTH_STEP1_PREFIX,
    _AUTH_STEP2_OPCODE,
    _AUTH_TOTAL_LENGTH_FIELD,
    _FIRST_CHUNK_HEADER,
    _NEXT_CHUNK_HEADER,
    logger,
)


def _aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    return encryptor.update(data) + encryptor.finalize()


class AuthMixin:
    """Auth and chunked-framing half of the simulated band state machine."""

    # ------------------------------------------------------- chunked framing
    def _consume_chunk(self, chunk: bytes) -> None:
        if len(chunk) < _NEXT_CHUNK_HEADER:
            return
        flags = chunk[1]
        handle = chunk[3]
        if flags & 0x01:
            if len(chunk) < _FIRST_CHUNK_HEADER:
                return
            total_length = int.from_bytes(chunk[5:9], "little")
            endpoint = int.from_bytes(chunk[9:11], "little")
            self._write_transactions[handle] = [endpoint, total_length, bytearray(chunk[11:])]
        else:
            transaction = self._write_transactions.get(handle)
            if transaction is None:
                return
            transaction[2].extend(chunk[_NEXT_CHUNK_HEADER:])

        transaction = self._write_transactions.get(handle)
        if transaction is not None and len(transaction[2]) >= transaction[1]:
            endpoint, total_length, buffer = transaction
            del self._write_transactions[handle]
            self._on_chunked_message(endpoint, bytes(buffer[:total_length]))

    def _on_chunked_message(self, endpoint: int, message: bytes) -> None:
        if endpoint != CHUNKED_ENDPOINT_AUTH:
            logger.debug("Simulated band: ignoring message for endpoint %s", endpoint)
            return
        if self.authenticated:
            return
        if message.startswith(_AUTH_STEP1_PREFIX):
            self._handle_auth_step1(message[len(_AUTH_STEP1_PREFIX) :])
        elif message[:1] == bytes([_AUTH_STEP2_OPCODE]):
            self._handle_auth_step2(message[1:])
        else:
            logger.debug("Simulated band: unknown auth message %s", message.hex())

    # ---------------------------------------------------------------- auth
    def _handle_auth_step1(self, client_public_key: bytes) -> None:
        if len(client_public_key) != 48:
            logger.warning("Simulated band: bad client public key length %d", len(client_public_key))
            return
        shared_secret = compute_shared_secret(self._prv_key, client_public_key)
        self._session_key = bytes(
            shared_secret[index + 8] ^ self.auth_key[index] for index in range(16)
        )
        self._emit_auth_step1_response()

    def _emit_auth_step1_response(self) -> None:
        payload = self._salt + self._pub_key
        total_length = len(payload) + _AUTH_TOTAL_LENGTH_FIELD

        first_header = bytearray(14)
        first_header[0] = 0x03
        first_header[1] = 0x01
        first_header[3] = 0x00  # handle
        first_header[4] = 0x00  # sequence
        first_header[5:9] = total_length.to_bytes(4, "little")
        first_header[9] = 0x82
        first_header[10] = 0x00
        first_header[11] = 0x10
        first_header[12] = 0x04  # auth step
        first_header[13] = 0x01  # status OK

        packets: list[bytes] = [bytes(first_header) + payload[:9]]
        offset = 9
        sequence = 1
        while offset < len(payload):
            fragment = payload[offset : offset + 18]
            header = bytearray(5)
            header[0] = 0x03
            header[1] = 0x06 if offset + len(fragment) >= len(payload) else 0x00
            header[3] = 0x00
            header[4] = sequence
            packets.append(bytes(header) + fragment)
            offset += len(fragment)
            sequence += 1

        for index, packet in enumerate(packets):
            if index in self.drop_auth_seqs:
                logger.warning("Simulated band: dropping auth fragment seq=%d", index)
                continue
            self._timers.append(
                self._loop.call_later(  # type: ignore[union-attr]
                    0.005 * index, self._emit, UUID_CHAR_CHUNKED_READ, packet
                )
            )

    def _handle_auth_step2(self, body: bytes) -> None:
        ok = False
        if self._session_key is not None and len(body) >= 32:
            iv = b"\x00" * 16
            expected_challenge = _aes_cbc_encrypt(self.auth_key, iv, self._salt)
            expected_session = _aes_cbc_encrypt(self._session_key, iv, self._salt)
            ok = (
                not self._fail_this_auth
                and body[:16] == expected_challenge
                and body[16:32] == expected_session
            )
        self.authenticated = ok
        logger.debug("Simulated band: auth step 2 verified=%s", ok)

        packet = bytearray(14)
        packet[0] = 0x03
        packet[1] = 0x01
        packet[3] = 0x01
        packet[4] = 0x00
        packet[5:9] = (3).to_bytes(4, "little")
        packet[9] = 0x82
        packet[10] = 0x00
        packet[11] = 0x10
        packet[12] = 0x05
        packet[13] = 0x01 if ok else 0x02
        self._timers.append(
            self._loop.call_later(  # type: ignore[union-attr]
                0.005, self._emit, UUID_CHAR_CHUNKED_READ, bytes(packet)
            )
        )
