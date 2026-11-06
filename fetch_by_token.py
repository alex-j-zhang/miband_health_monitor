"""Fetch devices and auth_key directly using user_id and app_token from Zepp web session."""

import getpass
import json
import os
import sys
from pathlib import Path
import requests

TARGET_MAC = "C0:FF:EE:C0:FF:EE"
ENV_PATH = Path(__file__).parent / ".env"

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
                    if mac.upper() == TARGET_MAC.upper() or len(items) == 1:
                        print(f"\n[+] Matched target device {TARGET_MAC}!")
                        update_env("AUTH_KEY", auth_key)
                        update_env("DEVICE_MAC", TARGET_MAC)
                        print(f"[+] Successfully written to {ENV_PATH}")
                        return True
                else:
                    print("         Auth Key: Not in record")

        except Exception as e:
            print(f"     Error on {name}: {e}")

    return False


def main():
    user_id = input("Enter User ID (press Enter for default 1189174311): ").strip()
    if not user_id:
        user_id = "1189174311"

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

