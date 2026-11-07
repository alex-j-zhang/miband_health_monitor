"""Tests for chunked-transfer framing and hardened auth reassembly."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from miband_client import (
    AUTH_STEP1_PAYLOAD_SIZE,
    CHUNKED_DEFAULT_MTU,
    CHUNKED_ENDPOINT_AUTH,
    MiBand6Client,
    write_chunked_value,
)

MAC = "C0:FF:EE:C0:FF:EE"
KEY = "0123456789abcdef0123456789abcdef"


class RecordingClient:
    """Minimal write-only stand-in for ``BleakClient``."""

    def __init__(self, mtu: int = CHUNKED_DEFAULT_MTU) -> None:
        self.mtu_size = mtu
        self.writes: list[bytes] = []

    async def write_gatt_char(self, _char_uuid, data, response=None) -> None:
        self.writes.append(bytes(data))


def _parse_chunked_writes(writes: list[bytes]) -> tuple[int, int, bytes]:
    """Reassemble a chunked transmission produced by the client."""
    first = writes[0]
    assert first[0] == 0x03
    total_length = int.from_bytes(first[5:9], "little")
    endpoint = int.from_bytes(first[9:11], "little")
    payload = bytearray(first[11:])
    for chunk in writes[1:]:
        assert chunk[0] == 0x03
        payload.extend(chunk[5:])
    assert len(payload) == total_length
    return total_length, endpoint, bytes(payload)


async def test_chunked_write_uses_default_mtu():
    client = RecordingClient(CHUNKED_DEFAULT_MTU)
    payload = bytes(range(52))

    await write_chunked_value(client, "char", CHUNKED_ENDPOINT_AUTH, 0, payload)

    # MTU 23 -> 20-byte ATT payload -> first chunk 9 bytes, then 15-byte chunks.
    assert [len(chunk) for chunk in client.writes] == [20, 20, 20, 18]
    assert client.writes[0][1] & 0x01  # first-chunk flag
    assert client.writes[-1][1] & 0x06  # end-of-transmission flag
    total, endpoint, reassembled = _parse_chunked_writes(client.writes)
    assert total == len(payload)
    assert endpoint == CHUNKED_ENDPOINT_AUTH
    assert reassembled == payload


async def test_chunked_write_uses_negotiated_mtu():
    client = RecordingClient(mtu=185)
    payload = bytes(range(52))

    await write_chunked_value(client, "char", CHUNKED_ENDPOINT_AUTH, 3, payload)

    assert len(client.writes) == 1  # 52-byte payload fits into one ATT write
    assert len(client.writes[0]) == 11 + len(payload)
    assert client.writes[0][1] == 0x07  # first + last
    assert client.writes[0][3] == 3  # handle
    total, endpoint, reassembled = _parse_chunked_writes(client.writes)
    assert (total, endpoint, reassembled) == (52, CHUNKED_ENDPOINT_AUTH, payload)


async def test_chunked_write_rejects_empty_payload():
    client = RecordingClient()
    with pytest.raises(ValueError):
        await write_chunked_value(client, "char", CHUNKED_ENDPOINT_AUTH, 0, b"")
    assert client.writes == []


async def test_chunked_write_explicit_mtu_override_is_clamped():
    client = RecordingClient(mtu=185)
    await write_chunked_value(client, "char", 0x82, 0, b"abc", mtu=10)
    # Clamped to the 23-byte floor -> one write with the 11-byte header.
    assert len(client.writes) == 1
    assert client.writes[0][11:] == b"abc"


# ---------------------------------------------------------------------------
# Hardened auth reassembly: drive the notification handler directly.
# ---------------------------------------------------------------------------
def _make_auth_packets(payload: bytes, total_length: int | None = None) -> list[bytes]:
    """Build the band-style notification packets for an auth step-1 response."""
    total = len(payload) + 3 if total_length is None else total_length

    header = bytearray(14)
    header[0] = 0x03
    header[1] = 0x01
    header[3] = 0x00
    header[4] = 0x00
    header[5:9] = total.to_bytes(4, "little")
    header[9] = CHUNKED_ENDPOINT_AUTH
    header[10] = 0x00
    header[11] = 0x10
    header[12] = 0x04
    header[13] = 0x01
    packets = [bytes(header) + payload[:9]]

    offset = 9
    sequence = 1
    while offset < len(payload):
        fragment = payload[offset : offset + 18]
        fragment_header = bytearray(5)
        fragment_header[0] = 0x03
        fragment_header[1] = 0x06 if offset + len(fragment) >= len(payload) else 0x00
        fragment_header[3] = 0x00
        fragment_header[4] = sequence
        packets.append(bytes(fragment_header) + fragment)
        offset += len(fragment)
        sequence += 1
    return packets


def _make_client() -> MiBand6Client:
    client = MiBand6Client(MAC, KEY)
    client._reset_auth_state()
    return client


async def test_reassembly_success_accepted():
    payload = bytes(range(64))
    client = _make_client()
    client._complete_auth_step2 = AsyncMock()  # type: ignore[method-assign]

    for packet in _make_auth_packets(payload):
        client._consume_chunked_packet(packet)
    await asyncio.sleep(0)

    assert client._reassemble_ptr == AUTH_STEP1_PAYLOAD_SIZE
    assert client._next_seq == 5
    assert client._step2_sent is True
    assert client._auth_error is None
    client._complete_auth_step2.assert_awaited_once()


async def test_reassembly_rejects_sequence_gap():
    payload = bytes(range(64))
    client = _make_client()
    packets = _make_auth_packets(payload)
    # Drop fragment seq=2, then deliver the rest.
    for index, packet in enumerate(packets):
        if index == 2:
            continue
        client._consume_chunked_packet(packet)

    assert client._auth_event.is_set()
    assert client._auth_error is not None
    assert "gap" in client._auth_error


async def test_reassembly_ignores_duplicate_fragment():
    payload = bytes(range(64))
    client = _make_client()
    client._complete_auth_step2 = AsyncMock()  # type: ignore[method-assign]
    packets = _make_auth_packets(payload)

    for packet in packets[:3]:
        client._consume_chunked_packet(packet)
    client._consume_chunked_packet(packets[2])  # duplicate seq=2
    for packet in packets[3:]:
        client._consume_chunked_packet(packet)
    await asyncio.sleep(0)

    assert client._reassemble_ptr == 64
    assert client._auth_error is None
    assert client._complete_auth_step2.await_count == 1


async def test_reassembly_rejects_overflow():
    payload = bytes(range(64))
    client = _make_client()
    packets = _make_auth_packets(payload)
    client._consume_chunked_packet(packets[0])

    oversized = bytearray(5)
    oversized[0] = 0x03
    oversized[4] = 1
    oversized.extend(b"\x00" * 100)
    client._consume_chunked_packet(bytes(oversized))

    assert client._auth_error is not None
    assert "overflow" in client._auth_error


async def test_reassembly_rejects_invalid_declared_size():
    client = _make_client()
    packet = _make_auth_packets(b"\x00" * 9, total_length=10)  # expected = 7 bytes
    client._consume_chunked_packet(packet[0])

    assert client._auth_error is not None
    assert "invalid auth payload size" in client._auth_error


async def test_reassembly_rejects_unknown_auth_step():
    client = _make_client()
    header = bytearray(14)
    header[0] = 0x03
    header[9] = CHUNKED_ENDPOINT_AUTH
    header[11] = 0x10
    header[12] = 0x09  # not 0x04 / 0x05
    header[13] = 0x01
    client._consume_chunked_packet(bytes(header))

    assert client._auth_error is not None
    assert "unexpected auth step" in client._auth_error


# ---------------------------------------------------------------------------
# Session MTU plumbing: the client resolves the MTU once per connection and
# forwards it to every chunked write (needed because Windows/macOS backends
# report different values than BlueZ).
# ---------------------------------------------------------------------------
async def test_client_forwards_configured_mtu_to_chunked_writes(monkeypatch):
    import miband_client as client_module
    from sim_band import SimulatedBand, make_simulated_client

    captured: list = []
    real_write = client_module.write_chunked_value

    async def spy(*args, **kwargs):
        captured.append(kwargs.get("mtu"))
        await real_write(*args, **kwargs)

    monkeypatch.setattr(client_module, "write_chunked_value", spy)
    band = SimulatedBand(auth_key=bytes.fromhex(KEY))
    client = make_simulated_client(MAC, KEY, band, scanner_address=MAC, mtu=185)
    try:
        await client.connect_and_authenticate(scan_timeout=2.0)
        assert client.authenticated is True
    finally:
        await client.disconnect()

    # One write for the client public key, one for the auth step-2 response.
    assert captured == [185, 185]


async def test_client_uses_backend_mtu_when_not_configured(monkeypatch):
    import miband_client as client_module
    from sim_band import SimulatedBand, make_simulated_client

    captured: list = []
    real_write = client_module.write_chunked_value

    async def spy(*args, **kwargs):
        captured.append(kwargs.get("mtu"))
        await real_write(*args, **kwargs)

    monkeypatch.setattr(client_module, "write_chunked_value", spy)
    band = SimulatedBand(auth_key=bytes.fromhex(KEY))
    client = make_simulated_client(MAC, KEY, band, scanner_address=MAC)
    try:
        await client.connect_and_authenticate(scan_timeout=2.0)
        assert client._session_mtu == CHUNKED_DEFAULT_MTU
    finally:
        await client.disconnect()

    assert captured == [CHUNKED_DEFAULT_MTU, CHUNKED_DEFAULT_MTU]
