"""Linux/BlueZ compatibility layer: D-Bus discovery, link standby, pairing, MTU.

The historical bluez_support module is preserved behind this package: every
public name (and the private helpers used by tests/tools) is re-exported here,
so callers can keep using a single import for the whole layer.  Internals are
split by responsibility:

- constants: service names, D-Bus timeouts and hardware timing evidence;
- platform: platform gate plus MAC/object-path conventions;
- dbus: low-level ObjectManager/Property plumbing;
- devices: Device1 discovery and cached/paired/connected fast paths;
- admin: adapter/device property writes and ghost-link cleanup;
- link: the long-running Device1.Connect standby loop;
- pairing: the in-process org.bluez.Agent1 pairing agent;
- mtu: ATT MTU acquisition;
- scanner: BleakScanner keyword construction;
- errors: D-Bus/BLE error translation.

Every helper degrades to a no-op on non-BlueZ platforms, so importing this
package is always safe on Windows and macOS.
"""

from __future__ import annotations

from .admin import (
    disconnect_device,
    set_adapter_pairable,
    set_device_trusted,
)
from .constants import (
    ADAPTER_IFACE,
    AGENT_MANAGER_IFACE,
    AGENT_MANAGER_PATH,
    BAND_PULSE_PERIOD,
    BLUEZ_SERVICE,
    BLUEZ_TEMP_OBJECT_TIMEOUT,
    CONNECT_SLICE_TIMEOUT,
    DBUS_TIMEOUT,
    DEVICE_IFACE,
    DUPLICATE_DATA_ENV,
    LE_CONN_TIMEOUT,
    OBJECT_MANAGER_IFACE,
    OBJECT_MANAGER_PATH,
    SMP_SETTLE_DELAY,
    logger,
)
from .dbus import _managed_objects, _unwrap, list_adapters
from .devices import (
    _device_info_from_props,
    _info_for_address,
    active_cached_device,
    connected_device,
    find_device,
    list_known_devices,
    make_ble_device,
    paired_device,
)
from .errors import (
    _DBUS_HINTS,
    describe_ble_error,
    is_in_progress_error,
    is_transient_connect_error,
)
from .link import wait_for_bluez_link
from .mtu import try_acquire_mtu
from .pairing import BlueZPairingAgent
from .platform import (
    device_path_for_address,
    is_bluez_platform,
    link_standby_budget,
    looks_like_mac,
    normalize_address,
)
from .scanner import scanner_kwargs

__all__ = [
    # constants
    "BLUEZ_SERVICE",
    "OBJECT_MANAGER_PATH",
    "OBJECT_MANAGER_IFACE",
    "ADAPTER_IFACE",
    "DEVICE_IFACE",
    "AGENT_MANAGER_PATH",
    "AGENT_MANAGER_IFACE",
    "DBUS_TIMEOUT",
    "LE_CONN_TIMEOUT",
    "CONNECT_SLICE_TIMEOUT",
    "BLUEZ_TEMP_OBJECT_TIMEOUT",
    "BAND_PULSE_PERIOD",
    "SMP_SETTLE_DELAY",
    "DUPLICATE_DATA_ENV",
    "logger",
    # platform
    "is_bluez_platform",
    "normalize_address",
    "looks_like_mac",
    "device_path_for_address",
    "link_standby_budget",
    # dbus
    "list_adapters",
    "_unwrap",
    "_managed_objects",
    # devices
    "find_device",
    "list_known_devices",
    "make_ble_device",
    "connected_device",
    "paired_device",
    "active_cached_device",
    "_device_info_from_props",
    "_info_for_address",
    # admin
    "set_adapter_pairable",
    "set_device_trusted",
    "disconnect_device",
    # link
    "wait_for_bluez_link",
    # pairing
    "BlueZPairingAgent",
    # mtu
    "try_acquire_mtu",
    # scanner
    "scanner_kwargs",
    # errors
    "describe_ble_error",
    "is_transient_connect_error",
    "is_in_progress_error",
    "_DBUS_HINTS",
]
