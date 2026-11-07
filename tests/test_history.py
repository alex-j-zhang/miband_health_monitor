"""Tests for the experimental band-side history/SpO2 fetch pipeline."""

import sqlite3
from datetime import datetime, timedelta

import pytest

from band_history import (
    ACTIVITY_SAMPLE_SIZE,
    SPO2_RECORD_SIZE,
    BandHistoryFetcher,
    FetchDataType,
    HistoryFetchError,
    HistoryParseError,
    decode_fetch_time,
    decode_short_calendar,
    encode_fetch_time,
    encode_short_calendar,
    parse_activity,
    parse_spo2_normal,
    summarize_history_result,
    sync_history,
)
from history_report import format_report, summarize
from miband_client import parse_args
from sim_band import SimulatedBand, make_simulated_client
from storage import TelemetryStore, read_jsonl

MAC = "C0:FF:EE:C0:FF:EE"
AUTH_KEY = "0123456789abcdef0123456789abcdef"
AUTH_KEY_BYTES = bytes.fromhex(AUTH_KEY)


async def _connected_client(band: SimulatedBand):
    client = make_simulated_client(MAC, AUTH_KEY, band, scanner_address=MAC)
    await client.connect_and_authenticate(scan_timeout=2.0)
    return client


# ---------------------------------------------------------------------------
# Time encoding / payload parsers
# ---------------------------------------------------------------------------
def test_short_calendar_roundtrip():
    moment = datetime(2026, 2, 3, 4, 5)
    raw = encode_short_calendar(moment)
    assert len(raw) == 6
    assert decode_short_calendar(raw).replace(tzinfo=None) == moment

    fetch_bytes = encode_fetch_time(moment)
    assert len(fetch_bytes) == 8
    assert decode_fetch_time(fetch_bytes).replace(tzinfo=None) == moment


def test_parse_spo2_normal_payload():
    payload = bytearray([0x02])
    payload += (1000).to_bytes(4, "little") + bytes([97]) + b"\x00" * 60
    # Automatic measurement: signed byte is the value minus 128 (96 -> -32 / 0xE0).
    payload += (1060).to_bytes(4, "little") + bytes([(96 - 128) & 0xFF]) + b"\x00" * 60

    samples = parse_spo2_normal(bytes(payload))
    assert len(samples) == 2
    assert samples[0] == {"ts": 1000.0, "spo2": 97, "auto_measure": False}
    assert samples[1]["spo2"] == 96
    assert samples[1]["auto_measure"] is True


def test_parse_spo2_rejects_bad_payloads():
    with pytest.raises(HistoryParseError):
        parse_spo2_normal(b"\x03" + b"\x00" * SPO2_RECORD_SIZE)  # wrong version
    with pytest.raises(HistoryParseError):
        parse_spo2_normal(b"\x02" + b"\x00" * (SPO2_RECORD_SIZE - 1))  # bad length


def test_parse_activity_payload():
    payload = bytes(
        [
            0x01, 10, 12, 70, 0, 0, 0, 0,
            0x01, 20, 30, 74, 0, 0, 0, 0,
        ]
    )
    assert len(payload) == 2 * ACTIVITY_SAMPLE_SIZE
    samples = parse_activity(payload, start_timestamp=1000.0)
    assert len(samples) == 2
    assert samples[0]["steps"] == 12
    assert samples[1]["heart_rate"] == 74
    assert samples[1]["ts"] == 1060.0  # one minute later

    with pytest.raises(HistoryParseError):
        parse_activity(b"\x00" * 7, start_timestamp=0.0)


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
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES)
    client = await _connected_client(band)
    try:
        fetcher = BandHistoryFetcher(client, timeout=2.0)
        with pytest.raises(HistoryFetchError, match="rejected"):
            await fetcher.fetch_payload(FetchDataType.SPO2_SLEEP, datetime.now())
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


