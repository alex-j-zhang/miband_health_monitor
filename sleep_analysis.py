"""Derive sleep sessions and per-minute sleep stages from band activity samples.

Why this module exists
----------------------
The 8-byte ACTIVITY record has three trailing bytes that look like they carry
sleep information (Gadgetbridge calls them ``sleep`` / ``deepSleep`` /
``remSleep``), but on a real Mi Band 6 they are **not** per-minute durations:
they never grow monotonically, they change smoothly by 1-3 per minute, and they
are only populated in some ``kind`` states (see
``docs/sleep_fields_investigation.md`` for the measurements). Treating them as
stages would be inventing a mapping rather than decoding one.

What *is* reliable, and used here -- all of it checked against Zepp Life's own
numbers for 2020-01-06 (score 73, 8 h 40 m asleep, 44 min deep = 8 %, 20 min
awake):

* ``steps`` -- the sleep/wake gate. A minute with at least one step is awake.
  Handing the same night to the app gives 20 awake minutes; the capture gives
  17, and in-bed 480 vs 481. A ``kind``-based gate does far worse: it misses
  the first 53 minutes and the last 71 (see ``docs/sleep_fields_investigation.md``
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
invention, not a measurement (see ``docs/sleep_fields_investigation.md`` §8).

Everything produced here is a **derived** value: it is clearly labelled and kept
apart from the band's raw fields. Wrist-only staging cannot match EEG, so the
output is meant for structure and trends (onset/wake times, total duration, deep
share, awakenings), not per-minute clinical judgement.
"""

from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

MINUTE = 60.0

STAGE_AWAKE = "awake"
STAGE_LIGHT = "light"
STAGE_DEEP = "deep"
#: Only used when a session carries no usable heart rate at all -- reporting it as
#: "light sleep" would silently invent a staged night.
STAGE_UNKNOWN = "unknown"
STAGES: tuple[str, ...] = (STAGE_AWAKE, STAGE_LIGHT, STAGE_DEEP, STAGE_UNKNOWN)

#: ``kind & 0xF0 == 0xF0`` is the band's sleep family (240/243/249/250 on real
#: hardware). It never matches daytime samples.
SLEEP_KIND_MASK = 0xF0
SLEEP_KIND_VALUE = 0xF0

#: The low nibble of ``kind`` is the activity code. Gadgetbridge reads the same
#: byte as ``rawKind & 0x0f`` (``MiBand2SampleProvider.postprocess``) and names
#: the codes in ``HuamiConst``: 0 no change, 1 activity, 3 *not worn*,
#: 9 light sleep, 10 ignore, 11 deep sleep, 12 wake up.
KIND_CODE_MASK = 0x0F
KIND_NO_CHANGE = 0x0
KIND_ACTIVITY = 0x01
KIND_NONWEAR = 0x03
KIND_LIGHT_SLEEP = 0x09
KIND_IGNORE = 0x0A
KIND_DEEP_SLEEP = 0x0B
KIND_WAKE_UP = 0x0C

#: ``intensity`` at or above this counts as body movement for the activity strip
#: (daytime walking sits at 50-90, sleep at 0-12).
MOVEMENT_INTENSITY = 20

#: A minute with at least one step is awake. Steps are the one movement signal
#: Zepp Life's numbers reproduce exactly: on 2020-01-06 it reports 20 awake
#: minutes and the capture holds 17, while an ``intensity >= 20`` rule would
#: claim 72 and lose 55 minutes of real sleep.
AWAKE_STEPS = 1

#: Quiet gaps up to this long are bridged inside one sleep session (a mid-night
#: wake-up of a few minutes should not split the night in two).
SESSION_GAP_MINUTES = 20
#: Sessions shorter than this are discarded as naps/artefacts.
MIN_SESSION_MINUTES = 20
#: How far a session may grow beyond the band's own "sleep" state, in minutes.
#: 60 covers the tail of a real night (the band stops reporting sleep at 04:55
#: while the app keeps counting until ~06:06) without reaching into the day.
ANCHOR_MINUTES = 60
#: A session needs this many minutes where the band itself said sleep, so that a
#: lone anchor minute (putting the band back on, then walking) cannot grow into a
#: hour-long "night". Real nights have hundreds.
MIN_ANCHOR_MINUTES = 10

