"""AACT's `ctgov.outcomes` and friends -> the raw.outcome_* shape
`ingest/results.py` defines.

Column names are introspected, not assumed: `_available` substitutes NULL for
anything absent, and a missing table is reported rather than raised, since
results are an enrichment of a pull that has already landed the protocol.
"""

from __future__ import annotations

from typing import Optional

import duckdb

from clinical_endpoints.ingest.results import (
    analysis_id_sql,
    baseline_id_sql,
    outcome_id_sql,
)

REQUIRED_TABLES = ("outcomes", "outcome_measurements")
OPTIONAL_TABLES = (
    "result_groups",
    "outcome_counts",
    "outcome_analyses",
    "outcome_analysis_groups",
    "baseline_measurements",
)


class ResultsUnavailable(RuntimeError):
    """Carried back as a warning rather than raised out of `pull`."""


def _columns(con: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_catalog = 'aact' AND table_schema = 'ctgov' AND table_name = ?
            """,
            [table],
        ).fetchall()
    }


def _available(
    present: set[str], wanted: str, *, alias: Optional[str] = None, qualifier: str = ""
) -> str:
    """`wanted` if the upstream table carries it, else NULL, aliased either
    way. Pass `qualifier` wherever the query joins more than one table;
    several of these column names exist on both sides."""
    name = alias or wanted
    source = f"{qualifier}.{wanted}" if qualifier else wanted
    return f"{source} AS {name}" if wanted in present else f"NULL AS {name}"


def _table_present(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    return bool(_columns(con, table))


def results_available(con: duckdb.DuckDBPyConnection) -> bool:
    return all(_table_present(con, table) for table in REQUIRED_TABLES)


def pull_results(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Land the results section for the studies in `_pulled_studies`. The
    caller has already `ensure`d the raw tables."""
    if not results_available(con):
        raise ResultsUnavailable(
            "AACT does not expose ctgov."
            + " / ctgov.".join(t for t in REQUIRED_TABLES if not _table_present(con, t))
            + " in this database, so the results section could not be landed. The protocol "
            "section was pulled normally. Re-run with `--source ctgov_api` to land results "
            "from the public API instead."
        )

    counts: dict[str, int] = {}
    outcomes = _columns(con, "outcomes")

    # `duplicate_ordinal` is a row_number over the four key fields, ordered by
    # AACT's surrogate id; 0 for every outcome that is not a duplicate.
    key_sql = outcome_id_sql(
        "o.nct_id", "o.outcome_type", "o.title", "o.time_frame", "o.duplicate_ordinal"
    )
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _pulled_outcome_keys AS
        SELECT o.*, {key_sql} AS outcome_id
        FROM (
            SELECT
                outcomes.id AS aact_outcome_id,
                outcomes.nct_id,
                {_available(outcomes, "outcome_type")},
                {_available(outcomes, "title")},
                {_available(outcomes, "description")},
                {_available(outcomes, "time_frame")},
                {_available(outcomes, "population")},
                {_available(outcomes, "param_type")},
                {_available(outcomes, "dispersion_type")},
                {_available(outcomes, "units", alias="unit_of_measure")},
                {_available(outcomes, "units_analyzed")},
                row_number() OVER (
                    PARTITION BY outcomes.nct_id, lower(outcomes.outcome_type),
                                 outcomes.title, outcomes.time_frame
                    ORDER BY outcomes.id
                ) - 1 AS duplicate_ordinal,
                row_number() OVER (PARTITION BY outcomes.nct_id ORDER BY outcomes.id) - 1
                    AS ordinal
            FROM aact.ctgov.outcomes AS outcomes
            JOIN _pulled_studies s USING (nct_id)
        ) o
        """
    )

    con.execute("DELETE FROM raw.outcome_measures WHERE nct_id IN (SELECT nct_id FROM _pulled_studies)")
    con.execute(
        """
        INSERT INTO raw.outcome_measures (
            outcome_id, nct_id, ordinal, outcome_type, title, description, time_frame,
            population, param_type, dispersion_type, unit_of_measure, units_analyzed,
            reporting_status
        )
        SELECT outcome_id, nct_id, ordinal, outcome_type, title, description, time_frame,
               population, param_type, dispersion_type, unit_of_measure, units_analyzed,
               NULL
        FROM _pulled_outcome_keys
        """
    )
    counts["outcome_measures"] = _count(con, "_pulled_outcome_keys")

    counts["outcome_groups"] = _pull_outcome_groups(con)
    counts["outcome_measurements"] = _pull_outcome_measurements(con)
    counts["outcome_analyses"] = _pull_outcome_analyses(con)
    counts["baseline_measurements"] = _pull_baseline_measurements(con)
    return counts


def _count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _clear(con: duckdb.DuckDBPyConnection, table: str) -> None:
    con.execute(f"DELETE FROM raw.{table} WHERE nct_id IN (SELECT nct_id FROM _pulled_studies)")


def _pull_outcome_groups(con: duckdb.DuckDBPyConnection) -> int:
    """One row per (reported outcome, arm), with the arm's `n` for that outcome.

    Pairs come from `ctgov.outcome_counts` where present, else from the arms
    that appear in `ctgov.outcome_measurements`; never a cross-join of arms
    against outcomes. `group_key` is AACT's `ctgov_group_code`, the
    counterpart of the API's `groupId`.
    """
    _clear(con, "outcome_groups")
    measurements = _columns(con, "outcome_measurements")
    counts = _columns(con, "outcome_counts") if _table_present(con, "outcome_counts") else set()
    groups = _columns(con, "result_groups") if _table_present(con, "result_groups") else set()

    has_counts = {"outcome_id", "result_group_id", "count", "ctgov_group_code"} <= counts
    if has_counts:
        # Prefer the measure-scoped, participant-unit count per (outcome, arm).
        scope_order = (
            "CASE WHEN lower(coalesce(scope, '')) = 'measure' THEN 0 ELSE 1 END, "
            if "scope" in counts
            else ""
        )
        units_order = (
            "CASE WHEN lower(coalesce(units, '')) LIKE '%participant%' THEN 0 ELSE 1 END, "
            if "units" in counts
            else ""
        )
        pairs_sql = f"""
        SELECT outcome_id AS aact_outcome_id, ctgov_group_code AS group_key,
               result_group_id, count AS n, {"units" if "units" in counts else "NULL"} AS n_units
        FROM (
            SELECT c.*, row_number() OVER (
                PARTITION BY c.outcome_id, c.ctgov_group_code
                ORDER BY {scope_order}{units_order} c.id
            ) AS pick
            FROM aact.ctgov.outcome_counts c
        ) WHERE pick = 1
        """
    elif "ctgov_group_code" in measurements:
        pairs_sql = f"""
        SELECT DISTINCT outcome_id AS aact_outcome_id, ctgov_group_code AS group_key,
               {"result_group_id" if "result_group_id" in measurements else "NULL"} AS result_group_id,
               NULL AS n, NULL AS n_units
        FROM aact.ctgov.outcome_measurements
        """
    else:
        return 0

    group_select = "NULL AS title, NULL AS description"
    group_join = ""
    if {"id", "title"} <= groups:
        group_select = "g.title AS title, " + (
            "g.description AS description" if "description" in groups else "NULL AS description"
        )
        group_join = "LEFT JOIN aact.ctgov.result_groups g ON g.id = p.result_group_id"

    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _pulled_outcome_groups AS
        SELECT
            k.outcome_id, k.nct_id, p.group_key,
            row_number() OVER (PARTITION BY k.outcome_id ORDER BY p.group_key) - 1 AS ordinal,
            {group_select},
            p.n, p.n_units
        FROM ({pairs_sql}) p
        JOIN _pulled_outcome_keys k ON k.aact_outcome_id = p.aact_outcome_id
        {group_join}
        """
    )
    con.execute(
        """
        INSERT INTO raw.outcome_groups (
            outcome_id, nct_id, group_key, ordinal, title, description, n, n_units
        )
        SELECT outcome_id, nct_id, group_key, ordinal, title, description,
               TRY_CAST(n AS INTEGER), n_units
        FROM _pulled_outcome_groups
        """
    )
    return _count(con, "_pulled_outcome_groups")


