"""In-memory Mi Band 6 simulator and fake ``bleak`` backend.

The simulator speaks the same wire protocol as a real Mi Band 6 at the level
the client cares about:

* chunked-transfer framing on the ``0x0016`` / ``0x0017`` characteristics,
* ECDH (NIST B-163) + AES-CBC challenge/response authentication,
* heart-rate, real-time steps and raw IMU notification streams.

It needs **no Bluetooth hardware**, which makes it usable for

* the integration test suite (``tests/test_simulated_band.py``), and
* an end-to-end demo: ``python miband_client.py --demo 10 --imu``.

The fake backend intentionally mimics real-world failure modes so robustness
can be tested: ``fail_auth`` rejects the challenge response, ``drop_auth_seqs``
drops selected auth fragments, and ``disconnect_after`` drops the link.
"""

from __future__ import annotations

import asyncio
import logging
import os
import struct
import time
from datetime import datetime
from typing import Callable, Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from band_history import (
    COMMAND_ACK,
    COMMAND_FETCH_DATA,
    COMMAND_START_DATE,
    RESPONSE,
    SUCCESS,
    UUID_CHAR_ACTIVITY_DATA,
    UUID_CHAR_FETCH,
    FetchDataType,
    decode_fetch_time,
    encode_fetch_time,
)
from ecdh_wrapper import compute_shared_secret, generate_ecdh_keys
from miband_client import (
    CHUNKED_ENDPOINT_AUTH,
    UUID_CHAR_CHUNKED_READ,
    UUID_CHAR_CHUNKED_WRITE,
    UUID_CHAR_HR_CONTROL,
    UUID_CHAR_HR_MEASUREMENT,
    UUID_CHAR_RAW_SENSOR_CONTROL,
    UUID_CHAR_RAW_SENSOR_DATA,
    UUID_CHAR_STEPS,
    MiBand6Client,
)

logger = logging.getLogger("SimBand")

DEFAULT_DEMO_MAC = "C0:FF:EE:C0:FF:EE"

# Protocol constants the simulator needs (kept local to avoid importing
# private helpers from the client module).
_AUTH_STEP1_PREFIX = b"\x04\x02\x00\x02"
_AUTH_STEP2_OPCODE = 0x05
_FIRST_CHUNK_HEADER = 11
_NEXT_CHUNK_HEADER = 5
_AUTH_TOTAL_LENGTH_FIELD = 3  # client derives payload size as total_len - 3


class FakeAdvertisement:
    """Minimal stand-in for ``bleak.backends.scanner.AdvertisementData``."""

    def __init__(self, rssi: int = -42, local_name: str = "Mi Smart Band 6") -> None:
        self.rssi = rssi
        self.local_name = local_name


class FakeDevice:
    """Minimal stand-in for ``bleak.backends.device.BLEDevice``."""

    def __init__(self, address: str) -> None:
        self.address = address
        self.name = "Mi Smart Band 6 (simulated)"
        self.rssi = -42
        self.advertisement = FakeAdvertisement()


