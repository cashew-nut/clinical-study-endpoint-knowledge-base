"""AACT's `ctgov.interventions` and friends -> the raw.intervention* shape
`ingest/interventions.py` defines.

Column names are introspected, not assumed, so an upstream rename costs the
drug-class axis rather than the pull. AACT has the stronger arm link (a real
join table) but publishes no intervention ancestors or browse branches, so
those two tables stay empty on an AACT pull.
"""

from __future__ import annotations

import duckdb

from clinical_endpoints.ingest.interventions import LINK_JOIN_TABLE

REQUIRED_TABLES = ("interventions",)
OPTIONAL_TABLES = ("intervention_other_names", "design_group_interventions")


class InterventionsUnavailable(RuntimeError):
    """Carried back as a warning rather than raised out of `pull`."""


def columns(con: duckdb.DuckDBPyConnection, table: str) -> set[str]:
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


def _table_present(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    return bool(columns(con, table))


def _available(present: set[str], wanted: str, *, alias: str | None = None, qualifier: str = "") -> str:
    """`wanted` if the upstream table carries it, else NULL, aliased either way."""
    name = alias or wanted
    source = f"{qualifier}.{wanted}" if qualifier else wanted
    return f"{source} AS {name}" if wanted in present else f"NULL AS {name}"


def interventions_available(con: duckdb.DuckDBPyConnection) -> bool:
    return all(_table_present(con, table) for table in REQUIRED_TABLES)


def _ordinal_source(present: set[str]) -> str:
    """AACT's `interventions.id` is used to order rows, never stored."""
    if "id" in present:
        return "id"
    return "name"


def pull_interventions(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Land the intervention tables for the studies in `_pulled_studies`.
    The caller has already `ensure`d the raw tables."""
    if not interventions_available(con):
        raise InterventionsUnavailable(
            "AACT does not expose ctgov."
            + " / ctgov.".join(t for t in REQUIRED_TABLES if not _table_present(con, t))
            + " in this database, so the interventions the drug-class axis needs could not "
            "be landed. The rest of the pull completed normally. Re-run with "
            "`--source ctgov_api` to land them from the public API instead."
        )

    counts: dict[str, int] = {}
    present = columns(con, "interventions")

    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _pulled_interventions AS
        SELECT
            i.nct_id,
            CAST(row_number() OVER (
                PARTITION BY i.nct_id ORDER BY i.{_ordinal_source(present)}
            ) - 1 AS INTEGER) AS ordinal,
            {_available(present, "intervention_type", qualifier="i")},
            {_available(present, "name", qualifier="i")},
            lower(trim({"i.name" if "name" in present else "NULL"})) AS name_normalised,
            {_available(present, "description", qualifier="i")},
            {"i.id" if "id" in present else "NULL"} AS _source_id
        FROM aact.ctgov.interventions i
        JOIN _pulled_studies s USING (nct_id)
        """
    )
    con.execute(
        "DELETE FROM raw.interventions WHERE nct_id IN (SELECT nct_id FROM _pulled_studies)"
    )
    con.execute(
        """
        INSERT INTO raw.interventions
            (nct_id, ordinal, intervention_type, name, name_normalised, description)
        SELECT nct_id, ordinal, intervention_type, name,
               nullif(name_normalised, '') AS name_normalised, description
        FROM _pulled_interventions
        """
    )
    counts["interventions"] = con.execute(
        "SELECT count(*) FROM _pulled_interventions"
    ).fetchone()[0]

    counts["intervention_other_names"] = _pull_other_names(con)
    counts["arm_interventions"] = _pull_arm_links(con)

    # Cleared so a study re-pulled through AACT does not keep stale ancestors from an API pull.
    for table in ("browse_intervention_ancestors", "browse_intervention_branches"):
        con.execute(f"DELETE FROM raw.{table} WHERE nct_id IN (SELECT nct_id FROM _pulled_studies)")
        counts[table] = 0

    return counts


def _pull_other_names(con: duckdb.DuckDBPyConnection) -> int:
    con.execute(
        "DELETE FROM raw.intervention_other_names "
        "WHERE nct_id IN (SELECT nct_id FROM _pulled_studies)"
    )
    present = columns(con, "intervention_other_names")
    if not present or "intervention_id" not in present or "name" not in present:
        return 0
    con.execute(
        """
        INSERT INTO raw.intervention_other_names
            (nct_id, ordinal, other_name, other_name_normalised)
        SELECT p.nct_id, p.ordinal, o.name, nullif(lower(trim(o.name)), '')
        FROM aact.ctgov.intervention_other_names o
        JOIN _pulled_interventions p ON p._source_id = o.intervention_id
        WHERE o.name IS NOT NULL
        """
    )
    return con.execute(
        "SELECT count(*) FROM raw.intervention_other_names "
        "WHERE nct_id IN (SELECT nct_id FROM _pulled_studies)"
    ).fetchone()[0]


def _pull_arm_links(con: duckdb.DuckDBPyConnection) -> int:
    """`ctgov.design_group_interventions` -> raw.arm_interventions, keyed on
    the arm title so both backends key the same way."""
    con.execute(
        "DELETE FROM raw.arm_interventions WHERE nct_id IN (SELECT nct_id FROM _pulled_studies)"
    )
    link_columns = columns(con, "design_group_interventions")
    group_columns = columns(con, "design_groups")
    needed = {"design_group_id", "intervention_id"} <= link_columns
    if not needed or "id" not in group_columns or "title" not in group_columns:
        return 0
    con.execute(
        """
        INSERT INTO raw.arm_interventions (nct_id, group_title, intervention_ordinal, link_method)
        SELECT DISTINCT p.nct_id, g.title, p.ordinal, ?
        FROM aact.ctgov.design_group_interventions dgi
        JOIN _pulled_interventions p ON p._source_id = dgi.intervention_id
        JOIN aact.ctgov.design_groups g ON g.id = dgi.design_group_id
        WHERE g.title IS NOT NULL
        """,
        [LINK_JOIN_TABLE],
    )
    return con.execute(
        "SELECT count(*) FROM raw.arm_interventions "
        "WHERE nct_id IN (SELECT nct_id FROM _pulled_studies)"
    ).fetchone()[0]
