"""Payload packaging for heart rate, activity bands and per-day steps.

The offline dashboard ships plain JSON to the browser: one compact series per
chart. The rules are unchanged from the historical module -- implausible pulse
readings are dropped (255 is "no measurement", not a heart rate), the band
history series wins over live samples, and "not worn" is kept apart from both
sleep and "no record".
"""

from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any, Optional, Sequence

from miband_monitor.sleep import is_movement, is_nonwear

from .constants import (
    BAND_ACTIVE,
    BAND_NONWEAR,
    BAND_REST,
    BAND_SLEEP,
    DAY_FORMAT,
    HR_MAX,
    HR_MIN,
    HR_SOURCE_HISTORY,
    HR_SOURCE_LIVE,
    HR_SOURCE_NONE,
    HR_UNAVAILABLE,
)


def _valid_hr(value: int) -> Optional[int]:
    if value in (0, HR_UNAVAILABLE) or not HR_MIN <= value <= HR_MAX:
        return None
    return value


def _package_heart_rate(rows: Sequence[tuple[float, int]], activity: Sequence[dict]) -> dict[str, Any]:
    """Prefer the per-minute history series, falling back to live HR samples."""
    points: list[list[Any]] = []
    for item in activity:
        hr = _valid_hr(item["heart_rate"])
        if hr is not None:
            points.append([item["ts"], hr])
    if not points:
        for ts, bpm in rows:
            hr = _valid_hr(bpm)
            if hr is not None:
                points.append([ts, hr])

    values = [bpm for _, bpm in points]
    return {
        "points": points,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "avg": round(statistics.mean(values), 1) if values else None,
        "source": HR_SOURCE_HISTORY if activity and points else (
            HR_SOURCE_LIVE if points else HR_SOURCE_NONE
        ),
    }


def _package_activity(
    activity: Sequence[dict], sleep_minutes: Optional[set[float]] = None
) -> dict[str, Any]:
    """Coarse activity band (rest / active / sleep / not worn) as ``[ts, code]``.

    "Sleep" here means *belongs to a detected session*, not "the band's state
    byte said sleep": a still afternoon would otherwise paint as sleep. The
    timestamps are kept because the band's history is not a gapless minute grid
    -- minutes it did not record are simply absent, so indexing by position
    would silently shift everything after the first hole.
    """
    sleep_minutes = sleep_minutes or set()
    points: list[list[float]] = []
    for item in activity:
        if is_nonwear(item["kind"]):
            # Checked first: both live in the 0xF0 family, and a night the band
            # spent on the nightstand must not paint as hours of sleep.
            code = BAND_NONWEAR
        elif item["ts"] in sleep_minutes:
            code = BAND_SLEEP
        elif is_movement(item):
            code = BAND_ACTIVE
        else:
            code = BAND_REST
        points.append([item["ts"], code])
    return {"points": points}


def _package_steps(activity: Sequence[dict]) -> dict[str, Any]:
    per_day: dict[str, int] = {}
    per_hour_today: list[int] = [0] * 24
    today = datetime.now().strftime(DAY_FORMAT)
    for item in activity:
        moment = datetime.fromtimestamp(item["ts"])
        day = moment.strftime(DAY_FORMAT)
        per_day[day] = per_day.get(day, 0) + int(item["steps"])
        if day == today:
            per_hour_today[moment.hour] += int(item["steps"])
    return {
        "daily": [{"date": day, "steps": steps} for day, steps in sorted(per_day.items())],
        "hourly_today": per_hour_today,
        "today": today,
        "total": sum(per_day.values()),
    }