class SimulatedBand:
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
                _imu_packet(sequence=0, samples=[(0, 0, 256), (0, 0, 250)]),
                _imu_packet(sequence=1, samples=[(16, -8, 248)]),
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

    def _on_heart_rate_control(self, data: bytes) -> None:
        if data == b"\x15\x01\x01":
            self._hr_started = True
            for index, bpm in enumerate(self.hr_values):
                self._timers.append(
                    self._loop.call_later(  # type: ignore[union-attr]
                        0.02 * (index + 1),
                        self._emit,
                        UUID_CHAR_HR_MEASUREMENT,
                        bytes([0x00, bpm & 0xFF]),
                    )
                )
        elif data[:2] in (b"\x15\x01", b"\x15\x02"):
            self._hr_started = False

    def _on_sensor_control(self, data: bytes) -> None:
        if data == b"\x03":
            self._sensor_started = False
        elif data and data[0] in (0x01, 0x02):
            self._sensor_started = True

    # ------------------------------------------------ historical data fetch
    def _on_fetch_write(self, data: bytes) -> None:
        if not data:
            return
        command = data[0]
        if command == COMMAND_START_DATE:
            if len(data) < 2:
                self._emit_later(
                    UUID_CHAR_FETCH, bytes([RESPONSE, COMMAND_START_DATE, 0x02]), 0.001
                )
                return
            data_type = data[1]
            try:
                since = decode_fetch_time(data[2:])
            except Exception:
                since = datetime.now()
            payload, start_timestamp = self._build_history_payload(data_type, since)
            if payload is None:  # unsupported data type
                self._emit_later(
                    UUID_CHAR_FETCH, bytes([RESPONSE, COMMAND_START_DATE, 0x02]), 0.001
                )
                return
            self._fetch_payload = payload
            metadata = (
                bytes([RESPONSE, COMMAND_START_DATE, SUCCESS])
                + len(payload).to_bytes(4, "little")
                + encode_fetch_time(datetime.fromtimestamp(start_timestamp))
            )
            self._emit_later(UUID_CHAR_FETCH, metadata, 0.001)
            return

        if command == COMMAND_FETCH_DATA:
            payload = self._fetch_payload
            if not payload:
                self._emit_later(
                    UUID_CHAR_FETCH, bytes([RESPONSE, COMMAND_FETCH_DATA, 0x02]), 0.001
                )
                return
            chunks = [payload[offset : offset + 19] for offset in range(0, len(payload), 19)]
            for index, chunk in enumerate(chunks):
                if index in self.drop_history_packets:
                    logger.warning("Simulated band: dropping history packet %d", index)
                    continue
                packet = bytes([index & 0xFF]) + chunk
                self._emit_later(UUID_CHAR_ACTIVITY_DATA, packet, 0.002 * (index + 1))
            self._emit_later(
                UUID_CHAR_FETCH,
                bytes([RESPONSE, COMMAND_FETCH_DATA, SUCCESS]),
                0.002 * (len(chunks) + 1),
            )
            self._fetch_data_sent = True
            return

        if command == COMMAND_ACK:
            self._emit_later(UUID_CHAR_FETCH, bytes([RESPONSE, COMMAND_ACK]), 0.001)
            self._fetch_payload = b""
            self._fetch_data_sent = False

    def _build_history_payload(
        self, data_type: int, since: datetime
    ) -> tuple[Optional[bytes], float]:
        """Build the payload a real band would return for *data_type* since *since*."""
        since_ts = since.timestamp()
        try:
            fetch_type = FetchDataType(data_type)
        except ValueError:
            return None, since_ts

        if fetch_type == FetchDataType.SPO2_NORMAL:
            samples = [s for s in self.history_spo2 if float(s["ts"]) >= since_ts]
            if not samples:
                return b"", since_ts
            payload = bytearray([0x02])  # payload version
            for sample in sorted(samples, key=lambda s: s["ts"]):
                spo2 = int(sample["spo2"])
                if sample.get("auto_measure"):
                    raw = spo2 - 128
                else:
                    raw = spo2
                payload += struct.pack("<i", int(sample["ts"]))
                payload += struct.pack("<b", raw)
                payload += b"\x00" * 60
            return bytes(payload), float(min(s["ts"] for s in samples))

        if fetch_type == FetchDataType.ACTIVITY:
            samples = [s for s in self.history_activity if float(s["ts"]) >= since_ts]
            if not samples:
                return b"", since_ts
            payload = bytearray()
            for sample in sorted(samples, key=lambda s: s["ts"]):
                payload += struct.pack(
                    "<BBBBBBBB",
                    int(sample.get("kind", 0)),
                    int(sample.get("intensity", 0)),
                    int(sample.get("steps", 0)),
                    int(sample.get("heart_rate", 0)),
                    int(sample.get("unknown1", 0)),
                    int(sample.get("sleep", 0)),
                    int(sample.get("deep_sleep", 0)),
                    int(sample.get("rem_sleep", 0)),
                )
            return bytes(payload), float(min(s["ts"] for s in samples))

        return None, since_ts

    def _emit_later(self, uuid: str, payload: bytes, delay: float) -> None:
        self._timers.append(
            self._loop.call_later(delay, self._emit, uuid, payload)  # type: ignore[union-attr]
        )

    # ------------------------------------------------------- chunked framing
    def _consume_chunk(self, chunk: bytes) -> None:
        if len(chunk) < _NEXT_CHUNK_HEADER:
            return
        flags = chunk[1]
        handle = chunk[3]
        if flags & 0x01:
            if len(chunk) < _FIRST_CHUNK_HEADER:
                return
            total_length = int.from_bytes(chunk[5:9], "little")
            endpoint = int.from_bytes(chunk[9:11], "little")
            self._write_transactions[handle] = [endpoint, total_length, bytearray(chunk[11:])]
        else:
            transaction = self._write_transactions.get(handle)
            if transaction is None:
                return
            transaction[2].extend(chunk[_NEXT_CHUNK_HEADER:])

        transaction = self._write_transactions.get(handle)
        if transaction is not None and len(transaction[2]) >= transaction[1]:
            endpoint, total_length, buffer = transaction
            del self._write_transactions[handle]
            self._on_chunked_message(endpoint, bytes(buffer[:total_length]))

    def _on_chunked_message(self, endpoint: int, message: bytes) -> None:
        if endpoint != CHUNKED_ENDPOINT_AUTH:
            logger.debug("Simulated band: ignoring message for endpoint %s", endpoint)
            return
        if self.authenticated:
            return
        if message.startswith(_AUTH_STEP1_PREFIX):
            self._handle_auth_step1(message[len(_AUTH_STEP1_PREFIX) :])
        elif message[:1] == bytes([_AUTH_STEP2_OPCODE]):
            self._handle_auth_step2(message[1:])
        else:
            logger.debug("Simulated band: unknown auth message %s", message.hex())

    # ---------------------------------------------------------------- auth
    def _handle_auth_step1(self, client_public_key: bytes) -> None:
        if len(client_public_key) != 48:
            logger.warning("Simulated band: bad client public key length %d", len(client_public_key))
            return
        shared_secret = compute_shared_secret(self._prv_key, client_public_key)
        self._session_key = bytes(
            shared_secret[index + 8] ^ self.auth_key[index] for index in range(16)
        )
        self._emit_auth_step1_response()

    def _emit_auth_step1_response(self) -> None:
        payload = self._salt + self._pub_key
        total_length = len(payload) + _AUTH_TOTAL_LENGTH_FIELD

        first_header = bytearray(14)
        first_header[0] = 0x03
        first_header[1] = 0x01
        first_header[3] = 0x00  # handle
        first_header[4] = 0x00  # sequence
        first_header[5:9] = total_length.to_bytes(4, "little")
        first_header[9] = 0x82
        first_header[10] = 0x00
        first_header[11] = 0x10
        first_header[12] = 0x04  # auth step
        first_header[13] = 0x01  # status OK

        packets: list[bytes] = [bytes(first_header) + payload[:9]]
        offset = 9
        sequence = 1
        while offset < len(payload):
            fragment = payload[offset : offset + 18]
            header = bytearray(5)
            header[0] = 0x03
            header[1] = 0x06 if offset + len(fragment) >= len(payload) else 0x00
            header[3] = 0x00
            header[4] = sequence
            packets.append(bytes(header) + fragment)
            offset += len(fragment)
            sequence += 1

        for index, packet in enumerate(packets):
            if index in self.drop_auth_seqs:
                logger.warning("Simulated band: dropping auth fragment seq=%d", index)
                continue
            self._timers.append(
                self._loop.call_later(  # type: ignore[union-attr]
                    0.005 * index, self._emit, UUID_CHAR_CHUNKED_READ, packet
                )
            )

    def _handle_auth_step2(self, body: bytes) -> None:
        ok = False
        if self._session_key is not None and len(body) >= 32:
            iv = b"\x00" * 16
            expected_challenge = _aes_cbc_encrypt(self.auth_key, iv, self._salt)
            expected_session = _aes_cbc_encrypt(self._session_key, iv, self._salt)
            ok = (
                not self._fail_this_auth
                and body[:16] == expected_challenge
                and body[16:32] == expected_session
            )
        self.authenticated = ok
        logger.debug("Simulated band: auth step 2 verified=%s", ok)

        packet = bytearray(14)
        packet[0] = 0x03
        packet[1] = 0x01
        packet[3] = 0x01
        packet[4] = 0x00
        packet[5:9] = (3).to_bytes(4, "little")
        packet[9] = 0x82
        packet[10] = 0x00
        packet[11] = 0x10
        packet[12] = 0x05
        packet[13] = 0x01 if ok else 0x02
        self._timers.append(
            self._loop.call_later(  # type: ignore[union-attr]
                0.005, self._emit, UUID_CHAR_CHUNKED_READ, bytes(packet)
            )
        )

    # ------------------------------------------------------------ streams
    def _schedule_steps(self) -> None:
        if self._steps_scheduled or not self.steps_packets:
            return
        self._steps_scheduled = True
        for index, packet in enumerate(self.steps_packets):
            self._timers.append(
                self._loop.call_later(  # type: ignore[union-attr]
                    0.01 * (index + 1), self._emit, UUID_CHAR_STEPS, packet
                )
            )

    def _schedule_imu(self) -> None:
        if self._imu_scheduled or not self.imu_packets:
            return
        self._imu_scheduled = True
        for index, packet in enumerate(self.imu_packets):
            self._timers.append(
                self._loop.call_later(  # type: ignore[union-attr]
                    0.01 * (index + 1), self._emit, UUID_CHAR_RAW_SENSOR_DATA, packet
                )
            )

    def _emit(self, uuid: str, payload: bytes) -> None:
        if not self.connected:
            return
        callback = self._notify_callbacks.get(uuid)
        if callback is None:
            return
        callback(uuid, bytearray(payload))


