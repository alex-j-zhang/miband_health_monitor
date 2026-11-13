"""High-level history synchronisation loop and its CLI summary helper.

``sync_history()`` repeats fetches until the band stops making forward progress,
isolating per-data-type failures so one unavailable stream does not abort the
others. Sample persistence is the caller's job: it is delivered through the
``on_spo2`` / ``on_activity`` / ``on_spo2_sleep`` callbacks.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, tzinfo
from typing import Callable, Optional

from miband_monitor.config.defaults import DEFAULT_HISTORY_DAYS

from .fetcher import BandHistoryFetcher
from .models import HistoryParseError, HistorySyncResult
from .parsers import _parse_payload
from .protocol import (
    DEFAULT_MAX_ROUNDS,
    DEFAULT_TIMEOUT,
    FetchDataType,
    _format_ts,
    _timestamp_usable,
    logger,
    resolve_band_tz,
)


# ---------------------------------------------------------------------------
# High-level sync loop
# ---------------------------------------------------------------------------
async def sync_history(
    client,
    *,
    days: int = DEFAULT_HISTORY_DAYS,
    include_spo2: bool = True,
    include_activity: bool = True,
    include_spo2_sleep: bool = False,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    timeout: float = DEFAULT_TIMEOUT,
    band_tz: Optional[str | tzinfo] = None,
    on_spo2: Optional[Callable[[dict], None]] = None,
    on_activity: Optional[Callable[[dict], None]] = None,
    on_spo2_sleep: Optional[Callable[[dict], None]] = None,
) -> HistorySyncResult:
    """Fetch up to *days* of band-side history, repeating while data is stale.

    Fetched samples are returned and optionally forwarded to the ``on_*``
    callbacks (used to persist them). A failure for one data type does not
    abort the other; errors are collected in :attr:`HistorySyncResult.errors`.

    ``include_spo2_sleep`` requests type ``0x26``. Its 30-byte layout is
    hardware-verified but the trailing fields are undocumented, so it stays off
    by default and consumers should treat those samples as capture material,
    not as validated measurements.
    """
    result = HistorySyncResult()
    if not (include_spo2 or include_activity or include_spo2_sleep):
        return result

    tz = resolve_band_tz(band_tz)
    fetcher = BandHistoryFetcher(client, timeout=timeout, band_tz=tz)
    since = (datetime.now(tz) - timedelta(days=max(0, int(days)))).replace(
        second=0, microsecond=0
    )
    now_ts = time.time()

    tasks: list[tuple[FetchDataType, str, Callable[[list[dict]], None]]] = []
    if include_activity:
        tasks.append((FetchDataType.ACTIVITY, "activity", _extend_activity))
    if include_spo2:
        tasks.append((FetchDataType.SPO2_NORMAL, "spo2", _extend_spo2))
    if include_spo2_sleep:
        tasks.append((FetchDataType.SPO2_SLEEP, "spo2_sleep", _extend_spo2_sleep))

    consumers: dict[FetchDataType, Optional[Callable[[dict], None]]] = {
        FetchDataType.ACTIVITY: on_activity,
        FetchDataType.SPO2_NORMAL: on_spo2,
        FetchDataType.SPO2_SLEEP: on_spo2_sleep,
    }

    for data_type, name, append in tasks:
        current = since
        previous_last_ts: Optional[float] = None
        result.rounds.setdefault(name, 0)
        for _round in range(max(1, max_rounds)):
            try:
                payload, start_timestamp = await fetcher.fetch_payload(data_type, current)
            except Exception as exc:
                message = f"{name} fetch failed: {exc}"
                logger.error(message)
                result.errors.append(message)
                break

            if not payload:
                break
            try:
                samples = _parse_payload(data_type, payload, start_timestamp)
            except HistoryParseError as exc:
                message = f"{name} payload parse failed: {exc}"
                logger.error(message)
                result.errors.append(message)
                break

            if not samples:
                break
            last_ts = max(sample["ts"] for sample in samples)
            if not _timestamp_usable(last_ts):
                message = f"{name} returned an out-of-range timestamp ({last_ts!r})"
                logger.error(message)
                result.errors.append(message)
                break
            if previous_last_ts is not None and last_ts <= previous_last_ts:
                logger.info("No forward progress in %s fetch; stopping.", name)
                break
            previous_last_ts = last_ts

            result.rounds[name] += 1
            append(result, samples)

            callback = consumers.get(data_type)
            if callback is not None:
                for sample in samples:
                    try:
                        callback(sample)
                    except Exception:  # pragma: no cover - consumer errors must not abort
                        logger.exception("History sample consumer failed")

            logger.info(
                "Fetched %d %s sample(s) up to %s", len(samples), name,
                _format_ts(last_ts, tz=tz),
            )
            if data_type == FetchDataType.ACTIVITY and last_ts > now_ts + 1800:
                logger.warning(
                    "Activity timestamps appear %.1fh ahead of host UTC; if the band "
                    "uses a different local timezone (e.g. Asia/Shanghai on a UTC host), "
                    "pass --band-tz or set BAND_TZ in .env so activity and SpO2 align.",
                    (last_ts - now_ts) / 3600.0,
                )
            if last_ts <= current.timestamp() + 60 or last_ts >= now_ts - 60:
                break
            # The band encodes "since" with minute precision: advance to the
            # next whole minute to avoid fetching the same sample twice.
            current = (datetime.fromtimestamp(last_ts, tz=tz) + timedelta(minutes=1)).replace(
                second=0, microsecond=0
            )

    return result


def _extend_activity(result: HistorySyncResult, samples: list[dict]) -> None:
    result.activity.extend(samples)


def _extend_spo2(result: HistorySyncResult, samples: list[dict]) -> None:
    result.spo2.extend(samples)


def _extend_spo2_sleep(result: HistorySyncResult, samples: list[dict]) -> None:
    result.spo2_sleep.extend(samples)


def summarize_history_result(result: HistorySyncResult) -> str:
    """Human-readable one-line summary used by the CLI."""
    if not result.total_samples and not result.errors:
        return "no new history data"
    parts = [f"{len(result.activity)} activity sample(s)", f"{len(result.spo2)} SpO2 sample(s)"]
    # Only mention the opt-in type when it actually ran, so the default log line
    # stays byte-for-byte compatible with existing tooling and tests.
    if result.spo2_sleep or "spo2_sleep" in result.rounds:
        parts.append(f"{len(result.spo2_sleep)} sleep SpO2 sample(s) [experimental]")
    if result.rounds:
        parts.append(
            "rounds=" + ",".join(f"{name}:{count}" for name, count in sorted(result.rounds.items()))
        )
    if result.errors:
        parts.append(f"errors={len(result.errors)}")
    return ", ".join(parts)
