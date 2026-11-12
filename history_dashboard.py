"""Render a self-contained historical health dashboard from the telemetry DB.

The live dashboard (``dashboard.py`` / ``dashboard.html``) streams realtime
samples over SSE. This one is the offline counterpart: it reads everything that
was synced into SQLite -- band-side activity history, SpO2 records and the
per-minute heart rate -- and bakes it into a single HTML file with no external
assets, so it can be opened locally or shared as-is.

Charts are hand-rolled on a canvas, matching the project's "no third-party
dependency" rule and the visual language of the live dashboard.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Sequence

import sleep_analysis
from sleep_analysis import (
    STAGE_AWAKE,
    STAGE_DEEP,
    STAGE_LIGHT,
    STAGE_UNKNOWN,
    analyze,
    load_samples,
)

STAGE_ORDER = (STAGE_DEEP, STAGE_LIGHT, STAGE_AWAKE, STAGE_UNKNOWN)
STAGE_LABELS = {
    STAGE_DEEP: "深睡",
    STAGE_LIGHT: "浅睡",
    STAGE_AWAKE: "清醒",
    STAGE_UNKNOWN: "无心率",
}

HR_UNAVAILABLE = 255
HR_MIN, HR_MAX = 30, 220


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------
def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _local(timestamp: float, fmt: str = "%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(timestamp).strftime(fmt)


#: Capture filename written by ``--sync-sleep-spo2`` alongside ``mi_band.db``.
SPO2_SLEEP_JSONL = "spo2_sleep.jsonl"


def _byte_group(value: Any) -> list[int]:
    """SQLite keeps the four uninterpreted trailing groups as JSON text."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return list(value) if isinstance(value, (list, tuple)) else []


def _load_spo2_sleep(
    conn: sqlite3.Connection,
    since: float,
    db_path: str | Path,
    explicit_path: str | Path | None = None,
) -> tuple[list[dict], str]:
    """Read 0x26 samples: dedicated table first, JSONL capture as fallback.

    Older runs produced only ``spo2_sleep.jsonl``. Keeping that fallback lets
    already-captured evidence be viewed without another sync -- the band may
    clear uploaded data after the ACK, so a second fetch often returns nothing.
    Returns ``(rows, source)`` where source is ``sqlite`` / ``jsonl`` / ``none``.
    """
    if _table_exists(conn, "spo2_sleep"):
        rows = [
            {
                "ts": float(ts),
                "spo2": int(spo2),
                "duration": int(duration),
                "spo2_high": _byte_group(high),
                "spo2_low": _byte_group(low),
                "signal_quality": _byte_group(signal),
                "extend": _byte_group(extend),
            }
            for ts, spo2, duration, high, low, signal, extend in conn.execute(
                "SELECT ts, spo2, duration, spo2_high, spo2_low, signal_quality, extend "
                "FROM spo2_sleep WHERE ts >= ? ORDER BY ts",
                (since,),
            )
        ]
        if rows:
            return rows, "sqlite"

    path = Path(explicit_path) if explicit_path else Path(db_path).parent / SPO2_SLEEP_JSONL
    if not path.is_file():
        return [], "none"

    rows = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue  # a half-written line must not break the whole page
            if not isinstance(item, dict) or "ts" not in item or "spo2" not in item:
                continue
            try:
                ts = float(item["ts"])
                spo2 = int(item.get("spo2", 0))
            except (TypeError, ValueError):
                continue
            if ts < since or not 50 <= spo2 <= 100:
                continue
            rows.append(
                {
                    "ts": ts,
                    "spo2": spo2,
                    "duration": int(item.get("duration", 0) or 0),
                    "spo2_high": _byte_group(item.get("spo2_high", [])),
                    "spo2_low": _byte_group(item.get("spo2_low", [])),
                    "signal_quality": _byte_group(item.get("signal_quality", [])),
                    "extend": _byte_group(item.get("extend", [])),
                }
            )
    rows.sort(key=lambda item: item["ts"])
    return rows, "jsonl" if rows else "none"


