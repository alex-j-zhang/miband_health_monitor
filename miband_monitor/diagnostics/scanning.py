"""BLE device scanner helper for discovering Mi Band devices.

The printed address is what you put into DEVICE_ID in .env:

* Linux / Windows - the Bluetooth MAC address, e.g. EC:39:C2:AA:BB:CC;
* macOS - CoreBluetooth does not expose MAC addresses, so the value is a
  per-host UUID.  Use it as DEVICE_ID (or set DEVICE_NAME instead).

Kept behaviour-identical to the historical scan_ble.py, including its longer
default window and the deliberate DuplicateData=True filter (the live scanner
uses the quieter default instead; see miband_monitor.bluez.scanner).
"""

from __future__ import annotations

import asyncio
import sys

from bleak import BleakScanner


async def scan_devices(duration_seconds: int = 90) -> None:
    """Scan for nearby BLE devices.

    The default window is long on purpose: a Mi Band 6 without a phone link only
    emits one isolated advertising pulse every 40-60 s (measured 2026-11-12), so
    the previous 8 s window almost always returned nothing.
    """
    print(
        f"Scanning for nearby BLE devices for {duration_seconds} seconds "
        "(a sleeping Mi Band answers only every 40-60 s)..."
    )
    kwargs = {}
    if sys.platform.startswith("linux"):
        kwargs["bluez"] = {"filters": {"DuplicateData": True, "Transport": "le"}}
    devices = await BleakScanner.discover(timeout=duration_seconds, return_adv=True, **kwargs)

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
        print("")
        print("=== Likely Mi Band Candidates ===")
        for address, name, rssi in found_bands:
            print(f"  * Address: {address} | Name: {name} | RSSI: {rssi} dBm")
    else:
        print("")
        print("No devices with 'Band/Mi/Xiaomi' in their advertised name were found.")
        print(
            "Tip: If the band is paired with your phone, disable phone Bluetooth and "
            "make sure 'Discoverable' (公共广播) was left enabled by Zepp Life. The band "
            "then advertises ONE pulse every 40-60 s and none while its screen is off; "
            "touching/sliding the screen does NOT make it advertise faster, so just wait."
        )

    print("")
    print("=== All Discovered BLE Devices ===")
    for address, name, rssi in other_devices:
        print(f"  - Address: {address} | Name: {name} | RSSI: {rssi} dBm")

    if sys.platform.startswith("linux"):
        try:
            from miband_monitor import bluez

            cached = await bluez.list_known_devices()
            known_bands = [
                d
                for d in cached
                if any(keyword in d.get("name", "").lower() for keyword in ["band", "mi", "xiaomi", "huami"])
            ]
            if known_bands:
                print("")
                print("=== BlueZ Cached / Known Devices ===")
                for d in known_bands:
                    status = (
                        f"paired={'yes' if d.get('paired') else 'no'} "
                        f"trusted={'yes' if d.get('trusted') else 'no'} "
                        f"connected={'yes' if d.get('connected') else 'no'}"
                    )
                    print(f"  * Address: {d['address']} | Name: {d['name']} | {status}")
        except Exception:
            pass

    if sys.platform == "darwin":
        print(
            "[macOS] CoreBluetooth exposes UUIDs instead of MAC addresses.\n"
            "Put the candidate's UUID into DEVICE_ID, or set DEVICE_NAME to a "
            "stable part of the advertised name (e.g. 'Mi Smart Band')."
        )
    else:
        print(
            "Put the band's MAC address into DEVICE_ID in .env "
            "(legacy DEVICE_MAC is still accepted)."
        )


if __name__ == "__main__":
    asyncio.run(scan_devices())
