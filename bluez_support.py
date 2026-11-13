"""Backward-compatible facade for miband_monitor.bluez.

The Linux/BlueZ D-Bus helpers, link standby loop, pairing agent and error
translation now live in the miband_monitor.bluez package. This module keeps the
historical import path and the complete public/private API.
"""

from __future__ import annotations

from miband_monitor.bluez import *  # noqa: F401,F403
from miband_monitor.bluez import __all__ as _package_all
from miband_monitor.bluez.constants import logger

__all__ = list(_package_all) + ["logger"]
