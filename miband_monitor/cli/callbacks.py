"""Wiring between the BLE/simulated band and the storage/dashboard sinks.

The callbacks are deliberately tiny: they forward one parsed sample to the
TelemetryStore and, when the live dashboard is enabled, to the TelemetryHub.
Keeping this glue in one module makes the main entry point readable and lets the
exact record shapes stay close to their consumers.
"""

from __future__ import annotations

from typing import Optional

from ..ble.client import StreamCallbacks
from ..dashboard import TelemetryHub
from ..storage import TelemetryStore


def build_callbacks(
    store: TelemetryStore,
    hub: Optional[TelemetryHub] = None,
) -> StreamCallbacks:
    """Return the StreamCallbacks used by the live monitor/demo runners."""

    def _on_heart_rate(bpm: int) -> None:
        store.record_heart_rate(bpm)
        if hub is not None:
            hub.publish_heart_rate(bpm)

    def _on_steps(info: dict) -> None:
        store.record_steps(info)
        if hub is not None:
            hub.publish_steps(info)

    def _on_imu(info: dict) -> None:
        store.record_imu(info)
        if hub is not None:
            hub.publish_imu(info)

    def _on_history_spo2(sample: dict) -> None:
        store.record_spo2(
            sample["spo2"],
            auto_measure=bool(sample.get("auto_measure")),
            timestamp=sample.get("ts"),
        )
        if hub is not None:
            hub.publish_spo2(
                sample["spo2"],
                status=1 if sample.get("auto_measure") else 0,
                timestamp=sample.get("ts"),
            )

    def _on_history_activity(sample: dict) -> None:
        store.record_activity_history(sample, timestamp=sample.get("ts"))

    def _on_history_spo2_sleep(sample: dict) -> None:
        store.record_spo2_sleep(sample)

    return StreamCallbacks(
        heart_rate=_on_heart_rate,
        steps=_on_steps,
        imu=_on_imu,
        history_spo2=_on_history_spo2,
        history_activity=_on_history_activity,
        history_spo2_sleep=_on_history_spo2_sleep,
    )
