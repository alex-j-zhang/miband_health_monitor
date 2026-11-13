"""Probe the band's declared activity-history window without consuming it.

"01 <type> <6B calendar><reason><tz>" only asks the firmware "how much do you
still have since this moment". It does NOT stream payload and does NOT send 03
(the receipt after which the firmware may reclaim the data), so a probe session
leaves the band's stores untouched.

Usage:
    python tools/probe_declared.py            # default grid, in minutes
    python tools/probe_declared.py 4320 2880  # custom hours
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from miband_monitor.ble import (  # noqa: E402
    MiBand6Client,
    _env_device_id,
    is_placeholder_device_id,
)
from miband_monitor.config.defaults import (  # noqa: E402
    DEFAULT_SCAN_TIMEOUT,
    ENV_AUTH_KEY,
    ENV_DEVICE_NAME,
)
from miband_monitor.config.env import env_str, load_env  # noqa: E402
from miband_monitor.history import (  # noqa: E402
    COMMAND_START_DATE,
    RESPONSE,
    SUCCESS,
    BandHistoryFetcher,
    FetchDataType,
    _format_ts,
    decode_fetch_time,
    encode_fetch_time,
)

DEFAULT_HOURS = [168, 96, 72, 48, 40, 33, 32, 24, 12, 6]


async def probe(fetcher: BandHistoryFetcher, data_type: FetchDataType, since: datetime):
    fetcher._drain()
    command = bytes([COMMAND_START_DATE, int(data_type) & 0xFF]) + encode_fetch_time(since)
    await fetcher._write(command)
    metadata = await fetcher._next(fetcher._fetch_queue, "start-date response")

    if len(metadata) < 3 or metadata[0] != RESPONSE or metadata[1] != COMMAND_START_DATE:
        return None, f"unexpected response {metadata.hex()}"
    if metadata[2] != SUCCESS:
        return None, f"status 0x{metadata[2]:02x}"
    if len(metadata) < 7:
        return None, f"truncated response {metadata.hex()}"

    declared = int.from_bytes(metadata[3:7], "little")
    # The band zero-fills the echoed calendar whenever it has nothing sensible
    # to say about "where the retained data starts", so a 1970 (or truncated)
    # decode means "the count is all you get", not "it starts at your since".
    start_ts = None
    if len(metadata) >= 13:
        try:
            decoded = decode_fetch_time(metadata[7:])
        except ValueError:
            decoded = None
        if decoded is not None and decoded.year >= 2000:
            start_ts = decoded.timestamp()
    return (declared, start_ts), None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "hours",
        nargs="*",
        type=float,
        default=None,
        help="hours back to ask about (default: a fixed grid)",
    )
    parser.add_argument("--device-id", default=None)
    parser.add_argument("--scan-timeout", type=float, default=DEFAULT_SCAN_TIMEOUT)
    args = parser.parse_args()

    load_env(ROOT / ".env")
    device_id = (args.device_id or _env_device_id()).strip()
    auth_key = env_str(ENV_AUTH_KEY)
    if is_placeholder_device_id(device_id) or not auth_key:
        print("DEVICE_ID / AUTH_KEY missing from .env", file=sys.stderr)
        return 2

    hours = args.hours or DEFAULT_HOURS
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    print(f"probe time: {now.isoformat(timespec='minutes')}")
    print(f"{'since':>22}  {'declared':>9}  {'first sample':>22}  note")

    # The firmware accepts exactly one start-date command per connection: a
    # second one comes back 0x02 until the pending fetch is consumed or the
    # link drops, and a too-quick reconnect occasionally answers 0x02 as well,
    # so retry once on a fresh link.
    for h in hours:
        since = now - timedelta(hours=h)
        reading, error = None, None
        for _attempt in range(3):
            client = MiBand6Client(
                device_id, auth_key, device_name=env_str(ENV_DEVICE_NAME) or None
            )
            try:
                await client.connect_and_authenticate(scan_timeout=args.scan_timeout)
                fetcher = BandHistoryFetcher(client)
                await fetcher._open()
                reading, error = await probe(fetcher, FetchDataType.ACTIVITY, since)
            except Exception as exc:  # pragma: no cover - hardware path
                reading, error = None, f"{type(exc).__name__}: {exc}"
            finally:
                await client.disconnect()
            if reading is not None:
                break
            await asyncio.sleep(2.0)
        if error:
            print(f"{since.isoformat(timespec='minutes'):>22}  {'--':>9}  {'--':>22}  {error}")
            continue
        declared, start_ts = reading
        first = _format_ts(start_ts)[:16] if start_ts else "(not given)"
        span = ""
        if declared:
            last = since + timedelta(minutes=declared)
            span = f"since+{declared}min -> {last.isoformat(timespec='minutes')}"
        print(f"{since.isoformat(timespec='minutes'):>22}  {declared:>9}  {first:>22}  {span}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
