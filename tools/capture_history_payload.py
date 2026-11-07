#!/usr/bin/env python3
"""Capture raw band-side history payloads for real-device validation.

This is a diagnostic companion for ``docs/history_hardware_validation.md``.
It runs the normal connection + experimental history sync, but intercepts every
fetched payload and stores it verbatim (``.bin``) together with metadata
(``.json``) so the protocol can be re-checked offline or attached to a bug
report.

Usage::

    python tools/capture_history_payload.py --days 7 --output validation/payloads
    python tools/capture_history_payload.py --days 1 --timeout 20

Requires ``.env`` with ``DEVICE_MAC`` / ``AUTH_KEY`` (see README).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Allow running as ``python tools/capture_history_payload.py`` from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from band_history import (  # noqa: E402
    BandHistoryFetcher,
    FetchDataType,
    sync_history,
    summarize_history_result,
)
from miband_client import MiBand6Client, normalize_auth_key  # noqa: E402


def _data_type_name(data_type: int) -> str:
    try:
        return FetchDataType(data_type).name
    except ValueError:
        return f"TYPE_{data_type:#04x}"


async def _run(args: argparse.Namespace) -> int:
    load_dotenv()
    mac = (os.getenv("DEVICE_MAC") or "").strip()
    key = (os.getenv("AUTH_KEY") or "").strip()
    if not mac or not key:
        print("[-] DEVICE_MAC / AUTH_KEY missing from environment/.env", file=sys.stderr)
        return 1
    try:
        normalize_auth_key(key)
    except ValueError as exc:
        print(f"[-] Invalid AUTH_KEY: {exc}", file=sys.stderr)
        return 1

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    original_fetch = BandHistoryFetcher.fetch_payload
    saved: list[dict[str, Any]] = []

    async def capturing_fetch(self, data_type, since):  # type: ignore[no-untyped-def]
        payload, start_timestamp = await original_fetch(self, data_type, since)
        if payload:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            base = output_dir / f"history_{int(data_type):02x}_{stamp}"
            binary_path = base.with_suffix(".bin")
            meta = {
                "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "data_type": int(data_type),
                "data_type_name": _data_type_name(int(data_type)),
                "since": since.isoformat(),
                "start_timestamp": start_timestamp,
                "start_iso": datetime.fromtimestamp(start_timestamp).astimezone().isoformat(
                    timespec="seconds"
                ),
                "payload_len": len(payload),
                "payload_sha256": hashlib.sha256(payload).hexdigest(),
            }
            binary_path.write_bytes(payload)
            base.with_suffix(".json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            saved.append(meta)
            print(f"[+] captured {len(payload):>6} B  {meta['data_type_name']:<10} -> {binary_path}")
        return payload, start_timestamp

    BandHistoryFetcher.fetch_payload = capturing_fetch  # type: ignore[method-assign]
    client = MiBand6Client(mac, key, scan_timeout=args.scan_timeout)
    try:
        print("[*] Connecting and authenticating (keep the band screen awake)...")
        await client.connect_and_authenticate(scan_timeout=args.scan_timeout)
        print(f"[*] Fetching up to {args.days} day(s) of history with timeout {args.timeout}s...")
        result = await sync_history(
            client,
            days=args.days,
            max_rounds=args.max_rounds,
            timeout=args.timeout,
        )
    except Exception as exc:
        print(f"[-] Capture failed: {exc}", file=sys.stderr)
        return 1
    finally:
        BandHistoryFetcher.fetch_payload = original_fetch  # type: ignore[method-assign]
        try:
            await client.disconnect()
        except Exception:
            pass

    summary = {
        "device_mac": mac,
        "days": args.days,
        "fetched": summarize_history_result(result),
        "activity_samples": len(result.activity),
        "spo2_samples": len(result.spo2),
        "rounds": result.rounds,
        "errors": result.errors,
        "payloads": saved,
    }
    summary_path = output_dir / "capture_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[+] Summary: {summary['fetched']}")
    if result.errors:
        for error in result.errors:
            print(f"[-] {error}", file=sys.stderr)
    print(f"[+] Wrote {len(saved)} payload(s) + {summary_path}")
    return 0 if not result.errors else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture raw Mi Band 6 history payloads for protocol validation."
    )
    parser.add_argument("--days", type=int, default=7, help="history window in days (default: 7)")
    parser.add_argument(
        "--output", default="validation/payloads", help="output directory for .bin/.json files"
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="per-message timeout seconds")
    parser.add_argument("--max-rounds", type=int, default=5, help="max fetch rounds per data type")
    parser.add_argument("--scan-timeout", type=float, default=90.0, help="BLE scan timeout seconds")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