#: Half-width (minutes) of the rolling window used for heart-rate variability.
HR_WINDOW_MINUTES = 7
#: Deep sleep ranks low on both heart rate and its short-term variability.
#:
#: Calibrated against Zepp Life's 2020-01-06 numbers, which is the only external
#: reference this device can offer: the app reports 44 deep minutes (8 %). The
#: previous 0.30/0.55 cut claimed 76 minutes (16 %) for the same night, clearly
#: over-attributing; 0.20/0.45 lands on 50 (11 %). One night is one night -- these
#: are provisional and should be revisited once more Zepp nights are available.
DEEP_HR_RANK = 0.20
DEEP_SD_RANK = 0.45
#: Stage runs shorter than this are merged into their neighbours.
MIN_STAGE_MINUTES = 3

HR_UNAVAILABLE = 255
HR_MIN = 30
HR_MAX = 220


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
    db_path: str | Path, *, table: str = "activity_history", include_scores: bool = False
) -> list[dict]:
    """Read activity samples from the telemetry SQLite database, oldest first.

    With ``include_scores`` the trailing ``sleep`` / ``deep_sleep`` / ``rem_sleep``
    raw bytes are carried along too, for :func:`score_estimates`. They are off by
    default so the sample dict stays the five fields every other caller expects.
    """
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {path}")

    columns = "ts, kind, intensity, steps, heart_rate"
    if include_scores:
        columns += ", sleep, deep_sleep, rem_sleep"
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


# ---------------------------------------------------------------------------
# The band's own per-minute stage scores (experimental)
# ---------------------------------------------------------------------------
#: The trailing three bytes are neither durations nor a partition of the
#: minute. They are three *independent* per-minute stage scores on a 0-127
#: scale -- what a classifier writes, not what a counter writes.
#:
#: Evidence, from the 01-06 -> 01-07 night (365 sleep-kind minutes, HR present)
#: and the 01-07 -> 01-08 night (680 minutes, **no heart rate at all**):
#:
#: * ``sleep`` (light) needs no heart rate: it sits at 61 +- 1 for eleven
#:   straight hours on the HR-less night. It is also the only one of the three
#:   that reacts to movement -- a 24 min walk at 22:22 drops it 58 -> 17 -> 13,
#:   and it climbs back to 52 over the next five minutes.
#: * ``deep_sleep`` falls as heart rate rises (HR 45-49 -> 38, 65-69 -> 20,
#:   r = -0.34); ``rem_sleep`` rises instead (45-49 -> 27, 65-69 -> 50,
#:   r = +0.23). Both are flat zero on the HR-less night: no HR, no staging.
#: * ``deep_sleep`` and ``rem_sleep`` are anti-correlated (r = -0.43) -- the
#:   signature of a NREM/REM discriminant rather than of two independent
#:   measurements.
#: * They are *not* a 0-100 probability split: ``corr(light, deep+rem)`` is
#:   +0.20 where a split needs -1, and on the HR-less night a split would
#:   claim 52% awake for a night that was slept through.
#:
#: The one reading that survives all four is "score / 128 = the share of that
#: minute attributed to the stage", so integrating gives expected minutes.
#: That is what :func:`score_estimates` reports. It does **not** feed the
#: default staging path yet: Zepp's own durations for the same night have not
#: been compared, and until they are this stays an encoding hypothesis.
#: ``docs/sleep_fields_investigation.md`` §5 has the numbers to compare.
SCORE_FLAG = 0x80
SCORE_MASK = 0x7F
SCORE_SCALE = 128
SCORE_STAGES = ("light", "deep", "rem")


def score_value(raw: int) -> int:
    """Low 7 bits of a ``deep_sleep``/``rem_sleep`` byte, 0 when unset."""
    raw = int(raw or 0)
    return raw & SCORE_MASK if raw & SCORE_FLAG else 0


