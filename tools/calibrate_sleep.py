"""Score every candidate reading of the sleep bytes against Zepp Life's numbers.

The bytes ``sleep`` / ``deep_sleep`` / ``rem_sleep`` in the activity history had
four plausible readings at some point, and three of them were only rejected
because a real Zepp page contradicted them. Comparing by eye is how that took
five rounds; this tool puts all four on one table and refuses to fit anything
when there is not enough data.

Candidate readings:

* ``integral``   -- ``(byte & 0x7F) / 128`` summed over the window, i.e. "this
  byte is a per-minute share of the stage".
* ``gb``         -- Gadgetbridge's thresholds: REM if ``rem > 55``, else deep if
  ``deep > 42``, else light.
* ``heuristic``  -- what this project ships: heart-rate level rank + short-term
  variability rank pick the deep minutes.
* ``sweep``      -- for the deep column only, every threshold from 0 to 96, to
  show whether *any* fixed cut-off reproduces the app on all nights.

Usage:

    ./.venv/Scripts/python.exe tools/calibrate_sleep.py --zepp data/zepp_nights.json

Night identity follows Zepp Life: a night belongs to the date you went to sleep
on, so ``2020-01-06`` means the session that started on the evening of the 6th.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sleep_analysis as sa  # noqa: E402

#: Gadgetbridge's thresholds (HuamiExtendedSampleProvider.java:124). The author
#: calls them "arbitrary" in the comment above them.
GB_REM_MIN = 55
GB_DEEP_MIN = 42


# ---------------------------------------------------------------------------
# Candidate readings
# ---------------------------------------------------------------------------
def _scored(sample: dict) -> tuple[int, int]:
    """``(deep, rem)`` with the 0x80 "scored" flag stripped."""
    def low(raw: Any) -> int:
        raw = int(raw or 0)
        return raw & 0x7F if raw & 0x80 else 0

    return low(sample.get("deep_sleep", 0)), low(sample.get("rem_sleep", 0))


def integral(session: Any) -> dict[str, float]:
    """``byte / 128`` summed over the window -> expected stage minutes."""
    deep = sum(_scored(s)[0] for s in session.samples) / 128
    rem = sum(_scored(s)[1] for s in session.samples) / 128
    light = sum(s.get("sleep", 0) for s in session.samples) / 128
    return {"deep": deep, "rem": rem, "light": light}


def gb_thresholds(session: Any) -> dict[str, float]:
    """Gadgetbridge's REM-then-deep threshold count, one minute per minute."""
    counts = {"deep": 0, "rem": 0, "light": 0}
    for sample in session.samples:
        deep, rem = _scored(sample)
        if rem > GB_REM_MIN:
            counts["rem"] += 1
        elif deep > GB_DEEP_MIN:
            counts["deep"] += 1
        else:
            counts["light"] += 1
    return {key: float(value) for key, value in counts.items()}


def heuristic(session: Any) -> dict[str, float]:
    """The shipped model, read back off the session's own staging."""
    return {
        key: float(session.stage_minutes.get(key, 0))
        for key in ("deep", "rem", "light")
    }


MODELS = {
    "integral": integral,
    "gb": gb_thresholds,
    "heuristic": heuristic,
}


