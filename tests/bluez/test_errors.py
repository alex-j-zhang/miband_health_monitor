"""Translation of BlueZ/D-Bus failures into actionable guidance."""

from __future__ import annotations

from miband_monitor.bluez import errors


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------
def test_describe_ble_error_translates_unknown_object():
    exc = RuntimeError(
        '[org.freedesktop.DBus.Error.UnknownObject] Method "WriteValue" ... does not exist'
    )
    text = errors.describe_ble_error(exc)
    assert "disconnected" in text
    # A band that is still bonded to a phone rejects `pair` with
    # AuthenticationCanceled, so the hint must steer to `trust` instead.
    assert "bluetoothctl trust" in text
    assert "bluetoothctl pair" not in text


def test_describe_ble_error_uses_dbus_error_name():
    class FakeDbusError(Exception):
        dbus_error_name = "org.bluez.Error.InProgress"

    text = errors.describe_ble_error(FakeDbusError("nope"))
    assert "busy" in text


def test_describe_ble_error_handles_timeouts_and_missing_devices():
    assert "timed out" in errors.describe_ble_error(TimeoutError())
    assert "not found" in errors.describe_ble_error(
        type("BleakDeviceNotFoundError", (Exception,), {})("x")
    )


def test_describe_ble_error_falls_back_to_the_original_text():
    assert errors.describe_ble_error(ValueError("boom")) == "ValueError: boom"
