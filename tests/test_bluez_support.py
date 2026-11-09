"""Tests for the Linux/BlueZ helpers, direct connect and the doctor command.

The helpers themselves are intentionally no-ops off Linux; these tests force
the BlueZ branches with monkeypatching so they also cover the Windows/macOS CI
legs without touching real D-Bus.
"""

from __future__ import annotations

import asyncio

import pytest

import bluez_support
import doctor
import miband_client
from sim_band import FakeBleakScanner, FakeDevice

MAC = "C0:FF:EE:C0:FF:EE"
KEY = "0123456789abcdef0123456789abcdef"


# ---------------------------------------------------------------------------
# Address / path helpers
# ---------------------------------------------------------------------------
def test_normalize_address_accepts_common_spellings():
    assert bluez_support.normalize_address("c0:ff:ee:c0:ff:ee") == "C0:FF:EE:C0:FF:EE"
    assert bluez_support.normalize_address("C0-FF-EE-C0-FF-EE") == "C0:FF:EE:C0:FF:EE"
    assert bluez_support.normalize_address("C0FFEEC0FFEE") == "C0:FF:EE:C0:FF:EE"
    assert bluez_support.normalize_address("") == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("C0:FF:EE:C0:FF:EE", True),
        ("c0-ff-ee-c0-ff-ee", True),
        ("1234ABCD-0000-1000-8000-00805F9B34FB", False),
        ("", False),
    ],
)
def test_looks_like_mac(value, expected):
    assert bluez_support.looks_like_mac(value) is expected


def test_device_path_matches_bluez_naming():
    assert (
        bluez_support.device_path_for_address("c0:ff:ee:c0:ff:ee")
        == "/org/bluez/hci0/dev_C0_FF_EE_C0_FF_EE"
    )
    assert (
        bluez_support.device_path_for_address("C0:FF:EE:C0:FF:EE", adapter="hci1")
        == "/org/bluez/hci1/dev_C0_FF_EE_C0_FF_EE"
    )


def test_device_info_unwraps_dbus_variants():
    class Variant:
        def __init__(self, value):
            self.value = value

    info = bluez_support._device_info_from_props(
        "/org/bluez/hci0/dev_C0_FF_EE_C0_FF_EE",
        {
            "Address": Variant("c0:ff:ee:c0:ff:ee"),
            "Alias": Variant("Mi Smart Band 6"),
            "Paired": Variant(True),
            "Trusted": Variant(False),
            "Connected": Variant(True),
        },
    )
    assert info["address"] == "C0:FF:EE:C0:FF:EE"
    assert info["name"] == "Mi Smart Band 6"
    assert info["paired"] is True
    assert info["trusted"] is False
    assert info["connected"] is True
    assert info["adapter"] == "hci0"


def test_make_ble_device_carries_the_bluez_object_path():
    pytest.importorskip("bleak")
    info = {
        "path": "/org/bluez/hci0/dev_C0_FF_EE_C0_FF_EE",
        "address": MAC,
        "name": "Mi Smart Band 6",
        "props": {"Address": MAC},
    }
    device = bluez_support.make_ble_device(info)
    assert device is not None
    assert device.address == MAC
    assert device.details["path"] == info["path"]


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------
def test_describe_ble_error_translates_unknown_object():
    exc = RuntimeError(
        '[org.freedesktop.DBus.Error.UnknownObject] Method "WriteValue" ... does not exist'
    )
    text = bluez_support.describe_ble_error(exc)
    assert "disconnected" in text
    assert "bluetoothctl pair" in text


def test_describe_ble_error_uses_dbus_error_name():
    class FakeDbusError(Exception):
        dbus_error_name = "org.bluez.Error.InProgress"

    text = bluez_support.describe_ble_error(FakeDbusError("nope"))
    assert "busy" in text


def test_describe_ble_error_handles_timeouts_and_missing_devices():
    assert "timed out" in bluez_support.describe_ble_error(TimeoutError())
    assert "not found" in bluez_support.describe_ble_error(
        type("BleakDeviceNotFoundError", (Exception,), {})("x")
    )


def test_describe_ble_error_falls_back_to_the_original_text():
    assert bluez_support.describe_ble_error(ValueError("boom")) == "ValueError: boom"


# ---------------------------------------------------------------------------
# Pairing / MTU helpers
# ---------------------------------------------------------------------------
async def test_paired_device_requires_pairing(monkeypatch):
    monkeypatch.setattr(bluez_support, "is_bluez_platform", lambda: True)

    async def unpaired(_address, timeout=0):
        return {"path": "/org/bluez/hci0/dev_X", "address": MAC, "paired": False, "trusted": False, "props": {}}

    monkeypatch.setattr(bluez_support, "find_device", unpaired)
    assert await bluez_support.paired_device(MAC) is None

    async def paired(_address, timeout=0):
        return {
            "path": "/org/bluez/hci0/dev_X",
            "address": MAC,
            "name": "Mi Smart Band 6",
            "paired": True,
            "trusted": False,
            "props": {},
        }

    monkeypatch.setattr(bluez_support, "find_device", paired)
    device = await bluez_support.paired_device(MAC)
    assert device is not None
    assert device.details["path"] == "/org/bluez/hci0/dev_X"


