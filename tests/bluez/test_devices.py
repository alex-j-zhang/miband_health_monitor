"""BlueZ device lookup: paired, connected, cached and disconnect helpers."""

from __future__ import annotations

import asyncio

from miband_monitor.bluez import admin, devices, platform
from tests.support import MAC


# ---------------------------------------------------------------------------
# Pairing / MTU helpers
# ---------------------------------------------------------------------------
async def test_paired_device_requires_pairing(monkeypatch):
    monkeypatch.setattr(platform, "is_bluez_platform", lambda: True)

    async def unpaired(_address, timeout=0):
        return {"path": "/org/bluez/hci0/dev_X", "address": MAC, "paired": False, "trusted": False, "props": {}}

    monkeypatch.setattr(devices, "find_device", unpaired)
    assert await devices.paired_device(MAC) is None

    async def paired(_address, timeout=0):
        return {
            "path": "/org/bluez/hci0/dev_X",
            "address": MAC,
            "name": "Mi Smart Band 6",
            "paired": True,
            "trusted": False,
            "props": {},
        }

    monkeypatch.setattr(devices, "find_device", paired)
    device = await devices.paired_device(MAC)
    assert device is not None
    assert device.details["path"] == "/org/bluez/hci0/dev_X"


async def test_paired_device_is_none_off_linux(monkeypatch):
    monkeypatch.setattr(platform, "is_bluez_platform", lambda: False)
    assert await devices.paired_device(MAC) is None


async def test_paired_device_rejects_trusted_only(monkeypatch):
    monkeypatch.setattr(platform, "is_bluez_platform", lambda: True)

    async def trusted_only(_address, timeout=0):
        return {
            "path": "/org/bluez/hci0/dev_X",
            "address": MAC,
            "paired": False,
            "bonded": False,
            "trusted": True,
            "connected": False,
            "props": {},
        }

    monkeypatch.setattr(devices, "find_device", trusted_only)
    assert await devices.paired_device(MAC) is None


async def test_connected_device_returns_device_when_connected(monkeypatch):
    monkeypatch.setattr(platform, "is_bluez_platform", lambda: True)

    async def disconnected(_address, timeout=0):
        return {"path": "/org/bluez/hci0/dev_X", "address": MAC, "connected": False, "props": {}}

    monkeypatch.setattr(devices, "find_device", disconnected)
    assert await devices.connected_device(MAC) is None

    async def connected(_address, timeout=0):
        return {
            "path": "/org/bluez/hci0/dev_X",
            "address": MAC,
            "name": "Mi Smart Band 6",
            "connected": True,
            "services_resolved": True,
            "props": {},
        }

    monkeypatch.setattr(devices, "find_device", connected)
    dev = await devices.connected_device(MAC)
    assert dev is not None
    assert dev.details["path"] == "/org/bluez/hci0/dev_X"

    # Ghost connection: Connected=True but ServicesResolved=False
    async def ghost_connected(_address, timeout=0):
        return {
            "path": "/org/bluez/hci0/dev_X",
            "address": MAC,
            "name": "Mi Smart Band 6",
            "connected": True,
            "services_resolved": False,
            "props": {},
        }

    monkeypatch.setattr(devices, "find_device", ghost_connected)
    assert await devices.connected_device(MAC) is None


async def test_active_cached_device(monkeypatch):
    monkeypatch.setattr(platform, "is_bluez_platform", lambda: True)

    async def with_rssi(_address, timeout=0):
        return {
            "path": "/org/bluez/hci0/dev_X",
            "address": MAC,
            "name": "Mi Smart Band 6",
            "rssi": -65,
            "props": {"RSSI": -65},
        }

    monkeypatch.setattr(devices, "find_device", with_rssi)
    dev = await devices.active_cached_device(MAC)
    assert dev is not None
    assert dev.details["path"] == "/org/bluez/hci0/dev_X"

    async def no_rssi(_address, timeout=0):
        return {
            "path": "/org/bluez/hci0/dev_X",
            "address": MAC,
            "name": "Mi Smart Band 6",
            "rssi": None,
            "props": {},
        }

    monkeypatch.setattr(devices, "find_device", no_rssi)
    assert await devices.active_cached_device(MAC) is None


async def test_disconnect_device_off_linux(monkeypatch):
    monkeypatch.setattr(platform, "is_bluez_platform", lambda: False)
    assert await admin.disconnect_device(MAC) is False


async def test_disconnect_device_when_not_connected(monkeypatch):
    monkeypatch.setattr(platform, "is_bluez_platform", lambda: True)
    monkeypatch.setattr(devices, "find_device", lambda *a, **k: asyncio.sleep(0, result=None))
    assert await admin.disconnect_device(MAC) is False
