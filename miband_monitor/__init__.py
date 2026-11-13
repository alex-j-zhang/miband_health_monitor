"""Mi Band 6 health monitor: first-party implementation package.

The package is split by responsibility so that the BLE transport, protocol
codecs, persistence, analysis and presentation layers can evolve and be tested
independently:

- miband_monitor.config: defaults, environment and paths
- miband_monitor.crypto: ECDH native / pure-Python backends
- miband_monitor.bluez: Linux/BlueZ D-Bus compatibility helpers
- miband_monitor.ble: Mi Band 6 BLE client and protocol codecs
- miband_monitor.history: band-side history fetch and parsers
- miband_monitor.storage: JSONL/SQLite telemetry sinks
- miband_monitor.dashboard: live HTTP/SSE dashboard
- miband_monitor.sleep: derived sleep sessions and stages
- miband_monitor.offline_dashboard: static historical dashboard
- miband_monitor.simulator: in-memory band and fake bleak backend
- miband_monitor.cli: command-line entry points
- miband_monitor.diagnostics: doctor and scanning helpers
- miband_monitor.reports: local telemetry summaries
- miband_monitor.auth: account auth-key retrieval helpers

Backwards compatibility: the historical top-level modules (miband_client,
storage, dashboard, ...) remain as thin facades that re-export the public names
from this package. New code should import from the package.
"""

__all__ = ["__version__"]

#: Human-readable release marker, kept in sync with README/docs.
__version__ = "0.2.0"