def collect(
    db_path: str | Path,
    *,
    days: int = 7,
    spo2_sleep_path: str | Path | None = None,
) -> dict[str, Any]:
    """Gather everything the dashboard needs into one JSON-serialisable dict."""
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {path}")

    since = (datetime.now() - timedelta(days=days)).replace(second=0, microsecond=0).timestamp()
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        activity = []
        if _table_exists(conn, "activity_history"):
            activity = [
                {"ts": float(ts), "kind": int(kind), "intensity": int(intensity),
                 "steps": int(steps), "heart_rate": int(hr)}
                for ts, kind, intensity, steps, hr in conn.execute(
                    "SELECT ts, kind, intensity, steps, heart_rate FROM activity_history "
                    "WHERE ts >= ? ORDER BY ts",
                    (since,),
                )
            ]

        hr_rows = []
        if _table_exists(conn, "heart_rate"):
            hr_rows = [
                (float(ts), int(bpm))
                for ts, bpm in conn.execute(
                    "SELECT ts, bpm FROM heart_rate WHERE ts >= ? ORDER BY ts", (since,)
                )
            ]

        spo2_rows = []
        if _table_exists(conn, "spo2"):
            for ts, spo2, auto in conn.execute(
                "SELECT ts, spo2, auto_measure FROM spo2 WHERE ts >= ? ORDER BY ts", (since,)
            ):
                spo2_rows.append({"ts": float(ts), "spo2": int(spo2), "auto": bool(auto)})
        spo2_sleep_rows, spo2_sleep_source = _load_spo2_sleep(
            conn, since, db_path, spo2_sleep_path
        )
    finally:
        conn.close()

    spo2_sleep_values = [row["spo2"] for row in spo2_sleep_rows]

    sleep_result = analyze(activity)
    sessions = [
        {
            **session.as_dict(),
            "hypnogram": session.hypnogram(),
            "stages": list(session.stages),
            # Without heart rate there is nothing to stack: show the in-bed span
            # as one flat band instead of four empty lanes.
            "svg": hypnogram_svg(session.stages)
            if session.as_dict()["heart_rate_available"]
            else (
                f'<svg class="hypno" viewBox="0 0 {max(1, session.minutes)} 44" '
                f'preserveAspectRatio="none" role="img" aria-label="卧床时段">'
                f'<rect x="0" y="12" width="{max(1, session.minutes)}" height="20" '
                f'fill="#33404f"/></svg>'
            ),
            "clock": _local(session.start, "%H:%M"),
            # Labelled by the date the night started, the way Zepp Life does.
            "date": _local(session.start, "%m-%d"),
            # A session that begins at the very first sample in the store is not
            # a short night -- it is the tail of a night whose beginning the band
            # has already overwritten.  Say so, or the numbers read as "slept
            # 5.4 h" and get compared against the app in bad faith.
            "truncated_start": bool(activity)
            and abs(session.start - activity[0]["ts"]) <= 60,
        }
        for session in sleep_result["_sessions"]
    ]

    return {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "source": str(db_path),
            "days": days,
            "activity_samples": len(activity),
            "hr_samples": len(hr_rows),
            "spo2_samples": len(spo2_rows),
            "spo2_sleep_samples": len(spo2_sleep_rows),
            "spo2_sleep_source": spo2_sleep_source,
            "range": [_local(activity[0]["ts"]) if activity else None,
                      _local(activity[-1]["ts"]) if activity else None],
        },
        "sleep": {
            "sessions": sessions,
            "totals": sleep_result["totals"],
            "not_worn": sleep_result["not_worn"],
        },
        "hr": _package_heart_rate(hr_rows, activity),
        "activity": _package_activity(
            activity,
            {sample["ts"] for session in sleep_result["_sessions"] for sample in session.samples},
        ),
        "steps": _package_steps(activity),
        "spo2": [
            {**item, "time": _local(item["ts"], "%m-%d %H:%M")} for item in spo2_rows
        ],
        "spo2_sleep": {
            "points": [[row["ts"], row["spo2"]] for row in spo2_sleep_rows],
            "count": len(spo2_sleep_rows),
            "min": min(spo2_sleep_values) if spo2_sleep_values else None,
            "max": max(spo2_sleep_values) if spo2_sleep_values else None,
            "avg": (
                round(statistics.mean(spo2_sleep_values), 1)
                if spo2_sleep_values else None
            ),
            "source": spo2_sleep_source,
        },
        "kpi": _kpis(
            sessions, activity, spo2_rows, hr_rows,
            sleep_result["not_worn"], spo2_sleep_rows,
        ),
    }


def _valid_hr(value: int) -> Optional[int]:
    if value in (0, HR_UNAVAILABLE) or not HR_MIN <= value <= HR_MAX:
        return None
    return value


def _package_heart_rate(rows: Sequence[tuple[float, int]], activity: Sequence[dict]) -> dict[str, Any]:
    """Prefer the per-minute history series, falling back to live HR samples."""
    points: list[list[Any]] = []
    for item in activity:
        hr = _valid_hr(item["heart_rate"])
        if hr is not None:
            points.append([item["ts"], hr])
    if not points:
        for ts, bpm in rows:
            hr = _valid_hr(bpm)
            if hr is not None:
                points.append([ts, hr])

    values = [bpm for _, bpm in points]
    return {
        "points": points,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "avg": round(statistics.mean(values), 1) if values else None,
        "source": "history" if activity and points else ("live" if points else "none"),
    }


#: Activity band codes shared with the renderer.
BAND_REST, BAND_ACTIVE, BAND_SLEEP, BAND_NONWEAR = 0, 1, 2, 3


def _package_activity(
    activity: Sequence[dict], sleep_minutes: Optional[set[float]] = None
) -> dict[str, Any]:
    """Coarse activity band (rest / active / sleep / not worn) as ``[ts, code]``.

    "Sleep" here means *belongs to a detected session*, not "the band's state
    byte said sleep": a still afternoon would otherwise paint as sleep. The
    timestamps are kept because the band's history is not a gapless minute grid
    -- minutes it did not record are simply absent, so indexing by position
    would silently shift everything after the first hole.
    """
    sleep_minutes = sleep_minutes or set()
    points: list[list[float]] = []
    for item in activity:
        if sleep_analysis.is_nonwear(item["kind"]):
            # Checked first: both live in the 0xF0 family, and a night the band
            # spent on the nightstand must not paint as hours of sleep.
            code = BAND_NONWEAR
        elif item["ts"] in sleep_minutes:
            code = BAND_SLEEP
        elif sleep_analysis.is_movement(item):
            code = BAND_ACTIVE
        else:
            code = BAND_REST
        points.append([item["ts"], code])
    return {"points": points}


