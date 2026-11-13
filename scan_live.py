"""Backward-compatible continuous BLE packet logger.

The implementation now lives in miband_monitor.diagnostics.live_scan. Use it
when the band only advertises for a short window; the printed address goes into
DEVICE_ID (a MAC on Linux/Windows, a CoreBluetooth UUID on macOS).
"""

from __future__ import annotations

import asyncio

from miband_monitor.diagnostics.live_scan import SCAN_SECONDS, main

__all__ = ["SCAN_SECONDS", "main"]


if __name__ == "__main__":
    asyncio.run(main())