# ---------------------------------------------------------------------------
# sync_history + storage integration
# ---------------------------------------------------------------------------
async def test_sync_history_persists_spo2_and_activity(tmp_path):
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES)
    client = await _connected_client(band)
    store = TelemetryStore(tmp_path, backend="both")
    try:
        result = await sync_history(
            client,
            days=1,
            max_rounds=2,
            timeout=3.0,
            on_spo2=lambda sample: store.record_spo2(
                sample["spo2"],
                auto_measure=sample["auto_measure"],
                timestamp=sample["ts"],
            ),
            on_activity=lambda sample: store.record_activity_history(
                sample, timestamp=sample["ts"]
            ),
        )
    finally:
        await client.disconnect()
        store.close()

    assert result.errors == []
    assert len(result.spo2) == len(band.history_spo2)
    assert len(result.activity) == len(band.history_activity)
    assert "SpO2" in summarize_history_result(result)

    conn = sqlite3.connect(tmp_path / "mi_band.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM spo2").fetchone()[0] == len(band.history_spo2)
        assert conn.execute("SELECT COUNT(*) FROM activity_history").fetchone()[0] == len(
            band.history_activity
        )
        spo2_rows = conn.execute("SELECT spo2, auto_measure FROM spo2 ORDER BY ts").fetchall()
    finally:
        conn.close()
    assert [row[0] for row in spo2_rows] == [s["spo2"] for s in band.history_spo2]
    assert any(row[1] for row in spo2_rows)

    spo2_jsonl = read_jsonl(tmp_path / "spo2.jsonl")
    assert len(spo2_jsonl) == len(band.history_spo2)
    assert all("ts" in row and "iso_time" in row for row in spo2_jsonl)
    activity_jsonl = read_jsonl(tmp_path / "activity_history.jsonl")
    assert len(activity_jsonl) == len(band.history_activity)


async def test_sync_history_reports_errors_without_raising(tmp_path):
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES, drop_history_packets=(0,))
    client = await _connected_client(band)
    try:
        store = TelemetryStore(tmp_path, backend="sqlite")
        try:
            result = await sync_history(
                client,
                days=1,
                max_rounds=1,
                timeout=2.0,
                on_spo2=lambda sample: store.record_spo2(
                    sample["spo2"], sample["auto_measure"], timestamp=sample["ts"]
                ),
            )
        finally:
            store.close()
    finally:
        await client.disconnect()

    assert result.errors, "packet loss should be reported as an error"
    assert result.spo2 == []


# ---------------------------------------------------------------------------
# Local history report
# ---------------------------------------------------------------------------
async def test_history_report_summarizes_database(tmp_path):
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES)
    client = await _connected_client(band)
    store = TelemetryStore(tmp_path, backend="sqlite")
    try:
        await sync_history(
            client,
            days=1,
            max_rounds=1,
            timeout=3.0,
            on_spo2=lambda sample: store.record_spo2(
                sample["spo2"], sample["auto_measure"], timestamp=sample["ts"]
            ),
            on_activity=lambda sample: store.record_activity_history(
                sample, timestamp=sample["ts"]
            ),
        )
        store.record_heart_rate(72)
    finally:
        await client.disconnect()
        store.close()

    summary = summarize(tmp_path / "mi_band.db", hours=24)
    assert summary["tables"]["spo2"]["count"] == len(band.history_spo2)
    assert summary["tables"]["activity_history"]["minutes"] == len(band.history_activity)
    assert summary["tables"]["activity_history"]["total_steps"] > 0
    assert summary["tables"]["heart_rate"]["avg"] == 72.0

    report = format_report(summary)
    assert "SpO2" in report
    assert "activity" in report
    assert "heart rate" in report


def test_history_report_missing_database(tmp_path):
    with pytest.raises(FileNotFoundError):
        summarize(tmp_path / "missing.db")


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------
def test_cli_sync_history_argument():
    args = parse_args(["--demo", "1", "--sync-history"])
    assert args.sync_history == 7
    args = parse_args(["--demo", "1", "--sync-history", "3"])
    assert args.sync_history == 3
    args = parse_args(["--demo", "1"])
    assert args.sync_history is None
