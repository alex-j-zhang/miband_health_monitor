"""Administrative BlueZ property writes.

These helpers change adapter/device state (pairable, trusted, Disconnect).
They are deliberately narrow: disconnect_device must only be used to clean up
a ghost connection (Connected=True with ServicesResolved=False) BEFORE a fresh
attempt.  Never cancel a pending LE Create Connection: on some controllers
(BARROT 33fa:0012) that leaves the adapter deaf to advertising until a
USB-level reset (see docs/validation/linux-ble-connect-findings.md).
"""

from __future__ import annotations

import asyncio

from . import devices, platform
from .constants import ADAPTER_IFACE, BLUEZ_SERVICE, DBUS_TIMEOUT, DEVICE_IFACE, logger


async def set_adapter_pairable(adapter: str = "hci0", pairable: bool = True, timeout: float = DBUS_TIMEOUT) -> bool:
    """Set the Pairable property on the BlueZ adapter."""
    if not platform.is_bluez_platform():
        return False
    try:
        from dbus_fast import BusType, Message, MessageType, Variant
        from dbus_fast.aio import MessageBus

        bus = await asyncio.wait_for(MessageBus(bus_type=BusType.SYSTEM).connect(), timeout=timeout)
        try:
            adapter_path = f"/org/bluez/{adapter}"
            msg = Message(
                destination=BLUEZ_SERVICE,
                path=adapter_path,
                interface="org.freedesktop.DBus.Properties",
                member="Set",
                signature="ssv",
                body=[ADAPTER_IFACE, "Pairable", Variant("b", pairable)],
            )
            reply = await asyncio.wait_for(bus.call(msg), timeout=timeout)
            return reply.message_type == MessageType.METHOD_RETURN or reply.message_type == 2
        finally:
            bus.disconnect()
    except Exception as exc:
        logger.debug("Failed to set adapter pairable: %s", exc)
        return False


async def set_device_trusted(device_path: str, trusted: bool = True, timeout: float = DBUS_TIMEOUT) -> bool:
    """Set the Trusted property on the BlueZ device."""
    if not platform.is_bluez_platform():
        return False
    try:
        from dbus_fast import BusType, Message, MessageType, Variant
        from dbus_fast.aio import MessageBus

        bus = await asyncio.wait_for(MessageBus(bus_type=BusType.SYSTEM).connect(), timeout=timeout)
        try:
            msg = Message(
                destination=BLUEZ_SERVICE,
                path=device_path,
                interface="org.freedesktop.DBus.Properties",
                member="Set",
                signature="ssv",
                body=[DEVICE_IFACE, "Trusted", Variant("b", trusted)],
            )
            reply = await asyncio.wait_for(bus.call(msg), timeout=timeout)
            return reply.message_type == MessageType.METHOD_RETURN or reply.message_type == 2
        finally:
            bus.disconnect()
    except Exception as exc:
        logger.debug("Failed to set device trusted: %s", exc)
        return False


async def disconnect_device(address: str, timeout: float = DBUS_TIMEOUT) -> bool:
    """Explicitly call Disconnect() on the device's BlueZ D-Bus object if connected.

    Only safe for cleaning up a ghost connection (Connected=True with
    ServicesResolved=False) before starting a fresh attempt.  Never call it
    while an LE Create Connection is still pending: cancelling that request is
    what leaves some controllers (BARROT 33fa:0012) deaf to advertising until a
    USB-level reset (see docs/validation/linux-ble-connect-findings.md).
    """
    if not platform.is_bluez_platform():
        return False
    info = await devices.find_device(address, timeout=timeout)
    if not info or not info.get("connected") or not info.get("path"):
        return False
    from dbus_fast import BusType, Message
    from dbus_fast.aio import MessageBus

    try:
        bus = await asyncio.wait_for(MessageBus(bus_type=BusType.SYSTEM).connect(), timeout=timeout)
        try:
            msg = Message(
                destination=BLUEZ_SERVICE,
                path=info["path"],
                interface=DEVICE_IFACE,
                member="Disconnect",
            )
            await asyncio.wait_for(bus.call(msg), timeout=timeout)
            return True
        finally:
            bus.disconnect()
    except Exception as exc:
        logger.debug("Failed to disconnect BlueZ device: %s", exc)
        return False
