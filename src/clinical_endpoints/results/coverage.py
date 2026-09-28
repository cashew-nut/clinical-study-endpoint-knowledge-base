"""`endpoints results coverage`: five measurements of the results section.

1. what share of conformed studies have results posted;
2. what share of reported outcome titles match a planned `measure`;
3. the observed `dispersion_type` and `param_type` value sets;
4. the share of `unit_of_measure` strings that resolve against scales.yaml;
5. the share of results groups linked to a protocol arm, and their roles.
"""

from __future__ import annotations

from typing import Optional

import duckdb


def _table_exists(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
            [schema, table],
        ).fetchone()
    )


class NoResults(RuntimeError):
    pass


def _scoped(nct_ids: Optional[list[str]], column: str = "nct_id") -> tuple[str, list]:
    """`(" AND <column> = ANY(?)", [ids])`, or nothing when unscoped."""
    if nct_ids is None:
        return "", []
    return f" AND {column} = ANY(?)", [list(nct_ids)]


def gate_measurements(
    con: duckdb.DuckDBPyConnection, *, top: int = 20, nct_ids: Optional[list[str]] = None
) -> dict:
    """`nct_ids` restricts every measurement to those studies; None means all."""
    if not _table_exists(con, "raw", "outcome_measures"):
        raise NoResults(
            "raw.outcome_measures is empty -- run `endpoints pull` (without --no-results) first"
        )

    return {
        "posting": _posting(con, nct_ids),
        "titles": _titles(con, nct_ids),
        "enumerations": _enumerations(con, nct_ids, top=top),
        "units": _units(con, nct_ids, top=top),
        "arms": _arms(con, nct_ids),
    }


def _posting(con: duckdb.DuckDBPyConnection, nct_ids: Optional[list[str]] = None) -> dict:
    """Three denominators: studies pulled, studies conformed, and studies whose
    results landed. The registry's `has_results` flag is a claim about the
    record; `landed` is what parsed."""
    scope, params = _scoped(nct_ids)
    pulled = con.execute(f"SELECT count(*) FROM raw.studies WHERE TRUE{scope}", params).fetchone()[0]
    flagged = con.execute(
        f"SELECT count(*) FROM raw.studies WHERE has_results{scope}", params
    ).fetchone()[0]
    landed = con.execute(
        f"SELECT count(DISTINCT nct_id) FROM raw.outcome_measures WHERE TRUE{scope}", params
    ).fetchone()[0]

    conformed = conformed_with_results = None
    if _table_exists(con, "conformed", "endpoints"):
        conformed = con.execute(
            f"SELECT count(DISTINCT nct_id) FROM conformed.endpoints WHERE TRUE{scope}", params
        ).fetchone()[0]
        conformed_with_results = con.execute(
            f"""
            SELECT count(DISTINCT e.nct_id) FROM conformed.endpoints e
            WHERE e.nct_id IN (SELECT nct_id FROM raw.outcome_measures){scope}
            """,
            params,
        ).fetchone()[0]

    return {
        "studies_pulled": pulled,
        "studies_flagged_has_results": flagged,
        "studies_with_results_landed": landed,
        "studies_conformed": conformed,
        "conformed_studies_with_results": conformed_with_results,
    }


def _titles(con: duckdb.DuckDBPyConnection, nct_ids: Optional[list[str]] = None) -> dict:
    if not _table_exists(con, "conformed", "endpoint_results"):
        return {"computed": False}
    scope, params = _scoped(nct_ids)
    mix = dict(
        con.execute(
            f"""
            SELECT coalesce(link_method, 'unlinked'), count(*)
            FROM conformed.endpoint_results WHERE result_kind = 'outcome'{scope}
            GROUP BY 1 ORDER BY 2 DESC
            """,
            params,
        ).fetchall()
    )
    total = sum(mix.values())
    unconformed = 0
    if _table_exists(con, "conformed", "results_review_queue"):
        unconformed = con.execute(
            f"""
            SELECT count(*) FROM conformed.results_review_queue
            WHERE reason = 'measurement_unmatched' AND result_kind = 'outcome'{scope}
            """,
            params,
        ).fetchone()[0]
    agrees_on_form = con.execute(
        f"""
        SELECT count(*) FROM conformed.endpoint_results
        WHERE result_kind = 'outcome' AND link_agrees_on_form{scope}
        """,
        params,
    ).fetchone()[0]
    return {
        "computed": True,
        "reported_outcomes": total + unconformed,
        "link_methods": mix,
        "conformed": total,
        "measurement_unmatched": unconformed,
        "linked_and_agrees_on_form": agrees_on_form,
    }


