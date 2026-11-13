"""In-memory Mi Band 6 simulator and fake bleak backend.

Backward-compatible facade. The implementation now lives in
miband_monitor.simulator, split into constants/payloads/devices/band/auth/
history/streams/fake_bleak/factory. This module keeps the historical import
path and re-exports the protocol names the old module happened to expose.
"""

from __future__ import annotations

from miband_monitor.ble import (
    CHUNKED_ENDPOINT_AUTH,
    UUID_CHAR_CHUNKED_READ,
    UUID_CHAR_CHUNKED_WRITE,
    UUID_CHAR_HR_CONTROL,
    UUID_CHAR_HR_MEASUREMENT,
    UUID_CHAR_RAW_SENSOR_CONTROL,
    UUID_CHAR_RAW_SENSOR_DATA,
    UUID_CHAR_STEPS,
    MiBand6Client,
)
from miband_monitor.crypto import compute_shared_secret, generate_ecdh_keys
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
from miband_monitor.simulator import *  # noqa: F401,F403
from miband_monitor.simulator import __all__ as _package_all
from miband_monitor.simulator.constants import logger

__all__ = list(_package_all) + [
    "CHUNKED_ENDPOINT_AUTH",
    "COMMAND_ACK",
    "COMMAND_FETCH_DATA",
    "COMMAND_START_DATE",
    "FetchDataType",
    "MiBand6Client",
    "RESPONSE",
    "SUCCESS",
    "UUID_CHAR_ACTIVITY_DATA",
    "UUID_CHAR_CHUNKED_READ",
    "UUID_CHAR_CHUNKED_WRITE",
    "UUID_CHAR_FETCH",
    "UUID_CHAR_HR_CONTROL",
    "UUID_CHAR_HR_MEASUREMENT",
    "UUID_CHAR_RAW_SENSOR_CONTROL",
    "UUID_CHAR_RAW_SENSOR_DATA",
    "UUID_CHAR_STEPS",
    "compute_shared_secret",
    "decode_fetch_time",
    "encode_fetch_time",
    "generate_ecdh_keys",
    "logger",
]
