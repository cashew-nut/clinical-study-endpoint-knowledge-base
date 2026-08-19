from __future__ import annotations

import duckdb
from typer.testing import CliRunner

from clinical_endpoints.cli.main import app
from clinical_endpoints.ingest import ctgov_api

runner = CliRunner()


def _make_study(
    nct_id, start_date, *, condition_meshes=None, intervention_meshes=None, browse_branches=None, outcomes=None
):
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id, "briefTitle": nct_id, "officialTitle": nct_id},
            "statusModule": {"overallStatus": "RECRUITING", "startDateStruct": {"date": start_date}},
            "designModule": {"phases": ["PHASE3"], "studyType": "INTERVENTIONAL"},
            "outcomesModule": outcomes or {},
            "conditionsModule": {"conditions": []},
        },
        "derivedSection": {
            "conditionBrowseModule": {
                "meshes": condition_meshes or [],
                "browseBranches": browse_branches or [],
            },
            "interventionBrowseModule": {"meshes": intervention_meshes or []},
        },
    }


class _FakeResponse:
    def __init__(self, studies):
        self.status_code = 200
        self._json = {"studies": studies}
        self.text = ""
        self.url = "https://clinicaltrials.gov/api/v2/studies?fake=1"

    def json(self):
        return self._json


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


def test_pull_rejects_ta_filter_when_vocab_not_loaded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["pull", "--phase", "3", "--ta", "oncology"])
    assert result.exit_code == 1
    assert "vocab validate" in result.output


