"""Sleep-kind interpretation and session-boundary detection."""

from __future__ import annotations

import pytest

from sleep_analysis import (
    KIND_NONWEAR,
    MIN_SESSION_MINUTES,
    detect_sessions,
    is_nonwear,
    is_sleep_kind,
    kind_code,
)
from tests.sleep.support import sample, series


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
