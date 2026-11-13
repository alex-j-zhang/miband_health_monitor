"""Tests for BlueZ standby backoff, bounded direct probe, and MAC masking."""

from __future__ import annotations

import asyncio
import logging
import sys
import types
from types import SimpleNamespace

import pytest

from miband_monitor import bluez
from miband_monitor.ble.client import MiBand6Client
from miband_monitor.diagnostics.doctor import run_doctor


class _FakeScanner:
    def __init__(self, *, detection_callback, **_kwargs) -> None:
        self._cb = detection_callback

    async def start(self) -> None:
        self._cb(SimpleNamespace(address="C0:FF:EE:C0:FF:EE", name="Mi Smart Band 6"), None)

    async def stop(self) -> None:
        return None


@pytest.mark.asyncio
async def test_discover_device_logs_pulse_detected(caplog: pytest.LogCaptureFixture) -> None:
    client = MiBand6Client(
        "C0:FF:EE:C0:FF:EE",
        "0123456789abcdef0123456789abcdef",
        scanner_factory=_FakeScanner,
    )
    caplog.set_level(logging.INFO, logger="MiBand6Client")
    dev = await client._discover_device(5.0)
    assert dev.address == "C0:FF:EE:C0:FF:EE"
    assert any("Band pulse detected" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_open_bluez_link_caps_direct_and_preserves_post_scan_standby(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_timeouts: list[float] = []
    now = [1000.0]

    monkeypatch.setattr("miband_monitor.ble.link.time.monotonic", lambda: now[0])
    monkeypatch.setattr(bluez, "is_bluez_platform", lambda: True)
    monkeypatch.setattr(bluez, "link_standby_budget", lambda _t: 90.0)
    monkeypatch.setattr(bluez, "make_ble_device", lambda info: info.get("ble"))

    async def fake_wait_for_link(_addr: str, *, timeout: float, **_kw) -> dict:
        observed_timeouts.append(timeout)
        if len(observed_timeouts) == 1:
            now[0] += timeout
            return {"ready": False, "reason": "connect-timeout", "attempts": 5, "info": None}
        return {
            "ready": True,
            "reason": "resolved",
            "attempts": 1,
            "info": {"ble": SimpleNamespace(address="C0:FF:EE:C0:FF:EE")},
        }

    monkeypatch.setattr(bluez, "wait_for_bluez_link", fake_wait_for_link)

    class _FakeBle:
        async def connect(self) -> None:
            return None

    client = MiBand6Client(
        "C0:FF:EE:C0:FF:EE",
        "0123456789abcdef0123456789abcdef",
        client_factory=lambda _dev, **_kw: _FakeBle(),
    )

    async def fake_discover(_scan_timeout: float, deadline: float | None = None):
        assert deadline is not None
        now[0] += 55.0
        return SimpleNamespace(address="C0:FF:EE:C0:FF:EE")

    async def noop(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(client, "_discover_device", fake_discover)
    monkeypatch.setattr(client, "_ensure_bluez_ready", noop)

    await client._open_bluez_link(
        SimpleNamespace(address="C0:FF:EE:C0:FF:EE"),
        scan_timeout=90.0,
        connect_timeout=90.0,
    )
    assert len(observed_timeouts) == 2
    assert observed_timeouts[0] <= 15.0
    assert observed_timeouts[1] >= bluez.BAND_PULSE_PERIOD


@pytest.mark.asyncio
async def test_wait_for_bluez_link_progressive_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleep_calls: list[float] = []
    fake_time = [0.0]
    real_loop = asyncio.get_running_loop()
    real_sleep = asyncio.sleep

    class _LoopProxy:
        def time(self) -> float:
            return fake_time[0]

        def __getattr__(self, name: str):
            return getattr(real_loop, name)

    class _FakeManager:
        async def call_get_managed_objects(self) -> dict:
            return {"/org/bluez/hci0/dev_AA_BB_CC_DD_46_BE": {}}

    class _FakeProxy:
        def get_interface(self, _iface: str) -> _FakeManager:
            return _FakeManager()

    class _FakeBus:
        async def connect(self):
            return self

        async def introspect(self, *_args):
            return object()

        def get_proxy_object(self, *_args) -> _FakeProxy:
            return _FakeProxy()

        async def call(self, _msg) -> None:
            raise RuntimeError("br-connection-create-socket")

        def disconnect(self) -> None:
            return None

    fake_dbus_fast = types.ModuleType("dbus_fast")
    fake_dbus_fast.BusType = SimpleNamespace(SYSTEM=1)
    fake_dbus_fast.Message = lambda **_kw: object()
    fake_dbus_aio = types.ModuleType("dbus_fast.aio")
    fake_dbus_aio.MessageBus = lambda **_kw: _FakeBus()

    monkeypatch.setitem(sys.modules, "dbus_fast", fake_dbus_fast)
    monkeypatch.setitem(sys.modules, "dbus_fast.aio", fake_dbus_aio)
    monkeypatch.setattr(bluez, "is_bluez_platform", lambda: True)
    monkeypatch.setattr(
        bluez.devices,
        "_info_for_address",
        lambda _objs, _addr: {
            "path": "/org/bluez/hci0/dev_AA_BB_CC_DD_46_BE",
            "connected": False,
            "services_resolved": False,
        },
    )
    monkeypatch.setattr("miband_monitor.bluez.link.asyncio.get_running_loop", _LoopProxy)

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        fake_time[0] += delay
        await real_sleep(0)

    monkeypatch.setattr("miband_monitor.bluez.link.asyncio.sleep", fake_sleep)

    outcome = await bluez.wait_for_bluez_link("C0:FF:EE:C0:FF:EE", timeout=14.0)
    assert outcome["reason"] == "connect-timeout"
    error_sleeps = [d for d in sleep_calls if d >= 1.0]
    assert error_sleeps[:4] == [1.0, 2.0, 4.0, 4.0]


@pytest.mark.asyncio
async def test_doctor_masks_mac_in_hints(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    real_mac = "C0:FF:EE:C0:FF:EE"
    monkeypatch.setattr("miband_monitor.diagnostics.doctor.is_bluez_platform", lambda: True)

    async def fake_list_adapters():
        return [{"name": "hci0", "powered": True, "pairable": False, "discovering": False}]

    async def fake_find_device(_mac: str):
        return {
            "address": real_mac,
            "paired": False,
            "bonded": False,
            "trusted": False,
            "connected": False,
        }

    monkeypatch.setattr("miband_monitor.diagnostics.doctor.list_adapters", fake_list_adapters)
    monkeypatch.setattr("miband_monitor.diagnostics.doctor.find_device", fake_find_device)
    await run_doctor(real_mac, "0123456789abcdef0123456789abcdef", str(tmp_path))
    out = capsys.readouterr().out
    assert real_mac not in out
    assert "bluetoothctl trust <MAC>" in out
