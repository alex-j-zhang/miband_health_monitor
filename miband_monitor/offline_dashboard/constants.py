"""Shared constants for the offline historical dashboard.

Stage order/labels and activity band codes define the payload contract consumed
by the bundled JavaScript; heart-rate and SpO2 bounds come from
:mod:`miband_monitor.config.health`, and the sleep SpO2 capture filename from
:mod:`miband_monitor.storage.schema`, so the dashboard cannot disagree with the
rest of the project.
"""

from __future__ import annotations

from miband_monitor.config.health import (
    HR_MAX,
    HR_MIN,
    HR_UNAVAILABLE,
    SPO2_MAX,
    SPO2_MIN,
)
from miband_monitor.sleep import STAGE_AWAKE, STAGE_DEEP, STAGE_LIGHT, STAGE_UNKNOWN
from miband_monitor.storage.schema import JSONL_FILENAMES

#: Stage display order: deepest first, then light, awake, and the no-heart-rate
#: sentinel last (used for labels/lane ordering, never for lane painting).
STAGE_ORDER = (STAGE_DEEP, STAGE_LIGHT, STAGE_AWAKE, STAGE_UNKNOWN)
STAGE_LABELS = {
    STAGE_DEEP: "深睡",
    STAGE_LIGHT: "浅睡",
    STAGE_AWAKE: "清醒",
    STAGE_UNKNOWN: "无心率",
}

#: Activity band codes shared with the renderer.
BAND_REST, BAND_ACTIVE, BAND_SLEEP, BAND_NONWEAR = 0, 1, 2, 3

#: Local-time formats used in payload labels and report headings.
LOCAL_DATETIME_FORMAT = "%m-%d %H:%M"
#: Timestamp format of the payload's generated_at field (includes the year).
GENERATED_AT_FORMAT = "%Y-%m-%d %H:%M"
CLOCK_FORMAT = "%H:%M"
DATE_FORMAT = "%m-%d"
DAY_FORMAT = "%Y-%m-%d"

#: Capture filename written by ``--sync-sleep-spo2`` alongside ``mi_band.db``.
SPO2_SLEEP_JSONL = JSONL_FILENAMES["spo2_sleep"]

#: Payload provenance values (data, not display copy).
SOURCE_SQLITE = "sqlite"
SOURCE_JSONL = "jsonl"
SOURCE_NONE = "none"
HR_SOURCE_HISTORY = "history"
HR_SOURCE_LIVE = "live"
HR_SOURCE_NONE = "none"