def test_pull_ta_rejects_unknown_area(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    warehouse = tmp_path / "wh.duckdb"
    runner.invoke(app, ["vocab", "validate", "--warehouse", str(warehouse)])

    result = runner.invoke(app, ["pull", "--phase", "3", "--ta", "bogus_area", "--warehouse", str(warehouse)])
    assert result.exit_code == 1
    assert "unknown therapeutic area" in result.output


def test_pull_resolves_and_optionally_filters_by_ta(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    warehouse = tmp_path / "wh.duckdb"
    validate_result = runner.invoke(app, ["vocab", "validate", "--warehouse", str(warehouse)])
    assert validate_result.exit_code == 0, validate_result.output

    studies = [
        _make_study(
            "NCT001", "2024-01-01", condition_meshes=[{"id": "D1", "term": "Lung Neoplasms"}]
        ),
        _make_study(
            "NCT002", "2024-02-01", condition_meshes=[{"id": "D2", "term": "Asthma"}]
        ),
    ]
    monkeypatch.setattr(ctgov_api.requests, "get", lambda *a, **k: _FakeResponse(studies))

    result = runner.invoke(app, ["pull", "--phase", "3", "--warehouse", str(warehouse)])
    assert result.exit_code == 0, result.output
    assert "Therapeutic areas resolved for 2 studies" in result.output

    con = duckdb.connect(str(warehouse))
    try:
        rows = con.execute(
            "SELECT nct_id, ta_id FROM conformed.study_therapeutic_area WHERE is_primary ORDER BY nct_id"
        ).fetchall()
        assert rows == [("NCT001", "oncology"), ("NCT002", "respiratory")]
    finally:
        con.close()

    filtered = runner.invoke(
        app, ["pull", "--phase", "3", "--ta", "oncology", "--warehouse", str(warehouse)]
    )
    assert filtered.exit_code == 0, filtered.output
    assert "Filtered to --ta ['oncology']" in filtered.output

    con = duckdb.connect(str(warehouse))
    try:
        assert con.execute("SELECT nct_id FROM raw.studies").fetchall() == [("NCT001",)]
        assert con.execute("SELECT nct_id FROM conformed.study_therapeutic_area").fetchall() == [
            ("NCT001",)
        ]
    finally:
        con.close()


def test_pull_without_ta_skips_resolution_when_vocab_not_loaded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    studies = [_make_study("NCT001", "2024-01-01")]
    monkeypatch.setattr(ctgov_api.requests, "get", lambda *a, **k: _FakeResponse(studies))

    result = runner.invoke(app, ["pull", "--phase", "3"])
    assert result.exit_code == 0, result.output
    assert "Skipped therapeutic-area resolution" in result.output


def test_ta_diff_tree_requires_vocab_and_pull(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    warehouse = tmp_path / "wh.duckdb"

    result = runner.invoke(app, ["ta", "diff-tree", "--warehouse", str(warehouse)])
    assert result.exit_code == 1
    assert "vocab validate" in result.output

    runner.invoke(app, ["vocab", "validate", "--warehouse", str(warehouse)])
    result = runner.invoke(app, ["ta", "diff-tree", "--warehouse", str(warehouse)])
    assert result.exit_code == 1
    assert "pull" in result.output


def test_ta_diff_tree_reports_disagreements(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    warehouse = tmp_path / "wh.duckdb"
    runner.invoke(app, ["vocab", "validate", "--warehouse", str(warehouse)])

    # a study whose CT.gov branch letter says oncology (BC04 -> C04) but whose
    # condition descriptor reads as respiratory -- an intentional disagreement
    # to exercise the diff tool via the CT.gov backend's coarse tree signal.
    studies = [
        _make_study(
            "NCT001",
            "2024-01-01",
            condition_meshes=[{"id": "D1", "term": "Weird Asthma Subtype"}],
            browse_branches=[{"abbrev": "BC04", "name": "Neoplasms"}],
        )
    ]
    monkeypatch.setattr(ctgov_api.requests, "get", lambda *a, **k: _FakeResponse(studies))
    pull_result = runner.invoke(app, ["pull", "--phase", "3", "--warehouse", str(warehouse)])
    assert pull_result.exit_code == 0, pull_result.output

    out_path = tmp_path / "diff.csv"
    result = runner.invoke(
        app, ["ta", "diff-tree", "--warehouse", str(warehouse), "--out", str(out_path)]
    )
    assert result.exit_code == 0, result.output
    assert "Wrote 1 disagreement" in result.output
    assert out_path.exists()

    import csv

    with out_path.open() as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["ta_from_tree"] == "oncology"
    assert rows[0]["ta_from_pattern"] == "respiratory"


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


def test_conform_requires_pull_first(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    warehouse = tmp_path / "wh.duckdb"
    runner.invoke(app, ["vocab", "validate", "--warehouse", str(warehouse)])

    result = runner.invoke(app, ["conform", "--warehouse", str(warehouse)])
    assert result.exit_code == 1
    assert "pull" in result.output


def test_conform_writes_conformed_tables_and_review_list_shows_the_queue(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    warehouse = tmp_path / "wh.duckdb"
    assert runner.invoke(app, ["vocab", "validate", "--warehouse", str(warehouse)]).exit_code == 0

    studies = [
        _make_study(
            "NCT001", "2024-01-01",
            outcomes={
                "primaryOutcomes": [
                    {"measure": "Progression-Free Survival (PFS)", "timeFrame": "Event-driven"},
                    {"measure": "Zzqxv Wibble Frotz Blorpington"},
                ]
            },
        ),
    ]
    monkeypatch.setattr(ctgov_api.requests, "get", lambda *a, **k: _FakeResponse(studies))
    assert runner.invoke(app, ["pull", "--phase", "3", "--warehouse", str(warehouse)]).exit_code == 0

    result = runner.invoke(app, ["conform", "--warehouse", str(warehouse)])
    assert result.exit_code == 0, result.output
    assert "Conformed 1 of 2 row(s)" in result.output

    con = duckdb.connect(str(warehouse))
    try:
        assert con.execute(
            "SELECT form_id, measurement_id FROM conformed.endpoints WHERE nct_id = 'NCT001'"
        ).fetchall() == [("time_to_event", "tumour_burden_recist")]
        assert con.execute("SELECT count(*) FROM conformed.review_queue").fetchone()[0] == 1
    finally:
        con.close()

    review = runner.invoke(app, ["review", "list", "--warehouse", str(warehouse)])
    assert review.exit_code == 0, review.output
    assert "NCT001" in review.output
    assert "Showing 1 row(s) with status='pending'" in review.output