def _package_steps(activity: Sequence[dict]) -> dict[str, Any]:
    per_day: dict[str, int] = {}
    per_hour_today: list[int] = [0] * 24
    today = datetime.now().strftime("%Y-%m-%d")
    for item in activity:
        moment = datetime.fromtimestamp(item["ts"])
        day = moment.strftime("%Y-%m-%d")
        per_day[day] = per_day.get(day, 0) + int(item["steps"])
        if day == today:
            per_hour_today[moment.hour] += int(item["steps"])
    return {
        "daily": [{"date": day, "steps": steps} for day, steps in sorted(per_day.items())],
        "hourly_today": per_hour_today,
        "today": today,
        "total": sum(per_day.values()),
    }


def _kpis(
    sessions: Sequence[dict],
    activity: Sequence[dict],
    spo2_rows: Sequence[dict],
    hr_rows: Sequence[tuple[float, int]],
    not_worn: Sequence[dict] = (),
    spo2_sleep_rows: Sequence[dict] = (),
) -> list[dict[str, str]]:
    kpis: list[dict[str, str]] = []
    # The most recent night can be one the band spent on a table. Reporting it as
    # "in bed" (which the old code did) invents eight hours that never happened.
    last_gap = not_worn[-1] if not_worn else None
    max_gap = max(not_worn, key=lambda span: span["minutes"], default=None)

    if sessions:
        last = sessions[-1]
        if last["heart_rate_available"]:
            kpis.append({"label": "最近一晚睡眠", "value": f"{last['minutes_asleep'] / 60:.1f}",
                         "unit": "小时", "hint": last["start"][5:16].replace("T", " ") + " 入睡"})
            deep = last["stage_share"].get(STAGE_DEEP)
            kpis.append({"label": "深睡占比", "value": f"{(deep or 0) * 100:.0f}", "unit": "%",
                         "hint": f"{last['stage_minutes'].get(STAGE_DEEP, 0)} 分钟"})
            kpis.append({"label": "睡眠效率", "value": f"{last['efficiency'] * 100:.0f}", "unit": "%",
                         "hint": f"夜间觉醒 {last['awakenings']} 次"})
        else:
            # Without heart rate the band's own sleep window is all we can trust.
            kpis.append({"label": "最近一晚卧床", "value": f"{last['minutes_in_bed'] / 60:.1f}",
                         "unit": "小时", "hint": "该夜无心率，无法判断睡眠"})
            kpis.append({"label": "深睡占比", "value": "--", "unit": "%",
                         "hint": "该夜无心率数据"})
            kpis.append({"label": "睡眠效率", "value": "--", "unit": "%",
                         "hint": "该夜无心率数据"})
    else:
        kpis.append({"label": "最近一晚睡眠", "value": "--", "unit": "小时", "hint": "暂无数据"})

    if max_gap and max_gap["minutes"] >= 120:
        hint = max_gap["start"][5:16].replace("T", " ") + " 起"
        if last_gap is max_gap:
            hint += "（最近一次）"
        kpis.append({"label": "手环未佩戴", "value": f"{max_gap['hours']:.1f}", "unit": "小时",
                     "hint": hint})

    steps_today = 0
    today = datetime.now().strftime("%Y-%m-%d")
    for item in activity:
        if datetime.fromtimestamp(item["ts"]).strftime("%Y-%m-%d") == today:
            steps_today += int(item["steps"])
    total_steps = sum(int(item["steps"]) for item in activity)
    kpis.append({"label": "今日步数", "value": f"{steps_today:,}", "unit": "步",
                 "hint": f"{len(activity)} 分钟样本 · 合计 {total_steps:,}"})

    readings = [hr for hr in (_valid_hr(item["heart_rate"]) for item in activity) if hr]
    if not readings:
        readings = [bool(_valid_hr(bpm)) and bpm for _, bpm in hr_rows]
        readings = [value for value in readings if isinstance(value, int)]
    if readings:
        kpis.append({"label": "心率区间", "value": f"{min(readings)}–{max(readings)}",
                     "unit": "BPM", "hint": f"均值 {statistics.mean(readings):.0f}"})
    else:
        kpis.append({"label": "心率区间", "value": "--", "unit": "BPM", "hint": "暂无数据"})

    # Point measurements (0x25) and the night-long sleep stream (0x26) are
    # different series: report the true minimum across both, and spell out the
    # split so nobody reads it as one homogeneous set of readings.
    all_spo2 = [item["spo2"] for item in spo2_rows]
    all_spo2 += [row["spo2"] for row in spo2_sleep_rows]
    if all_spo2:
        kpis.append({"label": "血氧最低", "value": f"{min(all_spo2)}", "unit": "%",
                     "hint": f"点测 {len(spo2_rows)} 条 · 整夜 {len(spo2_sleep_rows)} 条"})
    else:
        kpis.append({"label": "血氧最低", "value": "--", "unit": "%", "hint": "暂无记录"})

    return kpis


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return _TEMPLATE.replace("__DATA__", data).replace(
        "__STAGE_STYLE__", json.dumps(_stage_style(), ensure_ascii=False)
    )