def samples_have_scores(samples: Sequence[dict]) -> bool:
    """True when the band wrote a stage score for at least one sample.

    A zero value with the flag set does not count: on the HR-less night the
    band still stamps ``0x80`` and writes zero, which means "scored, nothing to
    report", not "this minute was worth 128".
    """
    return any(
        score_value(item.get("deep_sleep", 0)) or score_value(item.get("rem_sleep", 0))
        for item in samples
    )


def score_estimates(samples: Sequence[dict]) -> dict:
    """Integrate the band's per-minute stage scores into expected minutes.

    Only minutes the band actually scored enter the denominator, so a night
    with patchy heart rate reports its coverage instead of silently shrinking
    the window. ``minutes`` sums ``score / 128`` per stage; ``residual_minutes``
    is what the three scores leave over, which lines up with the movement-based
    awake count (28.6 vs 30 min on the verified night) rather than replacing it.
    """
    result: dict[str, Any] = {
        "available": False,
        "scale": SCORE_SCALE,
        "window_minutes": len(samples),
        "scored_minutes": 0,
        "coverage": 0.0,
        "mean": {stage: None for stage in SCORE_STAGES},
        "share": {stage: None for stage in SCORE_STAGES},
        "minutes": {stage: None for stage in SCORE_STAGES},
        "residual_minutes": None,
    }
    scored = [
        item for item in samples
        if score_value(item.get("deep_sleep", 0)) or score_value(item.get("rem_sleep", 0))
    ]
    if not scored:
        return result

    sums = {
        "light": sum(int(item.get("sleep", 0)) for item in scored),
        "deep": sum(score_value(item.get("deep_sleep", 0)) for item in scored),
        "rem": sum(score_value(item.get("rem_sleep", 0)) for item in scored),
    }
    minutes = {stage: sums[stage] / SCORE_SCALE for stage in SCORE_STAGES}
    result.update(
        available=True,
        scored_minutes=len(scored),
        coverage=round(len(scored) / len(samples), 4),
        mean={stage: round(sums[stage] / len(scored), 1) for stage in SCORE_STAGES},
        share={
            stage: round(sums[stage] / (len(scored) * SCORE_SCALE), 3) for stage in SCORE_STAGES
        },
        minutes={stage: round(value, 1) for stage, value in minutes.items()},
        residual_minutes=round(max(0.0, len(scored) - sum(minutes.values())), 1),
    )
    return result


