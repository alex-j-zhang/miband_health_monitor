"""Translation of BLE/D-Bus exceptions into actionable log lines.

The hints below are field observations, not guesses: each one names the exact
fix that recovered the link on real hardware (bond/trust, bluetoothd restart,
adapter power, or a USB-level reset for the wedged BARROT controller).
"""

from __future__ import annotations

import asyncio

#: Human explanations for the D-Bus errors that actually show up in the field.
_DBUS_HINTS = {
    "org.freedesktop.DBus.Error.UnknownObject": (
        "BlueZ dropped the device object because the band disconnected. "
        "An unpaired/untrusted band leaves after ~30-40 s: mark it trusted with "
        "bluetoothctl trust <MAC> (a full pair is optional and can be "
        "rejected as AuthenticationCanceled by a band still bonded to a phone)."
    ),
    "org.freedesktop.DBus.Error.ServiceUnknown": (
        "the BlueZ service is not running: systemctl start bluetooth."
    ),
    "org.bluez.Error.InProgress": (
        "the adapter is busy with another scan or connection; retry in a moment."
    ),
    "org.bluez.Error.NotReady": (
        "the Bluetooth adapter is not powered: bluetoothctl power on."
    ),
    "org.bluez.Error.NotAvailable": (
        "the device is not available (out of range, powered off, or asleep)."
    ),
    "org.bluez.Error.Failed": (
        "BlueZ reported a failure (band busy, out of range, or still connected "
        "to a phone)."
    ),
    "org.bluez.Error.AuthenticationFailed": (
        "pairing failed; remove the stale bond (bluetoothctl remove <MAC>) "
        "and pair again."
    ),
    "org.bluez.Error.AuthenticationRejected": (
        "the band rejected the pairing request."
    ),
    "org.bluez.Error.AuthenticationTimeout": "pairing timed out.",
    "org.bluez.Error.NotAuthorized": (
        "the device is not authorized; pair/trust it first."
    ),
}


def is_transient_connect_error(exc: BaseException) -> bool:
    """Return True if exc represents a transient connection failure suitable for fast retry."""
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True
    name = getattr(exc, "dbus_error_name", None) or ""
    text = str(exc or "")
    transient_tokens = (
        "le-connection-abort-by-local",
        "org.bluez.Error.InProgress",
        "InProgress",
        "Software caused connection abort",
        "Connection refused",
        "Resource temporarily unavailable",
        "0x3e",
        "timed out",
        "not found",
        "UnknownObject",
        # The band drops the link while BlueZ is still enumerating the ~150 GATT
        # handles (its unauthenticated-connection watchdog fires after ~9 s).
        # This is the *most* recoverable failure: the band has just been woken up
        # and immediately advertises again, so it must be retried, not raised.
        "failed to discover services",
        "device disconnected",
        "Not connected",
        "Try again",
    )
    return any(token in text or token in name for token in transient_tokens)


def is_in_progress_error(exc: BaseException) -> bool:
    """Return True if exc represents an InProgress BlueZ GATT error."""
    name = getattr(exc, "dbus_error_name", None) or ""
    text = str(exc or "")
    return "org.bluez.Error.InProgress" in name or "InProgress" in text


def describe_ble_error(exc: BaseException) -> str:
    """Translate a BLE/D-Bus exception into one actionable log line."""
    name = getattr(exc, "dbus_error_name", None)
    text = str(exc or "")

    for key, hint in _DBUS_HINTS.items():
        if (name and name == key) or key in text:
            prefix = f"[{key}] " if key not in text else ""
            return f"{prefix}{hint}"

    if isinstance(exc, TimeoutError):
        return (
            "timed out (the band is out of range, still connected to a phone, or "
            "advertising is off).  Measured behaviour of a Mi Band 6 without a "
            "phone link: one isolated advertising pulse every 40-60 s and none at "
            "all while the screen is off - lighting or sliding the screen does NOT "
            "make it advertise faster.  Keep the link armed for at least "
            "--connect-timeout 90, and re-enable public broadcasting once through "
            "Zepp Life (with phone Bluetooth off afterwards)"
        )

    if "le-connection-abort-by-local" in text:
        return (
            "the kernel gave up one LE Create Connection after 40 s because no "
            "advertising pulse arrived inside the initiating window; the link "
            "attempt has to be re-armed for the band's next pulse (see "
            "miband_monitor.bluez.link.wait_for_bluez_link)"
        )

    if "-112" in text or "host is down" in text.lower() or "hostdown" in text.lower():
        return (
            "the controller reported EHOSTDOWN (-112); on the BARROT 33fa:0012 "
            "adapter this means the radio state machine is wedged.  A D-Bus "
            "Powered=false/true cycle is NOT enough - reset the USB device "
            "(sudo usbreset 33fa:0012) or unplug/replug it, and restart bluetooth"
        )

    class_name = type(exc).__name__
    if class_name == "BleakDeviceNotFoundError":
        return (
            "device not found while scanning (band screen off, out of range, "
            "or connected to a phone)"
        )
    if text:
        return f"{class_name}: {text}"
    return class_name
