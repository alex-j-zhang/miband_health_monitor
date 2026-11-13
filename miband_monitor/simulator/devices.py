"""Minimal stand-ins for the bleak device/advertisement objects.

The real client only reads ``address``, ``name``, ``rssi`` and
``advertisement.local_name`` from a discovered device, so these light classes
are enough to drive the scan path without bleak's platform backends.
"""

from __future__ import annotations

from .constants import DEFAULT_ADVERTISE_NAME, DEFAULT_RSSI, SIMULATED_BAND_NAME


class FakeAdvertisement:
    """Minimal stand-in for ``bleak.backends.scanner.AdvertisementData``."""

    def __init__(self, rssi: int = DEFAULT_RSSI, local_name: str = DEFAULT_ADVERTISE_NAME) -> None:
        self.rssi = rssi
        self.local_name = local_name


class FakeDevice:
    """Minimal stand-in for ``bleak.backends.device.BLEDevice``."""

    def __init__(self, address: str) -> None:
        self.address = address
        self.name = SIMULATED_BAND_NAME
        self.rssi = DEFAULT_RSSI
        self.advertisement = FakeAdvertisement()
