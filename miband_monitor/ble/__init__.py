"""Mi Band 6 BLE client: GATT protocol, chunked transfer, ECDH auth and data streams.

This package is the refactored home of the historical monolithic
miband_client.py BLE layer. It is split by responsibility:

- protocol: UUIDs, chunked framing sizes, auth sizes, raw-sensor opcodes and
  the demo identity re-exported from miband_monitor.config.demo;
- parsers: pure decoders for heart rate, real-time steps and raw IMU frames;
- identity: auth-key parsing and cross-platform device matching;
- crypto: AES helpers used by the chunked ECDH handshake;
- chunked: Chunked Transfer Protocol framing and GATT writes;
- backends: the bleak import seam;
- discovery/link/mtu/listener: connection lifecycle mixins;
- auth_state/auth_connect/auth_handshake: chunked ECDH authentication;
- notifications/streams: live notification callbacks and stream control;
- client: StreamCallbacks and the public MiBand6Client class.

Everything that the old module exported (including the private helpers used by
tests, such as _env_device_id and _secure_env_file) is re-exported here, so
miband_monitor.ble can act as a drop-in replacement for miband_client.

See docs/modules/ble.md for responsibilities, public API and testing notes.
"""

from __future__ import annotations

from ..config.defaults import DEFAULT_CONNECT_TIMEOUT
from .chunked import (
    _resolve_mtu,
    _write_with_in_progress_retry,
    write_chunked_value,
)
from .client import MiBand6Client, StreamCallbacks
from .backends import BleakClient, BleakScanner
from .crypto import _aes_cbc_encrypt, encrypt_challenge
from .identity import (
    PLACEHOLDER_DEVICE_PREFIXES,
    _env_device_id,
    _secure_env_file,
    device_matches,
    is_placeholder_device_id,
    normalize_auth_key,
    normalize_device_identifier,
)
from .link import DIRECT_CONNECT_TIMEOUT
from .parsers import parse_heart_rate, parse_raw_sensor_data, parse_realtime_steps
from .protocol import (
    AUTH_STEP1_PAYLOAD_SIZE,
    AUTH_STEP1_PUBKEY_SIZE,
    AUTH_STEP1_RANDOM_SIZE,
    AUTH_TOTAL_LENGTH_OVERHEAD,
    CHUNKED_ATT_OVERHEAD,
    CHUNKED_DEFAULT_MTU,
    CHUNKED_ENDPOINT_AUTH,
    CHUNKED_FIRST_HEADER_SIZE,
    CHUNKED_NEXT_HEADER_SIZE,
    CMD_RAW_SENSOR_START_1,
    CMD_RAW_SENSOR_START_2,
    CMD_RAW_SENSOR_START_3,
    CMD_RAW_SENSOR_STOP,
    DEMO_AUTH_KEY_BYTES,
    DEMO_AUTH_KEY_HEX,
    DEMO_DEFAULT_AUTH_KEY,
    DEMO_DEFAULT_MAC,
    DEMO_DEVICE_ID,
    DEMO_DEVICE_NAME,
    MAX_REASSEMBLY_BYTES,
    RAW_SENSOR_ACCEL_LSB_PER_G,
    RAW_SENSOR_TYPE_ACCEL,
    RAW_SENSOR_TYPE_OPTICAL,
    RAW_SENSOR_TYPE_STATUS,
    UUID_CHAR_CHUNKED_READ,
    UUID_CHAR_CHUNKED_WRITE,
    UUID_CHAR_HR_CONTROL,
    UUID_CHAR_HR_MEASUREMENT,
    UUID_CHAR_RAW_SENSOR_CONTROL,
    UUID_CHAR_RAW_SENSOR_DATA,
    UUID_CHAR_STEPS,
    UUID_SVC_HEART_RATE,
    UUID_SVC_MIBAND1,
)

__all__ = [
    # Client and callbacks
    "MiBand6Client",
    "StreamCallbacks",
    "BleakClient",
    "BleakScanner",
    # GATT UUIDs
    "UUID_SVC_MIBAND1",
    "UUID_CHAR_RAW_SENSOR_CONTROL",
    "UUID_CHAR_RAW_SENSOR_DATA",
    "UUID_CHAR_STEPS",
    "UUID_CHAR_CHUNKED_WRITE",
    "UUID_CHAR_CHUNKED_READ",
    "UUID_SVC_HEART_RATE",
    "UUID_CHAR_HR_MEASUREMENT",
    "UUID_CHAR_HR_CONTROL",
    # Chunked-transfer framing
    "CHUNKED_ENDPOINT_AUTH",
    "CHUNKED_DEFAULT_MTU",
    "CHUNKED_FIRST_HEADER_SIZE",
    "CHUNKED_NEXT_HEADER_SIZE",
    "CHUNKED_ATT_OVERHEAD",
    # Auth payload sizes
    "AUTH_STEP1_RANDOM_SIZE",
    "AUTH_STEP1_PUBKEY_SIZE",
    "AUTH_STEP1_PAYLOAD_SIZE",
    "AUTH_TOTAL_LENGTH_OVERHEAD",
    "MAX_REASSEMBLY_BYTES",
    # Raw sensor protocol
    "CMD_RAW_SENSOR_START_1",
    "CMD_RAW_SENSOR_START_2",
    "CMD_RAW_SENSOR_START_3",
    "CMD_RAW_SENSOR_STOP",
    "RAW_SENSOR_TYPE_ACCEL",
    "RAW_SENSOR_TYPE_OPTICAL",
    "RAW_SENSOR_TYPE_STATUS",
    "RAW_SENSOR_ACCEL_LSB_PER_G",
    # Demo identity (never real credentials)
    "DEMO_DEVICE_ID",
    "DEMO_DEVICE_NAME",
    "DEMO_AUTH_KEY_HEX",
    "DEMO_AUTH_KEY_BYTES",
    "DEMO_DEFAULT_MAC",
    "DEMO_DEFAULT_AUTH_KEY",
    # Parsers
    "parse_heart_rate",
    "parse_realtime_steps",
    "parse_raw_sensor_data",
    # Identity / credentials
    "normalize_auth_key",
    "PLACEHOLDER_DEVICE_PREFIXES",
    "is_placeholder_device_id",
    "normalize_device_identifier",
    "device_matches",
    "_env_device_id",
    "_secure_env_file",
    # Crypto and chunked writes
    "encrypt_challenge",
    "_aes_cbc_encrypt",
    "write_chunked_value",
    "_resolve_mtu",
    "_write_with_in_progress_retry",
    # Link timing
    "DIRECT_CONNECT_TIMEOUT",
    "DEFAULT_CONNECT_TIMEOUT",
]
