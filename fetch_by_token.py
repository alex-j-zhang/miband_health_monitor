"""Fetch devices and auth_key directly using user_id and app_token from Zepp web session."""

import getpass
import json
import os
import sys
from pathlib import Path
import requests

ENV_PATH = Path(__file__).parent / ".env"

try:  # optional convenience: pick up DEVICE_MAC from the local .env
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None
if load_dotenv is not None:
    load_dotenv(ENV_PATH)

# Optional target device from DEVICE_MAC; never hard-code a real band MAC here.
TARGET_MAC = (os.getenv("DEVICE_MAC") or "").strip().upper()

ENDPOINTS = [
    ("api-mifit.huami.com (CN)", "https://api-mifit.huami.com/users/{user_id}/devices", "CN"),
    ("api-mifit-cn2.zepp.com (CN2)", "https://api-mifit-cn2.zepp.com/users/{user_id}/devices", "CN"),
    ("api-mifit.zepp.com (Global)", "https://api-mifit.zepp.com/users/{user_id}/devices", "US"),
    ("api-mifit-us3.zepp.com (US3)", "https://api-mifit-us3.zepp.com/users/{user_id}/devices", "US"),
]


def update_env(key: str, value: str) -> None:
    lines = []
    if ENV_PATH.exists():
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    found = False
    new_lines = []
    for line in lines:
        if line.strip().startswith(f"{key}=") or line.strip().startswith(f"{key} ="):
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


def fetch_devices(user_id: str, app_token: str) -> bool:
    print(f"\n[*] Querying device list for User ID: {user_id} across Zepp/Huami regional servers...")

    for name, endpoint_tpl, country in ENDPOINTS:
        url = endpoint_tpl.format(user_id=user_id)
        params = {
            "appid": "2882303761517383915",
            "channel": "play",
            "enableMultiDevice": "true",
            "device_type": "android_phone",
            "cv": "151262_8.7.1-play",
            "device": "android_33",
            "v": "2.0",
            "lang": "zh_CN" if country == "CN" else "en_US",
            "country": country,
        }
        headers = {
            "User-Agent": "Zepp/8.5.3-play (2201116SG; Android 12; Density/2.525)",
            "apptoken": app_token,
            "channel": "play",
            "appname": "com.huami.midong",
        }

        try:
            print(f"  -> Trying {name}...")
            res = requests.get(url, params=params, headers=headers, timeout=10)
            if res.status_code != 200:
                print(f"     Status: {res.status_code} ({res.reason})")
                continue

            data = res.json()
            items = data.get("items") or []
            print(f"     Status: 200 OK | Found {len(items)} device(s)")

            matches: list[tuple[str, str]] = []
            for idx, item in enumerate(items):
                mac = item.get("macAddress") or item.get("mac") or item.get("deviceSource") or "Unknown"
                name_str = item.get("deviceName") or item.get("model") or "Band"
                info_raw = item.get("additionalInfo") or "{}"
                if isinstance(info_raw, str):
                    try:
                        info = json.loads(info_raw)
                    except json.JSONDecodeError:
                        info = {}
                else:
                    info = info_raw

                auth_key = info.get("auth_key") or info.get("authKey") or ""
                print(f"     [{idx+1}] {name_str} (MAC: {mac})")
                if auth_key:
                    print(f"         Auth Key: {auth_key}")
                    matches.append((str(mac).upper(), auth_key))
                else:
                    print("         Auth Key: Not in record")

            chosen = None
            if TARGET_MAC:
                chosen = next((m for m in matches if m[0] == TARGET_MAC), None)
            if chosen is None and len(matches) == 1:
                chosen = matches[0]
            if chosen is None and not TARGET_MAC and matches:
                chosen = matches[0]

            if chosen is not None:
                found_mac, found_key = chosen
                if TARGET_MAC and found_mac != TARGET_MAC:
                    print(f"\n[!] {TARGET_MAC} not found; using device {found_mac}.")
                elif not TARGET_MAC and len(matches) > 1:
                    print(f"\n[!] Multiple devices found; using {found_mac}.")
                print(f"\n[+] Selected device {found_mac}.")
                update_env("AUTH_KEY", found_key)
                update_env("DEVICE_MAC", found_mac)
                print(f"[+] Successfully written to {ENV_PATH}")
                return True

        except Exception as e:
            print(f"     Error on {name}: {e}")

    return False


def main():
    # No default account id is embedded: enter your own Zepp/Xiaomi account id.
    user_id = input("Enter your numeric Zepp/Xiaomi account User ID: ").strip()
    if not user_id:
        print("[-] User ID is required.")
        return 1

    app_token = getpass.getpass("Enter App Token from browser (hidden input): ").strip()
    if not app_token:
        print("[-] App Token cannot be empty.")
        return 1

    success = fetch_devices(user_id, app_token)
    if success:
        print("\nAll done! You can now turn off phone Bluetooth and run:")
        print("  python miband_client.py")
        return 0
    else:
        print("\n[-] Could not locate device with auth_key. Check App Token validity.")
        return 1


if __name__ == "__main__":
    sys.exit(main())