def _stage_style() -> dict[str, str]:
    return {
        STAGE_DEEP: "#8b5cf6",
        STAGE_LIGHT: "#38bdf8",
        STAGE_AWAKE: "#64748b",
        STAGE_UNKNOWN: "#2a323c",
    }


#: Lane index for the hypnogram, top row is the deepest stage (awake at bottom).
STAGE_LANES = {STAGE_DEEP: 0, STAGE_LIGHT: 1, STAGE_AWAKE: 2}


def hypnogram_svg(stages: Sequence[str], *, height: int = 44, lane_gap: float = 1.0) -> str:
    """Render a hypnogram as inline SVG, one rectangle run per stage stretch.

    Drawing this server-side (and run-length encoded) keeps it crisp at any
    width and avoids the canvas resize dance the live charts need.
    """
    colors = _stage_style()
    width = max(1, len(stages))
    # One lane per drawable stage: the last one must still land inside the viewBox.
    lane = height / len(STAGE_LANES)
    parts = [
        f'<svg class="hypno" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" role="img" aria-label="睡眠分期图">'
    ]
    for index in range(1, len(STAGE_LANES)):
        y = index * lane
        parts.append(
            f'<line x1="0" y1="{y:.2f}" x2="{width}" y2="{y:.2f}" '
            f'stroke="#2a323c" stroke-width="0.6"/>'
        )

    run_start = 0
    while run_start < len(stages):
        stage = stages[run_start]
        run_end = run_start
        while run_end + 1 < len(stages) and stages[run_end + 1] == stage:
            run_end += 1
        color = colors.get(stage)
        if color and stage in STAGE_LANES:
            y = STAGE_LANES[stage] * lane
            parts.append(
                f'<rect x="{run_start}" y="{y:.2f}" width="{run_end - run_start + 1}" '
                f'height="{max(1.0, lane - lane_gap):.2f}" fill="{color}"/>'
            )
        run_start = run_end + 1

    parts.append("</svg>")
    return "".join(parts)


