"""Single-file dashboard page composed from the assets/ directory.

The live dashboard ships as one self-contained HTML document: the CSS and
JavaScript assets are inserted inline at request time so the browser needs no
extra round-trips and the page keeps working when served from an air-gapped
machine. Composition is cached with functools.lru_cache because the assets are
immutable for the life of the process; if they are missing, the server falls
back to FALLBACK_INDEX (API links only).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Tuple

from ..config.paths import package_path

logger = logging.getLogger("MiBandDashboard")

#: Directory holding the page shell and the inline CSS/JS assets.
ASSETS_DIR = package_path("dashboard", "assets")
PAGE_TEMPLATE_NAME = "dashboard.html"
CSS_ASSET_NAME = "dashboard.css"
SCRIPT_ASSET_NAME = "dashboard.js"

#: Markers in the HTML shell that the assets replace. They are deliberately
#: unique strings so composition is a single pass with no parser dependency.
CSS_TOKEN = "__DASHBOARD_CSS__"
SCRIPT_TOKEN = "__DASHBOARD_SCRIPT__"

#: Minimal page served when the dashboard assets cannot be read. It must keep
#: the JSON/SSE API discoverable, matching the historical fallback behaviour.
FALLBACK_INDEX = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Mi Band 6 实时看板</title></head>
<body style="font-family:sans-serif;background:#0f1216;color:#e6edf3;padding:24px">
<h1>Mi Band 6 实时看板</h1>
<p>dashboard.html 未找到，但 API 仍可用：</p>
<ul><li><a style="color:#38bdf8" href="/api/snapshot">/api/snapshot</a></li>
<li><a style="color:#38bdf8" href="/events">/events</a> (SSE)</li>
<li><a style="color:#38bdf8" href="/healthz">/healthz</a></li></ul>
</body></html>"""

#: Historical private alias kept for the module facade.
_FALLBACK_INDEX = FALLBACK_INDEX


@lru_cache(maxsize=1)
def _load_assets() -> Tuple[str, str, str]:
    """Read the page shell and inline assets once (text, CSS, JavaScript)."""
    base = Path(ASSETS_DIR)
    shell = (base / PAGE_TEMPLATE_NAME).read_text(encoding="utf-8")
    css = (base / CSS_ASSET_NAME).read_text(encoding="utf-8").rstrip("\n")
    script = (base / SCRIPT_ASSET_NAME).read_text(encoding="utf-8").rstrip("\n")
    return shell, css, script


def compose_page() -> str:
    """Return the single-file dashboard HTML (CSS/JS inserted inline)."""
    shell, css, script = _load_assets()
    return shell.replace(CSS_TOKEN, css).replace(SCRIPT_TOKEN, script)


@lru_cache(maxsize=1)
def page_bytes() -> bytes:
    """UTF-8 encoded composed page, cached for the HTTP handler."""
    return compose_page().encode("utf-8")


@lru_cache(maxsize=1)
def fallback_index_bytes() -> bytes:
    """UTF-8 encoded fallback page, cached for the HTTP handler."""
    return FALLBACK_INDEX.encode("utf-8")


__all__ = [
    "ASSETS_DIR",
    "CSS_ASSET_NAME",
    "CSS_TOKEN",
    "FALLBACK_INDEX",
    "PAGE_TEMPLATE_NAME",
    "SCRIPT_ASSET_NAME",
    "SCRIPT_TOKEN",
    "_FALLBACK_INDEX",
    "compose_page",
    "fallback_index_bytes",
    "page_bytes",
]
