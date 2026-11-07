"""Mi Band 6 BLE client: chunked-ECDH authentication + real-time telemetry.

Pipeline
--------
1. **Discover** the band with a continuous BLE scan (the band only advertises
   while its screen is awake and it is not connected to a phone).
2. **Authenticate** with the Huami "chunked transfer" protocol on
   characteristics ``0x0016`` (write) / ``0x0017`` (notify):
   NIST B-163 ECDH + AES-CBC challenge/response, with hardened reassembly
   (sequence continuity, bounds checks, duplicate handling).
3. **Stream** heart rate (standard BLE SIG service), real-time steps
   (private ``0x0007``) and optionally raw IMU accelerometer data
   (``0x0001``/``0x0002``).
4. **Persist** every sample with a UTC timestamp through :mod:`storage`
   (JSONL and/or SQLite), or just log it.

The supervisor :func:`run_monitor` reconnects with exponential backoff after
errors or unexpected disconnects. ``--demo`` runs the whole stack against the
in-memory simulator from :mod:`sim_band`, so no Bluetooth hardware is needed.

CLI::

    python miband_client.py 60 --imu          # 60 s of HR + steps + IMU
    python miband_client.py --demo 10 --imu   # same, simulated band
    python miband_client.py --export-csv      # also export SQLite -> CSV
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from dotenv import load_dotenv

from bleak import BleakClient, BleakScanner

from ecdh_wrapper import compute_shared_secret, generate_ecdh_keys
from export_csv import export_database
from storage import STORAGE_BACKENDS, TelemetryStore

logger = logging.getLogger("MiBand6Client")

# BLE GATT Service and Characteristic UUIDs
UUID_SVC_MIBAND1 = "0000fee0-0000-1000-8000-00805f9b34fb"
UUID_CHAR_RAW_SENSOR_CONTROL = "00000001-0000-3512-2118-0009af100700"
UUID_CHAR_RAW_SENSOR_DATA = "00000002-0000-3512-2118-0009af100700"
UUID_CHAR_STEPS = "00000007-0000-3512-2118-0009af100700"
UUID_CHAR_CHUNKED_WRITE = "00000016-0000-3512-2118-0009af100700"
UUID_CHAR_CHUNKED_READ = "00000017-0000-3512-2118-0009af100700"

UUID_SVC_HEART_RATE = "0000180d-0000-1000-8000-00805f9b34fb"
UUID_CHAR_HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
UUID_CHAR_HR_CONTROL = "00002a39-0000-1000-8000-00805f9b34fb"

CHUNKED_ENDPOINT_AUTH = 0x82

# Chunked-transfer framing constants.
CHUNKED_DEFAULT_MTU = 23
CHUNKED_FIRST_HEADER_SIZE = 11
CHUNKED_NEXT_HEADER_SIZE = 5
CHUNKED_ATT_OVERHEAD = 3

# The band's step-1 auth payload is always 16-byte challenge + 48-byte pubkey.
AUTH_STEP1_RANDOM_SIZE = 16
AUTH_STEP1_PUBKEY_SIZE = 48
AUTH_STEP1_PAYLOAD_SIZE = AUTH_STEP1_RANDOM_SIZE + AUTH_STEP1_PUBKEY_SIZE
AUTH_TOTAL_LENGTH_OVERHEAD = 3
MAX_REASSEMBLY_BYTES = 1024

# Raw sensor activation opcodes from the Huami2021 protocol.
CMD_RAW_SENSOR_START_1 = b"\x01\x03\x19"  # 25 Hz sampling rate
CMD_RAW_SENSOR_START_2 = b"\x01\x03\x00\x00\x00\x19"
CMD_RAW_SENSOR_START_3 = b"\x02"
CMD_RAW_SENSOR_STOP = b"\x03"

DEMO_DEFAULT_MAC = "C0:FF:EE:C0:FF:EE"
DEMO_DEFAULT_AUTH_KEY = "0123456789abcdef0123456789abcdef"


# --------------------------------------------------------------------------
# Crypto / parsing helpers
# --------------------------------------------------------------------------
def _aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    return encryptor.update(data) + encryptor.finalize()


def encrypt_challenge(auth_key: bytes, challenge_bytes: bytes) -> bytes:
    """Encrypt a 16-byte random challenge using AES-128-ECB without padding."""
    if len(auth_key) != 16:
        raise ValueError(f"Auth key must be exactly 16 bytes, got {len(auth_key)}")
    if len(challenge_bytes) != 16:
        raise ValueError(f"Challenge payload must be exactly 16 bytes, got {len(challenge_bytes)}")

    cipher = Cipher(algorithms.AES(auth_key), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(challenge_bytes) + encryptor.finalize()


def parse_heart_rate(data: bytearray) -> int:
    """Parse standard Bluetooth SIG Heart Rate Measurement payload."""
    if len(data) < 2:
        raise ValueError("Invalid heart rate measurement data length.")

    flags = data[0]
    # Bit 0 determines format: 0 -> uint8, 1 -> uint16
    if flags & 0x01 == 0:
        return int(data[1])
    return int.from_bytes(data[1:3], byteorder="little")


def parse_realtime_steps(data: bytearray | bytes) -> dict:
    """Parse real-time steps payload from characteristic 0x0007.

    Typical 13-byte Huami format:
    data[0]: status / mode
    data[1:5]: current step count (uint32 little-endian, or uint16 at [1:3])
    data[5:9]: meters / distance (uint32 little-endian)
    data[9:13]: calories burned (uint32 little-endian)
    """
    if len(data) < 3:
        raise ValueError(f"Real-time steps data too short: {len(data)} bytes")

    if len(data) >= 5:
        steps = int.from_bytes(data[1:5], byteorder="little")
    else:
        steps = int.from_bytes(data[1:3], byteorder="little")

    distance_meters = int.from_bytes(data[5:9], byteorder="little") if len(data) >= 9 else 0
    calories = int.from_bytes(data[9:13], byteorder="little") if len(data) >= 13 else 0

    return {
        "steps": steps,
        "distance_meters": distance_meters,
        "calories": calories,
    }


def parse_raw_sensor_data(data: bytearray | bytes) -> dict:
    """Parse raw 3-axis accelerometer/IMU samples from characteristic 0x0002.

    Byte 0: sensor frame type (0x00: 1Hz low-freq, 0x01: ~25Hz high-freq)
    Byte 1: sequence counter (0-255 rolling index)
    Bytes 2..end: 6-byte triplets (signed int16 x, y, z in little-endian)
    """
    if len(data) < 2:
        raise ValueError(f"Raw sensor payload too short: {len(data)} bytes")

    sensor_type = data[0]
    seq_index = data[1]
    payload = data[2:]

    if len(payload) % 6 != 0:
        raise ValueError(f"Sensor payload length ({len(payload)}) not divisible by 6")

    scale_factor = 4100.0 if sensor_type == 0x00 else 256.0
    gravity = 9.80665

    samples = []
    for i in range(0, len(payload), 6):
        x, y, z = struct.unpack("<hhh", payload[i : i + 6])
        samples.append({
            "x_raw": x,
            "y_raw": y,
            "z_raw": z,
            "ax_g": round(x / scale_factor, 4),
            "ay_g": round(y / scale_factor, 4),
            "az_g": round(z / scale_factor, 4),
            "ax_ms2": round((x / scale_factor) * gravity, 4),
            "ay_ms2": round((y / scale_factor) * gravity, 4),
            "az_ms2": round((z / scale_factor) * gravity, 4),
        })

    return {
        "type": sensor_type,
        "sequence": seq_index,
        "sample_count": len(samples),
        "samples": samples,
    }


def normalize_auth_key(auth_key_hex: str) -> bytes:
    """Validate and decode a 32-hex-char auth key (``0x`` prefix tolerated)."""
    cleaned = (auth_key_hex or "").strip().lower()
    if cleaned.startswith("0x"):
        cleaned = cleaned[2:]
    if len(cleaned) != 32:
        raise ValueError(f"Auth key hex string must be 32 characters, got {len(cleaned)}")
    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ValueError(f"Auth key is not valid hexadecimal: {exc}") from exc


def _resolve_mtu(client: object, mtu: Optional[int] = None) -> int:
    """Best-effort MTU discovery with a conservative floor of 23 bytes."""
    candidate: object = mtu
    if candidate is None:
        try:
            candidate = client.mtu_size  # type: ignore[attr-defined]
        except Exception:
            candidate = None
    try:
        resolved = int(candidate)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        resolved = CHUNKED_DEFAULT_MTU
    return max(CHUNKED_DEFAULT_MTU, resolved)


async def write_chunked_value(
    client: BleakClient,
    char_uuid: str,
    endpoint_type: int,
    handle: int,
    data: bytes,
    mtu: Optional[int] = None,
) -> None:
    """Split and transmit a payload using the Mi Band 6 Chunked Transfer Protocol.

    The first ATT write carries an 11-byte header (total length + endpoint);
    the remaining writes carry a 5-byte header. All payload sizes are derived
    from the negotiated MTU (minimum 23 bytes, i.e. 20-byte ATT payload).
    """
    if not data:
        raise ValueError("Cannot send an empty chunked payload")

    resolved_mtu = _resolve_mtu(client, mtu)
    first_payload_max = resolved_mtu - CHUNKED_ATT_OVERHEAD - CHUNKED_FIRST_HEADER_SIZE
    next_payload_max = resolved_mtu - CHUNKED_ATT_OVERHEAD - CHUNKED_NEXT_HEADER_SIZE
    if first_payload_max <= 0 or next_payload_max <= 0:
        raise ValueError(f"MTU {resolved_mtu} is too small for the chunked protocol")

    total_length = len(data)
    offset = 0
    count = 0
    first = True
    while offset < total_length:
        header_size = CHUNKED_FIRST_HEADER_SIZE if first else CHUNKED_NEXT_HEADER_SIZE
        max_chunk = first_payload_max if first else next_payload_max
        copy_bytes = min(total_length - offset, max_chunk)
        is_last = offset + copy_bytes >= total_length

        flags = 0x01 if first else 0x00
        if is_last:
            flags |= 0x06  # end of transmission frame

        chunk = bytearray(header_size + copy_bytes)
        chunk[0] = 0x03
        chunk[1] = flags
        chunk[2] = 0x00
        chunk[3] = handle & 0xFF
        chunk[4] = count & 0xFF
        if first:
            chunk[5:9] = total_length.to_bytes(4, "little")
            chunk[9:11] = (endpoint_type & 0xFFFF).to_bytes(2, "little")
        chunk[header_size:] = data[offset : offset + copy_bytes]

        await client.write_gatt_char(char_uuid, bytes(chunk), response=False)
        offset += copy_bytes
        count += 1
        first = False


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------
@dataclass
class StreamCallbacks:
    """Optional consumers for the three telemetry streams."""

    heart_rate: Optional[Callable[[int], None]] = None
    steps: Optional[Callable[[dict], None]] = None
    imu: Optional[Callable[[dict], None]] = None


class MiBand6Client:
    """Handles BLE connection, chunked ECDH auth, and real-time data streaming."""

    def __init__(
        self,
        mac_address: str,
        auth_key_hex: str,
        *,
        client_factory: Optional[Callable] = None,
        scanner_factory: Optional[Callable] = None,
        scan_timeout: float = 90.0,
        connect_timeout: float = 25.0,
        auth_timeout: float = 30.0,
    ) -> None:
        self.mac_address = mac_address.strip()
        self.auth_key = normalize_auth_key(auth_key_hex)
        self._client_factory = client_factory or BleakClient
        self._scanner_factory = scanner_factory or BleakScanner
        self.scan_timeout = scan_timeout
        self.connect_timeout = connect_timeout
        self.auth_timeout = auth_timeout

        self.client: Optional[BleakClient] = None
        self.authenticated = False

        self._auth_event = asyncio.Event()
        self._auth_error: Optional[str] = None
        self._disconnected = asyncio.Event()
        self._closing = False

        self._hr_callback: Optional[Callable[[int], None]] = None
        self._steps_callback: Optional[Callable[[dict], None]] = None
        self._sensor_callback: Optional[Callable[[dict], None]] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._hr_active = False
        self._steps_active = False
        self._sensor_active = False

        # Chunked auth reassembly state (reset per connection).
        self._prv_key = b""
        self._pub_key = b""
        self._handle = 0
        self._reassemble_buf = bytearray(MAX_REASSEMBLY_BYTES)
        self._reassemble_ptr = 0
        self._expected_bytes = 0
        self._next_seq = 0
        self._step2_sent = False

    # ------------------------------------------------------------- properties
    @property
    def disconnected_event(self) -> asyncio.Event:
        """Event set when the BLE link drops unexpectedly (watchdog signal)."""
        return self._disconnected

    async def wait_until_disconnected(self) -> None:
        await self._disconnected.wait()

    # ---------------------------------------------------------------- discovery
    async def _discover_device(self, scan_timeout: float):
        found_device = None
        stop_event = asyncio.Event()

        def _on_detection(device, _advertisement) -> None:
            nonlocal found_device
            if str(getattr(device, "address", "")).upper() == self.mac_address.upper():
                found_device = device
                stop_event.set()

        scanner = self._scanner_factory(detection_callback=_on_detection)
        await scanner.start()
        heartbeat = asyncio.create_task(self._scan_heartbeat(stop_event))
        try:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=scan_timeout)
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"Could not discover band {self.mac_address} within {scan_timeout:.0f}s. "
                    "Please verify: 1) Phone Bluetooth is OFF / Airplane mode. "
                    "2) Band screen is touched and lit."
                ) from None
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            await scanner.stop()
        return found_device

    @staticmethod
    async def _scan_heartbeat(stop_event: asyncio.Event) -> None:
        elapsed = 0
        while not stop_event.is_set():
            await asyncio.sleep(5.0)
            if stop_event.is_set():
                return
            elapsed += 5
            logger.info(
                "Scanning for band... (%ds elapsed, tap and keep sliding band screen)", elapsed
            )

    # ------------------------------------------------------------ connection
    async def connect_and_authenticate(
        self,
        scan_timeout: Optional[float] = None,
        connect_timeout: Optional[float] = None,
        auth_timeout: Optional[float] = None,
    ) -> None:
        """Discover, connect and execute the chunked-transfer ECDH handshake."""
        scan_timeout = self.scan_timeout if scan_timeout is None else scan_timeout
        connect_timeout = self.connect_timeout if connect_timeout is None else connect_timeout
        auth_timeout = self.auth_timeout if auth_timeout is None else auth_timeout

        self._reset_auth_state()
        self._disconnected.clear()
        self._prv_key, self._pub_key = generate_ecdh_keys()

        logger.info(
            "Waiting for Mi Band broadcast at %s (touch/light up band screen)...", self.mac_address
        )
        found_device = await self._discover_device(scan_timeout)
        logger.info("Band detected! Establishing BLE link (keep screen awake)...")

        # A fresh client object per session avoids stale D-Bus device handles.
        self.client = self._client_factory(
            found_device,
            timeout=connect_timeout,
            disconnected_callback=self._on_disconnected,
        )
        await self.client.connect()
        logger.info("BLE link established. Initializing chunked transfer listener...")

        await self.client.start_notify(UUID_CHAR_CHUNKED_READ, self._on_chunked_notification)

        initial_auth = b"\x04\x02\x00\x02" + self._pub_key
        logger.info("Initiating chunked ECDH handshake (sending client public key)...")
        await write_chunked_value(
            self.client,
            UUID_CHAR_CHUNKED_WRITE,
            CHUNKED_ENDPOINT_AUTH,
            0,
            initial_auth,
        )

        try:
            await asyncio.wait_for(self._auth_event.wait(), timeout=auth_timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"Chunked ECDH authentication timed out after {auth_timeout:.0f}s"
            ) from None

        if self._auth_error:
            raise RuntimeError(f"Authentication failed: {self._auth_error}")

        self.authenticated = True
        logger.info("Chunked ECDH authentication SUCCEEDED. Session is ready.")

    def _on_disconnected(self, _client: object) -> None:
        if self._closing:
            return
        logger.warning("BLE link to %s lost (signal watchdog).", self.mac_address)
        if self._keepalive_task is not None and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        self._disconnected.set()

    # --------------------------------------------------- chunked auth hardening
    def _reset_auth_state(self) -> None:
        self.authenticated = False
        self._auth_error = None
        self._auth_event.clear()
        self._handle = 0
        self._reassemble_ptr = 0
        self._expected_bytes = 0
        self._next_seq = 0
        self._step2_sent = False

    def _fail_auth(self, reason: str) -> None:
        if self._auth_event.is_set():
            logger.debug("Ignoring late auth failure after handshake settled: %s", reason)
            return
        self._auth_error = reason
        logger.error("Chunked authentication failed: %s", reason)
        self._auth_event.set()

    def _on_chunked_notification(self, _sender, data: bytearray) -> None:
        """Process one chunked-transfer notification from the band."""
        try:
            self._consume_chunked_packet(bytes(data))
        except Exception as exc:  # never let a malformed frame escape a BLE callback
            logger.exception("Error while handling chunked notification")
            if not self.authenticated and not self._auth_event.is_set():
                self._fail_auth(f"chunked notification handling error: {exc}")

    def _consume_chunked_packet(self, packet: bytes) -> None:
        if len(packet) < CHUNKED_NEXT_HEADER_SIZE or packet[0] != 0x03:
            logger.debug("Ignoring malformed chunked packet (%d bytes)", len(packet))
            return

        seq = packet[4]
        is_auth_control = (
            seq == 0
            and len(packet) >= 14
            and packet[9] == CHUNKED_ENDPOINT_AUTH
            and packet[10] == 0x00
            and packet[11] == 0x10
        )
        if is_auth_control:
            self._handle_auth_control(packet)
            return
        if seq > 0:
            self._handle_auth_fragment(packet)

    def _handle_auth_control(self, packet: bytes) -> None:
        step = packet[12]
        status = packet[13]

        if step == 0x04:
            if status != 0x01:
                self._fail_auth(f"auth step 1 rejected by band (status 0x{status:02x})")
                return
            if self._step2_sent:
                return
            if self._reassemble_ptr > 0:
                logger.debug("Ignoring duplicate auth step-1 header packet")
                return

            total_length = int.from_bytes(packet[5:9], "little")
            expected = total_length - AUTH_TOTAL_LENGTH_OVERHEAD
            if not (AUTH_STEP1_PAYLOAD_SIZE <= expected <= MAX_REASSEMBLY_BYTES):
                self._fail_auth(
                    f"invalid auth payload size (total={total_length}, expected={expected})"
                )
                return

            self._expected_bytes = expected
            self._next_seq = 1
            logger.info(
                "Auth step 1 acknowledged; reassembling %d-byte challenge (16B salt + 48B pubkey)...",
                expected,
            )
            self._append_auth_fragment(packet[14:])
            return

        if step == 0x05:
            if status == 0x01:
                logger.info("Auth step 2 acknowledged by band.")
                self._auth_event.set()
            else:
                self._fail_auth(f"auth step 2 rejected by band (status 0x{status:02x})")
            return

        self._fail_auth(f"unexpected auth step 0x{step:02x}")

    def _handle_auth_fragment(self, packet: bytes) -> None:
        if self._expected_bytes <= 0 or self._step2_sent or self._auth_event.is_set():
            return

        seq = packet[4]
        if seq < self._next_seq:
            logger.debug(
                "Ignoring duplicate auth fragment seq=%d (expected %d)", seq, self._next_seq
            )
            return
        if seq > self._next_seq:
            self._fail_auth(
                f"auth fragment gap: expected seq {self._next_seq}, received {seq}; "
                "BLE fragments were dropped — the session will be retried"
            )
            return
        self._append_auth_fragment(packet[CHUNKED_NEXT_HEADER_SIZE:])
        self._next_seq = seq + 1

    def _append_auth_fragment(self, payload: bytes) -> None:
        if not payload:
            return
        end = self._reassemble_ptr + len(payload)
        if end > self._expected_bytes:
            self._fail_auth(
                f"auth reassembly overflow: {end} bytes > expected {self._expected_bytes}"
            )
            return

        self._reassemble_buf[self._reassemble_ptr : end] = payload
        self._reassemble_ptr = end

        if self._reassemble_ptr < self._expected_bytes or self._step2_sent:
            return

        self._step2_sent = True
        logger.info(
            "Auth challenge fully reassembled (%d bytes). Computing ECDH shared secret...",
            self._expected_bytes,
        )
        asyncio.create_task(self._complete_auth_step2())

    async def _complete_auth_step2(self) -> None:
        """Compute the ECDH shared secret and send the AES-encrypted response."""
        try:
            if self.client is None:
                raise RuntimeError("BLE client is not connected")
            remote_random = bytes(self._reassemble_buf[:AUTH_STEP1_RANDOM_SIZE])
            remote_public = bytes(
                self._reassemble_buf[
                    AUTH_STEP1_RANDOM_SIZE : AUTH_STEP1_RANDOM_SIZE + AUTH_STEP1_PUBKEY_SIZE
                ]
            )
            logger.debug("Received random challenge salt (%s)", remote_random.hex())
            logger.debug("Received band ECDH public key (%s)", remote_public.hex())

            shared_secret = compute_shared_secret(self._prv_key, remote_public)
            session_key = bytes(
                shared_secret[index + 8] ^ self.auth_key[index] for index in range(16)
            )

            iv = b"\x00" * 16
            encrypted_auth_key = _aes_cbc_encrypt(self.auth_key, iv, remote_random)
            encrypted_session_key = _aes_cbc_encrypt(session_key, iv, remote_random)

            self._handle += 1
            response = b"\x05" + encrypted_auth_key + encrypted_session_key
            logger.info("Sending chunked auth step 2 response (%d bytes)...", len(response))
            await write_chunked_value(
                self.client,
                UUID_CHAR_CHUNKED_WRITE,
                CHUNKED_ENDPOINT_AUTH,
                self._handle,
                response,
            )
        except Exception as exc:
            logger.error("Failed to complete auth step 2: %s", exc)
            self._fail_auth(f"could not complete auth step 2: {exc}")

    # ---------------------------------------------------------- data streams
    def _on_heart_rate_notification(self, _sender, data: bytearray) -> None:
        try:
            bpm = parse_heart_rate(data)
            logger.info("❤️  Current Heart Rate: %d BPM", bpm)
            if self._hr_callback:
                self._hr_callback(bpm)
        except Exception as exc:
            logger.error("Failed to parse heart rate measurement: %s", exc)

    def _on_steps_notification(self, _sender, data: bytearray) -> None:
        try:
            steps_info = parse_realtime_steps(data)
            logger.info(
                "👟 Steps: %d | Distance: %d m | Calories: %d kcal",
                steps_info["steps"],
                steps_info["distance_meters"],
                steps_info["calories"],
            )
            if self._steps_callback:
                self._steps_callback(steps_info)
        except Exception as exc:
            logger.error("Failed to parse realtime steps notification: %s", exc)

    def _on_sensor_notification(self, _sender, data: bytearray) -> None:
        try:
            sensor_info = parse_raw_sensor_data(data)
            if sensor_info["samples"]:
                first = sensor_info["samples"][0]
                logger.debug(
                    "📊 IMU [Seq %d, %d samples] Accel: (%.2fg, %.2fg, %.2fg)",
                    sensor_info["sequence"],
                    sensor_info["sample_count"],
                    first["ax_g"],
                    first["ay_g"],
                    first["az_g"],
                )
            if self._sensor_callback:
                self._sensor_callback(sensor_info)
        except Exception as exc:
            logger.error("Failed to parse raw sensor notification: %s", exc)

    async def start_heart_rate_stream(self, callback: Optional[Callable[[int], None]] = None) -> None:
        """Subscribe to real-time heart rate notifications and start keep-alive."""
        if self.client is None:
            raise RuntimeError("Client is not connected")
        self._hr_callback = callback
        logger.info("Initializing heart rate sensor...")

        # Reset measurement state, subscribe, then start continuous measurement.
        await self.client.write_gatt_char(UUID_CHAR_HR_CONTROL, b"\x15\x02\x00", response=False)
        await self.client.write_gatt_char(UUID_CHAR_HR_CONTROL, b"\x15\x01\x00", response=False)
        await self.client.start_notify(UUID_CHAR_HR_MEASUREMENT, self._on_heart_rate_notification)
        await self.client.write_gatt_char(UUID_CHAR_HR_CONTROL, b"\x15\x01\x01", response=False)

        self._hr_active = True
        self._keepalive_task = asyncio.create_task(self._hr_keepalive_loop())
        logger.info("Continuous heart rate monitoring active!")

    async def _hr_keepalive_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(12)
                if self.client is None:
                    return
                logger.debug("Pinging heart rate monitor keep-alive [0x16]...")
                await self.client.write_gatt_char(UUID_CHAR_HR_CONTROL, b"\x16", response=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Heart-rate keep-alive failed; treating link as lost: %s", exc)
            self._disconnected.set()

    async def stop_heart_rate_stream(self) -> None:
        """Stop the heart-rate stream and cancel the keep-alive loop."""
        if self._keepalive_task is not None and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._keepalive_task
        if self._hr_active and self.client is not None:
            try:
                await self.client.stop_notify(UUID_CHAR_HR_MEASUREMENT)
            except Exception as exc:
                logger.debug("Error while stopping heart-rate notify: %s", exc)
        self._hr_active = False

    async def start_steps_stream(self, callback: Optional[Callable[[dict], None]] = None) -> None:
        """Subscribe to real-time steps and calories notifications."""
        if self.client is None:
            raise RuntimeError("Client is not connected")
        self._steps_callback = callback
        logger.info("Enabling real-time steps stream...")
        try:
            initial_data = await self.client.read_gatt_char(UUID_CHAR_STEPS)
            if initial_data:
                self._on_steps_notification(None, bytearray(initial_data))
        except Exception as exc:
            logger.debug("Failed initial steps read: %s", exc)

        await self.client.start_notify(UUID_CHAR_STEPS, self._on_steps_notification)
        self._steps_active = True
        logger.info("Real-time steps notification active!")

    async def stop_steps_stream(self) -> None:
        if self._steps_active and self.client is not None:
            try:
                await self.client.stop_notify(UUID_CHAR_STEPS)
            except Exception as exc:
                logger.debug("Error while stopping steps notify: %s", exc)
        self._steps_active = False

    async def start_raw_sensor_stream(
        self, callback: Optional[Callable[[dict], None]] = None
    ) -> None:
        """Activate raw IMU accelerometer streaming at ~25 Hz."""
        if self.client is None:
            raise RuntimeError("Client is not connected")
        self._sensor_callback = callback
        logger.info("Initializing raw IMU sensor stream...")
        await self.client.write_gatt_char(
            UUID_CHAR_RAW_SENSOR_CONTROL, CMD_RAW_SENSOR_START_1, response=False
        )
        await self.client.write_gatt_char(
            UUID_CHAR_RAW_SENSOR_CONTROL, CMD_RAW_SENSOR_START_2, response=False
        )
        await self.client.write_gatt_char(
            UUID_CHAR_RAW_SENSOR_CONTROL, CMD_RAW_SENSOR_START_3, response=False
        )
        await self.client.start_notify(UUID_CHAR_RAW_SENSOR_DATA, self._on_sensor_notification)
        self._sensor_active = True
        logger.info("Raw IMU sensor streaming active!")

    async def stop_raw_sensor_stream(self) -> None:
        if self._sensor_active and self.client is not None:
            try:
                await self.client.write_gatt_char(
                    UUID_CHAR_RAW_SENSOR_CONTROL, CMD_RAW_SENSOR_STOP, response=False
                )
                await self.client.stop_notify(UUID_CHAR_RAW_SENSOR_DATA)
            except Exception as exc:
                logger.debug("Error while stopping raw sensor stream: %s", exc)
        self._sensor_active = False

    async def disconnect(self) -> None:
        """Disconnect cleanly and stop every active stream (idempotent)."""
        self._closing = True
        try:
            await self.stop_heart_rate_stream()
            await self.stop_steps_stream()
            await self.stop_raw_sensor_stream()
            client = self.client
            self.client = None
            if client is not None and getattr(client, "is_connected", False):
                try:
                    await client.disconnect()
                    logger.info("Disconnected from Mi Band.")
                except Exception as exc:
                    logger.debug("Error while disconnecting: %s", exc)
        finally:
            self._closing = False
            self._disconnected.set()


# --------------------------------------------------------------------------
# Session supervisor
# --------------------------------------------------------------------------
async def run_monitor(
    mac_address: str,
    auth_key_hex: str,
    *,
    duration: Optional[int] = None,
    imu: bool = False,
    callbacks: Optional[StreamCallbacks] = None,
    scan_timeout: float = 90.0,
    reconnect: bool = True,
    reconnect_initial_delay: float = 1.0,
    reconnect_max_delay: float = 30.0,
    client_factory: Optional[Callable[[str, str], MiBand6Client]] = None,
) -> None:
    """Connect + stream until *duration* elapses, reconnecting with backoff."""
    callbacks = callbacks or StreamCallbacks()
    make_client = client_factory or (
        lambda mac, key: MiBand6Client(mac, key, scan_timeout=scan_timeout)
    )
    deadline = None if duration is None else time.monotonic() + duration
    attempt = 0

    while True:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            logger.info("Collection window (%ss) elapsed.", duration)
            return

        attempt += 1
        client = make_client(mac_address, auth_key_hex)
        failure: Optional[BaseException] = None
        try:
            await client.connect_and_authenticate(scan_timeout=scan_timeout)
            await client.start_heart_rate_stream(callbacks.heart_rate)
            await client.start_steps_stream(callbacks.steps)
            if imu:
                await client.start_raw_sensor_stream(callbacks.imu)
            logger.info(
                "Streaming session %d started (duration=%s, imu=%s).",
                attempt,
                f"{duration}s" if duration is not None else "until Ctrl+C",
                imu,
            )

            if remaining is None:
                await client.wait_until_disconnected()
                failure = RuntimeError("BLE link lost")
            else:
                try:
                    await asyncio.wait_for(
                        client.wait_until_disconnected(), timeout=remaining
                    )
                    failure = RuntimeError("BLE link lost")
                except asyncio.TimeoutError:
                    logger.info("Collection window (%ss) elapsed.", duration)
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = exc
            logger.error("Session %d failed: %s", attempt, exc)
        finally:
            await client.disconnect()

        if not reconnect:
            if failure is not None and not client.authenticated:
                raise failure
            return

        delay = min(
            reconnect_max_delay,
            reconnect_initial_delay * (2 ** min(attempt - 1, 5)),
        )
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None:
            if remaining <= 0:
                return
            delay = min(delay, remaining)
        logger.info("Reconnecting in %.2fs (next attempt %d)...", delay, attempt + 1)
        if delay > 0:
            await asyncio.sleep(delay)


async def run_demo(
    mac_address: str,
    auth_key_hex: str,
    *,
    duration: Optional[int] = None,
    imu: bool = False,
    callbacks: Optional[StreamCallbacks] = None,
) -> None:
    """Run the full client stack against the in-memory simulator (no hardware)."""
    from sim_band import FakeBleakScanner, SimulatedBand, simulated_factories

    band = SimulatedBand(auth_key=normalize_auth_key(auth_key_hex))
    sim_client_factory, _scanner_factory = simulated_factories(band)

    class _DemoScanner(FakeBleakScanner):
        default_address = mac_address

    def _factory(mac: str, key: str) -> MiBand6Client:
        return MiBand6Client(
            mac,
            key,
            client_factory=sim_client_factory,
            scanner_factory=_DemoScanner,
        )

    logger.info("DEMO MODE: using an in-memory simulated Mi Band 6 (no Bluetooth needed).")
    await run_monitor(
        mac_address,
        auth_key_hex,
        duration=duration,
        imu=imu,
        callbacks=callbacks,
        scan_timeout=10.0,
        reconnect=True,
        reconnect_initial_delay=0.2,
        reconnect_max_delay=1.0,
        client_factory=_factory,
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _configure_logging(level_name: str) -> None:
    level = getattr(logging, (level_name or "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _env_float(name: str, default: float) -> float:
    """Read a float from the environment, falling back safely on bad values."""
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Stream Mi Band 6 heart rate, steps and IMU data to your computer."
    )
    parser.add_argument(
        "duration",
        nargs="?",
        type=int,
        default=None,
        help="collection duration in seconds (default: until Ctrl+C)",
    )
    parser.add_argument("--imu", action="store_true", help="also stream raw IMU accelerometer data")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="run against the in-memory simulated band (no Bluetooth hardware)",
    )
    parser.add_argument(
        "--output",
        default=os.getenv("OUTPUT_DIR", "data"),
        help="directory for JSONL files and mi_band.db (default: data)",
    )
    parser.add_argument(
        "--storage",
        choices=list(STORAGE_BACKENDS),
        default=os.getenv("STORAGE_BACKEND", "both").lower(),
        help="storage backend (default: both)",
    )
    parser.add_argument(
        "--export-csv",
        action="store_true",
        help="export the SQLite database to CSV files after the session",
    )
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=_env_float("SCAN_TIMEOUT", 90.0),
        help="seconds to wait for the band to advertise (default: 90)",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        help="DEBUG | INFO | WARNING | ERROR (default: INFO)",
    )
    parser.add_argument(
        "--no-reconnect",
        action="store_true",
        help="do not automatically reconnect after errors or link loss",
    )
    args = parser.parse_args(argv)
    if args.storage not in STORAGE_BACKENDS:
        args.storage = "both"
    return args


async def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    _configure_logging(args.log_level)

    mac = (os.getenv("DEVICE_MAC") or "").strip()
    key = (os.getenv("AUTH_KEY") or "").strip()
    demo = args.demo or os.getenv("DEMO_MODE", "0").lower() in ("1", "true", "yes", "on")

    if demo:
        if not mac or mac.upper().startswith("XX:"):
            mac = DEMO_DEFAULT_MAC
        try:
            normalize_auth_key(key)
        except ValueError:
            logger.warning(
                "DEMO_MODE: no valid AUTH_KEY configured; using throwaway demo key."
            )
            key = DEMO_DEFAULT_AUTH_KEY
    else:
        if not mac or mac.upper().startswith("XX:") or not key or key.startswith("012345"):
            logger.error(
                "Please configure DEVICE_MAC and AUTH_KEY in .env before running "
                "(or run with --demo)."
            )
            return 1
        try:
            normalize_auth_key(key)
        except ValueError as exc:
            logger.error("Invalid AUTH_KEY: %s", exc)
            return 1

    store = TelemetryStore(args.output, backend=args.storage)
    callbacks = StreamCallbacks(
        heart_rate=store.record_heart_rate,
        steps=store.record_steps,
        imu=store.record_imu,
    )
    if args.storage == "none":
        logger.info("Storage disabled; telemetry will only be logged.")
    else:
        logger.info(
            "Telemetry output: backend=%s -> %s",
            args.storage,
            Path(args.output).resolve(),
        )

    exit_code = 0
    try:
        if demo:
            await run_demo(
                mac,
                key,
                duration=args.duration,
                imu=args.imu,
                callbacks=callbacks,
            )
        else:
            await run_monitor(
                mac,
                key,
                duration=args.duration,
                imu=args.imu,
                callbacks=callbacks,
                scan_timeout=args.scan_timeout,
                reconnect=not args.no_reconnect,
            )
    except KeyboardInterrupt:
        logger.info("Stopping on user request...")
    except Exception as exc:
        logger.error("Monitoring stopped after error: %s", exc)
        exit_code = 1
    finally:
        store.close()
        logger.info("Storage summary: %s", store.stats())
        if args.export_csv:
            if args.storage in ("sqlite", "both"):
                try:
                    exported = export_database(store.db_path, Path(args.output) / "csv")
                    for table, (path, rows) in exported.items():
                        logger.info("CSV export: %s (%d rows) -> %s", table, rows, path)
                except Exception as exc:
                    logger.error("CSV export failed: %s", exc)
            else:
                logger.warning(
                    "--export-csv requires --storage sqlite or both (current: %s)",
                    args.storage,
                )
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        sys.exit(130)
