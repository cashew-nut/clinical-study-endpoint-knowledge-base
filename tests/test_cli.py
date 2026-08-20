from __future__ import annotations

import json

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


def test_help_lists_the_command_surface():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("pull", "vocab", "conform", "review", "ta", "usdm", "serve",
                    "query", "export"):
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

    # A later, differently-filtered pull that re-scans the same underlying
    # studies must only ever land/keep matches for --ta -- it must not prune
    # NCT002, which an earlier pull (with no --ta) already legitimately
    # landed. `pull` never discards a study landed by an earlier pull with
    # different filters (see ingest/aact.py's `run_pull` docstring); --ta is
    # no exception, so NCT002 survives even though it doesn't match this
    # pull's --ta.
    filtered = runner.invoke(
        app, ["pull", "--phase", "3", "--ta", "oncology", "--warehouse", str(warehouse)]
    )
    assert filtered.exit_code == 0, filtered.output
    assert "Filtered to --ta ['oncology']" in filtered.output
    assert "1 studies" in filtered.output  # only NCT001 matched --ta on this pull

    con = duckdb.connect(str(warehouse))
    try:
        assert con.execute("SELECT nct_id FROM raw.studies ORDER BY nct_id").fetchall() == [
            ("NCT001",),
            ("NCT002",),
        ]
        # conformed.study_therapeutic_area is recomputed from every study in
        # raw.studies each pull, regardless of --ta -- it still covers both.
        rows = con.execute(
            "SELECT nct_id, ta_id FROM conformed.study_therapeutic_area WHERE is_primary ORDER BY nct_id"
        ).fetchall()
        assert rows == [("NCT001", "oncology"), ("NCT002", "respiratory")]
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


# ------------------------------------------------------------- `endpoints usdm`


