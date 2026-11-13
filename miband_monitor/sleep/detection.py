"""Sleep-session and non-wear span detection from band activity samples.

Two signals, in two roles: the band's own ``kind`` state machine anchors *where*
a night is, and stillness extends it outward. Extending on stillness alone would
swallow an entire day spent sitting down; anchoring on stillness alone loses the
edges of a real night. Non-wear (``0xF3``) stretches are reported separately so
an off-wrist night is never printed as hours in bed.
"""

from __future__ import annotations

from typing import Sequence

from .constants import (
    ANCHOR_MINUTES,
    MIN_ANCHOR_MINUTES,
    MIN_NONWEAR_MINUTES,
    MIN_SESSION_MINUTES,
    MINUTE,
    SESSION_GAP_MINUTES,
)
from .samples import is_asleep_minute, is_nonwear, is_sleep_kind, local_iso


def detect_sessions(
    samples: Sequence[dict],
    *,
    gap_minutes: int = SESSION_GAP_MINUTES,
    min_minutes: int = MIN_SESSION_MINUTES,
    anchor_minutes: int = ANCHOR_MINUTES,
) -> list[list[dict]]:
    """Group samples into sleep sessions.

    Two signals, in two roles:

    * ``kind`` **anchors**: the band's own state machine says "sleep" for part of
      every real night and never during the day, so it decides *where* a night
      is. Runs of it are clustered, allowing ``gap_minutes`` of wake-up between.
    * **stillness extends**: from the first and last anchor minute the session
      grows outward while the adjacent minute has no steps and the wrist is on,
      but never more than ``anchor_minutes`` past the anchor.

    Extending on stillness alone would swallow an entire day spent sitting down
    (a 13-hour "night" on the real capture); anchoring on stillness alone loses
    124 minutes at the two ends of a real night. Together they reproduce the app
    -- see the package docstring.
    """
    anchors = [index for index, sample in enumerate(samples) if is_sleep_kind(sample["kind"])]
    if not anchors:
        return []

    # Cluster the anchor minutes; a wake-up shorter than gap_minutes stays inside.
    clusters: list[list[int]] = [[anchors[0]]]
    for index in anchors[1:]:
        if samples[index]["ts"] - samples[clusters[-1][-1]]["ts"] > gap_minutes * MINUTE:
            clusters.append([index])
        else:
            clusters[-1].append(index)

    def extend(start: int, direction: int, limit_ts: float) -> int:
        """Walk outward from ``start``, bridging wake-ups shorter than the gap.

        A single step minute must not end the night -- the person turns over and
        keeps sleeping, and the app keeps counting. Extension stops at the first
        stretch longer than ``gap_minutes`` with nothing still in it, or once it
        is ``anchor_minutes`` away from the anchor.
        """
        current = start
        while True:
            found = None
            probe = current + direction
            while 0 <= probe < len(samples):
                if abs(samples[probe]["ts"] - samples[current]["ts"]) > gap_minutes * MINUTE:
                    break
                if is_asleep_minute(samples[probe]):
                    found = probe
                    break
                probe += direction
            if found is None:
                return current
            if abs(samples[found]["ts"] - limit_ts) > anchor_minutes * MINUTE:
                return current
            current = found

    sessions: list[list[dict]] = []
    for cluster in clusters:
        first, last = cluster[0], cluster[-1]
        left = extend(first, -1, samples[first]["ts"])
        right = extend(last, +1, samples[last]["ts"])
        session = list(samples[left:right + 1])
        asleep = sum(1 for sample in session if is_asleep_minute(sample))
        if asleep < min_minutes or len(cluster) < MIN_ANCHOR_MINUTES:
            continue
        if sessions and session[0]["ts"] - sessions[-1][-1]["ts"] <= gap_minutes * MINUTE:
            # The extension of the previous cluster reached up to this one: one
            # night, not two (a 2-minute roll-over must not split the report).
            sessions[-1].extend(
                sample for sample in session if sample["ts"] > sessions[-1][-1]["ts"]
            )
        else:
            sessions.append(session)
    return sessions


def detect_nonwear_spans(
    samples: Sequence[dict],
    *,
    min_minutes: int = MIN_NONWEAR_MINUTES,
) -> list[dict]:
    """Long stretches the band spent off the wrist, oldest first."""
    spans: list[dict] = []
    run: list[dict] = []

    def close() -> None:
        nonlocal run
        if len(run) >= min_minutes:
            spans.append(
                {
                    "start": local_iso(run[0]["ts"]),
                    "end": local_iso(run[-1]["ts"]),
                    "minutes": len(run),
                    "hours": round(len(run) / 60, 1),
                }
            )
        run = []

    for sample in samples:
        if is_nonwear(sample["kind"]):
            run.append(sample)
        elif run:
            close()
    if run:
        close()
    return spans
