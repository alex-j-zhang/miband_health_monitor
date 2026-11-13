"""Probe the band for the Huami SLEEP_SESSION history type (0x48).

Gadgetbridge decodes 0x48 as a list of sleep sessions and never requests it
from a Mi Band 6, because MiBand6Coordinator leaves supportsSleepScore() at the
false default that AbstractDeviceCoordinator provides. The band may or may not
answer anyway, and if it does, the payload carries what the activity stream
cannot: per-stage sleep spans and a sleep score.

Record layout, from HuamiSleepSessionSampleProvider.SleepSession:

    0x000  u32  session timestamp (seconds)
    0x004  u32  midnight boundary of the day (seconds, local)
    0x008  u8   1
    0x009  u8   1
    0x00a  u16  sleep start (minutes since previous noon)
    0x00c  u16  sleep end   (minutes since previous noon)
    0x015  u8   average heart rate
    0x016  u8   sleep score
    0x054  u8   stage count (<= 100)
    0x056  ...  stages, 5 bytes each: u16 start, u16 end, u8 type
    0x24a  u16  total REM minutes
    0x24c  u16  total light minutes
    0x24e  u16  total deep minutes
    0x250  u16  total awake minutes

Run with the band awake and nearby:

    ./.venv/Scripts/python.exe tools/probe_sleep_session.py
"""

from __future__ import annotations

import asyncio
import struct
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
    BandHistoryFetcher,
    FetchDataType,
    HistoryFetchError,
)

SLEEP_SESSION = FetchDataType.SLEEP_SESSION
RECORD_SIZE = 594
STAGE_NAMES = {4: "light", 5: "deep", 7: "awake", 8: "rem"}


def parse_sleep_session(record: bytes) -> dict:
    """Decode one 594-byte sleep session record."""
    timestamp, midnight = struct.unpack_from("<II", record, 0)
    start, end = struct.unpack_from("<HH", record, 0x0A)
    avg_hr = record[0x15]
    score = record[0x16]
    stage_count = record[0x54]

    stages = []
    for index in range(min(stage_count, 100)):
        offset = 0x56 + 5 * index
        stage_start, stage_end, stage_type = struct.unpack_from("<HHB", record, offset)
        stages.append(
            {
                "start": stage_start,
                "end": stage_end,
                "type": stage_type,
                "name": STAGE_NAMES.get(stage_type, f"0x{stage_type:02x}"),
            }
        )

    rem, light, deep, awake = struct.unpack_from("<HHHH", record, 0x24A)
    return {
        "timestamp": timestamp,
        "midnight": midnight,
        "sleep_start": start,
        "sleep_end": end,
        "avg_hr": avg_hr,
        "score": score,
        "stage_count": stage_count,
        "stages": stages,
        "totals": {"rem": rem, "light": light, "deep": deep, "awake": awake},
    }


async def main() -> int:
    load_env()
    mac = _env_device_id()
    key = env_str(ENV_AUTH_KEY)
    if is_placeholder_device_id(mac) or not key:
        print("DEVICE_ID / AUTH_KEY missing from .env")
        return 2

    client = MiBand6Client(mac, key)
    fetcher = BandHistoryFetcher(client)
    since = datetime.now() - timedelta(days=DEFAULT_HISTORY_DAYS)

    print(f"Connecting to {mask_address(mac)} ...")
    await client.connect_and_authenticate()
    print("Authenticated.")
    try:
        print(f"Requesting fetch type {int(SLEEP_SESSION):#04x} since {since:%Y-%m-%d %H:%M} ...")
        payload, start_ts = await fetcher.fetch_payload(SLEEP_SESSION, since)
    except HistoryFetchError as exc:
        print(f"Band refused or failed: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - probe, report anything
        print(f"Transport error: {exc!r}")
        return 1
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    print(f"Payload: {len(payload)} bytes, start timestamp {start_ts}")
    if not payload:
        print("EMPTY: the band returned nothing for 0x48.")
        return 0

    print("First 64 bytes:", payload[:64].hex())
    whole = len(payload) - len(payload) % RECORD_SIZE
    print(f"{whole // RECORD_SIZE} whole record(s) of {RECORD_SIZE} bytes")
    for index in range(whole // RECORD_SIZE):
        record = payload[index * RECORD_SIZE : (index + 1) * RECORD_SIZE]
        session = parse_sleep_session(record)
        print(
            f"\n[{index}] session {datetime.fromtimestamp(session['timestamp'])}  "
            f"midnight {datetime.fromtimestamp(session['midnight'])}  "
            f"score={session['score']}  avgHR={session['avg_hr']}  "
            f"slept {session['sleep_end'] - session['sleep_start']} min  "
            f"stages={session['stage_count']}"
        )
        print(f"     totals {session['totals']}")
        for stage in session["stages"]:
            print(
                f"     {stage['name']:6} {stage['start']:4} → {stage['end']:4} "
                f"({stage['end'] - stage['start'] + 1:3} min)"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
