#!/usr/bin/env python3
"""Check that relative Markdown links and images resolve inside the checkout.

External URLs (http/https/mailto) and pure in-page anchors are ignored. The
checker skips fenced code blocks, so example links inside code samples are not
treated as real references.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def markdown_files(root: Path) -> list[Path]:
    """Return every Markdown file in the repository."""
    return sorted(
        path
        for path in root.rglob("*.md")
        if not any(
            part in {".git", ".venv", "node_modules", "__pycache__"}
            for part in path.parts
        )
    )


def iter_links(text: str):
    """Yield links outside fenced code blocks."""
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("~~~") or stripped.startswith("\u0060\u0060\u0060"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in LINK_RE.finditer(line):
            yield match.group(1).strip()


def is_external(target: str) -> bool:
    """Return True for URLs and anchors that do not name a local file."""
    lower = target.lower()
    return lower.startswith(("http://", "https://", "mailto:", "tel:", "#", "data:"))


def main(argv: list[str] | None = None) -> int:
    """Validate local Markdown links and return a non-zero status on failure."""
    missing: list[str] = []
    checked = 0
    for path in markdown_files(ROOT):
        text = path.read_text(encoding="utf-8", errors="replace")
        for target in iter_links(text):
            if not target or is_external(target):
                continue
            clean = unquote(target.split("#", 1)[0].split("?", 1)[0]).strip()
            if not clean or clean.startswith("/"):
                continue
            checked += 1
            resolved = (path.parent / clean).resolve()
            if not resolved.exists():
                missing.append(f"{path.relative_to(ROOT)} -> {target}")

    print(f"checked {checked} local Markdown links")
    for item in missing:
        print(f"FAIL {item}")
    if missing:
        print(f"{len(missing)} broken local link(s)")
        return 1
    print("OK: all local Markdown links resolve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