_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mi Band 6 历史看板</title>
<style>
  :root {
    --bg: #0f1216; --panel: #171c22; --line: #2a323c; --text: #e6edf3;
    --muted: #8b98a5; --hr: #ff5c7a; --spo2: #38bdf8; --steps: #4ade80;
    --deep: #8b5cf6; --light: #38bdf8; --awake: #64748b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
          "PingFang SC", "Microsoft YaHei", sans-serif;
  }
  .wrap { max-width: 1120px; margin: 0 auto; padding: 22px 20px 40px; overflow-wrap: anywhere; }
  @media (max-width: 560px) { .wrap { padding: 16px 14px 32px; } h1 { font-size: 17px; } }
  header { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 18px; }
  h1 { font-size: 19px; margin: 0; font-weight: 650; letter-spacing: .2px; }
  .sub { color: var(--muted); font-size: 12px; }
  .sub b { color: var(--text); font-weight: 600; }
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
  .card {
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    padding: 13px 15px;
  }
  .card .label { color: var(--muted); font-size: 12px; }
  .card .value { font-size: 26px; font-weight: 700; margin-top: 3px; letter-spacing: -.5px; }
  .card .unit { font-size: 12px; color: var(--muted); margin-left: 4px; font-weight: 400; }
  .card .hint { color: var(--muted); font-size: 11px; margin-top: 2px; }
  section { margin-top: 22px; }
  h2 { font-size: 13px; color: var(--muted); font-weight: 500; margin: 0 0 10px; }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; }
  .night + .night { margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--line); }
  .night .hint { color: var(--muted); font-size: 11px; margin-top: 7px; line-height: 1.5; }
  .night-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
  .night-head .title { font-weight: 600; font-size: 14px; }
  .night-head .meta { color: var(--muted); font-size: 12px; }
  .hypno { width: 100%; height: 92px; display: block; margin: 10px 0 4px; }
  .scales { display: flex; justify-content: space-between; color: var(--muted); font-size: 11px; }
  .legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: var(--muted); margin-top: 8px; }
  .legend span { display: inline-flex; align-items: center; gap: 5px; }
  .swatch { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
  .bars { display: flex; height: 8px; border-radius: 4px; overflow: hidden; margin-top: 12px; }
  .bars div { height: 100%; }
  canvas.line { width: 100%; height: 190px; display: block; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--line); }
  th { color: var(--muted); font-weight: 500; font-size: 12px; }
  td.num { font-variant-numeric: tabular-nums; }
  footer { color: var(--muted); font-size: 11.5px; margin-top: 24px; line-height: 1.7; }
  footer code { word-break: break-all; }
  .pill {
    display: inline-block; padding: 1px 7px; border-radius: 999px; font-size: 11px;
    border: 1px solid var(--line); color: var(--muted); margin-left: 6px;
  }
  .empty { color: var(--muted); font-size: 13px; padding: 6px 0; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>⌚ Mi Band 6 历史看板</h1>
    <div class="sub" id="range"></div>
  </header>

  <div class="kpis" id="kpis"></div>

  <section>
    <h2>睡眠结构 <span class="pill">推导值，非手环直接上报</span></h2>
    <div class="panel" id="sleepPanel"></div>
  </section>

  <section>
    <h2>心率</h2>
    <div class="panel"><canvas class="line" id="hrChart"></canvas><div class="scales" id="hrScale"></div></div>
  </section>

  <section>
    <h2>活动节律</h2>
    <div class="panel"><canvas class="line" id="actChart"></canvas><div class="scales" id="actScale"></div></div>
  </section>

  <section>
    <h2>步数</h2>
    <div class="panel"><canvas class="line" id="stepChart"></canvas><div class="scales" id="stepScale"></div></div>
  </section>

  <section>
    <h2>整夜睡眠血氧 <span class="pill">0x26 · 独立于点测统计</span></h2>
    <div class="panel">
      <canvas class="line" id="spo2SleepChart"></canvas>
      <div class="scales" id="spo2SleepScale"></div>
    </div>
  </section>

  <section>
    <h2>点测血氧 <span class="pill">0x25</span></h2>
    <div class="panel" id="spo2Panel"></div>
  </section>

  <footer id="footer"></footer>
</div>

<script>
const DATA = __DATA__;
const STAGE_COLORS = __STAGE_STYLE__;
const STAGE_LABELS = {deep: "深睡", light: "浅睡", awake: "清醒", unknown: "无心率"};

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderHeader() {
  const meta = DATA.meta;
  const range = meta.range[0] && meta.range[1]
    ? `${meta.range[0]} → ${meta.range[1]}`
    : "暂无数据";
  document.getElementById("range").innerHTML =
    `<b>${range}</b> · 近 ${meta.days} 天 · 活动样本 ${meta.activity_samples} 分钟 · 生成于 ${meta.generated_at}`;
}

function renderKpis() {
  const host = document.getElementById("kpis");
  DATA.kpi.forEach(item => {
    const card = el("div", "card");
    card.appendChild(el("div", "label", item.label));
    const value = el("div", "value", item.value);
    if (item.unit && item.value !== "--") value.appendChild(el("span", "unit", item.unit));
    card.appendChild(value);
    card.appendChild(el("div", "hint", item.hint));
    host.appendChild(card);
  });
}

function renderSleep() {
  const host = document.getElementById("sleepPanel");
  const sessions = DATA.sleep.sessions;
  if (!sessions.length) { host.appendChild(el("div", "empty", "该窗口内没有识别到睡眠时段。")); return; }

  sessions.slice().reverse().forEach(session => {
    const box = el("div", "night");

    const head = el("div", "night-head");
    head.appendChild(el("span", "title", `${session.date} 夜`));
    const asleep = (session.minutes_asleep / 60).toFixed(1);
    const inBed = (session.minutes_in_bed / 60).toFixed(1);
    const efficiency = session.heart_rate_available
      ? ` · 效率 ${(session.efficiency * 100).toFixed(0)}%`
      : "";
    head.appendChild(el("span", "meta",
      `${session.start.slice(11, 16)} → ${session.end.slice(11, 16)} · 卧床 ${inBed}h · 睡眠 ${asleep}h${efficiency}`));
    box.appendChild(head);

    // hypnogram: pre-rendered SVG (one run per stage stretch), injected as-is
    const hypno = el("div");
    hypno.innerHTML = session.svg;
    box.appendChild(hypno);

    const scaleRow = el("div", "scales");
    const duration = session.minutes_in_bed;
    const tickCount = duration > 240 ? 4 : 2;
    for (let i = 0; i <= tickCount; i++) {
      const ts = session.start_ts + (duration * i / tickCount) * 60;
      const label = new Date(ts * 1000).toLocaleTimeString("zh-CN",
        {hour: "2-digit", minute: "2-digit", hour12: false});
      scaleRow.appendChild(el("span", "", label));
    }
    box.appendChild(scaleRow);

    if (session.truncated_start) {
      box.appendChild(el("div", "hint",
        `这一夜的开头在手环里已经没有了：数据最早只到 ${session.start.slice(11, 16)}。` +
        `卧床与睡眠时长都是被截断后的值，不要直接和 App 的数字比。`));
    }

    if (!session.heart_rate_available) {
      box.appendChild(el("div", "hint", "该夜手环未记录心率，无法推导深睡/浅睡，仅显示卧床时段。"));
      host.appendChild(box);
      return;
    }

    // stage proportions
    const shares = session.stage_share || {};
    const legend = el("div", "legend");
    ["deep", "light"].forEach(stage => {
      const item = el("span");
      const swatch = el("span", "swatch");
      swatch.style.background = STAGE_COLORS[stage];
      item.appendChild(swatch);
      const minutes = (session.stage_minutes || {})[stage] || 0;
      item.appendChild(document.createTextNode(`${STAGE_LABELS[stage]} ${minutes} 分 (${((shares[stage] || 0) * 100).toFixed(0)}%)`));
      legend.appendChild(item);
    });
    const awake = el("span");
    const awakeSwatch = el("span", "swatch");
    awakeSwatch.style.background = STAGE_COLORS.awake;
    awake.appendChild(awakeSwatch);
    awake.appendChild(document.createTextNode(`清醒 ${(session.stage_minutes || {}).awake || 0} 分`));
    legend.appendChild(awake);
    box.appendChild(legend);

    const bar = el("div", "bars");
    const total = session.minutes_in_bed || 1;
    ["deep", "light", "awake", "unknown"].forEach(stage => {
      const minutes = (session.stage_minutes || {})[stage] || 0;
      if (!minutes) return;
      const seg = el("div");
      seg.style.width = `${(minutes / total) * 100}%`;
      seg.style.background = STAGE_COLORS[stage];
      seg.title = `${STAGE_LABELS[stage]} ${minutes} 分`;
      bar.appendChild(seg);
    });
    box.appendChild(bar);

    host.appendChild(box);
  });
}

function axis(ctx, width, height, pad, minValue, maxValue, ticks, format) {
  ctx.strokeStyle = "#2a323c"; ctx.fillStyle = "#8b98a5";
  ctx.font = "11px -apple-system, 'PingFang SC', sans-serif";
  ctx.lineWidth = 1;
  for (let i = 0; i <= ticks; i++) {
    const value = minValue + (maxValue - minValue) * (i / ticks);
    const y = height - pad.bottom - (height - pad.top - pad.bottom) * (i / ticks);
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
    ctx.fillText(format(value), 4, y + 4);
  }
}

function lineChart(canvasId, points, options) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const opts = options || {};
  const draw = () => {
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth, height = canvas.clientHeight;
    canvas.width = width * dpr; canvas.height = height * dpr;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    if (!points.length) {
      ctx.fillStyle = "#8b98a5"; ctx.font = "12px sans-serif";
      ctx.fillText("暂无数据", 12, height / 2);
      return;
    }
    const pad = {left: 38, right: 10, top: 8, bottom: 18};
    const xs = points.map(p => p[0]);
    const ys = points.map(p => p[1]);
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    const y0 = opts.min !== undefined ? opts.min : Math.min(...ys);
    const y1 = opts.max !== undefined ? opts.max : Math.max(...ys);
    const spanY = (y1 - y0) || 1;
    const spanX = (x1 - x0) || 1;

    axis(ctx, width, height, pad, y0, y1, 4, v => Math.round(v));

    const sx = value => pad.left + ((value - x0) / spanX) * (width - pad.left - pad.right);
    const sy = value => height - pad.bottom - ((value - y0) / spanY) * (height - pad.top - pad.bottom);

    ctx.strokeStyle = opts.color || "#ff5c7a";
    ctx.lineWidth = 1.4;
    // Break the line across data gaps instead of drawing a long diagonal
    // through them (an unsynced night would otherwise invent a straight line).
    const maxGap = opts.maxGap || 300;
    const path = new Path2D();
    let pen = false;
    let lastX = null;
    for (const [t, v] of points) {
      const x = sx(t), y = sy(v);
      if (lastX !== null && t - lastX > maxGap) pen = false;
      if (!pen) { path.moveTo(x, y); pen = true; } else { path.lineTo(x, y); }
      lastX = t;
    }
    ctx.stroke(path);

    ctx.fillStyle = "#8b98a5"; ctx.font = "11px sans-serif";
    ctx.fillText(new Date(x0 * 1000).toLocaleString("zh-CN", {month: "2-digit", day: "2-digit", hour: "2-digit"}), pad.left, height - 4);
    const endLabel = new Date(x1 * 1000).toLocaleString("zh-CN", {month: "2-digit", day: "2-digit", hour: "2-digit"});
    ctx.fillText(endLabel, width - pad.right - ctx.measureText(endLabel).width, height - 4);
  };
  requestAnimationFrame(draw);
  window.addEventListener("resize", () => requestAnimationFrame(draw));
}

