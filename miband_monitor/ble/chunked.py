"""Chunked Transfer Protocol framing and GATT write helpers.

The Mi Band 6 handshake exchanges payloads that do not fit one ATT write. The
protocol splits every logical message into a first frame (11-byte header with
total length and endpoint type) and continuation frames (5-byte header with a
rolling sequence counter). Chunk sizes are derived from the negotiated ATT MTU,
with a conservative 23-byte floor.

Writes retry once on BlueZ's spurious org.bluez.Error.InProgress; everything
else propagates unchanged so callers see the same exceptions as before.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from .. import bluez
from .backends import BleakClient
from .protocol import (
    CHUNKED_ATT_OVERHEAD,
    CHUNKED_DEFAULT_MTU,
    CHUNKED_FIRST_HEADER_SIZE,
    CHUNKED_NEXT_HEADER_SIZE,
)


def _resolve_mtu(client: object, mtu: Optional[int] = None) -> int:
    """Best-effort MTU discovery with a conservative floor of 23 bytes."""
    candidate: object = mtu
    if candidate is None:
        try:
            candidate = client.mtu_size  # type: ignore[attr-defined]
        except Exception:
            candidate = None
    try:
        resolved = int(candidate)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        resolved = CHUNKED_DEFAULT_MTU
    return max(CHUNKED_DEFAULT_MTU, resolved)


async def _write_with_in_progress_retry(
    client: Any, char_uuid: str, data: bytes, response: bool = False
) -> None:
    """Write one GATT characteristic, retrying once on BlueZ InProgress.

    dbus-fast occasionally reports org.bluez.Error.InProgress when two
    operations race on the same ATT queue. It is transient: a short pause and a
    second write succeeds. Any other exception is re-raised untouched.
    """
    for attempt in range(2):
        try:
            await client.write_gatt_char(char_uuid, data, response=response)
            return
        except Exception as exc:
            if attempt == 0 and bluez.is_in_progress_error(exc):
                await asyncio.sleep(0.15)
                continue
            raise


async def write_chunked_value(
    client: BleakClient,
    char_uuid: str,
    endpoint_type: int,
    handle: int,
    data: bytes,
    mtu: Optional[int] = None,
    lock: Optional[asyncio.Lock] = None,
) -> None:
    """Split and transmit a payload using the Mi Band 6 Chunked Transfer Protocol.

    The first ATT write carries an 11-byte header (total length + endpoint);
    the remaining writes carry a 5-byte header. All payload sizes are derived
    from the negotiated MTU (minimum 23 bytes, i.e. 20-byte ATT payload).
    """
    if not data:
        raise ValueError("Cannot send an empty chunked payload")

    resolved_mtu = _resolve_mtu(client, mtu)
    first_payload_max = resolved_mtu - CHUNKED_ATT_OVERHEAD - CHUNKED_FIRST_HEADER_SIZE
    next_payload_max = resolved_mtu - CHUNKED_ATT_OVERHEAD - CHUNKED_NEXT_HEADER_SIZE
    if first_payload_max <= 0 or next_payload_max <= 0:
        raise ValueError(f"MTU {resolved_mtu} is too small for the chunked protocol")

    total_length = len(data)
    offset = 0
    count = 0
    first = True
    while offset < total_length:
        header_size = CHUNKED_FIRST_HEADER_SIZE if first else CHUNKED_NEXT_HEADER_SIZE
        max_chunk = first_payload_max if first else next_payload_max
        copy_bytes = min(total_length - offset, max_chunk)
        is_last = offset + copy_bytes >= total_length

        flags = 0x01 if first else 0x00
        if is_last:
            flags |= 0x06  # end of transmission frame

        chunk = bytearray(header_size + copy_bytes)
        chunk[0] = 0x03
        chunk[1] = flags
        chunk[2] = 0x00
        chunk[3] = handle & 0xFF
        chunk[4] = count & 0xFF
        if first:
            chunk[5:9] = total_length.to_bytes(4, "little")
            chunk[9:11] = (endpoint_type & 0xFFFF).to_bytes(2, "little")
        chunk[header_size:] = data[offset : offset + copy_bytes]

        if lock is not None:
            async with lock:
                await _write_with_in_progress_retry(client, char_uuid, bytes(chunk), response=False)
        else:
            await _write_with_in_progress_retry(client, char_uuid, bytes(chunk), response=False)
        offset += copy_bytes
        count += 1
        first = False
