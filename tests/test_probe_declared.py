"""Tests for the read-only retention probe (``tools/probe_declared.py``).

The probe answers one question -- "how far back does the band still have data?"
-- and the whole point is that it must never turn into a fetch. Two things need
pinning: the response decoding (a zero-filled calendar must not be mistaken for
"the data starts at the moment I asked about"), and the fact that the probe
only ever writes the start-date command, never ``0x02``/``0x03``.
"""

from datetime import datetime, timedelta
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import probe_declared as probe  # noqa: E402
from band_history import (  # noqa: E402
    COMMAND_ACK,
    COMMAND_FETCH_DATA,
    COMMAND_START_DATE,
    RESPONSE,
    FetchDataType,
)

SINCE = datetime(2026, 11, 1, 16, 32).astimezone()


class FakeFetcher:
    """Records writes and replays one canned start-date response."""

    def __init__(self, response: bytes) -> None:
        self.response = response
        self.writes: list[bytes] = []
        self._fetch_queue = None

    def _drain(self) -> None:
        pass

    async def _write(self, payload: bytes) -> None:
        self.writes.append(bytes(payload))

    async def _next(self, _queue, _what: str) -> bytes:
        return self.response


def build_response(count: int, *, calendar: bytes | None = None) -> bytes:
    body = count.to_bytes(4, "little")
    if calendar is None:
        calendar = b"\x00" * 6
    return bytes([RESPONSE, COMMAND_START_DATE, 0x01]) + body + calendar + b"\x00\x20"


@pytest.mark.asyncio
async def test_probe_reports_declared_minutes_without_fetching():
    fetcher = FakeFetcher(build_response(2406))
    reading, error = await probe.probe(fetcher, FetchDataType.ACTIVITY, SINCE)

    assert error is None
    declared, start_ts = reading
    assert declared == 2406
    # The calendar was zero-filled, so nothing usable is echoed back; falling
    # back to ``since`` here would invent an anchor the band never sent.
    assert start_ts is None

    assert len(fetcher.writes) == 1
    assert fetcher.writes[0][:2] == bytes([COMMAND_START_DATE, FetchDataType.ACTIVITY])
    assert COMMAND_FETCH_DATA not in fetcher.writes[0]
    assert COMMAND_ACK not in fetcher.writes[0]


@pytest.mark.asyncio
async def test_probe_decodes_echoed_calendar_when_present():
    # 2026-11-07 00:25 local
    calendar = (2026).to_bytes(2, "little") + bytes([11, 7, 0, 25])
    fetcher = FakeFetcher(build_response(418, calendar=calendar))
    reading, error = await probe.probe(fetcher, FetchDataType.ACTIVITY, SINCE)

    assert error is None
    declared, start_ts = reading
    assert declared == 418
    assert datetime.fromtimestamp(start_ts) == datetime(2026, 11, 7, 0, 25)


@pytest.mark.asyncio
async def test_probe_surfaces_firmware_rejection():
    response = bytes([RESPONSE, COMMAND_START_DATE, 0x02]) + b"\x00" * 10
    fetcher = FakeFetcher(response)
    reading, error = await probe.probe(fetcher, FetchDataType.ACTIVITY, SINCE)

    assert reading is None
    assert "0x02" in error


def test_default_grid_stays_inside_a_sane_week():
    assert probe.DEFAULT_HOURS == sorted(probe.DEFAULT_HOURS, reverse=True)
    assert max(probe.DEFAULT_HOURS) <= 24 * 7
    assert min(probe.DEFAULT_HOURS) > 0
    # The window measured on hardware is ~40 h; the grid has to bracket it or
    # the probe cannot show where the band starts answering "less than asked".
    assert any(h < 40 for h in probe.DEFAULT_HOURS)
    assert any(h > 40 for h in probe.DEFAULT_HOURS)


def test_grid_rows_are_ordered_recent_last():
    hours = [168, 24]
    stamps = [SINCE - timedelta(hours=h) for h in hours]
    assert stamps == sorted(stamps)
