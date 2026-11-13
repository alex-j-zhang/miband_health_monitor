"""The live dashboard page must compose into one self-contained document."""

from __future__ import annotations

from miband_monitor.dashboard import page


def test_compose_page_inlines_css_and_javascript():
    html = page.compose_page()
    assert page.CSS_TOKEN not in html
    assert page.SCRIPT_TOKEN not in html
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")
    assert "<style" in html
    assert "<script" in html


def test_composed_page_keeps_the_live_api_markers():
    html = page.compose_page()
    assert "/api/snapshot" in html
    assert "/api/history" in html
    assert "/events" in html


def test_page_bytes_are_the_encoded_composed_page():
    assert page.page_bytes() == page.compose_page().encode("utf-8")


def test_fallback_index_keeps_the_api_discoverable():
    assert "/api/snapshot" in page.FALLBACK_INDEX
    assert "/events" in page.FALLBACK_INDEX
    assert page.fallback_index_bytes() == page.FALLBACK_INDEX.encode("utf-8")
