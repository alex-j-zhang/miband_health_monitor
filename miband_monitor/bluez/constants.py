"""Constants for the Linux/BlueZ compatibility layer.

The BlueZ backend behaves differently from WinRT and CoreBluetooth in three
places that matter for connection robustness:

1. Device objects are transient.  A device that is not paired/trusted is
   removed from the BlueZ object tree as soon as it disconnects, so later GATT
   writes fail with org.freedesktop.DBus.Error.UnknownObject.  A band that is
   not bonded also drops the link after roughly 40 s.
2. bleak must scan before it can connect.  Passing a MAC address to
   BleakClient triggers a fresh discovery, even when BlueZ already knows the
   device.  A paired band is reachable through its cached D-Bus object, which
   lets us connect without waiting for an advertisement.
3. The ATT MTU is never acquired implicitly.  mtu_size stays at 23 (and emits
   a UserWarning) until the backend's _acquire_mtu() is called.

Every helper in this package degrades to a no-op on non-BlueZ platforms, so
importing it is always safe on Windows and macOS.
"""

from __future__ import annotations

import logging

from miband_monitor.config.defaults import ENV_BLUEZ_DUPLICATE_DATA

#: Shared logger name: operational logs from this package look exactly like the
#: historical bluez_support logs (channel "MiBand6Client.bluez").
logger = logging.getLogger("MiBand6Client.bluez")

BLUEZ_SERVICE = "org.bluez"
OBJECT_MANAGER_PATH = "/"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"

AGENT_MANAGER_PATH = "/org/bluez"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"

#: Default timeout for one short D-Bus introspection round trip.
DBUS_TIMEOUT = 2.0

#: The Linux kernel abandons one LE Create Connection after 40 s
#: (HCI_LE_CONN_TIMEOUT) and BlueZ reports
#: org.bluez.Error.Failed: le-connection-abort-by-local.  bleak 3.0.2 treats
#: that error as "the link was up once" and waits for a Connected: false
#: property change that BlueZ never emits for a link that was never
#: established, so that connect() call can stall for the rest of its timeout
#: without ever re-arming the controller.  Every connect slice we hand to bleak
#: must therefore stay below this window; the long standby loop is implemented
#: here instead (see wait_for_bluez_link).
LE_CONN_TIMEOUT = 40.0
CONNECT_SLICE_TIMEOUT = 25.0

#: BlueZ garbage-collects a temporary (unpaired/untrusted) device object after
#: TemporaryTimeout seconds without a discovery or connect request in flight
#: (default 30 s).  Standby cycles must stay inside this window.
BLUEZ_TEMP_OBJECT_TIMEOUT = 30.0

#: A Mi Band 6 that lost its phone link emits ONE isolated advertising pulse
#: every 40-60 s and stops advertising entirely while its screen is off;
#: lighting or sliding the screen does not change that (measured 2026-11-12,
#: see docs/validation/linux-ble-connect-findings.md).  A link attempt therefore has to
#: stay armed for longer than one full pulse period, otherwise the "detect
#: pulse -> connect for 15 s -> re-scan" pattern misses every other pulse.
BAND_PULSE_PERIOD = 70.0

#: Time BlueZ needs after ServicesResolved to settle the SMP new_auth() round
#: and the built-in profile probes.  A GATT write issued inside that window
#: blocks behind the single ATT queue for tens of seconds (measured: 56.8 s).
SMP_SETTLE_DELAY = 1.5

#: DuplicateData=true makes BlueZ re-arm the LE scan every ~5 s (31 HCI
#: commands in 75 s on extend-advertising controllers), which wedged the
#: BARROT BT 6.0 adapter into a state where it reports HCI command completions
#: but no advertising reports at all.  Default to plain duplicate filtering;
#: set MIBAND_BLUEZ_DUPLICATE_DATA=1 to restore the old behaviour.
DUPLICATE_DATA_ENV = ENV_BLUEZ_DUPLICATE_DATA
