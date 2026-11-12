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
import os
import sys
from typing import Any, Optional

logger = logging.getLogger("MiBand6Client.bluez")

BLUEZ_SERVICE = "org.bluez"
OBJECT_MANAGER_PATH = "/"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"

AGENT_MANAGER_PATH = "/org/bluez"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"

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
        "bonded": bool(_unwrap(props.get("Bonded", False))),
        "trusted": bool(_unwrap(props.get("Trusted", False))),
        "connected": bool(_unwrap(props.get("Connected", False))),
        "rssi": _unwrap(props.get("RSSI")),
        "props": props,
    }


async def list_known_devices(*, timeout: float = DBUS_TIMEOUT) -> list[dict]:
    """Return devices known to BlueZ (cached or bonded)."""
    if not is_bluez_platform():
        return []
    objects = await _managed_objects(timeout=timeout)
    devices = []
    for path, interfaces in objects.items():
        props = interfaces.get(DEVICE_IFACE)
        if not props:
            continue
        devices.append(_device_info_from_props(path, props))
    return devices


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


async def connected_device(address: str, *, timeout: float = DBUS_TIMEOUT) -> Optional[Any]:
    """Return a directly connectable ``BLEDevice`` if the device is ALREADY connected in BlueZ."""
    if not is_bluez_platform():
        return None
    info = await find_device(address, timeout=timeout)
    if info is None or not info.get("connected"):
        return None
    return make_ble_device(info)


async def paired_device(address: str, *, timeout: float = DBUS_TIMEOUT) -> Optional[Any]:
    """Return a directly connectable ``BLEDevice`` for a paired/bonded band.

    Only bonded devices are eligible: connecting to a merely cached or trusted
    device still requires it to advertise, which would just delay the fallback scan.
    """
    if not is_bluez_platform():
        return None
    info = await find_device(address, timeout=timeout)
    if info is None:
        return None
    if not (info.get("paired") or info.get("bonded")):
        logger.debug("Device %s is known to BlueZ but not paired/bonded.", address)
        return None
    return make_ble_device(info)


def is_transient_connect_error(exc: BaseException) -> bool:
    """Return True if exc represents a transient connection failure suitable for fast retry."""
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
    )
    return any(token in text or token in name for token in transient_tokens)


def is_in_progress_error(exc: BaseException) -> bool:
    """Return True if exc represents an InProgress BlueZ GATT error."""
    name = getattr(exc, "dbus_error_name", None) or ""
    text = str(exc or "")
    return "org.bluez.Error.InProgress" in name or "InProgress" in text


def scanner_kwargs(scanner_factory: Any = None) -> dict:
    """Return platform-specific kwargs for BleakScanner.

    On Linux with BleakScanner, enable DuplicateData=True so BlueZ does not
    suppress consecutive advertisements from the same band when asleep/low-power.
    """
    if not is_bluez_platform():
        return {}
    if scanner_factory is not None:
        factory_name = getattr(scanner_factory, "__name__", "")
        module_name = getattr(scanner_factory, "__module__", "")
        if "sim" in module_name or "Fake" in factory_name:
            return {}
    return {
        "bluez": {
            "filters": {
                "DuplicateData": True,
                "Transport": "le",
            }
        }
    }


async def set_adapter_pairable(adapter: str = "hci0", pairable: bool = True, timeout: float = DBUS_TIMEOUT) -> bool:
    """Set the Pairable property on the BlueZ adapter."""
    if not is_bluez_platform():
        return False
    try:
        from dbus_fast import BusType, Message, Variant
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
            return reply.message_type == 2  # METHOD_RETURN
        finally:
            bus.disconnect()
    except Exception as exc:
        logger.debug("Failed to set adapter pairable: %s", exc)
        return False


async def set_device_trusted(device_path: str, trusted: bool = True, timeout: float = DBUS_TIMEOUT) -> bool:
    """Set the Trusted property on the BlueZ device."""
    if not is_bluez_platform():
        return False
    try:
        from dbus_fast import BusType, Message, Variant
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
            return reply.message_type == 2
        finally:
            bus.disconnect()
    except Exception as exc:
        logger.debug("Failed to set device trusted: %s", exc)
        return False


