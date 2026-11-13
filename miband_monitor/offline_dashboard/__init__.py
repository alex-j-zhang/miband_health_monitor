"""Render a self-contained historical health dashboard from the telemetry DB.

The live dashboard (``miband_monitor.dashboard``) streams realtime samples over
SSE. This one is the offline counterpart: it reads everything that was synced
into SQLite -- band-side activity history, SpO2 records and the per-minute heart
rate -- and bakes it into a single HTML file with no external assets, so it can
be opened locally or shared as-is.

Charts are hand-rolled on a canvas, matching the project's "no third-party
dependency" rule and the visual language of the live dashboard.

Module layout
-------------
- :mod:`.constants`: stage order/labels, activity band codes, health bounds
- :mod:`.strings`: exact KPI labels/hints and other user-facing copy
- :mod:`.sources`: read-only SQLite/JSONL data sources
- :mod:`.packaging`: heart-rate, activity-band and step payloads
- :mod:`.kpis`: the KPI cards
- :mod:`.svg`: inline hypnogram SVG rendering
- :mod:`.collect`: gathers the payload
- :mod:`.render`: composes the single-file HTML from the bundled assets
- :mod:`.cli`: ``build()`` and the command-line entry point

The package re-exports the historical ``history_dashboard`` API unchanged.
"""

from __future__ import annotations

from .cli import build, main
from .collect import collect
from .constants import (
    BAND_ACTIVE,
    BAND_NONWEAR,
    BAND_REST,
    BAND_SLEEP,
    CLOCK_FORMAT,
    DATE_FORMAT,
    DAY_FORMAT,
    GENERATED_AT_FORMAT,
    HR_MAX,
    HR_MIN,
    HR_SOURCE_HISTORY,
    HR_SOURCE_LIVE,
    HR_SOURCE_NONE,
    HR_UNAVAILABLE,
    LOCAL_DATETIME_FORMAT,
    SOURCE_JSONL,
    SOURCE_NONE,
    SOURCE_SQLITE,
    SPO2_MAX,
    SPO2_MIN,
    SPO2_SLEEP_JSONL,
    STAGE_AWAKE,
    STAGE_DEEP,
    STAGE_LABELS,
    STAGE_LIGHT,
    STAGE_ORDER,
    STAGE_UNKNOWN,
)
from .kpis import _kpis
from .packaging import (
    _package_activity,
    _package_heart_rate,
    _package_steps,
    _valid_hr,
)
from .render import _TEMPLATE, render
from .sources import (
    _byte_group,
    _load_activity,
    _load_heart_rate,
    _load_spo2,
    _load_spo2_sleep,
    _local,
    _table_exists,
)
from .svg import (
    STAGE_LANES,
    _stage_style,
    flat_in_bed_svg,
    hypnogram_svg,
)

__all__ = [
    "STAGE_ORDER",
    "STAGE_LABELS",
    "STAGE_AWAKE",
    "STAGE_LIGHT",
    "STAGE_DEEP",
    "STAGE_UNKNOWN",
    "HR_UNAVAILABLE",
    "HR_MIN",
    "HR_MAX",
    "SPO2_MIN",
    "SPO2_MAX",
    "BAND_REST",
    "BAND_ACTIVE",
    "BAND_SLEEP",
    "BAND_NONWEAR",
    "SPO2_SLEEP_JSONL",
    "LOCAL_DATETIME_FORMAT",
    "GENERATED_AT_FORMAT",
    "CLOCK_FORMAT",
    "DATE_FORMAT",
    "DAY_FORMAT",
    "SOURCE_SQLITE",
    "SOURCE_JSONL",
    "SOURCE_NONE",
    "HR_SOURCE_HISTORY",
    "HR_SOURCE_LIVE",
    "HR_SOURCE_NONE",
    "_local",
    "_byte_group",
    "_table_exists",
    "_load_activity",
    "_load_heart_rate",
    "_load_spo2",
    "_load_spo2_sleep",
    "collect",
    "_valid_hr",
    "_package_heart_rate",
    "_package_activity",
    "_package_steps",
    "_kpis",
    "_stage_style",
    "STAGE_LANES",
    "hypnogram_svg",
    "flat_in_bed_svg",
    "_TEMPLATE",
    "render",
    "build",
    "main",
]
