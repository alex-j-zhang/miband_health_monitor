"""Offline dashboard collect(): payload packaging and robustness."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

import history_dashboard as hd
from history_dashboard import BAND_ACTIVE, BAND_REST, BAND_SLEEP, collect
from sleep_analysis import STAGE_UNKNOWN
from tests.offline_dashboard.support import (
    BASE,
    _capture_rows,
    _insert_sleep_spo2,
    make_db,
    nights,
)


# ---------------------------------------------------------------------------
# collect(): robustness
# ---------------------------------------------------------------------------
def test_collect_on_empty_database_does_not_raise(tmp_path):
    """A freshly created DB has all tables but no rows -- must still render."""
    payload = collect(make_db(tmp_path), days=7)

    assert payload["meta"]["activity_samples"] == 0
    assert payload["meta"]["range"] == [None, None]
    assert payload["sleep"]["sessions"] == []
    assert payload["hr"]["source"] == "none"
    assert payload["hr"]["points"] == []
    assert payload["activity"]["points"] == []
    assert payload["steps"]["total"] == 0
    assert payload["spo2"] == []
    assert payload["kpi"][0] == {"label": "最近一晚睡眠", "value": "--", "unit": "小时", "hint": "暂无数据"}


def test_collect_tolerates_missing_tables(tmp_path):
    """An older DB that predates the activity_history sync must not crash."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE heart_rate (id INTEGER PRIMARY KEY, ts REAL, iso_time TEXT, bpm INTEGER)")
    conn.execute("INSERT INTO heart_rate VALUES (1, ?, '', 71)", (BASE,))
    conn.commit()
    conn.close()

    payload = collect(path, days=7)
    assert payload["meta"]["activity_samples"] == 0
    assert payload["hr"]["source"] == "live"
    assert payload["hr"]["points"] == [[BASE, 71]]


def test_collect_respects_the_day_window(tmp_path):
    stale = [(BASE - timedelta(days=30).total_seconds() + i * 60, 243, 0, 0, 50) for i in range(60)]
    path = make_db(tmp_path, activity=stale)
    assert collect(path, days=7)["meta"]["activity_samples"] == 0
    assert collect(path, days=60)["meta"]["activity_samples"] == 60


# ---------------------------------------------------------------------------
# heart rate packaging
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw", [0, 255, 25, 230])
def test_invalid_heart_rate_is_dropped(raw):
    assert hd._valid_hr(raw) is None


@pytest.mark.parametrize("raw", [30, 71, 220])
def test_plausible_heart_rate_is_kept(raw):
    assert hd._valid_hr(raw) == raw


def test_heart_rate_prefers_the_band_history_series(tmp_path):
    """255 ("no measurement") must never enter the average -- it once read 172."""
    activity = [(BASE + i * 60, 243, 0, 0, hr) for i, hr in enumerate([71, 255, 73, 0, 76])]
    path = make_db(tmp_path, activity=activity, heart_rate=[(BASE, 150)])

    hr = collect(path, days=7)["hr"]
    assert hr["source"] == "history"
    assert [value for _, value in hr["points"]] == [71, 73, 76]
    assert hr["min"] == 71 and hr["max"] == 76
    assert hr["avg"] == pytest.approx(73.3, abs=0.1)


def test_heart_rate_falls_back_to_live_samples(tmp_path):
    path = make_db(tmp_path, heart_rate=[(BASE, 500), (BASE + 60, 68), (BASE + 120, 72)])
    hr = collect(path, days=7)["hr"]
    assert hr["source"] == "live"
    assert hr["points"] == [[BASE + 60, 68], [BASE + 120, 72]]


# ---------------------------------------------------------------------------
# activity / steps packaging
# ---------------------------------------------------------------------------
def test_activity_band_keeps_real_timestamps_across_holes():
    """The history is not a gapless minute grid; codes must stay at their time."""
    activity = [
        {"ts": BASE, "kind": 240, "intensity": 0, "steps": 0, "heart_rate": 0},           # sleep
        {"ts": BASE + 600, "kind": 1, "intensity": 0, "steps": 40, "heart_rate": 0},       # walking
        {"ts": BASE + 660, "kind": 80, "intensity": 0, "steps": 0, "heart_rate": 0},       # motionless
    ]
    # "Sleep" is session membership, not the band's state byte: a still afternoon
    # must not paint as sleep.
    points = hd._package_activity(activity, {BASE})["points"]
    assert points == [[BASE, BAND_SLEEP], [BASE + 600, BAND_ACTIVE], [BASE + 660, BAND_REST]]


