"""Tests for the derived sleep staging module."""

from datetime import datetime, timedelta

import pytest

from sleep_analysis import (
    KIND_NONWEAR,
    MIN_SESSION_MINUTES,
    SCORE_FLAG,
    STAGE_AWAKE,
    STAGE_DEEP,
    STAGE_LIGHT,
    STAGE_UNKNOWN,
    analyze,
    build_session,
    classify_session,
    detect_sessions,
    is_asleep_minute,
    is_awake,
    is_nonwear,
    is_sleep_kind,
    kind_code,
    load_samples,
    main,
    samples_have_scores,
    score_estimates,
    score_value,
    usable_heart_rate,
)
from storage import TelemetryStore

BASE = datetime(2026, 11, 6, 22, 0).timestamp()


def sample(minute: int, *, kind: int = 240, hr: int = 52, steps: int = 0, intensity: int = 0) -> dict:
    return {
        "ts": BASE + minute * 60,
        "kind": kind,
        "intensity": intensity,
        "steps": steps,
        "heart_rate": hr,
    }


def series(count: int, *, start: int = 0, **kwargs) -> list[dict]:
    return [sample(start + i, **kwargs) for i in range(count)]


# ---------------------------------------------------------------------------
# kind interpretation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", [240, 249, 250])
def test_sleep_kind_family(kind):
    assert is_sleep_kind(kind)


@pytest.mark.parametrize("kind", [1, 16, 17, 80, 96, 112, 129, 144, 224])
def test_non_sleep_kinds(kind):
    """The 0xF0 mask must not swallow daytime states or the 0x70 sleep-onset code."""
    assert not is_sleep_kind(kind)


def test_not_worn_masks_to_the_same_family_as_sleep():
    """0xF3 sits in the 0xF0 family but is *not worn*, not sleep.

    Gadgetbridge decodes ``rawKind & 0x0f`` and calls 3 ``TYPE_NONWEAR``; the
    hardware agrees on its own: all 679 non-wear minutes of the real capture
    carry no heart rate at all, while every other kind carries a reading in
    ~100% of its minutes.
    """
    assert kind_code(243) == KIND_NONWEAR
    assert is_nonwear(243)
    assert not is_sleep_kind(243)
    assert not is_nonwear(240)
    assert not is_nonwear(249)


@pytest.mark.parametrize(
    "kind, code",
    [(240, 0), (241, 1), (243, 3), (249, 9), (250, 10), (155, 11), (124, 12), (1, 1)],
)
def test_kind_code_is_the_low_nibble(kind, code):
    assert kind_code(kind) == code


# ---------------------------------------------------------------------------
# session detection
# ---------------------------------------------------------------------------
def test_session_bridges_short_wake_up():
    samples = series(30) + [sample(30 + i, kind=224, hr=70, steps=8, intensity=40) for i in range(5)] + series(
        30, start=35
    )
    sessions = detect_sessions(samples)
    assert len(sessions) == 1
    assert len(sessions[0]) == 65


def test_session_splits_when_the_person_gets_up_and_walks():
    walking = [sample(30 + i, kind=1, hr=90, steps=40, intensity=90) for i in range(45)]
    sessions = detect_sessions(series(30) + walking + series(30, start=75))
    assert len(sessions) == 2


def test_a_motionless_stretch_does_not_split_the_night():
    """45 quiet minutes are still sleep -- only movement ends a night.

    A ``kind``-based split used to break the night here, which is how the old
    model lost the first 53 minutes of the real night A.
    """
    samples = series(30) + series(45, start=30, kind=80, hr=70) + series(30, start=75)
    sessions = detect_sessions(samples)
    assert len(sessions) == 1
    assert len(sessions[0]) == 105


def test_short_session_is_discarded():
    samples = series(MIN_SESSION_MINUTES - 5)
    assert detect_sessions(samples) == []


def test_daytime_quiet_is_not_sleep():
    """Motionless daytime samples (kind 0x50/0x60) must never start a session."""
    assert detect_sessions(series(120, kind=80, hr=70)) == []


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
# database + CLI
# ---------------------------------------------------------------------------
def _seed_database(tmp_path, samples):
    store = TelemetryStore(tmp_path, backend="sqlite")
    try:
        for item in samples:
            store.record_activity_history(
                {
                    "kind": item["kind"],
                    "intensity": item["intensity"],
                    "steps": item["steps"],
                    "heart_rate": item["heart_rate"],
                    "unknown1": 5,
                    "sleep": item.get("sleep", 60),
                    "deep_sleep": item.get("deep_sleep", 128),
                    "rem_sleep": item.get("rem_sleep", 128),
                },
                timestamp=item["ts"],
            )
    finally:
        store.close()
    return tmp_path / "mi_band.db"


def test_load_samples_roundtrip(tmp_path):
    db = _seed_database(tmp_path, series(90, hr=50))
    samples = load_samples(db)
    assert len(samples) == 90
    assert samples[0]["kind"] == 240
    assert all(set(item) == {"ts", "kind", "intensity", "steps", "heart_rate"} for item in samples)


def test_cli_json_output(tmp_path, capsys):
    db = _seed_database(tmp_path, series(90, hr=50))
    assert main(["--db", str(db), "--json"]) == 0
    payload = capsys.readouterr().out
    assert '"derived": true' in payload
    assert '"sessions": 1' in payload


def test_cli_unknown_night(tmp_path, capsys):
    db = _seed_database(tmp_path, series(90, hr=50))
    assert main(["--db", str(db), "--night", "1999-01-01"]) == 1
    assert "no sleep session" in capsys.readouterr().out


def test_cli_text_report_has_hypnogram(tmp_path, capsys):
    db = _seed_database(tmp_path, series(90, hr=50))
    assert main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "hypnogram" in out
    assert "derived, not band-reported" in out
    assert STAGE_LIGHT in out


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


def test_cli_report_shows_band_scores(tmp_path, capsys):
    db = _seed_database(tmp_path, [scored(i, hr=50) for i in range(90)])
    assert main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "raw score bytes" in out
    assert "not stage minutes" in out
    assert "unaccounted" in out


def test_cli_report_omits_band_scores_when_absent(tmp_path, capsys):
    db = _seed_database(tmp_path, series(90, hr=50))
    assert main(["--db", str(db)]) == 0
    assert "raw score bytes" not in capsys.readouterr().out


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


def test_cli_says_not_worn_instead_of_inventing_a_night(tmp_path, capsys):
    db = _seed_database(tmp_path, series(240, kind=243, hr=0))
    assert main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "no sleep session detected" in out
    assert "band not worn" in out


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


def test_session_extends_at_most_one_hour_past_the_last_anchor():
    """The band stops reporting sleep before the person gets up; the app does not."""
    session = series(60) + series(180, start=60, kind=80, hr=60)  # 3 h of stillness
    sessions = detect_sessions(session)
    assert len(sessions) == 1
    # 60 anchor minutes + 60 minutes of allowed extension, not 240.
    assert len(sessions[0]) == 120


def test_a_lone_anchor_minute_cannot_create_a_night():
    """Putting the band back on and walking must not paint an hour of sleep."""
    samples = [sample(i, kind=80, hr=70, steps=30, intensity=80) for i in range(30)]
    samples += [sample(30, kind=240, hr=60)]
    samples += [sample(31 + i, kind=80, hr=70, steps=0) for i in range(60)]
    assert detect_sessions(samples) == []


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
