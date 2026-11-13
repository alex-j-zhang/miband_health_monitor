"""Text rendering of a sleep analysis result for the command line.

The report is deliberately explicit about what the band did and did not measure:
derived stages are labelled, a night with no heart rate says so instead of
showing empty stage rows, and the raw score-byte integration is kept apart from
stage minutes (it is evidence, not a stage mapping).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .constants import MINUTE, STAGE_AWAKE, STAGE_DEEP, STAGE_LIGHT


def _format_report(result: dict[str, Any]) -> str:
    lines = []
    totals = result["totals"]
    not_worn = result.get("not_worn") or []
    if not totals["sessions"]:
        if not_worn:
            spans = ", ".join(
                f"{span['start'][:16].replace('T', ' ')} → "
                f"{span['end'][:16].replace('T', ' ')} ({span['hours']} h)"
                for span in not_worn
            )
            return f"no sleep session detected; band not worn: {spans}"
        return "no sleep session detected"
    lines.append("Sleep analysis (derived from band activity samples)")
    lines.append(
        f"  sessions: {totals['sessions']}  in bed: {totals['minutes_in_bed']} min  "
        f"asleep: {totals['minutes_asleep']} min"
    )
    if not_worn:
        lines.append("  band not worn:")
        for span in not_worn:
            lines.append(
                f"    {span['start'][:16].replace('T', ' ')} → "
                f"{span['end'][:16].replace('T', ' ')}  {span['minutes']} min"
            )
    for session in result["_sessions"]:
        data = session.as_dict()
        started = data["start"][:16].replace("T", " ")
        ended = data["end"][:16].replace("T", " ")
        lines.append("")
        if not data["heart_rate_available"]:
            lines.append(
                f"  {started} -> {ended}  ({data['minutes_in_bed']} min in bed)"
            )
            lines.append(
                "    no heart rate recorded by the band for this session -- "
                "stages unavailable (wear the band tight and keep HR monitoring on)"
            )
            continue
        lines.append(
            f"  {started} -> {ended}  ({data['minutes_in_bed']} min in bed, "
            f"{data['minutes_asleep']} min asleep, efficiency {data['efficiency']:.0%})"
        )
        share = data["stage_share"]
        lines.append(
            f"    light {data['stage_minutes'].get(STAGE_LIGHT, 0)} min ({share[STAGE_LIGHT]:.0%})"
            f"  deep {data['stage_minutes'].get(STAGE_DEEP, 0)} min ({share[STAGE_DEEP]:.0%})"
            f"  awake {data['stage_minutes'].get(STAGE_AWAKE, 0)} min"
        )
        lines.append(
            f"    awakenings: {data['awakenings']}  longest stretch: {data['longest_sleep_minutes']} min"
        )
        band = data.get("band_scores") or {}
        if band.get("available"):
            minutes = band["minutes"]
            share = band["share"]
            # These are integrals of the raw score bytes, kept as evidence. They
            # are NOT stage minutes: the app reports 0 REM minutes on a night the
            # byte is nonzero for 458/485 minutes, and 87 REM minutes on a night
            # the byte is zero for all 396 (docs §8).
            lines.append(
                f"    raw score bytes ({band['scored_minutes']}/{band['window_minutes']} scored, "
                f"0-{band['scale']}/min, not stage minutes): "
                f"deep {minutes['deep']:.0f}"
                f"  rem {minutes['rem']:.0f}"
                f"  light {minutes['light']:.0f}"
                f"  unaccounted {band['residual_minutes']:.0f}"
            )
        hypnogram = session.hypnogram()
        lines.append(f"    hypnogram ({len(hypnogram)} min):")
        for offset in range(0, len(hypnogram), 60):
            clock = datetime.fromtimestamp(session.start + offset * MINUTE).strftime("%H:%M")
            lines.append(f"      {clock} {hypnogram[offset:offset + 60]}")
    lines.append("")
    lines.append("  legend: '.' awake  '-' light  '=' deep  '~' rem   (derived, not band-reported)")
    return "\n".join(lines)
