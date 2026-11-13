"""Offline dashboard render()/build(): template, SVG and CLI."""

from __future__ import annotations

import json
import re

import pytest

from history_dashboard import build, collect, hypnogram_svg, main, render
from sleep_analysis import STAGE_AWAKE, STAGE_DEEP, STAGE_LIGHT, STAGE_UNKNOWN
from tests.offline_dashboard.support import BASE, _capture_rows, make_db, nights


def extract_data(html: str) -> dict:
    match = re.search(r"const DATA = (\{.*?\});\nconst STAGE_COLORS", html, re.S)
    assert match, "template did not receive its DATA payload"
    return json.loads(match.group(1))


def _rects(svg: str) -> list[tuple[int, float, int]]:
    return [
        (int(x), float(y), int(width))
        for x, y, width in re.findall(r'<rect x="(\d+)" y="([\d.]+)" width="(\d+)"', svg)
    ]


# ---------------------------------------------------------------------------
# hypnogram_svg()
# ---------------------------------------------------------------------------
def test_hypnogram_runs_cover_every_minute_exactly_once():
    stages = [STAGE_DEEP] * 5 + [STAGE_LIGHT] * 3 + [STAGE_DEEP] * 2 + [STAGE_AWAKE] * 4
    rects = _rects(hypnogram_svg(stages, height=44))

    covered = sorted((x, x + width) for x, _, width in rects)
    assert covered == [(0, 5), (5, 8), (8, 10), (10, 14)]
    # run-length encoded: 4 stretches, not 14 one-minute boxes
    assert len(rects) == 4
    assert sum(width for _, _, width in rects) == len(stages)


def test_hypnogram_lanes_follow_stage_depth():
    """Deep on top, awake at the bottom -- a mis-ordered lane reads as a wrong night."""
    svg = hypnogram_svg([STAGE_DEEP, STAGE_LIGHT, STAGE_AWAKE], height=44)
    lanes = {y: stage for (_, y, _), stage in zip(_rects(svg), [STAGE_DEEP, STAGE_LIGHT, STAGE_AWAKE])}
    # Three lanes now that REM is gone: 44 px split in three, and the bottom
    # lane still lands inside the viewBox.
    assert [lanes[y] for y in sorted(lanes)] == [STAGE_DEEP, STAGE_LIGHT, STAGE_AWAKE]
    assert sorted(lanes) == pytest.approx([0.0, 44 / 3, 88 / 3], abs=0.01)


def test_hypnogram_does_not_draw_unstaged_minutes():
    """``unknown`` has a colour but no lane -- it must not paint anything."""
    svg = hypnogram_svg([STAGE_UNKNOWN] * 30 + [STAGE_LIGHT] * 10)
    rects = _rects(svg)
    assert len(rects) == 1
    assert rects[0][0] == 30 and rects[0][2] == 10


def test_hypnogram_empty_input_stays_valid():
    svg = hypnogram_svg([])
    assert 'viewBox="0 0 1 44"' in svg
    assert _rects(svg) == []


def test_hypnogram_width_tracks_the_night_length():
    for minutes in (1, 90, 601):
        assert f'viewBox="0 0 {minutes} 44"' in hypnogram_svg([STAGE_DEEP] * minutes)


# ---------------------------------------------------------------------------
# render() / build()
# ---------------------------------------------------------------------------
def test_render_leaves_no_placeholder_behind(tmp_path):
    html = render(collect(make_db(tmp_path, activity=nights()), days=7))
    assert "__DATA__" not in html
    assert "__STAGE_STYLE__" not in html
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")


def test_render_embeds_a_parseable_payload(tmp_path):
    payload = collect(make_db(tmp_path, activity=nights(), spo2=[(BASE + 300, 98, 0)]), days=7)
    data = extract_data(render(payload))

    assert data["meta"] == payload["meta"]
    assert data["sleep"]["sessions"][0]["svg"] == payload["sleep"]["sessions"][0]["svg"]
    assert data["spo2"][0]["spo2"] == 98
    # the stage palette is injected separately, as JS colours
    style = re.search(r"const STAGE_COLORS = (\{.*?\});", render(payload), re.S).group(1)
    assert STAGE_DEEP in json.loads(style)


def test_build_writes_a_self_contained_file(tmp_path):
    target = build(make_db(tmp_path, activity=nights()), tmp_path / "out" / "dash.html", days=7)
    html = target.read_text(encoding="utf-8")

    assert target.exists() and target.stat().st_size > 8000
    # no external assets: the file must render offline as-is
    assert not re.search(r'(src|href)="(https?:)?//', html)
    assert "<canvas" in html


def test_cli_builds_and_reports(tmp_path, capsys):
    path = make_db(tmp_path, activity=nights(), spo2=[(BASE + 60, 97, 1)])
    out = tmp_path / "cli.html"

    assert main(["--db", str(path), "-o", str(out), "--days", "7"]) == 0
    printed = capsys.readouterr().out
    assert "activity samples: 420" in printed
    assert "sleep sessions: 1" in printed
    assert "spo2: 1" in printed
    assert out.exists()


def test_missing_database_raises_and_exits(tmp_path):
    missing = tmp_path / "does_not_exist.db"
    with pytest.raises(FileNotFoundError):
        collect(missing)
    with pytest.raises(FileNotFoundError):
        build(missing, tmp_path / "out.html")
    assert main(["--db", str(missing)]) == 1
    assert not missing.exists()


def test_build_file_permissions(tmp_path):
    import os
    if os.name == "nt":
        pytest.skip("POSIX permission checks do not apply to Windows")
    db = make_db(tmp_path, activity=nights())
    out = tmp_path / "secure_dir" / "dash.html"
    build(db, out)
    assert out.parent.stat().st_mode & 0o777 == 0o700
    assert out.stat().st_mode & 0o777 == 0o600


def test_render_has_sleep_spo2_chart(tmp_path):
    html = render(collect(make_db(tmp_path), days=7))
    assert 'id="spo2SleepChart"' in html
    assert "function renderSpo2Sleep()" in html
    assert "没有睡眠血氧记录" in html
    assert "__DATA__" not in html
    assert "__STAGE_STYLE__" not in html


def test_build_accepts_explicit_spo2_sleep_path(tmp_path):
    db = make_db(tmp_path, name="main.db")
    elsewhere = tmp_path / "captures" / "spo2_sleep.jsonl"
    elsewhere.parent.mkdir()
    elsewhere.write_text(json.dumps(_capture_rows(91)[0]) + "\n", encoding="utf-8")

    target = build(db, tmp_path / "out.html", days=7, spo2_sleep_path=elsewhere)
    assert target.exists()
    payload = collect(db, days=7, spo2_sleep_path=elsewhere)
    assert payload["spo2_sleep"]["source"] == "jsonl"
    assert payload["spo2_sleep"]["count"] == 1


def test_nonwear_band_is_hatched_apart_from_a_gap(tmp_path):
    """'Not worn' and 'no data' must not be the same colour on the timeline."""
    html = render(collect(make_db(tmp_path, activity=nights()), days=7))
    assert "斜纹=未佩戴" in html
    assert "空白=无记录" in html
    assert "nonwearHatch" in html
