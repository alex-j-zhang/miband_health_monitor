"""Environment doctor and BLE scanning helpers.

The historical command-line helpers are preserved behind this package:

- report: result collection and the stable [OK]/[INFO]/[WARN]/[FAIL] format;
- checks: small pure helpers (bleak version, .env permissions, masking);
- doctor: run_doctor(), the read-only self-check behind --doctor;
- scanning: scan_devices(), the one-shot Mi Band discovery helper;
- live_scan: main(), the continuous packet logger.

run_doctor keeps the old printed output, summary line and exit codes.  The
package root deliberately re-exports the BlueZ names that older code patched on
the doctor module (is_bluez_platform, list_adapters, find_device) so test
migrations can target miband_monitor.diagnostics.doctor, while external callers
can simply import run_doctor.
"""

from __future__ import annotations

from .checks import _bleak_version, _env_mode_hint, _mask
from .doctor import _auth_helpers, _check_bluez, run_doctor
from .live_scan import SCAN_SECONDS, main as live_scan_main
from .report import FAIL, INFO, OK, WARN, _MARK, _PREFIX, _Report
from .scanning import scan_devices

__all__ = [
    "run_doctor",
    "scan_devices",
    "live_scan_main",
    "SCAN_SECONDS",
    "OK",
    "INFO",
    "WARN",
    "FAIL",
    "_Report",
    "_PREFIX",
    "_MARK",
    "_bleak_version",
    "_env_mode_hint",
    "_mask",
    "_auth_helpers",
    "_check_bluez",
]
