"""Client wiring for direct connect, BlueZ standby handoff and MTU."""

from __future__ import annotations

import asyncio

import pytest

import miband_monitor.ble.client as _ble_client_module
from miband_monitor import bluez
from miband_monitor.ble import CHUNKED_DEFAULT_MTU
from miband_monitor.ble.client import MiBand6Client
from sim_band import FakeBleakScanner, FakeDevice
from tests.support import KEY, MAC


# ---------------------------------------------------------------------------
# Client integration: direct connect, fallback and MTU wiring
# ---------------------------------------------------------------------------
async def test_direct_connect_target_needs_the_real_bleak_backend(monkeypatch):
    client = MiBand6Client(MAC, KEY, client_factory=lambda *a, **k: None)
    assert await client._direct_connect_target() is None


async def test_direct_connect_target_skips_scanning_for_a_bonded_band(monkeypatch):
    class DummyClient:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(_ble_client_module, "BleakClient", DummyClient)
    monkeypatch.setattr(bluez, "is_bluez_platform", lambda: True)

    sentinel = object()

    async def bonded(_address):
        return sentinel

    monkeypatch.setattr(bluez, "paired_device", bonded)
    client = MiBand6Client(MAC, KEY, client_factory=DummyClient)
    assert await client._direct_connect_target() is sentinel


async def test_direct_connect_target_ignores_uuids_and_the_disable_flag(monkeypatch):
    class DummyClient:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(_ble_client_module, "BleakClient", DummyClient)
    monkeypatch.setattr(bluez, "is_bluez_platform", lambda: True)
    monkeypatch.setattr(bluez, "paired_device", lambda *a, **k: asyncio.sleep(0, result="device"))

    uuid_client = MiBand6Client("1234ABCD-0000-1000-8000-00805F9B34FB", KEY, client_factory=DummyClient)
    assert await uuid_client._direct_connect_target() is None

    disabled = MiBand6Client(MAC, KEY, client_factory=DummyClient, direct_connect=False)
    assert await disabled._direct_connect_target() is None


async def test_open_client_falls_back_to_scanning_when_direct_connect_fails(monkeypatch):
    """Non-BlueZ backends keep the plain direct-attempt -> scan fallback."""

    instances = []

    class FlakyClient:
        def __init__(self, device, disconnected_callback=None, *, timeout=None, **kwargs):
            self.device = device
            self.address = getattr(device, "address", str(device))
            self.connected = False
            instances.append(self)

        async def connect(self):
            if len(instances) == 1:
                raise OSError("simulated direct-connect failure")
            self.connected = True

        async def disconnect(self):
            self.connected = False

    monkeypatch.setattr(bluez, "is_bluez_platform", lambda: False)
    FakeBleakScanner.default_address = MAC

    client = MiBand6Client(
        MAC,
        KEY,
        client_factory=FlakyClient,
        scanner_factory=FakeBleakScanner,
    )
    await client._open_client(FakeDevice(MAC), scan_timeout=1.0, connect_timeout=1.0)

    assert len(instances) == 2, "the direct attempt and the fallback should both build a client"
    assert instances[0].connected is False
    assert client.client is instances[1]
    assert instances[1].connected is True


async def _bluez_client(monkeypatch, client_factory, *, trusted=True):
    """Build a BlueZ-mode client with all D-Bus helpers stubbed out."""
    monkeypatch.setattr(_ble_client_module, "BleakClient", client_factory)
    monkeypatch.setattr(bluez, "is_bluez_platform", lambda: True)
    monkeypatch.setattr(
        bluez, "set_device_trusted", lambda *a, **k: asyncio.sleep(0, result=trusted)
    )
    monkeypatch.setattr(
        bluez, "connected_device", lambda *a, **k: asyncio.sleep(0, result=None)
    )
    monkeypatch.setattr(
        bluez, "paired_device", lambda *a, **k: asyncio.sleep(0, result=None)
    )
    monkeypatch.setattr(
        bluez, "disconnect_device", lambda *a, **k: asyncio.sleep(0, result=False)
    )
    FakeBleakScanner.default_address = MAC
    return MiBand6Client(
        MAC,
        KEY,
        client_factory=client_factory,
        scanner_factory=FakeBleakScanner,
    )


async def test_open_client_bluez_hands_off_an_already_established_link(monkeypatch):
    """The standby loop establishes the link; bleak only collects cached services."""

    instances = []

    class AttachedClient:
        def __init__(self, device, disconnected_callback=None, *, timeout=None, **kwargs):
            self.device = device
            self.address = getattr(device, "address", str(device))
            self.connected = False
            self.is_connected = False
            instances.append(self)

        async def connect(self):
            self.connected = True
            self.is_connected = True

        async def disconnect(self):
            self.connected = False
            self.is_connected = False

    client = await _bluez_client(monkeypatch, AttachedClient)
    info = {
        "address": MAC,
        "path": bluez.device_path_for_address(MAC),
        "connected": True,
        "services_resolved": True,
    }

    async def ready(_address, *, timeout=70.0, poll_interval=0.35):
        return {"ready": True, "reason": "resolved", "info": info, "attempts": 1, "errors": []}

    monkeypatch.setattr(bluez, "wait_for_bluez_link", ready)
    monkeypatch.setattr(bluez, "make_ble_device", lambda _info: FakeDevice(MAC))

    await client._open_client(None, scan_timeout=1.0, connect_timeout=1.0)

    assert len(instances) == 1
    assert client.client is instances[0]
    assert instances[0].connected is True


async def test_open_client_bluez_timeout_reports_guidance_without_disconnecting(monkeypatch):
    """A failed standby window must never send Disconnect (it wedges the adapter)."""

    disconnect_calls = []

    class NeverClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("no client may be created when the link never comes up")

    client = await _bluez_client(monkeypatch, NeverClient)
    monkeypatch.setattr(
        bluez,
        "disconnect_device",
        lambda *a, **k: (disconnect_calls.append(a), asyncio.sleep(0, result=False))[1],
    )

    async def never_ready(_address, *, timeout=70.0, poll_interval=0.35):
        return {
            "ready": False,
            "reason": "connect-timeout",
            "info": None,
            "attempts": 2,
            "errors": ["Failed: le-connection-abort-by-local"],
        }

    monkeypatch.setattr(bluez, "wait_for_bluez_link", never_ready)

    with pytest.raises(TimeoutError) as excinfo:
        await client._open_client(None, scan_timeout=1.0, connect_timeout=1.0)

    message = str(excinfo.value)
    assert "advertising pulse" in message
    assert "usbreset" in message
    assert disconnect_calls == []


async def test_apply_mtu_prefers_the_negotiated_value(monkeypatch):
    class FakeBle:
        def __init__(self, mtu):
            self.mtu_size = mtu

    client = MiBand6Client(MAC, KEY, client_factory=lambda *a, **k: None)
    client.client = FakeBle(23)

    async def acquire(ble):
        ble.mtu_size = 247
        return True

    monkeypatch.setattr(bluez, "try_acquire_mtu", acquire)
    await client._apply_mtu()
    assert client._session_mtu == 247


async def test_apply_mtu_keeps_the_floor_when_acquisition_fails(monkeypatch):
    class FakeBle:
        mtu_size = 23

    client = MiBand6Client(MAC, KEY, client_factory=lambda *a, **k: None)
    client.client = FakeBle()

    async def acquire(_ble):
        return False

    monkeypatch.setattr(bluez, "try_acquire_mtu", acquire)
    await client._apply_mtu()
    assert client._session_mtu == CHUNKED_DEFAULT_MTU
