"""Exact user-facing text produced by the Python side of the dashboard.

Keeping every label and hint here means the payload copy cannot drift while
modules are reorganised. The bundled JavaScript carries its own template text;
this module owns only strings that Python formats into the payload or CLI
output. All Chinese copy is byte-identical to the historical dashboard.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# KPI labels
# ---------------------------------------------------------------------------
LABEL_LAST_NIGHT_SLEEP = "最近一晚睡眠"
LABEL_DEEP_SHARE = "深睡占比"
LABEL_SLEEP_EFFICIENCY = "睡眠效率"
LABEL_LAST_NIGHT_IN_BED = "最近一晚卧床"
LABEL_BAND_NOT_WORN = "手环未佩戴"
LABEL_STEPS_TODAY = "今日步数"
LABEL_HEART_RATE_RANGE = "心率区间"
LABEL_SPO2_MIN = "血氧最低"

# ---------------------------------------------------------------------------
# Units and placeholders
# ---------------------------------------------------------------------------
UNIT_HOURS = "小时"
UNIT_PERCENT = "%"
UNIT_STEPS = "步"
UNIT_BPM = "BPM"
VALUE_PLACEHOLDER = "--"

# ---------------------------------------------------------------------------
# Standalone hints
# ---------------------------------------------------------------------------
HINT_NO_DATA = "暂无数据"
HINT_NO_HR_SLEEP_UNKNOWN = "该夜无心率，无法判断睡眠"
HINT_NO_HR_DATA = "该夜无心率数据"
HINT_NO_SPO2_RECORDS = "暂无记录"

#: Hypnogram/aria labels for the inline SVGs.
HYPNOGRAM_LABEL = "睡眠分期图"
FLAT_IN_BED_LABEL = "卧床时段"

#: CLI framing.
CLI_ERROR = "[-] {message}"
CLI_WROTE = "Wrote {target} ({size} KB)"
CLI_SUMMARY = (
    "  activity samples: {activity}, sleep sessions: {sessions}, "
    "point spo2: {spo2}, sleep spo2: {spo2_sleep} ({source})"
)


# ---------------------------------------------------------------------------
# Composed KPI hints
# ---------------------------------------------------------------------------
def hint_fell_asleep(clock: str) -> str:
    """Hint for the last night's sleep card: when the session started."""
    return f"{clock} 入睡"


def hint_since(clock: str) -> str:
    """Hint for the not-worn card: when the longest off-wrist stretch began."""
    return f"{clock} 起"


def hint_most_recent(hint: str) -> str:
    """Mark a hint as the most recent not-worn stretch."""
    return hint + "（最近一次）"


def hint_minutes(minutes: int) -> str:
    """Hint for the deep-sleep share card: the raw deep minutes."""
    return f"{minutes} 分钟"


def hint_awakenings(count: int) -> str:
    """Hint for the efficiency card: how many awakenings the night had."""
    return f"夜间觉醒 {count} 次"


def hint_steps_activity(samples: int, total_steps: int) -> str:
    """Hint for the steps card: sample count and all-window total."""
    return f"{samples} 分钟样本 · 合计 {total_steps:,}"


def hint_hr_mean(mean: float) -> str:
    """Hint for the heart-rate range card: the window mean."""
    return f"均值 {mean:.0f}"


def hint_spo2_split(point_rows: int, sleep_rows: int) -> str:
    """Hint for the SpO2 minimum card: point vs night-long series counts.

    Point measurements (0x25) and the night-long sleep stream (0x26) are
    different series; spelling out the split stops the minimum being read as one
    homogeneous set of readings.
    """
    return f"点测 {point_rows} 条 · 整夜 {sleep_rows} 条"
