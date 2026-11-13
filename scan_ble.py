"""Backward-compatible BLE scanner helper.

The implementation now lives in miband_monitor.diagnostics.scanning. The
printed address is what you put into DEVICE_ID in .env:

- Linux / Windows: the Bluetooth MAC address (e.g. C0:FF:EE:C0:FF:EE);
- macOS: CoreBluetooth does not expose MAC addresses, so the value is a
  per-host UUID. Use it as DEVICE_ID (or set DEVICE_NAME instead).
"""

from __future__ import annotations

import asyncio

from miband_monitor.diagnostics.scanning import scan_devices

__all__ = ["scan_devices"]


if __name__ == "__main__":
    asyncio.run(scan_devices())