function renderHeartRate() {
  const hr = DATA.hr;
  lineChart("hrChart", hr.points, {color: "#ff5c7a"});
  const scale = document.getElementById("hrScale");
  if (hr.points.length) {
    scale.textContent = `${hr.min}–${hr.max} BPM · 均值 ${hr.avg} · 来源：${hr.source === "history" ? "手环分钟历史" : "实时流"}`;
  } else {
    scale.textContent = "未采集到心率";
  }
}

function renderActivity() {
  const canvas = document.getElementById("actChart");
  const points = DATA.activity.points;
  const draw = () => {
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 600, height = canvas.clientHeight || 120;
    canvas.width = width * dpr; canvas.height = height * dpr;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (!points.length) {
      ctx.fillStyle = "#8b98a5"; ctx.font = "12px sans-serif"; ctx.fillText("暂无数据", 12, height / 2); return;
    }

    const dayStart = ts => {
      const d = new Date(ts * 1000);
      return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() / 1000;
    };
    const firstDay = dayStart(points[0][0]);
    const lastDay = dayStart(points[points.length - 1][0]);
    const days = Math.round((lastDay - firstDay) / 86400) + 1;
    const DAY = 86400;

    const left = 54, rowGap = 5;
    const rowH = Math.max(8, (height - rowGap * (days - 1)) / days);
    const plotW = width - left - 6;
    const colors = ["#33404f", "#4ade80", "#8b5cf6", "#1e2630"]; // rest / active / sleep / not worn
    // "Not worn" and "no record" are two different facts and must not look the
    // same.  The band writes a state byte for every minute it was off the wrist
    // (0xF3), so those minutes are *measured*; a hole in the history is a minute
    // the band no longer has -- typically because the phone already fetched it.
    // A diagonal hatch marks the former, a plain panel background the latter.
    const nonwearHatch = (() => {
      const tile = document.createElement("canvas");
      tile.width = 6; tile.height = 6;
      const t = tile.getContext("2d");
      t.fillStyle = "#1e2630"; t.fillRect(0, 0, 6, 6);
      t.strokeStyle = "#4a5866"; t.lineWidth = 1;
      t.beginPath(); t.moveTo(-1, 7); t.lineTo(7, -1); t.stroke();
      return ctx.createPattern(tile, "repeat");
    })();
    const cellW = Math.max(1, plotW / 1440);

    ctx.font = "11px -apple-system, 'PingFang SC', sans-serif";
    for (let day = 0; day < days; day++) {
      const y = day * (rowH + rowGap);
      ctx.strokeStyle = "#232b35";
      for (let hour = 0; hour <= 24; hour += 6) {
        const x = left + (hour / 24) * plotW;
        ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x, y + rowH); ctx.stroke();
      }
      const label = new Date((firstDay + day * DAY) * 1000)
        .toLocaleDateString("zh-CN", {month: "2-digit", day: "2-digit"});
      ctx.fillStyle = "#8b98a5";
      ctx.fillText(label, 4, y + rowH / 2 + 4);
      // baseline so an empty day still reads as a row
      ctx.fillStyle = "#1b232c";
      ctx.fillRect(left, y, plotW, rowH);
    }

    for (const [ts, code] of points) {
      const day = Math.floor((ts - firstDay) / DAY);
      if (day < 0 || day >= days) continue;
      const minute = (ts - (firstDay + day * DAY)) / 60;
      const y = day * (rowH + rowGap);
      ctx.fillStyle = code === 3 ? nonwearHatch : (colors[code] || "#333");
      ctx.fillRect(left + (minute / 1440) * plotW, y, cellW, rowH);
    }

    ctx.fillStyle = "#8b98a5";
    ["00", "06", "12", "18", "24"].forEach((hour, index) => {
      ctx.fillText(hour, left + (index / 4) * plotW - 6, height - 2);
    });
  };
  requestAnimationFrame(draw);
  window.addEventListener("resize", () => requestAnimationFrame(draw));
  document.getElementById("actScale").textContent =
    points.length ? "紫=睡眠 绿=活动 深灰=静止 斜纹=未佩戴 空白=无记录 · 每格 1 分钟，按天分行" : "";
}

