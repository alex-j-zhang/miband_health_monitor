"""Historical-data fetch half of the simulated band.

The simulator answers the same start-date / fetch-data / ack command sequence as
real firmware, including two hardware-observed quirks:

* an empty fetch echoes the epoch calendar (1970-01-01) rather than the
  requested window;
* the payload is streamed in 19-byte chunks with an incrementing counter byte,
  and individual chunks can be dropped to exercise the client's gap detection.
"""

from __future__ import annotations

from datetime import datetime

from miband_monitor.history import (
    COMMAND_ACK,
    COMMAND_FETCH_DATA,
    COMMAND_START_DATE,
    RESPONSE,
    SUCCESS,
    UUID_CHAR_ACTIVITY_DATA,
    UUID_CHAR_FETCH,
    FetchDataType,
    decode_fetch_time,
    encode_fetch_time,
)

from .constants import logger
from .payloads import _activity_payload, _spo2_payload, _spo2_sleep_payload


class HistoryMixin:
    """History-fetch half of the simulated band state machine."""

    # ------------------------------------------------ historical data fetch
    def _on_fetch_write(self, data: bytes) -> None:
        if not data:
            return
        command = data[0]
        if command == COMMAND_START_DATE:
            if len(data) < 2:
                self._emit_later(
                    UUID_CHAR_FETCH, bytes([RESPONSE, COMMAND_START_DATE, 0x02]), 0.001
                )
                return
            data_type = data[1]
            try:
                since = decode_fetch_time(data[2:])
            except Exception:
                since = datetime.now()
            payload, start_timestamp = self._build_history_payload(data_type, since)
            if payload is None:  # unsupported data type
                self._emit_later(
                    UUID_CHAR_FETCH, bytes([RESPONSE, COMMAND_START_DATE, 0x02]), 0.001
                )
                return
            self._fetch_payload = payload
            # Real hardware echoes the requested window when there is data, but
            # answers with the epoch calendar (1970-01-01) when the fetch is
            # empty -- reproduced here so the "nothing new" path is exercised.
            echoed = (
                datetime.fromtimestamp(start_timestamp) if payload else datetime(1970, 1, 1)
            )
            metadata = (
                bytes([RESPONSE, COMMAND_START_DATE, SUCCESS])
                + len(payload).to_bytes(4, "little")
                + encode_fetch_time(echoed)
            )
            self._emit_later(UUID_CHAR_FETCH, metadata, 0.001)
            return

        if command == COMMAND_FETCH_DATA:
            payload = self._fetch_payload
            if not payload:
                self._emit_later(
                    UUID_CHAR_FETCH, bytes([RESPONSE, COMMAND_FETCH_DATA, 0x02]), 0.001
                )
                return
            chunks = [payload[offset : offset + 19] for offset in range(0, len(payload), 19)]
            for index, chunk in enumerate(chunks):
                if index in self.drop_history_packets:
                    logger.warning("Simulated band: dropping history packet %d", index)
                    continue
                packet = bytes([index & 0xFF]) + chunk
                self._emit_later(UUID_CHAR_ACTIVITY_DATA, packet, 0.002 * (index + 1))
            self._emit_later(
                UUID_CHAR_FETCH,
                bytes([RESPONSE, COMMAND_FETCH_DATA, SUCCESS]),
                0.002 * (len(chunks) + 1),
            )
            self._fetch_data_sent = True
            return

        if command == COMMAND_ACK:
            self._emit_later(UUID_CHAR_FETCH, bytes([RESPONSE, COMMAND_ACK]), 0.001)
            self._fetch_payload = b""
            self._fetch_data_sent = False

    def _build_history_payload(
        self, data_type: int, since: datetime
    ) -> tuple[bytes | None, float]:
        """Build the payload a real band would return for *data_type* since *since*."""
        since_ts = since.timestamp()
        try:
            fetch_type = FetchDataType(data_type)
        except ValueError:
            return None, since_ts

        if fetch_type == FetchDataType.SPO2_NORMAL:
            samples = [s for s in self.history_spo2 if float(s["ts"]) >= since_ts]
            if not samples:
                return b"", since_ts
            return _spo2_payload(samples), float(min(s["ts"] for s in samples))

        if fetch_type == FetchDataType.SPO2_SLEEP:
            samples = [s for s in self.history_spo2_sleep if float(s["ts"]) >= since_ts]
            if not samples:
                return b"", since_ts
            return _spo2_sleep_payload(samples), float(min(s["ts"] for s in samples))

        if fetch_type == FetchDataType.ACTIVITY:
            samples = [s for s in self.history_activity if float(s["ts"]) >= since_ts]
            if not samples:
                return b"", since_ts
            return _activity_payload(samples), float(min(s["ts"] for s in samples))

        return None, since_ts
