"""Session-level reporting: off-wrist nights and capture regressions."""

from __future__ import annotations

from datetime import datetime

import pytest

from sleep_analysis import analyze, load_samples, usable_heart_rate
from tests.sleep.support import sample, series


# ---------------------------------------------------------------------------
# Nights the band spent off the wrist
# ---------------------------------------------------------------------------
def test_a_nonwear_night_is_not_a_sleep_session():
    """0xF3 is the sleep family's "not worn" member, not a sleep stage."""
    samples = series(200, kind=243, hr=0)
    result = analyze(samples)
    assert result["totals"]["sessions"] == 0
    assert result["totals"]["minutes_in_bed"] == 0
    assert result["totals"]["minutes_not_worn"] == 200


def test_nonwear_spans_are_reported_in_hours():
    samples = series(60) + series(120, start=60, kind=243, hr=0)
    result = analyze(samples)
    assert [span["minutes"] for span in result["not_worn"]] == [120]
    assert result["not_worn"][0]["hours"] == 2.0
    # One real session survives next to the off-wrist stretch.
    assert result["totals"]["sessions"] == 1


def test_short_nonwear_patches_are_not_reported():
    samples = series(60) + series(5, start=60, kind=243, hr=0)
    assert analyze(samples)["not_worn"] == []


def test_real_capture_reproduces_the_zepp_night():
    """Regression on a local capture, if one is present (no capture is committed).

    The exact date is deliberately not pinned: it belongs to local, ignored
    data. We compare the overlap of the app's 540-minute window instead:
    in bed ~480, asleep ~460, awake ~20, deep ~44 (8 %).
    """
    from pathlib import Path

    db = Path(__file__).resolve().parents[1] / "data/final-check/mi_band.db"
    if not db.exists():
        pytest.skip("real capture not present")

    result = analyze(load_samples(db))
    assert len(result["sessions"]) == 1
    night = result["sessions"][0]
    assert len(night["start"]) == 16 and night["start"][10] == "T"
    assert 440 <= night["minutes_in_bed"] <= 500
    assert 430 <= night["minutes_asleep"] <= 480
    assert 10 <= night["stage_minutes"]["awake"] <= 25
    assert 30 <= night["stage_minutes"]["deep"] <= 60
    # Zepp reports no REM for this band, so there must be no REM key at all.
    assert "rem" not in night["stage_minutes"]


# ---------------------------------------------------------------------------
# Night B: Zepp shows 23% REM, the band shows nothing at all
# ---------------------------------------------------------------------------
def test_zepp_night_b_cannot_have_come_from_this_band():
    """A night the app reports as 6 h 27 m with 87 min REM is a negative control.

    Zepp's window is 22:44 -> 05:11. The band recorded every minute of it as
    ``0xF3`` with no heart rate, no steps, no intensity, and ``0x80`` in both
    score bytes -- "scored, nothing to report". So whatever produced the app's
    stages for that night, it was not this band's activity history, and the
    bytes cannot be REM minutes either.
    """
    from pathlib import Path

    db = Path(__file__).resolve().parents[1] / "data/final-check/mi_band.db"
    if not db.exists():
        pytest.skip("real capture not present")

    samples = load_samples(db, include_scores=True)
    lo, hi = datetime(2026, 11, 7, 22, 44).timestamp(), datetime(2026, 11, 8, 5, 11).timestamp()
    window = [s for s in samples if lo <= s["ts"] <= hi]
    assert len(window) >= 380
    assert {s["kind"] for s in window} == {243}
    assert not any(s["steps"] or s["intensity"] for s in window)
    assert not any(usable_heart_rate(s) is not None for s in window)
    assert all(s["deep_sleep"] == 0x80 and s["rem_sleep"] == 0x80 for s in window)


def test_the_worn_night_scores_every_sleep_minute():
    """On the night the band *was* worn, the score bytes are lit almost always.

    That is the other half of the inversion: the app reports **0** REM minutes
    for night A while the byte is nonzero on 458 of its 485 minutes.
    """
    from pathlib import Path

    db = Path(__file__).resolve().parents[1] / "data/final-check/mi_band.db"
    if not db.exists():
        pytest.skip("real capture not present")

    samples = load_samples(db, include_scores=True)
    lo, hi = datetime(2026, 11, 6, 22, 6).timestamp(), datetime(2026, 11, 7, 6, 10).timestamp()
    night = [s for s in samples if lo <= s["ts"] <= hi]
    lit = [s for s in night if (s["deep_sleep"] & 0x7F) and (s["rem_sleep"] & 0x7F)]
    assert len(lit) / len(night) > 0.9
    # Nonzero exactly when there is a pulse to compute it from.
    assert all(usable_heart_rate(s) is not None for s in lit)
