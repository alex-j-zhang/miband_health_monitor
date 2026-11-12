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

import bluez_support
from band_history import sync_history, summarize_history_result
from dashboard import DashboardServer, TelemetryHub, make_db_history_provider
from ecdh_wrapper import backend_name as ecdh_backend_name
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

#: Upper bound for the Linux "connect the bonded device without scanning"
#: attempt.  A sleeping / out-of-range band must not stall startup, so a failed
#: direct attempt falls back to discovery quickly.
DIRECT_CONNECT_TIMEOUT = 3.5

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

# Characteristic 0x0002 multiplexes several sensor streams, each prefixed with a
# one-byte frame type.  Verified against a real Mi Band 6: only ``0x00`` frames
# carry the 3-axis accelerometer, ``0x01`` looks like an optical (PPG) channel
# and ``0x07`` is a short periodic status frame.
RAW_SENSOR_TYPE_ACCEL = 0x00
RAW_SENSOR_TYPE_OPTICAL = 0x01
RAW_SENSOR_TYPE_STATUS = 0x07
#: Accelerometer full scale on the Mi Band 6 is +/-8 g at 4096 LSB/g
#: (resting magnitude measures 0.997 g, which pins the scale down).
RAW_SENSOR_ACCEL_LSB_PER_G = 4096.0

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
    """Parse one notification from the raw-sensor characteristic (``0x0002``).

    The band multiplexes several streams on this characteristic, each prefixed
    with a one-byte frame type and a rolling sequence counter:

    * ``0x00`` – 3-axis accelerometer, 17-18 samples per frame at ~25 Hz;
    * ``0x01`` – optical (PPG) channel, not accelerometer data;
    * ``0x07`` – short periodic status frame.

    Accelerometer frames are laid out as::

        byte 0     frame type (0x00)
        byte 1     sequence counter (0-255 rolling)
        bytes 2..  6-byte triplets: signed int16 LE x, y, z at 4096 LSB/g

    Frames of any other type are returned with an empty ``samples`` list so
    callers can ignore them instead of treating them as parse failures.
    """
    if len(data) < 2:
        raise ValueError(f"Raw sensor payload too short: {len(data)} bytes")

    sensor_type = data[0]
    seq_index = data[1]

    if sensor_type != RAW_SENSOR_TYPE_ACCEL:
        return {
            "type": sensor_type,
            "sequence": seq_index,
            "sample_count": 0,
            "samples": [],
        }

    payload = data[2:]

    if len(payload) % 6 != 0:
        raise ValueError(f"Sensor payload length ({len(payload)}) not divisible by 6")

    scale_factor = RAW_SENSOR_ACCEL_LSB_PER_G
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


# --------------------------------------------------------------------------
# Device identity helpers (cross-platform)
# --------------------------------------------------------------------------
#: Values that mean "not configured yet" in a fresh ``.env``.
PLACEHOLDER_DEVICE_PREFIXES = ("XX:", "XX-", "XX_")


def is_placeholder_device_id(device_id: Optional[str]) -> bool:
    """Return True for empty or template values such as ``XX:XX:...``."""
    value = (device_id or "").strip()
    if not value:
        return True
    return value.upper().startswith(PLACEHOLDER_DEVICE_PREFIXES)


def normalize_device_identifier(value: str) -> str:
    """Normalise a Bluetooth address / CoreBluetooth UUID for comparison.

    Handles ``AA:BB:CC:DD:EE:FF``, ``AA-BB-...``, lowercase input and the
    ``{UUID}`` form some tools print for macOS device identifiers.
    """
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


def device_matches(
    device_id: Optional[str],
    device_name: Optional[str],
    device: object,
    advertisement: Optional[object] = None,
) -> bool:
    """Return True when *device* matches the configured identifier or name.

    ``device_id`` is compared against ``device.address``; on Linux/Windows this
    is the Bluetooth MAC address, while on macOS CoreBluetooth exposes a
    per-host UUID there.  ``device_name`` (a case-insensitive substring) is an
    optional extra match that works on every platform, which makes it possible
    to pair the client with a band even when the address is unavailable.
    """
    wanted = normalize_device_identifier(device_id or "")
    if wanted:
        address = normalize_device_identifier(str(getattr(device, "address", "") or ""))
        if address and address == wanted:
            return True

    wanted_name = (device_name or "").strip().lower()
    if wanted_name:
        names = [
            getattr(device, "name", None),
            getattr(advertisement, "local_name", None),
        ]
        for candidate in names:
            if candidate and wanted_name in str(candidate).lower():
                return True
    return False


