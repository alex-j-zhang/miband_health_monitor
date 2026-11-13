"""Platform detection plus BlueZ address/path conventions.

Everything here is pure and side-effect free: the platform gate keeps every
D-Bus helper in this package a no-op off Linux, and the address helpers mirror
BlueZ's own object naming (dev_AA_BB_CC_DD_EE_FF under the discovering adapter)
so a known device path can be predicted without a discovery pass.
"""

from __future__ import annotations

import sys
from typing import Optional

from .constants import BAND_PULSE_PERIOD


def is_bluez_platform() -> bool:
    """True on Linux, where bleak talks to BlueZ over D-Bus."""
    return sys.platform.startswith("linux")


def normalize_address(address: str) -> str:
    """Return the canonical AA:BB:CC:DD:EE:FF form of a MAC address."""
    raw = (address or "").strip().upper().replace("-", ":")
    compact = "".join(ch for ch in raw if ch in "0123456789ABCDEF")
    if len(compact) == 12:
        return ":".join(compact[i : i + 2] for i in range(0, 12, 2))
    return raw


def looks_like_mac(address: str) -> bool:
    return len(normalize_address(address)) == 17 and normalize_address(address).count(":") == 5


def device_path_for_address(address: str, adapter: str = "hci0") -> str:
    """Return the deterministic BlueZ object path for *address*.

    BlueZ names device objects dev_AA_BB_CC_DD_EE_FF under the adapter that
    discovered them, so the path can be predicted without a discovery pass.
    """
    return f"/org/bluez/{adapter}/dev_{normalize_address(address).replace(':', '_')}"


def link_standby_budget(connect_timeout: Optional[float]) -> float:
    """Return the BlueZ link budget for one connection attempt, in seconds.

    A sleeping band only answers during its next isolated pulse, so a budget
    shorter than one full BAND_PULSE_PERIOD cannot succeed and only produces the
    "detect pulse, wait, give up, re-scan" leapfrog loop.
    """
    try:
        requested = float(connect_timeout or 0.0)
    except (TypeError, ValueError):
        requested = 0.0
    return max(requested, BAND_PULSE_PERIOD)
