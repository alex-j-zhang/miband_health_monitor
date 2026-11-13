"""ATT MTU acquisition for the BlueZ backend.

bleak leaves mtu_size at 23 until the backend's _acquire_mtu() is called; the
WinRT and CoreBluetooth backends fill it in automatically.  Without this helper
every chunked-transfer write would be capped at the 20-byte default MTU.

Cross-module calls go through the owning submodule object (platform.*) so tests
can patch miband_monitor.bluez.platform.is_bluez_platform.
"""

from __future__ import annotations

import contextlib
import inspect
from typing import Any

from . import platform
from .constants import logger


async def try_acquire_mtu(client: Any) -> bool:
    """Ask bleak's BlueZ backend for the negotiated ATT MTU.

    Returns True when an acquisition was attempted successfully.
    """
    if not platform.is_bluez_platform():
        return False

    backend = getattr(client, "_backend", None)
    acquire = getattr(backend, "_acquire_mtu", None)
    if acquire is None:
        return False

    try:
        get_services = getattr(client, "get_services", None)
        if get_services is not None:
            with contextlib.suppress(Exception):
                await get_services()
        result = acquire()
        # bleak versions differ: _acquire_mtu is a coroutine in 3.x.
        if inspect.isawaitable(result):
            await result
        return True
    except Exception as exc:
        logger.debug("Could not acquire the ATT MTU: %s", exc)
        return False