def _env_device_id() -> str:
    """Read the configured device identifier from the environment.

    ``DEVICE_ID`` is the cross-platform name (MAC address or macOS UUID);
    ``DEVICE_MAC`` is still accepted for backwards compatibility.  Placeholder
    template values are ignored so a legacy real value wins over the template.
    """
    values = [(os.getenv("DEVICE_ID") or "").strip(), (os.getenv("DEVICE_MAC") or "").strip()]
    for value in values:
        if value and not is_placeholder_device_id(value):
            return value
    return values[0] or values[1]


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


async def _write_with_in_progress_retry(
    client: Any, char_uuid: str, data: bytes, response: bool = False
) -> None:
    for attempt in range(2):
        try:
            await client.write_gatt_char(char_uuid, data, response=response)
            return
        except Exception as exc:
            if attempt == 0 and bluez_support.is_in_progress_error(exc):
                await asyncio.sleep(0.15)
                continue
            raise


async def write_chunked_value(
    client: BleakClient,
    char_uuid: str,
    endpoint_type: int,
    handle: int,
    data: bytes,
    mtu: Optional[int] = None,
    lock: Optional[asyncio.Lock] = None,
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

        if lock is not None:
            async with lock:
                await _write_with_in_progress_retry(client, char_uuid, bytes(chunk), response=False)
        else:
            await _write_with_in_progress_retry(client, char_uuid, bytes(chunk), response=False)
        offset += copy_bytes
        count += 1
        first = False


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------
@dataclass
class StreamCallbacks:
    """Optional consumers for the telemetry and band-history data streams."""

    heart_rate: Optional[Callable[[int], None]] = None
    steps: Optional[Callable[[dict], None]] = None
    imu: Optional[Callable[[dict], None]] = None
    history_spo2: Optional[Callable[[dict], None]] = None
    history_activity: Optional[Callable[[dict], None]] = None
    history_spo2_sleep: Optional[Callable[[dict], None]] = None


class MiBand6Client:
    """Handles BLE connection, chunked ECDH auth, and real-time data streaming."""

    def __init__(
        self,
        device_id: str,
        auth_key_hex: str,
        *,
        device_name: Optional[str] = None,
        mtu: Optional[int] = None,
        client_factory: Optional[Callable] = None,
        scanner_factory: Optional[Callable] = None,
        scan_timeout: float = 90.0,
        connect_timeout: float = 25.0,
        auth_timeout: float = 30.0,
        direct_connect: bool = True,
    ) -> None:
        self.device_id = (device_id or "").strip()
        # Optional case-insensitive advertised-name match; works on platforms
        # (macOS) where the Bluetooth address is not exposed to applications.
        self.device_name = (device_name or "").strip() or None
        # Optional manual ATT MTU override; None means "ask the backend".
        self.mtu = mtu
        self.auth_key = normalize_auth_key(auth_key_hex)
        self._client_factory = client_factory or BleakClient
        self._scanner_factory = scanner_factory or BleakScanner
        self.scan_timeout = scan_timeout
        self.connect_timeout = connect_timeout
        self.auth_timeout = auth_timeout
        # Linux only: try the band's already-bonded BlueZ object before scanning.
        self._direct_connect = bool(direct_connect)
        self._session_mtu: Optional[int] = None

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
        #: Frame types seen on 0x0002 that are not accelerometer data (logged once).
        self._ignored_sensor_types: set[int] = set()

        # Chunked auth reassembly state (reset per connection).
        self._prv_key = b""
        self._pub_key = b""
        self._handle = 0
        self._reassemble_buf = bytearray(MAX_REASSEMBLY_BYTES)
        self._reassemble_ptr = 0
        self._expected_bytes = 0
        self._next_seq = 0
        self._step2_sent = False

        self._gatt_lock = asyncio.Lock()
        self._pairing_agent: Optional[bluez_support.BlueZPairingAgent] = None

    async def _write_gatt(self, char_uuid: str, data: bytes, response: bool = False) -> None:
        """Write to a GATT characteristic serialized by _gatt_lock with InProgress retry."""
        if self.client is None:
            raise RuntimeError("Client is not connected")
        async with self._gatt_lock:
            await _write_with_in_progress_retry(self.client, char_uuid, data, response=response)

    # ------------------------------------------------------------- properties
    @property
    def mac_address(self) -> str:
        """Backwards-compatible alias for :attr:`device_id`."""
        return self.device_id

    @property
    def disconnected_event(self) -> asyncio.Event:
        """Event set when the BLE link drops unexpectedly (watchdog signal)."""
        return self._disconnected

    async def wait_until_disconnected(self) -> None:
        await self._disconnected.wait()

    def _describe_target(self) -> str:
        """Human-readable discovery target for logs and timeout messages."""
        if self.device_name:
            return f"{self.device_id} (or a name containing {self.device_name!r})"
        return self.device_id

    # ---------------------------------------------------------------- discovery
    async def _direct_connect_target(self):
        """Return a directly connectable ``BLEDevice`` for a bonded band.

        On Linux a paired device keeps a BlueZ object path even while it is not
        advertising, so ``BleakClient`` can connect without a discovery pass.
        Injected factories (tests) and non-BlueZ platforms always scan, which
        keeps this fast path out of the simulated and Windows/macOS code paths.
        """
        if not self._direct_connect:
            return None
        if self._client_factory is not BleakClient:
            return None
        if not bluez_support.is_bluez_platform():
            return None
        if not bluez_support.looks_like_mac(self.device_id):
            return None

        # 1. If already connected in BlueZ, reuse connection directly without scanning.
        connected_dev = await bluez_support.connected_device(self.device_id)
        if connected_dev is not None:
            logger.info(
                "Mi Band %s is already connected in BlueZ; reusing connection.",
                self.device_id,
            )
            return connected_dev

        # 2. If properly paired/bonded in BlueZ, attempt fast direct connection.
        device = await bluez_support.paired_device(self.device_id)
        if device is not None:
            logger.info(
                "Bonded band %s found in the BlueZ object tree; skipping discovery.",
                self.device_id,
            )
        return device

    async def _discover_device(self, scan_timeout: float):
        found_device = None
        stop_event = asyncio.Event()

        def _on_detection(device, advertisement) -> None:
            nonlocal found_device
            if device_matches(self.device_id, self.device_name, device, advertisement):
                found_device = device
                stop_event.set()

        scanner_kwargs = bluez_support.scanner_kwargs(self._scanner_factory)
        scanner = self._scanner_factory(detection_callback=_on_detection, **scanner_kwargs)
        await scanner.start()
        heartbeat = asyncio.create_task(self._scan_heartbeat(stop_event))
        try:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=scan_timeout)
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"Could not discover band {self._describe_target()} "
                    f"within {scan_timeout:.0f}s. "
                    "Please verify: 1) Phone Bluetooth is OFF / Airplane mode. "
                    "2) Band screen is touched and lit. "
                    "3) The device identifier matches this platform "
                    "(on macOS use the CoreBluetooth UUID shown by scan_ble.py)."
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
    async def _open_client(self, direct_device, scan_timeout: float, connect_timeout: float) -> None:
        """Connect to *direct_device*, or scan and connect when it is ``None``.

        A direct connection to a bonded BlueZ device skips discovery, but the
        cached object can still be stale (bond removed, adapter reset), so a
        failed direct attempt always falls back to the scanning path.
        """

        def _new_client(device, timeout: float):
            return self._client_factory(
                device,
                timeout=timeout,
                disconnected_callback=self._on_disconnected,
            )

        async def _connect_with_transient_retry(client_obj: Any, max_retries: int = 2) -> None:
            for attempt in range(max_retries + 1):
                try:
                    await client_obj.connect()
                    return
                except Exception as exc:
                    if attempt < max_retries and bluez_support.is_transient_connect_error(exc):
                        logger.debug(
                            "Transient connection error (%s); fast-retrying in 0.4s (attempt %d/%d)...",
                            bluez_support.describe_ble_error(exc),
                            attempt + 1,
                            max_retries,
                        )
                        await asyncio.sleep(0.4)
                        continue
                    raise

        found_device = direct_device
        if found_device is None:
            logger.info(
                "Waiting for Mi Band broadcast at %s (touch/light up band screen)...",
                self._describe_target(),
            )
            found_device = await self._discover_device(scan_timeout)
            logger.info("Band detected! Establishing BLE link (keep screen awake)...")

        # A fresh client object per session avoids stale D-Bus device handles.
        # The direct attempt gets a shorter budget so a sleeping band cannot
        # delay the fallback scan by the full connect timeout.
        direct_timeout = min(connect_timeout, DIRECT_CONNECT_TIMEOUT)
        self.client = _new_client(
            found_device, connect_timeout if direct_device is None else direct_timeout
        )
        try:
            if direct_device is not None:
                await self.client.connect()
            else:
                await _connect_with_transient_retry(self.client)
        except Exception as exc:
            if direct_device is None:
                raise
            logger.warning(
                "Direct connection to the bonded device failed (%s); "
                "falling back to a scan...",
                bluez_support.describe_ble_error(exc),
            )
            with contextlib.suppress(Exception):
                await self.client.disconnect()
            self.client = None
            found_device = await self._discover_device(scan_timeout)
            logger.info("Band detected! Establishing BLE link (keep screen awake)...")
            self.client = _new_client(found_device, connect_timeout)
            await _connect_with_transient_retry(self.client)

    async def _apply_mtu(self) -> None:
        """Resolve the ATT MTU once, asking BlueZ for it explicitly first."""
        # BlueZ reports 23 bytes (and warns) until the backend is asked for the
        # real ATT MTU; WinRT and CoreBluetooth fill it in during connect.  Do
        # this before the first chunked write so the framing is sized once.
        acquired = False
        if self.mtu is None:
            acquired = await bluez_support.try_acquire_mtu(self.client)
        self._session_mtu = _resolve_mtu(self.client, self.mtu)
        logger.info(
            "BLE link established. Initializing chunked transfer listener (MTU=%d)...",
            self._session_mtu,
        )
        if acquired:
            logger.info("Negotiated ATT MTU via BlueZ: %d bytes.", self._session_mtu)

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

        if bluez_support.is_bluez_platform() and self._client_factory is BleakClient:
            if self._pairing_agent is None:
                agent = bluez_support.BlueZPairingAgent(self.device_id)
                if await agent.start():
                    self._pairing_agent = agent
                else:
                    logger.debug("BlueZ pairing agent failed to start; continuing without it.")

        direct_device = await self._direct_connect_target()
        await self._open_client(direct_device, scan_timeout, connect_timeout)
        if self._pairing_agent is not None:
            target_mac = getattr(self.client, "address", None) or self.device_id
            if bluez_support.looks_like_mac(target_mac):
                self._pairing_agent.target_address = bluez_support.normalize_address(target_mac)
            await self._pairing_agent.ensure_ready_for_device(target_mac)
        await self._apply_mtu()

        await self.client.start_notify(UUID_CHAR_CHUNKED_READ, self._on_chunked_notification)

        initial_auth = b"\x04\x02\x00\x02" + self._pub_key
        logger.info("Initiating chunked ECDH handshake (sending client public key)...")
        await write_chunked_value(
            self.client,
            UUID_CHAR_CHUNKED_WRITE,
            CHUNKED_ENDPOINT_AUTH,
            0,
            initial_auth,
            mtu=self._session_mtu,
            lock=self._gatt_lock,
        )

        # A link drop during the handshake must not leave the session waiting
        # for the full auth timeout: watch the disconnect signal alongside the
        # auth result so the supervisor can reconnect promptly.  This matters
        # most with the slower pure-Python ECDH backend.
        auth_wait = asyncio.create_task(self._auth_event.wait())
        link_wait = asyncio.create_task(self._disconnected.wait())
        try:
            await asyncio.wait(
                {auth_wait, link_wait},
                timeout=auth_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (auth_wait, link_wait):
                if not task.done():
                    task.cancel()
            await asyncio.gather(auth_wait, link_wait, return_exceptions=True)

        if self._auth_error:
            raise RuntimeError(f"Authentication failed: {self._auth_error}")
        if not self._auth_event.is_set():
            if self._disconnected.is_set():
                raise ConnectionError(
                    "BLE link lost during chunked ECDH authentication"
                )
            raise TimeoutError(
                f"Chunked ECDH authentication timed out after {auth_timeout:.0f}s"
            )

        self.authenticated = True
        logger.info("Chunked ECDH authentication SUCCEEDED. Session is ready.")

    def _on_disconnected(self, _client: object) -> None:
        if self._closing:
            return
        logger.warning("BLE link to %s lost (signal watchdog).", self.device_id)
        if self._keepalive_task is not None and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        self._disconnected.set()

    # --------------------------------------------------- chunked auth hardening
    def _reset_auth_state(self) -> None:
        self.authenticated = False
        self._auth_error = None
        self._auth_event.clear()
        self._session_mtu = None
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
                mtu=self._session_mtu,
                lock=self._gatt_lock,
            )
        except Exception as exc:
            logger.error("Failed to complete auth step 2: %s", exc)
            self._fail_auth(f"could not complete auth step 2: {exc}")

    # ---------------------------------------------------------- data streams
    def _on_heart_rate_notification(self, _sender, data: bytearray) -> None:
        try:
            bpm = parse_heart_rate(data)
            logger.info("[HR] Current Heart Rate: %d BPM", bpm)
            if self._hr_callback:
                self._hr_callback(bpm)
        except Exception as exc:
            logger.error("Failed to parse heart rate measurement: %s", exc)

    def _on_steps_notification(self, _sender, data: bytearray) -> None:
        try:
            steps_info = parse_realtime_steps(data)
            logger.info(
                "[STEPS] Steps: %d | Distance: %d m | Calories: %d kcal",
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
            if not sensor_info["samples"]:
                # Non-accelerometer frame sharing characteristic 0x0002
                # (optical/status); ignore quietly instead of logging an error.
                if sensor_info["type"] not in self._ignored_sensor_types:
                    self._ignored_sensor_types.add(sensor_info["type"])
                    logger.debug(
                        "Ignoring non-accelerometer raw-sensor frames of type 0x%02x "
                        "(length %d)",
                        sensor_info["type"],
                        len(data),
                    )
                return
            first = sensor_info["samples"][0]
            logger.debug(
                "[IMU] Seq %d, %d samples, accel: (%.2fg, %.2fg, %.2fg)",
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
        await self._write_gatt(UUID_CHAR_HR_CONTROL, b"\x15\x02\x00", response=False)
        await self._write_gatt(UUID_CHAR_HR_CONTROL, b"\x15\x01\x00", response=False)
        await self.client.start_notify(UUID_CHAR_HR_MEASUREMENT, self._on_heart_rate_notification)
        await self._write_gatt(UUID_CHAR_HR_CONTROL, b"\x15\x01\x01", response=False)

        self._hr_active = True
        self._keepalive_task = asyncio.create_task(self._hr_keepalive_loop())
        logger.info("Continuous heart rate monitoring active!")

    async def _hr_keepalive_loop(self) -> None:
        consecutive_failures = 0
        try:
            while True:
                await asyncio.sleep(12)
                if self.client is None:
                    return
                logger.debug("Pinging heart rate monitor keep-alive [0x16]...")
                try:
                    await self._write_gatt(UUID_CHAR_HR_CONTROL, b"\x16", response=False)
                    consecutive_failures = 0
                except Exception as exc:
                    consecutive_failures += 1
                    is_fatal = (
                        self.client is None
                        or not getattr(self.client, "is_connected", False)
                        or "UnknownObject" in str(exc)
                        or getattr(exc, "dbus_error_name", "") == "org.freedesktop.DBus.Error.UnknownObject"
                    )
                    if is_fatal or consecutive_failures >= 2:
                        logger.error(
                            "Heart-rate keep-alive failed (%d consecutive failures); treating link as lost: %s",
                            consecutive_failures,
                            bluez_support.describe_ble_error(exc),
                        )
                        self._disconnected.set()
                        return
                    logger.warning(
                        "Heart-rate keep-alive warning (attempt %d/2, retrying next cycle): %s",
                        consecutive_failures,
                        bluez_support.describe_ble_error(exc),
                    )
        except asyncio.CancelledError:
            raise

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
        await self._write_gatt(
            UUID_CHAR_RAW_SENSOR_CONTROL, CMD_RAW_SENSOR_START_1, response=False
        )
        await self._write_gatt(
            UUID_CHAR_RAW_SENSOR_CONTROL, CMD_RAW_SENSOR_START_2, response=False
        )
        await self._write_gatt(
            UUID_CHAR_RAW_SENSOR_CONTROL, CMD_RAW_SENSOR_START_3, response=False
        )
        await self.client.start_notify(UUID_CHAR_RAW_SENSOR_DATA, self._on_sensor_notification)
        self._sensor_active = True
        logger.info("Raw IMU sensor streaming active!")

    async def stop_raw_sensor_stream(self) -> None:
        if self._sensor_active and self.client is not None:
            try:
                await self._write_gatt(
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
            if self._pairing_agent is not None:
                with contextlib.suppress(Exception):
                    await self._pairing_agent.stop()
                self._pairing_agent = None
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
async def _sync_band_history(
    client: MiBand6Client,
    days: int,
    callbacks: StreamCallbacks,
    *,
    include_spo2_sleep: bool = False,
) -> None:
    """Fetch band-side history once; failures never abort live streaming."""
    logger.info("Fetching up to %d day(s) of band-side history (experimental)...", days)
    if include_spo2_sleep:
        logger.warning(
            "Sleep SpO2 (0x26) capture enabled: 30B layout verified on Mi Band 6, "
            "but trailing field semantics are unknown; samples go to the "
            "dedicated spo2_sleep table + JSONL, never the point spo2 table."
        )
    try:
        result = await sync_history(
            client,
            days=days,
            on_spo2=callbacks.history_spo2,
            on_activity=callbacks.history_activity,
            include_spo2_sleep=include_spo2_sleep,
            on_spo2_sleep=callbacks.history_spo2_sleep,
        )
    except Exception as exc:
        logger.error("Band history sync failed (continuing with live streams): %s", exc)
        return
    logger.info("Band history sync finished: %s", summarize_history_result(result))
    for error in result.errors:
        logger.warning("History sync issue: %s", error)


async def run_monitor(
    device_id: str,
    auth_key_hex: str,
    *,
    duration: Optional[int] = None,
    imu: bool = False,
    callbacks: Optional[StreamCallbacks] = None,
    scan_timeout: float = 90.0,
    reconnect: bool = True,
    reconnect_initial_delay: float = 1.0,
    reconnect_max_delay: float = 30.0,
    sync_history_days: Optional[int] = None,
    sync_sleep_spo2: bool = False,
    device_name: Optional[str] = None,
    mtu: Optional[int] = None,
    direct_connect: bool = True,
    client_factory: Optional[Callable[[str, str], MiBand6Client]] = None,
) -> None:
    """Connect + stream until *duration* elapses, reconnecting with backoff."""
    callbacks = callbacks or StreamCallbacks()
    make_client = client_factory or (
        lambda device, key: MiBand6Client(
            device,
            key,
            device_name=device_name,
            mtu=mtu,
            scan_timeout=scan_timeout,
            direct_connect=direct_connect,
        )
    )
    deadline = None if duration is None else time.monotonic() + duration
    attempt = 0
    consecutive_failures = 0
    history_synced = False

    while True:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            logger.info("Collection window (%ss) elapsed.", duration)
            return

        attempt += 1
        client = make_client(device_id, auth_key_hex)
        failure: Optional[BaseException] = None
        was_authenticated = False
        try:
            await client.connect_and_authenticate(scan_timeout=scan_timeout)
            was_authenticated = True
            consecutive_failures = 0

            if sync_history_days is not None and not history_synced:
                await _sync_band_history(
                    client,
                    sync_history_days,
                    callbacks,
                    include_spo2_sleep=sync_sleep_spo2,
                )
                history_synced = True
            # Order matters on real hardware: the raw sensor hub stream must be
            # activated *before* the heart-rate stream.  Starting it afterwards
            # silently starves the standard BLE heart-rate characteristic
            # (measured on a Mi Band 6: 0 HR notifications in 20 s vs ~1/s).
            if imu:
                await client.start_raw_sensor_stream(callbacks.imu)
            await client.start_heart_rate_stream(callbacks.heart_rate)
            await client.start_steps_stream(callbacks.steps)
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
            logger.error("Session %d failed: %s", attempt, bluez_support.describe_ble_error(exc))
        finally:
            await client.disconnect()

        if not reconnect:
            if failure is not None and not was_authenticated:
                raise failure
            return

        if was_authenticated and failure is not None:
            # When an established session drops, the band advertises aggressively for a few seconds.
            # Fast-reconnect immediately to catch this reconnection window without waking the screen!
            delay = min(0.3, reconnect_initial_delay)
        else:
            consecutive_failures += 1
            delay = min(
                reconnect_max_delay,
                reconnect_initial_delay * (2 ** min(consecutive_failures - 1, 5)),
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
    device_id: str,
    auth_key_hex: str,
    *,
    duration: Optional[int] = None,
    imu: bool = False,
    callbacks: Optional[StreamCallbacks] = None,
    sync_history_days: Optional[int] = None,
    sync_sleep_spo2: bool = False,
    mtu: Optional[int] = None,
) -> None:
    """Run the full client stack against the in-memory simulator (no hardware)."""
    from sim_band import FakeBleakScanner, SimulatedBand, simulated_factories

    band = SimulatedBand(auth_key=normalize_auth_key(auth_key_hex))
    sim_client_factory, _scanner_factory = simulated_factories(band)

    class _DemoScanner(FakeBleakScanner):
        default_address = device_id

    def _factory(mac: str, key: str) -> MiBand6Client:
        return MiBand6Client(
            mac,
            key,
            mtu=mtu,
            client_factory=sim_client_factory,
            scanner_factory=_DemoScanner,
        )

    logger.info("DEMO MODE: using an in-memory simulated Mi Band 6 (no Bluetooth needed).")
    await run_monitor(
        device_id,
        auth_key_hex,
        duration=duration,
        imu=imu,
        callbacks=callbacks,
        scan_timeout=10.0,
        reconnect=True,
        reconnect_initial_delay=0.2,
        reconnect_max_delay=1.0,
        sync_history_days=sync_history_days,
        sync_sleep_spo2=sync_sleep_spo2,
        mtu=mtu,
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
    # Windows: when stdout/stderr are redirected to a file the console encoding
    # may be a legacy code page (cp1252/cp936) that cannot encode every log
    # message.  Replace instead of raising so a redirected run never aborts
    # just because of decoration characters.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):  # pragma: no cover - defensive
                pass


def _env_float(name: str, default: float) -> float:
    """Read a float from the environment, falling back safely on bad values."""
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _env_optional_int(name: str) -> Optional[int]:
    """Read an optional integer from the environment (``None`` when unset)."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        logger.warning("Ignoring invalid %s=%r from environment", name, raw)
        return None


def _secure_env_file() -> None:
    if os.name == "nt":
        return
    env_file = Path(".env")
    if env_file.is_file():
        with contextlib.suppress(OSError):
            mode = env_file.stat().st_mode & 0o777
            if mode & 0o077:
                env_file.chmod(0o600)
                logger.debug("Tightened .env file permissions to 0600")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    load_dotenv()
    _secure_env_file()
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
        "--device-id",
        default=_env_device_id(),
        metavar="ID",
        help=(
            "Bluetooth MAC address (Linux/Windows) or CoreBluetooth UUID (macOS) "
            "of the band; defaults to DEVICE_ID / legacy DEVICE_MAC from .env"
        ),
    )
    parser.add_argument(
        "--device-name",
        default=os.getenv("DEVICE_NAME", ""),
        metavar="NAME",
        help=(
            "optional advertised-name substring used as an extra discovery match "
            "(useful on macOS where no MAC address is exposed; default: DEVICE_NAME env)"
        ),
    )
    parser.add_argument(
        "--mtu",
        type=int,
        default=_env_optional_int("BLE_MTU"),
        metavar="BYTES",
        help=(
            "override the ATT MTU used to size chunked auth writes "
            "(default: auto-detect, floor 23; env: BLE_MTU)"
        ),
    )
    parser.add_argument(
        "--sync-history",
        nargs="?",
        const=7,
        type=int,
        default=_env_optional_int("HISTORY_DAYS"),
        metavar="DAYS",
        help=(
            "fetch up to DAYS days of band-side history (activity + SpO2) after "
            "connecting, experimental (default: off, or HISTORY_DAYS env)"
        ),
    )
    parser.add_argument(
        "--sync-sleep-spo2",
        action="store_true",
        help=(
            "also fetch experimental sleep SpO2 (type 0x26) for payload capture; "
            "requires --sync-history; 30B layout verified, trailing fields unknown, "
            "samples are written to spo2_sleep.jsonl only"
        ),
    )
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
        "--web-port",
        type=int,
        default=_env_optional_int("WEB_PORT"),
        help="serve the live web dashboard on this port (0 = random free port; default: off)",
    )
    parser.add_argument(
        "--web-host",
        default=os.getenv("WEB_HOST", "127.0.0.1"),
        help="dashboard bind address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--no-reconnect",
        action="store_true",
        help="do not automatically reconnect after errors or link loss",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="run an environment self-check and exit (never connects to the band)",
    )
    parser.add_argument(
        "--no-direct-connect",
        action="store_true",
        help=(
            "always scan for the band instead of using the bonded BlueZ device "
            "(Linux only; direct connect is the default when the band is paired)"
        ),
    )
    args = parser.parse_args(argv)
    if args.sync_sleep_spo2 and args.sync_history is None:
        parser.error("--sync-sleep-spo2 requires --sync-history [DAYS]")
    if args.storage not in STORAGE_BACKENDS:
        args.storage = "both"
    return args


async def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    _configure_logging(args.log_level)

    if args.doctor:
        # Imported lazily: doctor imports this module for config validation.
        from doctor import run_doctor

        return await run_doctor(
            (args.device_id or "").strip(),
            (os.getenv("AUTH_KEY") or "").strip(),
            args.output,
        )

    logger.info("ECDH backend: %s", ecdh_backend_name())

    device_id = (args.device_id or "").strip()
    device_name = (args.device_name or "").strip() or None
    key = (os.getenv("AUTH_KEY") or "").strip()
    demo = args.demo or os.getenv("DEMO_MODE", "0").lower() in ("1", "true", "yes", "on")

    if args.mtu is not None and not (23 <= args.mtu <= 517):
        logger.error(
            "Invalid --mtu %s: ATT MTU must be between 23 and 517 bytes. "
            "Leave it unset to auto-detect.",
            args.mtu,
        )
        return 1

    if demo:
        if is_placeholder_device_id(device_id):
            device_id = DEMO_DEFAULT_MAC
        try:
            normalize_auth_key(key)
        except ValueError:
            logger.warning(
                "DEMO_MODE: no valid AUTH_KEY configured; using throwaway demo key."
            )
            key = DEMO_DEFAULT_AUTH_KEY
    else:
        if is_placeholder_device_id(device_id) or not key or key.startswith("012345"):
            logger.error(
                "Please configure DEVICE_ID (Bluetooth address, or macOS UUID) "
                "and AUTH_KEY in .env before running (or run with --demo). "
                "Legacy DEVICE_MAC is still accepted."
            )
            return 1
        try:
            normalize_auth_key(key)
        except ValueError as exc:
            logger.error("Invalid AUTH_KEY: %s", exc)
            return 1

    logger.info(
        "Discovery target: %s%s",
        device_id,
        f" (name filter: {device_name!r})" if device_name else "",
    )

    store = TelemetryStore(args.output, backend=args.storage)

    hub: Optional[TelemetryHub] = None
    web_server: Optional[DashboardServer] = None
    if args.web_port is not None:
        hub = TelemetryHub()
        hub.attach_loop(asyncio.get_running_loop())
        web_server = DashboardServer(
            hub,
            host=args.web_host,
            port=args.web_port,
            history_provider=make_db_history_provider(store.db_path),
        )
        try:
            await web_server.start()
            logger.info("Live dashboard: %s", web_server.url)
        except OSError as exc:
            logger.error(
                "Could not start the dashboard on %s:%s: %s",
                args.web_host,
                args.web_port,
                exc,
            )
            web_server = None
            hub = None

    def _on_heart_rate(bpm: int) -> None:
        store.record_heart_rate(bpm)
        if hub is not None:
            hub.publish_heart_rate(bpm)

    def _on_steps(info: dict) -> None:
        store.record_steps(info)
        if hub is not None:
            hub.publish_steps(info)

    def _on_imu(info: dict) -> None:
        store.record_imu(info)
        if hub is not None:
            hub.publish_imu(info)

    def _on_history_spo2(sample: dict) -> None:
        store.record_spo2(
            sample["spo2"],
            auto_measure=bool(sample.get("auto_measure")),
            timestamp=sample.get("ts"),
        )
        if hub is not None:
            hub.publish_spo2(
                sample["spo2"],
                status=1 if sample.get("auto_measure") else 0,
                timestamp=sample.get("ts"),
            )

    def _on_history_activity(sample: dict) -> None:
        store.record_activity_history(sample, timestamp=sample.get("ts"))

    def _on_history_spo2_sleep(sample: dict) -> None:
        store.record_spo2_sleep(sample)

    callbacks = StreamCallbacks(
        heart_rate=_on_heart_rate,
        steps=_on_steps,
        imu=_on_imu,
        history_spo2=_on_history_spo2,
        history_activity=_on_history_activity,
        history_spo2_sleep=_on_history_spo2_sleep,
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
                device_id,
                key,
                duration=args.duration,
                imu=args.imu,
                callbacks=callbacks,
                sync_history_days=args.sync_history,
                sync_sleep_spo2=args.sync_sleep_spo2,
                mtu=args.mtu,
            )
        else:
            await run_monitor(
                device_id,
                key,
                duration=args.duration,
                imu=args.imu,
                callbacks=callbacks,
                scan_timeout=args.scan_timeout,
                reconnect=not args.no_reconnect,
                sync_history_days=args.sync_history,
                sync_sleep_spo2=args.sync_sleep_spo2,
                device_name=device_name,
                mtu=args.mtu,
                direct_connect=not args.no_direct_connect,
            )
    except KeyboardInterrupt:
        logger.info("Stopping on user request...")
    except Exception as exc:
        logger.error("Monitoring stopped after error: %s", exc)
        exit_code = 1
    finally:
        if web_server is not None:
            try:
                await web_server.stop()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Error while stopping the dashboard: %s", exc)
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
