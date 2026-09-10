"""The AACT half of docs/DRUG_CLASS_SPEC.md phase 1: `ctgov.interventions` and
friends -> the same raw.intervention* shape `ingest/interventions.py` defines
and `ingest/ctgov_api.py` lands from the API payload.

These tables sit in the database `run_pull` has already attached, so nothing
here costs another connection.

**Every column name below is introspected, not assumed**, following
`aact_results.py` and `_pull_mesh_terms` before it: this project's build
environment cannot reach AACT, so a hardcoded column list would turn a single
upstream rename into a failed pull with a binder error. A missing table costs
the drug-class axis, never the pull.

Two structural differences from the API backend, both of which the resolver has
to cope with rather than paper over:

**AACT has the better arm link.** `ctgov.design_group_interventions` is a real
join table, where the API offers only a list of arm *labels* to string-match.
That is why `raw.arm_interventions.link_method` exists.

**AACT has no intervention ancestors or browse branches.** The API derives both
(`derivedSection.interventionBrowseModule`); AACT publishes neither, and
`ctgov.mesh_terms` -- which could in principle supply the ancestry -- carries
zero rows in the live database (confirmed against the AACT data dictionary
checked for this project, 2026-08-19; see `vocab/ta_mesh_mapping.yaml`'s
caveats). So `raw.browse_intervention_ancestors` and
`raw.browse_intervention_branches` stay empty-but-correctly-shaped on an AACT
pull, exactly as `raw.mesh_terms` does on a CT.gov pull, and the drug-class
resolver degrades to its descriptor, agent-name and modality layers. It does
not invent an ancestry source.
"""

from __future__ import annotations

import duckdb

from clinical_endpoints.ingest.interventions import LINK_JOIN_TABLE

#: Without `interventions` there is nothing to land at all. The other two each
#: contribute one table and are skipped individually when absent.
REQUIRED_TABLES = ("interventions",)
OPTIONAL_TABLES = ("intervention_other_names", "design_group_interventions")


class InterventionsUnavailable(RuntimeError):
    """AACT does not expose `ctgov.interventions` in the shape this needs.

    Carried back to the caller as a warning rather than raised out of `pull`:
    everything else in the pull has already succeeded by the time this runs.
    """


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
    """`wanted` if the upstream table carries it, else NULL -- aliased either
    way, so the SELECT list stays positionally stable whatever AACT is missing.
    Same helper, same reasoning, as `aact_results.py`'s."""
    name = alias or wanted
    source = f"{qualifier}.{wanted}" if qualifier else wanted
    return f"{source} AS {name}" if wanted in present else f"NULL AS {name}"


def interventions_available(con: duckdb.DuckDBPyConnection) -> bool:
    return all(_table_present(con, table) for table in REQUIRED_TABLES)


def _ordinal_source(present: set[str]) -> str:
    """What to order interventions by when assigning each study's ordinals.

    `ordinal` is this project's own key (see `ingest/interventions.py`): AACT's
    `interventions.id` is a surrogate that is not stable across AACT's rebuilds,
    the same objection docs/ENDPOINT_RESULTS_SPEC.md raises against
    `outcomes.id`, so it is used to *order* rows and never stored. Falling back
    to the name keeps the ordinal deterministic if `id` is ever absent.
    """
    if "id" in present:
        return "id"
    return "name"


def pull_interventions(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Land the intervention tables for the studies in `_pulled_studies` (the
    temp table `ingest/aact.py`'s `run_pull` has already built), replacing
    whatever those studies previously had. Returns per-table row counts.

    Assumes the caller has already `ensure`d the five raw tables against
    `INTERVENTION_TABLES`; it writes into them and never creates them, so the
    schema reconciliation stays in one place.
    """
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

    # Ordinals are assigned here, per study, so raw.arm_interventions has
    # something stable to point at on both backends.
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

    # Neither exists on this backend -- see the module docstring. The rows for
    # this pull's studies are still cleared, so re-pulling a study through AACT
    # that was previously landed from the API does not leave stale ancestors
    # behind claiming to describe the current pull.
    for table in ("browse_intervention_ancestors", "browse_intervention_branches"):
        con.execute(f"DELETE FROM raw.{table} WHERE nct_id IN (SELECT nct_id FROM _pulled_studies)")
        counts[table] = 0

    return counts


def _pull_other_names(con: duckdb.DuckDBPyConnection) -> int:
    """`ctgov.intervention_other_names` -> raw.intervention_other_names, joined
    back to the ordinal assigned above through AACT's own surrogate id."""
    con.execute(
        "DELETE FROM raw.intervention_other_names "
        "WHERE nct_id IN (SELECT nct_id FROM _pulled_studies)"
    )
    present = columns(con, "intervention_other_names")
    # The join needs both sides of AACT's surrogate key. Without either, the
    # aliases cannot be attached to a specific intervention, and attaching them
    # to the wrong one would hand the resolver a drug name the study never used.
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
    """`ctgov.design_group_interventions` -> raw.arm_interventions.

    The strong form of the arm link: a real join table, recorded as
    `link_method = 'join_table'`. Resolved to the arm *title* rather than
    AACT's `design_group_id` so both backends key the same way -- the API has
    no equivalent surrogate, only the label.
    """
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
