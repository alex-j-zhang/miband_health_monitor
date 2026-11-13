"""Dashboard CLI arguments and the persisted-history (?source=db) bridge."""

from __future__ import annotations

import pytest

from dashboard import make_db_history_provider


def test_cli_dashboard_arguments():
    from miband_client import parse_args

    args = parse_args(["--demo", "5", "--web-port", "0", "--web-host", "0.0.0.0"])
    assert args.web_port == 0
    assert args.web_host == "0.0.0.0"

    args = parse_args(["--demo", "5"])
    assert args.web_port is None


# ---------------------------------------------------------------------------
# Persisted-history bridge (?source=db)
# ---------------------------------------------------------------------------
def test_db_history_provider_reshapes_sqlite_rows(tmp_path):
    from storage import TelemetryStore

    store = TelemetryStore(tmp_path, backend="both")
    store.record_heart_rate(71, timestamp=1000.0)
    store.record_heart_rate(75, timestamp=1001.0)
    store.record_steps(
        {"steps": 100, "distance_meters": 70, "calories": 4}, timestamp=1000.0
    )
    store.record_spo2(98, auto_measure=True, timestamp=1000.0)
    store.record_imu(
        {"samples": [{"ax_g": 0.0, "ay_g": 0.0, "az_g": 1.0}]}, timestamp=1000.0
    )
    store.close()

    provider = make_db_history_provider(store.db_path)
    hr = provider("heart_rate", 10)
    assert [row["bpm"] for row in hr] == [71, 75]  # chronological, not DESC
    assert hr[0]["kind"] == "heart_rate" and hr[0]["iso_time"]
    assert provider("heart_rate", 1) == [hr[-1]]

    steps = provider("steps", 10)
    assert (steps[0]["steps"], steps[0]["distance_m"], steps[0]["calories"]) == (
        100, 70, 4,
    )
    spo2 = provider("spo2", 10)
    assert (spo2[0]["spo2"], spo2[0]["status"]) == (98, 1)
    imu = provider("imu", 10)
    assert imu[0]["magnitude_g"] == 1.0


def test_db_history_provider_tolerates_missing_data(tmp_path):
    import sqlite3

    assert make_db_history_provider(tmp_path / "nope.db")("heart_rate", 10) == []

    empty_db = tmp_path / "empty.db"
    sqlite3.connect(empty_db).close()
    assert make_db_history_provider(empty_db)("heart_rate", 10) == []


def test_db_history_provider_rejects_unknown_kind(tmp_path):
    with pytest.raises(ValueError, match="unknown metric kind"):
        make_db_history_provider(tmp_path / "nope.db")("bogus", 10)


def test_dashboard_page_prefills_from_database():
    from miband_monitor.dashboard.page import compose_page

    html = compose_page()
    assert "source=db" in html
    assert "dbSeries" in html
    assert "loadDbHistory" in html


async def test_main_wires_db_history_provider(monkeypatch, tmp_path):
    """The CLI must pass the provider, not merely expose the parameter."""
    import miband_monitor.cli.app as app_module

    captured = {}

    class FakeServer:
        def __init__(self, hub, **kwargs):
            captured["provider"] = kwargs.get("history_provider")
            self.url = "http://127.0.0.1:0"

        async def start(self):
            return None

        async def stop(self):
            return None

    monkeypatch.setattr(app_module, "DashboardServer", FakeServer)
    code = await app_module.main(
        [
            "--demo", "1", "--web-port", "0",
            "--output", str(tmp_path), "--storage", "both",
        ]
    )
    assert code == 0
    provider = captured.get("provider")
    assert callable(provider)
    assert provider("heart_rate", 10)  # demo rows were persisted before close
