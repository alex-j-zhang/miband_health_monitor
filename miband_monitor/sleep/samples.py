"""Activity-sample helpers and SQLite loading for sleep analysis.

Everything here interprets one stored ACTIVITY record (``ts``/``kind``/
``intensity``/``steps``/``heart_rate`` plus the optional trailing score bytes) or
loads those records from the telemetry database. Table and column names come
from :mod:`miband_monitor.storage.schema`, so the analysis cannot drift from the
writer that fills the database.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from miband_monitor.storage.schema import ACTIVITY_HISTORY_COLUMNS, ACTIVITY_HISTORY_TABLE

from .constants import (
    AWAKE_STEPS,
    HR_MAX,
    HR_MIN,
    HR_UNAVAILABLE,
    KIND_CODE_MASK,
    KIND_NONWEAR,
    MOVEMENT_INTENSITY,
    SLEEP_KIND_MASK,
    SLEEP_KIND_VALUE,
)

#: Columns every sample dict carries, in SELECT order.
_SAMPLE_COLUMNS = tuple(
    name
    for name in ACTIVITY_HISTORY_COLUMNS
    if name in ("ts", "kind", "intensity", "steps", "heart_rate")
)
#: Optional trailing score bytes read only when the caller asks for them.
_SCORE_COLUMNS = tuple(
    name for name in ACTIVITY_HISTORY_COLUMNS if name in ("sleep", "deep_sleep", "rem_sleep")
)


# ---------------------------------------------------------------------------
# Sample helpers
# ---------------------------------------------------------------------------
def kind_code(kind: int) -> int:
    """The low nibble of ``kind``: the band's activity code (see ``KIND_*``)."""
    return int(kind) & KIND_CODE_MASK


def is_nonwear(kind: int) -> bool:
    """True when the band says the strap was not on the wrist.

    Code 3 is ``TYPE_NONWEAR`` in Gadgetbridge's decoding, and the hardware
    agrees independently: on the real capture every one of the 679 non-wear
    minutes has **no heart rate at all** and zero movement, while every other
    kind carries a reading in ~100% of its minutes. An optical sensor that sees
    no skin cannot report a pulse, and a band lying on a table does not move.
    """
    return kind_code(kind) == KIND_NONWEAR


def is_sleep_kind(kind: int) -> bool:
    """True when the band's own state machine reports sleep for this minute.

    The ``0xF0`` family is the sleep state on this firmware, but it also covers
    ``0xF3``, which is *not worn* -- treating it as sleep is what once turned an
    unworn night into "11.3 hours in bed". The low-nibble code is what
    separates the two.

    This is no longer the session gate (see :func:`is_asleep_minute`): the band
    only sets it for part of a real night, so a ``kind`` gate loses the minutes
    around the edges. It stays as an independent "definitely asleep" signal --
    nothing in this family ever shows up during the day.
    """
    kind = int(kind)
    if (kind & SLEEP_KIND_MASK) != SLEEP_KIND_VALUE:
        return False
    return not is_nonwear(kind)


def is_movement(sample: dict) -> bool:
    """True when this minute shows body movement (steps or high intensity)."""
    return int(sample.get("steps", 0)) > 0 or int(sample.get("intensity", 0)) >= MOVEMENT_INTENSITY


def is_awake(sample: dict) -> bool:
    """True when the band counted a step in this minute -- the wake criterion."""
    return int(sample.get("steps", 0)) >= AWAKE_STEPS


def is_asleep_minute(sample: dict) -> bool:
    """True when this minute belongs in a sleep session: still, and worn.

    This is actigraphy, and it is what Zepp Life itself does: the app's numbers
    for 2020-01-06 (520 min asleep, 20 min awake, in bed 21:06-06:06) match a
    steps-based split of the same capture to within a few minutes, while a
    ``kind``-based split loses 124 minutes at the two ends of the night.
    """
    return not is_nonwear(sample.get("kind", 0)) and not is_awake(sample)


def usable_heart_rate(sample: dict) -> Optional[int]:
    """Return the heart rate, or ``None`` when the band had no measurement."""
    value = int(sample.get("heart_rate", HR_UNAVAILABLE))
    if value in (0, HR_UNAVAILABLE) or not HR_MIN <= value <= HR_MAX:
        return None
    return value


def local_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).isoformat(timespec="minutes")


def local_date(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_samples(
    db_path: str | Path, *, table: str = ACTIVITY_HISTORY_TABLE, include_scores: bool = False
) -> list[dict]:
    """Read activity samples from the telemetry SQLite database, oldest first.

    With ``include_scores`` the trailing ``sleep`` / ``deep_sleep`` / ``rem_sleep``
    raw bytes are carried along too, for :func:`score_estimates`. They are off by
    default so the sample dict stays the five fields every other caller expects.
    """
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {path}")

    columns = ", ".join(_SAMPLE_COLUMNS)
    if include_scores:
        columns += ", " + ", ".join(_SCORE_COLUMNS)
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not table_exists:
            return []
        rows = conn.execute(f"SELECT {columns} FROM {table} ORDER BY ts").fetchall()
    finally:
        conn.close()

    samples = []
    for row in rows:
        ts, kind, intensity, steps, hr = row[:5]
        sample = {"ts": float(ts), "kind": int(kind), "intensity": int(intensity),
                  "steps": int(steps), "heart_rate": int(hr)}
        if include_scores:
            sample["sleep"] = int(row[5])
            sample["deep_sleep"] = int(row[6])
            sample["rem_sleep"] = int(row[7])
        samples.append(sample)
    return samples
