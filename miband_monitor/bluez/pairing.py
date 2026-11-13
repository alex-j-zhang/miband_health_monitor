"""In-process org.bluez.Agent1 pairing agent.

A band that is still bonded to a phone rejects a fresh pair with
AuthenticationCanceled, so in practice "trust" is what matters for
auto-reconnect.  When a one-time SMP bond IS needed, this agent auto-accepts
the passkey/authorization prompts for the target band only and rejects
everything else.

Cross-module calls go through the owning submodule objects (admin.*,
devices.*, platform.*) so tests can patch e.g.
miband_monitor.bluez.admin.set_device_trusted.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Optional

from . import admin, devices, platform
from .constants import (
    AGENT_MANAGER_IFACE,
    AGENT_MANAGER_PATH,
    BLUEZ_SERVICE,
    logger,
)


class BlueZPairingAgent:
    """In-process org.bluez.Agent1 pairing agent that auto-accepts auth for the target band."""

    def __init__(self, target_address: Optional[str] = None) -> None:
        self.target_address = (
            platform.normalize_address(target_address)
            if target_address and platform.looks_like_mac(target_address)
            else None
        )
        self._bus = None
        self._agent = None
        self._agent_path: Optional[str] = None
        self._registered = False

    async def start(self) -> bool:
        if not platform.is_bluez_platform():
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
        if not platform.is_bluez_platform():
            return
        info = await devices.find_device(address)
        actual_adapter = (info.get("adapter") if info else None) or adapter or "hci0"
        dev_path = (info.get("path") if info else None) or platform.device_path_for_address(
            address, actual_adapter
        )
        await admin.set_adapter_pairable(actual_adapter, True)
        await admin.set_device_trusted(dev_path, True)
