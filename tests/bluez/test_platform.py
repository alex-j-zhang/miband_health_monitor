"""Linux/BlueZ address, path, device-info and scanner-option helpers."""

from __future__ import annotations

import pytest

from miband_monitor.bluez import devices, platform, scanner
from miband_monitor.bluez.constants import DUPLICATE_DATA_ENV
from tests.support import MAC


# ---------------------------------------------------------------------------
# Address / path helpers
# ---------------------------------------------------------------------------
def test_normalize_address_accepts_common_spellings():
    assert platform.normalize_address("c0:ff:ee:c0:ff:ee") == "C0:FF:EE:C0:FF:EE"
    assert platform.normalize_address("C0-FF-EE-C0-FF-EE") == "C0:FF:EE:C0:FF:EE"
    assert platform.normalize_address("C0FFEEC0FFEE") == "C0:FF:EE:C0:FF:EE"
    assert platform.normalize_address("") == ""


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
    assert platform.looks_like_mac(value) is expected


def test_device_path_matches_bluez_naming():
    assert (
        platform.device_path_for_address("c0:ff:ee:c0:ff:ee")
        == "/org/bluez/hci0/dev_C0_FF_EE_C0_FF_EE"
    )
    assert (
        platform.device_path_for_address("C0:FF:EE:C0:FF:EE", adapter="hci1")
        == "/org/bluez/hci1/dev_C0_FF_EE_C0_FF_EE"
    )


def test_device_info_unwraps_dbus_variants():
    class Variant:
        def __init__(self, value):
            self.value = value

    info = devices._device_info_from_props(
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
    device = devices.make_ble_device(info)
    assert device is not None
    assert device.address == MAC
    assert device.details["path"] == info["path"]


def test_scanner_kwargs(monkeypatch):
    monkeypatch.setattr(platform, "is_bluez_platform", lambda: True)
    monkeypatch.delenv(DUPLICATE_DATA_ENV, raising=False)

    # DuplicateData=True makes BlueZ re-arm the LE scan every ~5 s on BT 5.x/6.x
    # controllers, which wedged the BARROT adapter in the field: default off.
    kwargs = scanner.scanner_kwargs()
    filters = kwargs.get("bluez", {}).get("filters", {})
    assert filters.get("DuplicateData") is False
    assert filters.get("Transport") == "le"

    # ... but the old behaviour stays available for experiments.
    monkeypatch.setenv(DUPLICATE_DATA_ENV, "1")
    assert (
        scanner.scanner_kwargs()
        .get("bluez", {})
        .get("filters", {})
        .get("DuplicateData")
        is True
    )

    class FakeBleakScanner:
        pass

    assert scanner.scanner_kwargs(FakeBleakScanner) == {}


def test_device_info_from_props_includes_rssi_and_bonded():
    props = {
        "Address": "FA:12:34:56:78:90",
        "Name": "Mi Band 6",
        "Paired": True,
        "Bonded": True,
        "Trusted": True,
        "Connected": False,
        "RSSI": -68,
    }
    info = devices._device_info_from_props("/org/bluez/hci1/dev_FA_12_34_56_78_90", props)
    assert info["address"] == "FA:12:34:56:78:90"
    assert info["adapter"] == "hci1"
    assert info["bonded"] is True
    assert info["rssi"] == -68
