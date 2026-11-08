"""Tests for cross-platform device identity handling.

On Linux/Windows the band is identified by its Bluetooth MAC address, while
macOS exposes a per-host CoreBluetooth UUID.  The client therefore accepts an
identifier (``DEVICE_ID`` / legacy ``DEVICE_MAC``) and an optional advertised
name substring (``DEVICE_NAME``).
"""

from miband_client import (
    MiBand6Client,
    _env_device_id,
    device_matches,
    is_placeholder_device_id,
    normalize_device_identifier,
    parse_args,
)
from sim_band import FakeAdvertisement, FakeDevice, SimulatedBand, make_simulated_client

MAC = "C0:FF:EE:C0:FF:EE"
KEY = "0123456789abcdef0123456789abcdef"


class _RawDevice:
    """Minimal BLEDevice stand-in with a configurable address/name."""

    def __init__(self, address: str, name: str = "") -> None:
        self.address = address
        self.name = name


def test_normalize_device_identifier_handles_common_formats():
    assert normalize_device_identifier("c0:ff:ee:c0:ff:ee") == "C0FFEEC0FFEE"
    assert normalize_device_identifier("C0-FF-EE-C0-FF-EE") == "C0FFEEC0FFEE"
    assert normalize_device_identifier("{1234ABCD-0000-1000-8000-00805F9B34FB}") == (
        "1234ABCD00001000800000805F9B34FB"
    )
    assert normalize_device_identifier("") == ""


def test_placeholder_detection():
    assert is_placeholder_device_id("")
    assert is_placeholder_device_id(None)
    assert is_placeholder_device_id("XX:XX:XX:XX:XX:XX")
    assert not is_placeholder_device_id(MAC)
    assert not is_placeholder_device_id("1234ABCD-0000-1000-8000-00805F9B34FB")


def test_device_matches_mac_with_different_separators():
    device = FakeDevice(MAC)
    assert device_matches("c0:ff:ee:c0:ff:ee", None, device)
    assert device_matches("C0-FF-EE-C0-FF-EE", None, device)
    assert device_matches("C0FFEEC0FFEE", None, device)


def test_device_matches_macos_style_uuid():
    uuid = "1234ABCD-0000-1000-8000-00805F9B34FB"
    device = _RawDevice(uuid)
    assert device_matches(uuid, None, device)
    assert device_matches(uuid.lower(), None, device)
    assert device_matches("{" + uuid + "}", None, device)


def test_device_matches_by_advertised_name():
    device = FakeDevice(MAC)
    advertisement = FakeAdvertisement(local_name="Mi Smart Band 6")
    # Identifier deliberately does not match; only the name does.
    assert device_matches("11:22:33:44:55:66", "mi smart band", device, advertisement)
    assert device_matches("", "Mi Smart Band 6", device, advertisement)


def test_device_does_not_match_unrelated_device():
    device = _RawDevice(MAC, name="Some Other Watch")
    advertisement = FakeAdvertisement(local_name="Some Other Watch")
    assert not device_matches("11:22:33:44:55:66", "Mi Smart Band", device, advertisement)


def test_env_device_id_prefers_real_value_over_placeholder(monkeypatch):
    monkeypatch.delenv("DEVICE_ID", raising=False)
    monkeypatch.setenv("DEVICE_MAC", MAC)
    assert _env_device_id() == MAC

    monkeypatch.setenv("DEVICE_ID", "XX:XX:XX:XX:XX:XX")
    assert _env_device_id() == MAC

    uuid = "1234ABCD-0000-1000-8000-00805F9B34FB"
    monkeypatch.setenv("DEVICE_ID", uuid)
    assert _env_device_id() == uuid


def test_parse_args_device_and_mtu_overrides(monkeypatch):
    monkeypatch.setenv("BLE_MTU", "247")
    args = parse_args(
        [
            "--device-id",
            "AA:BB:CC:DD:EE:FF",
            "--device-name",
            "Mi Band",
        ]
    )
    assert args.device_id == "AA:BB:CC:DD:EE:FF"
    assert args.device_name == "Mi Band"
    assert args.mtu == 247


def test_client_keeps_legacy_mac_address_attribute():
    client = MiBand6Client(MAC, KEY)
    assert client.device_id == MAC
    assert client.mac_address == MAC
    assert client.device_name is None


async def test_name_based_discovery_connects_to_simulated_band():
    """Simulate macOS: configured id is a UUID, only the name matches."""
    band = SimulatedBand(auth_key=bytes.fromhex(KEY))
    client = make_simulated_client(
        "1234ABCD-0000-1000-8000-00805F9B34FB",
        KEY,
        band,
        scanner_address=MAC,
        device_name="Mi Smart Band",
    )
    try:
        await client.connect_and_authenticate(scan_timeout=2.0)
        assert client.authenticated is True
    finally:
        await client.disconnect()
