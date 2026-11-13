"""Fake device identity used by demo mode and simulator tests.

These values are intentionally not real credentials. They are the only device
identity/key literals allowed in first-party code; production values are read
from .env or the process environment by miband_monitor.config.env.
"""

from __future__ import annotations

#: Documentation-style MAC address reserved for the in-memory simulator.
DEMO_DEVICE_ID = "C0:FF:EE:C0:FF:EE"
#: Advertised-name fragment used by the fake bleak scanner.
DEMO_DEVICE_NAME = "Mi Smart Band 6 (simulated)"
#: 32-hex placeholder key; it authenticates only against the built-in simulator.
DEMO_AUTH_KEY_HEX = "0123456789abcdef0123456789abcdef"
#: Byte form for simulator/tests.
DEMO_AUTH_KEY_BYTES = bytes.fromhex(DEMO_AUTH_KEY_HEX)
