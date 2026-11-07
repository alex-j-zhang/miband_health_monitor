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

      10 01 01 <expected_len u32 LE> <time bytes...>

   ``expected_len == 0`` means "nothing new"; acknowledge and stop.
4. Write ``[0x02]`` to ``0x0004`` to request the actual data.
5. Band streams chunks on ``0x0005``, each prefixed by an incrementing counter
   byte; the concatenated payload is ``expected_len`` bytes long.
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

.. warning::
   **Experimental.** The protocol flow is implemented against the public
   reverse-engineering results and is verified against the in-memory simulator
   only. Validate against real hardware before relying on the fetched data.
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
SPO2_VERSION = 2
ACTIVITY_SAMPLE_SIZE = 8
DEFAULT_MAX_ROUNDS = 5
DEFAULT_TIMEOUT = 30.0


class FetchDataType(IntEnum):
    """Band-side history types (subset relevant to this project)."""

    ACTIVITY = 0x01
    SPO2_NORMAL = 0x25
    SPO2_SLEEP = 0x26


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
    if data_type == FetchDataType.ACTIVITY:
        return parse_activity(payload, start_timestamp)
    raise HistoryParseError(f"no parser for fetch data type {int(data_type):#04x}")


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

    async def _open(self) -> None:
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
            start_timestamp = (
                decode_fetch_time(metadata[7:]).timestamp() if len(metadata) >= 13 else since.timestamp()
            )
            logger.debug(
                "Band reports %d byte(s) of history starting at %s",
                expected_length,
                datetime.fromtimestamp(start_timestamp).isoformat(),
            )

            if expected_length == 0:
                await self._send_ack()
                return b"", start_timestamp

            await self._write(bytes([COMMAND_FETCH_DATA]))

            buffer = bytearray()
            last_counter = -1
            while len(buffer) < expected_length:
                packet = await self._next(self._data_queue, "activity data chunk")
                if not packet:
                    raise HistoryFetchError("received empty activity data chunk")
                counter = packet[0]
                expected_counter = (last_counter + 1) & 0xFF
                if counter != expected_counter:
                    raise HistoryFetchError(
                        f"packet counter mismatch: got {counter}, expected {expected_counter}"
                    )
                logger.debug("History chunk seq=%d (%d bytes)", counter, len(packet))
                last_counter = counter
                buffer.extend(packet[1:])

            completion = await self._next(self._fetch_queue, "fetch completion")
            logger.debug("Fetch completion response: %s", completion.hex())
            if (
                len(completion) < 3
                or completion[0] != RESPONSE
                or completion[1] != COMMAND_FETCH_DATA
            ):
                raise HistoryFetchError(f"unexpected completion response: {completion.hex()}")
            if completion[2] != SUCCESS:
                raise HistoryFetchError(
                    f"band reported fetch failure (status 0x{completion[2]:02x})"
                )

            payload = bytes(buffer[:expected_length])
            if len(completion) >= 7:
                expected_crc = int.from_bytes(completion[3:7], "little")
                actual_crc = zlib.crc32(payload) & 0xFFFFFFFF
                if expected_crc != actual_crc:
                    raise HistoryFetchError(
                        f"payload CRC32 mismatch (expected {expected_crc:#010x}, "
                        f"got {actual_crc:#010x})"
                    )

            await self._send_ack()
            logger.debug(
                "Fetched payload OK: %d bytes, crc=%s",
                len(payload),
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
    rounds: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def total_samples(self) -> int:
        return len(self.spo2) + len(self.activity)


async def sync_history(
    client,
    *,
    days: int = 7,
    include_spo2: bool = True,
    include_activity: bool = True,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    timeout: float = DEFAULT_TIMEOUT,
    on_spo2: Optional[Callable[[dict], None]] = None,
    on_activity: Optional[Callable[[dict], None]] = None,
) -> HistorySyncResult:
    """Fetch up to *days* of band-side history, repeating while data is stale.

    Fetched samples are returned and optionally forwarded to the ``on_*``
    callbacks (used to persist them). A failure for one data type does not
    abort the other; errors are collected in :attr:`HistorySyncResult.errors`.
    """
    result = HistorySyncResult()
    if not include_spo2 and not include_activity:
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
            if previous_last_ts is not None and last_ts <= previous_last_ts:
                logger.info("No forward progress in %s fetch; stopping.", name)
                break
            previous_last_ts = last_ts

            result.rounds[name] += 1
            append(result, samples)

            callback = on_spo2 if data_type == FetchDataType.SPO2_NORMAL else on_activity
            if callback is not None:
                for sample in samples:
                    try:
                        callback(sample)
                    except Exception:  # pragma: no cover - consumer errors must not abort
                        logger.exception("History sample consumer failed")

            logger.info(
                "Fetched %d %s sample(s) up to %s", len(samples), name,
                datetime.fromtimestamp(last_ts).isoformat(),
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


def summarize_history_result(result: HistorySyncResult) -> str:
    """Human-readable one-line summary used by the CLI."""
    if not result.total_samples and not result.errors:
        return "no new history data"
    parts = [f"{len(result.activity)} activity sample(s)", f"{len(result.spo2)} SpO2 sample(s)"]
    if result.rounds:
        parts.append(
            "rounds=" + ",".join(f"{name}:{count}" for name, count in sorted(result.rounds.items()))
        )
    if result.errors:
        parts.append(f"errors={len(result.errors)}")
    return ", ".join(parts)
