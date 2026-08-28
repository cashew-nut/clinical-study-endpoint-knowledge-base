"""Filtered pull of AACT `studies` + `design_outcomes` into raw.* (plan §2/§10 step 1),
plus the condition/intervention MeSH tables the TA resolver needs (step 2 gap 2)."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Callable, Optional

import duckdb

from clinical_endpoints.ingest.design import (
    DESIGN_GROUPS_COLUMNS,
    DESIGN_GROUPS_DDL,
    STUDY_COLUMNS,
)
from clinical_endpoints.ingest.design import STUDIES_DDL as SHARED_STUDIES_DDL
from clinical_endpoints.ingest.filters import PullFilters, normalize_phases
from clinical_endpoints.ingest.pull_log import write_pull_log
from clinical_endpoints.ingest.upsert import SchemaReconciler
from clinical_endpoints.ta.resolver import TaMapping, load_ta_mapping, resolve_study_ta_matches

SOURCE = "aact"

# What this backend actually lands, for raw._pull_log.source_tables (differs from
# the CT.gov API backend: AACT can carry MeSH tree numbers via mesh_terms, the API
# backend instead lands browse_condition_branches -- see ingest/ctgov_api.py).
SOURCE_TABLES = (
    "studies",
    "design_outcomes",
    "design_groups",
    "conditions",
    "browse_conditions",
    "browse_interventions",
    "mesh_terms",
)

# Shared with the ctgov_api backend so both land the same shape -- see
# ingest/design.py for the CDISC ct-gov_mapping.xlsx rows these columns serve.
STUDIES_DDL = SHARED_STUDIES_DDL
DESIGN_OUTCOMES_DDL = """
    nct_id VARCHAR, outcome_type VARCHAR, measure VARCHAR,
    time_frame VARCHAR, description VARCHAR, population VARCHAR
"""
CONDITIONS_DDL = "nct_id VARCHAR, name VARCHAR"
BROWSE_CONDITIONS_DDL = (
    "nct_id VARCHAR, mesh_term VARCHAR, mesh_term_normalised VARCHAR, mesh_type VARCHAR"
)
BROWSE_INTERVENTIONS_DDL = BROWSE_CONDITIONS_DDL

# Every INSERT below names these rather than relying on `SELECT *`, which binds
# by position: AACT is an upstream database whose tables carry columns this
# project doesn't model (ctgov.design_outcomes leads with its own `id`), and a
# positional insert makes the pull depend on that column list never changing.
DESIGN_OUTCOMES_COLUMNS = (
    "nct_id", "outcome_type", "measure", "time_frame", "description", "population",
)
CONDITIONS_COLUMNS = ("nct_id", "name")
BROWSE_COLUMNS = ("nct_id", "mesh_term", "mesh_term_normalised", "mesh_type")
MESH_TERMS_DDL = (
    "mesh_term VARCHAR, mesh_term_normalised VARCHAR, tree_number VARCHAR, "
    "PRIMARY KEY (mesh_term, tree_number)"
)

# The major landing steps `run_pull` reports progress against, in the order it
# performs them -- everything here is a single (fast, DuckDB-side) SQL
# statement, so this is a coarse "which table are we on" indicator rather than
# a fine-grained percentage.
PULL_STEPS = (
    "studies",
    "design_outcomes",
    "design_groups",
    "conditions",
    "browse_conditions",
    "browse_interventions",
    "mesh_terms",
)

# AACT has no server-side notion of this project's therapeutic areas either
# (see ingest/ctgov_api.py's MAX_PAGES_TA_FILTERED docstring for why --ta
# can't just be another SQL predicate alongside phase/since): `--ta` is
# resolved the same layered way `ta/resolver.py` does, batch by batch, most
# recent first, until `filters.limit` matches are found. TA_BATCH_SIZE is the
# candidate page size per round-trip; TA_MAX_SCANNED bounds how many
# phase/since-matching candidates get scanned in total before giving up.
TA_BATCH_SIZE = 500
TA_MAX_SCANNED = 30000

_PULLED_STUDIES_SELECT = """
    SELECT
        s.nct_id, s.phase, s.overall_status, s.study_type,
        s.start_date, s.primary_completion_date,
        s.brief_title, s.official_title,
        d.intervention_model, d.primary_purpose, d.allocation, d.masking,
        TRY_CAST(s.enrollment AS INTEGER) AS enrollment_count,
        s.enrollment_type,
        -- AACT stores this as free text ("Accepts Healthy Volunteers" / "No");
        -- anything else stays NULL rather than guessing, because a wrong
        -- includesHealthySubjects is a clinical claim, not a formatting slip.
        CASE lower(trim(e.healthy_volunteers))
            WHEN 'accepts healthy volunteers' THEN TRUE
            WHEN 'yes' THEN TRUE
            WHEN 'no' THEN FALSE
            ELSE NULL
        END AS healthy_volunteers,
        e.gender, e.minimum_age, e.maximum_age,
        e.population AS population_description,
        sp.organization
    FROM aact.ctgov.studies s
    LEFT JOIN aact.ctgov.designs d ON d.nct_id = s.nct_id
    LEFT JOIN aact.ctgov.eligibilities e ON e.nct_id = s.nct_id
    -- ctgov.sponsors carries one row per (nct_id, lead-or-collaborator); the
    -- GROUP BY/MIN collapses it to the one lead sponsor this project cares
    -- about (never a collaborator) and guards against a study somehow having
    -- more than one 'lead' row fanning this join out into duplicate study rows.
    LEFT JOIN (
        SELECT nct_id, MIN(name) AS organization
        FROM aact.ctgov.sponsors
        WHERE lower(lead_or_collaborator) = 'lead'
        GROUP BY nct_id
    ) sp ON sp.nct_id = s.nct_id
