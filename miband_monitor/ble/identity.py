"""Device identity, credential normalisation and environment lookups.

The client can be addressed either by a Bluetooth MAC (Linux/Windows) or by a
per-host CoreBluetooth UUID (macOS), and optionally by an advertised-name
substring. The helpers here implement that cross-platform matching plus the
credential parsing used by every entry point, so --device-id/.env behaviour is
identical across the CLI, tools and tests.

Environment variable names come from miband_monitor.config.defaults, and the
credentials-file permissions are delegated to
miband_monitor.config.env.secure_env_file.
"""

from __future__ import annotations

import os
from typing import Optional

from ..config.defaults import ENV_DEVICE_ID, ENV_DEVICE_MAC
from ..config.env import secure_env_file


def normalize_auth_key(auth_key_hex: str) -> bytes:
    """Validate and decode a 32-hex-char auth key (0x prefix tolerated)."""
    cleaned = (auth_key_hex or "").strip().lower()
    if cleaned.startswith("0x"):
        cleaned = cleaned[2:]
    if len(cleaned) != 32:
        raise ValueError(f"Auth key hex string must be 32 characters, got {len(cleaned)}")
    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ValueError(f"Auth key is not valid hexadecimal: {exc}") from exc


# ---------------------------------------------------------------------------
# Cross-platform device identity
# ---------------------------------------------------------------------------
#: Values that mean "not configured yet" in a fresh .env.
PLACEHOLDER_DEVICE_PREFIXES = ("XX:", "XX-", "XX_")


def is_placeholder_device_id(device_id: Optional[str]) -> bool:
    """Return True for empty or template values such as XX:XX:..."""
    value = (device_id or "").strip()
    if not value:
        return True
    return value.upper().startswith(PLACEHOLDER_DEVICE_PREFIXES)


def normalize_device_identifier(value: str) -> str:
    """Normalise a Bluetooth address / CoreBluetooth UUID for comparison.

    Handles AA:BB:CC:DD:EE:FF, AA-BB-..., lowercase input and the {UUID} form
    some tools print for macOS device identifiers.
    """
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


def device_matches(
    device_id: Optional[str],
    device_name: Optional[str],
    device: object,
    advertisement: Optional[object] = None,
) -> bool:
    """Return True when device matches the configured identifier or name.

    device_id is compared against device.address; on Linux/Windows this is the
    Bluetooth MAC address, while on macOS CoreBluetooth exposes a per-host UUID
    there. device_name (a case-insensitive substring) is an optional extra match
    that works on every platform, which makes it possible to pair the client
    with a band even when the address is unavailable.
    """
    wanted = normalize_device_identifier(device_id or "")
    if wanted:
        address = normalize_device_identifier(str(getattr(device, "address", "") or ""))
        if address and address == wanted:
            return True

    wanted_name = (device_name or "").strip().lower()
    if wanted_name:
        names = [
            getattr(device, "name", None),
            getattr(advertisement, "local_name", None),
        ]
        for candidate in names:
            if candidate and wanted_name in str(candidate).lower():
                return True
    return False


def _env_device_id() -> str:
    """Read the configured device identifier from the environment.

    DEVICE_ID is the cross-platform name (MAC address or macOS UUID);
    DEVICE_MAC is still accepted for backwards compatibility. Placeholder
    template values are ignored so a legacy real value wins over the template.
    """
    values = [
        (os.getenv(ENV_DEVICE_ID) or "").strip(),
        (os.getenv(ENV_DEVICE_MAC) or "").strip(),
    ]
    for value in values:
        if value and not is_placeholder_device_id(value):
            return value
    return values[0] or values[1]


def _secure_env_file() -> None:
    """Tighten .env to 0600 (historical helper kept as a thin wrapper).

    The real implementation lives in miband_monitor.config.env so every entry
    point shares the same permission policy. This alias preserves the old
    miband_client._secure_env_file name used by tests and callers.
    """
    secure_env_file()
