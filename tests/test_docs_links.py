"""tools/check_docs_links.py must resolve relative Markdown links."""

from __future__ import annotations

from tools import check_docs_links


def _write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_external_anchor_and_mailto_targets_are_ignored():
    assert check_docs_links.is_external("https://example.com/a.md")
    assert check_docs_links.is_external("#section")
    assert check_docs_links.is_external("mailto:dev@example.com")
    assert not check_docs_links.is_external("../docs/readme.md")


def test_fenced_code_blocks_are_not_checked():
    text = "\u0060\u0060\u0060\n[broken](missing.md)\n\u0060\u0060\u0060\n\n[good](ok.md)\n"
    assert list(check_docs_links.iter_links(text)) == ["ok.md"]


def test_valid_tree_passes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(check_docs_links, "ROOT", tmp_path)
    _write(tmp_path, "README.md", "[docs](docs/guide.md)\n")
    _write(tmp_path, "docs/guide.md", "# guide\n")
    assert check_docs_links.main([]) == 0
    assert "checked 1 local Markdown links" in capsys.readouterr().out


def test_broken_link_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(check_docs_links, "ROOT", tmp_path)
    _write(tmp_path, "README.md", "[missing](docs/missing.md)\n")
    assert check_docs_links.main([]) == 1
    out = capsys.readouterr().out
    assert "docs/missing.md" in out
    assert "broken local link" in out


def test_anchor_and_query_suffixes_still_resolve(tmp_path, monkeypatch):
    monkeypatch.setattr(check_docs_links, "ROOT", tmp_path)
    _write(tmp_path, "README.md", "[a](docs/guide.md#install)\n[b](docs/guide.md?raw=1)\n")
    _write(tmp_path, "docs/guide.md", "# guide\n")
    assert check_docs_links.main([]) == 0


def test_image_links_are_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(check_docs_links, "ROOT", tmp_path)
    _write(tmp_path, "docs/page.md", "![diagram](assets/diagram.png)\n")
    assert check_docs_links.main([]) == 1
