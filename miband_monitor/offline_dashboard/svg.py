"""Inline SVG hypnogram rendering for the offline dashboard.

Drawing server-side (and run-length encoded) keeps the chart crisp at any width
and avoids the canvas resize dance the live charts need. The stage palette is
also injected into the page as JavaScript colours, so it lives here next to the
lanes it must agree with.
"""

from __future__ import annotations

from typing import Sequence

from .constants import STAGE_AWAKE, STAGE_DEEP, STAGE_LIGHT, STAGE_UNKNOWN
from .strings import FLAT_IN_BED_LABEL, HYPNOGRAM_LABEL

#: Lane index for the hypnogram, top row is the deepest stage (awake at bottom).
STAGE_LANES = {STAGE_DEEP: 0, STAGE_LIGHT: 1, STAGE_AWAKE: 2}

#: Geometry of the flat in-bed band shown for a night without heart rate: no
#: stages exist, so stacking four empty lanes would pretend data exists.
FLAT_BAND_HEIGHT = 44
FLAT_BAND_Y = 12
FLAT_BAND_BAR_HEIGHT = 20
FLAT_BAND_FILL = "#33404f"


def _stage_style() -> dict[str, str]:
    return {
        STAGE_DEEP: "#8b5cf6",
        STAGE_LIGHT: "#38bdf8",
        STAGE_AWAKE: "#64748b",
        STAGE_UNKNOWN: "#2a323c",
    }


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
        f'preserveAspectRatio="none" role="img" aria-label="{HYPNOGRAM_LABEL}">'
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


def flat_in_bed_svg(minutes: int) -> str:
    """One flat in-bed band for a night the band recorded no heart rate for.

    Without heart rate there is nothing to stack, so the whole in-bed span is
    drawn as a single band instead of four empty lanes.
    """
    width = max(1, minutes)
    return (
        f'<svg class="hypno" viewBox="0 0 {width} {FLAT_BAND_HEIGHT}" '
        f'preserveAspectRatio="none" role="img" aria-label="{FLAT_IN_BED_LABEL}">'
        f'<rect x="0" y="{FLAT_BAND_Y}" width="{width}" height="{FLAT_BAND_BAR_HEIGHT}" '
        f'fill="{FLAT_BAND_FILL}"/></svg>'
    )
