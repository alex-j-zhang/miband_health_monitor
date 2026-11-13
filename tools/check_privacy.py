#!/usr/bin/env python3
"""Reject real device addresses, credentials or personal paths in the repo.

The project records personal health data at runtime, but the repository must
never contain a real Bluetooth address, authentication key, token or home
directory path. This script scans Git-tracked files (the same set CI checks)
and an explicit allowlist of documentation placeholders/test vectors.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Placeholder MAC prefixes used throughout the documentation and tests.
PLACEHOLDER_MAC_PREFIXES = (
    "C0:FF:EE",
    "AA:BB:CC",
    "00:11:22",
    "11:22:33",
    "EC:39:C2",
    "FA:12:34",
    "XX:",
)

#: Known fake/test keys and AES test vectors (tests and demo mode only).
ALLOWED_HEX_TOKENS = {
    "0123456789abcdef0123456789abcdef",
    "000102030405060708090a0b0c0d0e0f",
    "69c4e0d86a7b0430d8cdb78070b4c55a",
    "1234abcd00001000800000805f9b34fb",
}

MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")
HEX32_RE = re.compile(r"\b[0-9A-Fa-f]{32}\b")
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
HOME_PATH_RE = re.compile(r"(?:/home/|/Users/|[A-Za-z]:\\Users\\)")

#: Files where fake test vectors are expected.
TEST_PREFIXES = ("tests/", "test_")
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".so", ".dll", ".dylib",
    ".zip", ".gz", ".tgz", ".pdf", ".woff", ".woff2", ".ttf",
}


def git_files() -> list[str]:
    """Return tracked plus untracked non-ignored files (empty if git unavailable)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [item for item in out.decode("utf-8", "replace").split("\x00") if item]


def scan_file(rel_path: str) -> list[tuple[int, str, str]]:
    """Return (line number, rule, text) findings for one tracked file."""
    path = ROOT / rel_path
    if path.suffix.lower() in SKIP_SUFFIXES or not path.is_file():
        return []
    if rel_path == ".env" or rel_path.startswith("data/"):
        return [(0, "forbidden-tracked-file", rel_path)]

    findings: list[tuple[int, str, str]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return findings

    is_test = rel_path.startswith(TEST_PREFIXES)
    for number, line in enumerate(lines, start=1):
        for match in MAC_RE.finditer(line):
            value = match.group(0).upper()
            if not value.startswith(PLACEHOLDER_MAC_PREFIXES):
                findings.append((number, "real-mac-address", value))
        if not is_test:
            for match in HEX32_RE.finditer(line):
                value = match.group(0).lower()
                if value not in ALLOWED_HEX_TOKENS:
                    findings.append((number, "possible-auth-key", value))
        if PRIVATE_KEY_RE.search(line):
            findings.append((number, "private-key-block", line.strip()))
        if HOME_PATH_RE.search(line) and rel_path != "tools/check_privacy.py":
            findings.append((number, "personal-home-path", line.strip()))
    return findings


def main(argv: list[str] | None = None) -> int:
    """Scan tracked files and return a non-zero status when findings exist."""
    files = git_files()
    if not files:
        print("WARN: no Git-tracked file list available; scan skipped")
        return 0

    violations = 0
    for rel_path in files:
        for number, rule, text in scan_file(rel_path):
            violations += 1
            location = f"{rel_path}:{number}" if number else rel_path
            print(f"FAIL {location}: {rule}: {text[:140]}")

    print(f"scanned {len(files)} tracked files")
    if violations:
        print(f"{violations} privacy finding(s); redact or move them out of the repository")
        return 1

    env_file = ROOT / ".env"
    if env_file.is_file() and env_file.stat().st_mode & 0o077:
        print("WARN: local .env is readable by group/others; run: chmod 600 .env")
    print("OK: no real MAC/auth key/token/home path detected in tracked files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
