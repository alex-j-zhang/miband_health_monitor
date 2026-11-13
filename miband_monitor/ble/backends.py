"""Single import point for the optional bleak dependency.

The concrete classes are kept here so tests can monkeypatch
miband_monitor.ble.client.BleakClient / BleakScanner (the historical
miband_client.BleakClient seam) without importing bleak in every mixin. The
client captures the selected classes on the instance in __init__ and the mixins
compare against those attributes.
"""

from __future__ import annotations

from bleak import BleakClient, BleakScanner

__all__ = ["BleakClient", "BleakScanner"]
