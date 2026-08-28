from __future__ import annotations

import json
from datetime import date

import pytest

from clinical_endpoints.ingest import aact as aact_backend
from clinical_endpoints.ingest.aact import run_pull
from clinical_endpoints.ingest.filters import PullFilters
from clinical_endpoints.vocab.loader import default_vocab_dir, load_vocab, write_vocab_tables


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
        "design_groups": 3,  # NCT001 has two arms, NCT002 one; NCT003 excluded (PHASE1)
        "conditions": 2,
        "browse_conditions": 3,  # NCT001 has two MeSH conditions, NCT002 one; NCT003 excluded (PHASE1)
        "browse_interventions": 2,
        "mesh_terms": 0,  # AACT's mesh_terms is empty in the fake, same as the real database today
        # The results section (D4), landed by default. Only NCT001 posted one.
        "outcome_measures": 2,
        "outcome_groups": 4,
        "outcome_measurements": 4,
        "outcome_analyses": 2,
        "baseline_measurements": 1,
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
        "design_groups",
        "conditions",
        "browse_conditions",
        "browse_interventions",
        "mesh_terms",
        "outcome_measures",
        "outcome_groups",
        "outcome_measurements",
        "outcome_analyses",
        "baseline_measurements",
    }
    assert json.loads(row_counts) == {
        "studies": 2,
        "design_outcomes": 2,
        "design_groups": 3,
        "conditions": 2,
        "browse_conditions": 3,
        "browse_interventions": 2,
        "mesh_terms": 0,
        "outcome_measures": 2,
        "outcome_groups": 4,
        "outcome_measurements": 4,
        "outcome_analyses": 2,
        "baseline_measurements": 1,
    }


def test_run_pull_is_idempotent_refresh_not_append(fake_aact_con):
    filters = PullFilters(phases=("3",), limit=500)
    run_pull(fake_aact_con, filters)
    run_pull(fake_aact_con, filters)

    # re-running with the same filters upserts (updates) the same nct_ids
    # rather than duplicating them
    count = fake_aact_con.execute("SELECT count(*) FROM raw.studies").fetchone()[0]
    assert count == 2


def test_run_pull_upsert_preserves_studies_from_earlier_pulls_with_different_filters(fake_aact_con):
    """The bug this guards against: a pull used to `CREATE OR REPLACE TABLE`
    every raw.* table wholesale, so a second pull with different filters wiped
    out everything the first pull landed. `pull` must upsert instead --
    updating/inserting the newly-pulled studies without dropping studies a
    previous, differently-filtered pull already landed."""
    run_pull(fake_aact_con, PullFilters(phases=("3",), limit=500))  # lands NCT001, NCT002
    run_pull(fake_aact_con, PullFilters(phases=("1",), limit=500))  # lands NCT003

    nct_ids = {r[0] for r in fake_aact_con.execute("SELECT nct_id FROM raw.studies").fetchall()}
    assert nct_ids == {"NCT001", "NCT002", "NCT003"}

    outcome_nct_ids = {
        r[0] for r in fake_aact_con.execute("SELECT nct_id FROM raw.design_outcomes").fetchall()
    }
    assert outcome_nct_ids == {"NCT001", "NCT002", "NCT003"}

    condition_nct_ids = {
        r[0] for r in fake_aact_con.execute("SELECT nct_id FROM raw.conditions").fetchall()
    }
    assert condition_nct_ids == {"NCT001", "NCT002", "NCT003"}


def test_run_pull_updates_existing_study_fields_on_rerun(fake_aact_con):
    """The "update existing" half of upsert: a study re-pulled with fresher
    source data should have its raw.studies row updated in place, not left
    stale and not duplicated."""
    run_pull(fake_aact_con, PullFilters(phases=("3",), limit=500))
    fake_aact_con.execute(
        "UPDATE aact.ctgov.studies SET overall_status = 'COMPLETED' WHERE nct_id = 'NCT002'"
    )
    run_pull(fake_aact_con, PullFilters(phases=("3",), limit=500))

    row = fake_aact_con.execute(
        "SELECT overall_status FROM raw.studies WHERE nct_id = 'NCT002'"
    ).fetchone()
    assert row[0] == "COMPLETED"
    count = fake_aact_con.execute(
        "SELECT count(*) FROM raw.studies WHERE nct_id = 'NCT002'"
    ).fetchone()[0]
    assert count == 1  # updated in place, not duplicated


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


