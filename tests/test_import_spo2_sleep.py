"""Tests for importing captured 0x26 payloads into JSONL + SQLite."""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import import_spo2_sleep as imp  # noqa: E402
from sim_band import _spo2_sleep_record  # noqa: E402


def _payload(timestamps):
    return b"\x02" + b"".join(
        _spo2_sleep_record(ts, 95 + i, 32) for i, ts in enumerate(timestamps)
    )


def _jsonl_rows(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_import_writes_both_sinks_and_is_idempotent(tmp_path):
    src = tmp_path / "history_26_test.bin"
    src.write_bytes(_payload([1000, 1060]))
    out = tmp_path / "data"

    assert imp.import_payloads([src], out) == 0
    assert imp.import_payloads([src], out) == 0  # second run adds nothing

    rows = _jsonl_rows(out / "spo2_sleep.jsonl")
    assert len(rows) == 2
    assert {row["ts"] for row in rows} == {1000.0, 1060.0}

    conn = sqlite3.connect(out / "mi_band.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM spo2_sleep").fetchone()[0] == 2
        # Never leak into the point-measurement stream.
        assert conn.execute("SELECT COUNT(*) FROM spo2").fetchone()[0] == 0
    finally:
        conn.close()


def test_import_dry_run_writes_nothing(tmp_path):
    src = tmp_path / "history_26_test.bin"
    src.write_bytes(_payload([1000]))
    out = tmp_path / "data"

    assert imp.import_payloads([src], out, dry_run=True) == 0
    assert not (out / "spo2_sleep.jsonl").exists()
    assert not (out / "mi_band.db").exists()


def test_import_rejects_non_0x26_payload(tmp_path):
    bad = tmp_path / "history_25_test.bin"
    bad.write_bytes(b"\x02" + b"\x00" * 65)  # 65-byte record, not 30
    assert imp.import_payloads([bad], tmp_path / "data") == 1


def test_import_deduplicates_overlapping_payloads(tmp_path):
    first = tmp_path / "a.bin"
    second = tmp_path / "b.bin"
    first.write_bytes(_payload([1000, 1060]))
    second.write_bytes(_payload([1060, 1120]))
    out = tmp_path / "data"

    assert imp.import_payloads([first, second], out) == 0
    assert len(_jsonl_rows(out / "spo2_sleep.jsonl")) == 3
