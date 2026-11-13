"""Continuous BLE scanner that logs any packet immediately.

Useful when the band only advertises for a short window.  The printed address
is what goes into DEVICE_ID: a MAC on Linux/Windows, a CoreBluetooth UUID on
macOS.  Behaviour-identical to the historical scan_live.py.
"""

from __future__ import annotations

import asyncio
import sys

from bleak import BleakScanner

#: A band without a phone link advertises one isolated pulse every 40-60 s, so a
#: short window usually shows nothing (measured 2026-11-12).
SCAN_SECONDS = 90


async def main():
    def detection_callback(device, advertisement_data):
        name = device.name or advertisement_data.local_name or "Unknown"
        print(f"[FOUND] {device.address} | Name: {name} | RSSI: {advertisement_data.rssi}")

    kwargs = {}
    if sys.platform.startswith("linux"):
        from miband_monitor import bluez

        kwargs = bluez.scanner_kwargs()

    scanner = BleakScanner(detection_callback=detection_callback, **kwargs)
    print(
        f"Starting continuous live BLE scan for {SCAN_SECONDS} seconds. "
        "A sleeping Mi Band answers only every 40-60 s; sliding the screen does "
        "not make it advertise faster."
    )
    await scanner.start()
    await asyncio.sleep(SCAN_SECONDS)
    await scanner.stop()
    print("Scan finished. Put the band's address/UUID into DEVICE_ID (or use DEVICE_NAME).")


if __name__ == "__main__":
    asyncio.run(main())