class FakeBleakClient:
    """Just enough of ``bleak.BleakClient`` to drive the real client code."""

    def __init__(
        self,
        address_or_ble_device,
        disconnected_callback: Optional[Callable] = None,
        *,
        band: Optional[SimulatedBand] = None,
        timeout: Optional[float] = None,
        **_kwargs,
    ) -> None:
        self.address = getattr(address_or_ble_device, "address", str(address_or_ble_device))
        self.disconnected_callback = disconnected_callback
        self.timeout = timeout
        self.band = band
        if self.band is None:
            raise RuntimeError(
                "FakeBleakClient requires a SimulatedBand instance "
                "(use sim_band.simulated_factories() or pass band=...)."
            )
        self.is_connected = False

    @property
    def mtu_size(self) -> int:
        return 23

    async def connect(self) -> None:
        self.is_connected = True
        await asyncio.sleep(0)
        self.band.on_connect(self)

    async def disconnect(self) -> None:
        if self.is_connected:
            self.is_connected = False
            self.band.on_disconnect()

    def simulate_external_disconnect(self) -> None:
        """Drop the link as if the band went out of range or slept."""
        if not self.is_connected:
            return
        self.is_connected = False
        self.band.on_disconnect()
        callback = self.disconnected_callback
        if callback is not None:
            try:
                callback(self)
            except Exception:  # pragma: no cover - defensive
                logger.exception("Simulated disconnect callback failed")

    async def start_notify(self, char_specifier, callback) -> None:
        if not self.is_connected:
            raise OSError("simulated client is not connected")
        self.band.subscribe(str(char_specifier), callback)

    async def stop_notify(self, char_specifier) -> None:
        self.band.unsubscribe(str(char_specifier))

    async def write_gatt_char(self, char_specifier, data, response: Optional[bool] = None) -> None:
        if not self.is_connected:
            raise OSError("simulated client is not connected")
        self.band.feed_write(str(char_specifier), bytes(data))

    async def read_gatt_char(self, char_specifier) -> bytes:
        if not self.is_connected:
            raise OSError("simulated client is not connected")
        return self.band.read(str(char_specifier))


