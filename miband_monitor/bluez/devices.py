"""BlueZ Device1 discovery and cached-device fast paths.

These helpers only query the D-Bus object tree; they never scan or connect.
A device is "connected" only when both Connected and ServicesResolved are true
- Connected without ServicesResolved is almost always a stale/ghost link from
a peripheral that already dropped the connection.

Cross-module calls go through the owning submodule object (dbus.*, platform.*)
so tests can patch miband_monitor.bluez.dbus._managed_objects or
miband_monitor.bluez.platform.is_bluez_platform and affect every caller.
"""

from __future__ import annotations

from typing import Any, Optional

from . import dbus, platform
from .constants import DBUS_TIMEOUT, DEVICE_IFACE, logger


def _device_info_from_props(path: str, props: dict) -> dict:
    address = str(dbus._unwrap(props.get("Address", "")) or "")
    name = str(dbus._unwrap(props.get("Alias", "")) or dbus._unwrap(props.get("Name", "")) or "")
    adapter = path.split("/")[3] if path.startswith("/org/bluez/") and len(path.split("/")) > 3 else ""
    return {
        "path": path,
        "address": platform.normalize_address(address),
        "name": name,
        "adapter": adapter,
        "paired": bool(dbus._unwrap(props.get("Paired", False))),
        "bonded": bool(dbus._unwrap(props.get("Bonded", False))),
        "trusted": bool(dbus._unwrap(props.get("Trusted", False))),
        "connected": bool(dbus._unwrap(props.get("Connected", False))),
        "services_resolved": bool(dbus._unwrap(props.get("ServicesResolved", False))),
        "rssi": dbus._unwrap(props.get("RSSI")),
        "props": props,
    }


def _info_for_address(objects: dict, address: str) -> Optional[dict]:
    """Return the Device1 info dict for *address* inside GetManagedObjects."""
    wanted = platform.normalize_address(address)
    if not wanted:
        return None
    for path, interfaces in objects.items():
        props = interfaces.get(DEVICE_IFACE)
        if not props:
            continue
        info = _device_info_from_props(path, props)
        if info["address"] == wanted:
            return info
    return None


async def list_known_devices(*, timeout: float = DBUS_TIMEOUT) -> list[dict]:
    """Return devices known to BlueZ (cached or bonded)."""
    if not platform.is_bluez_platform():
        return []
    objects = await dbus._managed_objects(timeout=timeout)
    devices = []
    for path, interfaces in objects.items():
        props = interfaces.get(DEVICE_IFACE)
        if not props:
            continue
        devices.append(_device_info_from_props(path, props))
    return devices


async def find_device(address: str, *, timeout: float = DBUS_TIMEOUT) -> Optional[dict]:
    """Look up a device that BlueZ already knows about.

    Returns a dict with path/address/name/paired/trusted/connected/props, or
    None when the device is not in BlueZ's object tree (i.e. it has never been
    discovered on this host).
    """
    if not platform.is_bluez_platform():
        return None
    wanted = platform.normalize_address(address)
    if not wanted:
        return None

    objects = await dbus._managed_objects(timeout=timeout)
    return _info_for_address(objects, wanted)


def make_ble_device(info: dict) -> Optional[Any]:
    """Build a BLEDevice carrying BlueZ's object path.

    bleak skips discovery entirely when the device object already has a
    details["path"], which is what makes direct connection possible.
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


async def connected_device(address: str, *, timeout: float = DBUS_TIMEOUT) -> Optional[Any]:
    """Return a directly connectable BLEDevice if the device is ALREADY connected in BlueZ.

    A device is only considered genuinely connected if both Connected and
    ServicesResolved are True. If BlueZ shows Connected without
    ServicesResolved, it is almost always a stale/ghost connection from a
    disconnected peripheral.
    """
    if not platform.is_bluez_platform():
        return None
    info = await find_device(address, timeout=timeout)
    if info is None or not info.get("connected"):
        return None
    if not info.get("services_resolved"):
        logger.debug(
            "Device %s is marked Connected but ServicesResolved is False; stale connection.",
            address,
        )
        return None
    return make_ble_device(info)


async def paired_device(address: str, *, timeout: float = DBUS_TIMEOUT) -> Optional[Any]:
    """Return a directly connectable BLEDevice for a paired/bonded band.

    Only bonded devices are eligible: connecting to a merely cached or trusted
    device still requires it to advertise, which would just delay the fallback
    scan.
    """
    if not platform.is_bluez_platform():
        return None
    info = await find_device(address, timeout=timeout)
    if info is None:
        return None
    if not (info.get("paired") or info.get("bonded")):
        logger.debug("Device %s is known to BlueZ but not paired/bonded.", address)
        return None
    return make_ble_device(info)


async def active_cached_device(
    address: str,
    device_name: Optional[str] = None,
    *,
    timeout: float = DBUS_TIMEOUT,
) -> Optional[Any]:
    """Return a BLEDevice if BlueZ already has an active, recently seen cached object.

    Legacy fast path, kept for API compatibility but no longer used by the
    client: a Trusted device object keeps its last RSSI long after the band
    stopped advertising, so "RSSI is set" does not prove the band is reachable
    now.  Connecting to such a stale object burns the whole connect budget and
    misses the real pulse.  Use wait_for_bluez_link instead.
    """
    if not platform.is_bluez_platform():
        return None
    info = await find_device(address, timeout=timeout)
    if info is None:
        return None
    # If it has an RSSI property, it has been actively seen recently.
    if info.get("rssi") is not None:
        logger.info(
            "Found active cached advertisement for %s (RSSI=%s dBm); connecting directly.",
            address,
            info.get("rssi"),
        )
        return make_ble_device(info)
    return None