function renderSteps() {
  const canvas = document.getElementById("stepChart");
  const daily = DATA.steps.daily;
  const draw = () => {
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 600, height = canvas.clientHeight || 150;
    canvas.width = width * dpr; canvas.height = height * dpr;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (!daily.length) {
      ctx.fillStyle = "#8b98a5"; ctx.font = "12px sans-serif"; ctx.fillText("暂无数据", 12, height / 2); return;
    }

    const pad = {left: 46, right: 8, top: 10, bottom: 22};
    const maxValue = Math.max(...daily.map(d => d.steps), 1);
    const niceMax = Math.ceil(maxValue / 1000) * 1000 || 1000;
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;

    ctx.font = "11px -apple-system, 'PingFang SC', sans-serif";
    ctx.strokeStyle = "#2a323c";
    for (let i = 0; i <= 3; i++) {
      const y = pad.top + plotH * (i / 3);
      ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
      ctx.fillStyle = "#8b98a5";
      ctx.fillText(Math.round(niceMax * (1 - i / 3)).toLocaleString(), 4, y + 4);
    }

    const slot = plotW / daily.length;
    const barW = Math.min(72, slot * 0.55);
    daily.forEach((day, index) => {
      const h = (day.steps / niceMax) * plotH;
      const x = pad.left + slot * index + (slot - barW) / 2;
      const gradient = ctx.createLinearGradient(0, pad.top + plotH - h, 0, pad.top + plotH);
      gradient.addColorStop(0, "#4ade80");
      gradient.addColorStop(1, "rgba(74,222,128,.35)");
      ctx.fillStyle = gradient;
      ctx.beginPath();
      ctx.roundRect(x, pad.top + plotH - h, barW, h, 4);
      ctx.fill();

      ctx.fillStyle = "#e6edf3";
      ctx.textAlign = "center";
      ctx.fillText(day.steps.toLocaleString(), x + barW / 2, pad.top + plotH - h - 5);
      ctx.fillStyle = "#8b98a5";
      ctx.fillText(day.date.slice(5), x + barW / 2, height - 6);
      ctx.textAlign = "left";
    });
  };
  requestAnimationFrame(draw);
  window.addEventListener("resize", () => requestAnimationFrame(draw));
  document.getElementById("stepScale").textContent = daily.length
    ? `合计 ${DATA.steps.total.toLocaleString()} 步`
    : "";
}

