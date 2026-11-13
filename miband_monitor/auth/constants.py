"""Endpoint/header constants and .env helpers for Zepp/Xiaomi auth-key tools.

These are reverse-engineered public client constants, not user data. Loading
the project .env happens before resolving TARGET_MAC so pre-configured device
identifiers are honoured even in a fresh shell.
"""

from __future__ import annotations

from pathlib import Path

from ..config.defaults import ENV_DEVICE_ID, ENV_DEVICE_MAC
from ..config.env import env_str, load_env
from ..config.paths import default_env_path

#: Placeholder prefixes that mean "unconfigured template value" in .env.
_PLACEHOLDER_PREFIXES = ("XX:", "XX-", "XX_")

#: Credentials file updated by both helpers.
ENV_PATH = default_env_path()
load_env(ENV_PATH)


def resolve_target_mac() -> str:
    """Return the configured non-placeholder target identifier in uppercase."""
    for key in (ENV_DEVICE_ID, ENV_DEVICE_MAC):
        candidate = env_str(key).upper()
        if candidate and not candidate.startswith(_PLACEHOLDER_PREFIXES):
            return candidate
    return ""


def preferred_device_env_key(env_path: Path = ENV_PATH) -> str:
    """Return the .env key to write the device MAC to (DEVICE_ID when present)."""
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return ENV_DEVICE_MAC
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{ENV_DEVICE_ID}=") or stripped.startswith(f"{ENV_DEVICE_ID} ="):
            return ENV_DEVICE_ID
    return ENV_DEVICE_MAC


#: Optional target device read at import time (backwards-compatible constant).
TARGET_MAC = resolve_target_mac()

#: Zepp web API endpoints tried in order: (display name, URL template, region).
ZEPP_WEB_ENDPOINTS = (
    ("api-mifit.huami.com (CN)", "https://api-mifit.huami.com/users/{user_id}/devices", "CN"),
    ("api-mifit-cn2.zepp.com (CN2)", "https://api-mifit-cn2.zepp.com/users/{user_id}/devices", "CN"),
    ("api-mifit.zepp.com (Global)", "https://api-mifit.zepp.com/users/{user_id}/devices", "US"),
    ("api-mifit-us3.zepp.com (US3)", "https://api-mifit-us3.zepp.com/users/{user_id}/devices", "US"),
)

#: Fixed query parameters of the Zepp web device-list endpoint.
ZEPP_WEB_BASE_PARAMS = {
    "appid": "2882303761517383915",
    "channel": "play",
    "enableMultiDevice": "true",
    "device_type": "android_phone",
    "cv": "151262_8.7.1-play",
    "device": "android_33",
    "v": "2.0",
}

#: Fixed request headers of the Zepp web device-list endpoint.
ZEPP_WEB_HEADERS = {
    "User-Agent": "Zepp/8.5.3-play (2201116SG; Android 12; Density/2.525)",
    "channel": "play",
    "appname": "com.huami.midong",
}

#: Per-request timeout (seconds) for each regional endpoint.
ZEPP_WEB_TIMEOUT = 10
