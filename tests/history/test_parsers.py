"""Byte-level parsers for the band history payloads (SpO2 and activity)."""

from __future__ import annotations

import pytest

from band_history import (
    ACTIVITY_SAMPLE_SIZE,
    SPO2_RECORD_SIZE,
    SPO2_SLEEP_RECORD_SIZE,
    HistoryParseError,
    parse_activity,
    parse_spo2_normal,
    parse_spo2_sleep,
)
from sim_band import _spo2_sleep_record


def test_parse_spo2_normal_payload():
    payload = bytearray([0x02])
    payload += (1000).to_bytes(4, "little") + bytes([97]) + b"\x00" * 60
    # Automatic measurement: signed byte is the value minus 128 (96 -> -32 / 0xE0).
    payload += (1060).to_bytes(4, "little") + bytes([(96 - 128) & 0xFF]) + b"\x00" * 60

    samples = parse_spo2_normal(bytes(payload))
    assert len(samples) == 2
    assert samples[0] == {"ts": 1000.0, "spo2": 97, "auto_measure": False}
    assert samples[1]["spo2"] == 96
    assert samples[1]["auto_measure"] is True


def test_parse_spo2_rejects_bad_payloads():
    with pytest.raises(HistoryParseError):
        parse_spo2_normal(b"\x03" + b"\x00" * SPO2_RECORD_SIZE)  # wrong version
    with pytest.raises(HistoryParseError):
        parse_spo2_normal(b"\x02" + b"\x00" * (SPO2_RECORD_SIZE - 1))  # bad length


def test_parse_activity_payload():
    payload = bytes(
        [
            0x01, 10, 12, 70, 0, 0, 0, 0,
            0x01, 20, 30, 74, 0, 0, 0, 0,
        ]
    )
    assert len(payload) == 2 * ACTIVITY_SAMPLE_SIZE
    samples = parse_activity(payload, start_timestamp=1000.0)
    assert len(samples) == 2
    assert samples[0]["steps"] == 12
    assert samples[1]["heart_rate"] == 74
    assert samples[1]["ts"] == 1060.0  # one minute later

    with pytest.raises(HistoryParseError):
        parse_activity(b"\x00" * 7, start_timestamp=0.0)


def test_parse_spo2_normal_real_hardware_payload():
    """Decode a byte-exact SpO2 payload captured from a Mi Band 6."""
    tokens = (
        "02 cf 65 ee 6a 64 43 00 00 00 19 00 4e d6 ee 6a 0c be 08 00 4e d6 ee 6a"
    ).split()
    raw = bytes(int(token, 16) for token in tokens) + b"\x00" * 25 + b"\x01" + b"\x00" * 16
    assert len(raw) == 1 + SPO2_RECORD_SIZE

    samples = parse_spo2_normal(raw)
    assert samples == [
        {"ts": 1794008527.0, "spo2": 100, "auto_measure": False},
    ]


def test_parse_spo2_sleep_keeps_the_30_byte_layout():
    """0x26 has its own record size -- sharing the 65-byte normal layout is a bug."""
    assert SPO2_SLEEP_RECORD_SIZE == 30
    record = (
        _spo2_sleep_record(1577836800, 97, 60)[:6]
        + bytes(range(1, 7))       # spo2_high
        + bytes(range(7, 13))      # spo2_low
        + bytes(range(13, 21))     # signal_quality
        + bytes(range(21, 25))     # extend
    )
    assert len(record) == 30
    payload = b"\x02" + record + _spo2_sleep_record(1577836860, 95, 30)

    samples = parse_spo2_sleep(payload)
    assert len(samples) == 2
    assert samples[0] == {
        "ts": 1577836800.0,
        "spo2": 97,
        "duration": 60,
        "spo2_high": [1, 2, 3, 4, 5, 6],
        "spo2_low": [7, 8, 9, 10, 11, 12],
        "signal_quality": [13, 14, 15, 16, 17, 18, 19, 20],
        "extend": [21, 22, 23, 24],
        "source": "sleep",
    }
    assert samples[1]["spo2"] == 95
    assert samples[1]["ts"] == 1577836860.0


def test_parse_spo2_sleep_synthetic_fixture():
    """Byte-exact synthetic fixture for the verified 0x26 layout.

    No captured bytes live in the repository: real payloads stay in the ignored
    ``validation/`` directory. This fixture is hand-built from the documented
    layout (version 2 + N x 30 B, records 60 s apart) and pins every field
    offset, so the parser cannot silently drift from the spec.
    """
    raw = bytes.fromhex(
        "02"
        "00e10b5e6110626262000000616161000000010203040506070800000000"
        "3ce10b5e6220636363000000626262000000080706050403020101020304"
    )
    assert len(raw) == 1 + 2 * SPO2_SLEEP_RECORD_SIZE

    samples = parse_spo2_sleep(raw)
    assert len(samples) == 2
    assert samples[0]["spo2"] == 97
    assert samples[0]["duration"] == 16
    assert samples[0]["spo2_high"] == [98, 98, 98, 0, 0, 0]
    assert samples[0]["spo2_low"] == [97, 97, 97, 0, 0, 0]
    assert samples[0]["signal_quality"] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert samples[0]["extend"] == [0, 0, 0, 0]
    assert samples[1]["spo2"] == 98
    assert samples[1]["duration"] == 32
    assert samples[1]["ts"] - samples[0]["ts"] == 60


def test_parse_spo2_sleep_rejects_bad_payloads():
    assert parse_spo2_sleep(b"") == []
    with pytest.raises(HistoryParseError, match="version"):
        parse_spo2_sleep(b"\x03" + b"\x00" * SPO2_SLEEP_RECORD_SIZE)
    with pytest.raises(HistoryParseError, match="multiple of 30"):
        parse_spo2_sleep(b"\x02" + b"\x00" * 29)
