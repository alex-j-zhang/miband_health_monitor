"""Connect-and-authenticate mixin: discover, link, subscribe and start ECDH.

This is the public entry point of the live session (MiBand6Client.
connect_and_authenticate). It wires the per-backend link strategies from the
other mixins together, optionally starts the BlueZ pairing agent, sends the
client's NIST B-163 public key as the first chunked-auth payload, then waits for
the band's challenge/acknowledgement while watching the disconnect signal so a
dropped link fails fast instead of waiting out the whole auth timeout.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .. import bluez
from ..config.defaults import ENV_BLUEZ_AGENT
from ..config.env import env_str
from ..crypto.facade import generate_ecdh_keys
from .protocol import CHUNKED_ENDPOINT_AUTH, UUID_CHAR_CHUNKED_WRITE

logger = logging.getLogger("MiBand6Client")


async def _write_chunked_payload(*args, **kwargs) -> None:
    """Send a chunked payload through the public client module binding.

    The chunked writer is resolved through miband_monitor.ble.client at call
    time instead of being imported here. This preserves the historical test
    seam: before the refactor a single module owned both MiBand6Client and
    write_chunked_value, so tests monkeypatched one attribute; the lazy lookup
    keeps monkeypatching miband_monitor.ble.client.write_chunked_value
    observable for every internal call (client public key and step-2 response)
    without creating a circular import between the mixin modules and client.py.
    """
    from . import client as client_module

    await client_module.write_chunked_value(*args, **kwargs)


class AuthConnectMixin:
    """Discover, connect and execute the chunked-transfer ECDH handshake."""

    async def connect_and_authenticate(
        self,
        scan_timeout: Optional[float] = None,
        connect_timeout: Optional[float] = None,
        auth_timeout: Optional[float] = None,
    ) -> None:
        """Discover, connect and execute the chunked-transfer ECDH handshake."""
        scan_timeout = self.scan_timeout if scan_timeout is None else scan_timeout
        connect_timeout = self.connect_timeout if connect_timeout is None else connect_timeout
        auth_timeout = self.auth_timeout if auth_timeout is None else auth_timeout

        self._reset_auth_state()
        self._disconnected.clear()
        self._prv_key, self._pub_key = generate_ecdh_keys()

        if (
            bluez.is_bluez_platform()
            and self._client_factory is self._bleak_client_class
            and env_str(ENV_BLUEZ_AGENT, "").lower() in ("1", "true", "yes")
        ):
            if self._pairing_agent is None:
                agent = bluez.BlueZPairingAgent(self.device_id)
                if await agent.start():
                    self._pairing_agent = agent
                else:
                    logger.debug("BlueZ pairing agent failed to start; continuing without it.")

        direct_device = await self._direct_connect_target()
        await self._open_client(direct_device, scan_timeout, connect_timeout)
        if self._pairing_agent is not None:
            target_mac = getattr(self.client, "address", None) or self.device_id
            if bluez.looks_like_mac(target_mac):
                self._pairing_agent.target_address = bluez.normalize_address(target_mac)
            await self._pairing_agent.ensure_ready_for_device(target_mac)
        await self._settle_after_link()
        await self._apply_mtu()
        await self._start_chunked_listener()

        initial_auth = b"\x04\x02\x00\x02" + self._pub_key
        logger.info("Initiating chunked ECDH handshake (sending client public key)...")
        await _write_chunked_payload(
            self.client,
            UUID_CHAR_CHUNKED_WRITE,
            CHUNKED_ENDPOINT_AUTH,
            0,
            initial_auth,
            mtu=self._session_mtu,
            lock=self._gatt_lock,
        )

        # A link drop during the handshake must not leave the session waiting
        # for the full auth timeout: watch the disconnect signal alongside the
        # auth result so the supervisor can reconnect promptly.  This matters
        # most with the slower pure-Python ECDH backend.
        auth_wait = asyncio.create_task(self._auth_event.wait())
        link_wait = asyncio.create_task(self._disconnected.wait())
        try:
            await asyncio.wait(
                {auth_wait, link_wait},
                timeout=auth_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (auth_wait, link_wait):
                if not task.done():
                    task.cancel()
            await asyncio.gather(auth_wait, link_wait, return_exceptions=True)

        if self._auth_error:
            raise RuntimeError(f"Authentication failed: {self._auth_error}")
        if not self._auth_event.is_set():
            if self._disconnected.is_set():
                raise ConnectionError(
                    "BLE link lost during chunked ECDH authentication"
                )
            raise TimeoutError(
                f"Chunked ECDH authentication timed out after {auth_timeout:.0f}s"
            )

        self.authenticated = True
        logger.info("Chunked ECDH authentication SUCCEEDED. Session is ready.")
