"""Thresholds, stage vocabulary and band ``kind`` codes for sleep staging.

These values are the unchanged behavior contract of the derived sleep model:
session detection, staging cut-offs and report labels must match the historical
``sleep_analysis.py`` exactly. The comments record the hardware observation or
Zepp Life comparison that fixed each number, so values are not re-tuned without
the same evidence. Physiological heart-rate bounds are shared with the
dashboards through :mod:`miband_monitor.config.health`.
"""

from __future__ import annotations

from miband_monitor.config.health import HR_MAX, HR_MIN, HR_UNAVAILABLE

# ---------------------------------------------------------------------------
# Time base and stage vocabulary
# ---------------------------------------------------------------------------
#: Seconds in one band history minute.
MINUTE = 60.0

STAGE_AWAKE = "awake"
STAGE_LIGHT = "light"
STAGE_DEEP = "deep"
#: Only used when a session carries no usable heart rate at all -- reporting it as
#: "light sleep" would silently invent a staged night.
STAGE_UNKNOWN = "unknown"
STAGES: tuple[str, ...] = (STAGE_AWAKE, STAGE_LIGHT, STAGE_DEEP, STAGE_UNKNOWN)

# ---------------------------------------------------------------------------
# Band ``kind`` byte decoding
# ---------------------------------------------------------------------------
#: ``kind & 0xF0 == 0xF0`` is the band's sleep family (240/243/249/250 on real
#: hardware). It never matches daytime samples.
SLEEP_KIND_MASK = 0xF0
SLEEP_KIND_VALUE = 0xF0

#: The low nibble of ``kind`` is the activity code. Gadgetbridge reads the same
#: byte as ``rawKind & 0x0f`` (``MiBand2SampleProvider.postprocess``) and names
#: the codes in ``HuamiConst``: 0 no change, 1 activity, 3 *not worn*,
#: 9 light sleep, 10 ignore, 11 deep sleep, 12 wake up.
KIND_CODE_MASK = 0x0F
KIND_NO_CHANGE = 0x0
KIND_ACTIVITY = 0x01
KIND_NONWEAR = 0x03
KIND_LIGHT_SLEEP = 0x09
KIND_IGNORE = 0x0A
KIND_DEEP_SLEEP = 0x0B
KIND_WAKE_UP = 0x0C

# ---------------------------------------------------------------------------
# Movement and awake criteria
# ---------------------------------------------------------------------------
#: ``intensity`` at or above this counts as body movement for the activity strip
#: (daytime walking sits at 50-90, sleep at 0-12).
MOVEMENT_INTENSITY = 20

#: A minute with at least one step is awake. Steps are the one movement signal
#: Zepp Life's numbers reproduce exactly: on 2020-01-06 it reports 20 awake
#: minutes and the capture holds 17, while an ``intensity >= 20`` rule would
#: claim 72 and lose 55 minutes of real sleep.
AWAKE_STEPS = 1

# ---------------------------------------------------------------------------
# Session detection
# ---------------------------------------------------------------------------
#: Quiet gaps up to this long are bridged inside one sleep session (a mid-night
#: wake-up of a few minutes should not split the night in two).
SESSION_GAP_MINUTES = 20
#: Sessions shorter than this are discarded as naps/artefacts.
MIN_SESSION_MINUTES = 20
#: How far a session may grow beyond the band's own "sleep" state, in minutes.
#: 60 covers the tail of a real night (the band stops reporting sleep at 04:55
#: while the app keeps counting until ~06:06) without reaching into the day.
ANCHOR_MINUTES = 60
#: A session needs this many minutes where the band itself said sleep, so that a
#: lone anchor minute (putting the band back on, then walking) cannot grow into an
#: hour-long "night". Real nights have hundreds.
MIN_ANCHOR_MINUTES = 10
#: Non-wear runs shorter than this are not worth reporting (a 2-minute bathroom
#: trip with the band left on the nightstand is not "the band was off").
MIN_NONWEAR_MINUTES = 30

# ---------------------------------------------------------------------------
# Staging: heart-rate level rank and short-term variability rank
# ---------------------------------------------------------------------------
#: Half-width (minutes) of the rolling window used for heart-rate variability.
HR_WINDOW_MINUTES = 7
#: Deep sleep ranks low on both heart rate and its short-term variability.
#:
#: Calibrated against Zepp Life's 2020-01-06 numbers, which is the only external
#: reference this device can offer: the app reports 44 deep minutes (8 %). The
#: previous 0.30/0.55 cut claimed 76 minutes (16 %) for the same night, clearly
#: over-attributing; 0.20/0.45 lands on 50 (11 %). One night is one night -- these
#: are provisional and should be revisited once more Zepp nights are available.
DEEP_HR_RANK = 0.20
DEEP_SD_RANK = 0.45
#: Stage runs shorter than this are merged into their neighbours.
MIN_STAGE_MINUTES = 3
