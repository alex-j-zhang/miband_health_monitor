"""tools/check_privacy.py must reject real identifiers and personal paths."""

from __future__ import annotations

import pytest

from tools import check_privacy

# Built at runtime so this test file stays clean for the scanner itself: the
# literal patterns only exist in the generated fixture files, never in source.
REAL_MAC = ":".join(["DE", "AD", "BE", "EF", "00", "01"])
FAKE_KEY = "f" * 32
PRIVATE_KEY_HEADER = "-----BEGIN RSA " + "PRIVATE KEY-----"
PERSONAL_PATH = "/" + "home/" + "someone/project"


@pytest.fixture()
def scanner_root(tmp_path, monkeypatch):
    """Point the scanner at an isolated tree instead of the checkout."""
    monkeypatch.setattr(check_privacy, "ROOT", tmp_path)
    return tmp_path


def _write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_placeholder_identity_and_test_vectors_are_allowed(scanner_root):
    _write(
        scanner_root,
        "module.py",
        'MAC = "C0:FF:EE:C0:FF:EE"\n'
        'KEY = "0123456789abcdef0123456789abcdef"\n',
    )
    assert check_privacy.scan_file("module.py") == []


def test_real_identifiers_are_rejected(scanner_root):
    _write(scanner_root, "leak.py", f'MAC = "{REAL_MAC}"\nKEY = "{FAKE_KEY}"\n')
    rules = {rule for _, rule, _ in check_privacy.scan_file("leak.py")}
    assert {"real-mac-address", "possible-auth-key"} <= rules


def test_auth_key_rule_skips_test_files(scanner_root):
    _write(scanner_root, "tests/test_vectors.py", f'KEY = "{FAKE_KEY}"\n')
    assert check_privacy.scan_file("tests/test_vectors.py") == []


def test_private_key_block_and_personal_path_are_rejected(scanner_root):
    _write(scanner_root, "notes.txt", f"{PRIVATE_KEY_HEADER}\n{PERSONAL_PATH}\n")
    rules = {rule for _, rule, _ in check_privacy.scan_file("notes.txt")}
    assert {"private-key-block", "personal-home-path"} <= rules


def test_binary_suffixes_are_not_scanned(scanner_root):
    _write(scanner_root, "capture.png", f'MAC = "{REAL_MAC}"\n')
    assert check_privacy.scan_file("capture.png") == []


def test_data_directory_and_env_are_forbidden(scanner_root):
    _write(scanner_root, "data/mi_band.db", "")
    _write(scanner_root, ".env", "MIBAND_DEVICE_ID=C0:FF:EE:C0:FF:EE\n")
    assert check_privacy.scan_file("data/mi_band.db") == [
        (0, "forbidden-tracked-file", "data/mi_band.db"),
    ]
    assert check_privacy.scan_file(".env") == [(0, "forbidden-tracked-file", ".env")]


def test_main_returns_nonzero_only_when_findings_exist(scanner_root, monkeypatch, capsys):
    _write(scanner_root, "clean.py", "VALUE = 1\n")
    monkeypatch.setattr(check_privacy, "git_files", lambda: ["clean.py"])
    assert check_privacy.main([]) == 0
    assert "OK: no real MAC" in capsys.readouterr().out

    _write(scanner_root, "leak.py", f'MAC = "{REAL_MAC}"\n')
    monkeypatch.setattr(check_privacy, "git_files", lambda: ["leak.py"])
    assert check_privacy.main([]) == 1
    out = capsys.readouterr().out
    assert "real-mac-address" in out
    assert "leak.py" in out