def test_run_pull_lands_design_and_eligibility_columns(fake_aact_con):
    """The study-level facts the USDM projection needs, per CDISC's
    ct-gov_mapping.xlsx: a valid USDM Wrapper requires
    StudyDesignPopulation.includesHealthySubjects and
    InterventionalStudyDesign.model, and neither used to be collected."""
    run_pull(fake_aact_con, PullFilters(phases=("3",), limit=500, since=None))

    rows = fake_aact_con.execute(
        """
        SELECT nct_id, intervention_model, primary_purpose, allocation, masking,
               enrollment_count, enrollment_type, healthy_volunteers, gender,
               minimum_age, maximum_age, population_description
        FROM raw.studies ORDER BY nct_id
        """
    ).fetchall()
    assert rows[0] == (
        "NCT001", "Parallel Assignment", "Treatment", "Randomized", "Double",
        480, "Actual", False, "All", "18 Years", "75 Years", "Adults with advanced NSCLC",
    )
    # AACT writes healthy_volunteers as free text; "Accepts Healthy Volunteers"
    # normalises to True, and a missing maximum_age stays NULL.
    assert rows[1][7] is True
    assert rows[1][10] is None


def test_run_pull_lands_the_lead_organization_not_a_collaborator(fake_aact_con):
    run_pull(fake_aact_con, PullFilters(phases=("3",), limit=500))

    rows = fake_aact_con.execute(
        "SELECT nct_id, organization FROM raw.studies ORDER BY nct_id"
    ).fetchall()
    assert rows == [
        # NCT001's collaborator, "National Cancer Institute", must not win.
        ("NCT001", "Merck Sharp & Dohme LLC"),
        ("NCT002", "Genentech, Inc."),
    ]


def test_run_pull_lands_arms_into_design_groups(fake_aact_con):
    run_pull(fake_aact_con, PullFilters(phases=("3",), limit=500, since=None))
    assert fake_aact_con.execute(
        "SELECT nct_id, group_type, title FROM raw.design_groups ORDER BY nct_id, title"
    ).fetchall() == [
        ("NCT001", "Active Comparator", "Chemotherapy"),
        ("NCT001", "Experimental", "Pembrolizumab"),
        ("NCT002", "Experimental", "Trastuzumab"),
    ]


def test_run_pull_migrates_a_warehouse_left_by_the_pre_upsert_release(fake_aact_con):
    """The reported failure, end to end: a raw.studies created by the release
    that used `CREATE OR REPLACE TABLE ... AS SELECT` has no PRIMARY KEY, so the
    upsert's ON CONFLICT could not bind against it."""
    fake_aact_con.execute(
        """
        CREATE TABLE raw.studies AS
        SELECT 'NCT_OLD' AS nct_id, 'Phase 2' AS phase, 'Completed' AS overall_status,
               'Interventional' AS study_type, DATE '2020-01-01' AS start_date,
               NULL::DATE AS primary_completion_date,
               'landed by an earlier pull' AS brief_title, 'official' AS official_title
        """
    )

    result = run_pull(fake_aact_con, PullFilters(phases=("3",), limit=500))

    assert [c.table for c in result["migrations"]] == ["studies"]
    assert result["migrations"][0].added_key == ("nct_id",)
    # The point of migrating rather than refreshing: the earlier pull survives.
    assert [
        r[0]
        for r in fake_aact_con.execute("SELECT nct_id FROM raw.studies ORDER BY nct_id").fetchall()
    ] == ["NCT001", "NCT002", "NCT_OLD"]


def test_run_pull_needs_no_migration_on_a_warehouse_it_built_itself(fake_aact_con):
    run_pull(fake_aact_con, PullFilters(phases=("3",), limit=500))
    assert run_pull(fake_aact_con, PullFilters(phases=("3",), limit=500))["migrations"] == []


# --------------------------------------------------------------- --ta filtering
#
# Mirrors ingest/ctgov_api.py's --ta tests: --ta must filter *before* `limit`
# is applied, scanning past non-matching studies rather than truncating to
# the most recent `limit` studies of any therapeutic area and only then
# discarding the ones that don't match.


@pytest.fixture
def ta_fake_aact_con(fake_aact_con):
    """`fake_aact_con` plus the real shipped MeSH -> TA mapping loaded, the
    precondition `--ta` filtering requires (the CLI enforces the same thing
    before ever calling `run_pull`). NCT001/NCT002 (both PHASE3, in the base
    fixture) resolve to oncology; NCT003 is PHASE1 and never in scope here."""
    vocab_dir = default_vocab_dir(__file__)
    write_vocab_tables(fake_aact_con, load_vocab(vocab_dir), vocab_dir=vocab_dir)
    return fake_aact_con


