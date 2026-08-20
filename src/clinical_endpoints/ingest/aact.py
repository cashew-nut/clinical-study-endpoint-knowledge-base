"""Filtered pull of AACT `studies` + `design_outcomes` into raw.* (plan §2/§10 step 1),
plus the condition/intervention MeSH tables the TA resolver needs (step 2 gap 2)."""

from __future__ import annotations

import re

import duckdb

from clinical_endpoints.ingest.filters import PullFilters, normalize_phases
from clinical_endpoints.ingest.pull_log import write_pull_log
from clinical_endpoints.ingest.upsert import ensure_table

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

STUDIES_DDL = """
    nct_id VARCHAR PRIMARY KEY, phase VARCHAR, overall_status VARCHAR, study_type VARCHAR,
    start_date DATE, primary_completion_date DATE,
    brief_title VARCHAR, official_title VARCHAR
"""
DESIGN_OUTCOMES_DDL = """
    nct_id VARCHAR, outcome_type VARCHAR, measure VARCHAR,
    time_frame VARCHAR, description VARCHAR, population VARCHAR
"""
CONDITIONS_DDL = "nct_id VARCHAR, name VARCHAR"
BROWSE_CONDITIONS_DDL = (
    "nct_id VARCHAR, mesh_term VARCHAR, mesh_term_normalised VARCHAR, mesh_type VARCHAR"
)
BROWSE_INTERVENTIONS_DDL = BROWSE_CONDITIONS_DDL
MESH_TERMS_DDL = (
    "mesh_term VARCHAR, mesh_term_normalised VARCHAR, tree_number VARCHAR, "
    "PRIMARY KEY (mesh_term, tree_number)"
)


