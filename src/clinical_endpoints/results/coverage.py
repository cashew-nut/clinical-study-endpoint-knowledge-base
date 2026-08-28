"""The gate, made measurable rather than assumed.

The roadmap this tier implements gated the whole of Tier 1 on one live pull,
measuring four numbers before any of it was designed in full:

1. what share of conformed studies have results posted;
2. what share of results-section outcome titles match the planned `measure`
   string exactly -- and what the conformance engine does with the rest;
3. the observed distribution of `dispersion_type` and `param_type`, and the
   exact value sets both fields use;
4. the share of results rows whose `unit_of_measure` normalises against
   `scales.yaml` as it stands.

That pull could not be run here: clinicaltrials.gov and AACT are both
unreachable from this project's build environment (the same constraint
`ingest/ctgov_api.py`'s CAVEAT records), and an egress policy is not something
code can work around. So the gate ships as a command instead of as a number.
Everything downstream of it was built to be measured rather than to assume a
measurement: the two enumerations are recognised from an open set and an
unrecognised value is reported rather than coerced, so running
`endpoints results coverage` after a real pull answers all four questions and
names exactly which strings the vocabulary is still missing.

This is not a substitute for the measurement. It is the instrument for taking
it.
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
    """The warehouse holds no results section to measure."""


def gate_measurements(con: duckdb.DuckDBPyConnection, *, top: int = 20) -> dict:
    """The four gate numbers, plus the exact value sets behind them."""
    if not _table_exists(con, "raw", "outcome_measures"):
        raise NoResults(
            "raw.outcome_measures is empty -- run `endpoints pull` (without --no-results) first"
        )

    return {
        "posting": _posting(con),
        "titles": _titles(con),
        "enumerations": _enumerations(con, top=top),
        "units": _units(con, top=top),
    }


def _posting(con: duckdb.DuckDBPyConnection) -> dict:
    """Gate 1. Three denominators, because they answer different questions:
    every study pulled, every study whose endpoints conformed, and every study
    whose results actually landed. The registry's own `has_results` flag is a
    claim about the record; `landed` is what this pipeline could parse out of
    it, and the two are not the same number."""
    pulled = con.execute("SELECT count(*) FROM raw.studies").fetchone()[0]
    flagged = con.execute(
        "SELECT count(*) FROM raw.studies WHERE has_results"
    ).fetchone()[0]
    landed = con.execute(
        "SELECT count(DISTINCT nct_id) FROM raw.outcome_measures"
    ).fetchone()[0]

    conformed = conformed_with_results = None
    if _table_exists(con, "conformed", "endpoints"):
        conformed = con.execute(
            "SELECT count(DISTINCT nct_id) FROM conformed.endpoints"
        ).fetchone()[0]
        conformed_with_results = con.execute(
            """
            SELECT count(DISTINCT e.nct_id) FROM conformed.endpoints e
            WHERE e.nct_id IN (SELECT nct_id FROM raw.outcome_measures)
            """
        ).fetchone()[0]

    return {
        "studies_pulled": pulled,
        "studies_flagged_has_results": flagged,
        "studies_with_results_landed": landed,
        "studies_conformed": conformed,
        "conformed_studies_with_results": conformed_with_results,
    }


def _titles(con: duckdb.DuckDBPyConnection) -> dict:
    """Gate 2. The link-method mix `results conform` recorded, which is the
    same question asked once the answer is a table rather than a guess."""
    if not _table_exists(con, "conformed", "endpoint_results"):
        return {"computed": False}
    mix = dict(
        con.execute(
            """
            SELECT coalesce(link_method, 'unlinked'), count(*)
            FROM conformed.endpoint_results WHERE result_kind = 'outcome'
            GROUP BY 1 ORDER BY 2 DESC
            """
        ).fetchall()
    )
    total = sum(mix.values())
    unconformed = 0
    if _table_exists(con, "conformed", "results_review_queue"):
        unconformed = con.execute(
            """
            SELECT count(*) FROM conformed.results_review_queue
            WHERE reason = 'measurement_unmatched' AND result_kind = 'outcome'
            """
        ).fetchone()[0]
    agrees_on_form = con.execute(
        """
        SELECT count(*) FROM conformed.endpoint_results
        WHERE result_kind = 'outcome' AND link_agrees_on_form
        """
    ).fetchone()[0]
    return {
        "computed": True,
        "reported_outcomes": total + unconformed,
        "link_methods": mix,
        "conformed": total,
        "measurement_unmatched": unconformed,
        "linked_and_agrees_on_form": agrees_on_form,
    }


def _enumerations(con: duckdb.DuckDBPyConnection, *, top: int) -> dict:
    """Gate 3. The exact value sets `param_type` and `dispersion_type` use in
    this warehouse, each with the kind `results/dispersion.py` folded it to --
    so the rows reading `unknown` are a to-do list for the next vocabulary
    round rather than an invisible loss."""
    if not _table_exists(con, "conformed", "endpoint_dispersion"):
        return {"computed": False}
    out: dict = {"computed": True}
    for field, kind_column in (
        ("param_type_raw", "param_kind"),
        ("dispersion_type_raw", "dispersion_kind"),
    ):
        rows = con.execute(
            f"""
            SELECT coalesce({field}, '(null)'), {kind_column}, count(*)
            FROM conformed.endpoint_dispersion
            GROUP BY 1, 2 ORDER BY 3 DESC
            """
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
            """
            SELECT coalesce(sd_skip_reason, '(none -- an SD was derived)'), count(*)
            FROM conformed.endpoint_dispersion GROUP BY 1 ORDER BY 2 DESC
            """
        ).fetchall()
    )
    return out


def _units(con: duckdb.DuckDBPyConnection, *, top: int) -> dict:
    """Gate 4. What share of `unit_of_measure` strings scales.yaml recognises
    as it stands today, and the exact strings it does not."""
    if not _table_exists(con, "conformed", "endpoint_dispersion"):
        return {"computed": False}
    total, matched, convertible = con.execute(
        """
        SELECT count(*),
               count(*) FILTER (WHERE scale_match_method IS NOT NULL),
               count(*) FILTER (WHERE si_scale_id IS NOT NULL)
        FROM conformed.endpoint_dispersion
        """
    ).fetchone()
    unmatched = con.execute(
        """
        SELECT coalesce(unit_raw, '(null)'), count(*)
        FROM conformed.endpoint_dispersion
        WHERE scale_match_method IS NULL
        GROUP BY 1 ORDER BY 2 DESC
        """
    ).fetchall()
    return {
        "computed": True,
        "rows": total,
        "resolved": matched,
        "convertible_to_si": convertible,
        "unresolved_distinct": len(unmatched),
        "unresolved": [{"value": value, "rows": count} for value, count in unmatched[:top]],
    }


def share(numerator: Optional[int], denominator: Optional[int]) -> Optional[float]:
    if not denominator:
        return None
    return (numerator or 0) / denominator
