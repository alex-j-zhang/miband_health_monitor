"""Linux / BlueZ compatibility helpers.

The BlueZ backend behaves differently from WinRT and CoreBluetooth in three
places that matter for connection robustness:

1. **Device objects are transient.**  A device that is not paired/trusted is
   removed from the BlueZ object tree as soon as it disconnects, so later GATT
   writes fail with ``org.freedesktop.DBus.Error.UnknownObject``.  A band that
   is not bonded also drops the link after roughly 40 s.
2. **``bleak`` must scan before it can connect.**  Passing a MAC address to
   ``BleakClient`` triggers a fresh discovery, even when BlueZ already knows
   the device.  A paired band is reachable through its cached D-Bus object,
   which lets us connect without waiting for an advertisement.
3. **The ATT MTU is never acquired implicitly.**  ``mtu_size`` stays at 23 (and
   emits a ``UserWarning``) until the backend's ``_acquire_mtu()`` is called.

Every helper here degrades to a no-op on non-BlueZ platforms, so importing this
module is always safe on Windows and macOS.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import sys
from typing import Any, Optional

logger = logging.getLogger("MiBand6Client.bluez")

BLUEZ_SERVICE = "org.bluez"
OBJECT_MANAGER_PATH = "/"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"

#: Default timeout for one short D-Bus introspection round trip.
DBUS_TIMEOUT = 2.0

#: Human explanations for the D-Bus errors that actually show up in the field.
_DBUS_HINTS = {
    "org.freedesktop.DBus.Error.UnknownObject": (
        "BlueZ dropped the device object because the band disconnected. "
        "An unpaired/untrusted band leaves after ~40 s: pair it once with "
        "`bluetoothctl pair <MAC>` and `bluetoothctl trust <MAC>`."
    ),
    "org.freedesktop.DBus.Error.ServiceUnknown": (
        "the BlueZ service is not running: `systemctl start bluetooth`."
    ),
    "org.bluez.Error.InProgress": (
        "the adapter is busy with another scan or connection; retry in a moment."
    ),
    "org.bluez.Error.NotReady": (
        "the Bluetooth adapter is not powered: `bluetoothctl power on`."
    ),
    "org.bluez.Error.NotAvailable": (
        "the device is not available (out of range, powered off, or asleep)."
    ),
    "org.bluez.Error.Failed": (
        "BlueZ reported a failure (band busy, out of range, or still connected "
        "to a phone)."
    ),
    "org.bluez.Error.AuthenticationFailed": (
        "pairing failed; remove the stale bond (`bluetoothctl remove <MAC>`) "
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


def is_bluez_platform() -> bool:
    """True on Linux, where ``bleak`` talks to BlueZ over D-Bus."""
    return sys.platform.startswith("linux")


def normalize_address(address: str) -> str:
    """Return the canonical ``AA:BB:CC:DD:EE:FF`` form of a MAC address."""
    raw = (address or "").strip().upper().replace("-", ":")
    compact = "".join(ch for ch in raw if ch in "0123456789ABCDEF")
    if len(compact) == 12:
        return ":".join(compact[i : i + 2] for i in range(0, 12, 2))
    return raw


def looks_like_mac(address: str) -> bool:
    return len(normalize_address(address)) == 17 and normalize_address(address).count(":") == 5


def device_path_for_address(address: str, adapter: str = "hci0") -> str:
    """Return the deterministic BlueZ object path for *address*.

    BlueZ names device objects ``dev_AA_BB_CC_DD_EE_FF`` under the adapter that
    discovered them, so the path can be predicted without a discovery pass.
    """
    return f"/org/bluez/{adapter}/dev_{normalize_address(address).replace(':', '_')}"


def _unwrap(value: Any) -> Any:
    """Unwrap a ``dbus_fast`` ``Variant`` into its Python value."""
    return getattr(value, "value", value)


async def _managed_objects(timeout: float = DBUS_TIMEOUT) -> dict:
    """Return ``GetManagedObjects()`` from BlueZ, or ``{}`` when unavailable."""
    try:  # imported lazily: dbus_fast only exists on Linux (via bleak)
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus
    except Exception:  # pragma: no cover - non-Linux hosts
        return {}

    bus = None
    try:
        bus = await asyncio.wait_for(
            MessageBus(bus_type=BusType.SYSTEM).connect(), timeout=timeout
        )
        introspection = await asyncio.wait_for(
            bus.introspect(BLUEZ_SERVICE, OBJECT_MANAGER_PATH), timeout=timeout
        )
        proxy = bus.get_proxy_object(BLUEZ_SERVICE, OBJECT_MANAGER_PATH, introspection)
        manager = proxy.get_interface(OBJECT_MANAGER_IFACE)
        return await asyncio.wait_for(manager.call_get_managed_objects(), timeout=timeout)
    except Exception as exc:
        logger.debug("BlueZ ObjectManager query failed: %s", exc)
        return {}
    finally:
        if bus is not None:
            with contextlib.suppress(Exception):
                bus.disconnect()


def _device_info_from_props(path: str, props: dict) -> dict:
    address = str(_unwrap(props.get("Address", "")) or "")
    name = str(_unwrap(props.get("Alias", "")) or _unwrap(props.get("Name", "")) or "")
    adapter = path.split("/")[3] if path.startswith("/org/bluez/") and len(path.split("/")) > 3 else ""
    return {
        "path": path,
        "address": normalize_address(address),
        "name": name,
        "adapter": adapter,
        "paired": bool(_unwrap(props.get("Paired", False))),
        "trusted": bool(_unwrap(props.get("Trusted", False))),
        "connected": bool(_unwrap(props.get("Connected", False))),
        "props": props,
    }


async def find_device(address: str, *, timeout: float = DBUS_TIMEOUT) -> Optional[dict]:
    """Look up a device that BlueZ already knows about.

    Returns a dict with ``path``/``address``/``name``/``paired``/``trusted``/
    ``connected``/``props``, or ``None`` when the device is not in BlueZ's
    object tree (i.e. it has never been discovered on this host).
    """
    if not is_bluez_platform():
        return None
    wanted = normalize_address(address)
    if not wanted:
        return None

    objects = await _managed_objects(timeout=timeout)
    for path, interfaces in objects.items():
        props = interfaces.get(DEVICE_IFACE)
        if not props:
            continue
        info = _device_info_from_props(path, props)
        if info["address"] == wanted:
            return info
    return None


async def list_adapters(*, timeout: float = DBUS_TIMEOUT) -> list:
    """Return the Bluetooth adapters BlueZ currently exposes."""
    if not is_bluez_platform():
        return []

    objects = await _managed_objects(timeout=timeout)
    adapters = []
    for path, interfaces in objects.items():
        props = interfaces.get(ADAPTER_IFACE)
        if not props:
            continue
        adapters.append(
            {
                "path": path,
                "name": path.rsplit("/", 1)[-1],
                "address": normalize_address(str(_unwrap(props.get("Address", "")) or "")),
                "powered": bool(_unwrap(props.get("Powered", False))),
                "pairable": bool(_unwrap(props.get("Pairable", False))),
                "discovering": bool(_unwrap(props.get("Discovering", False))),
            }
        )
    return adapters


def make_ble_device(info: dict) -> Optional[Any]:
    """Build a ``BLEDevice`` carrying BlueZ's object path.

    ``bleak`` skips discovery entirely when the device object already has a
    ``details["path"]``, which is what makes direct connection possible.
    """
    try:
        from bleak.backends.device import BLEDevice
    except Exception:  # pragma: no cover - bleak always present in practice
        return None

    path = info.get("path")
    if not path:
        return None
    details = {"path": path, "props": info.get("props") or {}}
    try:
        return BLEDevice(info.get("address") or "", info.get("name") or None, details)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not build BLEDevice from BlueZ props: %s", exc)
        return None


async def paired_device(address: str, *, timeout: float = DBUS_TIMEOUT) -> Optional[Any]:
    """Return a directly connectable ``BLEDevice`` for a paired/trusted band.

    Only bonded devices are eligible: connecting to a merely cached device
    still requires it to advertise, which would just delay the fallback scan.
    """
    if not is_bluez_platform():
        return None
    info = await find_device(address, timeout=timeout)
    if info is None:
        return None
    if not (info["paired"] or info["trusted"]):
        logger.debug("Device %s is known to BlueZ but not paired/trusted.", address)
        return None
    return make_ble_device(info)


async def try_acquire_mtu(client: Any) -> bool:
    """Ask bleak's BlueZ backend for the negotiated ATT MTU.

    ``bleak`` leaves ``mtu_size`` at 23 until ``_acquire_mtu()`` is called; the
    WinRT and CoreBluetooth backends fill it in automatically.  Returns True
    when an acquisition was attempted successfully.
    """
    if not is_bluez_platform():
        return False

    backend = getattr(client, "_backend", None)
    acquire = getattr(backend, "_acquire_mtu", None)
    if acquire is None:
        return False

    try:
        get_services = getattr(client, "get_services", None)
        if get_services is not None:
            with contextlib.suppress(Exception):
                await get_services()
        result = acquire()
        # bleak versions differ: `_acquire_mtu` is a coroutine in 3.x.
        if inspect.isawaitable(result):
            await result
        return True
    except Exception as exc:
        logger.debug("Could not acquire the ATT MTU: %s", exc)
        return False


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
            "timed out (the band is asleep, out of range, or still paired with "
            "a phone; keep the screen awake and retry)"
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
