"""TelemetryHub ring buffer, snapshots and subscriber fan-out."""

from __future__ import annotations

import asyncio

import pytest

from dashboard import METRIC_KINDS, TelemetryHub


def test_hub_publish_and_snapshot():
    hub = TelemetryHub(max_points=10)
    hub.publish_heart_rate(72)
    hub.publish_steps({"steps": 1250, "distance_meters": 850, "calories": 45})
    hub.publish_imu(
        {
            "type": 1,
            "sequence": 3,
            "samples": [
                {"ax_g": 0.0, "ay_g": 0.0, "az_g": 1.0},
                {"ax_g": 0.0, "ay_g": 0.0, "az_g": 1.0},
            ],
        }
    )
    hub.publish_spo2(98)

    snapshot = hub.snapshot()
    assert set(snapshot["series"]) == set(METRIC_KINDS)
    assert snapshot["latest"]["heart_rate"]["bpm"] == 72
    assert snapshot["latest"]["spo2"]["spo2"] == 98
    assert snapshot["latest"]["steps"]["steps"] == 1250
    assert snapshot["latest"]["imu"]["magnitude_g"] == pytest.approx(1.0)
    assert snapshot["counts"] == {"heart_rate": 1, "steps": 1, "imu": 1, "spo2": 1}
    # Every row carries a UTC timestamp.
    for kind in METRIC_KINDS:
        row = snapshot["series"][kind][0]
        assert isinstance(row["ts"], float)
        assert row["iso_time"].endswith("+00:00")

    history = hub.history("heart_rate", limit=5)
    assert history[-1]["bpm"] == 72
    with pytest.raises(ValueError):
        hub.history("bogus")


def test_hub_ring_buffer_bounds():
    hub = TelemetryHub(max_points=3)
    for bpm in range(5):
        hub.publish_heart_rate(60 + bpm)
    assert len(hub.snapshot()["series"]["heart_rate"]) == 3
    assert hub.snapshot()["counts"]["heart_rate"] == 5


async def test_hub_fanout_to_subscribers():
    hub = TelemetryHub()
    hub.attach_loop(asyncio.get_running_loop())
    queue = hub.subscribe()
    try:
        hub.publish_spo2(97)
        record = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert record["kind"] == "spo2"
        assert record["spo2"] == 97
        assert hub.subscriber_count == 1
    finally:
        hub.unsubscribe(queue)
    assert hub.subscriber_count == 0
