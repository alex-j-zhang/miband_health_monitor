"""Account helpers that retrieve the band authentication key.

The cloud protocol itself lives in the vendored huami_token package; this
package contains only the interactive command-line flows and their endpoint
constants.
"""

from .constants import ENV_PATH, TARGET_MAC, preferred_device_env_key, resolve_target_mac
from .extractor import run_extractor
from .token import fetch_devices, main as token_main

__all__ = [
    "ENV_PATH",
    "TARGET_MAC",
    "preferred_device_env_key",
    "resolve_target_mac",
    "run_extractor",
    "fetch_devices",
    "token_main",
]
