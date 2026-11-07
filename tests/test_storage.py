"""Tests for the timestamped JSONL/SQLite persistence layer and CSV export."""

import csv
import sqlite3
import time

import pytest

from export_csv import export_database
from storage import STORAGE_BACKENDS, TelemetryStore, read_jsonl, utc_iso

IMU_PACKET = {
    "type": 1,
    "sequence": 7,
    "samples": [
        {
            "x_raw": 0,
            "y_raw": 0,
            "z_raw": 256,
            "ax_g": 0.0,
            "ay_g": 0.0,
            "az_g": 1.0,
            "ax_ms2": 0.0,
            "ay_ms2": 0.0,
            "az_ms2": 9.8067,
        },
        {
            "x_raw": -128,
            "y_raw": 128,
            "z_raw": 0,
            "ax_g": -0.5,
            "ay_g": 0.5,
            "az_g": 0.0,
            "ax_ms2": -4.9033,
            "ay_ms2": 4.9033,
            "az_ms2": 0.0,
        },
    ],
}

STEPS = {"steps": 1250, "distance_meters": 850, "calories": 45}


def _populate(store: TelemetryStore) -> None:
    before = time.time()
    store.record_heart_rate(72)
    store.record_steps(STEPS)
    store.record_imu(IMU_PACKET)

    explicit_ts = before + 1.5
    store.record_heart_rate(88, timestamp=explicit_ts)


def test_utc_iso_is_utc_with_milliseconds():
    iso = utc_iso(0.0)
    assert iso.startswith("1970-01-01T00:00:00")
    assert iso.endswith("+00:00")
    assert "." in iso


def test_backend_validation(tmp_path):
    with pytest.raises(ValueError):
        TelemetryStore(tmp_path, backend="sqlite3")
    assert "none" in STORAGE_BACKENDS


def test_none_backend_writes_nothing(tmp_path):
    store = TelemetryStore(tmp_path, backend="none")
    _populate(store)
    store.close()
    assert not any(tmp_path.iterdir())
    assert store.stats()["counts"]["heart_rate"] == 0


def test_jsonl_records_are_timestamped(tmp_path):
    with TelemetryStore(tmp_path, backend="jsonl") as store:
        _populate(store)

    for name, expected in (("heart_rate", 2), ("steps", 1), ("imu", 2)):
        rows = read_jsonl(tmp_path / f"{name}.jsonl")
        assert len(rows) == expected, name
        for row in rows:
            assert isinstance(row["ts"], float)
            assert row["iso_time"].endswith("+00:00")

    heart_rate = read_jsonl(tmp_path / "heart_rate.jsonl")
    assert [row["bpm"] for row in heart_rate] == [72, 88]
    assert heart_rate[1]["ts"] > heart_rate[0]["ts"]

    imu = read_jsonl(tmp_path / "imu.jsonl")
    assert [row["sample_index"] for row in imu] == [0, 1]
    assert imu[0]["sequence"] == 7
    assert imu[1]["ax_g"] == pytest.approx(-0.5)

    steps = read_jsonl(tmp_path / "steps.jsonl")[0]
    assert steps["steps"] == 1250
    assert steps["distance_m"] == 850
    assert steps["calories"] == 45


def test_sqlite_rows_are_queryable(tmp_path):
    with TelemetryStore(tmp_path, backend="sqlite") as store:
        _populate(store)

    db_path = tmp_path / "mi_band.db"
    assert db_path.exists()
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM heart_rate").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM imu").fetchone()[0] == 2

        bpm_rows = conn.execute("SELECT bpm FROM heart_rate ORDER BY ts").fetchall()
        assert [row[0] for row in bpm_rows] == [72, 88]
        assert conn.execute("SELECT steps, distance_m, calories FROM steps").fetchone() == (
            1250,
            850,
            45,
        )
        # ts column is monotonic with iso_time
        first_ts, first_iso = conn.execute(
            "SELECT ts, iso_time FROM heart_rate ORDER BY id LIMIT 1"
        ).fetchone()
        assert utc_iso(first_ts) == first_iso
    finally:
        conn.close()


def test_both_backends_contain_the_same_data(tmp_path):
    with TelemetryStore(tmp_path, backend="both") as store:
        _populate(store)

    assert (tmp_path / "mi_band.db").exists()
    conn = sqlite3.connect(tmp_path / "mi_band.db")
    try:
        sqlite_rows = conn.execute("SELECT bpm FROM heart_rate ORDER BY ts").fetchall()
    finally:
        conn.close()
    jsonl_rows = read_jsonl(tmp_path / "heart_rate.jsonl")
    assert [row[0] for row in sqlite_rows] == [row["bpm"] for row in jsonl_rows]


def test_spo2_and_activity_history_persist(tmp_path):
    with TelemetryStore(tmp_path, backend="both") as store:
        store.record_spo2(97, auto_measure=False, timestamp=1000.0)
        store.record_spo2(95, auto_measure=True, timestamp=1060.0)
        store.record_activity_history(
            {
                "kind": 1,
                "intensity": 10,
                "steps": 12,
                "heart_rate": 70,
                "sleep": 0,
                "deep_sleep": 0,
                "rem_sleep": 0,
            },
            timestamp=2000.0,
        )

    spo2_rows = read_jsonl(tmp_path / "spo2.jsonl")
    assert [(row["spo2"], row["auto_measure"]) for row in spo2_rows] == [(97, 0), (95, 1)]
    activity_rows = read_jsonl(tmp_path / "activity_history.jsonl")
    assert activity_rows[0]["steps"] == 12
    assert activity_rows[0]["heart_rate"] == 70

    conn = sqlite3.connect(tmp_path / "mi_band.db")
    try:
        assert conn.execute("SELECT spo2 FROM spo2 ORDER BY ts").fetchall() == [(97,), (95,)]
        assert conn.execute("SELECT steps, heart_rate FROM activity_history").fetchone() == (12, 70)
    finally:
        conn.close()


def test_export_csv_roundtrip(tmp_path):
    with TelemetryStore(tmp_path, backend="sqlite") as store:
        _populate(store)

    exported = export_database(tmp_path / "mi_band.db", tmp_path / "csv")
    assert set(exported) == {
        "heart_rate",
        "steps",
        "imu",
        "spo2",
        "activity_history",
    }
    assert exported["heart_rate"][1] == 2
    assert exported["imu"][1] == 2
    assert exported["spo2"][1] == 0

    with open(tmp_path / "csv" / "heart_rate.csv", newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["id", "ts", "iso_time", "bpm"]
    assert [row[3] for row in rows[1:]] == ["72", "88"]

    with open(tmp_path / "csv" / "imu.csv", newline="", encoding="utf-8") as handle:
        imu_rows = list(csv.reader(handle))
    assert len(imu_rows) == 3


def test_export_missing_database_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        export_database(tmp_path / "missing.db", tmp_path / "csv")


def test_records_after_close_are_dropped_not_raised(tmp_path):
    store = TelemetryStore(tmp_path, backend="jsonl")
    store.close()
    store.record_heart_rate(99)  # must not raise inside a BLE callback
    assert store.stats()["dropped"] == 1
    store.close()  # idempotent