def run_pull(con: duckdb.DuckDBPyConnection, filters: PullFilters) -> dict:
    """Pull filtered studies + their design_outcomes/conditions from AACT into raw.*, log the pull.

    Upserts rather than replaces: raw.studies is updated/inserted per nct_id,
    and every child table (design_outcomes, conditions, browse_*) has its rows
    for *this pull's* nct_ids replaced with a scoped delete-then-insert --
    studies landed by earlier pulls with different filters are never touched.
    raw._pull_log accumulates one row per invocation regardless, so the pull
    history stays auditable.
    """
    aact_phases = normalize_phases(list(filters.phases))

    ensure_table(con, "studies", STUDIES_DDL)
    ensure_table(con, "design_outcomes", DESIGN_OUTCOMES_DDL)
    ensure_table(con, "conditions", CONDITIONS_DDL)
    ensure_table(con, "browse_conditions", BROWSE_CONDITIONS_DDL)
    ensure_table(con, "browse_interventions", BROWSE_INTERVENTIONS_DDL)

    since_clause = "AND s.start_date >= ?" if filters.since else ""
    params: list = [aact_phases]
    if filters.since:
        params.append(filters.since)
    params.append(filters.limit)

    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _pulled_studies AS
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
        INSERT INTO raw.studies
        SELECT * FROM _pulled_studies
        ON CONFLICT (nct_id) DO UPDATE SET
            phase = excluded.phase,
            overall_status = excluded.overall_status,
            study_type = excluded.study_type,
            start_date = excluded.start_date,
            primary_completion_date = excluded.primary_completion_date,
            brief_title = excluded.brief_title,
            official_title = excluded.official_title
        """
    )

    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE _pulled_outcomes AS
        SELECT outcomes.*
        FROM aact.ctgov.design_outcomes AS outcomes
        JOIN _pulled_studies s USING (nct_id)
        """
    )
    con.execute("DELETE FROM raw.design_outcomes WHERE nct_id IN (SELECT nct_id FROM _pulled_studies)")
    con.execute("INSERT INTO raw.design_outcomes SELECT * FROM _pulled_outcomes")

    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE _pulled_conditions AS
        SELECT c.nct_id, c.name
        FROM aact.ctgov.conditions c
        JOIN _pulled_studies s USING (nct_id)
        """
    )
    con.execute("DELETE FROM raw.conditions WHERE nct_id IN (SELECT nct_id FROM _pulled_studies)")
    con.execute("INSERT INTO raw.conditions SELECT * FROM _pulled_conditions")

    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE _pulled_browse_conditions AS
        SELECT bc.nct_id, bc.mesh_term, lower(trim(bc.mesh_term)) AS mesh_term_normalised,
               'condition' AS mesh_type
        FROM aact.ctgov.browse_conditions bc
        JOIN _pulled_studies s USING (nct_id)
        """
    )
    con.execute(
        "DELETE FROM raw.browse_conditions WHERE nct_id IN (SELECT nct_id FROM _pulled_studies)"
    )
    con.execute("INSERT INTO raw.browse_conditions SELECT * FROM _pulled_browse_conditions")

    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE _pulled_browse_interventions AS
        SELECT bi.nct_id, bi.mesh_term, lower(trim(bi.mesh_term)) AS mesh_term_normalised,
               'intervention' AS mesh_type
        FROM aact.ctgov.browse_interventions bi
        JOIN _pulled_studies s USING (nct_id)
        """
    )
    con.execute(
        "DELETE FROM raw.browse_interventions WHERE nct_id IN (SELECT nct_id FROM _pulled_studies)"
    )
    con.execute("INSERT INTO raw.browse_interventions SELECT * FROM _pulled_browse_interventions")

    has_tree_numbers, mesh_terms_count = _pull_mesh_terms(con)

    pulled_nct_ids = [row[0] for row in con.execute("SELECT nct_id FROM _pulled_studies").fetchall()]
    row_counts = {
        "studies": con.execute("SELECT count(*) FROM _pulled_studies").fetchone()[0],
        "design_outcomes": con.execute("SELECT count(*) FROM _pulled_outcomes").fetchone()[0],
        "conditions": con.execute("SELECT count(*) FROM _pulled_conditions").fetchone()[0],
        "browse_conditions": con.execute(
            "SELECT count(*) FROM _pulled_browse_conditions"
        ).fetchone()[0],
        "browse_interventions": con.execute(
            "SELECT count(*) FROM _pulled_browse_interventions"
        ).fetchone()[0],
        "mesh_terms": mesh_terms_count,
    }

    log_entry = write_pull_log(
        con,
        source=SOURCE,
        filters=filters.as_dict(),
        row_counts=row_counts,
        source_tables=SOURCE_TABLES,
    )
    return {
        **log_entry,
        "row_counts": row_counts,
        "has_mesh_tree_numbers": has_tree_numbers,
        "nct_ids": pulled_nct_ids,
    }


_TREE_NUMBER_COLUMN_RE = re.compile(r"tree.?number", re.IGNORECASE)


def _pull_mesh_terms(con: duckdb.DuckDBPyConnection) -> tuple[bool, int]:
    """Upsert raw.mesh_terms (mesh_term, mesh_term_normalised, tree_number) from
    AACT's `ctgov.mesh_terms`, if it carries a tree-number column with data.
    Returns (has_tree_numbers, rows landed by this pull).

    `ta_mesh_mapping.yaml`'s `tree_prefixes` layer was written from the MeSH
    C/F branch structure without ever being checked against a live join (this
    build sandbox is egress-blocked from AACT). Rather than hardcode a column
    name we can't verify, this introspects `ctgov.mesh_terms`'s real columns
    at pull time and adapts -- or, if there's nothing usable, ensures the
    (correctly shaped) raw.mesh_terms table exists without touching it and
    reports `has_tree_numbers=False`, so the TA resolver degrades to the
    descriptor/regex layers instead of silently joining on a made-up column.

    As of the AACT data dictionary checked for this project (2026-08-19),
    `ctgov.mesh_terms` has zero rows, so this returns False against the real
    AACT database today regardless of its column names -- see
    vocab/ta_mesh_mapping.yaml's `caveats` block.
    """
    ensure_table(con, "mesh_terms", MESH_TERMS_DDL)

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
        return False, 0

    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _pulled_mesh_terms AS
        SELECT {term_col} AS mesh_term, lower(trim({term_col})) AS mesh_term_normalised,
               {tree_col} AS tree_number
        FROM aact.ctgov.mesh_terms
        WHERE {tree_col} IS NOT NULL
        """
    )
    con.execute(
        """
        INSERT INTO raw.mesh_terms
        SELECT * FROM _pulled_mesh_terms
        ON CONFLICT (mesh_term, tree_number) DO UPDATE SET
            mesh_term_normalised = excluded.mesh_term_normalised
        """
    )
    count = con.execute("SELECT count(*) FROM _pulled_mesh_terms").fetchone()[0]
    return count > 0, count