"""


def run_pull(
    con: duckdb.DuckDBPyConnection,
    filters: PullFilters,
    on_step: Optional[Callable[[str, int, int], None]] = None,
) -> dict:
    """Pull filtered studies + their design_outcomes/conditions from AACT into raw.*, log the pull.

    Upserts rather than replaces: raw.studies is updated/inserted per nct_id,
    and every child table (design_outcomes, conditions, browse_*) has its rows
    for *this pull's* nct_ids replaced with a scoped delete-then-insert --
    studies landed by earlier pulls with different filters are never touched.
    `filters.replace` overrides this -- see `ingest/upsert.py`'s `ensure_table`.
    raw._pull_log accumulates one row per invocation regardless, so the pull
    history stays auditable.

    `filters.org`, if given, filters to studies whose *lead* sponsor (never a
    collaborator) matches one of the given fragments, case-insensitively --
    applied as a plain SQL predicate (`_org_filter_sql`) alongside phase/since,
    in both branches below. Unlike `ta`, AACT (like the CT.gov API) can express
    this directly in SQL, so it needs no batch-scan machinery of its own.

    `on_step`, if given, is called as `on_step(step_name, index, total)` right
    after each of PULL_STEPS lands.
    """

    def _step(name: str) -> None:
        if on_step:
            on_step(name, PULL_STEPS.index(name) + 1, len(PULL_STEPS))

    aact_phases = normalize_phases(list(filters.phases))

    schema = SchemaReconciler(con, replace=filters.replace)
    schema.ensure("studies", STUDIES_DDL)
    schema.ensure("design_outcomes", DESIGN_OUTCOMES_DDL)
    schema.ensure("design_groups", DESIGN_GROUPS_DDL)
    schema.ensure("conditions", CONDITIONS_DDL)
    schema.ensure("browse_conditions", BROWSE_CONDITIONS_DDL)
    schema.ensure("browse_interventions", BROWSE_INTERVENTIONS_DDL)
    schema.ensure("mesh_terms", MESH_TERMS_DDL)

    ta_scanned: Optional[int] = None
    ta_hit_scan_cap = False

    if filters.ta:
        matched_nct_ids, ta_scanned, ta_hit_scan_cap = _ta_filtered_nct_ids(con, aact_phases, filters)
        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE _pulled_studies AS
            {_PULLED_STUDIES_SELECT}
            WHERE s.nct_id = ANY(?)
            ORDER BY s.start_date DESC
            """,
            [matched_nct_ids],
        )
    else:
        since_clause = "AND s.start_date >= ?" if filters.since else ""
        org_clause, org_params = _org_filter_sql(filters, nct_id_column="s.nct_id")
        params: list = [aact_phases]
        if filters.since:
            params.append(filters.since)
        params += org_params
        params.append(filters.limit)

        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE _pulled_studies AS
            {_PULLED_STUDIES_SELECT}
            WHERE s.phase = ANY(?)
            {since_clause}
            {org_clause}
            ORDER BY s.start_date DESC
            LIMIT ?
            """,
            params,
        )

    con.execute(
        """
        INSERT INTO raw.studies (""" + ", ".join(STUDY_COLUMNS) + """)
        SELECT """ + ", ".join(STUDY_COLUMNS) + """ FROM _pulled_studies
        ON CONFLICT (nct_id) DO UPDATE SET
        """
        + ",\n            ".join(
            f"{column} = excluded.{column}" for column in STUDY_COLUMNS if column != "nct_id"
        )
    )
    _step("studies")

    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE _pulled_design_outcomes AS
        SELECT """ + ", ".join(f"outcomes.{c}" for c in DESIGN_OUTCOMES_COLUMNS) + """
        FROM aact.ctgov.design_outcomes AS outcomes
        JOIN _pulled_studies s USING (nct_id)
        """
    )
    con.execute("DELETE FROM raw.design_outcomes WHERE nct_id IN (SELECT nct_id FROM _pulled_studies)")
    con.execute(_insert_pulled("design_outcomes", DESIGN_OUTCOMES_COLUMNS))
    _step("design_outcomes")

    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE _pulled_design_groups AS
        SELECT dg.nct_id, dg.group_type, dg.title, dg.description
        FROM aact.ctgov.design_groups dg
        JOIN _pulled_studies s USING (nct_id)
        """
    )
    con.execute("DELETE FROM raw.design_groups WHERE nct_id IN (SELECT nct_id FROM _pulled_studies)")
    con.execute(_insert_pulled("design_groups", DESIGN_GROUPS_COLUMNS))
    _step("design_groups")

    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE _pulled_conditions AS
        SELECT c.nct_id, c.name
        FROM aact.ctgov.conditions c
        JOIN _pulled_studies s USING (nct_id)
        """
    )
    con.execute("DELETE FROM raw.conditions WHERE nct_id IN (SELECT nct_id FROM _pulled_studies)")
    con.execute(_insert_pulled("conditions", CONDITIONS_COLUMNS))
    _step("conditions")

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
    con.execute(_insert_pulled("browse_conditions", BROWSE_COLUMNS))
    _step("browse_conditions")

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
    con.execute(_insert_pulled("browse_interventions", BROWSE_COLUMNS))
    _step("browse_interventions")

    has_tree_numbers, mesh_terms_count = _pull_mesh_terms(con)
    _step("mesh_terms")

    pulled_nct_ids = [row[0] for row in con.execute("SELECT nct_id FROM _pulled_studies").fetchall()]
    row_counts = {
        "studies": con.execute("SELECT count(*) FROM _pulled_studies").fetchone()[0],
        "design_outcomes": con.execute("SELECT count(*) FROM _pulled_design_outcomes").fetchone()[0],
        "design_groups": con.execute("SELECT count(*) FROM _pulled_design_groups").fetchone()[0],
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
        "migrations": schema.changes,
        "studies_scanned": ta_scanned,
        "hit_scan_cap": ta_hit_scan_cap,
    }


