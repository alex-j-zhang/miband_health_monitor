"""Chunked-ECDH auth state machine: reassembly of the band's step-1 payload.

The band answers the client's public key with a chunked notification stream:
a first control frame announces the total length and the auth step, then
sequence-numbered fragments carry the 16-byte challenge salt followed by the
48-byte NIST B-163 public key. This mixin implements the hardened reassembly
(sequence continuity, bounds checks, duplicate handling) and the step-2
acknowledgement handling.

All state is reset per connection by _reset_auth_state; failures are surfaced
through the _auth_event/_auth_error pair consumed by connect_and_authenticate.
"""

from __future__ import annotations

import asyncio
import logging

from .protocol import (
    AUTH_STEP1_PAYLOAD_SIZE,
    AUTH_TOTAL_LENGTH_OVERHEAD,
    CHUNKED_ENDPOINT_AUTH,
    CHUNKED_NEXT_HEADER_SIZE,
    MAX_REASSEMBLY_BYTES,
)

logger = logging.getLogger("MiBand6Client")


class AuthStateMixin:
    """Chunked-auth notification parsing and reassembly state."""

    def _reset_auth_state(self) -> None:
        """Drop all per-connection auth state before a fresh handshake."""
        self.authenticated = False
        self._auth_error = None
        self._auth_event.clear()
        self._session_mtu = None
        self._handle = 0
        self._reassemble_ptr = 0
        self._expected_bytes = 0
        self._next_seq = 0
        self._step2_sent = False

    def _fail_auth(self, reason: str) -> None:
        """Record the first auth failure and wake the handshake waiter."""
        if self._auth_event.is_set():
            logger.debug("Ignoring late auth failure after handshake settled: %s", reason)
            return
        self._auth_error = reason
        logger.error("Chunked authentication failed: %s", reason)
        self._auth_event.set()

    def _on_chunked_notification(self, _sender, data: bytearray) -> None:
        """Process one chunked-transfer notification from the band."""
        try:
            self._consume_chunked_packet(bytes(data))
        except Exception as exc:  # never let a malformed frame escape a BLE callback
            logger.exception("Error while handling chunked notification")
            if not self.authenticated and not self._auth_event.is_set():
                self._fail_auth(f"chunked notification handling error: {exc}")

    def _consume_chunked_packet(self, packet: bytes) -> None:
        """Route one notification to the auth control/fragment handler."""
        if len(packet) < CHUNKED_NEXT_HEADER_SIZE or packet[0] != 0x03:
            logger.debug("Ignoring malformed chunked packet (%d bytes)", len(packet))
            return

        seq = packet[4]
        is_auth_control = (
            seq == 0
            and len(packet) >= 14
            and packet[9] == CHUNKED_ENDPOINT_AUTH
            and packet[10] == 0x00
            and packet[11] == 0x10
        )
        if is_auth_control:
            self._handle_auth_control(packet)
            return
        if seq > 0:
            self._handle_auth_fragment(packet)

    def _handle_auth_control(self, packet: bytes) -> None:
        """Handle the step-1/step-2 control frames (sequence counter 0)."""
        step = packet[12]
        status = packet[13]

        if step == 0x04:
            if status != 0x01:
                self._fail_auth(f"auth step 1 rejected by band (status 0x{status:02x})")
                return
            if self._step2_sent:
                return
            if self._reassemble_ptr > 0:
                logger.debug("Ignoring duplicate auth step-1 header packet")
                return

            total_length = int.from_bytes(packet[5:9], "little")
            expected = total_length - AUTH_TOTAL_LENGTH_OVERHEAD
            if not (AUTH_STEP1_PAYLOAD_SIZE <= expected <= MAX_REASSEMBLY_BYTES):
                self._fail_auth(
                    f"invalid auth payload size (total={total_length}, expected={expected})"
                )
                return

            self._expected_bytes = expected
            self._next_seq = 1
            logger.info(
                "Auth step 1 acknowledged; reassembling %d-byte challenge (16B salt + 48B pubkey)...",
                expected,
            )
            self._append_auth_fragment(packet[14:])
            return

        if step == 0x05:
            if status == 0x01:
                logger.info("Auth step 2 acknowledged by band.")
                self._auth_event.set()
            else:
                self._fail_auth(f"auth step 2 rejected by band (status 0x{status:02x})")
            return

        self._fail_auth(f"unexpected auth step 0x{step:02x}")

    def _handle_auth_fragment(self, packet: bytes) -> None:
        """Append one sequence-numbered auth fragment, rejecting gaps/duplicates."""
        if self._expected_bytes <= 0 or self._step2_sent or self._auth_event.is_set():
            return

        seq = packet[4]
        if seq < self._next_seq:
            logger.debug(
                "Ignoring duplicate auth fragment seq=%d (expected %d)", seq, self._next_seq
            )
            return
        if seq > self._next_seq:
            self._fail_auth(
                f"auth fragment gap: expected seq {self._next_seq}, received {seq}; "
                "BLE fragments were dropped — the session will be retried"
            )
            return
        self._append_auth_fragment(packet[CHUNKED_NEXT_HEADER_SIZE:])
        self._next_seq = seq + 1

    def _append_auth_fragment(self, payload: bytes) -> None:
        """Copy one fragment into the reassembly buffer and finish step 1 if full."""
        if not payload:
            return
        end = self._reassemble_ptr + len(payload)
        if end > self._expected_bytes:
            self._fail_auth(
                f"auth reassembly overflow: {end} bytes > expected {self._expected_bytes}"
            )
            return

        self._reassemble_buf[self._reassemble_ptr : end] = payload
        self._reassemble_ptr = end

        if self._reassemble_ptr < self._expected_bytes or self._step2_sent:
            return

        self._step2_sent = True
        logger.info(
            "Auth challenge fully reassembled (%d bytes). Computing ECDH shared secret...",
            self._expected_bytes,
        )
        asyncio.create_task(self._complete_auth_step2())
