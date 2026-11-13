"""The environment doctor command and its hardware guidance."""

from __future__ import annotations

from miband_monitor.diagnostics import doctor
from tests.support import KEY, MAC


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
    assert "is NOT bonded" in text
    assert "bluetoothctl trust" in text
    assert "bypassed" in text
