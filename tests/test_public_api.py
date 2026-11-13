"""Root facades must keep exporting the historical public API.

The refactor moved the implementations into the 'miband_monitor.<domain>'
packages, but older scripts, tools and CI jobs still import the root modules.
Those compatibility facades are part of the public surface and are pinned here.
"""

from __future__ import annotations

import importlib

import pytest

FACADE_EXPORTS = {
    "miband_client": (
        "CHUNKED_DEFAULT_MTU",
        "MiBand6Client",
        "StreamCallbacks",
        "UUID_CHAR_HR_MEASUREMENT",
        "device_matches",
        "ecdh_backend_name",
        "encrypt_challenge",
        "main",
        "normalize_auth_key",
        "parse_args",
        "parse_heart_rate",
        "parse_raw_sensor_data",
        "parse_realtime_steps",
        "run_demo",
        "run_monitor",
        "write_chunked_value",
    ),
    "storage": ("STORAGE_BACKENDS", "TelemetryStore", "read_jsonl", "utc_iso"),
    "dashboard": (
        "DashboardServer",
        "METRIC_KINDS",
        "TelemetryHub",
        "compose_page",
        "make_db_history_provider",
    ),
    "sleep_analysis": (
        "STAGE_AWAKE",
        "STAGE_DEEP",
        "STAGE_LIGHT",
        "STAGE_UNKNOWN",
        "analyze",
        "build_session",
        "classify_session",
        "detect_sessions",
        "load_samples",
        "main",
        "score_estimates",
    ),
    "history_dashboard": (
        "BAND_ACTIVE",
        "BAND_REST",
        "BAND_SLEEP",
        "build",
        "collect",
        "hypnogram_svg",
        "main",
        "render",
    ),
    "bluez_support": (
        "BlueZPairingAgent",
        "connected_device",
        "device_path_for_address",
        "describe_ble_error",
        "disconnect_device",
        "find_device",
        "is_bluez_platform",
        "looks_like_mac",
        "make_ble_device",
        "normalize_address",
        "paired_device",
        "scanner_kwargs",
        "try_acquire_mtu",
        "wait_for_bluez_link",
    ),
    "sim_band": (
        "FakeBleakScanner",
        "FakeDevice",
        "SimulatedBand",
        "make_simulated_client",
        "simulated_factories",
    ),
    "band_history": (
        "BandHistoryFetcher",
        "FetchDataType",
        "HistoryFetchError",
        "HistoryParseError",
        "HistorySyncResult",
        "parse_activity",
        "parse_spo2_normal",
        "parse_spo2_sleep",
        "summarize_history_result",
        "sync_history",
    ),
    "ecdh_wrapper": ("backend_name", "compute_shared_secret", "generate_ecdh_keys"),
    "doctor": ("run_doctor",),
}


@pytest.mark.parametrize("module_name", sorted(FACADE_EXPORTS))
def test_root_facade_exports_legacy_names(module_name):
    module = importlib.import_module(module_name)
    missing = [name for name in FACADE_EXPORTS[module_name] if not hasattr(module, name)]
    assert not missing, f"{module_name} facade no longer exports: {missing}"


@pytest.mark.parametrize(
    ("module_name", "attribute", "canonical_module"),
    [
        ("miband_client", "MiBand6Client", "miband_monitor.ble.client"),
        ("storage", "TelemetryStore", "miband_monitor.storage"),
        ("dashboard", "DashboardServer", "miband_monitor.dashboard.server"),
        ("doctor", "run_doctor", "miband_monitor.diagnostics.doctor"),
    ],
)
def test_facades_delegate_to_the_canonical_objects(module_name, attribute, canonical_module):
    facade = importlib.import_module(module_name)
    canonical = importlib.import_module(canonical_module)
    assert getattr(facade, attribute) is getattr(canonical, attribute)
