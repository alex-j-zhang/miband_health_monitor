"""Unit tests for Mi Band 6 client crypto, ECDH, and parser modules."""

import pytest
import struct
from miband_client import (
    encrypt_challenge,
    parse_heart_rate,
    parse_realtime_steps,
    parse_raw_sensor_data,
    MiBand6Client,
)
from ecdh_wrapper import generate_ecdh_keys, compute_shared_secret


def test_ecdh_key_exchange():
    """Verify that ECDH key agreement generates matching shared secrets."""
    prv_a, pub_a = generate_ecdh_keys()
    prv_b, pub_b = generate_ecdh_keys()

    assert len(prv_a) == 24
    assert len(pub_a) == 48
    assert len(prv_b) == 24
    assert len(pub_b) == 48

    sec_a = compute_shared_secret(prv_a, pub_b)
    sec_b = compute_shared_secret(prv_b, pub_a)

    assert len(sec_a) == 48
    assert sec_a == sec_b


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
    raw_packet = bytearray([0x00, 78])
    assert parse_heart_rate(raw_packet) == 78


def test_parse_heart_rate_uint16():
    """Verify parsing of 16-bit heart rate format."""
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

    client_prefixed = MiBand6Client("00:11:22:33:44:55", "0x" + valid_key)
    assert client_prefixed.auth_key == bytes.fromhex(valid_key)

    with pytest.raises(ValueError):
        MiBand6Client("00:11:22:33:44:55", "invalid_short_key")


def test_parse_realtime_steps_valid():
    """Verify parsing of 13-byte real-time steps packet."""
    # steps = 1250 (0x04e2), distance = 850m (0x0352), calories = 45 kcal (0x002d)
    packet = bytearray([
        0x01,
        0xe2, 0x04, 0x00, 0x00,
        0x52, 0x03, 0x00, 0x00,
        0x2d, 0x00, 0x00, 0x00,
    ])
    result = parse_realtime_steps(packet)
    assert result["steps"] == 1250
    assert result["distance_meters"] == 850
    assert result["calories"] == 45


def test_parse_realtime_steps_short_packet():
    """Verify handling of truncated steps packets."""
    short_valid = bytearray([0x01, 0x10, 0x27])  # 10000 steps (uint16)
    result = parse_realtime_steps(short_valid)
    assert result["steps"] == 10000
    assert result["distance_meters"] == 0
    assert result["calories"] == 0

    with pytest.raises(ValueError):
        parse_realtime_steps(bytearray([0x01]))


def test_parse_raw_sensor_data_valid():
    """Verify parsing of a real-shaped accelerometer frame (type 0x00)."""
    # type 0x00 (accelerometer), seq 5, 4096 LSB/g
    # sample 1: (0, 2000, 3570) -> a ~1 g resting vector
    # sample 2: (-2048, 0, 0)   -> -0.5 g along x
    payload = bytearray([0x00, 5])
    payload += struct.pack("<hhh", 0, 2000, 3570)
    payload += struct.pack("<hhh", -2048, 0, 0)

    result = parse_raw_sensor_data(payload)
    assert result["type"] == 0x00
    assert result["sequence"] == 5
    assert result["sample_count"] == 2

    s0 = result["samples"][0]
    assert s0["x_raw"] == 0
    assert s0["y_raw"] == 2000
    assert s0["z_raw"] == 3570
    assert s0["az_g"] == pytest.approx(3570 / 4096, abs=1e-4)

    s1 = result["samples"][1]
    assert s1["ax_g"] == pytest.approx(-0.5)
    assert s1["ay_g"] == 0.0


def test_parse_raw_sensor_data_ignores_non_accel_frames():
    """Optical (0x01) and status (0x07) frames carry no accelerometer samples."""
    # 66-byte optical frame as captured from real hardware
    optical = bytes([0x01, 0x01]) + bytes(64)
    result = parse_raw_sensor_data(optical)
    assert result["type"] == 0x01
    assert result["sequence"] == 1
    assert result["sample_count"] == 0
    assert result["samples"] == []

    status = bytes([0x07, 0x27, 0x00, 0xF0, 0x01, 0x71, 0x05, 0x00, 0x00, 0x00, 0x00])
    result = parse_raw_sensor_data(status)
    assert result["type"] == 0x07
    assert result["samples"] == []


def test_parse_raw_sensor_data_invalid():
    """Verify error checking on malformed raw sensor packets."""
    with pytest.raises(ValueError):
        parse_raw_sensor_data(bytearray([0x00]))

    # Accelerometer payload length not divisible by 6 (5 payload bytes)
    with pytest.raises(ValueError):
        parse_raw_sensor_data(bytearray([0x00, 0x00, 1, 2, 3, 4, 5]))
