"""Discovery mixin: locate the band via a scan or the bonded BlueZ device.

A Mi Band 6 that is not linked to a phone emits a single isolated advertising
pulse every 40-60 s and does not advertise at all while its screen is off
(measured 2026-11-12). Discovery therefore has two strategies:

* reuse a genuinely connected/bonded BlueZ object so no advertisement is needed
  (Linux/BlueZ only, see _direct_connect_target);
* otherwise run a BleakScanner long enough to catch a pulse, logging progress
  and warning when the adapter itself appears deaf.

This mixin is combined into MiBand6Client; it assumes the attributes created by
miband_monitor.ble.client.MiBand6Client.__init__ (device_id, device_name,
_client_factory, _bleak_client_class, _scanner_factory, _direct_connect, ...).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Optional

from .. import bluez
from ..config.defaults import (
    DEFAULT_SCAN_HEARTBEAT_SECONDS,
    DEFAULT_SCAN_SILENCE_WARNING_SECONDS,
    ENV_BLUEZ_TRUST,
)
from ..config.env import env_str
from .identity import device_matches

logger = logging.getLogger("MiBand6Client")


class DiscoveryMixin:
    """Scanning, direct-connect selection and BlueZ trust bookkeeping."""

    def _describe_target(self) -> str:
        """Human-readable discovery target for logs and timeout messages."""
        if self.device_name:
            return f"{self.device_id} (or a name containing {self.device_name!r})"
        return self.device_id

    def _bluez_link_mode(self) -> bool:
        """True when the real BlueZ backend is driving this client."""
        return (
            self._client_factory is self._bleak_client_class
            and bluez.is_bluez_platform()
            and bluez.looks_like_mac(self.device_id)
        )

    # ---------------------------------------------------------------- discovery
    async def _direct_connect_target(self):
        """Return a directly connectable BLEDevice for an already-bonded band.

        Only a *bonded* band is eligible.  A bonded device keeps a BlueZ object
        path that can be addressed without a fresh advertisement, and its GATT
        attributes are cached, so the ~150-handle enumeration does not have to
        finish inside the band's ~9 s unauthenticated-connection watchdog.

        A merely cached/trusted object is deliberately NOT used: BlueZ keeps the
        last RSSI of a Trusted device long after it stopped advertising, so a
        "fresh advertisement" check based on RSSI is not fresh at all -
        connecting to it burns the budget and misses the real pulse.
        """
        if not self._direct_connect:
            return None
        if self._client_factory is not self._bleak_client_class:
            return None
        if not bluez.is_bluez_platform():
            return None
        if not bluez.looks_like_mac(self.device_id):
            return None

        # 1. If genuinely connected in BlueZ with services resolved, reuse the
        #    existing connection.
        connected_dev = await bluez.connected_device(self.device_id)
        if connected_dev is not None:
            logger.info(
                "Mi Band %s is already connected in BlueZ; reusing connection.",
                self.device_id,
            )
            return connected_dev

        # 2. Clean up a ghost connection (Connected without ServicesResolved);
        #    nothing is pending, so Disconnect() is safe here.
        await bluez.disconnect_device(self.device_id)

        # 3. If properly paired/bonded in BlueZ, attempt fast direct connection.
        device = await bluez.paired_device(self.device_id)
        if device is not None:
            logger.info(
                "Bonded band %s found in the BlueZ object tree; skipping discovery.",
                self.device_id,
            )
        return device

    async def _ensure_bluez_ready(self, device: Any = None) -> None:
        """Mark the device trusted so BlueZ keeps the object and caches attributes.

        Trusted=True is what makes the *next* run cheap: BlueZ keeps the
        discovered GATT table and enables kernel auto-connect, which is how a
        sleeping band can be re-linked without a scan (measured 2026-11-12:
        six consecutive auto-connects ~61 s apart).  Set MIBAND_BLUEZ_TRUST=0
        to opt out for A/B experiments.
        """
        if not bluez.is_bluez_platform() or self._client_factory is not self._bleak_client_class:
            return
        if env_str(ENV_BLUEZ_TRUST, "1").lower() in ("0", "false", "no", "off"):
            return
        target_mac = (
            getattr(device, "address", None)
            or getattr(self.client, "address", None)
            or self.device_id
        )
        if not bluez.looks_like_mac(target_mac):
            return
        details = getattr(device, "details", None)
        dev_path = (
            details.get("path")
            if isinstance(details, dict) and details.get("path")
            else bluez.device_path_for_address(target_mac)
        )
        if dev_path not in self._trusted_paths:
            with contextlib.suppress(Exception):
                if await bluez.set_device_trusted(dev_path, True):
                    self._trusted_paths.add(dev_path)
        if self._pairing_agent is not None:
            self._pairing_agent.target_address = bluez.normalize_address(target_mac)
            with contextlib.suppress(Exception):
                await self._pairing_agent.ensure_ready_for_device(target_mac)

    async def _discover_device(self, scan_timeout: float, deadline: Optional[float] = None):
        """Run a BLE scan until the first matching advertisement arrives."""
        found_device = None
        stop_event = asyncio.Event()
        stats = {"band": 0, "other": 0}

        def _on_detection(device, advertisement) -> None:
            nonlocal found_device
            if device_matches(self.device_id, self.device_name, device, advertisement):
                stats["band"] += 1
                found_device = device
                stop_event.set()
            else:
                stats["other"] += 1

        wait_for = float(scan_timeout)
        if deadline is not None:
            wait_for = max(1.0, min(wait_for, deadline - time.monotonic()))

        scanner_kwargs = bluez.scanner_kwargs(self._scanner_factory)
        scanner = self._scanner_factory(detection_callback=_on_detection, **scanner_kwargs)
        await scanner.start()
        heartbeat = asyncio.create_task(self._scan_heartbeat(stop_event, stats))
        try:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait_for)
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"Could not discover band {self._describe_target()} "
                    f"within {wait_for:.0f}s. "
                    "Please verify: 1) Phone Bluetooth is OFF / Airplane mode. "
                    "2) The band still has public broadcasting enabled (open Zepp "
                    "Life once, then switch phone Bluetooth off). 3) The band "
                    "screen is on - but note that a Mi Band 6 without a phone link "
                    "only emits one advertising pulse every 40-60 s, so use "
                    "--scan-timeout 120. 4) On Linux, if not a single BLE packet "
                    "(from any device) arrived, the USB adapter may be wedged: "
                    "`sudo usbreset <vid:pid>` and `sudo systemctl restart bluetooth`."
                ) from None
            if found_device is not None:
                logger.info(
                    "Band pulse detected (%s); arming BLE link...",
                    self._describe_target(),
                )
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            await scanner.stop()
        if found_device is not None:
            await self._ensure_bluez_ready(found_device)
        return found_device

    async def _scan_heartbeat(self, stop_event: asyncio.Event, stats: dict) -> None:
        """Log scan progress every 5 s and warn about a deaf adapter after 25 s."""
        step = int(DEFAULT_SCAN_HEARTBEAT_SECONDS)
        elapsed = 0
        warned = False
        while not stop_event.is_set():
            await asyncio.sleep(DEFAULT_SCAN_HEARTBEAT_SECONDS)
            if stop_event.is_set():
                return
            elapsed += step
            packets = stats.get("band", 0) + stats.get("other", 0)
            logger.info(
                "Scanning for band... (%ds elapsed, band=%d packets, other BLE=%d; "
                "the band emits one pulse every 40-60 s and sliding the screen does "
                "not speed that up)",
                elapsed,
                stats.get("band", 0),
                stats.get("other", 0),
            )
            if packets == 0 and elapsed >= DEFAULT_SCAN_SILENCE_WARNING_SECONDS and not warned:
                warned = True
                logger.warning(
                    "No BLE packet from ANY device for %ds while scanning. If other "
                    "Bluetooth devices are around, the adapter itself is probably "
                    "deaf (the BARROT BT 6.0 adapter does this after an aborted "
                    "connect, and a D-Bus power cycle does not recover it). Reset "
                    "the USB device: `sudo usbreset 33fa:0012` (or unplug/replug) "
                    "then `sudo systemctl restart bluetooth`.",
                    elapsed,
                )
