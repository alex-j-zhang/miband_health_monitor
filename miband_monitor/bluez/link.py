"""The long-running BlueZ Device1.Connect standby loop.

This is the link loop that suits a band which advertises a single isolated
pulse every 40-60 s (see BAND_PULSE_PERIOD).  It is implemented here because
bleak 3.0.2 cannot keep one Connect request pending across several 40 s kernel
cycles (its internal abort handling deadlocks), and because cancelling a
half-finished LE Create Connection can leave some controllers deaf to
advertising.

Cross-module calls go through the owning submodule object (devices.*,
platform.*) so tests can patch miband_monitor.bluez.devices._info_for_address
or miband_monitor.bluez.platform.is_bluez_platform.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Optional

from . import devices, platform
from .constants import (
    BAND_PULSE_PERIOD,
    BLUEZ_SERVICE,
    DBUS_TIMEOUT,
    DEVICE_IFACE,
    OBJECT_MANAGER_IFACE,
    OBJECT_MANAGER_PATH,
    logger,
)


async def wait_for_bluez_link(
    address: str,
    *,
    timeout: float = BAND_PULSE_PERIOD,
    poll_interval: float = 0.2,
    device_path: Optional[str] = None,
) -> dict:
    """Keep one BlueZ Device1.Connect in flight until the band answers.

    The loop:

    * never sends Disconnect and never abandons a request that is already
      pending - cancelling a half-finished LE Create Connection is what leaves
      the BARROT adapter reporting HCI command completions with no advertising
      events afterwards;
    * re-arms the next Connect immediately after the kernel gives up on the
      previous one (le-connection-abort-by-local after 40 s), so the controller
      is back in the initiating state before the next pulse;
    * keeps the request pending across several 40 s kernel cycles, which bleak
      3.0.2 cannot do because of its internal abort deadlock.

    Returns {"ready": bool, "reason": str, "info": dict | None,
    "attempts": int, "errors": list[str]}.  reason is a stable string:
    resolved, connect-timeout, services-timeout, device-gone, not-bluez,
    dbus-unavailable or dbus-error.
    """
    outcome: dict = {
        "ready": False,
        "reason": "not-bluez",
        "info": None,
        "attempts": 0,
        "errors": [],
    }
    if not platform.is_bluez_platform():
        return outcome
    try:
        from dbus_fast import BusType, Message
        from dbus_fast.aio import MessageBus
    except Exception as exc:  # pragma: no cover - dbus_fast ships with bleak
        outcome["reason"] = "dbus-unavailable"
        outcome["errors"].append(str(exc))
        return outcome

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(1.0, float(timeout))
    attempts = 0
    consecutive_errors = 0
    errors: list[str] = []
    bus = None
    last_path: Optional[str] = None
    pending: Optional[asyncio.Future] = None

    def _finish(reason: str, info: Optional[dict] = None) -> dict:
        outcome["reason"] = reason
        outcome["info"] = info
        outcome["attempts"] = attempts
        outcome["errors"] = errors
        return outcome

    try:
        bus = await asyncio.wait_for(
            MessageBus(bus_type=BusType.SYSTEM).connect(), timeout=DBUS_TIMEOUT
        )
        if device_path:
            # Fire Device1.Connect before introspect/GetManagedObjects so the
            # controller enters initiating state immediately after scan hit.
            last_path = device_path
            attempts += 1
            pending = asyncio.ensure_future(
                bus.call(
                    Message(
                        destination=BLUEZ_SERVICE,
                        path=device_path,
                        interface=DEVICE_IFACE,
                        member="Connect",
                    )
                )
            )
        introspection = await asyncio.wait_for(
            bus.introspect(BLUEZ_SERVICE, OBJECT_MANAGER_PATH), timeout=DBUS_TIMEOUT
        )
        proxy = bus.get_proxy_object(BLUEZ_SERVICE, OBJECT_MANAGER_PATH, introspection)
        manager = proxy.get_interface(OBJECT_MANAGER_IFACE)

        while True:
            try:
                objects = await asyncio.wait_for(
                    manager.call_get_managed_objects(), timeout=DBUS_TIMEOUT
                )
            except Exception as exc:  # transient bus hiccup: keep the loop alive
                errors.append(f"GetManagedObjects: {exc}")
                objects = {}

            info = devices._info_for_address(objects, address) if objects else None
            if info is None:
                return _finish("device-gone")

            if info["connected"] and info["services_resolved"]:
                logger.info(
                    "BlueZ link to %s is up and services are resolved (%d connect request(s)).",
                    address,
                    attempts,
                )
                outcome["ready"] = True
                return _finish("resolved", info)

            if loop.time() >= deadline:
                return _finish(
                    "services-timeout" if info["connected"] else "connect-timeout", info
                )

            if info["connected"]:
                # Link is up but BlueZ is still enumerating GATT: just wait, a
                # Disconnect here would throw away the only pulse we caught.
                await asyncio.sleep(min(poll_interval, 0.2))
                continue

            delay = poll_interval
            if pending is None or pending.done():
                if pending is not None:
                    exc = None
                    if not pending.cancelled():
                        exc = pending.exception()
                    pending = None
                    if exc is not None:
                        text = f"{type(exc).__name__}: {exc}"
                        errors.append(text)
                        # Give the controller a moment before re-arming; the
                        # useful case (kernel abort) still re-arms in ~150 ms.
                        if "abort-by-local" in text:
                            consecutive_errors = 0
                            delay = 0.15
                        elif "InProgress" in text:
                            # BlueZ/kernel auto-connect already owns the attempt.
                            consecutive_errors += 1
                            delay = 1.5
                        else:
                            # Back off progressively on repeated controller/stack
                            # errors so a sleeping band doesn't hammer hci0.
                            consecutive_errors += 1
                            delay = min(4.0, 1.0 * (2 ** min(consecutive_errors - 1, 2)))
                    else:
                        consecutive_errors = 0
                if info.get("path") and loop.time() < deadline:
                    last_path = info["path"]
                    attempts += 1
                    message = Message(
                        destination=BLUEZ_SERVICE,
                        path=info["path"],
                        interface=DEVICE_IFACE,
                        member="Connect",
                    )
                    pending = asyncio.ensure_future(bus.call(message))
            await asyncio.sleep(delay)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        outcome["reason"] = "dbus-error"
        errors.append(f"{type(exc).__name__}: {exc}")
        return outcome
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(pending, return_exceptions=True)
        if bus is not None:
            if outcome.get("reason") == "services-timeout" and last_path:
                # Only close a link that actually connected but hung during GATT
                # resolution; never Disconnect on connect-timeout (cancelling a
                # pending LE Create Connection can deafen BARROT adapters).
                with contextlib.suppress(Exception):
                    cancel_msg = Message(
                        destination=BLUEZ_SERVICE,
                        path=last_path,
                        interface=DEVICE_IFACE,
                        member="Disconnect",
                    )
                    await asyncio.wait_for(
                        bus.call(cancel_msg), timeout=min(DBUS_TIMEOUT, 2.0)
                    )
            with contextlib.suppress(Exception):
                bus.disconnect()