def test_run_pull_ta_filter_excludes_non_matching_area(ta_fake_aact_con):
    result = run_pull(ta_fake_aact_con, PullFilters(phases=("3",), limit=500, ta=("respiratory",)))

    assert result["nct_ids"] == []
    assert ta_fake_aact_con.execute("SELECT count(*) FROM raw.studies").fetchone()[0] == 0


def test_run_pull_ta_filter_keeps_matching_studies(ta_fake_aact_con):
    result = run_pull(ta_fake_aact_con, PullFilters(phases=("3",), limit=500, ta=("oncology",)))

    assert set(result["nct_ids"]) == {"NCT001", "NCT002"}
    landed = {r[0] for r in ta_fake_aact_con.execute("SELECT nct_id FROM raw.studies").fetchall()}
    assert landed == {"NCT001", "NCT002"}


def test_run_pull_ta_filter_scans_past_non_matching_batches(ta_fake_aact_con, monkeypatch):
    """NCT001/NCT002 (most recent) are both oncology; the one respiratory
    study is added last, so it's the oldest. A batch size of 1 forces
    multiple round-trips -- `run_pull` must keep scanning past the
    non-matching batches to find it, the same way ctgov_api paginates past
    non-matching pages."""
    monkeypatch.setattr(aact_backend, "TA_BATCH_SIZE", 1)

    ta_fake_aact_con.execute(
        "INSERT INTO aact.ctgov.studies VALUES "
        "('NCT004', 'PHASE3', 'RECRUITING', 'INTERVENTIONAL', '2022-01-01', NULL, "
        "'Trial D', 'Trial D official', NULL, NULL, NULL)"
    )
    ta_fake_aact_con.execute("INSERT INTO aact.ctgov.browse_conditions VALUES ('NCT004', 'Asthma')")

    result = run_pull(ta_fake_aact_con, PullFilters(phases=("3",), limit=1, ta=("respiratory",)))

    assert result["nct_ids"] == ["NCT004"]
    landed = {r[0] for r in ta_fake_aact_con.execute("SELECT nct_id FROM raw.studies").fetchall()}
    assert landed == {"NCT004"}  # NCT001/NCT002 (oncology) were scanned but never landed
    assert result["studies_scanned"] == 3
    assert result["hit_scan_cap"] is False


def test_run_pull_ta_filter_reports_hit_scan_cap_when_capped_before_a_match(ta_fake_aact_con, monkeypatch):
    monkeypatch.setattr(aact_backend, "TA_BATCH_SIZE", 1)
    monkeypatch.setattr(aact_backend, "TA_MAX_SCANNED", 1)

    result = run_pull(ta_fake_aact_con, PullFilters(phases=("3",), limit=1, ta=("respiratory",)))

    assert result["nct_ids"] == []
    assert result["studies_scanned"] == 1  # only the most recent candidate (NCT001) was scanned
    assert result["hit_scan_cap"] is True


def test_run_pull_ta_filter_since_still_applies(ta_fake_aact_con):
    """--since must still narrow the candidate pool --ta scans, exactly as it
    does without --ta."""
    result = run_pull(
        ta_fake_aact_con,
        PullFilters(phases=("3",), limit=500, since=date(2024, 1, 1), ta=("oncology",)),
    )

    # NCT002 starts 2023-01-01, excluded by --since before --ta even applies.
    assert result["nct_ids"] == ["NCT001"]


def test_run_pull_ta_filter_preserves_earlier_pulls_with_different_filters(ta_fake_aact_con):
    """The same upsert invariant every other pull honours: a study another
    pull landed with different filters is never touched, even if it doesn't
    match this pull's --ta."""
    run_pull(ta_fake_aact_con, PullFilters(phases=("3",), limit=500))  # lands NCT001, NCT002, no --ta

    run_pull(ta_fake_aact_con, PullFilters(phases=("3",), limit=500, ta=("respiratory",)))  # matches neither

    nct_ids = {r[0] for r in ta_fake_aact_con.execute("SELECT nct_id FROM raw.studies").fetchall()}
    assert nct_ids == {"NCT001", "NCT002"}  # untouched by the second, non-matching pull


# --------------------------------------------------------------- --org filtering


def test_run_pull_org_filter_keeps_matching_studies(fake_aact_con):
    result = run_pull(fake_aact_con, PullFilters(phases=("3",), limit=500, org=("Merck",)))

    assert result["nct_ids"] == ["NCT001"]
    landed = {r[0] for r in fake_aact_con.execute("SELECT nct_id FROM raw.studies").fetchall()}
    assert landed == {"NCT001"}


