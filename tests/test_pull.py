from __future__ import annotations

import json
from datetime import date

import pytest

from clinical_endpoints.ingest.pull import PHASE_ALIASES, PullFilters, normalize_phases, run_pull


def test_normalize_phases_maps_shorthand_to_aact_values():
    assert normalize_phases(["3"]) == ["PHASE3"]
    assert normalize_phases(["1/2", "3"]) == ["PHASE1/PHASE2", "PHASE3"]


def test_normalize_phases_rejects_unknown_value():
    with pytest.raises(ValueError, match="Unrecognized phase"):
        normalize_phases(["5"])


def test_normalize_phases_covers_all_declared_aliases():
    # every alias should round-trip without raising
    assert normalize_phases(list(PHASE_ALIASES)) == list(PHASE_ALIASES.values())


def test_run_pull_lands_filtered_studies_and_outcomes(fake_aact_con):
    filters = PullFilters(phases=("3",), limit=500)
    result = run_pull(fake_aact_con, filters)

    studies = fake_aact_con.execute("SELECT nct_id FROM raw.studies ORDER BY nct_id").fetchall()
    assert [r[0] for r in studies] == ["NCT001", "NCT002"]  # PHASE1 excluded

    outcomes = fake_aact_con.execute(
        "SELECT nct_id FROM raw.design_outcomes ORDER BY nct_id"
    ).fetchall()
    assert [r[0] for r in outcomes] == ["NCT001", "NCT002"]

    assert result["row_counts"] == {"studies": 2, "design_outcomes": 2}


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
        "SELECT filters_json, source_tables, row_counts FROM raw._pull_log ORDER BY pulled_at"
    ).fetchall()
    assert len(log_rows) == 2  # one entry per pull, history accumulates

    filters_json, source_tables, row_counts = log_rows[0]
    assert json.loads(filters_json) == {"phases": ["3"], "limit": 500, "since": None}
    assert set(source_tables) == {"studies", "design_outcomes"}
    assert json.loads(row_counts) == {"studies": 2, "design_outcomes": 2}


def test_run_pull_is_idempotent_refresh_not_append(fake_aact_con):
    filters = PullFilters(phases=("3",), limit=500)
    run_pull(fake_aact_con, filters)
    run_pull(fake_aact_con, filters)

    # raw.studies is replaced wholesale, not appended to, on re-run
    count = fake_aact_con.execute("SELECT count(*) FROM raw.studies").fetchone()[0]
    assert count == 2
