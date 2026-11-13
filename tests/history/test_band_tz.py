"""Tests for --band-tz / BAND_TZ resolution and history timezone alignment."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from miband_monitor.cli import app as cli_app
from miband_monitor.config.defaults import ENV_BAND_TZ
from miband_monitor.history import resolve_band_tz
from miband_monitor.history.protocol import (
    decode_fetch_time,
    decode_short_calendar,
    encode_fetch_time,
    encode_short_calendar,
)


def test_resolve_band_tz_formats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_BAND_TZ, raising=False)
    assert resolve_band_tz("UTC") == timezone.utc
    assert resolve_band_tz("+08:00").utcoffset(None) == timedelta(hours=8)
    assert resolve_band_tz("-0530").utcoffset(None) == timedelta(hours=-5, minutes=-30)
    shanghai = resolve_band_tz("Asia/Shanghai")
    assert shanghai.utcoffset(datetime(2026, 11, 13)) == timedelta(hours=8)

    monkeypatch.setenv(ENV_BAND_TZ, "+09:00")
    assert resolve_band_tz().utcoffset(None) == timedelta(hours=9)

    with pytest.raises(ValueError):
        resolve_band_tz("Not/A_Real_Zone")
    with pytest.raises(ValueError):
        resolve_band_tz("+25:00")


def test_calendar_and_fetch_time_respect_band_tz() -> None:
    tz_cst = timezone(timedelta(hours=8))
    # 2026-11-13 20:00:00 in UTC+8 == 2026-11-13 12:00:00 UTC.
    raw_cal = (2026).to_bytes(2, "little") + bytes([11, 13, 20, 0])
    dt_utc = decode_short_calendar(raw_cal, tz=timezone.utc)
    dt_cst = decode_short_calendar(raw_cal, tz=tz_cst)
    assert dt_utc.timestamp() - dt_cst.timestamp() == 8 * 3600
    assert encode_short_calendar(dt_cst, tz=tz_cst) == raw_cal

    fetch_bytes = encode_fetch_time(dt_cst, tz=tz_cst)
    assert fetch_bytes == raw_cal + bytes([0x00, 32])  # +8h * 4 quarter-hours
    assert decode_fetch_time(fetch_bytes, tz=tz_cst) == dt_cst


@pytest.mark.asyncio
async def test_cli_rejects_invalid_band_tz() -> None:
    code = await cli_app.main(["--demo", "1", "--band-tz", "Invalid/Zone", "--storage", "none"])
    assert code == 1


@pytest.mark.asyncio
async def test_sync_history_warns_when_activity_ahead_of_utc(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging
    import time
    from miband_monitor.history import FetchDataType, sync_history

    future_ts = int(time.time()) + 8 * 3600
    calls = 0

    class _FakeFetcher:
        def __init__(self, _client, *, timeout: float = 30.0, band_tz=None) -> None:
            self.timeout = timeout
            self.band_tz = band_tz

        async def fetch_payload(self, data_type, _since):
            nonlocal calls
            calls += 1
            if data_type == FetchDataType.ACTIVITY and calls == 1:
                return bytes([0x10, 0x05, 0x0A, 72, 0, 0, 0, 0]), future_ts
            return b"", future_ts

    monkeypatch.setattr("miband_monitor.history.sync.BandHistoryFetcher", _FakeFetcher)
    caplog.set_level(logging.WARNING, logger="MiBand6History")
    result = await sync_history(object(), days=1, band_tz="UTC")
    assert len(result.activity) == 1
    assert any("ahead of host UTC" in rec.message and "--band-tz" in rec.message for rec in caplog.records)
