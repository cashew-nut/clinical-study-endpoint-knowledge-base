"""Filtered pull of AACT `studies` + `design_outcomes` into raw.* (plan §2/§10 step 1),
plus the condition/intervention MeSH tables the TA resolver needs (step 2 gap 2)."""

from __future__ import annotations

import re

import duckdb

from clinical_endpoints.ingest.filters import PullFilters, normalize_phases
from clinical_endpoints.ingest.pull_log import write_pull_log

SOURCE = "aact"

# What this backend actually lands, for raw._pull_log.source_tables (differs from
# the CT.gov API backend: AACT can carry MeSH tree numbers via mesh_terms, the API
# backend instead lands browse_condition_branches -- see ingest/ctgov_api.py).
SOURCE_TABLES = (
    "studies",
    "design_outcomes",
    "conditions",
    "browse_conditions",
    "browse_interventions",
    "mesh_terms",
)


def run_pull(con: duckdb.DuckDBPyConnection, filters: PullFilters) -> dict:
    """Pull filtered studies + their design_outcomes/conditions from AACT into raw.*, log the pull.

    Idempotent: raw.studies / raw.design_outcomes / raw.conditions / raw.browse_* are
    replaced wholesale on each run (a refresh, not an append), while raw._pull_log
    accumulates one row per invocation -- so re-running `pull` with the same filters
    is a refresh, and the pull history stays auditable.
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

    con.execute(
        """
        CREATE OR REPLACE TABLE raw.conditions AS
        SELECT c.nct_id, c.name
        FROM aact.ctgov.conditions c
        JOIN raw.studies s USING (nct_id)
        """
    )

    con.execute(
        """
        CREATE OR REPLACE TABLE raw.browse_conditions AS
        SELECT bc.nct_id, bc.mesh_term, lower(trim(bc.mesh_term)) AS mesh_term_normalised,
               'condition' AS mesh_type
        FROM aact.ctgov.browse_conditions bc
        JOIN raw.studies s USING (nct_id)
        """
    )

    con.execute(
        """
        CREATE OR REPLACE TABLE raw.browse_interventions AS
        SELECT bi.nct_id, bi.mesh_term, lower(trim(bi.mesh_term)) AS mesh_term_normalised,
               'intervention' AS mesh_type
        FROM aact.ctgov.browse_interventions bi
        JOIN raw.studies s USING (nct_id)
        """
    )

    has_tree_numbers = _pull_mesh_terms(con)

    row_counts = {
        "studies": con.execute("SELECT count(*) FROM raw.studies").fetchone()[0],
        "design_outcomes": con.execute("SELECT count(*) FROM raw.design_outcomes").fetchone()[0],
        "conditions": con.execute("SELECT count(*) FROM raw.conditions").fetchone()[0],
        "browse_conditions": con.execute("SELECT count(*) FROM raw.browse_conditions").fetchone()[0],
        "browse_interventions": con.execute(
            "SELECT count(*) FROM raw.browse_interventions"
        ).fetchone()[0],
        "mesh_terms": con.execute("SELECT count(*) FROM raw.mesh_terms").fetchone()[0],
    }

    log_entry = write_pull_log(
        con,
        source=SOURCE,
        filters=filters.as_dict(),
        row_counts=row_counts,
        source_tables=SOURCE_TABLES,
    )
    return {**log_entry, "row_counts": row_counts, "has_mesh_tree_numbers": has_tree_numbers}


_TREE_NUMBER_COLUMN_RE = re.compile(r"tree.?number", re.IGNORECASE)


def _pull_mesh_terms(con: duckdb.DuckDBPyConnection) -> bool:
    """Land raw.mesh_terms (mesh_term, mesh_term_normalised, tree_number) from
    AACT's `ctgov.mesh_terms`, if it carries a tree-number column with data.

    `ta_mesh_mapping.yaml`'s `tree_prefixes` layer was written from the MeSH
    C/F branch structure without ever being checked against a live join (this
    build sandbox is egress-blocked from AACT). Rather than hardcode a column
    name we can't verify, this introspects `ctgov.mesh_terms`'s real columns
    at pull time and adapts -- or lands an empty (but correctly shaped)
    raw.mesh_terms and reports `has_tree_numbers=False` if there's nothing
    usable, so the TA resolver degrades to the descriptor/regex layers
    instead of silently joining on a made-up column.

    As of the AACT data dictionary checked for this project (2026-08-19),
    `ctgov.mesh_terms` has zero rows, so this returns False against the real
    AACT database today regardless of its column names -- see
    vocab/ta_mesh_mapping.yaml's `caveats` block.
    """
    columns = {
        row[0]
        for row in con.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_catalog = 'aact' AND table_schema = 'ctgov' AND table_name = 'mesh_terms'
            """
        ).fetchall()
    }

    tree_col = next((c for c in columns if _TREE_NUMBER_COLUMN_RE.search(c)), None)
    term_col = next((c for c in ("mesh_term", "term", "heading", "name") if c in columns), None)

    if not tree_col or not term_col:
        con.execute(
            """
            CREATE OR REPLACE TABLE raw.mesh_terms (
                mesh_term VARCHAR, mesh_term_normalised VARCHAR, tree_number VARCHAR
            )
            """
        )
        return False

    con.execute(
        f"""
        CREATE OR REPLACE TABLE raw.mesh_terms AS
        SELECT {term_col} AS mesh_term, lower(trim({term_col})) AS mesh_term_normalised,
               {tree_col} AS tree_number
        FROM aact.ctgov.mesh_terms
        WHERE {tree_col} IS NOT NULL
        """
    )
    return con.execute("SELECT count(*) FROM raw.mesh_terms").fetchone()[0] > 0
