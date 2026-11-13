"""Low-level driver for the band-side "fetch data" characteristic pair.

:class:`BandHistoryFetcher` owns one fetch round: it subscribes to the two
history characteristics, sends the start-date / fetch-data / ack commands and
assembles the streamed activity-data chunks into one payload. Parsing and the
high-level retry loop live in :mod:`parsers` and :mod:`sync`.
"""

from __future__ import annotations

import asyncio
import zlib
from datetime import datetime, tzinfo
from typing import Optional

from .models import HistoryFetchError
from .parsers import _trim_to_records
from .protocol import (
    COMMAND_ACK,
    COMMAND_FETCH_DATA,
    COMMAND_START_DATE,
    DEFAULT_TIMEOUT,
    MAX_PAYLOAD_BYTES,
    POST_COMPLETION_GRACE,
    RESPONSE,
    SUCCESS,
    UUID_CHAR_ACTIVITY_DATA,
    UUID_CHAR_FETCH,
    FetchDataType,
    _format_ts,
    _resolve_start_timestamp,
    encode_fetch_time,
    logger,
    resolve_band_tz,
)


# ---------------------------------------------------------------------------
# Low-level fetcher
# ---------------------------------------------------------------------------
class BandHistoryFetcher:
    """Drive the fetch-data characteristic pair for one data type at a time."""

    def __init__(
        self,
        client,
        timeout: float = DEFAULT_TIMEOUT,
        *,
        band_tz: Optional[str | tzinfo] = None,
    ) -> None:
        # Accept either a raw ``BleakClient`` or a ``MiBand6Client`` wrapper.
        underlying = getattr(client, "client", None)
        if underlying is not None and hasattr(underlying, "start_notify"):
            client = underlying
        self.client = client
        self.timeout = timeout
        self.band_tz = resolve_band_tz(band_tz)
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
                since, tz=self.band_tz
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
            start_timestamp = _resolve_start_timestamp(metadata, since, tz=self.band_tz)
            # ACTIVITY reports a minute count here, not a byte count (verified on
            # hardware: 960 "bytes" for a 16-hour window), so the number is only
            # informational -- _collect() ends on the completion response.
            unit = "minute(s)" if data_type == FetchDataType.ACTIVITY else "byte(s)"
            logger.debug(
                "Band reports %d %s of history starting at %s",
                expected_length,
                unit,
                _format_ts(start_timestamp, tz=self.band_tz),
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