# ---------------------------------------------------------------------------
# Night lookup
# ---------------------------------------------------------------------------
def _local_date(timestamp: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


def find_session(result: dict, date: str) -> Optional[Any]:
    for session in result["_sessions"]:
        if _local_date(session.start) == date:
            return session
    return None


def nonwear_minutes(result: dict, date: str) -> int:
    """Minutes of ``0xF3`` the band recorded on the evening of *date*."""
    return sum(
        span["minutes"]
        for span in result.get("not_worn", [])
        if span.get("start", "").startswith(date)
    )


def _window_bounds(night: dict) -> Optional[tuple[float, float]]:
    """Zepp's in-bed window as epoch seconds.

    The app labels a night by the evening it started, so ``["22:44", "05:11"]``
    is one night that crosses midnight: an end that is not after the start rolls
    over to the next day.
    """
    window = night.get("window")
    if not window:
        return None
    from datetime import datetime, timedelta

    start = datetime.strptime(f"{night['date']} {window[0]}", "%Y-%m-%d %H:%M")
    end = datetime.strptime(f"{night['date']} {window[1]}", "%Y-%m-%d %H:%M")
    if end <= start:
        end += timedelta(days=1)
    return start.timestamp(), end.timestamp()


def _overlap_minutes(night: dict, session: Any) -> Optional[int]:
    """How many of the session's minutes fall inside the window Zepp reports."""
    bounds = _window_bounds(night)
    if bounds is None:
        return None
    low, high = bounds
    return sum(1 for sample in session.samples if low <= sample["ts"] <= high)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def report(db: str, nights: list[dict]) -> int:
    # include_scores keeps the three trailing bytes on every sample; without
    # them the integral and sweep readings have nothing to read.
    samples = sa.load_samples(db, include_scores=True)
    result = sa.analyze(samples)
    print(f"数据库 {db}")
    if samples:
        span = (
            f"{_local_date(samples[0]['ts'])} {_clock(samples[0]['ts'])}"
            f" → {_local_date(samples[-1]['ts'])} {_clock(samples[-1]['ts'])}"
        )
        print(f"  数据范围 {span}（{len(samples)} 分钟）")
        print("  早于这个范围的夜晚没有可对照的数据，不是比对失败。")
    print(f"  睡眠段 {len(result['sessions'])} 个，未佩戴段 {len(result['not_worn'])} 个")

    usable: list[dict] = []
    unverified: list[str] = []
    print("\n=== 逐夜可用性 ===")
    print(f"{'日期':<12}{'Zepp 深/浅/REM/清醒':<26}{'我们的窗口':<22}{'判定'}")
    for night in nights:
        date = night["date"]
        session = find_session(result, date)
        zepp = f"{night['deep_min']}/{night['light_min']}/{night['rem_min']}/{night['awake_min']}"
        if session is None:
            worn = nonwear_minutes(result, date)
            if worn:
                reason = f"手环那一夜未佩戴（{worn} 分钟 0xF3，无心率）→ 这一页来自别的设备"
            elif samples:
                reason = (
                    f"库里没有这一夜（数据从 {_local_date(samples[0]['ts'])[5:]} "
                    f"{_clock(samples[0]['ts'])} 才开始）"
                )
            else:
                reason = "库里没有任何活动历史"
            print(f"{date:<12}{zepp:<26}{'—':<22}{reason}")
            continue
        window = (
            f"{_local_date(session.start)[5:]} "
            f"{_clock(session.start)}→{_clock(session.end)}"
        )
        hr = sum(1 for s in session.samples if sa.usable_heart_rate(s) is not None)
        if hr == 0:
            print(f"{date:<12}{zepp:<26}{window:<22}无心率，无法分期（仅作负对照）")
            continue
        usable.append({"night": night, "session": session})
        print(f"{date:<12}{zepp:<26}{window:<22}可用（{len(session.samples)} 分钟，{hr} 条心率）")
        if not night.get("device"):
            unverified.append(date)
        overlap = _overlap_minutes(night, session)
        bounds = _window_bounds(night)
        if overlap is not None and bounds is not None:
            total = int(round((bounds[1] - bounds[0]) / 60))
            share = overlap / total if total else 0.0
            note = "  ← 缺口较大，差值只能当参考" if share < 0.9 else ""
            print(
                f"{'':<12}↳ Zepp 窗口 {total} 分钟，我们覆盖 {overlap} 分钟"
                f"（{share:.0%}），缺 {total - overlap} 分钟{note}"
            )

    if not usable:
        print("\n没有可用的夜晚：无法比较任何读法。")
        return 1

    print("\n=== 各读法的预测（分钟） ===")
    header = f"{'日期':<12}{'读法':<12}{'深睡':>14}{'REM':>14}{'浅睡':>14}"
    for item in usable:
        night, session = item["night"], item["session"]
        print(f"\n{header}")
        for name, fn in MODELS.items():
            pred = fn(session)
            print(
                f"{night['date']:<12}{name:<12}"
                f"{pred['deep']:>7.0f} / z{night['deep_min']:<4}"
                f"{pred['rem']:>7.0f} / z{night['rem_min']:<4}"
                f"{pred['light']:>7.0f} / z{night['light_min']:<4}"
            )
        best = _best_deep_cutoff(session, night["deep_min"])
        print(f"{night['date']:<12}{'sweep':<12}{best['minutes']:>7} / z{night['deep_min']:<4}"
              f"{'—':>14}{'—':>14}   (deep>t 时 t={best['cutoff']})")

    if unverified:
        print(
            f"\n注意：{', '.join(unverified)} 的 Zepp 数字**来源设备未记录**。"
            "Zepp Life 绑定多台设备时，日记页显示的不一定是你手上这块手环算的——"
            "01-07 就是这样：手环整夜未佩戴，App 却有完整分期。"
        )
        print("      请在 JSON 的 device 字段里记下设备名，否则无法确认这些基准是同一条数据流。")

    print("\n=== 固定阈值深睡列：逐夜最优 cut-off ===")
    for item in usable:
        night, session = item["night"], item["session"]
        best = _best_deep_cutoff(session, night["deep_min"])
        print(f"  {night['date']}  Zepp 深睡 {night['deep_min']:>3} 分钟 → "
              f"最好的固定阈值 deep > {best['cutoff']:>2}（得 {best['minutes']} 分钟，"
              f"差 {best['minutes'] - night['deep_min']:+d}）")

    print()
    if len(usable) < 2:
        print("结论：只有 %d 个可用夜晚。**不做拟合**——一个夜晚能对齐任何读法，"
              "在这里挑参数就是拿数据反推魔法值。" % len(usable))
        return 0

    print("结论：")
    for name, fn in MODELS.items():
        err = []
        for item in usable:
            night, session = item["night"], item["session"]
            pred = fn(session)
            err.append(abs(pred["deep"] - night["deep_min"]))
            err.append(abs(pred["rem"] - night["rem_min"]))
        print(f"  {name:<10} 深睡+REM 平均绝对误差 {sum(err) / len(err):6.1f} 分钟")
    cuts = {_best_deep_cutoff(i["session"], i["night"]["deep_min"])["cutoff"] for i in usable}
    print(f"  逐夜最优 cut-off: {sorted(cuts)}"
          + ("  ← 一致，固定阈值可用" if len(cuts) == 1 else "  ← 不一致，固定阈值不成立"))
    return 0


def _clock(timestamp: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(timestamp).strftime("%H:%M")


def _best_deep_cutoff(session: Any, target: int) -> dict[str, int]:
    """The ``deep > t`` cut-off whose minute count lands closest to *target*.

    Ties resolve to the lowest cut-off, i.e. the loosest threshold that still
    gives that count -- the range of equivalent thresholds matters more than the
    single point, so the caller should read this as "t >= this value".
    """
    values = [_scored(s)[0] for s in session.samples]
    best = {"cutoff": 0, "minutes": len(values)}
    for cutoff in range(0, 97):
        minutes = sum(1 for value in values if value > cutoff)
        if abs(minutes - target) < abs(best["minutes"] - target):
            best = {"cutoff": cutoff, "minutes": minutes}
    return best


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/final-check/mi_band.db")
    parser.add_argument("--zepp", default="data/zepp_nights.json",
                        help="JSON list of nights as reported by the app")
    args = parser.parse_args(argv)

    nights = json.loads(Path(args.zepp).read_text(encoding="utf-8"))
    if isinstance(nights, dict):
        nights = nights["nights"]
    return report(args.db, nights)


if __name__ == "__main__":
    raise SystemExit(main())
