"""Environment self-check behind ``miband_client.py --doctor``.

The check is read-only: it inspects configuration, the ECDH backend, the
output directory and (on Linux) the BlueZ adapter/device state.  It never
connects to the band and never changes system settings; when something is
missing it prints the exact command that fixes it.

Exit code: 0 when no check failed, 1 otherwise.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Callable, Optional

from bluez_support import is_bluez_platform, list_adapters, find_device
from ecdh_wrapper import backend_name as ecdh_backend_name
from ecdh_wrapper import native_library_candidates

try:  # Python 3.8+
    from importlib.metadata import PackageNotFoundError, version as _pkg_version
except Exception:  # pragma: no cover - very old Python
    _pkg_version = None  # type: ignore[assignment]

    class PackageNotFoundError(Exception):  # type: ignore[no-redef]
        pass


OK = "OK"
INFO = "INFO"
WARN = "WARN"
FAIL = "FAIL"

_PREFIX = {OK: "[OK]  ", INFO: "[INFO]", WARN: "[WARN]", FAIL: "[FAIL]"}
_MARK = {OK: "ok", INFO: "info", WARN: "warning", FAIL: "error"}


class _Report:
    """Collect and print check results."""

    def __init__(self, emit: Optional[Callable[[str], None]] = None) -> None:
        self._lines: list[str] = []
        self._emit = emit or print
        self.counts = {OK: 0, INFO: 0, WARN: 0, FAIL: 0}

    def add(self, status: str, label: str, detail: str, hint: Optional[str] = None) -> None:
        self.counts[status] = self.counts.get(status, 0) + 1
        line = f"{_PREFIX[status]} {label:<14} {detail}"
        self._lines.append(line)
        if hint:
            for part in hint.splitlines():
                self._lines.append(f"       {'':<14} {part}")

    def flush(self) -> None:
        for line in self._lines:
            self._emit(line)

    def summary(self) -> str:
        return (
            f"{self.counts[FAIL]} error(s), {self.counts[WARN]} warning(s), "
            f"{self.counts[OK]} check(s) passed"
        )


def _bleak_version() -> str:
    if _pkg_version is None:
        return "unknown"
    try:
        return _pkg_version("bleak")
    except PackageNotFoundError:
        return "not installed"
    except Exception:  # pragma: no cover - defensive
        return "unknown"


def _env_mode_hint() -> Optional[str]:
    """Warn when ``.env`` is readable by other local users."""
    if os.name == "nt":
        return None
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return None
    try:
        mode = env_path.stat().st_mode & 0o777
    except OSError:
        return None
    if mode & 0o077:
        return f"chmod 600 {env_path.name}"
    return None


async def run_doctor(
    device_id: str,
    auth_key: str,
    output_dir: str,
    *,
    emit: Optional[Callable[[str], None]] = None,
) -> int:
    """Run every check and return a process exit code."""
    from miband_client import is_placeholder_device_id, normalize_auth_key

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
    path = Path(output_dir or "data").expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
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


def _mask(identifier: str) -> str:
    """Show only the last two octets of a MAC / last 4 chars of a UUID."""
    text = identifier.strip()
    if ":" in text and len(text) == 17:
        return "XX:XX:XX:XX:" + text[-5:]
    return text[-4:].rjust(len(text), "*") if text else ""


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
            WARN,
            "pairable",
            "adapter is not pairable; first-time pairing will fail",
            hint="bluetoothctl pairable on",
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
                "Wake the band and run: python3 scan_ble.py\n"
                "then pair it once (see docs/cross_platform.md §2.3)"
            ),
        )
        return

    mac = info["address"] or device_id
    if info["paired"] or info["trusted"]:
        report.add(
            OK,
            "device",
            f"{_mask(mac)} paired={'yes' if info['paired'] else 'no'} "
            f"trusted={'yes' if info['trusted'] else 'no'} "
            f"connected={'yes' if info['connected'] else 'no'}",
        )
        if not info["trusted"]:
            report.add(
                INFO,
                "trust",
                "not trusted: BlueZ will not auto-reconnect on its own",
                hint=f"bluetoothctl trust {mac}",
            )
        report.add(
            OK,
            "direct connect",
            "available (bonded device in the BlueZ object tree)",
        )
    else:
        report.add(
            WARN,
            "device",
            f"{_mask(mac)} is known but NOT paired; the band drops the link after ~40 s",
            hint=(
                "bluetoothctl pairable on\n"
                f"bluetoothctl pair {mac}\n"
                f"bluetoothctl trust {mac}\n"
                "(bluetoothctl registers the pairing agent it needs)"
            ),
        )
