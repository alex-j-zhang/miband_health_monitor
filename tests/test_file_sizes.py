"""tools/check_file_sizes.py must enforce the first-party line budget."""

from __future__ import annotations

from pathlib import Path

from tools import check_file_sizes


def _write_lines(path: Path, count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join("# filler" for _ in range(count)) + "\n", encoding="utf-8")


def _budget_root(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(check_file_sizes, "ROOT", tmp_path)
    return tmp_path


def test_a_file_at_the_limit_passes(tmp_path, monkeypatch, capsys):
    root = _budget_root(tmp_path, monkeypatch)
    _write_lines(root / "ok.py", check_file_sizes.MAX_LINES)
    assert check_file_sizes.main([]) == 0
    assert "within budget" in capsys.readouterr().out


def test_a_file_over_the_limit_fails(tmp_path, monkeypatch, capsys):
    root = _budget_root(tmp_path, monkeypatch)
    _write_lines(root / "too_big.py", check_file_sizes.MAX_LINES + 1)
    assert check_file_sizes.main([]) == 1
    out = capsys.readouterr().out
    assert "too_big.py" in out
    assert "exceed" in out


def test_excluded_directories_and_suffixes_are_skipped(tmp_path):
    _write_lines(tmp_path / "vendor" / "big.py", check_file_sizes.MAX_LINES + 100)
    _write_lines(tmp_path / "huami_token" / "big.py", check_file_sizes.MAX_LINES + 100)
    _write_lines(tmp_path / "__pycache__" / "big.py", check_file_sizes.MAX_LINES + 100)
    _write_lines(tmp_path / "notes.txt", check_file_sizes.MAX_LINES + 100)
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

    found = {
        path.relative_to(tmp_path).as_posix()
        for path in check_file_sizes.iter_code_files(tmp_path)
    }
    assert found == {"app.py"}


def test_line_count_ignores_the_trailing_newline(tmp_path):
    path = tmp_path / "lines.py"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    assert check_file_sizes.line_count(path) == 3


def test_repository_tests_respect_the_budget():
    tests_dir = Path(check_file_sizes.ROOT) / "tests"
    oversized = [
        path.relative_to(check_file_sizes.ROOT).as_posix()
        for path in tests_dir.rglob("*.py")
        if check_file_sizes.line_count(path) > check_file_sizes.MAX_LINES
    ]
    assert oversized == []
