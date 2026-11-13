"""Simulated band-side fetch protocol and fetcher lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from band_history import (
    BandHistoryFetcher,
    FetchDataType,
    HistoryFetchError,
    parse_activity,
    parse_spo2_normal,
    parse_spo2_sleep,
)
from sim_band import SimulatedBand
from tests.support import (
    AUTH_KEY_BYTES,
    connect_simulated_client as _connected_client,
)


# ---------------------------------------------------------------------------
# Simulated fetch protocol
# ---------------------------------------------------------------------------
async def test_simulated_spo2_fetch_roundtrip():
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES)
    client = await _connected_client(band)
    try:
        fetcher = BandHistoryFetcher(client, timeout=3.0)
        since = datetime.now() - timedelta(hours=2)
        payload, start_ts = await fetcher.fetch_payload(FetchDataType.SPO2_NORMAL, since)
        samples = parse_spo2_normal(payload)
        assert len(samples) == len(band.history_spo2)
        assert [s["spo2"] for s in samples] == [s["spo2"] for s in band.history_spo2]
        assert start_ts > 0
    finally:
        await client.disconnect()


async def test_simulated_activity_fetch_roundtrip():
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES)
    client = await _connected_client(band)
    try:
        fetcher = BandHistoryFetcher(client, timeout=3.0)
        since = datetime.now() - timedelta(hours=2)
        payload, start_ts = await fetcher.fetch_payload(FetchDataType.ACTIVITY, since)
        samples = parse_activity(payload, start_ts)
        assert len(samples) == len(band.history_activity)
        assert [s["steps"] for s in samples] == [s["steps"] for s in band.history_activity]
    finally:
        await client.disconnect()


async def test_simulated_fetch_rejects_unsupported_type():
    """0x26 is supported now; use a type this client genuinely does not handle."""
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES)
    client = await _connected_client(band)
    try:
        fetcher = BandHistoryFetcher(client, timeout=2.0)
        with pytest.raises(HistoryFetchError, match="rejected"):
            await fetcher.fetch_payload(0x2C, datetime.now())  # STATISTICS upstream
    finally:
        await client.disconnect()


async def test_simulated_spo2_sleep_fetch_roundtrip():
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES)
    client = await _connected_client(band)
    try:
        fetcher = BandHistoryFetcher(client, timeout=3.0)
        since = datetime.now() - timedelta(hours=2)
        payload, _start_ts = await fetcher.fetch_payload(FetchDataType.SPO2_SLEEP, since)
        samples = parse_spo2_sleep(payload)
        assert len(samples) == len(band.history_spo2_sleep)
        assert [s["spo2"] for s in samples] == [s["spo2"] for s in band.history_spo2_sleep]
    finally:
        await client.disconnect()


async def test_simulated_empty_fetch_keeps_requested_window():
    """A "nothing new" answer must not blow up on the epoch start timestamp.

    Real hardware replies to an empty fetch with ``10 01 01 00 00 00 00`` plus a
    1970-01-01 calendar; converting that on Windows raised ``OSError [Errno 22]``
    while only logging, so the whole sync aborted with no usable payload.
    """
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES)
    band.history_spo2.clear()
    client = await _connected_client(band)
    try:
        fetcher = BandHistoryFetcher(client, timeout=3.0)
        since = (datetime.now() - timedelta(hours=2)).replace(microsecond=0)
        payload, start_ts = await fetcher.fetch_payload(FetchDataType.SPO2_NORMAL, since)
        assert payload == b""
        assert start_ts == pytest.approx(since.timestamp())
    finally:
        await client.disconnect()


async def test_simulated_fetch_detects_packet_loss():
    # Three SpO2 records => 196-byte payload => 11 chunks; drop chunk #1.
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES, drop_history_packets=(1,))
    client = await _connected_client(band)
    try:
        fetcher = BandHistoryFetcher(client, timeout=2.0)
        with pytest.raises(HistoryFetchError, match="counter mismatch"):
            await fetcher.fetch_payload(
                FetchDataType.SPO2_NORMAL, datetime.now() - timedelta(hours=1)
            )
    finally:
        await client.disconnect()


async def test_simulated_fetch_empty_history():
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES, history_spo2=[], history_activity=[])
    client = await _connected_client(band)
    try:
        fetcher = BandHistoryFetcher(client, timeout=2.0)
        payload, _start = await fetcher.fetch_payload(
            FetchDataType.SPO2_NORMAL, datetime.now() - timedelta(hours=1)
        )
        assert payload == b""
    finally:
        await client.disconnect()


async def test_fetcher_built_before_connect_still_finds_the_link():
    """A fetcher may be constructed before the wrapper owns its BLE client.

    ``MiBand6Client.client`` is ``None`` until ``connect_and_authenticate()``
    finishes, so resolving once in ``__init__`` would keep the wrapper and fail
    later with ``AttributeError: 'MiBand6Client' object has no attribute
    'start_notify'`` -- which is exactly what the first hardware probe of the
    ``0x48`` sleep-session type hit.
    """
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES)
    client = await _connected_client(band)
    try:
        fetcher = BandHistoryFetcher(client, timeout=3.0)
        # Simulate the ordering: hand the fetcher the wrapper, then connect.
        fetcher.client = client
        assert not hasattr(fetcher.client, "start_notify")
        payload, _ = await fetcher.fetch_payload(FetchDataType.ACTIVITY, datetime.now() - timedelta(hours=2))
        assert payload
        assert hasattr(fetcher.client, "start_notify")
    finally:
        await client.disconnect()


async def test_fetcher_refuses_to_fetch_without_a_link():
    class NotConnected:
        client = None

    fetcher = BandHistoryFetcher(NotConnected(), timeout=1.0)
    with pytest.raises(HistoryFetchError, match="not connected"):
        await fetcher._open()
