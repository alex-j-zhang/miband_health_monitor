"""Heart-rate/movement staging and the band's own score bytes."""

from __future__ import annotations

from datetime import datetime

import pytest

from sleep_analysis import (
    SCORE_FLAG,
    STAGE_AWAKE,
    STAGE_DEEP,
    STAGE_LIGHT,
    STAGE_UNKNOWN,
    analyze,
    build_session,
    classify_session,
    is_awake,
    samples_have_scores,
    score_estimates,
    score_value,
)
from tests.sleep.support import sample, scored, series


# ---------------------------------------------------------------------------
# staging
# ---------------------------------------------------------------------------
def test_staging_puts_the_low_end_of_the_night_in_deep():
    """Deep sleep sits at the bottom of the night's own heart-rate distribution."""
    session = series(30, hr=45) + series(90, start=30, hr=60)
    stages = classify_session(session)

    assert stages[:20] == [STAGE_DEEP] * 20
    assert stages.count(STAGE_DEEP) < 60
    assert STAGE_AWAKE not in stages
    # Zepp Life reports no REM for this band, so no stage may be invented for it.
    assert set(stages) <= {STAGE_DEEP, STAGE_LIGHT}


def test_movement_is_awake():
    session = series(40) + [sample(40, steps=12, intensity=45)] + series(40, start=41)
    stages = classify_session(session)
    assert stages[40] == STAGE_AWAKE


def test_session_without_heart_rate_is_unknown_but_still_timed():
    """No pulse -> no staging; the amount of sleep is still measurable."""
    session = series(59, hr=255) + [sample(59, hr=255, steps=6)]
    stages = classify_session(session)
    assert stages.count(STAGE_UNKNOWN) == 59
    assert stages[-1] == STAGE_AWAKE

    summary = build_session(session).as_dict()
    assert summary["heart_rate_available"] is False
    assert summary["minutes_in_bed"] == 60
    # Steps, not heart rate, decide sleep/wake -- the same accounting Zepp uses.
    assert summary["minutes_asleep"] == 59


def test_stage_share_excludes_unknown():
    session = build_session(series(60, hr=255))
    assert session.stage_share(STAGE_DEEP) == 0.0


def test_analyze_reports_one_session_per_night():
    samples = series(120, hr=50)
    result = analyze(samples)
    assert result["totals"]["sessions"] == 1
    assert result["totals"]["minutes_in_bed"] == 120
    night = samples[-1]["ts"]
    assert datetime.fromtimestamp(night).strftime("%Y-%m-%d") in result["nights"]


# ---------------------------------------------------------------------------
# The band's own per-minute stage scores
# ---------------------------------------------------------------------------
def scored(minute: int, *, light: int = 64, deep: int = 32, rem: int = 32, **kwargs) -> dict:
    """A sample carrying the trailing score bytes the way the band writes them."""
    item = sample(minute, **kwargs)
    item["sleep"] = light
    item["deep_sleep"] = SCORE_FLAG | deep if deep is not None else 0
    item["rem_sleep"] = SCORE_FLAG | rem if rem is not None else 0
    return item


@pytest.mark.parametrize(
    "raw, expected",
    [
        (SCORE_FLAG | 37, 37),
        (SCORE_FLAG, 0),
        (SCORE_FLAG | 0x7F, 0x7F),
        (37, 0),  # without the flag the byte carries no score at all
        (0, 0),
    ],
)
def test_score_value_needs_the_flag(raw, expected):
    assert score_value(raw) == expected


def test_samples_have_scores_ignores_scored_but_empty_minutes():
    assert samples_have_scores([]) is False
    # The HR-less night stamps 0x80 and writes zero: "scored, nothing to report".
    assert samples_have_scores([scored(0, deep=None, rem=None)]) is False
    assert samples_have_scores([scored(0, deep=1, rem=None)]) is True


def test_score_estimates_integrates_scores_into_minutes():
    result = score_estimates([scored(i, light=64, deep=32, rem=32) for i in range(4)])
    assert result["available"] is True
    assert result["scored_minutes"] == 4
    assert result["coverage"] == 1.0
    assert result["mean"] == {"light": 64.0, "deep": 32.0, "rem": 32.0}
    # 64/128 = 0.5 min of light per minute, 32/128 = 0.25 min of deep and rem.
    assert result["minutes"] == {"light": 2.0, "deep": 1.0, "rem": 1.0}
    assert result["share"] == {"light": 0.5, "deep": 0.25, "rem": 0.25}
    assert result["residual_minutes"] == 0.0


def test_score_estimates_reports_coverage_when_scores_are_patchy():
    samples = [scored(0), scored(1), scored(2, deep=None, rem=None), scored(3, deep=None, rem=None)]
    result = score_estimates(samples)
    assert result["available"] is True
    assert (result["scored_minutes"], result["window_minutes"]) == (2, 4)
    assert result["coverage"] == 0.5
    # Means are taken over the scored minutes, not diluted by the unscored ones.
    assert result["mean"]["deep"] == 32.0
    assert result["minutes"]["light"] == 1.0


def test_score_estimates_unavailable_without_scores():
    result = score_estimates(series(10))
    assert result["available"] is False
    assert result["minutes"] == {"light": None, "deep": None, "rem": None}
    assert result["residual_minutes"] is None


def test_session_carries_band_scores():
    data = build_session([scored(i, hr=50) for i in range(60)]).as_dict()
    assert data["band_scores"]["available"] is True
    assert data["band_scores"]["minutes"]["deep"] == 15.0


def test_session_without_scores_reports_them_as_unavailable():
    data = build_session(series(60, hr=50)).as_dict()
    assert data["band_scores"]["available"] is False


# ---------------------------------------------------------------------------
# The Zepp Life calibration (night A: score 73, 8 h 40 m asleep,
# 44 min deep = 8 %, 20 min awake)
# ---------------------------------------------------------------------------
def test_awake_is_counted_by_steps_not_intensity():
    """Rolling over is not an awakening.

    Zepp Life reports 20 awake minutes for the night; counting ``intensity >= 20``
    would claim 72 and throw away 55 minutes of real sleep, while steps>0 gives 17.
    """
    session = series(10) + [sample(10, intensity=60, steps=0)] + series(10, start=11)
    stages = classify_session(session)
    assert STAGE_AWAKE not in stages
    assert is_awake({"steps": 1}) is True
    assert is_awake({"steps": 0, "intensity": 90}) is False
