"""Pull study data from AACT into raw.*."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Callable, Optional

import duckdb

from clinical_endpoints.ingest import aact_interventions, aact_results
from clinical_endpoints.ingest.design import (
    DESIGN_GROUPS_COLUMNS,
    DESIGN_GROUPS_DDL,
    STUDY_COLUMNS,
)
from clinical_endpoints.ingest.design import STUDIES_DDL as SHARED_STUDIES_DDL
from clinical_endpoints.ingest.filters import PullFilters, normalize_phases
from clinical_endpoints.ingest.interventions import INTERVENTION_TABLE_NAMES, INTERVENTION_TABLES
from clinical_endpoints.ingest.pull_log import write_pull_log
from clinical_endpoints.ingest.results import RESULTS_TABLE_NAMES, RESULTS_TABLES
from clinical_endpoints.ingest.upsert import SchemaReconciler
from clinical_endpoints.ta.resolver import TaMapping, load_ta_mapping, resolve_study_ta_matches
from clinical_endpoints.drug_class.resolver import (
    DrugClassMapping,
    Intervention,
    load_drug_class_mapping,
    resolve_study_drug_class_matches,
)

SOURCE = "aact"

# AACT can carry MeSH tree numbers via mesh_terms; the CT.gov backend lands
# browse_condition_branches instead.
SOURCE_TABLES = (
    "studies",
    "design_outcomes",
    "design_groups",
    "conditions",
    "browse_conditions",
    "browse_interventions",
    "mesh_terms",
) + INTERVENTION_TABLE_NAMES

RESULTS_SOURCE_TABLES = RESULTS_TABLE_NAMES

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

# Every INSERT names these rather than relying on positional `SELECT *`.
DESIGN_OUTCOMES_COLUMNS = (
    "nct_id", "outcome_type", "measure", "time_frame", "description", "population",
)
CONDITIONS_COLUMNS = ("nct_id", "name")
BROWSE_COLUMNS = ("nct_id", "mesh_term", "mesh_term_normalised", "mesh_type")
MESH_TERMS_DDL = (
    "mesh_term VARCHAR, mesh_term_normalised VARCHAR, tree_number VARCHAR, "
    "PRIMARY KEY (mesh_term, tree_number)"
)

PULL_STEPS = (
    "studies",
    "design_outcomes",
    "design_groups",
    "conditions",
    "browse_conditions",
    "browse_interventions",
    "mesh_terms",
    "interventions",
    "results",
)

# `--ta` / `--drug-class` cannot be SQL predicates, so candidates are scanned
# in batches, most recent first, until `limit` matches are found.
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
        CASE lower(trim(e.healthy_volunteers))
            WHEN 'accepts healthy volunteers' THEN TRUE
            WHEN 'yes' THEN TRUE
            WHEN 'no' THEN FALSE
            ELSE NULL
        END AS healthy_volunteers,
        e.gender, e.minimum_age, e.maximum_age,
        e.population AS population_description,
        sp.organization,
        (s.results_first_submitted_date IS NOT NULL) AS has_results
    FROM aact.ctgov.studies s
    LEFT JOIN aact.ctgov.designs d ON d.nct_id = s.nct_id
    LEFT JOIN aact.ctgov.eligibilities e ON e.nct_id = s.nct_id
    -- one row per (nct_id, lead-or-collaborator); collapse to the lead sponsor
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
    """Pull filtered studies from AACT into raw.* and log the pull.

    Upserts: raw.studies per nct_id, child tables replaced for this pull's
    nct_ids only. `filters.replace` drops the tables first.
    `on_step(step_name, index, total)` is called after each of PULL_STEPS.
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
    for table, ddl, _columns in INTERVENTION_TABLES:
        schema.ensure(table, ddl)
    if filters.with_results:
        for table, ddl, _columns in RESULTS_TABLES:
            schema.ensure(table, ddl)

    ta_scanned: Optional[int] = None
    ta_hit_scan_cap = False

    if filters.ta or filters.drug_class:
        matched_nct_ids, ta_scanned, ta_hit_scan_cap = _scan_filtered_nct_ids(
            con, aact_phases, filters
        )
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

    # Interventions and results are enrichments: a backend that cannot supply
    # them costs that axis, not the pull.
    intervention_counts: dict[str, int] = {name: 0 for name in INTERVENTION_TABLE_NAMES}
    interventions_warning: Optional[str] = None
    try:
        intervention_counts.update(aact_interventions.pull_interventions(con))
    except aact_interventions.InterventionsUnavailable as exc:
        interventions_warning = str(exc)
    _step("interventions")

    results_counts: dict[str, int] = {}
    results_warning: Optional[str] = None
    if filters.with_results:
        try:
            results_counts = aact_results.pull_results(con)
        except aact_results.ResultsUnavailable as exc:
            results_warning = str(exc)
    _step("results")

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
        **intervention_counts,
        **results_counts,
    }

    log_entry = write_pull_log(
        con,
        source=SOURCE,
        filters=filters.as_dict(),
        row_counts=row_counts,
        source_tables=SOURCE_TABLES + (RESULTS_SOURCE_TABLES if results_counts else ()),
    )
    return {
        **log_entry,
        "row_counts": row_counts,
        "has_mesh_tree_numbers": has_tree_numbers,
        "results_warning": results_warning,
        "interventions_warning": interventions_warning,
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
    """(tree_number_column, term_column) present on `ctgov.mesh_terms`, or (None, None)."""
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
    """Upsert raw.mesh_terms from `ctgov.mesh_terms` if it carries a tree-number
    column. Column names are introspected. Returns (has_tree_numbers, rows).

    As of the AACT data dictionary checked 2026-08-19, `ctgov.mesh_terms` has
    zero rows, so this returns False against the live database.
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
            branch_tree_prefixes=[],
            mapping=mapping,
        )
        if set(matches) & wanted_ta_ids:
            matched.add(nct_id)
    return matched


def _org_filter_sql(filters: PullFilters, *, nct_id_column: str) -> tuple[str, list]:
    """Predicate keeping studies whose lead sponsor matches one of `filters.org`.

    One `LIKE ?` per fragment rather than `LIKE ANY(?)`, which DuckDB parses
    as the ANY(subquery) form that does not support LIKE.
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


def _drug_class_matches_in_batch(
    con: duckdb.DuckDBPyConnection,
    mapping: DrugClassMapping,
    wanted_class_ids: set[str],
    nct_ids: list[str],
) -> set[str]:
    """AACT publishes no intervention ancestors or browse branches, so the
    match rests on the intervention rows and MeSH descriptors."""
    if not aact_interventions.interventions_available(con):
        return set()

    interventions_by_nct: dict[str, dict[int, Intervention]] = defaultdict(dict)
    present = aact_interventions.columns(con, "interventions")
    name_col = "name" if "name" in present else "NULL"
    type_col = "intervention_type" if "intervention_type" in present else "NULL"
    order_col = "id" if "id" in present else "name"
    rows = con.execute(
        f"""
        SELECT nct_id,
               CAST(row_number() OVER (PARTITION BY nct_id ORDER BY {order_col}) - 1 AS INTEGER),
               {type_col}, {name_col}
        FROM aact.ctgov.interventions WHERE nct_id = ANY(?)
        """,
        [nct_ids],
    ).fetchall()
    for nct_id, ordinal, intervention_type, name in rows:
        interventions_by_nct[nct_id][ordinal] = Intervention(
            ordinal=ordinal,
            intervention_type=intervention_type,
            name=name,
            name_normalised=(name or "").strip().lower() or None,
        )

    mesh_by_nct: dict[str, list[str]] = defaultdict(list)
    for nct_id, mesh_term in con.execute(
        "SELECT nct_id, mesh_term FROM aact.ctgov.browse_interventions WHERE nct_id = ANY(?)",
        [nct_ids],
    ).fetchall():
        mesh_by_nct[nct_id].append(mesh_term)

    matched: set[str] = set()
    for nct_id in nct_ids:
        matches = resolve_study_drug_class_matches(
            interventions=list(interventions_by_nct.get(nct_id, {}).values()),
            mesh_terms=mesh_by_nct.get(nct_id, []),
            ancestors=[],
            branches=[],
            mapping=mapping,
        )
        if set(matches) & wanted_class_ids:
            matched.add(nct_id)
    return matched


def _scan_filtered_nct_ids(
    con: duckdb.DuckDBPyConnection, aact_phases: list[str], filters: PullFilters
) -> tuple[list[str], int, bool]:
    """Scan phase/since/org-matching candidates in batches, most recent first,
    keeping those that match `filters.ta` and/or `filters.drug_class`, until
    `filters.limit` matches are found, the candidates run out, or
    TA_MAX_SCANNED is hit. Returns (matched_nct_ids, studies_scanned, hit_scan_cap)."""
    mapping = load_ta_mapping(con) if filters.ta else None
    wanted_ta_ids = set(filters.ta) if filters.ta else None
    class_mapping = load_drug_class_mapping(con) if filters.drug_class else None
    wanted_class_ids = set(filters.drug_class) if filters.drug_class else None
    since_clause = "AND start_date >= ?" if filters.since else ""
    org_clause, org_params = _org_filter_sql(filters, nct_id_column="nct_id")

    matched: list[str] = []
    scanned = 0
    offset = 0
    exhausted = False
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

        matching = set(batch_nct_ids)
        if wanted_ta_ids is not None:
            matching &= _ta_matches_in_batch(con, mapping, wanted_ta_ids, batch_nct_ids)
        if wanted_class_ids is not None:
            matching &= _drug_class_matches_in_batch(
                con, class_mapping, wanted_class_ids, batch_nct_ids
            )
        matched.extend(nct_id for nct_id in batch_nct_ids if nct_id in matching)

        if len(batch_nct_ids) < TA_BATCH_SIZE:
            exhausted = True
            break

    hit_scan_cap = len(matched) < filters.limit and not exhausted
    return matched[: filters.limit], scanned, hit_scan_cap
