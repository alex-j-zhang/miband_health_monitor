"""Regression tests for auth-helper configuration, log redaction, and layering."""

from __future__ import annotations

import ast
import logging
import os
from pathlib import Path

from miband_monitor.auth import token as auth_token
from miband_monitor.auth.constants import preferred_device_env_key, resolve_target_mac
from miband_monitor.cli.logging import LOG_FORMAT, RedactingFormatter

SAMPLE_MAC = ":".join(["C0", "FF", "EE", "12", "34", "56"])
SAMPLE_KEY = "0123456789abcdef0123456789abcdef"


def test_resolve_target_mac_skips_placeholder(monkeypatch):
    monkeypatch.setenv("DEVICE_ID", "XX:XX:XX:XX:XX:XX")
    monkeypatch.setenv("DEVICE_MAC", SAMPLE_MAC.lower())
    assert resolve_target_mac() == SAMPLE_MAC

    monkeypatch.delenv("DEVICE_MAC", raising=False)
    assert resolve_target_mac() == ""


def test_preferred_device_env_key_respects_existing_file(tmp_path):
    env_file = tmp_path / ".env"
    assert preferred_device_env_key(env_file) == "DEVICE_MAC"

    env_file.write_text("DEVICE_ID=XX:XX:XX:XX:XX:XX\n", encoding="utf-8")
    assert preferred_device_env_key(env_file) == "DEVICE_ID"


def test_fetch_devices_redacts_console_and_secures_env(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("DEVICE_ID=XX:XX:XX:XX:XX:XX\n", encoding="utf-8")
    monkeypatch.setattr(auth_token, "ENV_PATH", env_file)
    monkeypatch.setenv("DEVICE_ID", "XX:XX:XX:XX:XX:XX")
    monkeypatch.delenv("DEVICE_MAC", raising=False)

    class FakeResponse:
        status_code = 200
        reason = "OK"

        @staticmethod
        def json():
            return {
                "items": [
                    {
                        "macAddress": SAMPLE_MAC,
                        "deviceName": "Mi Smart Band 6",
                        "additionalInfo": {"auth_key": "0x" + SAMPLE_KEY},
                    }
                ]
            }

    monkeypatch.setattr(auth_token.requests, "get", lambda *a, **k: FakeResponse())
    assert auth_token.fetch_devices("10001", "fake-app-token") is True

    out = capsys.readouterr().out
    assert SAMPLE_KEY not in out
    assert SAMPLE_MAC not in out
    assert "C0:FF:**:**:34:56" in out

    saved = env_file.read_text(encoding="utf-8")
    assert f"AUTH_KEY={SAMPLE_KEY}" in saved
    assert f"DEVICE_ID={SAMPLE_MAC}" in saved
    if os.name != "nt":
        assert (env_file.stat().st_mode & 0o777) == 0o600


def test_redacting_formatter_masks_message_and_traceback_without_mutating_record():
    formatter = RedactingFormatter(fmt=LOG_FORMAT)
    try:
        raise RuntimeError(f"failed for {SAMPLE_MAC} with key {SAMPLE_KEY}")
    except RuntimeError:
        import sys

        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="MiBandTest",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="connecting to %s",
        args=(SAMPLE_MAC,),
        exc_info=exc_info,
    )
    rendered = formatter.format(record)
    assert SAMPLE_MAC not in rendered
    assert SAMPLE_KEY not in rendered
    assert "C0:FF:**:**:34:56" in rendered
    assert "<redacted>" in rendered
    # Shared LogRecord fields remain untouched for other handlers / caplog.
    assert record.msg == "connecting to %s"
    assert record.args == (SAMPLE_MAC,)


def test_package_never_imports_legacy_root_shims():
    pkg_root = Path(__file__).resolve().parents[1] / "miband_monitor"
    legacy_shims = {
        "miband_client", "storage", "dashboard", "band_history", "bluez_support",
        "sleep_analysis", "history_dashboard", "sim_band", "ecdh_wrapper",
        "doctor", "export_csv", "history_report", "scan_ble", "scan_live",
        "get_auth_key", "fetch_by_token",
    }
    for path in pkg_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                assert node.module.split(".")[0] not in legacy_shims, f"{path} imports {node.module}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in legacy_shims, f"{path} imports {alias.name}"
