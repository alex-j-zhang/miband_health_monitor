"""ECDH authentication, fast reconnect and write-retry hardening."""

from __future__ import annotations

import asyncio
import time

import pytest

from sim_band import SimulatedBand, make_simulated_client
from tests.support import (
    AUTH_KEY,
    AUTH_KEY_BYTES,
    KEY,
    MAC,
    connect_simulated_client as _connect,
)


async def test_simulated_authentication_succeeds():
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES)
    client = await _connect(band)
    try:
        assert client.authenticated is True
        assert band.authenticated is True
        assert band.connect_count == 1
    finally:
        await client.disconnect()


async def test_simulated_authentication_failure_raises():
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES, fail_auth=True)
    client = make_simulated_client(MAC, AUTH_KEY, band, scanner_address=MAC)
    try:
        with pytest.raises(RuntimeError, match="Authentication failed"):
            await client.connect_and_authenticate(scan_timeout=2.0)
        assert client.authenticated is False
    finally:
        await client.disconnect()


async def test_simulated_authentication_gap_is_detected():
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES, drop_auth_seqs=(2,))
    client = make_simulated_client(MAC, AUTH_KEY, band, scanner_address=MAC)
    try:
        with pytest.raises(RuntimeError, match="gap"):
            await client.connect_and_authenticate(scan_timeout=2.0)
    finally:
        await client.disconnect()


async def test_auth_wait_aborts_when_link_drops():
    """A link drop mid-handshake must fail fast, not wait for auth_timeout."""
    # Dropping the first auth control packet leaves the handshake pending, so
    # only the disconnect watchdog can end the wait.
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES, drop_auth_seqs=(0,))
    client = make_simulated_client(MAC, AUTH_KEY, band, scanner_address=MAC)

    async def drop_link() -> None:
        await asyncio.sleep(0.1)
        client._on_disconnected(None)  # simulate the bleak disconnect callback

    drop_task = asyncio.create_task(drop_link())
    started = time.monotonic()
    try:
        with pytest.raises(ConnectionError, match="link lost"):
            await client.connect_and_authenticate(scan_timeout=2.0, auth_timeout=5.0)
    finally:
        if not drop_task.done():
            drop_task.cancel()
        await asyncio.gather(drop_task, return_exceptions=True)
        await client.disconnect()

    assert time.monotonic() - started < 2.0, "auth wait did not abort on disconnect"
    assert client.authenticated is False


async def test_open_client_fast_connect_retry():
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES)
    client = make_simulated_client(MAC, AUTH_KEY, band, scanner_address=MAC)

    orig_client_factory = client._client_factory
    attempts = 0

    def retry_factory(device, timeout=10.0, disconnected_callback=None):
        nonlocal attempts
        c = orig_client_factory(device, timeout=timeout, disconnected_callback=disconnected_callback)
        orig_connect = c.connect

        async def flaky_connect():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("org.bluez.Error.Failed: le-connection-abort-by-local")
            await orig_connect()

        c.connect = flaky_connect
        return c

    client._client_factory = retry_factory
    await client.connect_and_authenticate(scan_timeout=2.0)
    try:
        assert client.authenticated is True
        assert attempts == 2
    finally:
        await client.disconnect()


async def test_write_gatt_retries_on_in_progress():
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES)
    client = await _connect(band)
    try:
        call_count = 0
        orig_write = client.client.write_gatt_char

        async def flaky_write(uuid, data, response=False):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("org.bluez.Error.InProgress")
            await orig_write(uuid, data, response=response)

        client.client.write_gatt_char = flaky_write
        await client._write_gatt("00002a39-0000-1000-8000-00805f9b34fb", b"\x16")
        assert call_count == 2
    finally:
        await client.disconnect()


async def test_hr_keepalive_loop_tolerates_transient_failure():
    band = SimulatedBand(auth_key=AUTH_KEY_BYTES)
    client = await _connect(band)
    try:
        fail_count = 0

        async def failing_write(*a, **k):
            nonlocal fail_count
            fail_count += 1
            raise RuntimeError("transient bus error")

        client._write_gatt = failing_write
        assert getattr(client.client, "is_connected", False) is True

        sleep_count = 0

        async def fast_sleep(sec):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError()

        orig_sleep = asyncio.sleep
        try:
            asyncio.sleep = fast_sleep
            with pytest.raises(asyncio.CancelledError):
                await client._hr_keepalive_loop()
        finally:
            asyncio.sleep = orig_sleep

        assert fail_count == 1
        assert not client.disconnected_event.is_set()
    finally:
        await client.disconnect()
