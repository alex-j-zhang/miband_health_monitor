"""Mi Band 6 BLE client implementation with Chunked Transfer ECDH authentication."""

import asyncio
import logging
import os
import sys
from typing import Callable, Optional
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from dotenv import load_dotenv
from bleak import BleakClient, BleakScanner
from ecdh_wrapper import generate_ecdh_keys, compute_shared_secret

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("MiBand6Client")

# BLE GATT Service and Characteristic UUIDs
UUID_SVC_MIBAND1 = "0000fee0-0000-1000-8000-00805f9b34fb"
UUID_CHAR_CHUNKED_WRITE = "00000016-0000-3512-2118-0009af100700"
UUID_CHAR_CHUNKED_READ = "00000017-0000-3512-2118-0009af100700"

UUID_SVC_HEART_RATE = "0000180d-0000-1000-8000-00805f9b34fb"
UUID_CHAR_HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
UUID_CHAR_HR_CONTROL = "00002a39-0000-1000-8000-00805f9b34fb"

CHUNKED_ENDPOINT_AUTH = 0x82


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


async def write_chunked_value(
    client: BleakClient,
    char_uuid: str,
    endpoint_type: int,
    handle: int,
    data: bytes
) -> None:
    """Split and transmit a payload using the Mi Band 6 Chunked Transfer Protocol."""
    remaining = len(data)
    count = 0
    header_size = 11
    mtu = 23

    while remaining > 0:
        max_chunk = mtu - 3 - header_size
        copy_bytes = min(remaining, max_chunk)
        chunk = bytearray(copy_bytes + header_size)
        flags = 0

        if count == 0:
            flags |= 0x01
            chunk[5] = len(data) & 0xFF
            chunk[6] = (len(data) >> 8) & 0xFF
            chunk[7] = (len(data) >> 16) & 0xFF
            chunk[8] = (len(data) >> 24) & 0xFF
            chunk[9] = endpoint_type & 0xFF
            chunk[10] = (endpoint_type >> 8) & 0xFF

        if remaining <= max_chunk:
            flags |= 0x06  # End of transmission frame

        chunk[0] = 0x03
        chunk[1] = flags
        chunk[2] = 0
        chunk[3] = handle & 0xFF
        chunk[4] = count & 0xFF

        offset = len(data) - remaining
        chunk[header_size:] = data[offset : offset + copy_bytes]

        await client.write_gatt_char(char_uuid, bytes(chunk), response=False)
        remaining -= copy_bytes
        header_size = 5
        count += 1