function renderSpo2Sleep() {
  const s = DATA.spo2_sleep || {points: [], count: 0};
  const scale = document.getElementById("spo2SleepScale");
  if (!s.points.length) {
    const canvas = document.getElementById("spo2SleepChart");
    if (canvas) canvas.style.display = "none";
    scale.textContent = "该窗口内没有睡眠血氧记录（需 --sync-sleep-spo2，或提供 spo2_sleep.jsonl）";
    return;
  }
  // Fixed upper bound keeps the night readable; a lower minimum than 80 only
  // widens the axis when the band actually reported something below it.
  lineChart("spo2SleepChart", s.points, {
    color: "#38bdf8",
    min: Math.min(80, Math.floor(s.min - 2)),
    max: 100,
    maxGap: 300
  });
  const origin = s.source === "sqlite" ? "SQLite spo2_sleep 表" : "spo2_sleep.jsonl";
  scale.textContent = `${s.count} 条 · ${s.min}–${s.max}% · 均值 ${s.avg}% · 来源：${origin}`;
}

function renderSpo2() {
  const host = document.getElementById("spo2Panel");
  if (!DATA.spo2.length) {
    host.appendChild(el("div", "empty", "该窗口内没有点测血氧记录。"));
    return;
  }
  const table = el("table");
  const head = el("tr");
  ["时间", "血氧", "类型"].forEach(name => head.appendChild(el("th", "", name)));
  table.appendChild(head);
  DATA.spo2.slice().reverse().forEach(item => {
    const row = el("tr");
    row.appendChild(el("td", "", item.time));
    row.appendChild(el("td", "num", `${item.spo2}%`));
    row.appendChild(el("td", "", item.auto ? "自动测量" : "手动测量"));
    table.appendChild(row);
  });
  host.appendChild(table);
}

function renderFooter() {
  const meta = DATA.meta;
  document.getElementById("footer").innerHTML =
    `数据来源：Mi Band 6 BLE 直连同步（活动 ${meta.activity_samples} 分钟 · 心率 ${meta.hr_samples} 条 · 点测血氧 ${meta.spo2_samples} 条 · 整夜血氧 ${meta.spo2_sleep_samples} 条）` +
    ` · 数据库 <code>${meta.source}</code><br>` +
    `睡眠分期为<b>推导值</b>：手环并不直接上报每分钟阶段，这里用 <code>kind</code> 状态机判定睡眠窗口，` +
    `再按心率水平与心率变异性区分深睡与浅睡；腕部无 EEG，结果用于看趋势，不作逐分钟判读。` +
    `深睡/浅睡在无心率时段显示为“无心率”。` +
    `活动条里 <b>斜纹</b> 是手环记录到的“未佩戴”（<code>kind=0xF3</code>，心率无读数），` +
    `<b>空白</b> 是那一分钟手环已经没有数据（通常已被手机 App 取走）——两者含义完全不同，别混看。` +
    `Zepp Life 的六页里，手环佩戴的五夜都没有 REM 一栏，唯一有 REM 的那夜手环未佩戴（另一台设备）；` +
    `手环本身也不产出分期结果（0x48 返回 0 条），因此这里只给深睡/浅睡。`;
}

renderHeader();
renderKpis();
renderSleep();
renderHeartRate();
renderActivity();
renderSteps();
renderSpo2Sleep();
renderSpo2();
renderFooter();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build(
    db_path: str | Path,
    output: str | Path,
    *,
    days: int = 7,
    spo2_sleep_path: str | Path | None = None,
) -> Path:
    payload = collect(db_path, days=days, spo2_sleep_path=spo2_sleep_path)
    html = render(payload)
    target = Path(output)
    # ``--output data/reports/dash.html`` is a natural thing to type; don't make
    # the user mkdir first.
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        with contextlib.suppress(OSError):
            target.parent.chmod(0o700)
    target.write_text(html, encoding="utf-8")
    if os.name != "nt":
        with contextlib.suppress(OSError):
            target.chmod(0o600)
    return target


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a self-contained historical health dashboard (HTML)."
    )
    parser.add_argument("--db", required=True, help="path to mi_band.db")
    parser.add_argument("-o", "--output", default="history_dashboard.html", help="output HTML file")
    parser.add_argument("--days", type=int, default=7, help="window in days (default 7)")
    parser.add_argument(
        "--spo2-sleep-jsonl",
        default=None,
        help=(
            "explicit path to spo2_sleep.jsonl; by default the file next to "
            "--db is used, and the dedicated spo2_sleep SQLite table wins"
        ),
    )
    args = parser.parse_args(argv)

    try:
        target = build(
            args.db, args.output, days=args.days,
            spo2_sleep_path=args.spo2_sleep_jsonl,
        )
        payload = collect(
            args.db, days=args.days, spo2_sleep_path=args.spo2_sleep_jsonl
        )
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f"[-] {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {target} ({target.stat().st_size // 1024} KB)")
    print(
        f"  activity samples: {payload['meta']['activity_samples']}, "
        f"sleep sessions: {len(payload['sleep']['sessions'])}, "
        f"point spo2: {payload['meta']['spo2_samples']}, "
        f"sleep spo2: {payload['meta']['spo2_sleep_samples']} "
        f"({payload['meta']['spo2_sleep_source']})"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
