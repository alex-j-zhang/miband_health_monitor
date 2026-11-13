"""Environment self-check behind miband_client.py --doctor.

The check is read-only: it inspects configuration, the ECDH backend, the output
directory and (on Linux) the BlueZ adapter/device state.  It never connects to
the band and never changes system settings; when something is missing it prints
the exact command that fixes it.

Exit code: 0 when no check failed, 1 otherwise.

Module-level BlueZ imports are deliberate: tests and integrations patch
miband_monitor.diagnostics.doctor.is_bluez_platform / list_adapters /
find_device exactly like they used to patch the old doctor module.
"""

from __future__ import annotations

import asyncio
import os
import platform
import sys
from pathlib import Path
from typing import Callable, Optional

from miband_monitor.bluez import find_device, is_bluez_platform, list_adapters
from miband_monitor.config.defaults import (
    DEFAULT_DATA_DIR,
    ENV_AUTH_KEY,
    ENV_OUTPUT_DIR,
)
from miband_monitor.crypto import backend_name as ecdh_backend_name
from miband_monitor.crypto import native_library_candidates

from .checks import _bleak_version, _env_mode_hint, _mask
from .report import FAIL, INFO, OK, WARN, _Report


async def run_doctor(
    device_id: str,
    auth_key: str,
    output_dir: str,
    *,
    emit: Optional[Callable[[str], None]] = None,
) -> int:
    """Run every check and return a process exit code."""
    is_placeholder_device_id, normalize_auth_key = _auth_helpers()

    report = _Report(emit)
    report.add(INFO, "platform", f"{sys.platform} {platform.release()} ({platform.machine()})")
    report.add(INFO, "python", platform.python_version())
    report.add(INFO, "bleak", _bleak_version())

    # --- ECDH backend -----------------------------------------------------
    backend = ecdh_backend_name()
    available = sorted({p.name for p in native_library_candidates() if p.exists()})
    report.add(
        OK,
        "ECDH backend",
        f"{backend}" + (f" (native candidates: {', '.join(available)})" if available else ""),
        hint=None
        if backend == "native"
        else "pure-Python fallback is fine; install gcc to enable the native backend",
    )

    # --- configuration ----------------------------------------------------
    device_id = (device_id or "").strip()
    if not device_id or is_placeholder_device_id(device_id):
        report.add(
            FAIL,
            "config",
            "DEVICE_ID / DEVICE_MAC is not configured",
            hint="cp .env.example .env  then set DEVICE_ID (MAC or macOS UUID) and AUTH_KEY",
        )
    else:
        report.add(OK, "config", f"DEVICE_ID set ({_mask(device_id)})")

    key = (auth_key or "").strip()
    if not key:
        report.add(FAIL, "auth key", "AUTH_KEY is not set", hint="see get_auth_key.py / fetch_by_token.py")
    else:
        try:
            normalize_auth_key(key)
            report.add(OK, "auth key", "AUTH_KEY looks valid (32 hex characters)")
        except ValueError as exc:
            report.add(FAIL, "auth key", str(exc))

    chmod_hint = _env_mode_hint()
    if chmod_hint:
        report.add(
            WARN,
            ".env",
            "readable by other local users (recommended mode 0600)",
            hint=chmod_hint,
        )

    # --- output directory -------------------------------------------------
    from miband_monitor.config.security import secure_directory

    path = Path(output_dir or DEFAULT_DATA_DIR).expanduser()
    try:
        secure_directory(path)
        probe = path / ".doctor-write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        report.add(OK, "output dir", f"{path} (writable)")
    except OSError as exc:
        report.add(FAIL, "output dir", f"{path} is not writable: {exc}")

    # --- Linux / BlueZ ----------------------------------------------------
    if is_bluez_platform():
        await _check_bluez(report, device_id)
    else:
        report.add(INFO, "bluetooth", "BlueZ checks skipped (not Linux)")

    report.flush()
    print("")
    print(f"Summary: {report.summary()}")
    return 1 if report.counts[FAIL] else 0


