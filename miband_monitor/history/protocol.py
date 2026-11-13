"""Wire protocol constants, time codecs and log formatting for band history.

The band stores per-minute activity samples and SpO2 measurements internally.
They are retrieved over two private characteristics of the Mi Band service:

===========================================  ==================================
``0x0004`` (notify + write)                  fetch control / metadata
``0x0005`` (notify)                          activity data chunks
===========================================  ==================================

Protocol (as reverse-engineered by the Gadgetbridge project, AGPL-3.0; the
flow/constants are re-implemented here from scratch, no code was copied):

1. Subscribe to both characteristics.
2. Write ``[0x01, data_type] + time_bytes`` to ``0x0004`` ("start date").
3. Band answers on ``0x0004``::

      10 01 01 <count u32 LE> <time bytes...>

   ``count == 0`` means "nothing new"; acknowledge and stop. The echoed calendar
   is then the epoch (``1970-01-01``), *not* the requested window.
4. Write ``[0x02]`` to ``0x0004`` to request the actual data.
5. Band streams chunks on ``0x0005``, each prefixed by an incrementing counter
   byte (the counter is *not* reset per fetch). The concatenated payload is only
   bounded by the completion response: on real hardware ``count`` is a **minute**
   count for ``ACTIVITY`` (960 for a 16-hour window), so it can never be used as
   a byte length.
6. Band answers on ``0x0004`` with ``10 02 01`` (optionally + CRC32 u32 LE).
7. Write ``[0x03]`` to ``0x0004`` to acknowledge; band answers ``10 03 ...``.

Payload formats
---------------
* ``SPO2_NORMAL`` (type ``0x25``): 1 version byte (must be ``2``) followed by
  65-byte records: ``int32 LE unix-timestamp``, ``int8 spo2``, 60 spare bytes.
  A negative ``spo2`` byte means an automatic measurement; the value is
  ``raw + 128``.
* ``ACTIVITY`` (type ``0x01``): Mi Band 6 uses 8-byte per-minute samples:
  ``kind, intensity, steps, heart_rate, unknown1, sleep, deep_sleep, rem_sleep``.
* ``SPO2_SLEEP`` (type ``0x26``): 1 version byte (must be ``2``) followed by
  30-byte records: ``int32 LE unix-timestamp``, ``uint8 spo2``, ``uint8 duration``,
  6 + 6 bytes of high/low SpO2 values, 8 bytes signal quality, 4 spare bytes.
  Layout follows Gadgetbridge's ``FetchSpo2SleepOperation`` (AGPL-3.0, protocol
  facts only). Unlike ``SPO2_NORMAL`` the value byte is **unsigned** here.

.. warning::
   **Experimental.** The flow is verified against a real Mi Band 6 (activity and
   ``SPO2_NORMAL`` payloads both decode; ``tests/test_history.py`` carries
   byte-exact captures), but the band-side stores are only partially documented.
   Sample semantics of the spare bytes in an ``ACTIVITY`` record are still unknown.

   ``SPO2_SLEEP``'s 30-byte **layout is verified** on a real Mi Band 6
   (2020-01-08: 12901 B = 1 + 430 x 30, version 2, strictly increasing
   timestamps, all SpO2 values within 80..100; a byte-exact prefix is pinned in
   ``tests/test_history.py``). The *semantics* of ``duration`` and the three
   trailing byte groups are still unknown, and upstream Gadgetbridge still marks
   its handler ``TODO save``. It therefore stays *opt-in* and ``[experimental]``,
   written to its OWN ``spo2_sleep`` table -- never the point ``spo2`` table.
   Enable it with
   ``include_spo2_sleep=True`` / ``--sync-sleep-spo2``.
"""

from __future__ import annotations

import logging
import os
import re
import struct
import time
from datetime import datetime, timedelta, timezone, tzinfo
from enum import IntEnum
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from miband_monitor.config.defaults import ENV_BAND_TZ

from .models import HistoryParseError

logger = logging.getLogger("MiBandHistory")

UUID_CHAR_FETCH = "00000004-0000-3512-2118-0009af100700"
UUID_CHAR_ACTIVITY_DATA = "00000005-0000-3512-2118-0009af100700"

COMMAND_START_DATE = 0x01
COMMAND_FETCH_DATA = 0x02
COMMAND_ACK = 0x03
RESPONSE = 0x10
SUCCESS = 0x01

SPO2_RECORD_SIZE = 65
#: Sleep-SpO2 records are a different, 30-byte layout (see module docstring).
SPO2_SLEEP_RECORD_SIZE = 30
SPO2_VERSION = 2
ACTIVITY_SAMPLE_SIZE = 8
DEFAULT_MAX_ROUNDS = 12
DEFAULT_TIMEOUT = 30.0
#: Hard cap on one fetched payload, so a runaway stream cannot exhaust memory.
MAX_PAYLOAD_BYTES = 65536
#: Short grace period after the completion response to collect a straggler chunk.
POST_COMPLETION_GRACE = 0.05


