# Diagnostic and forensic tools

These scripts are optional companions to the main client. They never run as part
of the telemetry pipeline and most of them talk to real hardware, so read the
module docstring before running one.

| Script | Purpose |
|---|---|
| capture_history_payload.py | Capture raw band-side history payloads (.bin + SHA-256 metadata) for offline validation. |
| import_spo2_sleep.py | Decode captured 0x26 payloads and replay them through TelemetryStore. |
| probe_declared.py | Ask the firmware how much history it still holds without consuming it. |
| probe_fetch_types.py | Query arbitrary history type codes and print the start-date reply. |
| probe_sleep_session.py | Probe the Huami SLEEP_SESSION (0x48) history type. |
| calibrate_sleep.py | Compare candidate readings of the sleep bytes against Zepp Life numbers. |
| check_file_sizes.py | CI guard: first-party code/asset files must stay <= 300 lines. |
| check_privacy.py | CI guard: no real MAC/auth key/home path in Git-tracked files. |
| check_docs_links.py | CI guard: relative Markdown links resolve. |

Run everything from the repository root, for example:

    ./.venv/bin/python tools/check_file_sizes.py
    ./.venv/bin/python tools/capture_history_payload.py --days 7

The first-party implementation lives in miband_monitor/; these CLIs import it and
keep only argument parsing plus hardware-facing diagnostics.
