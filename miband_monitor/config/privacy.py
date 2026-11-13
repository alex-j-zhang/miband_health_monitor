"""Small, dependency-free helpers that keep identifiers out of logs.

The project deliberately logs enough to debug a flaky BLE link; real device
addresses and authentication material must not appear verbatim in those logs,
in terminal output from the auth-key helpers, or in issue reports.
"""

from __future__ import annotations

import re
from typing import Optional

#: Bluetooth MAC address (colon or dash separated).
_MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
#: Long hex blobs can only be keys/digests; never echo them from telemetry logs.
_LONG_HEX_RE = re.compile(r"\b[0-9A-Fa-f]{32,}\b")

#: Environment variables whose values are credentials, never log them.
SENSITIVE_ENV_KEYS = frozenset(
    {
        "AUTH_KEY",
        "MIBAND_AUTH_KEY",
        "APP_TOKEN",
        "PASSWORD",
        "MIBAND_ECDH_LIB",  # may embed a private path, still user-specific
    }
)

_REDACTED = "<redacted>"


def mask_secret(value: Optional[str], visible: int = 0) -> str:
    """Return a redacted representation of a credential.

    visible may keep a small prefix for debugging, but the default hides the
    whole value. An empty value stays empty so log messages remain readable.
    """
    text = (value or "").strip()
    if not text:
        return ""
    if visible and visible > 0:
        return text[:visible] + "*" * max(0, len(text) - visible)
    return _REDACTED


def mask_address(address: Optional[str]) -> str:
    """Mask the middle octets of a Bluetooth MAC, or most of a UUID.

    Example: AA:BB:CC:DD:EE:FF -> AA:BB:**:**:EE:FF.
    """
    text = (address or "").strip()
    if not text:
        return ""
    parts = text.replace("-", ":").split(":")
    if len(parts) == 6 and all(parts):
        return ":".join([parts[0], parts[1], "**", "**", parts[4], parts[5]])
    if len(text) > 12:
        return text[:8] + "..." + text[-4:]
    return text


def redact_log_value(value: object) -> str:
    """Best-effort redaction for arbitrary log values."""
    text = str(value)
    if len(text) >= 24:
        return text[:4] + "..." + text[-4:]
    return _REDACTED


def redact_text(text: str) -> str:
    """Mask MAC addresses and long hex blobs inside an arbitrary log line."""
    if not text:
        return text
    text = _MAC_RE.sub(lambda match: mask_address(match.group(0)), text)
    return _LONG_HEX_RE.sub(_REDACTED, text)
