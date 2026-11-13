"""Central configuration for the Mi Band 6 health monitor.

Everything that varies by environment (paths, ports, timeouts, credentials) is
defined here instead of being duplicated across domain modules. The modules in
this subpackage have no project dependencies of their own, so every other
package may import them.
"""

from .defaults import (
    DEFAULT_AUTH_TIMEOUT,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_DATA_DIR,
    DEFAULT_DIRECT_CONNECT_TIMEOUT,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_LOG_LEVEL,
    DEFAULT_MAX_POINTS,
    DEFAULT_MTU_MAX,
    DEFAULT_MTU_MIN,
    DEFAULT_RECONNECT_FAST_DELAY,
    DEFAULT_RECONNECT_INITIAL_DELAY,
    DEFAULT_RECONNECT_MAX_DELAY,
    DEFAULT_SCAN_TIMEOUT,
    DEFAULT_SNAPSHOT_POINTS,
    DEFAULT_SSE_KEEPALIVE_SECONDS,
    DEFAULT_SSE_QUEUE_SIZE,
    DEFAULT_WEB_HOST,
    DEFAULT_WEB_PORT,
    ENV_AUTH_KEY,
    ENV_BLE_MTU,
    ENV_CONNECT_TIMEOUT,
    ENV_DEMO_MODE,
    ENV_DEVICE_ID,
    ENV_DEVICE_MAC,
    ENV_DEVICE_NAME,
    ENV_HISTORY_DAYS,
    ENV_LOG_LEVEL,
    ENV_OUTPUT_DIR,
    ENV_SCAN_TIMEOUT,
    ENV_STORAGE_BACKEND,
    ENV_WEB_HOST,
    ENV_WEB_PORT,
)
from .demo import DEMO_AUTH_KEY_HEX, DEMO_DEVICE_ID, DEMO_DEVICE_NAME
from .env import (
    env_flag,
    env_float,
    env_int,
    env_optional_int,
    env_str,
    load_env,
    secure_env_file,
    update_env_file,
)
from .health import HR_MAX, HR_MIN, HR_UNAVAILABLE, SPO2_MAX, SPO2_MIN, STANDARD_GRAVITY
from .paths import (
    DEFAULT_CSV_DIRNAME,
    PACKAGE_ROOT,
    PROJECT_ROOT,
    default_csv_dir,
    default_data_dir,
    default_db_path,
    default_env_path,
    package_path,
    project_path,
)
from .privacy import mask_address, mask_secret, redact_log_value, redact_text
from .security import secure_path

__all__ = [name for name in globals() if not name.startswith("_")]