def test_usdm_show_writes_a_usdm_document(usdm_warehouse_path, tmp_path):
    out = tmp_path / "endpoints.json"
    result = runner.invoke(
        app,
        ["usdm", "show", "NCT00000001", "--warehouse", usdm_warehouse_path, "-o", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert "5 endpoint(s)" in result.output

    body = json.loads(out.read_text(encoding="utf-8"))
    assert body["usdmVersion"] == "4.0.0"
    assert sum(len(o["endpoints"]) for o in body["objectives"]) == 5
    assert '<usdm:tag name="measurement"/>' in body["objectives"][0]["endpoints"][0]["text"]


def test_usdm_show_to_stdout_and_filtered_by_level(usdm_warehouse_path):
    result = runner.invoke(
        app,
        ["usdm", "show", "NCT00000001", "--warehouse", usdm_warehouse_path, "--level", "primary"],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert len(body["objectives"]) == 1


def test_usdm_show_rejects_an_unknown_envelope(usdm_warehouse_path):
    result = runner.invoke(
        app,
        ["usdm", "show", "NCT00000001", "--warehouse", usdm_warehouse_path, "--envelope", "nope"],
    )
    assert result.exit_code == 2
    assert "--envelope must be one of" in result.output


def test_usdm_show_on_an_unpulled_trial_exits_nonzero(usdm_warehouse_path):
    result = runner.invoke(
        app, ["usdm", "show", "NCT09999999", "--warehouse", usdm_warehouse_path]
    )
    assert result.exit_code == 1
    assert "NCT09999999" in result.output


def test_usdm_coverage_reports_the_tier_mix(usdm_warehouse_path):
    result = runner.invoke(app, ["usdm", "coverage", "--warehouse", usdm_warehouse_path])
    assert result.exit_code == 0, result.output
    assert "templated" in result.output and "verbatim" in result.output


def test_usdm_coverage_reports_defaulted_tag_counts(tmp_path):
    """docs/USDM_PROJECTION_INTEGRITY_SPEC.md change 1: the tier mix alone
    cannot show how much of `templated` is standing on an announced default --
    a standalone warehouse with one reference-fallback-triggering endpoint
    ("CFB in HbA1c", no reference in the text) exercises the count end to end
    through the CLI."""
    from clinical_endpoints.conform.pipeline import run_conform
    from clinical_endpoints.db import SCHEMAS
    from clinical_endpoints.ingest.design import STUDIES_DDL
    from clinical_endpoints.ingest.pull_log import write_pull_log
    from clinical_endpoints.vocab.loader import default_vocab_dir, load_vocab, write_vocab_tables

    warehouse = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(warehouse))
    for schema in SCHEMAS:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    write_vocab_tables(con, load_vocab(default_vocab_dir()), vocab_dir=default_vocab_dir())
    con.execute(f"CREATE TABLE raw.studies ({STUDIES_DDL})")
    con.execute(
        "INSERT INTO raw.studies VALUES (" + ", ".join(["?"] * 19) + ")",
        [
            "NCT03000000", "PHASE2", "COMPLETED", "INTERVENTIONAL", "2020-01-01", "2021-01-01",
            "A diabetes trial", "A diabetes trial, officially",
            "Single Group Assignment", "Treatment", "Non-Randomized", "None", 60, "Actual",
            False, "All", "18 Years", "75 Years", "Adults with type 2 diabetes",
        ],
    )
    con.execute(
        "CREATE TABLE raw.design_outcomes (nct_id VARCHAR, outcome_type VARCHAR, measure VARCHAR, "
        "time_frame VARCHAR, description VARCHAR, population VARCHAR)"
    )
    con.execute(
        "INSERT INTO raw.design_outcomes VALUES (?, ?, ?, ?, ?, ?)",
        ("NCT03000000", "primary", "CFB in HbA1c", "Week 24", None, None),
    )
    write_pull_log(
        con, source="ctgov_api", filters={}, row_counts={"studies": 1, "design_outcomes": 1},
        source_tables=("studies", "design_outcomes"),
    )
    run_conform(con)
    con.close()

    result = runner.invoke(app, ["usdm", "coverage", "--warehouse", str(warehouse)])
    assert result.exit_code == 0, result.output
    assert "reference defaulted: 1" in result.output


def test_pull_reports_a_schema_migration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    warehouse = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(warehouse))
    con.execute("CREATE SCHEMA raw")
    con.execute(
        """
        CREATE TABLE raw.studies AS
        SELECT 'NCT_OLD' AS nct_id, 'Phase 2' AS phase, 'Completed' AS overall_status,
               'Interventional' AS study_type, DATE '2020-01-01' AS start_date,
               NULL::DATE AS primary_completion_date, 'brief' AS brief_title,
               'official' AS official_title
        """
    )
    con.close()

    monkeypatch.setattr(
        ctgov_api.requests, "get", lambda *a, **k: _FakeResponse([_make_study("NCT001", "2024-01-01")])
    )
    result = runner.invoke(app, ["pull", "--phase", "3", "--warehouse", str(warehouse)])

    assert result.exit_code == 0, result.output
    assert "Migrated raw.studies" in result.output
    assert "added PRIMARY KEY (nct_id)" in result.output
    assert "1 row preserved" in result.output


def test_pull_refuses_a_table_it_cannot_migrate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    warehouse = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(warehouse))
    con.execute("CREATE SCHEMA raw")
    con.execute("CREATE TABLE raw.studies AS SELECT 'brief' AS brief_title")
    con.close()

    monkeypatch.setattr(
        ctgov_api.requests, "get", lambda *a, **k: _FakeResponse([_make_study("NCT001", "2024-01-01")])
    )
    result = runner.invoke(app, ["pull", "--phase", "3", "--warehouse", str(warehouse)])

    assert result.exit_code == 1
    assert "no nct_id column" in result.output
    con = duckdb.connect(str(warehouse))
    try:  # refused, not silently dropped
        assert con.execute("SELECT * FROM raw.studies").fetchall() == [("brief",)]
    finally:
        con.close()
