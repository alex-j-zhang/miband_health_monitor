"""BlueZ MTU negotiation and transient connection-error classifiers."""

from __future__ import annotations

from miband_monitor.bluez import errors, mtu, platform


async def test_try_acquire_mtu_uses_the_backend(monkeypatch):
    monkeypatch.setattr(platform, "is_bluez_platform", lambda: True)

    class Backend:
        mtu_size = 23
        called = False

        def _acquire_mtu(self):
            self.called = True
            self.mtu_size = 247

    class Client:
        def __init__(self):
            self._backend = Backend()

        @property
        def mtu_size(self):
            return self._backend.mtu_size

        async def get_services(self):
            return None

    client = Client()
    assert await mtu.try_acquire_mtu(client) is True
    assert client._backend.called is True
    assert client.mtu_size == 247


async def test_try_acquire_mtu_supports_a_coroutine_backend(monkeypatch):
    """bleak 3.x implements ``_acquire_mtu`` as a coroutine."""
    monkeypatch.setattr(platform, "is_bluez_platform", lambda: True)

    class Backend:
        mtu_size = 23

        async def _acquire_mtu(self):
            self.mtu_size = 185

    class Client:
        def __init__(self):
            self._backend = Backend()

        @property
        def mtu_size(self):
            return self._backend.mtu_size

    client = Client()
    assert await mtu.try_acquire_mtu(client) is True
    assert client.mtu_size == 185


async def test_try_acquire_mtu_is_a_noop_without_a_bluez_backend(monkeypatch):
    monkeypatch.setattr(platform, "is_bluez_platform", lambda: True)
    assert await mtu.try_acquire_mtu(object()) is False


def test_is_transient_connect_error():
    assert errors.is_transient_connect_error(Exception("le-connection-abort-by-local")) is True
    assert errors.is_transient_connect_error(Exception("org.bluez.Error.InProgress")) is True
    assert errors.is_transient_connect_error(Exception("Software caused connection abort")) is True
    assert errors.is_transient_connect_error(Exception("Permanent authentication failure")) is False


def test_is_in_progress_error():
    assert errors.is_in_progress_error(Exception("org.bluez.Error.InProgress")) is True
    assert errors.is_in_progress_error(Exception("GATT InProgress")) is True
    assert errors.is_in_progress_error(Exception("Other error")) is False
