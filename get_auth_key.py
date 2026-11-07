"""Interactive and safe Auth Key extractor for Xiaomi and Amazfit/Zepp accounts."""

import getpass
import json
import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent))

from huami_token.errors import HuamiTokenError
from huami_token.xiaomi import XiaomiClient, XiaomiSession
from huami_token.zepp import ZeppClient, ZeppSession

ENV_PATH = Path(__file__).parent / ".env"

try:  # optional convenience: pick up DEVICE_MAC from the local .env
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None
if load_dotenv is not None:
    load_dotenv(ENV_PATH)

# Never hard-code a real band MAC in the repository: prefer the configured one.
TARGET_MAC = (os.getenv("DEVICE_MAC") or "").strip().upper()


def update_env_file(key: str, value: str) -> None:
    """Safely update or add an environment variable in the .env file."""
    lines = []
    found = False

    if ENV_PATH.exists():
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)

    if not found:
        new_lines.append(f"{key}={value}\n")

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    # Credentials file: keep it readable only by the owner.
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:  # pragma: no cover - e.g. exotic filesystems
        pass


def run_extractor() -> int:
    print("=" * 60)
    print("  Mi Band 6 - Safe Auth Key Extractor")
    print("=" * 60)
    print("This script connects to your account to retrieve the pairing token.")
    print("Your password is input securely (hidden) and will NEVER be stored.\n")

    print("Select login type:")
    print("  [1] Xiaomi Account (小米账号 / 手机号 / 邮箱 - 默认)")
    print("  [2] Amazfit / Zepp Account (华米官方邮箱账号)")
    choice = input("Enter choice [1/2] (Default 1): ").strip()
    method = "amazfit" if choice == "2" else "xiaomi"

    username = input("\nEnter your Account (Phone / Email / ID): ").strip()
    if not username:
        print("[-] Account cannot be empty.")
        return 1

    password = getpass.getpass("Enter your Password (hidden): ")
    if not password:
        print("[-] Password cannot be empty.")
        return 1

    print(f"\n[*] Authenticating with {method.capitalize()} servers...")

    try:
        found_key = None
        device_name = "Unknown"
        target_mac = TARGET_MAC

        if method == "xiaomi":
            session = XiaomiSession(username=username, password=password)
            session.login()
            client = XiaomiClient(session)
            result = client.get_source_list()
            sources = result.get("result", {}).get("list") or []

            print(f"[*] Found {len(sources)} device(s) linked to your account:\n")
            for idx, source in enumerate(sources):
                name = source.get("name", "Unknown").strip()
                detail = source.get("detail", {})
                if isinstance(detail, str):
                    try:
                        detail = json.loads(detail)
                    except json.JSONDecodeError:
                        detail = {}

                mac = detail.get("mac", source.get("mac", "Unknown")).upper()
                auth_key = detail.get("auth_key", "")
                if auth_key.startswith("0x") or auth_key.startswith("0X"):
                    auth_key = auth_key[2:]

                print(f"  [{idx + 1}] {name} (MAC: {mac})")
                if auth_key:
                    print(f"      Auth Key: {auth_key}")
                    if (TARGET_MAC and mac == TARGET_MAC) or not found_key:
                        found_key = auth_key
                        device_name = name
                        target_mac = mac
                else:
                    print("      Auth Key: (Not present in record)")

        else:  # amazfit
            session = ZeppSession(username=username, password=password)
            session.login()
            client = ZeppClient(session)
            devices = client.get_devices()

            print(f"[*] Found {len(devices)} device(s) linked to your account:\n")
            for idx, device in enumerate(devices):
                mac = str(device.mac).upper()
                auth_key = str(device.auth_key or "")
                if auth_key.startswith("0x") or auth_key.startswith("0X"):
                    auth_key = auth_key[2:]

                print(f"  [{idx + 1}] Device {mac} | Active: {device.active}")
                if auth_key:
                    print(f"      Auth Key: {auth_key}")
                    if (TARGET_MAC and mac == TARGET_MAC) or not found_key:
                        found_key = auth_key
                        device_name = f"Device {mac}"
                        target_mac = mac

        if found_key:
            print("\n" + "=" * 60)
            print(f"[+] Successfully extracted Auth Key for {device_name}!")
            print(f"[+] Key: {found_key}")
            update_env_file("AUTH_KEY", found_key)
            if target_mac:
                update_env_file("DEVICE_MAC", target_mac)
            print(f"[+] Successfully updated {ENV_PATH}")
            print("=" * 60)
            print("\nNext steps:")
            print("1. Turn OFF Bluetooth on your phone now.")
            print("2. Run the client: python miband_client.py")
            return 0
        else:
            print("\n[-] Could not find an Auth Key in the account's device records.")
            return 1

    except HuamiTokenError as exc:
        print(f"\n[-] Authentication error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\n[-] Unexpected error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run_extractor())