# ---------------------------------------------------------------------------
# Session detection
# ---------------------------------------------------------------------------
def detect_sessions(
    samples: Sequence[dict],
    *,
    gap_minutes: int = SESSION_GAP_MINUTES,
    min_minutes: int = MIN_SESSION_MINUTES,
    anchor_minutes: int = ANCHOR_MINUTES,
) -> list[list[dict]]:
    """Group samples into sleep sessions.

    Two signals, in two roles:

    * ``kind`` **anchors**: the band's own state machine says "sleep" for part of
      every real night and never during the day, so it decides *where* a night
      is. Runs of it are clustered, allowing ``gap_minutes`` of wake-up between.
    * **stillness extends**: from the first and last anchor minute the session
      grows outward while the adjacent minute has no steps and the wrist is on,
      but never more than ``anchor_minutes`` past the anchor.

    Extending on stillness alone would swallow an entire day spent sitting down
    (a 13-hour "night" on the real capture); anchoring on stillness alone loses
    124 minutes at the two ends of a real night. Together they reproduce the app
    -- see the module docstring.
    """
    anchors = [index for index, sample in enumerate(samples) if is_sleep_kind(sample["kind"])]
    if not anchors:
        return []

    # Cluster the anchor minutes; a wake-up shorter than gap_minutes stays inside.
    clusters: list[list[int]] = [[anchors[0]]]
    for index in anchors[1:]:
        if samples[index]["ts"] - samples[clusters[-1][-1]]["ts"] > gap_minutes * MINUTE:
            clusters.append([index])
        else:
            clusters[-1].append(index)

    def extend(start: int, direction: int, limit_ts: float) -> int:
        """Walk outward from ``start``, bridging wake-ups shorter than the gap.

        A single step minute must not end the night -- the person turns over and
        keeps sleeping, and the app keeps counting. Extension stops at the first
        stretch longer than ``gap_minutes`` with nothing still in it, or once it
        is ``anchor_minutes`` away from the anchor.
        """
        current = start
        while True:
            found = None
            probe = current + direction
            while 0 <= probe < len(samples):
                if abs(samples[probe]["ts"] - samples[current]["ts"]) > gap_minutes * MINUTE:
                    break
                if is_asleep_minute(samples[probe]):
                    found = probe
                    break
                probe += direction
            if found is None:
                return current
            if abs(samples[found]["ts"] - limit_ts) > anchor_minutes * MINUTE:
                return current
            current = found

    sessions: list[list[dict]] = []
    for cluster in clusters:
        first, last = cluster[0], cluster[-1]
        left = extend(first, -1, samples[first]["ts"])
        right = extend(last, +1, samples[last]["ts"])
        session = list(samples[left:right + 1])
        asleep = sum(1 for sample in session if is_asleep_minute(sample))
        if asleep < min_minutes or len(cluster) < MIN_ANCHOR_MINUTES:
            continue
        if sessions and session[0]["ts"] - sessions[-1][-1]["ts"] <= gap_minutes * MINUTE:
            # The extension of the previous cluster reached up to this one: one
            # night, not two (a 2-minute roll-over must not split the report).
            sessions[-1].extend(
                sample for sample in session if sample["ts"] > sessions[-1][-1]["ts"]
            )
        else:
            sessions.append(session)
    return sessions


#: Non-wear runs shorter than this are not worth reporting (a 2-minute bathroom
#: trip with the band left on the nightstand is not "the band was off").
MIN_NONWEAR_MINUTES = 30


def detect_nonwear_spans(
    samples: Sequence[dict],
    *,
    min_minutes: int = MIN_NONWEAR_MINUTES,
) -> list[dict]:
    """Long stretches the band spent off the wrist, oldest first."""
    spans: list[dict] = []
    run: list[dict] = []

    def close() -> None:
        nonlocal run
        if len(run) >= min_minutes:
            spans.append(
                {
                    "start": local_iso(run[0]["ts"]),
                    "end": local_iso(run[-1]["ts"]),
                    "minutes": len(run),
                    "hours": round(len(run) / 60, 1),
                }
            )
        run = []

    for sample in samples:
        if is_nonwear(sample["kind"]):
            run.append(sample)
        elif run:
            close()
    if run:
        close()
    return spans


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
@dataclass
class SleepSession:
    """One derived sleep session."""

    start: float
    end: float
    minutes: int
    stage_minutes: dict[str, int]
    awakenings: int
    longest_sleep_minutes: int
    samples: list[dict] = field(default_factory=list, repr=False)
    stages: list[str] = field(default_factory=list, repr=False)
    band_scores: dict = field(default_factory=dict, repr=False)

    @property
    def slept_minutes(self) -> int:
        """Minutes asleep, defined by movement -- the same accounting Zepp uses.

        Staging quality (``unknown``) does not change this number: a night with
        no heart rate is still a night with a measurable amount of sleep.
        """
        return self.minutes - self.stage_minutes.get(STAGE_AWAKE, 0)

    @property
    def efficiency(self) -> float:
        return round(self.slept_minutes / self.minutes, 3) if self.minutes else 0.0

    def stage_share(self, stage: str) -> float:
        return round(self.stage_minutes.get(stage, 0) / self.slept_minutes, 3) if self.slept_minutes else 0.0

    def as_dict(self) -> dict[str, Any]:
        unknown = self.stage_minutes.get(STAGE_UNKNOWN, 0)
        return {
            "derived": True,
            "start": local_iso(self.start),
            "end": local_iso(self.end),
            "start_ts": self.start,
            "end_ts": self.end,
            "minutes_in_bed": self.minutes,
            "minutes_asleep": self.slept_minutes,
            "efficiency": self.efficiency,
            "stage_minutes": dict(self.stage_minutes),
            "stage_share": {
                stage: self.stage_share(stage) for stage in (STAGE_LIGHT, STAGE_DEEP)
            },
            "heart_rate_available": unknown == 0,
            "awakenings": self.awakenings,
            "longest_sleep_minutes": self.longest_sleep_minutes,
            "band_scores": dict(self.band_scores),
        }

    def hypnogram(self) -> str:
        """One character per minute: ``.`` awake, ``-`` light, ``=`` deep, ``?`` unknown."""
        symbols = {STAGE_AWAKE: ".", STAGE_LIGHT: "-", STAGE_DEEP: "=", STAGE_UNKNOWN: "?"}
        return "".join(symbols[stage] for stage in self.stages)


