"""In-memory Mi Band 6 state machine used by the integration suite and demo.

The simulator speaks the same wire protocol as a real Mi Band 6 at the level the
client cares about:

* chunked-transfer framing on the ``0x0016`` / ``0x0017`` characteristics,
* ECDH (NIST B-163) + AES-CBC challenge/response authentication,
* heart-rate, real-time steps and raw IMU notification streams,
* the historical-data fetch command sequence.

It needs **no Bluetooth hardware**, which makes it usable for the integration
test suite (``tests/test_simulated_band.py``) and for the end-to-end demo
(``python miband_client.py --demo 10 --imu``).

This module holds the connection lifecycle and GATT write dispatch; the auth,
history and stream behaviour lives in the mixins. The fake backend mimics
real-world failure modes so robustness can be tested: ``fail_auth`` rejects the
challenge response, ``drop_auth_seqs`` drops selected auth fragments and
``disconnect_after`` drops the link.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Callable, Optional

from miband_monitor.ble import (
    UUID_CHAR_CHUNKED_WRITE,
    UUID_CHAR_HR_CONTROL,
    UUID_CHAR_RAW_SENSOR_CONTROL,
    UUID_CHAR_RAW_SENSOR_DATA,
    UUID_CHAR_STEPS,
)
from miband_monitor.crypto import generate_ecdh_keys
from miband_monitor.history import UUID_CHAR_FETCH

from .auth import AuthMixin
from .constants import logger
from .history import HistoryMixin
from .payloads import (
    _activity_sample,
    _imu_packet,
    _optical_packet,
    _status_packet,
    _steps_packet,
)
from .streams import StreamMixin


class SimulatedBand(HistoryMixin, AuthMixin, StreamMixin):
    """State machine that behaves like a Mi Band 6 for the client's purposes."""

    def __init__(
        self,
        auth_key: bytes,
        *,
        hr_values: tuple[int, ...] = (72, 75, 78),
        steps_packets: Optional[list[bytes]] = None,
        imu_packets: Optional[list[bytes]] = None,
        fail_auth: bool = False,
        fail_auth_connections: int = 0,
        drop_auth_seqs: tuple[int, ...] = (),
        disconnect_after: Optional[float] = None,
        history_spo2: Optional[list[dict]] = None,
        history_activity: Optional[list[dict]] = None,
        history_spo2_sleep: Optional[list[dict]] = None,
        drop_history_packets: tuple[int, ...] = (),
    ) -> None:
        if len(auth_key) != 16:
            raise ValueError("auth_key must be 16 bytes")
        self.auth_key = bytes(auth_key)
        self._prv_key, self._pub_key = generate_ecdh_keys()
        self._salt = os.urandom(16)
        self._session_key: Optional[bytes] = None

        self.hr_values = list(hr_values)
        self.steps_packets = (
            list(steps_packets)
            if steps_packets is not None
            else [
                _steps_packet(1250, 850, 45),
                _steps_packet(1263, 861, 46),
            ]
        )
        self.imu_packets = (
            list(imu_packets)
            if imu_packets is not None
            else [
                _imu_packet(sequence=0, samples=[(0, 2000, 3570), (16, -8, 4088)]),
                _imu_packet(sequence=1, samples=[(-120, 1990, 3580)]),
                # Real bands interleave non-accelerometer frames (optical/status)
                # on the same characteristic; the client must ignore them.
                _optical_packet(sequence=0),
                _status_packet(),
            ]
        )

        now = time.time()
        self.history_spo2 = (
            list(history_spo2)
            if history_spo2 is not None
            else [
                {"ts": now - 600, "spo2": 97, "auto_measure": False},
                {"ts": now - 300, "spo2": 96, "auto_measure": True},
                {"ts": now - 60, "spo2": 98, "auto_measure": False},
            ]
        )
        self.history_activity = (
            list(history_activity)
            if history_activity is not None
            else [
                _activity_sample(now - 240, steps=12, heart_rate=70),
                _activity_sample(now - 180, steps=30, heart_rate=74),
                _activity_sample(now - 120, steps=8, heart_rate=72),
                _activity_sample(now - 60, steps=21, heart_rate=76),
            ]
        )
        # Type 0x26 uses a 30-byte record (Gadgetbridge FetchSpo2SleepOperation).
        # Kept separate so the opt-in path can be exercised without touching the
        # normal SpO2 fixture data.
        self.history_spo2_sleep = (
            list(history_spo2_sleep)
            if history_spo2_sleep is not None
            else [
                {"ts": now - 480, "spo2": 98, "duration": 60},
                {"ts": now - 240, "spo2": 96, "duration": 60},
            ]
        )
        self.drop_history_packets = set(drop_history_packets)

        self.fail_auth = fail_auth
        self._remaining_auth_failures = max(0, int(fail_auth_connections))
        self._fail_this_auth = fail_auth
        self.drop_auth_seqs = set(drop_auth_seqs)
        self.disconnect_after = disconnect_after

        self.connect_count = 0
        self.authenticated = False
        self.connected = False

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client: Optional["FakeBleakClient"] = None
        self._notify_callbacks: dict[str, Callable] = {}
        self._write_transactions: dict[int, list] = {}
        self._timers: list[asyncio.TimerHandle] = []
        self._hr_started = False
        self._sensor_started = False
        self._steps_scheduled = False
        self._imu_scheduled = False
        self._fetch_payload = b""
        self._fetch_data_sent = False

    # ------------------------------------------------------- connection life
    def on_connect(self, client: "FakeBleakClient") -> None:
        self._loop = asyncio.get_running_loop()
        self._client = client
        self.connect_count += 1
        self.connected = True
        self._reset_session()
        # Optionally fail authentication for the first N connections.
        self._fail_this_auth = self.fail_auth or self._remaining_auth_failures > 0
        if self._remaining_auth_failures > 0:
            self._remaining_auth_failures -= 1
        if self.disconnect_after is not None:
            self._timers.append(
                self._loop.call_later(self.disconnect_after, client.simulate_external_disconnect)
            )
        logger.debug("Simulated band: connection #%d established", self.connect_count)

    def on_disconnect(self) -> None:
        self.connected = False
        self._client = None
        self._notify_callbacks.clear()
        self._cancel_timers()
        logger.debug("Simulated band: disconnected (auth=%s)", self.authenticated)

    def _reset_session(self) -> None:
        self.authenticated = False
        self._session_key = None
        self._write_transactions.clear()
        self._hr_started = False
        self._sensor_started = False
        self._steps_scheduled = False
        self._imu_scheduled = False
        self._fetch_payload = b""
        self._fetch_data_sent = False

    def _cancel_timers(self) -> None:
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()

    # ----------------------------------------------------------- GATT writes
    def subscribe(self, uuid: str, callback: Callable) -> None:
        self._notify_callbacks[uuid] = callback
        if uuid == UUID_CHAR_STEPS:
            self._schedule_steps()
        elif uuid == UUID_CHAR_RAW_SENSOR_DATA and self._sensor_started:
            self._schedule_imu()

    def unsubscribe(self, uuid: str) -> None:
        self._notify_callbacks.pop(uuid, None)

    def feed_write(self, uuid: str, data: bytes) -> None:
        if not self.connected:
            raise OSError("simulated band is not connected")
        if uuid == UUID_CHAR_CHUNKED_WRITE:
            self._consume_chunk(data)
        elif uuid == UUID_CHAR_HR_CONTROL:
            self._on_heart_rate_control(data)
        elif uuid == UUID_CHAR_RAW_SENSOR_CONTROL:
            self._on_sensor_control(data)
        elif uuid == UUID_CHAR_FETCH:
            self._on_fetch_write(data)

    def read(self, uuid: str) -> bytes:
        if uuid == UUID_CHAR_STEPS and self.steps_packets:
            return self.steps_packets[0]
        return b""

    # ------------------------------------------------------------ notifying
    def _emit_later(self, uuid: str, payload: bytes, delay: float) -> None:
        self._timers.append(
            self._loop.call_later(delay, self._emit, uuid, payload)  # type: ignore[union-attr]
        )

    def _emit(self, uuid: str, payload: bytes) -> None:
        if not self.connected:
            return
        callback = self._notify_callbacks.get(uuid)
        if callback is None:
            return
        callback(uuid, bytearray(payload))
