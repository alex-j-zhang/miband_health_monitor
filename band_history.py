"""Experimental historical-data fetch for Mi Band 5/6 (Huami "fetch data" protocol).

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
   (2026-11-08: 12901 B = 1 + 430 x 30, version 2, strictly increasing
   timestamps, all SpO2 values within 80..100; a byte-exact prefix is pinned in
   ``tests/test_history.py``). The *semantics* of ``duration`` and the three
   trailing byte groups are still unknown, and upstream Gadgetbridge still marks
   its handler ``TODO save``. It therefore stays *opt-in* and ``[experimental]``,
   written to its OWN ``spo2_sleep`` table -- never the point ``spo2`` table.
   Enable it with
   ``include_spo2_sleep=True`` / ``--sync-sleep-spo2``.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from typing import Callable, Optional

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


class HistoryFetchError(RuntimeError):
    """Raised when the band-side history fetch fails or is out of sync."""


class HistoryParseError(ValueError):
    """Raised when a fetched payload cannot be decoded."""


# ---------------------------------------------------------------------------
# Time encoding (the band expects local wall-clock time, minute precision)
# ---------------------------------------------------------------------------
def _local_tz():
    return datetime.now().astimezone().tzinfo or timezone.utc


def encode_short_calendar(moment: datetime) -> bytes:
    """Encode a datetime as 6 bytes: year LE16, month, day, hour, minute."""
    local = moment.astimezone(_local_tz()) if moment.tzinfo else moment
    return struct.pack(
        "<HBBBB",
        local.year,
        local.month,
        local.day,
        local.hour,
        local.minute,
    )


def decode_short_calendar(raw: bytes) -> datetime:
    """Decode the 6-byte short calendar into a local-time datetime."""
    if len(raw) < 6:
        raise HistoryParseError(f"short calendar needs 6 bytes, got {len(raw)}")
    year, month, day, hour, minute = struct.unpack("<HBBBB", raw[:6])
    return datetime(year, month, day, hour, minute, tzinfo=_local_tz())


def encode_fetch_time(moment: datetime) -> bytes:
    """Encode the fetch "since" time: 6-byte calendar + reason byte + tz."""
    local = moment.astimezone(_local_tz()) if moment.tzinfo else moment
    offset = local.utcoffset() or timedelta(0)
    tz_quarters = int(offset.total_seconds() // 900)
    tz_quarters = max(-128, min(127, tz_quarters))
    return encode_short_calendar(moment) + bytes([0x00, tz_quarters & 0xFF])


def decode_fetch_time(raw: bytes) -> datetime:
    """Decode the fetch timestamp returned by the band."""
    if len(raw) < 6:
        raise HistoryParseError(f"fetch timestamp needs >= 6 bytes, got {len(raw)}")
    return decode_short_calendar(raw[:6])


def _resolve_start_timestamp(metadata: bytes, fallback: datetime) -> float:
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
        decoded = decode_fetch_time(metadata[7:])
    except ValueError:
        return fallback_ts
    if decoded.year < 2000:
        return fallback_ts
    try:
        return decoded.timestamp()
    except (OSError, OverflowError, ValueError):
        return fallback_ts


def _format_ts(timestamp: float) -> str:
    """Format a timestamp for logs without ever raising on odd values."""
    try:
        return datetime.fromtimestamp(timestamp).isoformat()
    except (OSError, OverflowError, ValueError):
        return f"<invalid timestamp {timestamp!r}>"


def _timestamp_usable(timestamp: float) -> bool:
    """Reject timestamps a 2020s-era band can never legitimately report."""
    return 946684800 <= timestamp <= time.time() + 86400  # 2000-01-01 .. now+1d


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
       Layout verified on a Mi Band 6 (2026-11-08); the meaning of ``duration``
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


# ---------------------------------------------------------------------------
# Low-level fetcher
# ---------------------------------------------------------------------------
class BandHistoryFetcher:
    """Drive the fetch-data characteristic pair for one data type at a time."""

    def __init__(self, client, timeout: float = DEFAULT_TIMEOUT) -> None:
        # Accept either a raw ``BleakClient`` or a ``MiBand6Client`` wrapper.
        underlying = getattr(client, "client", None)
        if underlying is not None and hasattr(underlying, "start_notify"):
            client = underlying
        self.client = client
        self.timeout = timeout
        self._fetch_queue: asyncio.Queue = asyncio.Queue()
        self._data_queue: asyncio.Queue = asyncio.Queue()

    # -- notification plumbing
    def _on_fetch_notification(self, _sender, data: bytearray) -> None:
        self._fetch_queue.put_nowait(bytes(data))

    def _on_data_notification(self, _sender, data: bytearray) -> None:
        self._data_queue.put_nowait(bytes(data))

    def _resolve_client(self) -> None:
        """Bind the underlying ``BleakClient`` lazily.

        A ``MiBand6Client`` wrapper only owns its ``BleakClient`` once it has
        connected, so a fetcher constructed beforehand would keep the wrapper
        and fail at the first ``start_notify`` with ``AttributeError``.  Resolve
        again on every open so both construction orders work.
        """
        if hasattr(self.client, "start_notify"):
            return
        underlying = getattr(self.client, "client", None)
        if underlying is not None and hasattr(underlying, "start_notify"):
            self.client = underlying

    async def _open(self) -> None:
        self._resolve_client()
        if not hasattr(self.client, "start_notify"):
            raise HistoryFetchError(
                "BLE client is not connected yet; call connect_and_authenticate() "
                "before fetching history"
            )
        await self.client.start_notify(UUID_CHAR_FETCH, self._on_fetch_notification)
        await self.client.start_notify(UUID_CHAR_ACTIVITY_DATA, self._on_data_notification)

    async def _close(self) -> None:
        for uuid in (UUID_CHAR_FETCH, UUID_CHAR_ACTIVITY_DATA):
            try:
                await self.client.stop_notify(uuid)
            except Exception as exc:  # pragma: no cover - backend specific
                logger.debug("Error while stopping %s notifications: %s", uuid, exc)

    async def _next(self, queue: asyncio.Queue, what: str) -> bytes:
        try:
            return await asyncio.wait_for(queue.get(), timeout=self.timeout)
        except asyncio.TimeoutError:
            raise HistoryFetchError(f"timed out waiting for {what}") from None

    def _drain(self) -> None:
        """Discard packets left over from a previous fetch round."""
        for queue in (self._fetch_queue, self._data_queue):
            while not queue.empty():
                queue.get_nowait()

    async def _collect(self, expected_length: int) -> tuple[bytes, bytes]:
        """Read activity-data chunks until the band reports completion.

        ``expected_length`` is deliberately *not* used to decide how many bytes
        to read: on real hardware it is a minute count for ``ACTIVITY`` and the
        band always streams whole chunks, so the completion response
        (``10 02 xx``) is the only reliable end-of-data signal.
        """
        buffer = bytearray()
        last_counter: Optional[int] = None

        def append(packet: bytes) -> None:
            nonlocal last_counter
            if not packet:
                raise HistoryFetchError("received empty activity data chunk")
            counter = packet[0]
            if last_counter is None:
                if counter != 0:
                    logger.debug(
                        "First chunk starts at counter %d "
                        "(band does not reset it per fetch)",
                        counter,
                    )
            elif counter != ((last_counter + 1) & 0xFF):
                raise HistoryFetchError(
                    f"packet counter mismatch: got {counter}, "
                    f"expected {(last_counter + 1) & 0xFF}"
                )
            last_counter = counter
            buffer.extend(packet[1:])
            logger.debug("History chunk seq=%d (%d bytes)", counter, len(packet))
            if len(buffer) > MAX_PAYLOAD_BYTES:
                raise HistoryFetchError(
                    f"history payload exceeded {MAX_PAYLOAD_BYTES} bytes"
                )

        while True:
            data_task = asyncio.ensure_future(self._data_queue.get())
            fetch_task = asyncio.ensure_future(self._fetch_queue.get())
            done, pending = await asyncio.wait(
                {data_task, fetch_task},
                timeout=self.timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if not done:
                raise HistoryFetchError("timed out waiting for activity data chunk")

            if data_task in done:
                append(data_task.result())

            if fetch_task in done:
                response = fetch_task.result()
                if (
                    len(response) >= 2
                    and response[0] == RESPONSE
                    and response[1] == COMMAND_FETCH_DATA
                ):
                    # The final chunk can already be queued when completion is
                    # processed (both wake up in the same loop iteration), so
                    # drain before returning.
                    await asyncio.sleep(POST_COMPLETION_GRACE)
                    while not self._data_queue.empty():
                        append(self._data_queue.get_nowait())
                    return bytes(buffer), response
                logger.debug("Ignoring unexpected fetch response: %s", response.hex())

    # -- protocol steps
    async def _write(self, payload: bytes) -> None:
        await self.client.write_gatt_char(UUID_CHAR_FETCH, payload, response=False)

    async def _send_ack(self) -> None:
        await self._write(bytes([COMMAND_ACK]))
        try:
            response = await self._next(self._fetch_queue, "ack response")
        except HistoryFetchError:
            logger.warning("No ack response received from band (continuing)")
            return
        if len(response) >= 2 and response[0] == RESPONSE and response[1] == COMMAND_ACK:
            logger.debug("Ack response: %s", response.hex())
            if len(response) >= 3 and response[2] != SUCCESS:
                raise HistoryFetchError(
                    f"band rejected activity-data ack (status 0x{response[2]:02x})"
                )
        else:
            logger.debug("Unexpected ack response: %s", response.hex())

    async def fetch_payload(
        self,
        data_type: FetchDataType,
        since: datetime,
    ) -> tuple[bytes, float]:
        """Fetch one history block; returns ``(payload, start_timestamp)``."""
        # A previous round may have left notifications queued (the band pads its
        # last chunk, so extra packets can arrive after we stop reading).
        self._drain()
        await self._open()
        try:
            logger.debug(
                "Fetching history type %#04x since %s", int(data_type), since.isoformat()
            )
            start_command = bytes([COMMAND_START_DATE, int(data_type) & 0xFF]) + encode_fetch_time(
                since
            )
            await self._write(start_command)

            metadata = await self._next(self._fetch_queue, "start-date response")
            logger.debug("Start-date response: %s", metadata.hex())
            if len(metadata) < 3 or metadata[0] != RESPONSE or metadata[1] != COMMAND_START_DATE:
                raise HistoryFetchError(f"unexpected start-date response: {metadata.hex()}")
            if metadata[2] != SUCCESS:
                raise HistoryFetchError(
                    f"band rejected fetch type {int(data_type):#04x} "
                    f"(status 0x{metadata[2]:02x})"
                )
            if len(metadata) < 7:
                raise HistoryFetchError(f"truncated start-date response: {metadata.hex()}")

            expected_length = int.from_bytes(metadata[3:7], "little")
            start_timestamp = _resolve_start_timestamp(metadata, since)
            # ACTIVITY reports a minute count here, not a byte count (verified on
            # hardware: 960 "bytes" for a 16-hour window), so the number is only
            # informational -- _collect() ends on the completion response.
            unit = "minute(s)" if data_type == FetchDataType.ACTIVITY else "byte(s)"
            logger.debug(
                "Band reports %d %s of history starting at %s",
                expected_length,
                unit,
                _format_ts(start_timestamp),
            )

            if expected_length == 0:
                await self._send_ack()
                return b"", start_timestamp

            await self._write(bytes([COMMAND_FETCH_DATA]))

            raw, completion = await self._collect(expected_length)
            logger.debug("Fetch completion response: %s", completion.hex())
            payload = _trim_to_records(data_type, raw)

            if (
                data_type in (FetchDataType.SPO2_NORMAL, FetchDataType.SPO2_SLEEP)
                and expected_length
                and len(raw) < expected_length
            ):
                raise HistoryFetchError(
                    f"incomplete payload: got {len(raw)} of {expected_length} byte(s)"
                )

            if not payload:
                await self._send_ack()
                return b"", start_timestamp

            if len(completion) < 3 or completion[0] != RESPONSE or completion[1] != COMMAND_FETCH_DATA:
                raise HistoryFetchError(f"unexpected completion response: {completion.hex()}")
            if completion[2] != SUCCESS:
                raise HistoryFetchError(
                    f"band reported fetch failure (status 0x{completion[2]:02x})"
                )

            if len(completion) >= 7:
                expected_crc = int.from_bytes(completion[3:7], "little")
                candidates = [payload]
                if 0 < expected_length <= len(raw):
                    candidates.append(raw[:expected_length])
                if all((zlib.crc32(c) & 0xFFFFFFFF) != expected_crc for c in candidates):
                    raise HistoryFetchError(
                        f"payload CRC32 mismatch (expected {expected_crc:#010x})"
                    )

            await self._send_ack()
            logger.debug(
                "Fetched payload OK: %d bytes (raw %d), crc=%s",
                len(payload),
                len(raw),
                "verified" if len(completion) >= 7 else "absent",
            )
            return payload, start_timestamp
        finally:
            await self._close()


# ---------------------------------------------------------------------------
# High-level sync loop
# ---------------------------------------------------------------------------
@dataclass
class HistorySyncResult:
    """Summary of one history synchronisation run."""

    spo2: list[dict] = field(default_factory=list)
    activity: list[dict] = field(default_factory=list)
    #: Opt-in stream with its own table (layout verified, field semantics unknown).
    spo2_sleep: list[dict] = field(default_factory=list)
    rounds: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def total_samples(self) -> int:
        return len(self.spo2) + len(self.activity) + len(self.spo2_sleep)


async def sync_history(
    client,
    *,
    days: int = 7,
    include_spo2: bool = True,
    include_activity: bool = True,
    include_spo2_sleep: bool = False,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    timeout: float = DEFAULT_TIMEOUT,
    on_spo2: Optional[Callable[[dict], None]] = None,
    on_activity: Optional[Callable[[dict], None]] = None,
    on_spo2_sleep: Optional[Callable[[dict], None]] = None,
) -> HistorySyncResult:
    """Fetch up to *days* of band-side history, repeating while data is stale.

    Fetched samples are returned and optionally forwarded to the ``on_*``
    callbacks (used to persist them). A failure for one data type does not
    abort the other; errors are collected in :attr:`HistorySyncResult.errors`.

    ``include_spo2_sleep`` requests type ``0x26``. Its 30-byte layout is
    hardware-verified but the trailing fields are undocumented, so it stays off
    by default and consumers should treat those samples as capture material,
    not as validated measurements.
    """
    result = HistorySyncResult()
    if not (include_spo2 or include_activity or include_spo2_sleep):
        return result

    fetcher = BandHistoryFetcher(client, timeout=timeout)
    since = (datetime.now() - timedelta(days=max(0, int(days)))).replace(
        second=0, microsecond=0
    )
    now_ts = time.time()

    tasks: list[tuple[FetchDataType, str, Callable[[list[dict]], None]]] = []
    if include_activity:
        tasks.append((FetchDataType.ACTIVITY, "activity", _extend_activity))
    if include_spo2:
        tasks.append((FetchDataType.SPO2_NORMAL, "spo2", _extend_spo2))
    if include_spo2_sleep:
        tasks.append((FetchDataType.SPO2_SLEEP, "spo2_sleep", _extend_spo2_sleep))

    consumers: dict[FetchDataType, Optional[Callable[[dict], None]]] = {
        FetchDataType.ACTIVITY: on_activity,
        FetchDataType.SPO2_NORMAL: on_spo2,
        FetchDataType.SPO2_SLEEP: on_spo2_sleep,
    }

    for data_type, name, append in tasks:
        current = since
        previous_last_ts: Optional[float] = None
        result.rounds.setdefault(name, 0)
        for _round in range(max(1, max_rounds)):
            try:
                payload, start_timestamp = await fetcher.fetch_payload(data_type, current)
            except Exception as exc:
                message = f"{name} fetch failed: {exc}"
                logger.error(message)
                result.errors.append(message)
                break

            if not payload:
                break
            try:
                samples = _parse_payload(data_type, payload, start_timestamp)
            except HistoryParseError as exc:
                message = f"{name} payload parse failed: {exc}"
                logger.error(message)
                result.errors.append(message)
                break

            if not samples:
                break
            last_ts = max(sample["ts"] for sample in samples)
            if not _timestamp_usable(last_ts):
                message = f"{name} returned an out-of-range timestamp ({last_ts!r})"
                logger.error(message)
                result.errors.append(message)
                break
            if previous_last_ts is not None and last_ts <= previous_last_ts:
                logger.info("No forward progress in %s fetch; stopping.", name)
                break
            previous_last_ts = last_ts

            result.rounds[name] += 1
            append(result, samples)

            callback = consumers.get(data_type)
            if callback is not None:
                for sample in samples:
                    try:
                        callback(sample)
                    except Exception:  # pragma: no cover - consumer errors must not abort
                        logger.exception("History sample consumer failed")

            logger.info(
                "Fetched %d %s sample(s) up to %s", len(samples), name,
                _format_ts(last_ts),
            )
            if last_ts <= current.timestamp() + 60 or last_ts >= now_ts - 60:
                break
            # The band encodes "since" with minute precision: advance to the
            # next whole minute to avoid fetching the same sample twice.
            current = (datetime.fromtimestamp(last_ts) + timedelta(minutes=1)).replace(
                second=0, microsecond=0
            )

    return result


def _extend_activity(result: HistorySyncResult, samples: list[dict]) -> None:
    result.activity.extend(samples)


def _extend_spo2(result: HistorySyncResult, samples: list[dict]) -> None:
    result.spo2.extend(samples)


def _extend_spo2_sleep(result: HistorySyncResult, samples: list[dict]) -> None:
    result.spo2_sleep.extend(samples)


def summarize_history_result(result: HistorySyncResult) -> str:
    """Human-readable one-line summary used by the CLI."""
    if not result.total_samples and not result.errors:
        return "no new history data"
    parts = [f"{len(result.activity)} activity sample(s)", f"{len(result.spo2)} SpO2 sample(s)"]
    # Only mention the opt-in type when it actually ran, so the default log line
    # stays byte-for-byte compatible with existing tooling and tests.
    if result.spo2_sleep or "spo2_sleep" in result.rounds:
        parts.append(f"{len(result.spo2_sleep)} sleep SpO2 sample(s) [experimental]")
    if result.rounds:
        parts.append(
            "rounds=" + ",".join(f"{name}:{count}" for name, count in sorted(result.rounds.items()))
        )
    if result.errors:
        parts.append(f"errors={len(result.errors)}")
    return ", ".join(parts)
