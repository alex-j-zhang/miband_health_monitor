"""Band-side historical data: fetch protocol, payload parsers and sync loop.

Public surface (re-exported so legacy import sites and tests keep working):

* protocol: UUIDs, fetch command/response bytes, record sizes, :class:`FetchDataType`,
  the short-calendar/fetch-time codecs and the log-formatting helpers;
* parsers: :func:`parse_spo2_normal`, :func:`parse_spo2_sleep`,
  :func:`parse_activity`, plus the internal payload trimmer;
* fetcher: :class:`BandHistoryFetcher` (one fetch round, GATT plumbing);
* sync: :func:`sync_history`, :func:`summarize_history_result` and
  :class:`HistorySyncResult`.

The split keeps the wire codecs dependency-free so both the fetcher and the
in-memory simulator can import them without pulling in a BLE backend.
"""

from .models import HistoryFetchError, HistoryParseError, HistorySyncResult
from .protocol import (
    ACTIVITY_SAMPLE_SIZE,
    COMMAND_ACK,
    COMMAND_FETCH_DATA,
    COMMAND_START_DATE,
    DEFAULT_MAX_ROUNDS,
    DEFAULT_TIMEOUT,
    MAX_PAYLOAD_BYTES,
    POST_COMPLETION_GRACE,
    RESPONSE,
    SPO2_RECORD_SIZE,
    SPO2_SLEEP_RECORD_SIZE,
    SPO2_VERSION,
    SUCCESS,
    UUID_CHAR_ACTIVITY_DATA,
    UUID_CHAR_FETCH,
    FetchDataType,
    _format_ts,
    _local_tz,
    _resolve_start_timestamp,
    _timestamp_usable,
    decode_fetch_time,
    decode_short_calendar,
    encode_fetch_time,
    encode_short_calendar,
    logger,
    resolve_band_tz,
)
from .parsers import (
    _parse_payload,
    _trim_to_records,
    parse_activity,
    parse_spo2_normal,
    parse_spo2_sleep,
)
from .fetcher import BandHistoryFetcher
from .sync import (
    _extend_activity,
    _extend_spo2,
    _extend_spo2_sleep,
    summarize_history_result,
    sync_history,
)

__all__ = [
    # models
    "HistoryFetchError",
    "HistoryParseError",
    "HistorySyncResult",
    # protocol constants / codecs
    "ACTIVITY_SAMPLE_SIZE",
    "COMMAND_ACK",
    "COMMAND_FETCH_DATA",
    "COMMAND_START_DATE",
    "DEFAULT_MAX_ROUNDS",
    "DEFAULT_TIMEOUT",
    "MAX_PAYLOAD_BYTES",
    "POST_COMPLETION_GRACE",
    "RESPONSE",
    "SPO2_RECORD_SIZE",
    "SPO2_SLEEP_RECORD_SIZE",
    "SPO2_VERSION",
    "SUCCESS",
    "UUID_CHAR_ACTIVITY_DATA",
    "UUID_CHAR_FETCH",
    "FetchDataType",
    "_format_ts",
    "_local_tz",
    "_resolve_start_timestamp",
    "_timestamp_usable",
    "decode_fetch_time",
    "decode_short_calendar",
    "encode_fetch_time",
    "encode_short_calendar",
    "logger",
    "resolve_band_tz",
    # parsers
    "_parse_payload",
    "_trim_to_records",
    "parse_activity",
    "parse_spo2_normal",
    "parse_spo2_sleep",
    # fetcher
    "BandHistoryFetcher",
    # sync
    "_extend_activity",
    "_extend_spo2",
    "_extend_spo2_sleep",
    "summarize_history_result",
    "sync_history",
]
