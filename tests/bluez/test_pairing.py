"""BlueZ pairing agent: registration lifecycle and target matching."""

from __future__ import annotations

from miband_monitor.bluez import admin, devices, pairing, platform
from tests.support import MAC


async def test_pairing_agent_lifecycle(monkeypatch):
    agent = pairing.BlueZPairingAgent(MAC)
    assert agent.target_address == MAC
    monkeypatch.setattr(platform, "is_bluez_platform", lambda: False)
    assert await agent.start() is False
    await agent.stop()


async def test_pairing_agent_start_validates_message_type(monkeypatch):
    monkeypatch.setattr(platform, "is_bluez_platform", lambda: True)

    class FakeReply:
        def __init__(self, message_type, body=None):
            self.message_type = message_type
            self.body = body or []

    class FakeBus:
        def __init__(self, reply):
            self._reply = reply
            self.exported = []

        def export(self, path, interface):
            self.exported.append((path, interface))

        async def call(self, msg):
            return self._reply

        def unexport(self, path, interface):
            pass

        def disconnect(self):
            pass

    from dbus_fast import MessageType

    fake_bus_ok = FakeBus(FakeReply(MessageType.METHOD_RETURN))

    class FakeMessageBusOk:
        def __init__(self, bus_type):
            pass

        async def connect(self):
            return fake_bus_ok

    import dbus_fast.aio
    monkeypatch.setattr(dbus_fast.aio, "MessageBus", FakeMessageBusOk)

    agent = pairing.BlueZPairingAgent(MAC)
    assert await agent.start() is True
    assert agent._registered is True
    await agent.stop()

    fake_bus_err = FakeBus(FakeReply(MessageType.ERROR, ["Registration rejected"]))

    class FakeMessageBusErr:
        def __init__(self, bus_type):
            pass

        async def connect(self):
            return fake_bus_err

    monkeypatch.setattr(dbus_fast.aio, "MessageBus", FakeMessageBusErr)
    agent_err = pairing.BlueZPairingAgent(MAC)
    assert await agent_err.start() is False
    assert agent_err._registered is False


async def test_pairing_agent_target_matching_and_adapter_resolution(monkeypatch):
    # Non-MAC strings should not become bogus target filters
    agent = pairing.BlueZPairingAgent("Mi Smart Band 6")
    assert agent.target_address is None

    # Dynamically updating target MAC
    agent.target_address = MAC
    assert agent.target_address == MAC

    # ensure_ready_for_device dynamically resolves adapter
    called_adapter = None
    called_device = None

    async def mock_find_device(address, timeout=0):
        return {
            "adapter": "hci2",
            "path": f"/org/bluez/hci2/dev_{address.replace(':', '_')}",
        }

    async def mock_pairable(adapter, pairable):
        nonlocal called_adapter
        called_adapter = adapter
        return True

    async def mock_trusted(dev_path, trusted):
        nonlocal called_device
        called_device = dev_path
        return True

    monkeypatch.setattr(platform, "is_bluez_platform", lambda: True)
    monkeypatch.setattr(devices, "find_device", mock_find_device)
    monkeypatch.setattr(admin, "set_adapter_pairable", mock_pairable)
    monkeypatch.setattr(admin, "set_device_trusted", mock_trusted)

    await agent.ensure_ready_for_device(MAC)
    assert called_adapter == "hci2"
    assert called_device == f"/org/bluez/hci2/dev_{MAC.replace(':', '_')}"
