"""Sleep sessions: per-minute staging, summary metrics and night grouping.

A session is one detected night. Staging is delegated to
:mod:`miband_monitor.sleep.classification`; this module only turns the stage list
into the reportable metrics (minutes in bed/asleep, efficiency, awakenings,
longest stretch, band score evidence) and groups sessions by the date the night
started -- the way Zepp Life labels them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from .classification import classify_session
from .constants import STAGE_AWAKE, STAGE_DEEP, STAGE_LIGHT, STAGE_UNKNOWN, STAGES
from .detection import detect_nonwear_spans, detect_sessions
from .samples import is_nonwear, local_date, local_iso
from .scoring import score_estimates


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