async def test_paired_device_is_none_off_linux(monkeypatch):
    monkeypatch.setattr(bluez_support, "is_bluez_platform", lambda: False)
    assert await bluez_support.paired_device(MAC) is None


async def test_try_acquire_mtu_uses_the_backend(monkeypatch):
    monkeypatch.setattr(bluez_support, "is_bluez_platform", lambda: True)

    class Backend:
        mtu_size = 23
        called = False

        def _acquire_mtu(self):
            self.called = True
            self.mtu_size = 247

    class Client:
        def __init__(self):
            self._backend = Backend()

        @property
        def mtu_size(self):
            return self._backend.mtu_size

        async def get_services(self):
            return None

    client = Client()
    assert await bluez_support.try_acquire_mtu(client) is True
    assert client._backend.called is True
    assert client.mtu_size == 247


async def test_try_acquire_mtu_supports_a_coroutine_backend(monkeypatch):
    """bleak 3.x implements ``_acquire_mtu`` as a coroutine."""
    monkeypatch.setattr(bluez_support, "is_bluez_platform", lambda: True)

    class Backend:
        mtu_size = 23

        async def _acquire_mtu(self):
            self.mtu_size = 185

    class Client:
        def __init__(self):
            self._backend = Backend()

        @property
        def mtu_size(self):
            return self._backend.mtu_size

    client = Client()
    assert await bluez_support.try_acquire_mtu(client) is True
    assert client.mtu_size == 185


async def test_try_acquire_mtu_is_a_noop_without_a_bluez_backend(monkeypatch):
    monkeypatch.setattr(bluez_support, "is_bluez_platform", lambda: True)
    assert await bluez_support.try_acquire_mtu(object()) is False


# ---------------------------------------------------------------------------
# Client integration: direct connect, fallback and MTU wiring
# ---------------------------------------------------------------------------
async def test_direct_connect_target_needs_the_real_bleak_backend(monkeypatch):
    client = miband_client.MiBand6Client(MAC, KEY, client_factory=lambda *a, **k: None)
    assert await client._direct_connect_target() is None


async def test_direct_connect_target_skips_scanning_for_a_bonded_band(monkeypatch):
    class DummyClient:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(miband_client, "BleakClient", DummyClient)
    monkeypatch.setattr(bluez_support, "is_bluez_platform", lambda: True)

    sentinel = object()

    async def bonded(_address):
        return sentinel

    monkeypatch.setattr(bluez_support, "paired_device", bonded)
    client = miband_client.MiBand6Client(MAC, KEY, client_factory=DummyClient)
    assert await client._direct_connect_target() is sentinel


async def test_direct_connect_target_ignores_uuids_and_the_disable_flag(monkeypatch):
    class DummyClient:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(miband_client, "BleakClient", DummyClient)
    monkeypatch.setattr(bluez_support, "is_bluez_platform", lambda: True)
    monkeypatch.setattr(bluez_support, "paired_device", lambda *a, **k: asyncio.sleep(0, result="device"))

    uuid_client = miband_client.MiBand6Client("1234ABCD-0000-1000-8000-00805F9B34FB", KEY, client_factory=DummyClient)
    assert await uuid_client._direct_connect_target() is None

    disabled = miband_client.MiBand6Client(MAC, KEY, client_factory=DummyClient, direct_connect=False)
    assert await disabled._direct_connect_target() is None


