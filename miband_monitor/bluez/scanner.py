"""BleakScanner keyword construction for BlueZ.

Kept deliberately minimal on Linux.  DuplicateData=True was added to fight
"the band only advertises in short pulses", but on Bluetooth 5.x/6.x
controllers BlueZ implements it by tearing down and re-arming the extended scan
every ~5 s (measured: 31 0x2041/0x2042 HCI commands in a 75 s window).  On the
BARROT 33fa:0012 adapter that churn eventually wedges the scan state machine,
after which the controller still completes every HCI command yet reports zero
advertising events - a silent deaf adapter that only a USB-level reset recovers
from.  Duplicate filtering is therefore left at the BlueZ default;
MIBAND_BLUEZ_DUPLICATE_DATA=1 restores the old behaviour for experiments.

Cross-module calls go through the owning submodule object (platform.*) so tests
can patch miband_monitor.bluez.platform.is_bluez_platform.
"""

from __future__ import annotations

import os
from typing import Any

from . import platform
from .constants import DUPLICATE_DATA_ENV


def scanner_kwargs(scanner_factory: Any = None) -> dict:
    """Return platform-specific kwargs for BleakScanner."""
    if not platform.is_bluez_platform():
        return {}
    if scanner_factory is not None:
        factory_name = getattr(scanner_factory, "__name__", "")
        module_name = getattr(scanner_factory, "__module__", "")
        if "sim" in module_name or "Fake" in factory_name:
            return {}
    duplicate = (os.getenv(DUPLICATE_DATA_ENV, "") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    return {
        "bluez": {
            "filters": {
                "DuplicateData": duplicate,
                "Transport": "le",
            }
        }
    }
