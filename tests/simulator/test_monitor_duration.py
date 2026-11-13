"""Tests for run_monitor post-authentication duration and failure exit behavior."""

from __future__ import annotations

import asyncio

import pytest

from miband_monitor.cli.runner import run_monitor


class _FailingClient:
    def __init__(self, _mac: str, _key: str) -> None:
        return None

    async def connect_and_authenticate(self, **_kwargs) -> None:
        raise TimeoutError("no advertising pulse")

    async def disconnect(self) -> None:
        return None


class _SlowAuthClient:
    def __init__(self, wait_timeouts: list[float]) -> None:
        self._wait_timeouts = wait_timeouts
        self._done = asyncio.Event()

    async def connect_and_authenticate(self, **_kwargs) -> None:
        await asyncio.sleep(0.25)

    async def start_heart_rate_stream(self, _cb) -> None:
        return None

    async def start_steps_stream(self, _cb) -> None:
        return None

    async def wait_until_disconnected(self) -> None:
        await self._done.wait()

    async def disconnect(self) -> None:
        self._done.set()


@pytest.mark.asyncio
async def test_run_monitor_raises_when_zero_sessions_succeeded() -> None:
    with pytest.raises(TimeoutError, match="no advertising pulse"):
        await run_monitor(
            "C0:FF:EE:C0:FF:EE",
            "0123456789abcdef0123456789abcdef",
            duration=0.15,
            reconnect=True,
            reconnect_initial_delay=0.05,
            reconnect_max_delay=0.05,
            client_factory=lambda mac, key: _FailingClient(mac, key),
        )


@pytest.mark.asyncio
async def test_run_monitor_counts_duration_after_first_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_timeouts: list[float] = []
    real_wait_for = asyncio.wait_for

    async def spy_wait_for(coro, timeout=None):
        if timeout is not None:
            captured_timeouts.append(timeout)
        return await real_wait_for(coro, timeout=timeout)

    monkeypatch.setattr("miband_monitor.cli.runner.asyncio.wait_for", spy_wait_for)

    await run_monitor(
        "C0:FF:EE:C0:FF:EE",
        "0123456789abcdef0123456789abcdef",
        duration=0.35,
        reconnect=True,
        client_factory=lambda _mac, _key: _SlowAuthClient(captured_timeouts),
    )
    assert len(captured_timeouts) == 1
    assert 0.25 <= captured_timeouts[0] <= 0.36
