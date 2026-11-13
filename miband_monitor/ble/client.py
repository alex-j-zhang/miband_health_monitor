"""Public Mi Band 6 BLE client assembled from the domain mixins.

MiBand6Client is the same object the historical miband_client.MiBand6Client
was: same constructor signature, attribute names, defaults, exceptions and log
messages. Implementation lives in focused mixins so each file stays small:

- DiscoveryMixin / LinkMixin: find the band and open the BLE link
- MtuMixin / ListenerMixin: negotiate the ATT MTU, subscribe to 0x0017
- AuthStateMixin / AuthConnectMixin / AuthHandshakeMixin: chunked ECDH auth
- NotificationMixin / StreamMixin: live HR/steps/IMU streams and teardown

Test seams: _client_factory/_scanner_factory accept injected factories (the
simulator uses them) and _bleak_client_class records the real bleak class so
monkeypatching miband_monitor.ble.client.BleakClient is observable exactly like
the old miband_client.BleakClient monkeypatching. write_chunked_value is
imported into this module's namespace on purpose: the auth mixins resolve it
lazily through miband_monitor.ble.client so tests can patch it in one place.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, Optional

from .. import bluez
from ..config.defaults import (
    DEFAULT_AUTH_TIMEOUT,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_SCAN_TIMEOUT,
)
from .auth_connect import AuthConnectMixin
from .auth_handshake import AuthHandshakeMixin
from .auth_state import AuthStateMixin
from .backends import BleakClient, BleakScanner
from .chunked import write_chunked_value  # noqa: F401  (lazy test/patch seam)
from .discovery import DiscoveryMixin
from .identity import normalize_auth_key
from .link import LinkMixin
from .listener import ListenerMixin
from .mtu import MtuMixin
from .notifications import NotificationMixin
from .protocol import MAX_REASSEMBLY_BYTES
from .streams import StreamMixin


@dataclass
class StreamCallbacks:
    """Optional consumers for the telemetry and band-history data streams."""

    heart_rate: Optional[Callable[[int], None]] = None
    steps: Optional[Callable[[dict], None]] = None
    imu: Optional[Callable[[dict], None]] = None
    history_spo2: Optional[Callable[[dict], None]] = None
    history_activity: Optional[Callable[[dict], None]] = None
    history_spo2_sleep: Optional[Callable[[dict], None]] = None


class MiBand6Client(
    DiscoveryMixin,
    LinkMixin,
    MtuMixin,
    ListenerMixin,
    AuthStateMixin,
    AuthConnectMixin,
    AuthHandshakeMixin,
    NotificationMixin,
    StreamMixin,
):
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
        scan_timeout: float = DEFAULT_SCAN_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        auth_timeout: float = DEFAULT_AUTH_TIMEOUT,
        direct_connect: bool = True,
    ) -> None:
        self.device_id = (device_id or "").strip()
        # Optional case-insensitive advertised-name match; works on platforms
        # (macOS) where the Bluetooth address is not exposed to applications.
        self.device_name = (device_name or "").strip() or None
        # Optional manual ATT MTU override; None means "ask the backend".
        self.mtu = mtu
        self.auth_key = normalize_auth_key(auth_key_hex)
        # Concrete bleak classes, captured once for the identity checks in the
        # mixins (real backend vs injected test/simulator factory).
        self._bleak_client_class = BleakClient
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
        self._pairing_agent: Optional[bluez.BlueZPairingAgent] = None
        #: BlueZ device paths already marked Trusted in this session.
        self._trusted_paths: set[str] = set()

    # ------------------------------------------------------------- properties
    @property
    def mac_address(self) -> str:
        """Backwards-compatible alias for device_id."""
        return self.device_id

    @property
    def disconnected_event(self) -> asyncio.Event:
        """Event set when the BLE link drops unexpectedly (watchdog signal)."""
        return self._disconnected

    async def wait_until_disconnected(self) -> None:
        """Block until the link watchdog (or disconnect) fires."""
        await self._disconnected.wait()
