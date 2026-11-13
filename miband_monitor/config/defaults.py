"""Default values and environment-variable names.

Values here are defaults, never secrets. Every constant documents its unit and,
where it matters, the hardware observation behind it. Command-line flags and
.env keep overriding these defaults exactly as before the refactor.
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Environment variable names (single source of truth)
# ---------------------------------------------------------------------------
ENV_DEVICE_ID = "DEVICE_ID"
ENV_DEVICE_MAC = "DEVICE_MAC"
ENV_DEVICE_NAME = "DEVICE_NAME"
ENV_AUTH_KEY = "AUTH_KEY"
ENV_BLE_MTU = "BLE_MTU"
ENV_OUTPUT_DIR = "OUTPUT_DIR"
ENV_STORAGE_BACKEND = "STORAGE_BACKEND"
ENV_SCAN_TIMEOUT = "SCAN_TIMEOUT"
ENV_CONNECT_TIMEOUT = "CONNECT_TIMEOUT"
ENV_LOG_LEVEL = "LOG_LEVEL"
ENV_DEMO_MODE = "DEMO_MODE"
ENV_WEB_PORT = "WEB_PORT"
ENV_WEB_HOST = "WEB_HOST"
ENV_HISTORY_DAYS = "HISTORY_DAYS"
ENV_BAND_TZ = "BAND_TZ"
ENV_ECDH_BACKEND = "MIBAND_ECDH_BACKEND"
ENV_ECDH_LIBRARY = "MIBAND_ECDH_LIB"
ENV_BLUEZ_TRUST = "MIBAND_BLUEZ_TRUST"
ENV_BLUEZ_AGENT = "MIBAND_BLUEZ_AGENT"
ENV_BLUEZ_DUPLICATE_DATA = "MIBAND_BLUEZ_DUPLICATE_DATA"
ENV_SMP_SETTLE_DELAY = "MIBAND_SMP_SETTLE_DELAY"

# ---------------------------------------------------------------------------
# Paths and CLI defaults
# ---------------------------------------------------------------------------
#: Default telemetry directory, relative to the current working directory.
DEFAULT_DATA_DIR = "data"
#: SQLite database filename created inside the telemetry directory.
DEFAULT_DB_FILENAME = "mi_band.db"
#: Sub-directory created by --export-csv inside the output directory.
DEFAULT_CSV_DIRNAME = "csv"
DEFAULT_LOG_LEVEL = "INFO"

# ---------------------------------------------------------------------------
# BLE connection budgets (seconds)
# ---------------------------------------------------------------------------
#: Time spent waiting for one advertising packet from the band. A Mi Band 6
#: without a phone link emits one isolated pulse every 40-60 s, so short windows
#: cannot work.
DEFAULT_SCAN_TIMEOUT = 90.0
#: Budget for one full BLE link attempt; raised to at least one advertising
#: period by the BlueZ link helper.
DEFAULT_CONNECT_TIMEOUT = 75.0
#: Shorter budget for the bonded-device fast path before a scan fallback.
DEFAULT_DIRECT_CONNECT_TIMEOUT = 10.0
#: Chunked ECDH handshake deadline (seconds).
DEFAULT_AUTH_TIMEOUT = 30.0
#: Reconnect back-off policy after failures / unexpected disconnects.
DEFAULT_RECONNECT_INITIAL_DELAY = 1.0
DEFAULT_RECONNECT_MAX_DELAY = 30.0
#: A session that was authenticated and then dropped reconnects almost at once
#: to catch the band's short aggressive-advertising window.
DEFAULT_RECONNECT_FAST_DELAY = 0.3

#: ATT MTU bounds accepted from --mtu / BLE_MTU.
DEFAULT_MTU_MIN = 23
DEFAULT_MTU_MAX = 517

#: How often the scan loop logs progress while waiting for a pulse.
DEFAULT_SCAN_HEARTBEAT_SECONDS = 5.0
#: Seconds of complete radio silence after which the scan warns about a wedged
#: USB adapter (measured on the BARROT BT 6.0 adapter).
DEFAULT_SCAN_SILENCE_WARNING_SECONDS = 25

# ---------------------------------------------------------------------------
# Web dashboard defaults
# ---------------------------------------------------------------------------
DEFAULT_WEB_HOST = "127.0.0.1"
#: None means: do not start the dashboard.
DEFAULT_WEB_PORT: Optional[int] = None
DEFAULT_MAX_POINTS = 7200
DEFAULT_SNAPSHOT_POINTS = 600
DEFAULT_SSE_QUEUE_SIZE = 512
DEFAULT_SSE_KEEPALIVE_SECONDS = 15.0
DEFAULT_HTTP_READ_TIMEOUT = 10.0
DEFAULT_HTTP_HEADER_TIMEOUT = 5.0
DEFAULT_HTTP_STOP_TIMEOUT = 2.0

# ---------------------------------------------------------------------------
# History / analysis defaults
# ---------------------------------------------------------------------------
DEFAULT_HISTORY_DAYS = 7
#: Default window (days) for the offline historical dashboard.
DEFAULT_OFFLINE_DAYS = 7
