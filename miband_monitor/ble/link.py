"""Link mixin: turn a discovered band into an open BLE connection.

Two strategies exist, chosen by the backend:

* Linux/BlueZ keeps a single Device1.Connect armed across the band's full
  ~70 s advertising period and hands the established link to bleak (see
  _open_bluez_link); re-scanning between short attempts is what creates the
  "leapfrog" trap that makes a sleeping band impossible to reach.
* Windows/macOS (and injected test factories) use the plain scan -> connect
  path with a shorter direct-connect attempt for an already-bonded device.

This mixin is combined into MiBand6Client; it assumes the attributes created by
MiBand6Client.__init__ (device_id, _client_factory, client, ...).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from .. import bluez
from ..config.defaults import DEFAULT_DIRECT_CONNECT_TIMEOUT

logger = logging.getLogger("MiBand6Client")

#: Upper bound for handing an already-established BlueZ link to bleak, and for
#: the fast "direct connect" attempt on non-BlueZ backends before a scan.
DIRECT_CONNECT_TIMEOUT = DEFAULT_DIRECT_CONNECT_TIMEOUT


class LinkMixin:
    """BLE link establishment, retries and the disconnect watchdog callback."""

    async def _open_client(self, direct_device, scan_timeout: float, connect_timeout: float) -> None:
        """Open the BLE link, using the BlueZ-aware standby loop on Linux.

        The BlueZ path is deliberately different from WinRT/CoreBluetooth: a band
        without a phone link advertises one isolated pulse every 40-60 s, so the
        only strategy that works is to keep a Device1.Connect armed across a
        full pulse period.  The old "scan -> connect 15 s -> re-scan" pattern
        misses every other pulse by construction (see
        docs/validation/linux-ble-connect-findings.md).  Injected test factories and
        non-Linux platforms keep the plain scan/retry behaviour.
        """
        if self._bluez_link_mode():
            await self._open_bluez_link(direct_device, scan_timeout, connect_timeout)
        else:
            await self._open_generic_link(direct_device, scan_timeout, connect_timeout)

    def _new_client(self, device: Any, timeout: float):
        """Build the bleak client through the injected factory (test seam)."""
        return self._client_factory(
            device,
            timeout=timeout,
            disconnected_callback=self._on_disconnected,
        )

    @staticmethod
    def _bluez_link_failure_hint(budget: float) -> str:
        """Actionable TimeoutError text for the BlueZ standby loop."""
        return (
            f"could not establish a BLE link within {budget:.0f}s. The band emits one "
            "advertising pulse every 40-60 s and none while its screen is off; "
            "sliding the screen does not change that. Check that public "
            "broadcasting is still enabled (open Zepp Life once, then switch phone "
            "Bluetooth off), keep the band near the adapter, and if not a single "
            "packet from any device was seen, reset the USB adapter "
            "(`sudo usbreset <vid:pid>` or unplug/replug) and restart bluetooth."
        )

    async def _connect_with_transient_retry(self, client_obj: Any, max_retries: int = 3) -> Any:
        """Retry a transient connect() failure in place, without re-scanning.

        Re-scanning inside the retry loop is what caused the "leapfrog" trap:
        every retry window was shorter than the band's 40-60 s pulse period, so
        the scanner swallowed each pulse and the controller was never in the
        initiating state when the next one arrived.
        """
        for attempt in range(max_retries + 1):
            try:
                await client_obj.connect()
                return client_obj
            except Exception as exc:
                if attempt >= max_retries or not bluez.is_transient_connect_error(exc):
                    raise
                logger.info(
                    "Transient connection attempt failed (%s); retrying in 0.4s "
                    "(attempt %d/%d)...",
                    bluez.describe_ble_error(exc),
                    attempt + 1,
                    max_retries,
                )
                await asyncio.sleep(0.4)
        return client_obj

    async def _open_generic_link(self, direct_device, scan_timeout: float, connect_timeout: float) -> None:
        """Scan-and-connect path for Windows/macOS and injected test factories."""
        found_device = direct_device
        if found_device is None:
            logger.info(
                "Waiting for Mi Band broadcast at %s (touch/light up band screen)...",
                self._describe_target(),
            )
            found_device = await self._discover_device(scan_timeout)
            logger.info("Band detected! Establishing BLE link (keep screen awake)...")

        # The direct attempt gets a shorter budget so a stale bonded object cannot
        # delay the fallback scan by the full connect timeout.
        timeout = (
            min(connect_timeout, DIRECT_CONNECT_TIMEOUT)
            if direct_device is not None
            else connect_timeout
        )
        self.client = self._new_client(found_device, timeout)
        try:
            await self._connect_with_transient_retry(self.client)
        except Exception as exc:
            if direct_device is None:
                raise
            logger.warning(
                "Direct connection to the bonded device failed (%s); falling back to a scan...",
                bluez.describe_ble_error(exc),
            )
            with contextlib.suppress(Exception):
                await self.client.disconnect()
            self.client = None
            found_device = await self._discover_device(scan_timeout)
            logger.info("Band detected! Establishing BLE link (keep screen awake)...")
            self.client = self._new_client(found_device, connect_timeout)
            await self._connect_with_transient_retry(self.client)

    async def _open_bluez_link(self, direct_device, scan_timeout: float, connect_timeout: float) -> None:
        """Keep one BlueZ Connect armed until the band answers its next pulse.

        Steps, all evidence-driven:

        1. Reuse a genuinely connected (Connected + ServicesResolved) device, or
           a bonded one, when BlueZ already has it.
        2. Mark the device Trusted *before* the attempt so BlueZ keeps the
           object, caches the GATT table and arms kernel auto-connect.
        3. Wait through bluez.wait_for_bluez_link, which never sends Disconnect
           and immediately re-arms after the kernel's 40 s abort, so the
           controller is initiating when the next pulse arrives.
        4. Hand the already-established link to bleak (bleak sees the device as
           connected and just collects the cached service table).
        """
        budget = bluez.link_standby_budget(connect_timeout)
        deadline = time.monotonic() + budget
        logger.info(
            "Linux/BlueZ link mode: keeping Device1.Connect armed for up to %.0fs "
            "(a sleeping band only answers its next 40-60 s pulse)...",
            budget,
        )
        device = direct_device
        # Pre-mark Trusted before scanning so pulse detection does not pay an
        # extra D-Bus roundtrip before arming Device1.Connect.
        await self._ensure_bluez_ready(direct_device)
        phases = ["direct", "scan"] if direct_device is not None else ["scan"]
        for phase in phases:
            if phase == "scan":
                remaining = deadline - time.monotonic()
                if remaining < 12.0:
                    break
                logger.info(
                    "Waiting for the band's next broadcast pulse at %s...",
                    self._describe_target(),
                )
                # Give discovery a full pulse period (capped by scan_timeout) even
                # after a direct probe; wait_for_bluez_link below gets its own
                # BAND_PULSE_PERIOD standby window once a pulse arrives.
                scan_budget = max(remaining, min(float(scan_timeout), bluez.BAND_PULSE_PERIOD))
                scan_deadline = time.monotonic() + scan_budget
                device = await self._discover_device(scan_timeout, deadline=scan_deadline)
            remaining = deadline - time.monotonic()
            if remaining < 3.0:
                break
            await self._ensure_bluez_ready(device)
            # A bonded-but-absent band must only spend a brief probe in direct
            # connect before falling back to scanning; once a scan catches a
            # pulse, allow up to one full BAND_PULSE_PERIOD in standby so a
            # late scan hit still completes link establishment.
            if phase == "direct":
                direct_cap = max(DIRECT_CONNECT_TIMEOUT, min(15.0, budget * 0.2))
                slice_timeout = min(remaining, direct_cap)
            else:
                slice_timeout = max(remaining, bluez.BAND_PULSE_PERIOD)
            details = getattr(device, "details", None)
            dev_path = details.get("path") if isinstance(details, dict) else None
            link_kwargs = {"device_path": dev_path} if dev_path else {}
            outcome = await bluez.wait_for_bluez_link(
                self.device_id, timeout=max(3.0, slice_timeout), **link_kwargs
            )
            if not outcome["ready"]:
                logger.warning(
                    "BlueZ could not link the band in that window (%s after %d "
                    "connect request(s)).",
                    outcome["reason"],
                    outcome["attempts"],
                )
                continue
            info = outcome["info"] or {}
            ble_device = bluez.make_ble_device(info) or device
            slice_budget = max(8.0, min(deadline - time.monotonic(), bluez.CONNECT_SLICE_TIMEOUT))
            client = self._new_client(ble_device, slice_budget)
            self.client = client
            try:
                await client.connect()
            except Exception as exc:
                logger.warning(
                    "Hand-off to bleak failed although BlueZ had the link up (%s); "
                    "re-entering standby...",
                    bluez.describe_ble_error(exc),
                )
                with contextlib.suppress(Exception):
                    await client.disconnect()
                self.client = None
                continue
            logger.info(
                "BLE link established via BlueZ (%d connect request(s), %s).",
                outcome["attempts"],
                outcome["reason"],
            )
            return
        raise TimeoutError(self._bluez_link_failure_hint(budget))

    def _on_disconnected(self, _client: object) -> None:
        """Watchdog callback: cancel keep-alive and signal the disconnect event."""
        if self._closing:
            return
        logger.warning("BLE link to %s lost (signal watchdog).", self.device_id)
        if self._keepalive_task is not None and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        self._disconnected.set()