def _count_awakenings(stages: Sequence[str], min_run: int = 3) -> int:
    count = 0
    index = 0
    while index < len(stages):
        if stages[index] != STAGE_AWAKE:
            index += 1
            continue
        end = index
        while end + 1 < len(stages) and stages[end + 1] == STAGE_AWAKE:
            end += 1
        if end - index + 1 >= min_run:
            count += 1
        index = end + 1
    return count


def _longest_sleep_run(stages: Sequence[str]) -> int:
    best = run = 0
    for stage in stages:
        if stage == STAGE_AWAKE:
            run = 0
        else:
            run += 1
            best = max(best, run)
    return best


def build_session(samples: Sequence[dict]) -> SleepSession:
    stages = classify_session(samples)
    stage_minutes: dict[str, int] = {}
    for stage in stages:
        stage_minutes[stage] = stage_minutes.get(stage, 0) + 1
    return SleepSession(
        start=samples[0]["ts"],
        end=samples[-1]["ts"],
        minutes=len(samples),
        stage_minutes=stage_minutes,
        awakenings=_count_awakenings(stages),
        longest_sleep_minutes=_longest_sleep_run(stages),
        samples=list(samples),
        stages=stages,
        band_scores=score_estimates(samples),
    )


def analyze(samples: Sequence[dict]) -> dict[str, Any]:
    """Run session detection plus staging over a full sample list."""
    sessions = [build_session(session) for session in detect_sessions(samples)]
    nights: dict[str, list[SleepSession]] = {}
    for session in sessions:
        # A night belongs to the date you went to sleep on -- that is how Zepp
        # Life labels the page (a 22:06-05:55 session is "1 月 6 日", not 7).
        nights.setdefault(local_date(session.start), []).append(session)

    totals = {
        "sessions": len(sessions),
        "minutes_in_bed": sum(s.minutes for s in sessions),
        "minutes_asleep": sum(s.slept_minutes for s in sessions),
        "stage_minutes": {
            stage: sum(s.stage_minutes.get(stage, 0) for s in sessions) for stage in STAGES
        },
        "awakenings": sum(s.awakenings for s in sessions),
        "minutes_not_worn": sum(1 for sample in samples if is_nonwear(sample["kind"])),
    }
    return {
        "derived": True,
        "generated_at": local_iso(datetime.now().timestamp()),
        "totals": totals,
        "sessions": [s.as_dict() for s in sessions],
        "not_worn": detect_nonwear_spans(samples),
        "nights": {
            date: [s.as_dict() for s in items] for date, items in sorted(nights.items())
        },
        "_sessions": sessions,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _format_report(result: dict[str, Any]) -> str:
    lines = []
    totals = result["totals"]
    not_worn = result.get("not_worn") or []
    if not totals["sessions"]:
        if not_worn:
            spans = ", ".join(
                f"{span['start'][:16].replace('T', ' ')} → "
                f"{span['end'][:16].replace('T', ' ')} ({span['hours']} h)"
                for span in not_worn
            )
            return f"no sleep session detected; band not worn: {spans}"
        return "no sleep session detected"
    lines.append("Sleep analysis (derived from band activity samples)")
    lines.append(
        f"  sessions: {totals['sessions']}  in bed: {totals['minutes_in_bed']} min  "
        f"asleep: {totals['minutes_asleep']} min"
    )
    if not_worn:
        lines.append("  band not worn:")
        for span in not_worn:
            lines.append(
                f"    {span['start'][:16].replace('T', ' ')} → "
                f"{span['end'][:16].replace('T', ' ')}  {span['minutes']} min"
            )
    for session in result["_sessions"]:
        data = session.as_dict()
        started = data["start"][:16].replace("T", " ")
        ended = data["end"][:16].replace("T", " ")
        lines.append("")
        if not data["heart_rate_available"]:
            lines.append(
                f"  {started} -> {ended}  ({data['minutes_in_bed']} min in bed)"
            )
            lines.append(
                "    no heart rate recorded by the band for this session -- "
                "stages unavailable (wear the band tight and keep HR monitoring on)"
            )
            continue
        lines.append(
            f"  {started} -> {ended}  ({data['minutes_in_bed']} min in bed, "
            f"{data['minutes_asleep']} min asleep, efficiency {data['efficiency']:.0%})"
        )
        share = data["stage_share"]
        lines.append(
            f"    light {data['stage_minutes'].get(STAGE_LIGHT, 0)} min ({share[STAGE_LIGHT]:.0%})"
            f"  deep {data['stage_minutes'].get(STAGE_DEEP, 0)} min ({share[STAGE_DEEP]:.0%})"
            f"  awake {data['stage_minutes'].get(STAGE_AWAKE, 0)} min"
        )
        lines.append(
            f"    awakenings: {data['awakenings']}  longest stretch: {data['longest_sleep_minutes']} min"
        )
        band = data.get("band_scores") or {}
        if band.get("available"):
            minutes = band["minutes"]
            share = band["share"]
            # These are integrals of the raw score bytes, kept as evidence. They
            # are NOT stage minutes: the app reports 0 REM minutes on a night the
            # byte is nonzero for 458/485 minutes, and 87 REM minutes on a night
            # the byte is zero for all 396 (docs §8).
            lines.append(
                f"    raw score bytes ({band['scored_minutes']}/{band['window_minutes']} scored, "
                f"0-{band['scale']}/min, not stage minutes): "
                f"deep {minutes['deep']:.0f}"
                f"  rem {minutes['rem']:.0f}"
                f"  light {minutes['light']:.0f}"
                f"  unaccounted {band['residual_minutes']:.0f}"
            )
        hypnogram = session.hypnogram()
        lines.append(f"    hypnogram ({len(hypnogram)} min):")
        for offset in range(0, len(hypnogram), 60):
            clock = datetime.fromtimestamp(session.start + offset * MINUTE).strftime("%H:%M")
            lines.append(f"      {clock} {hypnogram[offset:offset + 60]}")
    lines.append("")
    lines.append("  legend: '.' awake  '-' light  '=' deep  '~' rem   (derived, not band-reported)")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Derive sleep sessions and stages from band activity samples."
    )
    parser.add_argument("--db", required=True, help="path to mi_band.db (SQLite)")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument(
        "--night",
        metavar="YYYY-MM-DD",
        help="only report sessions that ended on this local date",
    )
    args = parser.parse_args(argv)

    try:
        samples = load_samples(args.db, include_scores=True)
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f"[-] {exc}", file=sys.stderr)
        return 1

    if args.night:
        sessions = detect_sessions(samples)
        samples = [
            sample
            for session in sessions
            if local_date(session[-1]["ts"]) == args.night
            for sample in session
        ]
        # Re-run detection on the filtered samples so the session list matches.
        if not samples:
            print(f"no sleep session ending on {args.night}")
            return 1

    result = analyze(samples)
    if args.json:
        payload = {key: value for key, value in result.items() if key != "_sessions"}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_format_report(result))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
