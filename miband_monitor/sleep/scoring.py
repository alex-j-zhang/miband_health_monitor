"""The band's own per-minute stage scores (experimental, evidence only).

The trailing three ACTIVITY bytes are neither durations nor a partition of the
minute: they are three *independent* per-minute stage scores on a 0-127 scale --
what a classifier writes, not what a counter writes. This module keeps the raw
integration as evidence and deliberately does not feed the default staging path.
"""

from __future__ import annotations

from typing import Any, Sequence

# ---------------------------------------------------------------------------
# Score byte decoding
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
#: ``docs/investigations/sleep-fields.md`` §5 has the numbers to compare.
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
