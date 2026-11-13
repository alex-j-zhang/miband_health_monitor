"""Mi Band 6 GATT protocol and chunked-transfer wire constants.

Every value in this module is a wire-level fact: the Huami private service
UUIDs, the standard Bluetooth SIG heart-rate service, the "chunked transfer"
framing sizes used by the ECDH handshake and the raw-sensor opcodes/frame types
of the Mi Band 6 (Huami2021 protocol).

Nothing here performs I/O. miband_monitor.ble.chunked turns the framing
constants into GATT writes, miband_monitor.ble.parsers decodes the
notifications described below, and miband_monitor.ble.client combines them
into the public MiBand6Client.

The fake device identity used by demo mode/simulator tests lives in
miband_monitor.config.demo and is re-exported here so the BLE package is a
drop-in replacement for the historical miband_client namespace.
"""

from __future__ import annotations

# Re-exported demo identity (the only device/key literals allowed in first-party
# code; real credentials always come from .env / the environment).
from ..config.demo import (
    DEMO_AUTH_KEY_BYTES,
    DEMO_AUTH_KEY_HEX,
    DEMO_DEVICE_ID,
    DEMO_DEVICE_NAME,
)

# ---------------------------------------------------------------------------
# BLE GATT service and characteristic UUIDs
# ---------------------------------------------------------------------------
#: Huami private "Mi Band" service (0xFEE0); hosts the raw sensor and chunked
#: transfer characteristics used by the authenticated session.
UUID_SVC_MIBAND1 = "0000fee0-0000-1000-8000-00805f9b34fb"
#: Vendor-specific characteristic 0x0001: raw-sensor command/control writes.
UUID_CHAR_RAW_SENSOR_CONTROL = "00000001-0000-3512-2118-0009af100700"
#: Vendor-specific characteristic 0x0002: multiplexed raw-sensor notifications.
UUID_CHAR_RAW_SENSOR_DATA = "00000002-0000-3512-2118-0009af100700"
#: Vendor-specific characteristic 0x0007: real-time steps/distance/calories.
UUID_CHAR_STEPS = "00000007-0000-3512-2118-0009af100700"
#: Chunked Transfer Protocol write endpoint (0x0016).
UUID_CHAR_CHUNKED_WRITE = "00000016-0000-3512-2118-0009af100700"
#: Chunked Transfer Protocol notify endpoint (0x0017).
UUID_CHAR_CHUNKED_READ = "00000017-0000-3512-2118-0009af100700"

#: Standard Bluetooth SIG heart-rate service (0x180D).
UUID_SVC_HEART_RATE = "0000180d-0000-1000-8000-00805f9b34fb"
#: Standard Heart Rate Measurement characteristic (0x2A37).
UUID_CHAR_HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
#: Standard Heart Rate Control Point characteristic (0x2A39).
UUID_CHAR_HR_CONTROL = "00002a39-0000-1000-8000-00805f9b34fb"

#: Endpoint type announced in the first chunked-auth header (Huami auth).
CHUNKED_ENDPOINT_AUTH = 0x82

# ---------------------------------------------------------------------------
# Chunked Transfer Protocol framing
# ---------------------------------------------------------------------------
#: Conservative ATT floor: 23-byte MTU -> 20-byte ATT payload.
CHUNKED_DEFAULT_MTU = 23
#: First frame header: 0x03, flags, 0x00, handle, count, total length(4), endpoint(2).
CHUNKED_FIRST_HEADER_SIZE = 11
#: Continuation frame header: 0x03, flags, 0x00, handle, sequence counter.
CHUNKED_NEXT_HEADER_SIZE = 5
#: ATT write overhead (opcode + attribute handle) counted against the MTU.
CHUNKED_ATT_OVERHEAD = 3

# ---------------------------------------------------------------------------
# Chunked-ECDH auth payload sizes
# ---------------------------------------------------------------------------
#: The band's step-1 payload is always a 16-byte challenge salt plus a 48-byte
#: NIST B-163 public key.
AUTH_STEP1_RANDOM_SIZE = 16
AUTH_STEP1_PUBKEY_SIZE = 48
AUTH_STEP1_PAYLOAD_SIZE = AUTH_STEP1_RANDOM_SIZE + AUTH_STEP1_PUBKEY_SIZE
#: Header + endpoint/status fields not counted in the announced total length.
AUTH_TOTAL_LENGTH_OVERHEAD = 3
#: Hard upper bound for one reassembled auth payload (defensive against a
#: malformed/hostile total-length field).
MAX_REASSEMBLY_BYTES = 1024

# ---------------------------------------------------------------------------
# Raw sensor (IMU) activation opcodes from the Huami2021 protocol
# ---------------------------------------------------------------------------
#: 25 Hz sampling rate (short form).
CMD_RAW_SENSOR_START_1 = b"\x01\x03\x19"
#: 25 Hz sampling rate (long form with explicit interval bytes).
CMD_RAW_SENSOR_START_2 = b"\x01\x03\x00\x00\x00\x19"
#: Start the sensor hub stream.
CMD_RAW_SENSOR_START_3 = b"\x02"
#: Stop the sensor hub stream.
CMD_RAW_SENSOR_STOP = b"\x03"

# ---------------------------------------------------------------------------
# Raw sensor frame types (characteristic 0x0002 multiplexes several streams)
# ---------------------------------------------------------------------------
#: One-byte frame type prefix; only 0x00 carries the 3-axis accelerometer.
RAW_SENSOR_TYPE_ACCEL = 0x00
#: Optical (PPG) channel, not accelerometer data.
RAW_SENSOR_TYPE_OPTICAL = 0x01
#: Short periodic status frame.
RAW_SENSOR_TYPE_STATUS = 0x07
#: Accelerometer full scale on the Mi Band 6 is +/-8 g at 4096 LSB/g (a resting
#: magnitude measures 0.997 g, which pins the scale down).
RAW_SENSOR_ACCEL_LSB_PER_G = 4096.0

# ---------------------------------------------------------------------------
# Legacy demo aliases
# ---------------------------------------------------------------------------
#: Historical names kept so old callers/tests keep importing exactly as before.
DEMO_DEFAULT_MAC = DEMO_DEVICE_ID
DEMO_DEFAULT_AUTH_KEY = DEMO_AUTH_KEY_HEX
