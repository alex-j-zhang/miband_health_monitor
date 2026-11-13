"""Packet builders that produce frames shaped exactly like a real Mi Band 6.

Each helper builds one wire message (or a complete history payload) so the
simulator's state machine can stay readable. The byte layouts mirror the
protocol notes in :mod:`miband_monitor.history.protocol`; the accelerometer and
history fixtures are documented inline because their values encode hardware
observations (e.g. 4096 LSB/g for the IMU).
"""

from __future__ import annotations

import struct


# ---------------------------------------------------------------------------
# Notification stream packets
# ---------------------------------------------------------------------------
def _steps_packet(steps: int, distance_m: int, calories: int) -> bytes:
    return (
        b"\x01"
        + steps.to_bytes(4, "little")
        + distance_m.to_bytes(4, "little")
        + calories.to_bytes(4, "little")
    )


def _imu_packet(sequence: int, samples: list[tuple[int, int, int]]) -> bytes:
    """Build an accelerometer frame shaped like a real Mi Band 6 one.

    Type byte 0x00, rolling sequence, then signed int16 LE triplets at
    4096 LSB/g (so (0, 2000, 3570) is a ~1 g resting vector).
    """
    packet = bytearray([0x00, sequence & 0xFF])
    for x, y, z in samples:
        for value in (x, y, z):
            packet.extend(int(value).to_bytes(2, "little", signed=True))
    return bytes(packet)


def _optical_packet(sequence: int, samples: int = 32) -> bytes:
    """Build a non-accelerometer (0x01) frame as seen on real hardware."""
    packet = bytearray([0x01, sequence & 0xFF])
    for index in range(samples):
        packet.extend((25000 + (index * 37) % 900).to_bytes(2, "little"))
    return bytes(packet)


def _status_packet() -> bytes:
    """Build the short 0x07 status frame real bands emit a few times a second."""
    return bytes([0x07, 0x27, 0x00, 0xF0, 0x01, 0x71, 0x05, 0x00, 0x00, 0x00, 0x00])


# ---------------------------------------------------------------------------
# Historical payload fixtures
# ---------------------------------------------------------------------------
def _activity_sample(
    timestamp: float,
    *,
    kind: int = 1,
    intensity: int = 10,
    steps: int = 0,
    heart_rate: int = 0,
    unknown1: int = 0,
    sleep: int = 0,
    deep_sleep: int = 0,
    rem_sleep: int = 0,
) -> dict:
    """Build one per-minute historical activity sample for the simulator."""
    return {
        "ts": float(timestamp),
        "kind": kind,
        "intensity": intensity,
        "steps": steps,
        "heart_rate": heart_rate,
        "unknown1": unknown1,
        "sleep": sleep,
        "deep_sleep": deep_sleep,
        "rem_sleep": rem_sleep,
    }


def _spo2_sleep_record(timestamp: int, spo2: int, duration: int) -> bytes:
    """One 30-byte sleep-SpO2 record (Gadgetbridge FetchSpo2SleepOperation).

    4B ts + 1B spo2 + 1B duration + 6B high + 6B low + 8B signal + 4B spare.
    The trailing groups carry no known semantics yet, so the simulator leaves
    them zeroed; the parser must preserve them verbatim.
    """
    return (
        struct.pack("<i", timestamp)
        + bytes([spo2 & 0xFF, duration & 0xFF])
        + b"\x00" * 6
        + b"\x00" * 6
        + b"\x00" * 8
        + b"\x00" * 4
    )


def _spo2_payload(samples: list[dict]) -> bytes:
    """Assemble a ``SPO2_NORMAL`` payload (version byte + 65-byte records)."""
    payload = bytearray([0x02])  # payload version
    for sample in sorted(samples, key=lambda s: s["ts"]):
        spo2 = int(sample["spo2"])
        if sample.get("auto_measure"):
            raw = spo2 - 128
        else:
            raw = spo2
        payload += struct.pack("<i", int(sample["ts"]))
        payload += struct.pack("<b", raw)
        payload += b"\x00" * 60
    return bytes(payload)


def _spo2_sleep_payload(samples: list[dict]) -> bytes:
    """Assemble an opt-in ``SPO2_SLEEP`` payload (version byte + 30-byte records)."""
    payload = bytearray([0x02])  # payload version
    for sample in sorted(samples, key=lambda s: s["ts"]):
        payload += _spo2_sleep_record(
            int(sample["ts"]),
            int(sample["spo2"]),
            int(sample.get("duration", 60)),
        )
    return bytes(payload)


def _activity_payload(samples: list[dict]) -> bytes:
    """Assemble an ``ACTIVITY`` payload (8-byte per-minute records)."""
    payload = bytearray()
    for sample in sorted(samples, key=lambda s: s["ts"]):
        payload += struct.pack(
            "<BBBBBBBB",
            int(sample.get("kind", 0)),
            int(sample.get("intensity", 0)),
            int(sample.get("steps", 0)),
            int(sample.get("heart_rate", 0)),
            int(sample.get("unknown1", 0)),
            int(sample.get("sleep", 0)),
            int(sample.get("deep_sleep", 0)),
            int(sample.get("rem_sleep", 0)),
        )
    return bytes(payload)