def test_activity_band_without_sessions_has_no_sleep_cells():
    activity = [{"ts": BASE, "kind": 240, "intensity": 0, "steps": 0, "heart_rate": 0}]
    assert hd._package_activity(activity)["points"] == [[BASE, BAND_REST]]


def test_steps_are_grouped_by_local_day(tmp_path):
    day_one = datetime(2026, 11, 7, 10, 0).timestamp()
    day_two = datetime(2026, 11, 8, 10, 0).timestamp()
    activity = [(day_one, 16, 0, 500, 0), (day_one + 60, 16, 0, 250, 0), (day_two, 16, 0, 300, 0)]

    steps = collect(make_db(tmp_path, activity=activity), days=7)["steps"]
    assert steps["daily"] == [
        {"date": "2026-11-07", "steps": 750},
        {"date": "2026-11-08", "steps": 300},
    ]
    assert steps["total"] == 1050
    assert len(steps["hourly_today"]) == 24


# ---------------------------------------------------------------------------
# sleep payload
# ---------------------------------------------------------------------------
def test_sleep_session_is_packaged_with_svg_and_clock(tmp_path):
    payload = collect(make_db(tmp_path, activity=nights()), days=7)
    sessions = payload["sleep"]["sessions"]
    assert len(sessions) == 1

    session = sessions[0]
    assert session["heart_rate_available"] is True
    assert session["minutes_in_bed"] == 420
    assert session["minutes_asleep"] > 0
    assert len(session["stages"]) == 420
    assert session["svg"].startswith('<svg class="hypno"')
    assert 'aria-label="睡眠分期图"' in session["svg"]
    assert session["clock"] == datetime.fromtimestamp(BASE).strftime("%H:%M")
    assert session["hypnogram"]


def test_session_without_heart_rate_uses_a_flat_in_bed_band(tmp_path):
    """No usable HR -> the flat band, never four empty lanes pretending to be data."""
    payload = collect(make_db(tmp_path, activity=nights(hr=255)), days=7)
    session = payload["sleep"]["sessions"][0]

    assert session["heart_rate_available"] is False
    # The amount of sleep is still known -- it comes from movement, not staging.
    assert session["minutes_asleep"] == 420
    assert set(session["stages"]) == {STAGE_UNKNOWN}
    assert 'y="12"' in session["svg"] and "#33404f" in session["svg"]
    assert "aria-label=\"睡眠分期图\"" not in session["svg"]

    labels = {kpi["label"]: kpi for kpi in payload["kpi"]}
    assert labels["最近一晚卧床"]["value"] == "7.0"
    assert "无心率" in labels["最近一晚卧床"]["hint"]
    assert labels["深睡占比"]["value"] == "--"
    assert labels["睡眠效率"]["value"] == "--"


def test_kpi_reports_stage_shares_for_a_night_with_heart_rate(tmp_path):
    payload = collect(make_db(tmp_path, activity=nights()), days=7)
    labels = {kpi["label"]: kpi for kpi in payload["kpi"]}

    assert labels["最近一晚睡眠"]["value"] == "7.0"
    assert labels["最近一晚睡眠"]["unit"] == "小时"
    assert labels["深睡占比"]["unit"] == "%"
    assert labels["睡眠效率"]["unit"] == "%"
    assert labels["心率区间"]["value"] == "52–52"


def test_nonwear_minutes_do_not_paint_as_sleep(tmp_path):
    """The off-wrist night is its own band code, not hours of sleep."""
    activity = nights(minutes=60) + [
        (BASE + (60 + i) * 60, 243, 0, 0, 0) for i in range(180)
    ]
    payload = collect(make_db(tmp_path, activity=activity), days=7)
    codes = {ts: code for ts, code in payload["activity"]["points"]}
    assert codes[BASE] == BAND_SLEEP
    assert codes[BASE + 60 * 60] == hd.BAND_NONWEAR
    assert payload["sleep"]["sessions"][0]["minutes_in_bed"] == 60


