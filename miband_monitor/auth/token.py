"""Fetch devices and auth_key directly using user_id and app_token from a Zepp web session."""

from __future__ import annotations

import getpass
import json

import requests

from ..config.defaults import ENV_AUTH_KEY
from ..config.env import load_env, update_env_file
from ..config.privacy import mask_address, mask_secret
from .constants import (
    ENV_PATH,
    ZEPP_WEB_BASE_PARAMS,
    ZEPP_WEB_ENDPOINTS,
    ZEPP_WEB_HEADERS,
    ZEPP_WEB_TIMEOUT,
    preferred_device_env_key,
    resolve_target_mac,
)


def _request_params(country: str) -> dict:
    """Return the query parameters for one regional endpoint."""
    params = dict(ZEPP_WEB_BASE_PARAMS)
    params["lang"] = "zh_CN" if country == "CN" else "en_US"
    params["country"] = country
    return params


def _request_headers(app_token: str) -> dict:
    """Return the headers for one device-list request."""
    headers = dict(ZEPP_WEB_HEADERS)
    headers["apptoken"] = app_token
    return headers


def _extract_matches(items: list[dict]) -> list[tuple[str, str]]:
    """Return (mac, auth_key) pairs from one regional response."""
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
        if auth_key.startswith(("0x", "0X")):
            auth_key = auth_key[2:]
        print(f"     [{idx + 1}] {name_str} (MAC: {mask_address(str(mac))})")
        if auth_key:
            print(f"         Auth Key: {mask_secret(auth_key)} (found)")
            matches.append((str(mac).upper(), auth_key))
        else:
            print("         Auth Key: Not in record")
    return matches


def fetch_devices(user_id: str, app_token: str) -> bool:
    """Query the regional endpoints and write the selected auth_key to .env."""
    load_env(ENV_PATH)
    target_mac = resolve_target_mac()
    print(f"\n[*] Querying device list for User ID: {user_id} across Zepp/Huami regional servers...")

    for name, endpoint_tpl, country in ZEPP_WEB_ENDPOINTS:
        url = endpoint_tpl.format(user_id=user_id)
        try:
            print(f"  -> Trying {name}...")
            res = requests.get(
                url,
                params=_request_params(country),
                headers=_request_headers(app_token),
                timeout=ZEPP_WEB_TIMEOUT,
            )
            if res.status_code != 200:
                print(f"     Status: {res.status_code} ({res.reason})")
                continue

            data = res.json()
            items = data.get("items") or []
            print(f"     Status: 200 OK | Found {len(items)} device(s)")
            matches = _extract_matches(items)

            chosen = None
            if target_mac:
                chosen = next((m for m in matches if m[0] == target_mac), None)
            if chosen is None and len(matches) == 1:
                chosen = matches[0]
            if chosen is None and not target_mac and matches:
                chosen = matches[0]

            if chosen is not None:
                found_mac, found_key = chosen
                if target_mac and found_mac != target_mac:
                    print(
                        f"\n[!] {mask_address(target_mac)} not found; "
                        f"using device {mask_address(found_mac)}."
                    )
                elif not target_mac and len(matches) > 1:
                    print(f"\n[!] Multiple devices found; using {mask_address(found_mac)}.")
                print(f"\n[+] Selected device {mask_address(found_mac)}.")
                update_env_file(ENV_PATH, ENV_AUTH_KEY, found_key)
                update_env_file(ENV_PATH, preferred_device_env_key(ENV_PATH), found_mac)
                print(f"[+] Successfully written to {ENV_PATH}")
                return True

        except Exception as exc:
            print(f"     Error on {name}: {exc}")

    return False


def main() -> int:
    """Interactive CLI entry point for fetch_by_token.py."""
    # No default account id is embedded: enter your own Zepp/Xiaomi account id.
    user_id = input("Enter your numeric Zepp/Xiaomi account User ID: ").strip()
    if not user_id:
        print("[-] User ID is required.")
        return 1

    app_token = getpass.getpass("Enter App Token from browser (hidden input): ").strip()
    if not app_token:
        print("[-] App Token cannot be empty.")
        return 1

    if fetch_devices(user_id, app_token):
        print("\nAll done! You can now turn off phone Bluetooth and run:")
        print("  python miband_client.py")
        return 0
    print("\n[-] Could not locate device with auth_key. Check App Token validity.")
    return 1
