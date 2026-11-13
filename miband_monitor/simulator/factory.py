"""Wiring helpers that plug the simulated band into the real BLE client.

``simulated_factories()`` returns the ``(client_factory, scanner_factory)`` pair
the client accepts as constructor overrides; ``make_simulated_client()`` builds a
real :class:`MiBand6Client` with the fake backend already installed, which is how
the integration tests and the demo command run without Bluetooth hardware.
"""

from __future__ import annotations

from typing import Callable, Optional

from miband_monitor.ble import MiBand6Client

from .band import SimulatedBand
from .fake_bleak import FakeBleakClient, FakeBleakScanner


def simulated_factories(
    band: SimulatedBand,
) -> tuple[Callable, type[FakeBleakScanner]]:
    """Return ``(client_factory, scanner_factory)`` wired to *band*."""

    def client_factory(device_or_address, **kwargs):
        return FakeBleakClient(device_or_address, band=band, **kwargs)

    return client_factory, FakeBleakScanner


def make_simulated_client(
    mac: str,
    auth_key_hex: str,
    band: SimulatedBand,
    *,
    scanner_address: Optional[str] = None,
    device_name: Optional[str] = None,
    mtu: Optional[int] = None,
) -> MiBand6Client:
    """Build a real :class:`MiBand6Client` wired to the fake backend."""
    client_factory, scanner_factory = simulated_factories(band)
    if scanner_address is not None:
        FakeBleakScanner.default_address = scanner_address
    return MiBand6Client(
        mac,
        auth_key_hex,
        device_name=device_name,
        mtu=mtu,
        client_factory=client_factory,
        scanner_factory=scanner_factory,
    )
