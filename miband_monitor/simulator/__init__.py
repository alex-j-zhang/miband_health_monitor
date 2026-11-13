"""In-memory Mi Band 6 simulator plus fake bleak backend for demo/tests.

The package is split so the wire-format fixtures, the fake bleak backend and the
band's state machine can be read independently:

* :mod:`.constants` / :mod:`.payloads` -- protocol constants and packet builders;
* :mod:`.devices` / :mod:`.fake_bleak` -- bleak stand-ins used by the client;
* :mod:`.band` -- connection lifecycle and GATT dispatch, composed from the
  :mod:`.auth`, :mod:`.history` and :mod:`.streams` mixins;
* :mod:`.factory` -- ``simulated_factories`` / ``make_simulated_client``.

Every public (and test-used private) name of the historical ``sim_band`` module
is re-exported here, so existing import sites keep working unchanged.
"""

from .constants import (
    DEFAULT_ADVERTISE_NAME,
    DEFAULT_DEMO_MAC,
    DEFAULT_RSSI,
    SIMULATED_BAND_NAME,
    _AUTH_STEP1_PREFIX,
    _AUTH_STEP2_OPCODE,
    _AUTH_TOTAL_LENGTH_FIELD,
    _FIRST_CHUNK_HEADER,
    _NEXT_CHUNK_HEADER,
    logger,
)
from .payloads import (
    _activity_payload,
    _activity_sample,
    _imu_packet,
    _optical_packet,
    _spo2_payload,
    _spo2_sleep_payload,
    _spo2_sleep_record,
    _status_packet,
    _steps_packet,
)
from .devices import FakeAdvertisement, FakeDevice
from .auth import AuthMixin, _aes_cbc_encrypt
from .history import HistoryMixin
from .streams import StreamMixin
from .band import SimulatedBand
from .fake_bleak import FakeBleakClient, FakeBleakScanner
from .factory import make_simulated_client, simulated_factories

__all__ = [
    # constants
    "DEFAULT_ADVERTISE_NAME",
    "DEFAULT_DEMO_MAC",
    "DEFAULT_RSSI",
    "SIMULATED_BAND_NAME",
    "_AUTH_STEP1_PREFIX",
    "_AUTH_STEP2_OPCODE",
    "_AUTH_TOTAL_LENGTH_FIELD",
    "_FIRST_CHUNK_HEADER",
    "_NEXT_CHUNK_HEADER",
    "logger",
    # packet builders
    "_activity_payload",
    "_activity_sample",
    "_imu_packet",
    "_optical_packet",
    "_spo2_payload",
    "_spo2_sleep_payload",
    "_spo2_sleep_record",
    "_status_packet",
    "_steps_packet",
    # devices / backend
    "FakeAdvertisement",
    "FakeDevice",
    "AuthMixin",
    "HistoryMixin",
    "StreamMixin",
    "_aes_cbc_encrypt",
    "SimulatedBand",
    "FakeBleakClient",
    "FakeBleakScanner",
    # factories
    "make_simulated_client",
    "simulated_factories",
]
