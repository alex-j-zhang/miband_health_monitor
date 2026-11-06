"""Unit tests for Mi Band 6 client crypto and parser modules."""

import pytest
from miband_client import encrypt_challenge, parse_heart_rate, MiBand6Client


def test_encrypt_challenge_valid():
    """Verify AES-128-ECB encryption against standard NIST test vector."""
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    plaintext = bytes.fromhex("00112233445566778899aabbccddeeff")
    expected_ciphertext = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")

    result = encrypt_challenge(key, plaintext)
    assert result == expected_ciphertext


def test_encrypt_challenge_invalid_length():
    """Ensure ValueError is raised when key or challenge has invalid length."""
    with pytest.raises(ValueError):
        encrypt_challenge(b"short_key", bytes(16))

    with pytest.raises(ValueError):
        encrypt_challenge(bytes(16), b"short_challenge")


def test_parse_heart_rate_uint8():
    """Verify parsing of 8-bit heart rate format."""
    # Flag: 0x00 (uint8 format), BPM: 78
    raw_packet = bytearray([0x00, 78])
    assert parse_heart_rate(raw_packet) == 78


def test_parse_heart_rate_uint16():
    """Verify parsing of 16-bit heart rate format."""
    # Flag: 0x01 (uint16 format), BPM: 135 (0x0087)
    raw_packet = bytearray([0x01, 0x87, 0x00])
    assert parse_heart_rate(raw_packet) == 135


def test_parse_heart_rate_invalid_packet():
    """Verify exception on truncated packet."""
    with pytest.raises(ValueError):
        parse_heart_rate(bytearray([0x00]))


def test_client_initialization_key_formats():
    """Verify auth key sanitization in MiBand6Client."""
    valid_key = "0123456789abcdef0123456789abcdef"
    client = MiBand6Client("00:11:22:33:44:55", valid_key)
    assert client.auth_key == bytes.fromhex(valid_key)

    # Prefix 0x handling
    client_prefixed = MiBand6Client("00:11:22:33:44:55", "0x" + valid_key)
    assert client_prefixed.auth_key == bytes.fromhex(valid_key)

    # Invalid key length
    with pytest.raises(ValueError):
        MiBand6Client("00:11:22:33:44:55", "invalid_short_key")
