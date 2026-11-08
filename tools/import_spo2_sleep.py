#!/usr/bin/env python3
"""Import captured 0x26 sleep-SpO2 payloads into the normal data directory.

``tools/capture_history_payload.py`` saves raw ``.bin`` evidence but writes no
telemetry. This companion decodes those payloads with the production parser and
replays the samples through ``TelemetryStore``, so they land in BOTH
``spo2_sleep.jsonl`` and the dedicated ``spo2_sleep`` SQLite table.

That matters more than it looks: the band may clear uploaded data after the
fetch ACK, so importing the captured payload is often the only way to keep a
night's samples. Re-running is safe -- timestamps already present in either
sink are skipped.

Usage::

    python tools/import_spo2_sleep.py --output data \\
        validation/payloads-26/history_26_*.bin
    python tools/import_spo2_sleep.py --dry-run validation/payloads-26/history_26_*.bin

Only ``0x26`` payloads (version byte 2 + N x 30 B) are accepted; point SpO2
``0x25`` and activity ``0x01`` payloads must go through a normal sync instead.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# Allow running as ``python tools/import_spo2_sleep.py`` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from band_history import HistoryParseError, parse_spo2_sleep  # noqa: E402
from storage import TelemetryStore  # noqa: E402


def _existing_timestamps(output_dir: Path) -> set[float]:
    """Timestamps already persisted in either sink (dedup across re-runs)."""
    seen: set[float] = set()
    jsonl = output_dir / "spo2_sleep.jsonl"
    if jsonl.is_file():
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                seen.add(float(json.loads(line)["ts"]))
            except (ValueError, KeyError, TypeError):
                continue  # tolerate a half-written capture line

    db = output_dir / "mi_band.db"
    if db.is_file():
        conn = sqlite3.connect(str(db))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "spo2_sleep" in tables:
                seen.update(
                    float(ts) for (ts,) in conn.execute("SELECT ts FROM spo2_sleep")
                )
        finally:
            conn.close()
    return seen


def import_payloads(
    paths: list[Path], output_dir: Path, *, dry_run: bool = False
) -> int:
    samples: dict[float, dict] = {}
    for path in paths:
        if not path.is_file():
            print(f"[-] missing payload: {path}", file=sys.stderr)
            return 1
        try:
            parsed = parse_spo2_sleep(path.read_bytes())
        except HistoryParseError as exc:
            print(f"[-] cannot decode {path}: {exc}", file=sys.stderr)
            return 1
        print(f"[+] {path.name}: {len(parsed)} sample(s)")
        for sample in parsed:
            samples.setdefault(float(sample["ts"]), sample)

    known = _existing_timestamps(output_dir)
    fresh = [sample for ts, sample in sorted(samples.items()) if ts not in known]
    print(
        f"[=] {len(samples)} unique sample(s), {len(fresh)} new, "
        f"{len(samples) - len(fresh)} already present"
    )
    if dry_run or not fresh:
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    store = TelemetryStore(output_dir, backend="both")
    try:
        for sample in fresh:
            store.record_spo2_sleep(sample)
    finally:
        store.close()
    print(f"[+] wrote {len(fresh)} sample(s) to {output_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import captured sleep-SpO2 (0x26) payloads into JSONL + SQLite."
    )
    parser.add_argument(
        "payloads", nargs="+", type=Path, help="history_26_*.bin payload file(s)"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data"),
        help="data directory holding mi_band.db (default: data)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="decode and report only, write nothing"
    )
    args = parser.parse_args(argv)
    return import_payloads(args.payloads, args.output, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
