"""Main entry point behind miband_client.py.

The orchestration is moved verbatim from the former monolithic module: parse
arguments, configure logging, optionally run the doctor, validate the device
credentials, wire storage + optional live dashboard, then run the real or
simulated session and export CSV on shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from ..ble.identity import is_placeholder_device_id, normalize_auth_key
from ..config.demo import DEMO_AUTH_KEY_HEX, DEMO_DEVICE_ID
from ..config.defaults import (
    DEFAULT_MTU_MAX,
    DEFAULT_MTU_MIN,
    ENV_AUTH_KEY,
)
from ..config.env import env_str
from ..crypto.facade import backend_name as ecdh_backend_name
from ..dashboard import DashboardServer, TelemetryHub, make_db_history_provider
from ..history import resolve_band_tz
from ..storage import TelemetryStore
from ..storage.export_csv import export_database
from .args import demo_requested, parse_args
from .callbacks import build_callbacks
from .logging import configure_logging
from .runner import run_demo, run_monitor

logger = logging.getLogger("MiBand6Client")


async def main(argv: Optional[list[str]] = None) -> int:
    """Run the CLI; returns a process exit code (0 success, 1 failure)."""
    args = parse_args(argv)
    configure_logging(args.log_level)

    if args.doctor:
        # Imported lazily: doctor imports configuration helpers itself.
        from ..diagnostics.doctor import run_doctor

        return await run_doctor(
            (args.device_id or "").strip(),
            env_str(ENV_AUTH_KEY),
            args.output,
        )

    logger.info("ECDH backend: %s", ecdh_backend_name())

    device_id = (args.device_id or "").strip()
    device_name = (args.device_name or "").strip() or None
    key = env_str(ENV_AUTH_KEY)
    demo = demo_requested(args)

    if args.mtu is not None and not (DEFAULT_MTU_MIN <= args.mtu <= DEFAULT_MTU_MAX):
        logger.error(
            "Invalid --mtu %s: ATT MTU must be between %d and %d bytes. "
            "Leave it unset to auto-detect.",
            args.mtu,
            DEFAULT_MTU_MIN,
            DEFAULT_MTU_MAX,
        )
        return 1

    band_tz = (getattr(args, "band_tz", "") or "").strip() or None
    if band_tz is not None:
        try:
            resolve_band_tz(band_tz)
        except ValueError as exc:
            logger.error("Invalid --band-tz %r: %s", band_tz, exc)
            return 1

    if demo:
        if is_placeholder_device_id(device_id):
            device_id = DEMO_DEVICE_ID
        try:
            normalize_auth_key(key)
        except ValueError:
            logger.warning(
                "DEMO_MODE: no valid AUTH_KEY configured; using throwaway demo key."
            )
            key = DEMO_AUTH_KEY_HEX
    else:
        if is_placeholder_device_id(device_id) or not key or key.startswith("012345"):
            logger.error(
                "Please configure DEVICE_ID (Bluetooth address, or macOS UUID) "
                "and AUTH_KEY in .env before running (or run with --demo). "
                "Legacy DEVICE_MAC is still accepted."
            )
            return 1
        try:
            normalize_auth_key(key)
        except ValueError as exc:
            logger.error("Invalid AUTH_KEY: %s", exc)
            return 1

    logger.info(
        "Discovery target: %s%s",
        device_id,
        f" (name filter: {device_name!r})" if device_name else "",
    )

    store = TelemetryStore(args.output, backend=args.storage)

    hub: Optional[TelemetryHub] = None
    web_server: Optional[DashboardServer] = None
    if args.web_port is not None:
        hub = TelemetryHub()
        hub.attach_loop(asyncio.get_running_loop())
        web_server = DashboardServer(
            hub,
            host=args.web_host,
            port=args.web_port,
            history_provider=make_db_history_provider(store.db_path),
        )
        try:
            await web_server.start()
            logger.info("Live dashboard: %s", web_server.url)
        except OSError as exc:
            logger.error(
                "Could not start the dashboard on %s:%s: %s",
                args.web_host,
                args.web_port,
                exc,
            )
            web_server = None
            hub = None

    callbacks = build_callbacks(store, hub)
    if args.storage == "none":
        logger.info("Storage disabled; telemetry will only be logged.")
    else:
        logger.info(
            "Telemetry output: backend=%s -> %s",
            args.storage,
            Path(args.output).resolve(),
        )

    exit_code = 0
    try:
        if demo:
            await run_demo(
                device_id,
                key,
                duration=args.duration,
                imu=args.imu,
                callbacks=callbacks,
                sync_history_days=args.sync_history,
                sync_sleep_spo2=args.sync_sleep_spo2,
                band_tz=band_tz,
                mtu=args.mtu,
            )
        else:
            await run_monitor(
                device_id,
                key,
                duration=args.duration,
                imu=args.imu,
                callbacks=callbacks,
                scan_timeout=args.scan_timeout,
                connect_timeout=args.connect_timeout,
                reconnect=not args.no_reconnect,
                sync_history_days=args.sync_history,
                sync_sleep_spo2=args.sync_sleep_spo2,
                band_tz=band_tz,
                device_name=device_name,
                mtu=args.mtu,
                direct_connect=not args.no_direct_connect,
            )
    except KeyboardInterrupt:
        logger.info("Stopping on user request...")
    except Exception as exc:
        logger.error("Monitoring stopped after error: %s", exc)
        exit_code = 1
    finally:
        if web_server is not None:
            try:
                await web_server.stop()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Error while stopping the dashboard: %s", exc)
        store.close()
        logger.info("Storage summary: %s", store.stats())
        if args.export_csv:
            if args.storage in ("sqlite", "both"):
                try:
                    exported = export_database(store.db_path, Path(args.output) / "csv")
                    for table, (path, rows) in exported.items():
                        logger.info("CSV export: %s (%d rows) -> %s", table, rows, path)
                except Exception as exc:
                    logger.error("CSV export failed: %s", exc)
            else:
                logger.warning(
                    "--export-csv requires --storage sqlite or both (current: %s)",
                    args.storage,
                )
    return exit_code