class FakeBleakScanner:
    """Just enough of ``bleak.BleakScanner`` to feed the client one device."""

    default_address = DEFAULT_DEMO_MAC
    default_rssi = -42

    def __init__(self, detection_callback: Optional[Callable] = None, **_kwargs) -> None:
        self.detection_callback = detection_callback
        self._running = False

    async def start(self) -> None:
        self._running = True
        if self.detection_callback is not None:
            device = FakeDevice(self.default_address)
            advertisement = FakeAdvertisement(rssi=self.default_rssi)
            asyncio.get_running_loop().call_soon(self.detection_callback, device, advertisement)

    async def stop(self) -> None:
        self._running = False

    @classmethod
    async def find_device_by_address(cls, address: str, timeout: float = 5.0) -> FakeDevice:
        await asyncio.sleep(0)
        return FakeDevice(address)

    @classmethod
    async def discover(cls, timeout: float = 5.0, return_adv: bool = False):
        await asyncio.sleep(0)
        device = FakeDevice(cls.default_address)
        if return_adv:
            return {device.address: (device, FakeAdvertisement())}
        return [device]


def simulated_factories(
    band: SimulatedBand,
) -> tuple[Callable, type[FakeBleakScanner]]:
    """Return ``(client_factory, scanner_factory)`` wired to *band*."""

    def client_factory(device_or_address, **kwargs):
        return FakeBleakClient(device_or_address, band=band, **kwargs)

    return client_factory, FakeBleakScanner


