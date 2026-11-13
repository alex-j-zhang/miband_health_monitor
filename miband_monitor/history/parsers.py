"""Payload parsers for the three band-side history formats.

Every parser returns a list of plain dicts with a float ``"ts"`` field (unix
seconds), so downstream consumers can persist them without caring about the
wire layout. Record sizes and version bytes are part of the verified protocol
(see :mod:`miband_monitor.history.protocol`), so a malformed payload raises
:class:`HistoryParseError` instead of producing garbage samples.
"""

from __future__ import annotations

import struct

from .models import HistoryParseError
from .protocol import (
    ACTIVITY_SAMPLE_SIZE,
    SPO2_RECORD_SIZE,
    SPO2_SLEEP_RECORD_SIZE,
    SPO2_VERSION,
    FetchDataType,
    logger,
)


# ---------------------------------------------------------------------------
# Payload parsers
# ---------------------------------------------------------------------------
def parse_spo2_normal(payload: bytes) -> list[dict]:
    """Parse a ``SPO2_NORMAL`` history payload into sample dicts."""
    if not payload:
        return []
    if payload[0] != SPO2_VERSION:
        raise HistoryParseError(f"unsupported SpO2 payload version {payload[0]}")
    body = payload[1:]
    if len(body) % SPO2_RECORD_SIZE != 0:
        raise HistoryParseError(
            f"SpO2 payload length {len(body)} is not a multiple of {SPO2_RECORD_SIZE}"
        )

    samples: list[dict] = []
    for offset in range(0, len(body), SPO2_RECORD_SIZE):
        record = body[offset : offset + SPO2_RECORD_SIZE]
        timestamp = struct.unpack_from("<i", record, 0)[0]
        raw_value = struct.unpack_from("<b", record, 4)[0]
        automatic = raw_value < 0
        spo2 = raw_value + 128 if automatic else raw_value
        samples.append(
            {
                "ts": float(timestamp),
                "spo2": int(spo2),
                "auto_measure": bool(automatic),
            }
        )
    return samples


def parse_spo2_sleep(payload: bytes) -> list[dict]:
    """Parse a ``SPO2_SLEEP`` (0x26) payload into per-record sample dicts.

    Layout is taken from Gadgetbridge's ``FetchSpo2SleepOperation``: one version
    byte (2) followed by N 30-byte records. The trailing byte groups are kept
    verbatim as lists so a captured payload can be re-examined once their exact
    semantics are confirmed; nothing here is interpreted as a measurement.

    .. note::
       Layout verified on a Mi Band 6 (2020-01-08); the meaning of ``duration``
       and of the trailing groups is not. ``spo2`` is an **unsigned** byte here,
       unlike ``SPO2_NORMAL`` where a negative value flags automatic measurement.
    """
    if not payload:
        return []
    if payload[0] != SPO2_VERSION:
        raise HistoryParseError(f"unsupported sleep SpO2 payload version {payload[0]}")
    body = payload[1:]
    if len(body) % SPO2_SLEEP_RECORD_SIZE != 0:
        raise HistoryParseError(
            f"sleep SpO2 payload length {len(body)} is not a multiple of "
            f"{SPO2_SLEEP_RECORD_SIZE}"
        )

    samples: list[dict] = []
    for offset in range(0, len(body), SPO2_SLEEP_RECORD_SIZE):
        record = body[offset : offset + SPO2_SLEEP_RECORD_SIZE]
        timestamp = struct.unpack_from("<i", record, 0)[0]
        samples.append(
            {
                "ts": float(timestamp),
                "spo2": record[4],
                "duration": record[5],
                "spo2_high": list(record[6:12]),
                "spo2_low": list(record[12:18]),
                "signal_quality": list(record[18:26]),
                "extend": list(record[26:30]),
                "source": "sleep",
            }
        )
    return samples


def parse_activity(payload: bytes, start_timestamp: float) -> list[dict]:
    """Parse an ``ACTIVITY`` history payload into per-minute sample dicts."""
    if len(payload) % ACTIVITY_SAMPLE_SIZE != 0:
        raise HistoryParseError(
            f"activity payload length {len(payload)} is not a multiple of "
            f"{ACTIVITY_SAMPLE_SIZE}"
        )
    samples: list[dict] = []
    for index, offset in enumerate(range(0, len(payload), ACTIVITY_SAMPLE_SIZE)):
        (
            kind,
            intensity,
            steps,
            heart_rate,
            unknown1,
            sleep,
            deep_sleep,
            rem_sleep,
        ) = struct.unpack_from("<BBBBBBBB", payload, offset)
        samples.append(
            {
                "ts": float(start_timestamp) + index * 60.0,
                "kind": kind,
                "intensity": intensity,
                "steps": steps,
                "heart_rate": heart_rate,
                "unknown1": unknown1,
                "sleep": sleep,
                "deep_sleep": deep_sleep,
                "rem_sleep": rem_sleep,
            }
        )
    return samples


def _parse_payload(
    data_type: FetchDataType, payload: bytes, start_timestamp: float
) -> list[dict]:
    if data_type == FetchDataType.SPO2_NORMAL:
        return parse_spo2_normal(payload)
    if data_type == FetchDataType.SPO2_SLEEP:
        return parse_spo2_sleep(payload)
    if data_type == FetchDataType.ACTIVITY:
        return parse_activity(payload, start_timestamp)
    raise HistoryParseError(f"no parser for fetch data type {int(data_type):#04x}")


def _trim_to_records(data_type: FetchDataType, payload: bytes) -> bytes:
    """Drop a trailing partial record from an assembled payload.

    The band pads its last chunk to a full BLE packet and, for ``ACTIVITY``,
    reports ``expected_length`` as a *minute count* rather than a byte count
    (verified on a Mi Band 6: 361 declared vs 480 bytes delivered).  The
    assembled buffer therefore routinely ends mid-record.
    """
    if not payload:
        return payload
    if data_type == FetchDataType.ACTIVITY:
        usable = len(payload) - (len(payload) % ACTIVITY_SAMPLE_SIZE)
    elif data_type in (FetchDataType.SPO2_NORMAL, FetchDataType.SPO2_SLEEP):
        if payload[0] != SPO2_VERSION:
            return payload
        record_size = (
            SPO2_SLEEP_RECORD_SIZE
            if data_type == FetchDataType.SPO2_SLEEP
            else SPO2_RECORD_SIZE
        )
        body_length = len(payload) - 1
        usable = 1 + body_length - (body_length % record_size)
    else:
        return payload

    if usable != len(payload):
        logger.warning(
            "Dropping %d trailing byte(s) of %s payload (not a whole record)",
            len(payload) - usable,
            data_type.name,
        )
    return payload[:usable]
