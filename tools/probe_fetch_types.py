"""Ask the band for arbitrary history types and print the raw start-date reply.

fetch_payload hides the metadata. The status byte in that reply is what tells
"the firmware knows this type, there is simply no data" apart from "the
firmware ignores the request": both produce an empty payload otherwise.

Usage:

    ./.venv/Scripts/python.exe tools/probe_fetch_types.py 48 2c 01
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from miband_monitor.ble import (  # noqa: E402
    MiBand6Client,
    _env_device_id,
    is_placeholder_device_id,
)
from miband_monitor.config.defaults import DEFAULT_HISTORY_DAYS, ENV_AUTH_KEY  # noqa: E402
from miband_monitor.config.env import env_str, load_env  # noqa: E402
from miband_monitor.config.privacy import mask_address  # noqa: E402
from miband_monitor.history import (  # noqa: E402
    COMMAND_START_DATE,
    UUID_CHAR_ACTIVITY_DATA,
    UUID_CHAR_FETCH,
    BandHistoryFetcher,
    HistoryFetchError,
    encode_fetch_time,
)


async def probe(fetcher: BandHistoryFetcher, code: int, since: datetime) -> None:
    await fetcher._open()
    try:
        fetcher._drain()
        await fetcher._write(bytes([COMMAND_START_DATE, code & 0xFF]) + encode_fetch_time(since))
        metadata = await fetcher._next(fetcher._fetch_queue, "start-date response")
        print(f"  type {code:#04x}: reply {metadata.hex()}")
        if len(metadata) >= 7 and metadata[2] == 0x01:
            declared = int.from_bytes(metadata[3:7], "little")
            print(f"            accepted, declares {declared}")
        elif len(metadata) >= 3:
            print(f"            status 0x{metadata[2]:02x} (0x01 would be success)")
    finally:
        # Leave the characteristic in a clean state for the next probe.
        for uuid in (UUID_CHAR_FETCH, UUID_CHAR_ACTIVITY_DATA):
            try:
                await fetcher.client.stop_notify(uuid)
            except Exception:  # noqa: BLE001 - best effort
                pass


async def main(argv: list[str]) -> int:
    codes = [int(item, 16) for item in argv] or [0x48]
    load_env()
    mac = _env_device_id()
    key = env_str(ENV_AUTH_KEY)
    if is_placeholder_device_id(mac) or not key:
        print("DEVICE_ID / AUTH_KEY missing from .env")
        return 2

    client = MiBand6Client(mac, key)
    print(f"Connecting to {mask_address(mac)} ...")
    await client.connect_and_authenticate()
    print("Authenticated.")
    fetcher = BandHistoryFetcher(client)
    since = (datetime.now() - timedelta(days=DEFAULT_HISTORY_DAYS)).replace(
        second=0, microsecond=0
    )
    print(f"Asking for data since {since:%Y-%m-%d %H:%M}")
    try:
        for code in codes:
            try:
                await probe(fetcher, code, since)
            except HistoryFetchError as exc:
                print(f"  type {code:#04x}: {exc}")
            await asyncio.sleep(0.5)
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
