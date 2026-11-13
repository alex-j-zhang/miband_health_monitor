"""Shared sample builders and database seeding for the sleep tests."""

from __future__ import annotations

from datetime import datetime

from sleep_analysis import SCORE_FLAG
from storage import TelemetryStore

BASE = datetime(2026, 11, 6, 22, 0).timestamp()


def sample(minute: int, *, kind: int = 240, hr: int = 52, steps: int = 0, intensity: int = 0) -> dict:
    return {
        "ts": BASE + minute * 60,
        "kind": kind,
        "intensity": intensity,
        "steps": steps,
        "heart_rate": hr,
    }


def series(count: int, *, start: int = 0, **kwargs) -> list[dict]:
    return [sample(start + i, **kwargs) for i in range(count)]


def scored(minute: int, *, light: int = 64, deep: int = 32, rem: int = 32, **kwargs) -> dict:
    """A sample carrying the trailing score bytes the way the band writes them."""
    item = sample(minute, **kwargs)
    item["sleep"] = light
    item["deep_sleep"] = SCORE_FLAG | deep if deep is not None else 0
    item["rem_sleep"] = SCORE_FLAG | rem if rem is not None else 0
    return item


def _seed_database(tmp_path, samples):
    store = TelemetryStore(tmp_path, backend="sqlite")
    try:
        for item in samples:
            store.record_activity_history(
                {
                    "kind": item["kind"],
                    "intensity": item["intensity"],
                    "steps": item["steps"],
                    "heart_rate": item["heart_rate"],
                    "unknown1": 5,
                    "sleep": item.get("sleep", 60),
                    "deep_sleep": item.get("deep_sleep", 128),
                    "rem_sleep": item.get("rem_sleep", 128),
                },
                timestamp=item["ts"],
            )
    finally:
        store.close()
    return tmp_path / "mi_band.db"