class BlueZPairingAgent:
    """In-process org.bluez.Agent1 pairing agent that auto-accepts auth for the target band."""

    def __init__(self, target_address: Optional[str] = None) -> None:
        self.target_address = (
            normalize_address(target_address)
            if target_address and looks_like_mac(target_address)
            else None
        )
        self._bus = None
        self._agent = None
        self._agent_path: Optional[str] = None
        self._registered = False

    async def start(self) -> bool:
        if not is_bluez_platform():
            return False
        if self._registered:
            return True
        try:
            from dbus_fast import BusType, Message, MessageType
            from dbus_fast.aio import MessageBus
            from dbus_fast.errors import DBusError
            from dbus_fast.service import ServiceInterface, method
        except Exception as exc:
            logger.debug("Cannot import dbus_fast for pairing agent: %s", exc)
            return False

        agent_parent = self

        def _is_target(device_path: str) -> bool:
            target = agent_parent.target_address
            if not target:
                return True
            suffix = target.replace(":", "_").upper()
            return str(device_path).rstrip("/").upper().endswith(suffix)

        class _AgentImpl(ServiceInterface):
            def __init__(self):
                super().__init__("org.bluez.Agent1")

            @method()
            def Release(self):
                logger.debug("Pairing agent released by BlueZ.")

            @method()
            def RequestPinCode(self, device: "o") -> "s":
                if not _is_target(device):
                    raise DBusError("org.bluez.Error.Rejected", "Unauthorized device")
                logger.info("Auto-accepting PIN code request for %s", device)
                return "0000"

            @method()
            def DisplayPinCode(self, device: "o", pincode: "s"):
                pass

            @method()
            def RequestPasskey(self, device: "o") -> "u":
                if not _is_target(device):
                    raise DBusError("org.bluez.Error.Rejected", "Unauthorized device")
                logger.info("Auto-accepting passkey request for %s", device)
                return 0

            @method()
            def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
                pass

            @method()
            def RequestConfirmation(self, device: "o", passkey: "u"):
                if not _is_target(device):
                    raise DBusError("org.bluez.Error.Rejected", "Unauthorized device")
                logger.info("Auto-confirming pairing passkey %06d for target band (%s)", passkey, device)
                return None

            @method()
            def RequestAuthorization(self, device: "o"):
                if not _is_target(device):
                    raise DBusError("org.bluez.Error.Rejected", "Unauthorized device")
                logger.info("Auto-authorizing pairing request for target band (%s)", device)
                return None

            @method()
            def AuthorizeService(self, device: "o", uuid: "s"):
                if not _is_target(device):
                    raise DBusError("org.bluez.Error.Rejected", "Unauthorized device")
                logger.debug("Auto-authorizing service %s for target band (%s)", uuid, device)
                return None

            @method()
            def Cancel(self):
                logger.debug("Pairing agent request cancelled.")

        try:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            self._agent_path = f"/miband/agent/{os.getpid()}_{id(self)}"
            self._agent = _AgentImpl()
            self._bus.export(self._agent_path, self._agent)

            reg_msg = Message(
                destination=BLUEZ_SERVICE,
                path=AGENT_MANAGER_PATH,
                interface=AGENT_MANAGER_IFACE,
                member="RegisterAgent",
                signature="os",
                body=[self._agent_path, "NoInputNoOutput"],
            )
            reply = await self._bus.call(reg_msg)
            if reply.message_type != MessageType.METHOD_RETURN:
                logger.warning("Could not register pairing agent: %s", reply.body)
                await self.stop()
                return False

            req_def = Message(
                destination=BLUEZ_SERVICE,
                path=AGENT_MANAGER_PATH,
                interface=AGENT_MANAGER_IFACE,
                member="RequestDefaultAgent",
                signature="o",
                body=[self._agent_path],
            )
            with contextlib.suppress(Exception):
                await self._bus.call(req_def)

            self._registered = True
            logger.debug("BlueZ in-process pairing agent registered at %s", self._agent_path)
            return True
        except Exception as exc:
            logger.warning("Failed to initialize BlueZ pairing agent: %s", exc)
            await self.stop()
            return False

    async def stop(self) -> None:
        if not self._registered and self._bus is None:
            return
        bus = self._bus
        agent = self._agent
        agent_path = self._agent_path
        self._registered = False
        self._bus = None
        self._agent = None
        self._agent_path = None

        if bus is not None and agent_path is not None:
            try:
                from dbus_fast import Message

                unreg = Message(
                    destination=BLUEZ_SERVICE,
                    path=AGENT_MANAGER_PATH,
                    interface=AGENT_MANAGER_IFACE,
                    member="UnregisterAgent",
                    signature="o",
                    body=[agent_path],
                )
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(bus.call(unreg), timeout=1.0)
            except Exception:
                pass
            if agent is not None:
                with contextlib.suppress(Exception):
                    bus.unexport(agent_path, agent)
            with contextlib.suppress(Exception):
                bus.disconnect()

    async def ensure_ready_for_device(self, address: str, adapter: Optional[str] = None) -> None:
        """Ensure the adapter is pairable and the target device is marked trusted in BlueZ."""
        if not is_bluez_platform():
            return
        info = await find_device(address)
        actual_adapter = (info.get("adapter") if info else None) or adapter or "hci0"
        dev_path = (info.get("path") if info else None) or device_path_for_address(address, actual_adapter)
        await set_adapter_pairable(actual_adapter, True)
        await set_device_trusted(dev_path, True)


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
            "timed out (the band is asleep, out of range, still paired with a "
            "phone, or public broadcasting is off; keep sliding the band screen, "
            "confirm 'Discoverable' is on in Zepp Life, and retry)"
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
