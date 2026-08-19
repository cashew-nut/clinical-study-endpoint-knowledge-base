from __future__ import annotations

import json
from datetime import date

from clinical_endpoints.ingest.aact import run_pull
from clinical_endpoints.ingest.filters import PullFilters


def test_run_pull_lands_filtered_studies_and_outcomes(fake_aact_con):
    filters = PullFilters(phases=("3",), limit=500)
    result = run_pull(fake_aact_con, filters)

    studies = fake_aact_con.execute("SELECT nct_id FROM raw.studies ORDER BY nct_id").fetchall()
    assert [r[0] for r in studies] == ["NCT001", "NCT002"]  # PHASE1 excluded

    outcomes = fake_aact_con.execute(
        "SELECT nct_id FROM raw.design_outcomes ORDER BY nct_id"
    ).fetchall()
    assert [r[0] for r in outcomes] == ["NCT001", "NCT002"]

    assert result["row_counts"] == {
        "studies": 2,
        "design_outcomes": 2,
        "conditions": 2,
        "browse_conditions": 3,  # NCT001 has two MeSH conditions, NCT002 one; NCT003 excluded (PHASE1)
        "browse_interventions": 2,
        "mesh_terms": 0,  # AACT's mesh_terms is empty in the fake, same as the real database today
    }
    assert result["has_mesh_tree_numbers"] is False

    conditions = fake_aact_con.execute(
        "SELECT nct_id, name FROM raw.conditions ORDER BY nct_id"
    ).fetchall()
    assert conditions == [("NCT001", "Non-Small Cell Lung Cancer"), ("NCT002", "Breast Cancer")]

    browse_conditions = fake_aact_con.execute(
        "SELECT nct_id, mesh_term, mesh_term_normalised, mesh_type FROM raw.browse_conditions "
        "ORDER BY nct_id, mesh_term"
    ).fetchall()
    assert browse_conditions == [
        ("NCT001", "Carcinoma, Non-Small-Cell Lung", "carcinoma, non-small-cell lung", "condition"),
        ("NCT001", "Lung Neoplasms", "lung neoplasms", "condition"),
        ("NCT002", "Breast Neoplasms", "breast neoplasms", "condition"),
    ]

    browse_interventions = fake_aact_con.execute(
        "SELECT nct_id, mesh_term, mesh_term_normalised, mesh_type FROM raw.browse_interventions "
        "ORDER BY nct_id"
    ).fetchall()
    assert browse_interventions == [
        ("NCT001", "Pembrolizumab", "pembrolizumab", "intervention"),
        ("NCT002", "Trastuzumab", "trastuzumab", "intervention"),
    ]


def test_run_pull_respects_limit_and_since(fake_aact_con):
    filters = PullFilters(phases=("3",), limit=1, since=date(2024, 1, 1))
    result = run_pull(fake_aact_con, filters)

    # NCT002 starts 2023-01-01, excluded by `since`; limit=1 keeps only NCT001
    assert result["row_counts"]["studies"] == 1
    row = fake_aact_con.execute("SELECT nct_id FROM raw.studies").fetchone()
    assert row[0] == "NCT001"


def test_run_pull_logs_every_invocation(fake_aact_con):
    run_pull(fake_aact_con, PullFilters(phases=("3",), limit=500))
    run_pull(fake_aact_con, PullFilters(phases=("3",), limit=500))

    log_rows = fake_aact_con.execute(
        "SELECT source, filters_json, source_tables, row_counts FROM raw._pull_log ORDER BY pulled_at"
    ).fetchall()
    assert len(log_rows) == 2  # one entry per pull, history accumulates

    source, filters_json, source_tables, row_counts = log_rows[0]
    assert source == "aact"
    assert json.loads(filters_json) == {"phases": ["3"], "limit": 500, "since": None}
    assert set(source_tables) == {
        "studies",
        "design_outcomes",
        "conditions",
        "browse_conditions",
        "browse_interventions",
        "mesh_terms",
    }
    assert json.loads(row_counts) == {
        "studies": 2,
        "design_outcomes": 2,
        "conditions": 2,
        "browse_conditions": 3,
        "browse_interventions": 2,
        "mesh_terms": 0,
    }


def test_run_pull_is_idempotent_refresh_not_append(fake_aact_con):
    filters = PullFilters(phases=("3",), limit=500)
    run_pull(fake_aact_con, filters)
    run_pull(fake_aact_con, filters)

    # raw.studies is replaced wholesale, not appended to, on re-run
    count = fake_aact_con.execute("SELECT count(*) FROM raw.studies").fetchone()[0]
    assert count == 2


def test_pull_mesh_terms_picks_up_a_populated_tree_number_column(fake_aact_con):
    """If AACT's mesh_terms ever carries real tree numbers, `_pull_mesh_terms`
    should find them by introspecting the schema, not by a hardcoded column
    name -- this is the "verify before writing the query" ask from
    docs/NEXT_SESSION.md task 3, exercised against a populated fake."""
    fake_aact_con.execute("DROP TABLE aact.ctgov.mesh_terms")
    fake_aact_con.execute(
        "CREATE TABLE aact.ctgov.mesh_terms (mesh_term VARCHAR, tree_number VARCHAR)"
    )
    fake_aact_con.execute(
        """
        INSERT INTO aact.ctgov.mesh_terms VALUES
            ('Lung Neoplasms', 'C04.588.894'),
            ('Carcinoma, Non-Small-Cell Lung', 'C04.557.470.200.025'),
            ('Breast Neoplasms', 'C04.588.180'),
            ('Pulmonary Disease, Chronic Obstructive', NULL)
        """
    )

    result = run_pull(fake_aact_con, PullFilters(phases=("3",), limit=500))

    assert result["has_mesh_tree_numbers"] is True
    assert result["row_counts"]["mesh_terms"] == 3  # the NULL tree_number row is dropped

    rows = fake_aact_con.execute(
        "SELECT mesh_term, mesh_term_normalised, tree_number FROM raw.mesh_terms ORDER BY mesh_term"
    ).fetchall()
    assert ("Lung Neoplasms", "lung neoplasms", "C04.588.894") in rows
