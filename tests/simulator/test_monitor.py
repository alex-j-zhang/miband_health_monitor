"""run_monitor supervisor: reconnects, auth retries and one-shot history sync."""

from __future__ import annotations

import sqlite3

import pytest

import miband_monitor.cli.runner as cli_runner
from ecdh_wrapper import backend_name as ecdh_backend_name
from miband_client import MiBand6Client, StreamCallbacks, run_monitor
from sim_band import FakeBleakScanner, SimulatedBand, simulated_factories
from storage import TelemetryStore
from tests.support import (
    AUTH_KEY,
    AUTH_KEY_BYTES,
    KEY,
    MAC,
)


def _make_scanner(address: str):
    class _Scanner(FakeBleakScanner):
        default_address = address

    return _Scanner


async def test_run_monitor_reconnects_after_link_loss(tmp_path):
    # Drop the link after the handshake and at least one heart-rate
    # notification, so reconnects are exercised with real data.  The pure-Python
    # ECDH backend needs a wider window (~0.3 s for the handshake) than the
    # native one, otherwise the band disconnects before authentication finishes.
    disconnect_after = 0.12 if ecdh_backend_name() == "native" else 0.5
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES, disconnect_after=disconnect_after)
    sim_client_factory, _ = simulated_factories(band)
    scanner_class = _make_scanner(MAC)

    def factory(mac: str, key: str) -> MiBand6Client:
        return MiBand6Client(
            mac, key, client_factory=sim_client_factory, scanner_factory=scanner_class
        )

    store = TelemetryStore(tmp_path, backend="sqlite")
    callbacks = StreamCallbacks(
        heart_rate=store.record_heart_rate,
        steps=store.record_steps,
        imu=store.record_imu,
    )
    try:
        await run_monitor(
            MAC,
            AUTH_KEY,
            duration=0.7,
            imu=True,
            callbacks=callbacks,
            scan_timeout=2.0,
            reconnect=True,
            reconnect_initial_delay=0.01,
            reconnect_max_delay=0.02,
            client_factory=factory,
        )
    finally:
        store.close()

    assert band.connect_count >= 2, "supervisor should reconnect after link loss"
    conn = sqlite3.connect(tmp_path / "mi_band.db")
    try:
        heart_rate_rows = conn.execute("SELECT COUNT(*) FROM heart_rate").fetchone()[0]
    finally:
        conn.close()
    assert heart_rate_rows >= 1


async def test_run_monitor_retries_after_authentication_failure(tmp_path):
    # First connection rejects the challenge response; the supervisor must retry
    # and the second connection must authenticate and stream data.
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES, fail_auth_connections=1)
    sim_client_factory, _ = simulated_factories(band)
    scanner_class = _make_scanner(MAC)

    def factory(mac: str, key: str) -> MiBand6Client:
        return MiBand6Client(
            mac, key, client_factory=sim_client_factory, scanner_factory=scanner_class
        )

    store = TelemetryStore(tmp_path, backend="sqlite")
    callbacks = StreamCallbacks(heart_rate=store.record_heart_rate)
    try:
        await run_monitor(
            MAC,
            AUTH_KEY,
            duration=0.6,
            callbacks=callbacks,
            scan_timeout=2.0,
            reconnect=True,
            reconnect_initial_delay=0.01,
            reconnect_max_delay=0.02,
            client_factory=factory,
        )
    finally:
        store.close()

    assert band.connect_count >= 2, "supervisor should retry after auth failure"
    conn = sqlite3.connect(tmp_path / "mi_band.db")
    try:
        heart_rate_rows = conn.execute("SELECT COUNT(*) FROM heart_rate").fetchone()[0]
    finally:
        conn.close()
    assert heart_rate_rows >= 1


async def test_run_monitor_without_reconnect_stops_after_failure():
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES, fail_auth=True)
    sim_client_factory, _ = simulated_factories(band)
    scanner_class = _make_scanner(MAC)

    def factory(mac: str, key: str) -> MiBand6Client:
        return MiBand6Client(
            mac, key, client_factory=sim_client_factory, scanner_factory=scanner_class
        )

    with pytest.raises(RuntimeError, match="Authentication failed"):
        await run_monitor(
            MAC,
            AUTH_KEY,
            duration=5,
            scan_timeout=2.0,
            reconnect=False,
            client_factory=factory,
        )
    assert band.connect_count == 1


async def test_run_monitor_history_synced_once_across_reconnects(tmp_path, monkeypatch):
    disconnect_after = 0.12 if ecdh_backend_name() == "native" else 0.5
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES, disconnect_after=disconnect_after)
    sim_client_factory, _ = simulated_factories(band)
    scanner_class = _make_scanner(MAC)

    def factory(mac: str, key: str) -> MiBand6Client:
        return MiBand6Client(
            mac, key, client_factory=sim_client_factory, scanner_factory=scanner_class
        )

    sync_calls = 0

    async def mock_sync_history(*a, **k):
        nonlocal sync_calls
        sync_calls += 1

    monkeypatch.setattr(cli_runner, "_sync_band_history", mock_sync_history)

    store = TelemetryStore(tmp_path, backend="sqlite")
    callbacks = StreamCallbacks(
        heart_rate=store.record_heart_rate,
        steps=store.record_steps,
        imu=store.record_imu,
    )
    try:
        await run_monitor(
            MAC,
            AUTH_KEY,
            duration=0.7,
            imu=False,
            callbacks=callbacks,
            scan_timeout=2.0,
            reconnect=True,
            reconnect_initial_delay=0.01,
            reconnect_max_delay=0.02,
            sync_history_days=1,
            client_factory=factory,
        )
    finally:
        store.close()

    assert band.connect_count >= 2, "supervisor should reconnect after link loss"
    assert sync_calls == 1, "history should only sync once per supervisor lifecycle"
