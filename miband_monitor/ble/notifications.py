"""Notification callbacks for the live telemetry streams.

Each callback decodes one BLE notification, logs a human-readable line and
forwards the parsed value to the user-supplied consumer. Parsing errors are
caught and logged so a single malformed frame can never tear down the BLE
event loop; non-accelerometer frames on the shared 0x0002 characteristic are
ignored quietly (logged once per frame type) because the optical/status streams
are not part of the IMU data path.
"""

from __future__ import annotations

import logging

from .parsers import parse_heart_rate, parse_raw_sensor_data, parse_realtime_steps

logger = logging.getLogger("MiBand6Client")


class NotificationMixin:
    """Heart-rate, steps and raw-sensor notification handlers."""

    def _on_heart_rate_notification(self, _sender, data: bytearray) -> None:
        """Handle one standard Heart Rate Measurement notification."""
        try:
            bpm = parse_heart_rate(data)
            logger.info("[HR] Current Heart Rate: %d BPM", bpm)
            if self._hr_callback:
                self._hr_callback(bpm)
        except Exception as exc:
            logger.error("Failed to parse heart rate measurement: %s", exc)

    def _on_steps_notification(self, _sender, data: bytearray) -> None:
        """Handle one real-time steps notification from characteristic 0x0007."""
        try:
            steps_info = parse_realtime_steps(data)
            logger.info(
                "[STEPS] Steps: %d | Distance: %d m | Calories: %d kcal",
                steps_info["steps"],
                steps_info["distance_meters"],
                steps_info["calories"],
            )
            if self._steps_callback:
                self._steps_callback(steps_info)
        except Exception as exc:
            logger.error("Failed to parse realtime steps notification: %s", exc)

    def _on_sensor_notification(self, _sender, data: bytearray) -> None:
        """Handle one raw-sensor notification from characteristic 0x0002."""
        try:
            sensor_info = parse_raw_sensor_data(data)
            if not sensor_info["samples"]:
                # Non-accelerometer frame sharing characteristic 0x0002
                # (optical/status); ignore quietly instead of logging an error.
                if sensor_info["type"] not in self._ignored_sensor_types:
                    self._ignored_sensor_types.add(sensor_info["type"])
                    logger.debug(
                        "Ignoring non-accelerometer raw-sensor frames of type 0x%02x "
                        "(length %d)",
                        sensor_info["type"],
                        len(data),
                    )
                return
            first = sensor_info["samples"][0]
            logger.debug(
                "[IMU] Seq %d, %d samples, accel: (%.2fg, %.2fg, %.2fg)",
                sensor_info["sequence"],
                sensor_info["sample_count"],
                first["ax_g"],
                first["ay_g"],
                first["az_g"],
            )
            if self._sensor_callback:
                self._sensor_callback(sensor_info)
        except Exception as exc:
            logger.error("Failed to parse raw sensor notification: %s", exc)
