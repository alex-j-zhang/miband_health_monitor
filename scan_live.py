"""Continuous BLE scanner that logs any packet immediately.

Useful when the band only advertises for a short window.  The printed
``address`` is what goes into ``DEVICE_ID``: a MAC on Linux/Windows, a
CoreBluetooth UUID on macOS.
"""

import asyncio
import sys

from bleak import BleakScanner

SCAN_SECONDS = 15


async def main():
    def detection_callback(device, advertisement_data):
        name = device.name or advertisement_data.local_name or "Unknown"
        print(f"[FOUND] {device.address} | Name: {name} | RSSI: {advertisement_data.rssi}")

    kwargs = {}
    if sys.platform.startswith("linux"):
        kwargs["bluez"] = {"filters": {"DuplicateData": True, "Transport": "le"}}

    scanner = BleakScanner(detection_callback=detection_callback, **kwargs)
    print(
        f"Starting continuous live BLE scan for {SCAN_SECONDS} seconds. "
        "Tap and keep sliding the band screen..."
    )
    await scanner.start()
    await asyncio.sleep(SCAN_SECONDS)
    await scanner.stop()
    print("Scan finished. Put the band's address/UUID into DEVICE_ID (or use DEVICE_NAME).")


if __name__ == "__main__":
    asyncio.run(main())
