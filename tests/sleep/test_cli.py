"""Sleep CLI output, database loading and band-score reporting."""

from __future__ import annotations

import pytest

from sleep_analysis import STAGE_LIGHT, load_samples, main
from tests.sleep.support import _seed_database, scored, series


# ---------------------------------------------------------------------------
# database + CLI
# ---------------------------------------------------------------------------


def test_load_samples_roundtrip(tmp_path):
    db = _seed_database(tmp_path, series(90, hr=50))
    samples = load_samples(db)
    assert len(samples) == 90
    assert samples[0]["kind"] == 240
    assert all(set(item) == {"ts", "kind", "intensity", "steps", "heart_rate"} for item in samples)


def test_load_samples_missing_database_raises_and_exits(tmp_path):
    missing = tmp_path / "does_not_exist.db"
    with pytest.raises(FileNotFoundError):
        load_samples(missing)
    assert main(["--db", str(missing)]) == 1
    assert not missing.exists()


def test_cli_json_output(tmp_path, capsys):
    db = _seed_database(tmp_path, series(90, hr=50))
    assert main(["--db", str(db), "--json"]) == 0
    payload = capsys.readouterr().out
    assert '"derived": true' in payload
    assert '"sessions": 1' in payload


def test_cli_unknown_night(tmp_path, capsys):
    db = _seed_database(tmp_path, series(90, hr=50))
    assert main(["--db", str(db), "--night", "1999-01-01"]) == 1
    assert "no sleep session" in capsys.readouterr().out


def test_cli_text_report_has_hypnogram(tmp_path, capsys):
    db = _seed_database(tmp_path, series(90, hr=50))
    assert main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "hypnogram" in out
    assert "derived, not band-reported" in out
    assert STAGE_LIGHT in out


def test_cli_report_shows_band_scores(tmp_path, capsys):
    db = _seed_database(tmp_path, [scored(i, hr=50) for i in range(90)])
    assert main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "raw score bytes" in out
    assert "not stage minutes" in out
    assert "unaccounted" in out


def test_cli_report_omits_band_scores_when_absent(tmp_path, capsys):
    db = _seed_database(tmp_path, series(90, hr=50))
    assert main(["--db", str(db)]) == 0
    assert "raw score bytes" not in capsys.readouterr().out


def test_cli_says_not_worn_instead_of_inventing_a_night(tmp_path, capsys):
    db = _seed_database(tmp_path, series(240, kind=243, hr=0))
    assert main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "no sleep session detected" in out
    assert "band not worn" in out