def test_run_pull_org_filter_excludes_collaborator_only_match(fake_aact_con):
    """NCT001's collaborator is "National Cancer Institute" -- --org must match
    the lead sponsor only (the same distinction the CT.gov API backend's
    AREA[LeadSponsorName] draws), so a fragment only the collaborator carries
    matches nothing."""
    result = run_pull(
        fake_aact_con, PullFilters(phases=("3",), limit=500, org=("National Cancer Institute",))
    )

    assert result["nct_ids"] == []


def test_run_pull_org_filter_is_case_insensitive_substring(fake_aact_con):
    result = run_pull(fake_aact_con, PullFilters(phases=("3",), limit=500, org=("genentech",)))

    assert result["nct_ids"] == ["NCT002"]


def test_run_pull_org_filter_keeps_studies_matching_any_requested_org(fake_aact_con):
    result = run_pull(
        fake_aact_con, PullFilters(phases=("3",), limit=500, org=("Merck", "Genentech"))
    )

    assert set(result["nct_ids"]) == {"NCT001", "NCT002"}


def test_run_pull_org_filter_since_still_applies(fake_aact_con):
    result = run_pull(
        fake_aact_con,
        PullFilters(phases=("3",), limit=500, since=date(2024, 1, 1), org=("Merck", "Genentech")),
    )

    # NCT002 starts 2023-01-01, excluded by --since before --org even applies.
    assert result["nct_ids"] == ["NCT001"]


def test_run_pull_org_filter_combines_with_ta(ta_fake_aact_con):
    """--org and --ta compose (AND), rather than one silently overriding the
    other -- NCT001/NCT002 are both oncology (per ta_fake_aact_con's fixture),
    so --ta oncology alone keeps both; adding --org Merck narrows to NCT001."""
    result = run_pull(
        ta_fake_aact_con,
        PullFilters(phases=("3",), limit=500, ta=("oncology",), org=("Merck",)),
    )

    assert result["nct_ids"] == ["NCT001"]


# --------------------------------------------------------------- --replace


def test_run_pull_replace_discards_studies_from_an_earlier_differently_filtered_pull(fake_aact_con):
    """The inverse of
    test_run_pull_upsert_preserves_studies_from_earlier_pulls_with_different_filters:
    --replace is the explicit opt-out of that guarantee."""
    run_pull(fake_aact_con, PullFilters(phases=("3",), limit=500))  # lands NCT001, NCT002

    run_pull(fake_aact_con, PullFilters(phases=("1",), limit=500, replace=True))  # lands NCT003 only

    nct_ids = {r[0] for r in fake_aact_con.execute("SELECT nct_id FROM raw.studies").fetchall()}
    assert nct_ids == {"NCT003"}  # NCT001/NCT002, from the earlier pull, are gone


def test_run_pull_replace_also_empties_child_tables_from_earlier_pulls(fake_aact_con):
    run_pull(fake_aact_con, PullFilters(phases=("3",), limit=500))
    assert fake_aact_con.execute("SELECT count(*) FROM raw.design_outcomes").fetchone()[0] == 2
    assert fake_aact_con.execute("SELECT count(*) FROM raw.conditions").fetchone()[0] == 2

    run_pull(fake_aact_con, PullFilters(phases=("1",), limit=500, replace=True))

    # NCT001/NCT002's outcome/condition rows are gone too -- not just the studies.
    assert fake_aact_con.execute("SELECT nct_id FROM raw.design_outcomes").fetchall() == [
        ("NCT003",)
    ]
    assert fake_aact_con.execute("SELECT nct_id FROM raw.conditions").fetchall() == [("NCT003",)]


def test_run_pull_replace_reports_no_migrations(fake_aact_con):
    """A replace on a pre-existing, differently-shaped raw.studies must not be
    reported as a migration -- it's a deliberate wipe, not a reconciliation."""
    fake_aact_con.execute(
        """
        CREATE TABLE raw.studies AS
        SELECT 'NCT_OLD' AS nct_id, 'Phase 2' AS phase, 'Completed' AS overall_status,
               'Interventional' AS study_type, DATE '2020-01-01' AS start_date,
               NULL::DATE AS primary_completion_date,
               'old' AS brief_title, 'official' AS official_title
        """
    )

    result = run_pull(fake_aact_con, PullFilters(phases=("3",), limit=500, replace=True))

    assert result["migrations"] == []
    nct_ids = {r[0] for r in fake_aact_con.execute("SELECT nct_id FROM raw.studies").fetchall()}
    assert nct_ids == {"NCT001", "NCT002"}  # NCT_OLD is gone, not migrated in alongside them