def make_simulated_client(
    mac: str,
    auth_key_hex: str,
    band: SimulatedBand,
    *,
    scanner_address: Optional[str] = None,
    device_name: Optional[str] = None,
    mtu: Optional[int] = None,
) -> MiBand6Client:
    """Build a real :class:`MiBand6Client` wired to the fake backend."""
    client_factory, scanner_factory = simulated_factories(band)
    if scanner_address is not None:
        FakeBleakScanner.default_address = scanner_address
    return MiBand6Client(
        mac,
        auth_key_hex,
        device_name=device_name,
        mtu=mtu,
        client_factory=client_factory,
        scanner_factory=scanner_factory,
    )


# ----------------------------------------------------------------- helpers
def _aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    return encryptor.update(data) + encryptor.finalize()


def _steps_packet(steps: int, distance_m: int, calories: int) -> bytes:
    return (
        b"\x01"
        + steps.to_bytes(4, "little")
        + distance_m.to_bytes(4, "little")
        + calories.to_bytes(4, "little")
    )


def _activity_sample(
    timestamp: float,
    *,
    kind: int = 1,
    intensity: int = 10,
    steps: int = 0,
    heart_rate: int = 0,
    unknown1: int = 0,
    sleep: int = 0,
    deep_sleep: int = 0,
    rem_sleep: int = 0,
) -> dict:
    """Build one per-minute historical activity sample for the simulator."""
    return {
        "ts": float(timestamp),
        "kind": kind,
        "intensity": intensity,
        "steps": steps,
        "heart_rate": heart_rate,
        "unknown1": unknown1,
        "sleep": sleep,
        "deep_sleep": deep_sleep,
        "rem_sleep": rem_sleep,
    }


def _imu_packet(sequence: int, samples: list[tuple[int, int, int]]) -> bytes:
    packet = bytearray([0x01, sequence & 0xFF])
    for x, y, z in samples:
        for value in (x, y, z):
            packet.extend(int(value).to_bytes(2, "little", signed=True))
    return bytes(packet)
