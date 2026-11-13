"""Protocol constants and demo identity used by the in-memory band simulator.

The simulator deliberately keeps its own copies of the handful of client-side
framing constants it needs (instead of importing private helpers from the BLE
package), so that the fake and real transports cannot drift silently: the
values below are part of the wire format, not tunables.
"""

from __future__ import annotations

import logging

from miband_monitor.config.demo import DEMO_DEVICE_ID, DEMO_DEVICE_NAME

logger = logging.getLogger("SimBand")

#: Documentation-style MAC address reserved for the in-memory simulator; not a
#: real device. Production addresses come from DEVICE_ID / .env.
DEFAULT_DEMO_MAC = DEMO_DEVICE_ID
#: Advertised name real Mi Band 6 units emit (the client matches a substring).
DEFAULT_ADVERTISE_NAME = "Mi Smart Band 6"
#: Device name reported through the fake BLEDevice stand-in.
SIMULATED_BAND_NAME = DEMO_DEVICE_NAME
#: Signal strength reported by the fake advertisement/scanner.
DEFAULT_RSSI = -42

# Protocol constants the simulator needs (kept local to avoid importing
# private helpers from the client module).
_AUTH_STEP1_PREFIX = b"\x04\x02\x00\x02"
_AUTH_STEP2_OPCODE = 0x05
_FIRST_CHUNK_HEADER = 11
_NEXT_CHUNK_HEADER = 5
_AUTH_TOTAL_LENGTH_FIELD = 3  # client derives payload size as total_len - 3
