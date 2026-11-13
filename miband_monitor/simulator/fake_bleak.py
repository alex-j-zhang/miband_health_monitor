"""Just-enough fakes for ``bleak.BleakClient`` and ``bleak.BleakScanner``.

The real client code is exercised unchanged against these two classes: writes
and notifications are forwarded to a :class:`SimulatedBand`, and the scanner
reports one documentation-style demo device. Failure modes (external
disconnect, not-connected errors) are reproduced faithfully enough for the
integration tests.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Optional

from .constants import DEFAULT_DEMO_MAC, DEFAULT_RSSI, logger
from .devices import FakeAdvertisement, FakeDevice


class FakeBleakClient:
    """Just enough of ``bleak.BleakClient`` to drive the real client code."""

    def __init__(
        self,
        address_or_ble_device,
        disconnected_callback: Optional[Callable] = None,
        *,
        band: Optional["SimulatedBand"] = None,
        timeout: Optional[float] = None,
        **_kwargs,
    ) -> None:
        self.address = getattr(address_or_ble_device, "address", str(address_or_ble_device))
        self.disconnected_callback = disconnected_callback
        self.timeout = timeout
        self.band = band
        if self.band is None:
            raise RuntimeError(
                "FakeBleakClient requires a SimulatedBand instance "
                "(use sim_band.simulated_factories() or pass band=...)."
            )
        self.is_connected = False

    @property
    def mtu_size(self) -> int:
        return 23

    async def connect(self) -> None:
        self.is_connected = True
        await asyncio.sleep(0)
        self.band.on_connect(self)

    async def disconnect(self) -> None:
        if self.is_connected:
            self.is_connected = False
            self.band.on_disconnect()

    def simulate_external_disconnect(self) -> None:
        """Drop the link as if the band went out of range or slept."""
        if not self.is_connected:
            return
        self.is_connected = False
        self.band.on_disconnect()
        callback = self.disconnected_callback
        if callback is not None:
            try:
                callback(self)
            except Exception:  # pragma: no cover - defensive
                logger.exception("Simulated disconnect callback failed")

    async def start_notify(self, char_specifier, callback) -> None:
        if not self.is_connected:
            raise OSError("simulated client is not connected")
        self.band.subscribe(str(char_specifier), callback)

    async def stop_notify(self, char_specifier) -> None:
        self.band.unsubscribe(str(char_specifier))

    async def write_gatt_char(self, char_specifier, data, response: Optional[bool] = None) -> None:
        if not self.is_connected:
            raise OSError("simulated client is not connected")
        self.band.feed_write(str(char_specifier), bytes(data))

    async def read_gatt_char(self, char_specifier) -> bytes:
        if not self.is_connected:
            raise OSError("simulated client is not connected")
        return self.band.read(str(char_specifier))


class FakeBleakScanner:
    """Just enough of ``bleak.BleakScanner`` to feed the client one device."""

    default_address = DEFAULT_DEMO_MAC
    default_rssi = DEFAULT_RSSI

    def __init__(self, detection_callback: Optional[Callable] = None, **_kwargs) -> None:
        self.detection_callback = detection_callback
        self._running = False

    async def start(self) -> None:
        self._running = True
        if self.detection_callback is not None:
            device = FakeDevice(self.default_address)
            advertisement = FakeAdvertisement(rssi=self.default_rssi)
            asyncio.get_running_loop().call_soon(self.detection_callback, device, advertisement)

    async def stop(self) -> None:
        self._running = False

    @classmethod
    async def find_device_by_address(cls, address: str, timeout: float = 5.0) -> FakeDevice:
        await asyncio.sleep(0)
        return FakeDevice(address)

    @classmethod
    async def discover(cls, timeout: float = 5.0, return_adv: bool = False):
        await asyncio.sleep(0)
        device = FakeDevice(cls.default_address)
        if return_adv:
            return {device.address: (device, FakeAdvertisement())}
        return [device]
