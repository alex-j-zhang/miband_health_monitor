"""Shared constants and helpers for the test suite.

The demo identity is imported from the package configuration so tests and the
simulator agree on one placeholder credential. Nothing here is real data.
"""

from __future__ import annotations

from miband_monitor.config.demo import DEMO_AUTH_KEY_HEX, DEMO_DEVICE_ID

DEMO_AUTH_KEY_BYTES = bytes.fromhex(DEMO_AUTH_KEY_HEX)

# Short aliases used throughout the protocol and hardware tests.
MAC = DEMO_DEVICE_ID
KEY = DEMO_AUTH_KEY_HEX
AUTH_KEY = DEMO_AUTH_KEY_HEX
AUTH_KEY_BYTES = DEMO_AUTH_KEY_BYTES


async def connect_simulated_client(band, *, mac: str = MAC, key: str = KEY):
    """Connect a client to an in-memory band and finish the handshake."""
    from sim_band import make_simulated_client

    client = make_simulated_client(mac, key, band, scanner_address=mac)
    await client.connect_and_authenticate(scan_timeout=2.0)
    return client