def _insert_pulled(table: str, columns: tuple[str, ...]) -> str:
    names = ", ".join(columns)
    return f"INSERT INTO raw.{table} ({names}) SELECT {names} FROM _pulled_{table}"


_TREE_NUMBER_COLUMN_RE = re.compile(r"tree.?number", re.IGNORECASE)


def _mesh_terms_columns(con: duckdb.DuckDBPyConnection) -> tuple[Optional[str], Optional[str]]:
    """(tree_number_column, term_column) actually present on `ctgov.mesh_terms`,
    introspected rather than hardcoded (see `_pull_mesh_terms`) -- or (None,
    None) if it doesn't carry a usable pair. Shared by `_pull_mesh_terms` and
    `_ta_matches_in_batch`, which both need the same tree-number join."""
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
    return tree_col, term_col


def _pull_mesh_terms(con: duckdb.DuckDBPyConnection) -> tuple[bool, int]:
    """Upsert raw.mesh_terms (mesh_term, mesh_term_normalised, tree_number) from
    AACT's `ctgov.mesh_terms`, if it carries a tree-number column with data.
    Returns (has_tree_numbers, rows landed by this pull).

    `ta_mesh_mapping.yaml`'s `tree_prefixes` layer was written from the MeSH
    C/F branch structure without ever being checked against a live join (this
    build sandbox is egress-blocked from AACT). Rather than hardcode a column
    name we can't verify, this introspects `ctgov.mesh_terms`'s real columns
    at pull time and adapts -- or, if there's nothing usable, leaves the
    (already-ensured, correctly shaped) raw.mesh_terms table untouched and
    reports `has_tree_numbers=False`, so the TA resolver degrades to the
    descriptor/regex layers instead of silently joining on a made-up column.

    As of the AACT data dictionary checked for this project (2026-08-19),
    `ctgov.mesh_terms` has zero rows, so this returns False against the real
    AACT database today regardless of its column names -- see
    vocab/ta_mesh_mapping.yaml's `caveats` block.
    """
    tree_col, term_col = _mesh_terms_columns(con)
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
        INSERT INTO raw.mesh_terms (mesh_term, mesh_term_normalised, tree_number)
        SELECT mesh_term, mesh_term_normalised, tree_number FROM _pulled_mesh_terms
        ON CONFLICT (mesh_term, tree_number) DO UPDATE SET
            mesh_term_normalised = excluded.mesh_term_normalised
        """
    )
    count = con.execute("SELECT count(*) FROM _pulled_mesh_terms").fetchone()[0]
    return count > 0, count


def _ta_matches_in_batch(
    con: duckdb.DuckDBPyConnection, mapping: TaMapping, wanted_ta_ids: set[str], nct_ids: list[str]
) -> set[str]:
    """Which of `nct_ids` match one of `wanted_ta_ids`, judged by the exact
    layered rules `ta/resolver.py` uses to write conformed.study_therapeutic_area
    (via `resolve_study_ta_matches`), so a pull-time match always agrees with
    the truth `pull` resolves afterward."""
    conditions_by_nct: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for nct_id, mesh_term in con.execute(
        "SELECT nct_id, mesh_term FROM aact.ctgov.browse_conditions WHERE nct_id = ANY(?)",
        [nct_ids],
    ).fetchall():
        conditions_by_nct[nct_id].append((mesh_term, mesh_term.strip().lower()))

    interventions_by_nct: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for nct_id, mesh_term in con.execute(
        "SELECT nct_id, mesh_term FROM aact.ctgov.browse_interventions WHERE nct_id = ANY(?)",
        [nct_ids],
    ).fetchall():
        interventions_by_nct[nct_id].append((mesh_term, mesh_term.strip().lower()))

    tree_by_nct: dict[str, dict[str, str]] = defaultdict(dict)
    tree_col, term_col = _mesh_terms_columns(con)
    if tree_col and term_col:
        rows = con.execute(
            f"""
            SELECT bc.nct_id, lower(trim(bc.mesh_term)), mt.{tree_col}
            FROM aact.ctgov.browse_conditions bc
            JOIN aact.ctgov.mesh_terms mt ON lower(trim(mt.{term_col})) = lower(trim(bc.mesh_term))
            WHERE bc.nct_id = ANY(?) AND mt.{tree_col} IS NOT NULL
            """,
            [nct_ids],
        ).fetchall()
        for nct_id, mesh_term_normalised, tree_number in rows:
            tree_by_nct[nct_id][mesh_term_normalised] = tree_number

    matched: set[str] = set()
    for nct_id in nct_ids:
        matches = resolve_study_ta_matches(
            conditions=conditions_by_nct.get(nct_id, []),
            interventions=interventions_by_nct.get(nct_id, []),
            tree_numbers=tree_by_nct.get(nct_id, {}),
            branch_tree_prefixes=[],  # AACT has no CT.gov-style coarse browse branches
            mapping=mapping,
        )
        if set(matches) & wanted_ta_ids:
            matched.add(nct_id)
    return matched


def _org_filter_sql(filters: PullFilters, *, nct_id_column: str) -> tuple[str, list]:
    """SQL predicate (empty, with no params, if `filters.org` wasn't given) that
    keeps only studies whose *lead* sponsor -- never a collaborator, the same
    distinction ingest/ctgov_api.py's AREA[LeadSponsorName] draws -- matches
    one of `filters.org` case-insensitively. `nct_id_column` lets one predicate
    serve both call sites: `run_pull`'s aliased main query (`s.nct_id`) and the
    unaliased candidate scan `_ta_filtered_nct_ids` runs (`nct_id`).

    Built as one `LIKE ?` per fragment, OR'd together, rather than a single
    `LIKE ANY(?)` bound to a list parameter -- DuckDB parses `ANY(?)` there as
    the ANY(subquery) form, which doesn't support LIKE ("Unsupported
    comparison '~~' for ANY/ALL subquery"), not the ANY(array) form this needs.
    """
    if not filters.org:
        return "", []
    patterns = [f"%{o.lower()}%" for o in filters.org]
    like_clauses = " OR ".join("lower(name) LIKE ?" for _ in patterns)
    predicate = (
        f"AND {nct_id_column} IN ("
        "SELECT nct_id FROM aact.ctgov.sponsors "
        f"WHERE lower(lead_or_collaborator) = 'lead' AND ({like_clauses})"
        ")"
    )
    return predicate, patterns


def _ta_filtered_nct_ids(
    con: duckdb.DuckDBPyConnection, aact_phases: list[str], filters: PullFilters
) -> tuple[list[str], int, bool]:
    """Which AACT studies (phase/since/org-matching, most-recent-first) also
    match one of `filters.ta` -- (matched_nct_ids, studies_scanned, hit_scan_cap).

    AACT has no server-side way to express this project's therapeutic areas
    either, so this fetches phase/since/org-matching candidates in batches, most
    recent first, and keeps only the ones `_ta_matches_in_batch` confirms,
    continuing until `filters.limit` matches are found, the candidates run
    out, or `TA_MAX_SCANNED` is hit -- the same reasoning as
    ingest/ctgov_api.py's `MAX_PAGES_TA_FILTERED`: recency alone skews toward
    whichever conditions dominate trial registrations generally, so `--ta`
    has to keep scanning past non-matching studies rather than filtering only
    the first `filters.limit` studies of any area. `--org`, if also given,
    narrows the candidate pool itself (a plain SQL predicate, unlike `--ta`)
    rather than needing its own scan/batch logic.
    """
    mapping = load_ta_mapping(con)
    wanted_ta_ids = set(filters.ta)
    since_clause = "AND start_date >= ?" if filters.since else ""
    org_clause, org_params = _org_filter_sql(filters, nct_id_column="nct_id")

    matched: list[str] = []
    scanned = 0
    offset = 0
    exhausted = False  # every phase/since/org-matching candidate has been scanned
    while len(matched) < filters.limit and scanned < TA_MAX_SCANNED:
        params: list = [aact_phases]
        if filters.since:
            params.append(filters.since)
        params += org_params
        params += [TA_BATCH_SIZE, offset]
        batch = con.execute(
            f"""
            SELECT nct_id FROM aact.ctgov.studies
            WHERE phase = ANY(?)
            {since_clause}
            {org_clause}
            ORDER BY start_date DESC, nct_id
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
        if not batch:
            exhausted = True
            break

        batch_nct_ids = [row[0] for row in batch]
        scanned += len(batch_nct_ids)
        offset += len(batch_nct_ids)

        matching = _ta_matches_in_batch(con, mapping, wanted_ta_ids, batch_nct_ids)
        matched.extend(nct_id for nct_id in batch_nct_ids if nct_id in matching)

        if len(batch_nct_ids) < TA_BATCH_SIZE:
            exhausted = True
            break

    # A cap hit only means something if candidates were actually cut off by
    # it -- if scanning simply ran out of phase/since-matching studies to
    # look at, that's not the scan cap's doing, however few matches it found.
    hit_scan_cap = len(matched) < filters.limit and not exhausted
    return matched[: filters.limit], scanned, hit_scan_cap