class MiBand6Client:
    """Handles BLE connection, Chunked Transfer ECDH authentication, and heart rate streaming."""

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

        self._handle = 0
        self._reassemble_buf = bytearray(512)
        self._reassemble_ptr = 0
        self._expected_bytes = 0
        self._last_seq = 0
        self._step2_sent = False

        # Generate NIST B-163 ECDH key pair
        self._prv_key, self._pub_key = generate_ecdh_keys()

    def _on_chunked_notification(self, _sender, data: bytearray) -> None:
        """Process incoming chunked transfer packets from the band."""
        if len(data) > 1 and data[0] == 0x03:
            seq = data[4]
            if (
                seq == 0
                and len(data) >= 14
                and data[9] == CHUNKED_ENDPOINT_AUTH
                and data[10] == 0x00
                and data[11] == 0x10
            ):
                step = data[12]
                status = data[13]

                if step == 0x04 and status == 0x01:
                    logger.info("Chunked Auth Step 1 acknowledged. Reassembling challenge & band public key...")
                    self._reassemble_ptr = 0
                    self._expected_bytes = data[5] - 3
                    self._last_seq = 0
                    header_size = 14
                    payload = data[header_size:]
                    self._reassemble_buf[self._reassemble_ptr : self._reassemble_ptr + len(payload)] = payload
                    self._reassemble_ptr += len(payload)

                elif step == 0x05 and status == 0x01:
                    logger.info("Chunked Auth Step 2 acknowledged! Mi Band 6 authentication SUCCEEDED!")
                    self._auth_event.set()
                    return
                else:
                    self._auth_error = f"Auth step {hex(step)} returned status {hex(status)}"
                    logger.warning("Auth failure: %s", self._auth_error)
                    self._auth_event.set()
                    return

            elif seq > 0:
                header_size = 5
                payload = data[header_size:]
                self._reassemble_buf[self._reassemble_ptr : self._reassemble_ptr + len(payload)] = payload
                self._reassemble_ptr += len(payload)
                self._last_seq = seq

            if self._reassemble_ptr == self._expected_bytes and not self._step2_sent:
                self._step2_sent = True
                asyncio.create_task(self._complete_auth_step2())

    async def _complete_auth_step2(self) -> None:
        """Compute ECDH shared secret and send challenge response."""
        remote_random = bytes(self._reassemble_buf[:16])
        remote_public = bytes(self._reassemble_buf[16:64])
        logger.info("Received random challenge salt (%s)", remote_random.hex())
        logger.info("Received band ECDH public key (%s)", remote_public.hex())

        # Compute ECDH shared secret (48 bytes)
        shared_secret = compute_shared_secret(self._prv_key, remote_public)

        # Derive final shared session AES key (16 bytes)
        final_shared_session_aes = bytearray(16)
        for i in range(16):
            final_shared_session_aes[i] = shared_secret[i + 8] ^ self.auth_key[i]

        # AES-CBC encrypt challenge with IV = 16 zeros
        iv = b"\x00" * 16
        cipher1 = Cipher(algorithms.AES(self.auth_key), modes.CBC(iv))
        enc1 = cipher1.encryptor()
        out1 = enc1.update(remote_random) + enc1.finalize()

        cipher2 = Cipher(algorithms.AES(bytes(final_shared_session_aes)), modes.CBC(iv))
        enc2 = cipher2.encryptor()
        out2 = enc2.update(remote_random) + enc2.finalize()

        command = b"\x05" + out1 + out2
        logger.info("Sending Chunked Auth Step 2 response (33 bytes)...")
        self._handle += 1
        await write_chunked_value(
            self.client,
            UUID_CHAR_CHUNKED_WRITE,
            CHUNKED_ENDPOINT_AUTH,
            self._handle,
            command,
        )

    def _on_heart_rate_notification(self, _sender, data: bytearray) -> None:
        """Handle incoming heart rate measurement notifications."""
        try:
            bpm = parse_heart_rate(data)
            logger.info("❤️  Current Heart Rate: %d BPM", bpm)
            if self._hr_callback:
                self._hr_callback(bpm)
        except Exception as exc:
            logger.error("Failed to parse heart rate measurement: %s", exc)

    async def connect_and_authenticate(self, timeout_seconds: float = 30.0) -> None:
        """Establish BLE connection and execute Chunked Transfer ECDH authentication."""
        logger.info("Waiting for Mi Band broadcast at %s (please touch/light up band screen)...", self.mac_address)
        device = None
        for attempt in range(6):
            device = await BleakScanner.find_device_by_address(self.mac_address, timeout=5.0)
            if device:
                break
            logger.info("Searching for band... (%d/6, tap band screen to wake up)", attempt + 1)

        if device is None:
            raise TimeoutError(
                f"Could not discover band {self.mac_address}. Please ensure the band screen is lit up and phone Bluetooth is off."
            )

        logger.info("Band detected! RSSI: %s dBm. Establishing BLE link...", getattr(device, "rssi", "N/A"))
        self.client = BleakClient(device)
        await self.client.connect()
        logger.info("BLE link established. Initializing chunked transfer listener...")

        await self.client.start_notify(UUID_CHAR_CHUNKED_READ, self._on_chunked_notification)

        self._auth_event.clear()
        self._auth_error = None
        self._step2_sent = False

        # Auth Step 1: Send client public key
        initial_auth = b"\x04\x02\x00\x02" + self._pub_key
        logger.info("Initiating Chunked ECDH handshake (sending client public key)...")
        self._handle = 0
        await write_chunked_value(
            self.client,
            UUID_CHAR_CHUNKED_WRITE,
            CHUNKED_ENDPOINT_AUTH,
            self._handle,
            initial_auth,
        )

        try:
            await asyncio.wait_for(self._auth_event.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            raise TimeoutError("Authentication timed out waiting for band ECDH response.")

        if self._auth_error:
            raise RuntimeError(f"Authentication failed: {self._auth_error}")

    async def start_heart_rate_stream(self, callback: Optional[Callable[[int], None]] = None) -> None:
        """Subscribe to real-time heart rate notifications and start sensor keep-alive."""
        self._hr_callback = callback
        logger.info("Initializing heart rate sensor...")

        # Reset measurement state
        await self.client.write_gatt_char(UUID_CHAR_HR_CONTROL, b"\x15\x02\x00", response=False)
        await self.client.write_gatt_char(UUID_CHAR_HR_CONTROL, b"\x15\x01\x00", response=False)

        await self.client.start_notify(UUID_CHAR_HR_MEASUREMENT, self._on_heart_rate_notification)

        # Start continuous measurement
        logger.info("Starting continuous heart rate measurement [0x15, 0x01, 0x01]...")
        await self.client.write_gatt_char(UUID_CHAR_HR_CONTROL, b"\x15\x01\x01", response=False)

        async def _keepalive_loop():
            try:
                while True:
                    await asyncio.sleep(12)
                    logger.debug("Pinging heart rate monitor keep-alive [0x16]...")
                    await self.client.write_gatt_char(UUID_CHAR_HR_CONTROL, b"\x16", response=False)
            except asyncio.CancelledError:
                pass

        self._keepalive_task = asyncio.create_task(_keepalive_loop())
        logger.info("Continuous heart rate monitoring active! Streaming real-time data...")

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
        logger.info("Heart rate streaming active. Press Ctrl+C to exit.")
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Exiting on user request...")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
