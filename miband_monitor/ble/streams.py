"""Stream mixin: GATT writes plus start/stop of the live telemetry streams.

Order matters on real hardware (the supervisor in miband_monitor.cli.runner
enforces it): the raw sensor hub stream must be activated *before* the heart-rate
stream, otherwise the standard BLE heart-rate characteristic is silently starved
(measured on a Mi Band 6: 0 HR notifications in 20 s vs ~1/s).

disconnect() is idempotent and stops every active stream, the keep-alive loop,
the optional BlueZ pairing agent and the underlying bleak client. The link
watchdog callback (_on_disconnected) lives in link.py because it belongs to the
connection lifecycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Callable, Optional

from .. import bluez
from .chunked import _write_with_in_progress_retry
from .protocol import (
    CMD_RAW_SENSOR_START_1,
    CMD_RAW_SENSOR_START_2,
    CMD_RAW_SENSOR_START_3,
    CMD_RAW_SENSOR_STOP,
    UUID_CHAR_HR_CONTROL,
    UUID_CHAR_HR_MEASUREMENT,
    UUID_CHAR_RAW_SENSOR_CONTROL,
    UUID_CHAR_RAW_SENSOR_DATA,
    UUID_CHAR_STEPS,
)

logger = logging.getLogger("MiBand6Client")


class StreamMixin:
    """Live heart-rate, steps and raw-IMU subscription management."""

    async def _write_gatt(self, char_uuid: str, data: bytes, response: bool = False) -> None:
        """Write to a GATT characteristic serialized by _gatt_lock with InProgress retry."""
        if self.client is None:
            raise RuntimeError("Client is not connected")
        async with self._gatt_lock:
            await _write_with_in_progress_retry(self.client, char_uuid, data, response=response)

    # --------------------------------------------------------------- heart rate
    async def start_heart_rate_stream(self, callback: Optional[Callable[[int], None]] = None) -> None:
        """Subscribe to real-time heart rate notifications and start keep-alive."""
        if self.client is None:
            raise RuntimeError("Client is not connected")
        self._hr_callback = callback
        logger.info("Initializing heart rate sensor...")

        # Reset measurement state, subscribe, then start continuous measurement.
        await self._write_gatt(UUID_CHAR_HR_CONTROL, b"\x15\x02\x00", response=False)
        await self._write_gatt(UUID_CHAR_HR_CONTROL, b"\x15\x01\x00", response=False)
        await self.client.start_notify(UUID_CHAR_HR_MEASUREMENT, self._on_heart_rate_notification)
        await self._write_gatt(UUID_CHAR_HR_CONTROL, b"\x15\x01\x01", response=False)

        self._hr_active = True
        self._keepalive_task = asyncio.create_task(self._hr_keepalive_loop())
        logger.info("Continuous heart rate monitoring active!")

    async def _hr_keepalive_loop(self) -> None:
        """Ping the heart-rate control point every 12 s; abort after 2 failures."""
        consecutive_failures = 0
        try:
            while True:
                await asyncio.sleep(12)
                if self.client is None:
                    return
                logger.debug("Pinging heart rate monitor keep-alive [0x16]...")
                try:
                    await self._write_gatt(UUID_CHAR_HR_CONTROL, b"\x16", response=False)
                    consecutive_failures = 0
                except Exception as exc:
                    consecutive_failures += 1
                    is_fatal = (
                        self.client is None
                        or not getattr(self.client, "is_connected", False)
                        or "UnknownObject" in str(exc)
                        or getattr(exc, "dbus_error_name", "") == "org.freedesktop.DBus.Error.UnknownObject"
                    )
                    if is_fatal or consecutive_failures >= 2:
                        logger.error(
                            "Heart-rate keep-alive failed (%d consecutive failures); treating link as lost: %s",
                            consecutive_failures,
                            bluez.describe_ble_error(exc),
                        )
                        self._disconnected.set()
                        return
                    logger.warning(
                        "Heart-rate keep-alive warning (attempt %d/2, retrying next cycle): %s",
                        consecutive_failures,
                        bluez.describe_ble_error(exc),
                    )
        except asyncio.CancelledError:
            raise

    async def stop_heart_rate_stream(self) -> None:
        """Stop the heart-rate stream and cancel the keep-alive loop."""
        if self._keepalive_task is not None and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._keepalive_task
        if self._hr_active and self.client is not None:
            try:
                await self.client.stop_notify(UUID_CHAR_HR_MEASUREMENT)
            except Exception as exc:
                logger.debug("Error while stopping heart-rate notify: %s", exc)
        self._hr_active = False

    # ------------------------------------------------------------------- steps
    async def start_steps_stream(self, callback: Optional[Callable[[dict], None]] = None) -> None:
        """Subscribe to real-time steps and calories notifications."""
        if self.client is None:
            raise RuntimeError("Client is not connected")
        self._steps_callback = callback
        logger.info("Enabling real-time steps stream...")
        try:
            initial_data = await self.client.read_gatt_char(UUID_CHAR_STEPS)
            if initial_data:
                self._on_steps_notification(None, bytearray(initial_data))
        except Exception as exc:
            logger.debug("Failed initial steps read: %s", exc)

        await self.client.start_notify(UUID_CHAR_STEPS, self._on_steps_notification)
        self._steps_active = True
        logger.info("Real-time steps notification active!")

    async def stop_steps_stream(self) -> None:
        """Stop the real-time steps notification subscription."""
        if self._steps_active and self.client is not None:
            try:
                await self.client.stop_notify(UUID_CHAR_STEPS)
            except Exception as exc:
                logger.debug("Error while stopping steps notify: %s", exc)
        self._steps_active = False

    # ------------------------------------------------------------- raw sensor
    async def start_raw_sensor_stream(
        self, callback: Optional[Callable[[dict], None]] = None
    ) -> None:
        """Activate raw IMU accelerometer streaming at ~25 Hz."""
        if self.client is None:
            raise RuntimeError("Client is not connected")
        self._sensor_callback = callback
        logger.info("Initializing raw IMU sensor stream...")
        await self._write_gatt(
            UUID_CHAR_RAW_SENSOR_CONTROL, CMD_RAW_SENSOR_START_1, response=False
        )
        await self._write_gatt(
            UUID_CHAR_RAW_SENSOR_CONTROL, CMD_RAW_SENSOR_START_2, response=False
        )
        await self._write_gatt(
            UUID_CHAR_RAW_SENSOR_CONTROL, CMD_RAW_SENSOR_START_3, response=False
        )
        await self.client.start_notify(UUID_CHAR_RAW_SENSOR_DATA, self._on_sensor_notification)
        self._sensor_active = True
        logger.info("Raw IMU sensor streaming active!")

    async def stop_raw_sensor_stream(self) -> None:
        """Stop the raw IMU stream and its notification subscription."""
        if self._sensor_active and self.client is not None:
            try:
                await self._write_gatt(
                    UUID_CHAR_RAW_SENSOR_CONTROL, CMD_RAW_SENSOR_STOP, response=False
                )
                await self.client.stop_notify(UUID_CHAR_RAW_SENSOR_DATA)
            except Exception as exc:
                logger.debug("Error while stopping raw sensor stream: %s", exc)
        self._sensor_active = False

    # ------------------------------------------------------------- teardown
    async def disconnect(self) -> None:
        """Disconnect cleanly and stop every active stream (idempotent)."""
        self._closing = True
        try:
            await self.stop_heart_rate_stream()
            await self.stop_steps_stream()
            await self.stop_raw_sensor_stream()
            if self._pairing_agent is not None:
                with contextlib.suppress(Exception):
                    await self._pairing_agent.stop()
                self._pairing_agent = None
            client = self.client
            self.client = None
            if client is not None and getattr(client, "is_connected", False):
                try:
                    await client.disconnect()
                    logger.info("Disconnected from Mi Band.")
                except Exception as exc:
                    logger.debug("Error while disconnecting: %s", exc)
        finally:
            self._closing = False
            self._disconnected.set()
