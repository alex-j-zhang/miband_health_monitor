"""Continuous BLE scanner that logs any packet immediately."""

import asyncio
from bleak import BleakScanner


async def main():
    def detection_callback(device, advertisement_data):
        name = device.name or advertisement_data.local_name or "Unknown"
        print(f"[FOUND] {device.address} | Name: {name} | RSSI: {advertisement_data.rssi}")

    scanner = BleakScanner(detection_callback=detection_callback)
    print("Starting continuous live BLE scan for 15 seconds. Please tap your band...")
    await scanner.start()
    await asyncio.sleep(15)
    await scanner.stop()
    print("Scan finished.")


if __name__ == "__main__":
    asyncio.run(main())

