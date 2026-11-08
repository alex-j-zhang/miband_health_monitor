"""Tests for the Zepp calibration harness (``tools/calibrate_sleep.py``).

The harness exists because five rounds of this investigation went wrong by
comparing numbers by eye. What needs pinning is not the printing, but the two
rules that keep it honest: each reading must read the bytes the documented way,
and the tool must refuse to tune anything on a single night.
"""

from datetime import datetime
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import calibrate_sleep as cal  # noqa: E402
from sleep_analysis import SCORE_FLAG, build_session  # noqa: E402

BASE = datetime(2026, 11, 6, 22, 0).timestamp()


def scored(minute: int, *, light: int = 64, deep: int = 32, rem: int = 32) -> dict:
    return {
        "ts": BASE + minute * 60,
        "kind": 240,
        "intensity": 0,
        "steps": 0,
        "heart_rate": 52,
        "sleep": light,
        "deep_sleep": SCORE_FLAG | deep,
        "rem_sleep": SCORE_FLAG | rem,
    }


def test_integral_divides_by_128_not_by_100():
    result = cal.integral(build_session([scored(i) for i in range(4)]))
    assert result == {"deep": 1.0, "rem": 1.0, "light": 2.0}


def test_integral_ignores_bits_without_the_scored_flag():
    """``0x80`` alone means "scored, nothing to report" -- it is not 128."""
    plain = {**scored(0), "deep_sleep": 0x80, "rem_sleep": 0x80}
    assert cal.integral(build_session([plain])) == {"deep": 0.0, "rem": 0.0, "light": 0.5}


def test_gadgetbridge_thresholds_check_rem_before_deep():
    """A minute above both thresholds counts as REM, exactly like the Java code."""
    both = {**scored(0), "deep_sleep": SCORE_FLAG | 45, "rem_sleep": SCORE_FLAG | 60}
    only_deep = {**scored(1), "deep_sleep": SCORE_FLAG | 45, "rem_sleep": SCORE_FLAG | 5}
    neither = {**scored(2), "deep_sleep": SCORE_FLAG | 5, "rem_sleep": SCORE_FLAG | 5}
    result = cal.gb_thresholds(build_session([both, only_deep, neither]))
    assert result == {"deep": 1.0, "rem": 1.0, "light": 1.0}


def test_find_session_uses_the_evening_date_not_the_morning():
    """Zepp labels a 22:06 -> 05:55 night as the date it started."""
    session = build_session([scored(i) for i in range(60)])
    result = {"_sessions": [session]}
    assert cal.find_session(result, "2026-11-06") is session
    assert cal.find_session(result, "2026-11-07") is None


def test_best_deep_cutoff_returns_the_closest_count():
    # deep scores 10, 20, 30, 40: count(v > t) is 4 for t < 10, then 3, 2, 1, 0.
    # Every t in 20..29 yields 2; ties resolve to the lowest, i.e. the loosest
    # cut-off that still gives that count.
    session = build_session([{**scored(i), "deep_sleep": SCORE_FLAG | v}
                             for i, v in enumerate((10, 20, 30, 40))])
    best = cal._best_deep_cutoff(session, target=2)
    assert best["minutes"] == 2
    assert best["cutoff"] == 20


def test_window_bounds_roll_over_midnight():
    """Zepp labels a night by its evening, so the window ends on the next day."""
    low, high = cal._window_bounds({"date": "2026-11-07", "window": ["22:44", "05:11"]})
    assert round((high - low) / 60) == 387  # 6 h 27 m, exactly the app's own total


def test_overlap_counts_only_the_minutes_the_app_covers():
    session = build_session([scored(i) for i in range(60)])  # 22:00 -> 22:59
    night = {"date": "2026-11-06", "window": ["22:30", "23:30"]}
    assert cal._overlap_minutes(night, session) == 30


def test_report_refuses_to_fit_on_a_single_usable_night(capsys):
    """One night can be made to fit any reading -- so the tool refuses to tune."""
    db = Path(__file__).resolve().parents[1] / "data/final-check/mi_band.db"
    nights = Path(__file__).resolve().parents[1] / "data/zepp_nights.json"
    if not db.exists() or not nights.exists():
        pytest.skip("real capture / Zepp numbers not present")

    import json

    assert cal.report(str(db), json.loads(nights.read_text(encoding="utf-8"))) == 0
    out = capsys.readouterr().out
    assert "不做拟合" in out
    # The two unusable nights are named, not silently dropped.
    assert "未佩戴" in out
    assert "库里没有这一夜" in out
    # An off-wrist band cannot have produced the app's page for that night.
    assert "这一页来自别的设备" in out
    # And a night whose Zepp provenance is unrecorded is flagged, not trusted.
    assert "来源设备未记录" in out
    assert "540 分钟" in out  # the app's window, vs the 470 we cover
    # Every night in the file is accounted for, including the ones we never had data for.
    for date in ("2026-11-01", "2026-11-02", "2026-11-03", "2026-11-05"):
        assert date in out
