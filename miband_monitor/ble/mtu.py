"""MTU mixin: resolve the ATT MTU once and let BlueZ SMP/profile probes settle.

BlueZ reports 23 bytes (and warns) until the backend is asked for the real ATT
MTU, while WinRT/CoreBluetooth fill it in during connect. The client resolves it
once per connection, before the first chunked write, so framing is sized exactly
once for the whole session.

The settle delay exists because the first chunked write issued right after
ServicesResolved can land inside BlueZ's new_auth() window ("No agent available
for request type 2") and then stall behind the single ATT queue for ~57 s; see
docs/validation/linux-ble-connect-findings.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from .. import bluez
from ..config.defaults import ENV_SMP_SETTLE_DELAY
from ..config.env import env_str
from .chunked import _resolve_mtu

logger = logging.getLogger("MiBand6Client")


class MtuMixin:
    """ATT MTU negotiation and post-link settle handling."""

    async def _apply_mtu(self) -> None:
        """Resolve the ATT MTU once, asking BlueZ for it explicitly first."""
        # BlueZ reports 23 bytes (and warns) until the backend is asked for the
        # real ATT MTU; WinRT and CoreBluetooth fill it in during connect.  Do
        # this before the first chunked write so the framing is sized once.
        acquired = False
        if self.mtu is None:
            acquired = await bluez.try_acquire_mtu(self.client)
        self._session_mtu = _resolve_mtu(self.client, self.mtu)
        logger.info(
            "BLE link established. Initializing chunked transfer listener (MTU=%d)...",
            self._session_mtu,
        )
        if acquired:
            logger.info("Negotiated ATT MTU via BlueZ: %d bytes.", self._session_mtu)

    async def _settle_after_link(self) -> None:
        """Give BlueZ time to finish SMP (new_auth) and its profile probes.

        Measured on 2026-11-12: the first chunked write issued 1 ms after
        ServicesResolved landed inside the new_auth() window and then sat behind
        the single ATT queue for 56.8 s until the timeout killed the link.
        Writing only after BlueZ has settled avoids that stall entirely.
        """
        if not self._bluez_link_mode():
            return
        delay = bluez.SMP_SETTLE_DELAY
        override = env_str(ENV_SMP_SETTLE_DELAY, "")
        if override:
            with contextlib.suppress(ValueError):
                delay = max(0.0, float(override))
        if delay > 0:
            logger.debug("Waiting %.1fs for BlueZ SMP/profile probes to settle...", delay)
            await asyncio.sleep(delay)
