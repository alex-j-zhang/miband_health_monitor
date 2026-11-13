"""Compose the single-file HTML dashboard from the bundled assets.

The HTML skeleton, stylesheet and JavaScript live in ``assets/`` and are inlined
at render time, so the produced file has no external resources. The data payload
and the stage palette are injected into the two markers the JavaScript defines
(``__DATA__`` and ``__STAGE_STYLE__``). The script is split across several
files only to keep every asset under 300 lines; the pieces are concatenated with
blank lines in their original order.
"""

from __future__ import annotations

import json
from typing import Any

from miband_monitor.config.paths import package_path

from .svg import _stage_style

#: Directory holding the inlined template assets.
_ASSET_DIR = package_path("offline_dashboard", "assets")
_HTML_ASSET = "history_dashboard.html"
_CSS_ASSET = "history_dashboard.css"
_SCRIPT_ASSETS = (
    "history_dashboard.js",
    "history_charts.js",
    "history_dashboard_tables.js",
)
_CSS_MARKER = "__STYLE__"
_SCRIPT_MARKER = "__SCRIPT__"


def _read_asset(name: str) -> str:
    return (_ASSET_DIR / name).read_text(encoding="utf-8")


def _load_template() -> str:
    """Inline the CSS/JS assets into the HTML skeleton (data markers stay)."""
    script = "\n\n".join(_read_asset(name) for name in _SCRIPT_ASSETS)
    return (
        _read_asset(_HTML_ASSET)
        .replace(_CSS_MARKER, _read_asset(_CSS_ASSET))
        .replace(_SCRIPT_MARKER, script)
    )


#: Complete template with assets inlined; render() only fills in the two data
#: markers, exactly like the historical module-level ``_TEMPLATE``.
_TEMPLATE = _load_template()


def render(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return _TEMPLATE.replace("__DATA__", data).replace(
        "__STAGE_STYLE__", json.dumps(_stage_style(), ensure_ascii=False)
    )
