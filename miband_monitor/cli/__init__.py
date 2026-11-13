"""Command-line entry points for the Mi Band 6 health monitor.

The historical scripts (miband_client.py, history_report.py, export_csv.py,
sleep_analysis.py, history_dashboard.py, scan_ble.py, ...) remain as thin
facades that call into this package, so existing commands and documentation
keep working unchanged.
"""

from .app import main

__all__ = ["main"]
