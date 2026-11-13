"""Low-level BlueZ D-Bus plumbing.

Only thin, defensive wrappers live here: every call degrades to an empty result
when the system bus or the BlueZ service is unavailable, matching the exact
behaviour the historical bluez_support module provided on non-BlueZ hosts.

Cross-module calls go through the owning submodule object (platform.*) so tests
can monkeypatch e.g. miband_monitor.bluez.platform.is_bluez_platform and have it
steer every caller.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from . import platform
from .constants import (
    ADAPTER_IFACE,
    BLUEZ_SERVICE,
    DBUS_TIMEOUT,
    OBJECT_MANAGER_IFACE,
    OBJECT_MANAGER_PATH,
    logger,
)


def _unwrap(value: Any) -> Any:
    """Unwrap a dbus_fast Variant into its Python value."""
    return getattr(value, "value", value)


async def _managed_objects(timeout: float = DBUS_TIMEOUT) -> dict:
    """Return GetManagedObjects() from BlueZ, or {} when unavailable."""
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


async def list_adapters(*, timeout: float = DBUS_TIMEOUT) -> list:
    """Return the Bluetooth adapters BlueZ currently exposes."""
    if not platform.is_bluez_platform():
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
                "address": platform.normalize_address(
                    str(_unwrap(props.get("Address", "")) or "")
                ),
                "powered": bool(_unwrap(props.get("Powered", False))),
                "pairable": bool(_unwrap(props.get("Pairable", False))),
                "discovering": bool(_unwrap(props.get("Discovering", False))),
            }
        )
    return adapters
