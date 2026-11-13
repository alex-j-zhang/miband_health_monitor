#!/usr/bin/env python3
"""Capture raw band-side history payloads for real-device validation.

This is a diagnostic companion for docs/validation/history-hardware.md.
It runs the normal connection + experimental history sync, but intercepts every
fetched payload and stores it verbatim (.bin) together with metadata (.json) so
the protocol can be re-checked offline or attached to a bug report.

All written directories and capture files are created with private permissions
(0700/0600 on POSIX), and the summary file stores only a masked device identifier.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Allow running as "python tools/capture_history_payload.py" from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miband_monitor.ble import (  # noqa: E402
    MiBand6Client,
    _env_device_id,
    is_placeholder_device_id,
    normalize_auth_key,
)
from miband_monitor.config.defaults import (  # noqa: E402
    DEFAULT_HISTORY_DAYS,
    DEFAULT_SCAN_TIMEOUT,
    ENV_AUTH_KEY,
)
from miband_monitor.config.env import env_str, load_env  # noqa: E402
from miband_monitor.config.privacy import mask_address  # noqa: E402
from miband_monitor.config.security import secure_directory, secure_file  # noqa: E402
from miband_monitor.history import (  # noqa: E402
    DEFAULT_MAX_ROUNDS,
    DEFAULT_TIMEOUT,
    BandHistoryFetcher,
    FetchDataType,
    summarize_history_result,
    sync_history,
)


def _data_type_name(data_type: int) -> str:
    try:
        return FetchDataType(data_type).name
    except ValueError:
        return f"TYPE_{data_type:#04x}"


async def _run(args: argparse.Namespace) -> int:
    load_env()
    mac = _env_device_id()
    key = env_str(ENV_AUTH_KEY)
    if is_placeholder_device_id(mac) or not key:
        print(
            "[-] DEVICE_ID (or legacy DEVICE_MAC) / AUTH_KEY missing from "
            "environment/.env",
            file=sys.stderr,
        )
        return 1
    try:
        normalize_auth_key(key)
    except ValueError as exc:
        print(f"[-] Invalid AUTH_KEY: {exc}", file=sys.stderr)
        return 1

    output_dir = Path(args.output)
    secure_directory(output_dir)

    original_fetch = BandHistoryFetcher.fetch_payload
    saved: list[dict[str, Any]] = []

    async def capturing_fetch(self, data_type, since):  # type: ignore[no-untyped-def]
        payload, start_timestamp = await original_fetch(self, data_type, since)
        if payload:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            base = output_dir / f"history_{int(data_type):02x}_{stamp}"
            binary_path = base.with_suffix(".bin")
            json_path = base.with_suffix(".json")
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
            secure_file(binary_path)
            json_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            secure_file(json_path)
            saved.append(meta)
            print(f"[+] captured {len(payload):>6} B  {meta['data_type_name']:<10} -> {binary_path}")
        return payload, start_timestamp

    BandHistoryFetcher.fetch_payload = capturing_fetch  # type: ignore[method-assign]
    client = MiBand6Client(mac, key, scan_timeout=args.scan_timeout)
    try:
        print("[*] Connecting and authenticating (keep the band screen awake)...")
        await client.connect_and_authenticate(scan_timeout=args.scan_timeout)
        print(f"[*] Fetching up to {args.days} day(s) of history with timeout {args.timeout}s...")
        if args.spo2_sleep:
            print(
                "[!] Also capturing type 0x26 (sleep SpO2): 30B layout verified "
                "on Mi Band 6; trailing field semantics are still unknown, so "
                "raw payloads remain the evidence."
            )
        result = await sync_history(
            client,
            days=args.days,
            include_spo2_sleep=args.spo2_sleep,
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
        "device_mac": mask_address(mac),
        "days": args.days,
        "fetched": summarize_history_result(result),
        "activity_samples": len(result.activity),
        "spo2_samples": len(result.spo2),
        "spo2_sleep_samples": len(result.spo2_sleep),
        "spo2_sleep_requested": bool(args.spo2_sleep),
        "rounds": result.rounds,
        "errors": result.errors,
        "payloads": saved,
    }
    summary_path = output_dir / "capture_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    secure_file(summary_path)
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
    parser.add_argument(
        "--days", type=int, default=DEFAULT_HISTORY_DAYS, help="history window in days (default: 7)"
    )
    parser.add_argument(
        "--output", default="validation/payloads", help="output directory for .bin/.json files"
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help="per-message timeout seconds"
    )
    parser.add_argument(
        "--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS, help="max fetch rounds per data type"
    )
    parser.add_argument(
        "--spo2-sleep",
        action="store_true",
        help=(
            "also request type 0x26 (sleep SpO2) and save its raw payload; "
            "layout verified, trailing field semantics still unknown"
        ),
    )
    parser.add_argument(
        "--scan-timeout", type=float, default=DEFAULT_SCAN_TIMEOUT, help="BLE scan timeout seconds"
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
