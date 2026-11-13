"""Interactive, safe auth-key extractor for Xiaomi and Amazfit/Zepp accounts.

The flow is unchanged from get_auth_key.py except that credentials are never
echoed back in full: the terminal only reports that a key was found and written
to the local .env (which is kept at 0600 and git-ignored).
"""

from __future__ import annotations

import getpass
import json
import sys

from huami_token.errors import HuamiTokenError
from huami_token.xiaomi import XiaomiClient, XiaomiSession
from huami_token.zepp import ZeppClient, ZeppSession

from ..config.defaults import ENV_AUTH_KEY
from ..config.env import load_env, update_env_file
from ..config.privacy import mask_address, mask_secret
from .constants import (
    ENV_PATH,
    TARGET_MAC,
    preferred_device_env_key,
    resolve_target_mac,
)


def _preferred_device_env_key() -> str:
    """Backwards-compatible wrapper around preferred_device_env_key(ENV_PATH)."""
    return preferred_device_env_key(ENV_PATH)


def _print_key_status(auth_key: str) -> None:
    """Report that a key exists without revealing it."""
    if auth_key:
        print(f"      Auth Key: {mask_secret(auth_key)} (found)")
    else:
        print("      Auth Key: (Not present in record)")


def _extract_xiaomi(
    username: str, password: str, configured_mac: str
) -> tuple[str | None, str, str]:
    """Log in to a Xiaomi account and return (key, device_name, mac)."""
    session = XiaomiSession(username=username, password=password)
    session.login()
    client = XiaomiClient(session)
    result = client.get_source_list()
    sources = result.get("result", {}).get("list") or []

    print(f"[*] Found {len(sources)} device(s) linked to your account:\n")
    found_key = None
    device_name = "Unknown"
    target_mac = configured_mac
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
        if auth_key.startswith(("0x", "0X")):
            auth_key = auth_key[2:]

        print(f"  [{idx + 1}] {name} (MAC: {mask_address(mac)})")
        _print_key_status(auth_key)
        if auth_key and ((configured_mac and mac == configured_mac) or not found_key):
            found_key = auth_key
            device_name = name
            target_mac = mac
    return found_key, device_name, target_mac


def _extract_zepp(
    username: str, password: str, configured_mac: str
) -> tuple[str | None, str, str]:
    """Log in to an Amazfit/Zepp account and return (key, device_name, mac)."""
    session = ZeppSession(username=username, password=password)
    session.login()
    client = ZeppClient(session)
    devices = client.get_devices()

    print(f"[*] Found {len(devices)} device(s) linked to your account:\n")
    found_key = None
    device_name = "Unknown"
    target_mac = configured_mac
    for idx, device in enumerate(devices):
        mac = str(device.mac).upper()
        auth_key = str(device.auth_key or "")
        if auth_key.startswith(("0x", "0X")):
            auth_key = auth_key[2:]

        print(f"  [{idx + 1}] Device {mask_address(mac)} | Active: {device.active}")
        _print_key_status(auth_key)
        if auth_key and ((configured_mac and mac == configured_mac) or not found_key):
            found_key = auth_key
            device_name = f"Device {mask_address(mac)}"
            target_mac = mac
    return found_key, device_name, target_mac


def run_extractor() -> int:
    """Run the interactive extractor and return a process exit code."""
    load_env(ENV_PATH)
    configured_mac = resolve_target_mac() or TARGET_MAC

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
        if method == "xiaomi":
            found_key, device_name, target_mac = _extract_xiaomi(
                username, password, configured_mac
            )
        else:
            found_key, device_name, target_mac = _extract_zepp(
                username, password, configured_mac
            )

        if not found_key:
            print("\n[-] Could not find an Auth Key in the account's device records.")
            return 1

        print("\n" + "=" * 60)
        print(f"[+] Successfully extracted Auth Key for {device_name}!")
        print(f"[+] Key: {mask_secret(found_key)} (written to .env)")
        update_env_file(ENV_PATH, ENV_AUTH_KEY, found_key)
        if target_mac:
            update_env_file(ENV_PATH, _preferred_device_env_key(), target_mac)
        print(f"[+] Successfully updated {ENV_PATH}")
        print("=" * 60)
        print("\nNext steps:")
        print("1. Turn OFF Bluetooth on your phone now.")
        print("2. Run the client: python miband_client.py")
        return 0

    except HuamiTokenError as exc:
        print(f"\n[-] Authentication error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\n[-] Unexpected error: {exc}", file=sys.stderr)
        return 1