def test_kpi_reports_the_off_wrist_stretch(tmp_path):
    activity = nights(minutes=60) + [
        (BASE + (60 + i) * 60, 243, 0, 0, 0) for i in range(240)
    ]
    payload = collect(make_db(tmp_path, activity=activity), days=7)
    labels = {card["label"]: card for card in payload["kpi"]}
    assert labels["手环未佩戴"]["value"] == "4.0"
    assert "起" in labels["手环未佩戴"]["hint"]
    # The real night is still reported as a night.
    assert labels["最近一晚睡眠"]["value"] == "1.0"


def test_collect_reads_sleep_spo2_from_dedicated_table(tmp_path):
    path = make_db(tmp_path, activity=nights(minutes=60), spo2=[(BASE, 100, 0)])
    _insert_sleep_spo2(path, [(BASE, 96, 32), (BASE + 60, 88, 16)])

    payload = collect(path, days=7)
    series = payload["spo2_sleep"]
    assert series["count"] == 2
    assert (series["min"], series["max"], series["avg"]) == (88, 96, 92.0)
    assert series["source"] == "sqlite"
    assert [point[1] for point in series["points"]] == [96, 88]
    assert payload["meta"]["spo2_sleep_samples"] == 2
    # The KPI is the true minimum across BOTH series, with the split spelled out.
    kpi = next(card for card in payload["kpi"] if card["label"] == "血氧最低")
    assert kpi["value"] == "88"
    assert "整夜 2 条" in kpi["hint"]


def test_collect_falls_back_to_spo2_sleep_jsonl(tmp_path):
    """Old captures only have a JSONL file; a corrupt line must not break it."""
    path = make_db(tmp_path)
    rows = _capture_rows(95, 89)
    (tmp_path / "spo2_sleep.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n{broken\n", encoding="utf-8"
    )

    payload = collect(path, days=7)
    assert payload["spo2_sleep"]["source"] == "jsonl"
    assert payload["spo2_sleep"]["count"] == 2
    assert payload["spo2_sleep"]["min"] == 89
    assert payload["meta"]["spo2_sleep_samples"] == 2


def test_collect_prefers_table_over_jsonl(tmp_path):
    path = make_db(tmp_path)
    _insert_sleep_spo2(path, [(BASE, 92, 32)])
    (tmp_path / "spo2_sleep.jsonl").write_text(
        json.dumps(_capture_rows(80, 81)[0]) + "\n", encoding="utf-8"
    )

    series = collect(path, days=7)["spo2_sleep"]
    assert series["source"] == "sqlite"
    assert series["count"] == 1
    assert series["min"] == 92


def test_collect_filters_out_of_window_and_implausible_rows(tmp_path):
    path = make_db(tmp_path)
    old = {"ts": BASE - 30 * 86400, "spo2": 91}
    bad = {"ts": BASE + 60, "spo2": 200}
    good = _capture_rows(94)[0]
    (tmp_path / "spo2_sleep.jsonl").write_text(
        "\n".join(json.dumps(row) for row in (old, bad, good)) + "\n", encoding="utf-8"
    )
    series = collect(path, days=7)["spo2_sleep"]
    assert series["count"] == 1
    assert series["points"][0][1] == 94


def test_truncated_night_is_flagged_not_silently_short(tmp_path):
    """A session starting at the first stored sample is a truncated night.

    When the band has already overwritten the beginning of a night, the session
    detector can only see the tail.  Reporting that as "slept 5.4 h" invites a
    bad-faith comparison against the app, so the payload has to carry the fact.
    """
    truncated = collect(make_db(tmp_path, activity=nights(minutes=60)), days=7)
    assert truncated["sleep"]["sessions"][0]["truncated_start"] is True

    # Same night, but the store reaches further back: nothing is cut off.
    activity = [(BASE - 30 * 60, 80, 0, 0, 70)] + nights(minutes=60)
    whole = collect(make_db(tmp_path, name="b.db", activity=activity), days=7)
    assert whole["sleep"]["sessions"][0]["truncated_start"] is False
