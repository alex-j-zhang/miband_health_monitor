"""Session supervisor: connect, stream, reconnect and demo mode.

The logic is moved verbatim from miband_client.py. It intentionally stays in the
CLI package because it wires a user-facing session (duration, reconnects, IMU,
history sync) rather than implementing BLE protocol details.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from ..ble.client import MiBand6Client, StreamCallbacks
from ..ble.identity import normalize_auth_key
from ..bluez.errors import describe_ble_error
from ..config.defaults import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_RECONNECT_FAST_DELAY,
    DEFAULT_RECONNECT_INITIAL_DELAY,
    DEFAULT_RECONNECT_MAX_DELAY,
    DEFAULT_SCAN_TIMEOUT,
)
from ..history.sync import summarize_history_result, sync_history

logger = logging.getLogger("MiBand6Client")


async def _sync_band_history(
    client: MiBand6Client,
    days: int,
    callbacks: StreamCallbacks,
    *,
    include_spo2_sleep: bool = False,
    band_tz: Optional[str] = None,
) -> None:
    """Fetch band-side history once; failures never abort live streaming."""
    logger.info("Fetching up to %d day(s) of band-side history (experimental)...", days)
    if include_spo2_sleep:
        logger.warning(
            "Sleep SpO2 (0x26) capture enabled: 30B layout verified on Mi Band 6, "
            "but trailing field semantics are unknown; samples go to the "
            "dedicated spo2_sleep table + JSONL, never the point spo2 table."
        )
    try:
        result = await sync_history(
            client,
            days=days,
            on_spo2=callbacks.history_spo2,
            on_activity=callbacks.history_activity,
            include_spo2_sleep=include_spo2_sleep,
            on_spo2_sleep=callbacks.history_spo2_sleep,
            band_tz=band_tz,
        )
    except Exception as exc:
        logger.error("Band history sync failed (continuing with live streams): %s", exc)
        return
    logger.info("Band history sync finished: %s", summarize_history_result(result))
    for error in result.errors:
        logger.warning("History sync issue: %s", error)


async def run_monitor(
    device_id: str,
    auth_key_hex: str,
    *,
    duration: Optional[int] = None,
    imu: bool = False,
    callbacks: Optional[StreamCallbacks] = None,
    scan_timeout: float = DEFAULT_SCAN_TIMEOUT,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    reconnect: bool = True,
    reconnect_initial_delay: float = DEFAULT_RECONNECT_INITIAL_DELAY,
    reconnect_max_delay: float = DEFAULT_RECONNECT_MAX_DELAY,
    sync_history_days: Optional[int] = None,
    sync_sleep_spo2: bool = False,
    band_tz: Optional[str] = None,
    device_name: Optional[str] = None,
    mtu: Optional[int] = None,
    direct_connect: bool = True,
    client_factory: Optional[Callable[[str, str], MiBand6Client]] = None,
) -> None:
    """Connect + stream until duration elapses, reconnecting with backoff."""
    callbacks = callbacks or StreamCallbacks()
    make_client = client_factory or (
        lambda device, key: MiBand6Client(
            device,
            key,
            device_name=device_name,
            mtu=mtu,
            scan_timeout=scan_timeout,
            connect_timeout=connect_timeout,
            direct_connect=direct_connect,
        )
    )
    pre_auth_deadline = None if duration is None else time.monotonic() + duration
    collection_deadline: Optional[float] = None
    attempt = 0
    consecutive_failures = 0
    succeeded_sessions = 0
    last_failure: Optional[BaseException] = None
    history_synced = False

    while True:
        active_deadline = (
            collection_deadline if collection_deadline is not None else pre_auth_deadline
        )
        remaining = None if active_deadline is None else active_deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            if collection_deadline is not None:
                logger.info("Collection window (%ss) elapsed.", duration)
                return
            if succeeded_sessions == 0 and last_failure is not None:
                raise last_failure
            return

        attempt += 1
        client = make_client(device_id, auth_key_hex)
        failure: Optional[BaseException] = None
        was_authenticated = False
        try:
            await client.connect_and_authenticate(scan_timeout=scan_timeout)
            was_authenticated = True
            succeeded_sessions += 1
            consecutive_failures = 0
            if duration is not None and collection_deadline is None:
                # Count --duration as collection time after first authentication
                # rather than letting BLE scan/connect consume the window.
                collection_deadline = time.monotonic() + duration

            if sync_history_days is not None and not history_synced:
                await _sync_band_history(
                    client,
                    sync_history_days,
                    callbacks,
                    include_spo2_sleep=sync_sleep_spo2,
                    band_tz=band_tz,
                )
                history_synced = True
            # Order matters on real hardware: the raw sensor hub stream must be
            # activated before the heart-rate stream. Starting it afterwards
            # silently starves the standard BLE heart-rate characteristic
            # (measured on a Mi Band 6: 0 HR notifications in 20 s vs ~1/s).
            if imu:
                await client.start_raw_sensor_stream(callbacks.imu)
            await client.start_heart_rate_stream(callbacks.heart_rate)
            await client.start_steps_stream(callbacks.steps)
            logger.info(
                "Streaming session %d started (duration=%s, imu=%s).",
                attempt,
                f"{duration}s" if duration is not None else "until Ctrl+C",
                imu,
            )

            remaining = (
                None
                if collection_deadline is None
                else collection_deadline - time.monotonic()
            )
            if remaining is None:
                await client.wait_until_disconnected()
                failure = RuntimeError("BLE link lost")
            elif remaining <= 0:
                logger.info("Collection window (%ss) elapsed.", duration)
                return
            else:
                try:
                    await asyncio.wait_for(
                        client.wait_until_disconnected(), timeout=remaining
                    )
                    failure = RuntimeError("BLE link lost")
                except asyncio.TimeoutError:
                    logger.info("Collection window (%ss) elapsed.", duration)
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = exc
            last_failure = exc
            logger.error("Session %d failed: %s", attempt, describe_ble_error(exc))
        finally:
            await client.disconnect()

        if not reconnect:
            if failure is not None and not was_authenticated:
                raise failure
            return

        if was_authenticated and failure is not None:
            # When an established session drops, the band advertises aggressively
            # for a few seconds. Fast-reconnect immediately to catch that window
            # without waking the screen.
            delay = min(DEFAULT_RECONNECT_FAST_DELAY, reconnect_initial_delay)
        else:
            consecutive_failures += 1
            delay = min(
                reconnect_max_delay,
                reconnect_initial_delay * (2 ** min(consecutive_failures - 1, 5)),
            )
        active_deadline = (
            collection_deadline if collection_deadline is not None else pre_auth_deadline
        )
        remaining = None if active_deadline is None else active_deadline - time.monotonic()
        if remaining is not None:
            if remaining <= 0:
                if succeeded_sessions == 0 and last_failure is not None:
                    raise last_failure
                return
            delay = min(delay, remaining)
        logger.info("Reconnecting in %.2fs (next attempt %d)...", delay, attempt + 1)
        if delay > 0:
            await asyncio.sleep(delay)


async def run_demo(
    device_id: str,
    auth_key_hex: str,
    *,
    duration: Optional[int] = None,
    imu: bool = False,
    callbacks: Optional[StreamCallbacks] = None,
    sync_history_days: Optional[int] = None,
    sync_sleep_spo2: bool = False,
    band_tz: Optional[str] = None,
    mtu: Optional[int] = None,
) -> None:
    """Run the full client stack against the in-memory simulator (no hardware)."""
    from ..simulator import FakeBleakScanner, SimulatedBand, simulated_factories

    band = SimulatedBand(auth_key=normalize_auth_key(auth_key_hex))
    sim_client_factory, _scanner_factory = simulated_factories(band)

    class _DemoScanner(FakeBleakScanner):
        default_address = device_id

    def _factory(mac: str, key: str) -> MiBand6Client:
        return MiBand6Client(
            mac,
            key,
            mtu=mtu,
            client_factory=sim_client_factory,
            scanner_factory=_DemoScanner,
        )

    logger.info("DEMO MODE: using an in-memory simulated Mi Band 6 (no Bluetooth needed).")
    await run_monitor(
        device_id,
        auth_key_hex,
        duration=duration,
        imu=imu,
        callbacks=callbacks,
        scan_timeout=10.0,
        reconnect=True,
        reconnect_initial_delay=0.2,
        reconnect_max_delay=1.0,
        sync_history_days=sync_history_days,
        sync_sleep_spo2=sync_sleep_spo2,
        band_tz=band_tz,
        mtu=mtu,
        client_factory=_factory,
    )