def _pull_outcome_measurements(con: duckdb.DuckDBPyConnection) -> int:
    _clear(con, "outcome_measurements")
    measurements = _columns(con, "outcome_measurements")
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _pulled_outcome_measurements AS
        SELECT
            k.outcome_id, k.nct_id,
            {_available(measurements, "ctgov_group_code", alias="group_key", qualifier="m")},
            {_available(measurements, "classification", alias="class_title", qualifier="m")},
            {_available(measurements, "category", alias="category_title", qualifier="m")},
            {_available(measurements, "param_value", qualifier="m")},
            {_available(measurements, "param_value_num", qualifier="m")},
            {_available(measurements, "dispersion_value", qualifier="m")},
            {_available(measurements, "dispersion_value_num", qualifier="m")},
            {_available(measurements, "dispersion_lower_limit", qualifier="m")},
            {_available(measurements, "dispersion_upper_limit", qualifier="m")},
            {_available(measurements, "explanation_of_na", alias="comment", qualifier="m")}
        FROM aact.ctgov.outcome_measurements m
        JOIN _pulled_outcome_keys k ON k.aact_outcome_id = m.outcome_id
        """
    )
    con.execute(
        """
        INSERT INTO raw.outcome_measurements (
            outcome_id, nct_id, group_key, class_title, category_title,
            param_value, param_value_num, dispersion_value, dispersion_value_num,
            dispersion_lower_limit, dispersion_upper_limit, n, comment
        )
        SELECT outcome_id, nct_id, group_key, class_title, category_title,
               CAST(param_value AS VARCHAR), TRY_CAST(param_value_num AS DOUBLE),
               CAST(dispersion_value AS VARCHAR), TRY_CAST(dispersion_value_num AS DOUBLE),
               TRY_CAST(dispersion_lower_limit AS DOUBLE),
               TRY_CAST(dispersion_upper_limit AS DOUBLE),
               NULL, comment
        FROM _pulled_outcome_measurements
        """
    )
    return _count(con, "_pulled_outcome_measurements")


def _pull_outcome_analyses(con: duckdb.DuckDBPyConnection) -> int:
    _clear(con, "outcome_analyses")
    if not _table_present(con, "outcome_analyses"):
        return 0
    analyses = _columns(con, "outcome_analyses")

    # AACT splits the compared arms into their own table; the API carries a
    # `groupIds` array. Both land as a JSON array.
    groups_select = "NULL AS group_keys"
    groups_join = ""
    if _table_present(con, "outcome_analysis_groups"):
        analysis_groups = _columns(con, "outcome_analysis_groups")
        if "ctgov_group_code" in analysis_groups and "outcome_analysis_id" in analysis_groups:
            groups_select = "coalesce(ag.group_keys, '[]') AS group_keys"
            groups_join = """
            LEFT JOIN (
                SELECT outcome_analysis_id,
                       to_json(list(ctgov_group_code ORDER BY ctgov_group_code)) AS group_keys
                FROM aact.ctgov.outcome_analysis_groups
                GROUP BY outcome_analysis_id
            ) ag ON ag.outcome_analysis_id = a.id
            """

    ordinal = "row_number() OVER (PARTITION BY k.outcome_id ORDER BY a.id) - 1"
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _pulled_outcome_analyses AS
        SELECT
            k.outcome_id, k.nct_id, {ordinal} AS ordinal,
            {groups_select},
            {_available(analyses, "groups_description", alias="group_description", qualifier="a")},
            {_available(analyses, "param_type", qualifier="a")},
            {_available(analyses, "param_value", qualifier="a")},
            {_available(analyses, "dispersion_type", qualifier="a")},
            {_available(analyses, "dispersion_value", qualifier="a")},
            {_available(analyses, "p_value", qualifier="a")},
            {_available(analyses, "p_value_modifier", qualifier="a")},
            {_available(analyses, "p_value_description", qualifier="a")},
            {_available(analyses, "ci_percent", qualifier="a")},
            {_available(analyses, "ci_n_sides", qualifier="a")},
            {_available(analyses, "ci_lower_limit", qualifier="a")},
            {_available(analyses, "ci_upper_limit", qualifier="a")},
            {_available(analyses, "method", qualifier="a")},
            {_available(analyses, "method_description", qualifier="a")},
            {_available(analyses, "non_inferiority_type", qualifier="a")},
            {_available(analyses, "non_inferiority_description", qualifier="a")},
            {_available(analyses, "estimate_description", qualifier="a")},
            {_available(analyses, "other_analysis_description", qualifier="a")}
        FROM aact.ctgov.outcome_analyses a
        JOIN _pulled_outcome_keys k ON k.aact_outcome_id = a.outcome_id
        {groups_join}
        """
    )

    # The p-value is reassembled from AACT's (modifier, value) pair to match
    # the three columns ingest/results.py's `split_p_value` produces.
    con.execute(
        f"""
        INSERT INTO raw.outcome_analyses (
            analysis_id, outcome_id, nct_id, ordinal, group_keys, group_description,
            param_type, param_value, param_value_num,
            dispersion_type, dispersion_value, dispersion_value_num,
            p_value, p_value_num, p_value_modifier, p_value_description,
            ci_percent, ci_n_sides, ci_lower_limit, ci_upper_limit,
            method, method_description,
            non_inferiority, non_inferiority_type, non_inferiority_description,
            estimate_description, other_analysis_description
        )
        SELECT
            {analysis_id_sql("outcome_id", "ordinal")}, outcome_id, nct_id, ordinal,
            CAST(group_keys AS VARCHAR), group_description,
            param_type, CAST(param_value AS VARCHAR), TRY_CAST(param_value AS DOUBLE),
            dispersion_type, CAST(dispersion_value AS VARCHAR),
            TRY_CAST(dispersion_value AS DOUBLE),
            nullif(CASE
                WHEN p_value IS NULL THEN ''
                WHEN coalesce(CAST(p_value_modifier AS VARCHAR), '=') IN ('', '=')
                    THEN CAST(p_value AS VARCHAR)
                ELSE CAST(p_value_modifier AS VARCHAR) || CAST(p_value AS VARCHAR)
            END, ''),
            TRY_CAST(p_value AS DOUBLE),
            CASE WHEN p_value IS NULL THEN nullif(CAST(p_value_modifier AS VARCHAR), '')
                 ELSE coalesce(nullif(CAST(p_value_modifier AS VARCHAR), ''), '=') END,
            p_value_description,
            TRY_CAST(ci_percent AS DOUBLE), CAST(ci_n_sides AS VARCHAR),
            TRY_CAST(ci_lower_limit AS DOUBLE), TRY_CAST(ci_upper_limit AS DOUBLE),
            method, method_description,
            -- ingest/results.py's `is_non_inferiority`, in SQL
            CASE
                WHEN non_inferiority_type IS NULL THEN NULL
                WHEN lower(non_inferiority_type) LIKE '%non-inferiority%'
                  OR lower(non_inferiority_type) LIKE '%noninferiority%'
                  OR lower(non_inferiority_type) LIKE '%equivalence%' THEN TRUE
                ELSE FALSE
            END,
            non_inferiority_type, non_inferiority_description,
            estimate_description, other_analysis_description
        FROM _pulled_outcome_analyses
        """
    )
    return _count(con, "_pulled_outcome_analyses")


