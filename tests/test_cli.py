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

    result = runner.invoke(app, ["pull", "--phase", "3", "--source", "aact"])
    assert result.exit_code == 1
    assert "Missing AACT credentials" in result.output


def test_pull_rejects_unknown_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["pull", "--phase", "3", "--source", "bogus"])
    assert result.exit_code == 1
    assert "--source must be one of" in result.output


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


def test_vocab_validate_check_only_passes_on_shipped_vocabulary():
    result = runner.invoke(app, ["vocab", "validate", "--check-only"])
    assert result.exit_code == 0, result.output
    assert "Vocabulary valid" in result.output


def test_vocab_validate_writes_vocab_tables(tmp_path):
    import duckdb

    warehouse = tmp_path / "wh.duckdb"
    result = runner.invoke(app, ["vocab", "validate", "--warehouse", str(warehouse)])
    assert result.exit_code == 0, result.output

    con = duckdb.connect(str(warehouse))
    try:
        tables = {
            row[0]
            for row in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'vocab'"
            ).fetchall()
        }
        assert {"forms", "measurements", "synonyms", "ta_mesh_term_overrides"} <= tables
        assert con.execute("SELECT count(*) FROM vocab.forms").fetchone()[0] > 0
    finally:
        con.close()


def test_vocab_validate_reports_errors_and_writes_nothing(tmp_path):
    import shutil

    from clinical_endpoints.vocab.loader import default_vocab_dir

    broken_dir = tmp_path / "vocab"
    shutil.copytree(default_vocab_dir(__file__), broken_dir)
    forms = broken_dir / "forms.yaml"
    forms.write_text(forms.read_text().replace("  - id: time_to_event", "  - id: responder_proportion", 1))

    warehouse = tmp_path / "wh.duckdb"
    result = runner.invoke(
        app, ["vocab", "validate", "--vocab-dir", str(broken_dir), "--warehouse", str(warehouse)]
    )
    assert result.exit_code == 1
    # Rich hard-wraps console output, so compare against whitespace-collapsed text.
    output = " ".join(result.output.split())
    assert "duplicate term id" in output
    assert "nothing written" in output
    assert not warehouse.exists()


def test_vocab_validate_reports_a_missing_directory():
    result = runner.invoke(app, ["vocab", "validate", "--vocab-dir", "/nonexistent/vocab"])
    assert result.exit_code == 1
    assert "Missing vocab file" in result.output
