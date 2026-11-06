"""BLE device scanner helper for discovering Mi Band devices."""

import asyncio
from bleak import BleakScanner


async def scan_devices(duration_seconds: int = 8) -> None:
    print(f"Scanning for nearby BLE devices for {duration_seconds} seconds...")
    devices = await BleakScanner.discover(timeout=duration_seconds, return_adv=True)

    found_bands = []
    other_devices = []

    for dev, adv in devices.values():
        name = dev.name or adv.local_name or "Unknown"
        rssi = adv.rssi
        entry = (dev.address, name, rssi)
        if any(keyword in name.lower() for keyword in ["band", "mi", "xiaomi", "huami"]):
            found_bands.append(entry)
        else:
            other_devices.append(entry)

    if found_bands:
        print("\n=== Likely Mi Band Candidates ===")
        for address, name, rssi in found_bands:
            print(f"  * Address: {address} | Name: {name} | RSSI: {rssi} dBm")
    else:
        print("\nNo devices with 'Band/Mi/Xiaomi' in their advertised name were found.")
        print("Tip: If the band is paired with your phone, disable phone Bluetooth so the band advertises.")

    print("\n=== All Discovered BLE Devices ===")
    for address, name, rssi in other_devices:
        print(f"  - Address: {address} | Name: {name} | RSSI: {rssi} dBm")


if __name__ == "__main__":
    asyncio.run(scan_devices())