class FetchDataType(IntEnum):
    """Band-side history types (subset relevant to this project)."""

    ACTIVITY = 0x01
    SPO2_NORMAL = 0x25
    SPO2_SLEEP = 0x26
    #: Sleep sessions (594-byte records with real stage spans and a score).
    #: Gadgetbridge keeps it behind ``supportsSleepScore()``, which a Mi Band 6
    #: does not advertise, and the hardware agrees: the band answers this type
    #: with status success and zero records, so nothing here parses it.
    SLEEP_SESSION = 0x48


# ---------------------------------------------------------------------------
# Time encoding (the band expects local wall-clock time, minute precision)
# ---------------------------------------------------------------------------
_OFFSET_RE = re.compile(r"^([+-])(\d{1,2})(?::?(\d{2}))?$")


def _local_tz() -> tzinfo:
    return datetime.now().astimezone().tzinfo or timezone.utc


def resolve_band_tz(spec: Optional[str | tzinfo] = None) -> tzinfo:
    """Resolve the band's wall-clock timezone from *spec*, ``BAND_TZ`` or host OS."""
    if isinstance(spec, tzinfo):
        return spec
    raw = (spec if spec is not None else os.getenv(ENV_BAND_TZ, "")).strip()
    if not raw:
        return _local_tz()
    if raw.upper() in ("UTC", "Z"):
        return timezone.utc
    match = _OFFSET_RE.match(raw)
    if match:
        sign = 1 if match.group(1) == "+" else -1
        hours = int(match.group(2))
        minutes = int(match.group(3) or 0)
        if hours > 23 or minutes > 59:
            raise ValueError(f"Invalid band timezone offset: {raw!r}")
        return timezone(sign * timedelta(hours=hours, minutes=minutes))
    try:
        return ZoneInfo(raw)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            f"Unknown band timezone {raw!r} (use an IANA name like 'Asia/Shanghai' "
            "or a fixed offset like '+08:00')"
        ) from exc


def encode_short_calendar(moment: datetime, *, tz: Optional[str | tzinfo] = None) -> bytes:
    """Encode a datetime as 6 bytes: year LE16, month, day, hour, minute."""
    band_tz = resolve_band_tz(tz)
    local = moment.astimezone(band_tz) if moment.tzinfo else moment
    return struct.pack(
        "<HBBBB",
        local.year,
        local.month,
        local.day,
        local.hour,
        local.minute,
    )


def decode_short_calendar(raw: bytes, *, tz: Optional[str | tzinfo] = None) -> datetime:
    """Decode the 6-byte short calendar into a band-timezone datetime."""
    if len(raw) < 6:
        raise HistoryParseError(f"short calendar needs 6 bytes, got {len(raw)}")
    year, month, day, hour, minute = struct.unpack("<HBBBB", raw[:6])
    return datetime(year, month, day, hour, minute, tzinfo=resolve_band_tz(tz))


def encode_fetch_time(moment: datetime, *, tz: Optional[str | tzinfo] = None) -> bytes:
    """Encode the fetch "since" time: 6-byte calendar + reason byte + tz."""
    band_tz = resolve_band_tz(tz)
    local = moment.astimezone(band_tz) if moment.tzinfo else moment.replace(tzinfo=band_tz)
    offset = local.utcoffset() or timedelta(0)
    tz_quarters = int(offset.total_seconds() // 900)
    tz_quarters = max(-128, min(127, tz_quarters))
    return encode_short_calendar(local, tz=band_tz) + bytes([0x00, tz_quarters & 0xFF])


def decode_fetch_time(raw: bytes, *, tz: Optional[str | tzinfo] = None) -> datetime:
    """Decode the fetch timestamp returned by the band."""
    if len(raw) < 6:
        raise HistoryParseError(f"fetch timestamp needs >= 6 bytes, got {len(raw)}")
    return decode_short_calendar(raw[:6], tz=tz)


def _resolve_start_timestamp(
    metadata: bytes, fallback: datetime, *, tz: Optional[str | tzinfo] = None
) -> float:
    """Decode the start timestamp the band echoes back, tolerating junk.

    When a fetch comes back empty (``expected_length == 0``) the band zero-fills
    the calendar portion of the response. Decoding that yields 1970-01-01, and
    ``datetime.fromtimestamp()`` raises ``OSError`` on Windows for such values
    (observed on real hardware as ``[Errno 22] Invalid argument``). Anything
    older than 2000 is therefore treated as "no usable timestamp" and the
    requested ``since`` value is returned instead.
    """
    fallback_ts = fallback.timestamp()
    if len(metadata) < 13:
        return fallback_ts
    try:
        decoded = decode_fetch_time(metadata[7:], tz=tz)
    except ValueError:
        return fallback_ts
    if decoded.year < 2000:
        return fallback_ts
    try:
        return decoded.timestamp()
    except (OSError, OverflowError, ValueError):
        return fallback_ts


def _format_ts(timestamp: float, *, tz: Optional[str | tzinfo] = None) -> str:
    """Format a timestamp for logs without ever raising on odd values."""
    try:
        return datetime.fromtimestamp(timestamp, tz=resolve_band_tz(tz)).isoformat()
    except (OSError, OverflowError, ValueError):
        return f"<invalid timestamp {timestamp!r}>"


def _timestamp_usable(timestamp: float) -> bool:
    """Reject timestamps a 2020s-era band can never legitimately report."""
    return 946684800 <= timestamp <= time.time() + 86400  # 2000-01-01 .. now+1d
