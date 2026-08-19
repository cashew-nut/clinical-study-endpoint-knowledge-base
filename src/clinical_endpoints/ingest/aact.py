"""Filtered pull of AACT `studies` + `design_outcomes` into raw.* (plan §2/§10 step 1)."""

from __future__ import annotations

import duckdb

from clinical_endpoints.ingest.filters import PullFilters, normalize_phases
from clinical_endpoints.ingest.pull_log import write_pull_log

SOURCE = "aact"


def run_pull(con: duckdb.DuckDBPyConnection, filters: PullFilters) -> dict:
    """Pull filtered studies + their design_outcomes from AACT into raw.*, log the pull.

    Idempotent: raw.studies / raw.design_outcomes are replaced wholesale on each
    run (a refresh, not an append), while raw._pull_log accumulates one row per
    invocation -- so re-running `pull` with the same filters is a refresh, and
    the pull history stays auditable.
    """
    aact_phases = normalize_phases(list(filters.phases))

    since_clause = "AND s.start_date >= ?" if filters.since else ""
    params: list = [aact_phases]
    if filters.since:
        params.append(filters.since)
    params.append(filters.limit)

    con.execute(
        f"""
        CREATE OR REPLACE TABLE raw.studies AS
        SELECT
            s.nct_id, s.phase, s.overall_status, s.study_type,
            s.start_date, s.primary_completion_date,
            s.brief_title, s.official_title
        FROM aact.ctgov.studies s
        WHERE s.phase = ANY(?)
        {since_clause}
        ORDER BY s.start_date DESC
        LIMIT ?
        """,
        params,
    )

    con.execute(
        """
        CREATE OR REPLACE TABLE raw.design_outcomes AS
        SELECT outcomes.*
        FROM aact.ctgov.design_outcomes AS outcomes
        JOIN raw.studies s USING (nct_id)
        """
    )

    row_counts = {
        "studies": con.execute("SELECT count(*) FROM raw.studies").fetchone()[0],
        "design_outcomes": con.execute("SELECT count(*) FROM raw.design_outcomes").fetchone()[0],
    }

    log_entry = write_pull_log(con, source=SOURCE, filters=filters.as_dict(), row_counts=row_counts)
    return {**log_entry, "row_counts": row_counts}
