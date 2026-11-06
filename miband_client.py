"""Mi Band 6 BLE client implementation for authentication and real-time health data streaming."""

import asyncio
import logging
import os
from typing import Callable, Optional
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from dotenv import load_dotenv
from bleak import BleakClient, BleakScanner

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("MiBand6Client")

# BLE GATT Service and Characteristic UUIDs
UUID_SVC_HUAMI_AUTH = "0000fee1-0000-1000-8000-00805f9b34fb"
UUID_CHAR_HUAMI_AUTH = "00000009-0000-3512-2118-0009af100700"
UUID_SVC_HEART_RATE = "0000180d-0000-1000-8000-00805f9b34fb"
UUID_CHAR_HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
UUID_CHAR_HR_CONTROL = "00002a39-0000-1000-8000-00805f9b34fb"


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


class MiBand6Client:
    """Handles BLE connection, Huami authentication handshake, and sensor streaming."""

    def __init__(self, mac_address: str, auth_key_hex: str):
        self.mac_address = mac_address
        cleaned_key = auth_key_hex.strip().lower()
        if cleaned_key.startswith("0x"):
            cleaned_key = cleaned_key[2:]
        if len(cleaned_key) != 32:
            raise ValueError(f"Auth key hex string must be 32 characters, got {len(cleaned_key)}")

        self.auth_key = bytes.fromhex(cleaned_key)
        self.client = BleakClient(mac_address)
        self._auth_event = asyncio.Event()
        self._auth_error: Optional[str] = None
        self._hr_callback: Optional[Callable[[int], None]] = None
        self._keepalive_task: Optional[asyncio.Task] = None

    async def _on_auth_notification(self, _sender, data: bytearray) -> None:
        """Handle auth characteristic notification packets."""
        logger.debug("Auth notification received: %s", data.hex())

        # Response to random challenge request (0x10, 0x02, 0x01)
        if len(data) >= 19 and data[:3] == b"\x10\x02\x01":
            random_challenge = bytes(data[3:19])
            logger.info("Received challenge salt from band: %s", random_challenge.hex())
            encrypted_salt = encrypt_challenge(self.auth_key, random_challenge)
            auth_b = getattr(self, "_auth_byte", b"\x08")
            response = b"\x03" + auth_b + encrypted_salt
            logger.info("Sending encrypted challenge response to band...")
            await self.client.write_gatt_char(UUID_CHAR_HUAMI_AUTH, response, response=False)

        # Response to encrypted challenge response (0x10, 0x03, status)
        elif len(data) >= 3 and data[:2] == b"\x10\x03":
            status = data[2]
            if status == 0x01:
                logger.info("Handshake verified! Device successfully authenticated.")
                self._auth_event.set()
            elif status == 0x04:
                self._auth_error = "Invalid Auth Key (rejected by device)."
                logger.error("Authentication failed: %s", self._auth_error)
                self._auth_event.set()
            else:
                self._auth_error = f"Authentication returned unknown status: {hex(status)}"
                logger.warning("Auth response status: %s", self._auth_error)
                self._auth_event.set()

    def _on_heart_rate_notification(self, _sender, data: bytearray) -> None:
        """Handle incoming heart rate measurement notifications."""
        try:
            bpm = parse_heart_rate(data)
            logger.info("Current Heart Rate: %d BPM", bpm)
            if self._hr_callback:
                self._hr_callback(bpm)
        except Exception as exc:
            logger.error("Failed to parse heart rate measurement: %s", exc)

    async def connect_and_authenticate(self, timeout_seconds: float = 25.0) -> None:
        """Establish BLE connection and execute Huami authentication handshake."""
        logger.info("Scanning for Mi Band at %s...", self.mac_address)
        device = await BleakScanner.find_device_by_address(self.mac_address, timeout=10.0)
        if device is None:
            logger.warning("Device not found during quick scan, attempting direct connect...")
            target = self.mac_address
        else:
            logger.info("Device discovered! RSSI: %s dBm", getattr(device, "rssi", "N/A"))
            target = device

        self.client = BleakClient(target)
        await self.client.connect()
        logger.info("BLE link established. Initializing auth service listener...")

        await self.client.start_notify(UUID_CHAR_HUAMI_AUTH, self._on_auth_notification)

        # Trigger random challenge request (try 0x02, 0x08)
        self._auth_event.clear()
        self._auth_error = None
        self._auth_byte = b"\x08"
        logger.info("Requesting random auth challenge from band [0x02, 0x08]...")
        await self.client.write_gatt_char(UUID_CHAR_HUAMI_AUTH, b"\x02\x08", response=False)

        try:
            await asyncio.wait_for(self._auth_event.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            # Fallback retry with 0x02, 0x00 if band uses alternative auth byte
            logger.warning("No response for [0x02, 0x08], retrying with [0x02, 0x00]...")
            self._auth_byte = b"\x00"
            await self.client.write_gatt_char(UUID_CHAR_HUAMI_AUTH, b"\x02\x00", response=False)
            try:
                await asyncio.wait_for(self._auth_event.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                raise TimeoutError("Authentication timed out waiting for device challenge response.")

        if self._auth_error:
            raise RuntimeError(f"Authentication failed: {self._auth_error}")

    async def start_heart_rate_stream(self, callback: Optional[Callable[[int], None]] = None) -> None:
        """Subscribe to real-time heart rate notifications and start sensor keep-alive."""
        self._hr_callback = callback
        logger.info("Subscribing to heart rate measurement notifications...")
        await self.client.start_notify(UUID_CHAR_HR_MEASUREMENT, self._on_heart_rate_notification)

        # Send activation command to Heart Rate Control Point
        logger.info("Sending sensor continuous mode command [0x15, 0x01, 0x01]...")
        await self.client.write_gatt_char(UUID_CHAR_HR_CONTROL, b"\x15\x01\x01", response=True)

        async def _keepalive_loop():
            try:
                while True:
                    await asyncio.sleep(12)
                    logger.debug("Sending sensor keep-alive ping [0x16]...")
                    await self.client.write_gatt_char(UUID_CHAR_HR_CONTROL, b"\x16", response=True)
            except asyncio.CancelledError:
                pass

        self._keepalive_task = asyncio.create_task(_keepalive_loop())
        logger.info("Continuous heart rate monitoring started successfully.")

    async def stop_heart_rate_stream(self) -> None:
        """Stop sensor stream and cancel keep-alive loop."""
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass

        try:
            await self.client.stop_notify(UUID_CHAR_HR_MEASUREMENT)
        except Exception as exc:
            logger.debug("Error while stopping notify: %s", exc)

    async def disconnect(self) -> None:
        """Disconnect BLE client cleanly."""
        await self.stop_heart_rate_stream()
        if self.client.is_connected:
            await self.client.disconnect()
            logger.info("Disconnected from Mi Band.")


async def main() -> None:
    """Entry point for standalone execution using .env configuration."""
    load_dotenv()
    mac = os.getenv("DEVICE_MAC")
    key = os.getenv("AUTH_KEY")

    if not mac or not key or mac.startswith("XX:") or key.startswith("012345"):
        logger.error("Please configure DEVICE_MAC and AUTH_KEY in .env before running.")
        return

    client = MiBand6Client(mac, key)
    try:
        await client.connect_and_authenticate()
        await client.start_heart_rate_stream()
        logger.info("Streaming active. Press Ctrl+C to exit.")
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Exiting on user request...")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