def _auth_helpers():
    """Return (is_placeholder_device_id, normalize_auth_key) from the BLE package."""
    from miband_monitor.ble import is_placeholder_device_id, normalize_auth_key

    return is_placeholder_device_id, normalize_auth_key


async def _check_bluez(report: _Report, device_id: str) -> None:
    adapters = await list_adapters()
    if not adapters:
        report.add(
            FAIL,
            "adapter",
            "no Bluetooth adapter found",
            hint="bluetoothctl list  # WSL2/containers do not expose BLE to the host",
        )
        return

    adapter = adapters[0]
    report.add(
        OK if adapter["powered"] else FAIL,
        f"adapter {adapter['name']}",
        f"powered={'yes' if adapter['powered'] else 'no'} "
        f"pairable={'yes' if adapter['pairable'] else 'no'} "
        f"discovering={'yes' if adapter['discovering'] else 'no'}",
        hint=None if adapter["powered"] else "bluetoothctl power on",
    )
    if adapter["powered"] and not adapter["pairable"]:
        report.add(
            INFO,
            "pairable",
            "adapter is not pairable; only matters for SM bonding, not for "
            "Trusted + auto-connect",
            hint="bluetoothctl pairable on  # optional, only needed for a one-time Bond",
        )
    report.add(
        INFO,
        "band advertising",
        "a Mi Band 6 without a phone link emits ONE pulse every 40-60 s and none "
        "while its screen is off; sliding the screen does not make it advertise",
        hint="use --scan-timeout 120 (default 90); one pulse period is 40-60 s",
    )

    if not device_id:
        return

    info = await find_device(device_id)
    if info is None:
        report.add(
            WARN,
            "device",
            "not known to BlueZ yet (never discovered on this host)",
            hint=(
                "Open Zepp Life once so the band re-enables public broadcasting, "
                "then switch phone Bluetooth off and run: python3 scan_ble.py\n"
                "First link only needs `trust` (see docs/guides/cross-platform.md §2.3)"
            ),
        )
        return

    mac = info["address"] or device_id
    is_paired = bool(info.get("paired")) or bool(info.get("bonded"))
    is_trusted = bool(info.get("trusted"))
    is_connected = bool(info.get("connected"))

    report.add(
        OK if is_paired else WARN,
        "device",
        f"{_mask(mac)} paired={'yes' if is_paired else 'no'} "
        f"trusted={'yes' if is_trusted else 'no'} "
        f"connected={'yes' if is_connected else 'no'}",
    )
    if not is_paired:
        report.add(
            WARN,
            "bonding",
            f"{_mask(mac)} is NOT bonded; the band drops an unauthenticated link "
            "after ~9 s of GATT enumeration, so the first connection can be lost "
            "until BlueZ has cached the service table",
            hint=(
                "Trust is what matters for auto-reconnect:\n"
                "bluetoothctl trust <MAC>\n"
                "(optional) MIBAND_BLUEZ_AGENT=1 python3 miband_client.py 15 "
                "to run the in-process SMP agent;\n"
                "a bare `bluetoothctl pair <MAC>` is often rejected with "
                "AuthenticationCanceled while the band is still bonded to a phone"
            ),
        )
    elif not is_trusted:
        report.add(
            INFO,
            "trust",
            "not trusted: BlueZ will not auto-reconnect on its own",
            hint="bluetoothctl trust <MAC>",
        )

    if is_paired:
        report.add(
            OK,
            "direct connect",
            "available (bonded device in the BlueZ object tree, cached GATT table)",
        )
    else:
        report.add(
            INFO,
            "direct connect",
            "bypassed (unbonded device needs a scan or an armed Device1.Connect)",
        )


if __name__ == "__main__":
    from miband_monitor.ble import _env_device_id
    from miband_monitor.config.env import load_env

    load_env()

    _dev_id = _env_device_id()
    _auth = (os.getenv(ENV_AUTH_KEY) or "").strip()
    _out = (os.getenv(ENV_OUTPUT_DIR) or DEFAULT_DATA_DIR).strip()
    sys.exit(asyncio.run(run_doctor(_dev_id, _auth, _out)))
