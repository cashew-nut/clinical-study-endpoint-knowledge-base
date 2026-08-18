from __future__ import annotations

from typer.testing import CliRunner

from clinical_endpoints.cli.main import app

runner = CliRunner()


def test_help_lists_full_planned_interface():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("pull", "vocab", "conform", "review", "graph", "query", "export"):
        assert command in result.output


def test_pull_without_credentials_fails_clearly(tmp_path, monkeypatch):
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.delenv("PGPORT", raising=False)
    monkeypatch.delenv("PGDATABASE", raising=False)
    monkeypatch.delenv("PGUSER", raising=False)
    monkeypatch.delenv("PGPASSWORD", raising=False)
    monkeypatch.setattr("clinical_endpoints.db.load_dotenv", lambda: None)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["pull", "--phase", "3"])
    assert result.exit_code == 1
    assert "Missing AACT credentials" in result.output


def test_pull_rejects_ta_filter_not_yet_supported(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["pull", "--phase", "3", "--ta", "oncology"])
    assert result.exit_code == 1
    assert "not supported yet" in result.output


def test_pull_rejects_bad_since_date(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["pull", "--phase", "3", "--since", "not-a-date"])
    assert result.exit_code == 1
    assert "YYYY-MM-DD" in result.output
