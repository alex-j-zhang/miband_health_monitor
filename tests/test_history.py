"""Tests for the experimental band-side history/SpO2 fetch pipeline."""

import sqlite3
from datetime import datetime, timedelta

import pytest

from band_history import (
    ACTIVITY_SAMPLE_SIZE,
    SPO2_RECORD_SIZE,
    SPO2_SLEEP_RECORD_SIZE,
    BandHistoryFetcher,
    FetchDataType,
    HistoryFetchError,
    HistoryParseError,
    HistorySyncResult,
    _trim_to_records,
    decode_fetch_time,
    decode_short_calendar,
    encode_fetch_time,
    encode_short_calendar,
    parse_activity,
    parse_spo2_normal,
    parse_spo2_sleep,
    summarize_history_result,
    sync_history,
)
from history_report import format_report, summarize
from miband_client import parse_args
from sim_band import SimulatedBand, _spo2_sleep_record, make_simulated_client
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


async def test_sync_history_spo2_sleep_is_opt_in():
    """Default sync must never request 0x26; the opt-in path delivers samples."""
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES)
    client = await _connected_client(band)
    try:
        baseline = await sync_history(client, days=1, timeout=3.0, max_rounds=1)
        assert baseline.spo2_sleep == []
        assert "spo2_sleep" not in baseline.rounds

        seen: list[dict] = []
        result = await sync_history(
            client,
            days=1,
            include_spo2_sleep=True,
            on_spo2_sleep=seen.append,
            timeout=3.0,
            max_rounds=1,
        )
        assert len(result.spo2_sleep) == len(band.history_spo2_sleep)
        assert seen == result.spo2_sleep
        assert "spo2_sleep" in result.rounds
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


def test_parse_spo2_normal_real_hardware_payload():
    """Decode a byte-exact SpO2 payload captured from a Mi Band 6."""
    tokens = (
        "02 cf 65 ee 6a 64 43 00 00 00 19 00 4e d6 ee 6a 0c be 08 00 4e d6 ee 6a"
    ).split()
    raw = bytes(int(token, 16) for token in tokens) + b"\x00" * 25 + b"\x01" + b"\x00" * 16
    assert len(raw) == 1 + SPO2_RECORD_SIZE

    samples = parse_spo2_normal(raw)
    assert samples == [
        {"ts": 1794008527.0, "spo2": 100, "auto_measure": False},
    ]


def test_parse_spo2_sleep_keeps_the_30_byte_layout():
    """0x26 has its own record size -- sharing the 65-byte normal layout is a bug."""
    assert SPO2_SLEEP_RECORD_SIZE == 30
    record = (
        _spo2_sleep_record(1577836800, 97, 60)[:6]
        + bytes(range(1, 7))       # spo2_high
        + bytes(range(7, 13))      # spo2_low
        + bytes(range(13, 21))     # signal_quality
        + bytes(range(21, 25))     # extend
    )
    assert len(record) == 30
    payload = b"\x02" + record + _spo2_sleep_record(1577836860, 95, 30)

    samples = parse_spo2_sleep(payload)
    assert len(samples) == 2
    assert samples[0] == {
        "ts": 1577836800.0,
        "spo2": 97,
        "duration": 60,
        "spo2_high": [1, 2, 3, 4, 5, 6],
        "spo2_low": [7, 8, 9, 10, 11, 12],
        "signal_quality": [13, 14, 15, 16, 17, 18, 19, 20],
        "extend": [21, 22, 23, 24],
        "source": "sleep",
    }
    assert samples[1]["spo2"] == 95
    assert samples[1]["ts"] == 1577836860.0


def test_parse_spo2_sleep_synthetic_fixture():
    """Byte-exact synthetic fixture for the verified 0x26 layout.

    No captured bytes live in the repository: real payloads stay in the ignored
    ``validation/`` directory. This fixture is hand-built from the documented
    layout (version 2 + N x 30 B, records 60 s apart) and pins every field
    offset, so the parser cannot silently drift from the spec.
    """
    raw = bytes.fromhex(
        "02"
        "00e10b5e6110626262000000616161000000010203040506070800000000"
        "3ce10b5e6220636363000000626262000000080706050403020101020304"
    )
    assert len(raw) == 1 + 2 * SPO2_SLEEP_RECORD_SIZE

    samples = parse_spo2_sleep(raw)
    assert len(samples) == 2
    assert samples[0]["spo2"] == 97
    assert samples[0]["duration"] == 16
    assert samples[0]["spo2_high"] == [98, 98, 98, 0, 0, 0]
    assert samples[0]["spo2_low"] == [97, 97, 97, 0, 0, 0]
    assert samples[0]["signal_quality"] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert samples[0]["extend"] == [0, 0, 0, 0]
    assert samples[1]["spo2"] == 98
    assert samples[1]["duration"] == 32
    assert samples[1]["ts"] - samples[0]["ts"] == 60


def test_parse_spo2_sleep_rejects_bad_payloads():
    assert parse_spo2_sleep(b"") == []
    with pytest.raises(HistoryParseError, match="version"):
        parse_spo2_sleep(b"\x03" + b"\x00" * SPO2_SLEEP_RECORD_SIZE)
    with pytest.raises(HistoryParseError, match="multiple of 30"):
        parse_spo2_sleep(b"\x02" + b"\x00" * 29)


def test_trim_to_records_uses_the_sleep_record_size():
    """A padded final chunk must be trimmed at 30 bytes, not 65."""
    payload = b"\x02" + _spo2_sleep_record(1577836800, 98, 60) + b"\xaa" * 7
    trimmed = _trim_to_records(FetchDataType.SPO2_SLEEP, payload)
    assert len(trimmed) == 1 + SPO2_SLEEP_RECORD_SIZE
    assert len(parse_spo2_sleep(trimmed)) == 1

    normal = b"\x02" + b"\x11" * SPO2_RECORD_SIZE + b"\xbb" * 3
    assert len(_trim_to_records(FetchDataType.SPO2_NORMAL, normal)) == 1 + SPO2_RECORD_SIZE


def test_summarize_marks_sleep_spo2_as_experimental():
    default = HistorySyncResult()
    assert "sleep SpO2" not in summarize_history_result(default)

    enabled = HistorySyncResult(spo2_sleep=[{"ts": 1.0, "spo2": 97}])
    summary = summarize_history_result(enabled)
    assert "1 sleep SpO2 sample(s) [experimental]" in summary


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


def test_activity_average_ignores_unavailable_heart_rate(tmp_path):
    """255 is the band's "measurement unavailable" sentinel, not a BPM value.

    Real 2-day data carries it in roughly a third of the minutes; averaging it
    in reported 172 BPM where the true average was 71.
    """
    def sample(heart_rate: int, steps: int = 0) -> dict:
        return {
            "kind": 1,
            "intensity": 5,
            "steps": steps,
            "heart_rate": heart_rate,
            "unknown1": 0,
            "sleep": 0,
            "deep_sleep": 0,
            "rem_sleep": 0,
        }

    store = TelemetryStore(tmp_path, backend="sqlite")
    try:
        for heart_rate in (60, 80, 255, 255, 0):
            store.record_activity_history(sample(heart_rate))
    finally:
        store.close()

    summary = summarize(tmp_path / "mi_band.db", hours=48)
    activity = summary["tables"]["activity_history"]
    assert activity["minutes"] == 5
    assert activity["avg_heart_rate"] == 70.0


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