def _enumerations(
    con: duckdb.DuckDBPyConnection, nct_ids: Optional[list[str]] = None, *, top: int
) -> dict:
    """The value sets `param_type` and `dispersion_type` use, each with the
    kind results/dispersion.py folded it to."""
    if not _table_exists(con, "conformed", "endpoint_dispersion"):
        return {"computed": False}
    scope, params = _scoped(nct_ids)
    out: dict = {"computed": True}
    for field, kind_column in (
        ("param_type_raw", "param_kind"),
        ("dispersion_type_raw", "dispersion_kind"),
    ):
        rows = con.execute(
            f"""
            SELECT coalesce({field}, '(null)'), {kind_column}, count(*)
            FROM conformed.endpoint_dispersion WHERE TRUE{scope}
            GROUP BY 1, 2 ORDER BY 3 DESC
            """,
            params,
        ).fetchall()
        out[field] = [
            {"value": value, "kind": kind, "rows": count} for value, kind, count in rows[:top]
        ]
        out[f"{field}_distinct"] = len(rows)
        out[f"{field}_unrecognised"] = [
            {"value": value, "rows": count}
            for value, kind, count in rows
            if kind == "unknown"
        ][:top]
    out["skip_reasons"] = dict(
        con.execute(
            f"""
            SELECT coalesce(sd_skip_reason, '(none -- an SD was derived)'), count(*)
            FROM conformed.endpoint_dispersion WHERE TRUE{scope} GROUP BY 1 ORDER BY 2 DESC
            """,
            params,
        ).fetchall()
    )
    return out


def _units(
    con: duckdb.DuckDBPyConnection, nct_ids: Optional[list[str]] = None, *, top: int
) -> dict:
    if not _table_exists(con, "conformed", "endpoint_dispersion"):
        return {"computed": False}
    scope, params = _scoped(nct_ids)
    total, matched, convertible = con.execute(
        f"""
        SELECT count(*),
               count(*) FILTER (WHERE scale_match_method IS NOT NULL),
               count(*) FILTER (WHERE si_scale_id IS NOT NULL)
        FROM conformed.endpoint_dispersion WHERE TRUE{scope}
        """,
        params,
    ).fetchone()
    unmatched = con.execute(
        f"""
        SELECT coalesce(unit_raw, '(null)'), count(*)
        FROM conformed.endpoint_dispersion
        WHERE scale_match_method IS NULL{scope}
        GROUP BY 1 ORDER BY 2 DESC
        """,
        params,
    ).fetchall()
    return {
        "computed": True,
        "rows": total,
        "resolved": matched,
        "convertible_to_si": convertible,
        "unresolved_distinct": len(unmatched),
        "unresolved": [{"value": value, "rows": count} for value, count in unmatched[:top]],
    }


def _arms(con: duckdb.DuckDBPyConnection, nct_ids: Optional[list[str]] = None) -> dict:
    """How often a results group's title finds its protocol arm, and what
    role the linked ones carry. Counted over distinct (study, group title)
    pairs, and over the arm-level rows with a usable SD, which is what
    `stats --arm-role` stands on."""
    if not _table_exists(con, "conformed", "result_group_arm"):
        return {"computed": False}
    scope, params = _scoped(nct_ids)
    links = dict(
        con.execute(
            f"""
            SELECT coalesce(link_method, 'unlinked: ' || link_skip_reason), count(*)
            FROM conformed.result_group_arm WHERE TRUE{scope}
            GROUP BY 1 ORDER BY 2 DESC
            """,
            params,
        ).fetchall()
    )
    roles = dict(
        con.execute(
            f"""
            SELECT coalesce(arm_role, 'no_role') || ' (' || coalesce(role_source, '-') || ')',
                   count(*)
            FROM conformed.result_group_arm WHERE link_method IS NOT NULL{scope}
            GROUP BY 1 ORDER BY 2 DESC
            """,
            params,
        ).fetchall()
    )
    conflicts = con.execute(
        f"SELECT count(*) FROM conformed.result_group_arm WHERE role_conflict{scope}", params
    ).fetchone()[0]
    d_scope, d_params = _scoped(nct_ids, "d.nct_id")
    sd_rows, sd_rows_with_role = con.execute(
        f"""
        SELECT count(*), count(a.arm_role)
        FROM conformed.endpoint_dispersion d
        LEFT JOIN conformed.result_group_arm a
          ON a.nct_id = d.nct_id AND a.group_title IS NOT DISTINCT FROM d.group_title
        WHERE d.sd_estimate IS NOT NULL{d_scope}
        """,
        d_params,
    ).fetchone()
    return {
        "computed": True,
        "groups": sum(links.values()),
        "link_methods": links,
        "roles": roles,
        "role_conflicts": conflicts,
        "sd_rows": sd_rows,
        "sd_rows_with_role": sd_rows_with_role,
    }


def share(numerator: Optional[int], denominator: Optional[int]) -> Optional[float]:
    if not denominator:
        return None
    return (numerator or 0) / denominator
