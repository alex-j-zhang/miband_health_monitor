"""Decoders for the BLE notifications the Mi Band 6 emits.

The payload layouts are hardware-verified (see the docstrings below and
docs/validation/history-hardware.md). Parsers are pure functions: they raise
ValueError for structurally impossible payloads and never touch BLE state, so
they can be unit-tested and reused by the simulator.

Accelerometer scaling uses miband_monitor.config.health.STANDARD_GRAVITY so the
live stream and every analysis consumer agree on the g -> m/s^2 factor.
"""

from __future__ import annotations

import struct

from ..config.health import STANDARD_GRAVITY
from .protocol import RAW_SENSOR_ACCEL_LSB_PER_G, RAW_SENSOR_TYPE_ACCEL


def parse_heart_rate(data: bytearray) -> int:
    """Parse standard Bluetooth SIG Heart Rate Measurement payload."""
    if len(data) < 2:
        raise ValueError("Invalid heart rate measurement data length.")

    flags = data[0]
    # Bit 0 determines format: 0 -> uint8, 1 -> uint16
    if flags & 0x01 == 0:
        return int(data[1])
    return int.from_bytes(data[1:3], byteorder="little")


def parse_realtime_steps(data: bytearray | bytes) -> dict:
    """Parse real-time steps payload from characteristic 0x0007.

    Typical 13-byte Huami format:
    data[0]: status / mode
    data[1:5]: current step count (uint32 little-endian, or uint16 at [1:3])
    data[5:9]: meters / distance (uint32 little-endian)
    data[9:13]: calories burned (uint32 little-endian)
    """
    if len(data) < 3:
        raise ValueError(f"Real-time steps data too short: {len(data)} bytes")

    if len(data) >= 5:
        steps = int.from_bytes(data[1:5], byteorder="little")
    else:
        steps = int.from_bytes(data[1:3], byteorder="little")

    distance_meters = int.from_bytes(data[5:9], byteorder="little") if len(data) >= 9 else 0
    calories = int.from_bytes(data[9:13], byteorder="little") if len(data) >= 13 else 0

    return {
        "steps": steps,
        "distance_meters": distance_meters,
        "calories": calories,
    }


def parse_raw_sensor_data(data: bytearray | bytes) -> dict:
    """Parse one notification from the raw-sensor characteristic (0x0002).

    The band multiplexes several streams on this characteristic, each prefixed
    with a one-byte frame type and a rolling sequence counter:

    * 0x00 - 3-axis accelerometer, 17-18 samples per frame at ~25 Hz;
    * 0x01 - optical (PPG) channel, not accelerometer data;
    * 0x07 - short periodic status frame.

    Accelerometer frames are laid out as::

        byte 0     frame type (0x00)
        byte 1     sequence counter (0-255 rolling)
        bytes 2..  6-byte triplets: signed int16 LE x, y, z at 4096 LSB/g

    Frames of any other type are returned with an empty samples list so callers
    can ignore them instead of treating them as parse failures.
    """
    if len(data) < 2:
        raise ValueError(f"Raw sensor payload too short: {len(data)} bytes")

    sensor_type = data[0]
    seq_index = data[1]

    if sensor_type != RAW_SENSOR_TYPE_ACCEL:
        return {
            "type": sensor_type,
            "sequence": seq_index,
            "sample_count": 0,
            "samples": [],
        }

    payload = data[2:]

    if len(payload) % 6 != 0:
        raise ValueError(f"Sensor payload length ({len(payload)}) not divisible by 6")

    scale_factor = RAW_SENSOR_ACCEL_LSB_PER_G
    gravity = STANDARD_GRAVITY

    samples = []
    for i in range(0, len(payload), 6):
        x, y, z = struct.unpack("<hhh", payload[i : i + 6])
        samples.append({
            "x_raw": x,
            "y_raw": y,
            "z_raw": z,
            "ax_g": round(x / scale_factor, 4),
            "ay_g": round(y / scale_factor, 4),
            "az_g": round(z / scale_factor, 4),
            "ax_ms2": round((x / scale_factor) * gravity, 4),
            "ay_ms2": round((y / scale_factor) * gravity, 4),
            "az_ms2": round((z / scale_factor) * gravity, 4),
        })

    return {
        "type": sensor_type,
        "sequence": seq_index,
        "sample_count": len(samples),
        "samples": samples,
    }
