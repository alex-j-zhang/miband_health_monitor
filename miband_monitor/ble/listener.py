"""Notification-listener mixin: enable the chunked-transfer notify endpoint.

This mixin owns the one subscription the authenticated session cannot work
without: characteristic 0x0017 (chunked transfer notifications). Stream
subscriptions (heart rate, steps, raw sensor) live in streams.py; this is the
handshake-critical listener that must be armed before the first chunked write.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .. import bluez
from .protocol import UUID_CHAR_CHUNKED_READ

logger = logging.getLogger("MiBand6Client")


class ListenerMixin:
    """Subscribe to the chunked-transfer notification characteristic."""

    async def _start_chunked_listener(self) -> None:
        """Enable the chunked-transfer notification with a bounded, retried attempt.

        start_notify used to be a bare await: when BlueZ's built-in HID
        (hog-lib) / device-info profile probes still owned the ATT queue, the
        request blocked for ~57 s and was only unblocked by the band dropping the
        link.  A bounded attempt turns that into a fast, actionable failure.
        """
        if self.client is None:
            raise RuntimeError("Client is not connected")
        attempts = 2
        per_try = 6.0
        last_exc: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            try:
                await asyncio.wait_for(
                    self.client.start_notify(
                        UUID_CHAR_CHUNKED_READ, self._on_chunked_notification
                    ),
                    timeout=per_try,
                )
                return
            except asyncio.TimeoutError as exc:
                last_exc = exc
            except Exception as exc:
                if not bluez.is_transient_connect_error(exc):
                    raise
                last_exc = exc
            logger.warning(
                "start_notify(0x0017) did not complete within %.0fs (attempt %d/%d); "
                "BlueZ profile/HID probes may still own the ATT queue - retrying...",
                per_try,
                attempt,
                attempts,
            )
            await asyncio.sleep(0.5)
        raise TimeoutError(
            "could not enable the chunked-transfer notification (0x0017): the ATT "
            f"queue stayed busy for {attempts * (per_try + 0.5):.0f}s. This usually "
            "means BlueZ is still running its built-in HID/device-info profile "
            "probes; keep MIBAND_BLUEZ_TRUST=1 and retry, or disable the input "
            "plugin (`bluetoothd --noplugin=input`) for a diagnostic run."
        ) from last_exc