async def test_open_client_falls_back_to_scanning_when_direct_connect_fails(monkeypatch):
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

    monkeypatch.setattr(miband_client, "BleakClient", FlakyClient)
    monkeypatch.setattr(bluez_support, "is_bluez_platform", lambda: True)
    FakeBleakScanner.default_address = MAC

    client = miband_client.MiBand6Client(
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


async def test_apply_mtu_prefers_the_negotiated_value(monkeypatch):
    class FakeBle:
        def __init__(self, mtu):
            self.mtu_size = mtu

    client = miband_client.MiBand6Client(MAC, KEY, client_factory=lambda *a, **k: None)
    client.client = FakeBle(23)

    async def acquire(ble):
        ble.mtu_size = 247
        return True

    monkeypatch.setattr(bluez_support, "try_acquire_mtu", acquire)
    await client._apply_mtu()
    assert client._session_mtu == 247


async def test_apply_mtu_keeps_the_floor_when_acquisition_fails(monkeypatch):
    class FakeBle:
        mtu_size = 23

    client = miband_client.MiBand6Client(MAC, KEY, client_factory=lambda *a, **k: None)
    client.client = FakeBle()

    async def acquire(_ble):
        return False

    monkeypatch.setattr(bluez_support, "try_acquire_mtu", acquire)
    await client._apply_mtu()
    assert client._session_mtu == miband_client.CHUNKED_DEFAULT_MTU


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------
async def test_doctor_reports_a_healthy_config(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "is_bluez_platform", lambda: False)
    lines: list[str] = []
    code = await doctor.run_doctor(MAC, KEY, str(tmp_path / "data"), emit=lines.append)
    text = "\n".join(lines)
    assert code == 0
    assert "platform" in text
    assert "output dir" in text
    assert (tmp_path / "data").is_dir()


async def test_doctor_fails_on_an_invalid_auth_key(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "is_bluez_platform", lambda: False)
    lines: list[str] = []
    code = await doctor.run_doctor(MAC, "not-a-key", str(tmp_path / "data"), emit=lines.append)
    assert code == 1
    assert any("auth key" in line for line in lines)


async def test_doctor_warns_when_the_device_is_not_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "is_bluez_platform", lambda: False)
    lines: list[str] = []
    code = await doctor.run_doctor("", "", str(tmp_path / "data"), emit=lines.append)
    assert code == 1


async def test_paired_device_rejects_trusted_only(monkeypatch):
    monkeypatch.setattr(bluez_support, "is_bluez_platform", lambda: True)

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

    monkeypatch.setattr(bluez_support, "find_device", trusted_only)
    assert await bluez_support.paired_device(MAC) is None


async def test_connected_device_returns_device_when_connected(monkeypatch):
    monkeypatch.setattr(bluez_support, "is_bluez_platform", lambda: True)

    async def disconnected(_address, timeout=0):
        return {"path": "/org/bluez/hci0/dev_X", "address": MAC, "connected": False, "props": {}}

    monkeypatch.setattr(bluez_support, "find_device", disconnected)
    assert await bluez_support.connected_device(MAC) is None

    async def connected(_address, timeout=0):
        return {
            "path": "/org/bluez/hci0/dev_X",
            "address": MAC,
            "name": "Mi Smart Band 6",
            "connected": True,
            "props": {},
        }

    monkeypatch.setattr(bluez_support, "find_device", connected)
    dev = await bluez_support.connected_device(MAC)
    assert dev is not None
    assert dev.details["path"] == "/org/bluez/hci0/dev_X"


def test_is_transient_connect_error():
    assert bluez_support.is_transient_connect_error(Exception("le-connection-abort-by-local")) is True
    assert bluez_support.is_transient_connect_error(Exception("org.bluez.Error.InProgress")) is True
    assert bluez_support.is_transient_connect_error(Exception("Software caused connection abort")) is True
    assert bluez_support.is_transient_connect_error(Exception("Permanent authentication failure")) is False


def test_is_in_progress_error():
    assert bluez_support.is_in_progress_error(Exception("org.bluez.Error.InProgress")) is True
    assert bluez_support.is_in_progress_error(Exception("GATT InProgress")) is True
    assert bluez_support.is_in_progress_error(Exception("Other error")) is False


def test_scanner_kwargs(monkeypatch):
    monkeypatch.setattr(bluez_support, "is_bluez_platform", lambda: True)
    kwargs = bluez_support.scanner_kwargs()
    assert kwargs.get("bluez", {}).get("filters", {}).get("DuplicateData") is True

    class FakeBleakScanner:
        pass

    assert bluez_support.scanner_kwargs(FakeBleakScanner) == {}


async def test_pairing_agent_lifecycle(monkeypatch):
    agent = bluez_support.BlueZPairingAgent(MAC)
    assert agent.target_address == MAC
    monkeypatch.setattr(bluez_support, "is_bluez_platform", lambda: False)
    assert await agent.start() is False
    await agent.stop()


async def test_doctor_warns_when_device_is_trusted_but_unpaired(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "is_bluez_platform", lambda: True)

    async def mock_adapters(*a, **k):
        return [{"name": "hci0", "powered": True, "pairable": True, "discovering": False}]

    async def mock_device(addr, *a, **k):
        return {
            "address": MAC,
            "name": "Mi Smart Band 6",
            "paired": False,
            "bonded": False,
            "trusted": True,
            "connected": False,
        }

    monkeypatch.setattr(doctor, "list_adapters", mock_adapters)
    monkeypatch.setattr(doctor, "find_device", mock_device)

    lines = []
    code = await doctor.run_doctor(MAC, KEY, str(tmp_path / "data"), emit=lines.append)
    text = "\n".join(lines)
    assert code == 0
    assert "is NOT paired/bonded" in text
    assert "bypassed" in text
