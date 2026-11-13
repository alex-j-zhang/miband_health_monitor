"""Band history time encoding and record trimming."""

from __future__ import annotations

from datetime import datetime

from band_history import (
    SPO2_RECORD_SIZE,
    SPO2_SLEEP_RECORD_SIZE,
    FetchDataType,
    _trim_to_records,
    decode_fetch_time,
    decode_short_calendar,
    encode_fetch_time,
    encode_short_calendar,
    parse_spo2_sleep,
)
from sim_band import _spo2_sleep_record


def test_short_calendar_roundtrip():
    moment = datetime(2026, 2, 3, 4, 5)
    raw = encode_short_calendar(moment)
    assert len(raw) == 6
    assert decode_short_calendar(raw).replace(tzinfo=None) == moment

    fetch_bytes = encode_fetch_time(moment)
    assert len(fetch_bytes) == 8
    assert decode_fetch_time(fetch_bytes).replace(tzinfo=None) == moment


def test_trim_to_records_uses_the_sleep_record_size():
    """A padded final chunk must be trimmed at 30 bytes, not 65."""
    payload = b"\x02" + _spo2_sleep_record(1577836800, 98, 60) + b"\xaa" * 7
    trimmed = _trim_to_records(FetchDataType.SPO2_SLEEP, payload)
    assert len(trimmed) == 1 + SPO2_SLEEP_RECORD_SIZE
    assert len(parse_spo2_sleep(trimmed)) == 1

    normal = b"\x02" + b"\x11" * SPO2_RECORD_SIZE + b"\xbb" * 3
    assert len(_trim_to_records(FetchDataType.SPO2_NORMAL, normal)) == 1 + SPO2_RECORD_SIZE
