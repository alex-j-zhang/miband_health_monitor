"""Per-minute sleep staging from heart-rate level and variability.

Awake comes from steps; the remaining minutes are split into deep and light using
the night's *own* heart-rate distribution (a rank, not a fixed value) and a
rolling standard deviation. With no usable heart rate the asleep minutes stay
``unknown`` -- inventing "light sleep" would silently claim a staged night.
"""

from __future__ import annotations

import bisect
import statistics
from typing import Optional, Sequence

from .constants import (
    DEEP_HR_RANK,
    DEEP_SD_RANK,
    HR_WINDOW_MINUTES,
    MIN_STAGE_MINUTES,
    STAGE_AWAKE,
    STAGE_DEEP,
    STAGE_LIGHT,
    STAGE_UNKNOWN,
)
from .samples import is_awake, usable_heart_rate


def _rank_within(values: Sequence[float]) -> list[float]:
    """Percentile rank (0..1) of every value inside its own sequence.

    Ties share the mid-rank: heart rate is an integer and a resting night reuses
    the same few values over and over, so taking the leftmost position would
    bias every tie downwards and shrink the deep tail for no real reason.
    """
    count = len(values)
    if count <= 1:
        return [0.5] * count
    ordered = sorted(values)
    span = count - 1
    ranks: list[float] = []
    for value in values:
        low = bisect.bisect_left(ordered, value)
        high = bisect.bisect_right(ordered, value)
        ranks.append(((low + high - 1) / 2) / span)
    return ranks


def _rolling_sd(readings: Sequence[Optional[int]], window: int) -> list[Optional[float]]:
    """Standard deviation of the heart rate in a +/- ``window`` minute span.

    Deep sleep is quiet in both level and stability, while lighter sleep keeps
    drifting, so variability is the second signal (besides the level itself) used
    to split the two. On the real night sample it climbs 2.6 -> 3.2 -> 3.6 SD
    from the first third to the last, which is the opposite of what a
    "stable = deep" rule would predict late in the night -- one more reason the
    deep cut-off is a rank rather than a fixed value.
    """
    result: list[Optional[float]] = []
    for index in range(len(readings)):
        chunk = [v for v in readings[max(0, index - window): index + window + 1] if v is not None]
        result.append(statistics.pstdev(chunk) if len(chunk) >= 4 else None)
    return result


def classify_session(session: Sequence[dict]) -> list[str]:
    """Assign a stage to every minute of one sleep session.

    Awake comes from steps, deep from the night's own heart-rate distribution.
    With no usable heart rate the asleep minutes stay ``unknown`` -- the awake
    ones are still reported as awake, since that verdict needs no heart rate.
    """
    awake = [STAGE_AWAKE if is_awake(sample) else None for sample in session]
    readings = [usable_heart_rate(sample) for sample in session]
    if not any(value is not None for value in readings):
        return [stage or STAGE_UNKNOWN for stage in awake]

    # Forward-fill gaps so a missing minute does not read as a stage change.
    filled: list[int] = []
    last: Optional[int] = None
    for value in readings:
        if value is not None:
            last = value
        filled.append(last if last is not None else readings[0] or 0)
    # Leading gap (no reading yet) borrows the first known value.
    first_known = next(value for value in readings if value is not None)
    filled = [value if value else first_known for value in filled]

    hr_rank = _rank_within(filled)
    sd_raw = _rolling_sd(readings, HR_WINDOW_MINUTES)
    known_sd = [value for value in sd_raw if value is not None]
    if known_sd:
        fallback = statistics.median(known_sd)
        sd_rank = _rank_within([value if value is not None else fallback for value in sd_raw])
    else:
        sd_rank = [0.5] * len(session)

    stages: list[str] = []
    for index in range(len(session)):
        if awake[index]:
            stages.append(STAGE_AWAKE)
        elif hr_rank[index] <= DEEP_HR_RANK and sd_rank[index] <= DEEP_SD_RANK:
            stages.append(STAGE_DEEP)
        else:
            stages.append(STAGE_LIGHT)

    return _smooth(stages, session, MIN_STAGE_MINUTES)


def _smooth(stages: list[str], session: Sequence[dict], min_run: int) -> list[str]:
    """Merge deep runs that are too short to be meaningful into their neighbours.

    Awake is never smoothed away: a 2-minute awakening is real information.
    """
    if min_run <= 1 or len(stages) < min_run:
        return stages

    result = list(stages)
    index = 0
    while index < len(result):
        stage = result[index]
        end = index
        while end + 1 < len(result) and result[end + 1] == stage:
            end += 1
        run = end - index + 1
        if stage == STAGE_DEEP and run < min_run:
            neighbours = []
            if index > 0:
                neighbours.append(result[index - 1])
            if end + 1 < len(result):
                neighbours.append(result[end + 1])
            neighbours = [n for n in neighbours if n not in (STAGE_DEEP, STAGE_AWAKE)]
            replacement = neighbours[0] if neighbours else (STAGE_LIGHT if stage != STAGE_LIGHT else stage)
            for offset in range(index, end + 1):
                result[offset] = replacement
        index = end + 1
    return result
