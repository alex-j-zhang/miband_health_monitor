#!/usr/bin/env python3
"""Fail when a first-party code/asset file exceeds the 300-line budget.

Third-party code is intentionally excluded because it is kept verbatim from its
upstream distribution:

- vendor/tiny_ecdh/   : MIT pure-Python ECDH fallback (byte-for-byte upstream)
- huami_token/        : MIT Xiaomi/Zepp cloud client (upstream package)
- ecdh.c / ecdh.h     : tiny-ECDH-c native library (upstream C sources)
- libecdh.so          : prebuilt binary artifact

Everything else under the checkout (package, tests, tools, HTML/CSS/JS assets)
must stay at or below MAX_LINES lines so modules remain reviewable.
"""

from __future__ import annotations

import sys
from pathlib import Path

MAX_LINES = 300

ROOT = Path(__file__).resolve().parents[1]

#: Directories never scanned for the line budget.
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".cache",
    "data",
    "vendor",
    "huami_token",
}

#: Individual third-party files kept verbatim.
EXCLUDED_FILES = {
    ROOT / "ecdh.c",
    ROOT / "ecdh.h",
}

#: Extensions treated as code/assets.
CODE_SUFFIXES = {".py", ".html", ".css", ".js", ".c", ".h"}


def iter_code_files(root: Path) -> list[Path]:
    """Return every first-party code/asset file under root."""
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in CODE_SUFFIXES:
            continue
        if any(part in EXCLUDED_DIRS for part in path.relative_to(root).parts):
            continue
        if path in EXCLUDED_FILES:
            continue
        files.append(path)
    return sorted(files)


def line_count(path: Path) -> int:
    """Count physical lines, ignoring a single trailing newline."""
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def main(argv: list[str] | None = None) -> int:
    """Check all first-party files and return a non-zero status on violation."""
    files = iter_code_files(ROOT)
    violations = [(path, line_count(path)) for path in files]
    violations = [(path, count) for path, count in violations if count > MAX_LINES]

    print(f"checked {len(files)} first-party code/asset files, budget {MAX_LINES} lines")
    for path, count in violations:
        print(f"FAIL {count:>4} lines  {path.relative_to(ROOT)}")
    if violations:
        print(f"\n{len(violations)} file(s) exceed the {MAX_LINES}-line budget")
        return 1
    print("OK: every first-party code/asset file is within budget")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