def _pull_baseline_measurements(con: duckdb.DuckDBPyConnection) -> int:
    _clear(con, "baseline_measurements")
    if not _table_present(con, "baseline_measurements"):
        return 0
    baseline = _columns(con, "baseline_measurements")

    group_title_select = "NULL AS group_title"
    group_join = ""
    if _table_present(con, "result_groups") and "result_group_id" in baseline:
        group_title_select = "g.title AS group_title"
        group_join = "LEFT JOIN aact.ctgov.result_groups g ON g.id = b.result_group_id"

    key_sql = baseline_id_sql("b.nct_id", "b.title", "b.units" if "units" in baseline else "NULL")
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _pulled_baseline_measurements AS
        SELECT
            {key_sql} AS baseline_id, b.nct_id,
            {_available(baseline, "ctgov_group_code", alias="group_key", qualifier="b")},
            NULL AS ordinal,
            {_available(baseline, "title", qualifier="b")},
            {_available(baseline, "description", qualifier="b")},
            NULL AS population,
            {_available(baseline, "classification", alias="class_title", qualifier="b")},
            {_available(baseline, "category", alias="category_title", qualifier="b")},
            {_available(baseline, "units", alias="unit_of_measure", qualifier="b")},
            {_available(baseline, "param_type", qualifier="b")},
            {_available(baseline, "param_value", qualifier="b")},
            {_available(baseline, "param_value_num", qualifier="b")},
            {_available(baseline, "dispersion_type", qualifier="b")},
            {_available(baseline, "dispersion_value", qualifier="b")},
            {_available(baseline, "dispersion_value_num", qualifier="b")},
            {_available(baseline, "dispersion_lower_limit", qualifier="b")},
            {_available(baseline, "dispersion_upper_limit", qualifier="b")},
            {_available(baseline, "number_analyzed", alias="n", qualifier="b")},
            {group_title_select}
        FROM aact.ctgov.baseline_measurements b
        JOIN _pulled_studies s ON s.nct_id = b.nct_id
        {group_join}
        """
    )
    con.execute(
        """
        INSERT INTO raw.baseline_measurements (
            baseline_id, nct_id, group_key, ordinal, title, description, population,
            class_title, category_title, unit_of_measure, param_type,
            param_value, param_value_num, dispersion_type, dispersion_value,
            dispersion_value_num, dispersion_lower_limit, dispersion_upper_limit, n,
            group_title
        )
        SELECT baseline_id, nct_id, group_key, TRY_CAST(ordinal AS INTEGER), title,
               description, population, class_title, category_title, unit_of_measure,
               param_type, CAST(param_value AS VARCHAR), TRY_CAST(param_value_num AS DOUBLE),
               dispersion_type, CAST(dispersion_value AS VARCHAR),
               TRY_CAST(dispersion_value_num AS DOUBLE),
               TRY_CAST(dispersion_lower_limit AS DOUBLE),
               TRY_CAST(dispersion_upper_limit AS DOUBLE),
               TRY_CAST(n AS INTEGER), group_title
        FROM _pulled_baseline_measurements
        """
    )
    return _count(con, "_pulled_baseline_measurements")
