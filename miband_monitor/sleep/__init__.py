"""Derive sleep sessions and per-minute sleep stages from band activity samples.

Why this package exists
-----------------------
The 8-byte ACTIVITY record has three trailing bytes that look like they carry
sleep information (Gadgetbridge calls them ``sleep`` / ``deepSleep`` /
``remSleep``), but on a real Mi Band 6 they are **not** per-minute durations:
they never grow monotonically, they change smoothly by 1-3 per minute, and they
are only populated in some ``kind`` states (see
``docs/investigations/sleep-fields.md`` for the measurements). Treating them as
stages would be inventing a mapping rather than decoding one.

What *is* reliable, and used here -- all of it checked against Zepp Life's own
numbers for 2020-01-06 (score 73, 8 h 40 m asleep, 44 min deep = 8 %, 20 min
awake):

* ``steps`` -- the sleep/wake gate. A minute with at least one step is awake.
  Handing the same night to the app gives 20 awake minutes; the capture gives
  17, and in-bed 480 vs 481. A ``kind``-based gate does far worse: it misses
  the first 53 minutes and the last 71 (see ``docs/investigations/sleep-fields.md``
  §7).
* ``kind`` -- the band's own state machine. Its low nibble carries the activity
  code (``3`` = not worn, the only value that vetoes sleep), and ``0xF0``-family
  minutes are daytime-impossible sleep evidence.
* ``heart_rate`` -- autonomic state; deep sleep sits at the low end of the
  night's distribution, with low short-term variability.

There is no REM stage here, deliberately. Zepp Life *does* have a REM row, but
this band cannot produce one: the type that carries real stage spans (``0x48``)
answers with zero records, and the trailing ``rem_sleep`` byte is nonzero on
458/485 minutes of a night the app calls REM-free while sitting at zero for all
396 minutes of a night it calls 23% REM. Emitting REM from that would be an
invention, not a measurement (see ``docs/investigations/sleep-fields.md`` §8).

Everything produced here is a **derived** value: it is clearly labelled and kept
apart from the band's raw fields. Wrist-only staging cannot match EEG, so the
output is meant for structure and trends (onset/wake times, total duration, deep
share, awakenings), not per-minute clinical judgement.

Module layout
-------------
- :mod:`.constants`: stage names, band ``kind`` codes and every threshold
- :mod:`.samples`: sample interpretation and SQLite loading
- :mod:`.scoring`: experimental integration of the band's own score bytes
- :mod:`.detection`: sleep-session and non-wear span detection
- :mod:`.classification`: per-minute deep/light staging
- :mod:`.session`: ``SleepSession``, summary metrics and night grouping
- :mod:`.report`: text report
- :mod:`.cli`: command-line entry point
- :mod:`.calibration`: Zepp Life calibration harness (candidate readings)

The package re-exports the historical ``sleep_analysis`` API unchanged.
"""

from __future__ import annotations

from .classification import (
    _rank_within,
    _rolling_sd,
    _smooth,
    classify_session,
)
from .cli import main
from .constants import (
    ANCHOR_MINUTES,
    AWAKE_STEPS,
    DEEP_HR_RANK,
    DEEP_SD_RANK,
    HR_MAX,
    HR_MIN,
    HR_UNAVAILABLE,
    HR_WINDOW_MINUTES,
    KIND_ACTIVITY,
    KIND_CODE_MASK,
    KIND_DEEP_SLEEP,
    KIND_IGNORE,
    KIND_LIGHT_SLEEP,
    KIND_NO_CHANGE,
    KIND_NONWEAR,
    KIND_WAKE_UP,
    MIN_ANCHOR_MINUTES,
    MIN_NONWEAR_MINUTES,
    MIN_SESSION_MINUTES,
    MIN_STAGE_MINUTES,
    MINUTE,
    MOVEMENT_INTENSITY,
    SESSION_GAP_MINUTES,
    SLEEP_KIND_MASK,
    SLEEP_KIND_VALUE,
    STAGE_AWAKE,
    STAGE_DEEP,
    STAGE_LIGHT,
    STAGE_UNKNOWN,
    STAGES,
)
from .detection import detect_nonwear_spans, detect_sessions
from .report import _format_report
from .samples import (
    is_asleep_minute,
    is_awake,
    is_movement,
    is_nonwear,
    is_sleep_kind,
    kind_code,
    load_samples,
    local_date,
    local_iso,
    usable_heart_rate,
)
from .scoring import (
    SCORE_FLAG,
    SCORE_MASK,
    SCORE_SCALE,
    SCORE_STAGES,
    samples_have_scores,
    score_estimates,
    score_value,
)
from .session import (
    SleepSession,
    _count_awakenings,
    _longest_sleep_run,
    analyze,
    build_session,
)

__all__ = [
    "MINUTE",
    "STAGE_AWAKE",
    "STAGE_LIGHT",
    "STAGE_DEEP",
    "STAGE_UNKNOWN",
    "STAGES",
    "SLEEP_KIND_MASK",
    "SLEEP_KIND_VALUE",
    "KIND_CODE_MASK",
    "KIND_NO_CHANGE",
    "KIND_ACTIVITY",
    "KIND_NONWEAR",
    "KIND_LIGHT_SLEEP",
    "KIND_IGNORE",
    "KIND_DEEP_SLEEP",
    "KIND_WAKE_UP",
    "MOVEMENT_INTENSITY",
    "AWAKE_STEPS",
    "SESSION_GAP_MINUTES",
    "MIN_SESSION_MINUTES",
    "ANCHOR_MINUTES",
    "MIN_ANCHOR_MINUTES",
    "MIN_NONWEAR_MINUTES",
    "HR_WINDOW_MINUTES",
    "DEEP_HR_RANK",
    "DEEP_SD_RANK",
    "MIN_STAGE_MINUTES",
    "HR_UNAVAILABLE",
    "HR_MIN",
    "HR_MAX",
    "kind_code",
    "is_nonwear",
    "is_sleep_kind",
    "is_movement",
    "is_awake",
    "is_asleep_minute",
    "usable_heart_rate",
    "local_iso",
    "local_date",
    "load_samples",
    "SCORE_FLAG",
    "SCORE_MASK",
    "SCORE_SCALE",
    "SCORE_STAGES",
    "score_value",
    "samples_have_scores",
    "score_estimates",
    "detect_sessions",
    "detect_nonwear_spans",
    "classify_session",
    "SleepSession",
    "build_session",
    "analyze",
    "main",
]
