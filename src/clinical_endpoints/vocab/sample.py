"""Export a human-reviewable sample of raw.design_outcomes vocabulary.

Two export shapes:

* `frequency` (default): a long-format CSV (field, value, frequency) -- one
  block per field, most-frequent-first. Every value occurring `--min-frequency`
  or more times is kept uncapped; the long tail below that threshold is
  represented by a seeded random `--singleton-sample`, not an alphabetical
  head, so the sample isn't silently biased toward "starts with A". A
  `<out>_coverage.csv` sidecar records, per field, what fraction of rows the
  kept values actually account for.
* `rows`: one row per `raw.design_outcomes` record (nct_id, outcome_type,
  measure, time_frame, description), seeded and optionally capped by
  `--limit`. This is what makes measure/time_frame/description co-occurrence
  reviewable -- the frequency blocks are independent per-field tables and
  can't answer "what measure had this time_frame".

This feeds the vocab review conversation that builds vocab/*.yaml (build
order step 2).
"""

from __future__ import annotations

import csv
from pathlib import Path

import duckdb

FIELDS = ("measure", "description", "time_frame")
OUTCOME_TYPES = ("primary", "secondary", "other")


def _outcome_type_clause(outcome_types: tuple[str, ...] | None) -> tuple[str, list]:
    if not outcome_types:
        return "", []
    return "AND outcome_type = ANY(?)", [list(outcome_types)]


def run_vocab_sample(
    con: duckdb.DuckDBPyConnection,
    *,
    min_frequency: int = 2,
    singleton_sample: int = 300,
    seed: int = 42,
    limit: int = 0,
    out_path: Path | str = "vocab_review.csv",
    fmt: str = "frequency",
    outcome_types: tuple[str, ...] | None = None,
) -> dict:
    """Write the vocab review export to `out_path`; return summary counts.

    `fmt="frequency"` (default) writes the per-field distinct-value/frequency
    CSV plus a coverage sidecar. `fmt="rows"` writes a joinable row-level
    sample instead. See module docstring for the shape of each.
    """
    if fmt == "rows":
        return _run_row_sample(con, seed=seed, limit=limit, out_path=out_path, outcome_types=outcome_types)
    if fmt != "frequency":
        raise ValueError(f"fmt must be 'frequency' or 'rows', got {fmt!r}")
    return _run_frequency_sample(
        con,
        min_frequency=min_frequency,
        singleton_sample=singleton_sample,
        seed=seed,
        limit=limit,
        out_path=out_path,
        outcome_types=outcome_types,
    )


def _run_frequency_sample(
    con: duckdb.DuckDBPyConnection,
    *,
    min_frequency: int,
    singleton_sample: int,
    seed: int,
    limit: int,
    out_path: Path | str,
    outcome_types: tuple[str, ...] | None,
) -> dict:
    out_path = Path(out_path)
    coverage_path = out_path.with_name(f"{out_path.stem}_coverage.csv")
    oc_clause, oc_params = _outcome_type_clause(outcome_types)

    rows: list[tuple[str, str, int]] = []
    coverage: list[dict] = []
    field_counts: dict[str, int] = {}

    for field in FIELDS:
        base_where = f"{field} IS NOT NULL AND trim({field}) != '' {oc_clause}"

        distinct_total, rows_total = con.execute(
            f"""
            SELECT count(*) AS distinct_total, coalesce(sum(frequency), 0) AS rows_total
            FROM (
                SELECT trim({field}) AS value, count(*) AS frequency
                FROM raw.design_outcomes
                WHERE {base_where}
                GROUP BY value
            )
            """,
            list(oc_params),
        ).fetchone()

        frequent = con.execute(
            f"""
            SELECT trim({field}) AS value, count(*) AS frequency
            FROM raw.design_outcomes
            WHERE {base_where}
            GROUP BY value
            HAVING count(*) >= ?
            ORDER BY frequency DESC, value ASC
            """,
            [*oc_params, min_frequency],
        ).fetchall()

        singleton = con.execute(
            f"""
            SELECT trim({field}) AS value, count(*) AS frequency
            FROM raw.design_outcomes
            WHERE {base_where}
            GROUP BY value
            HAVING count(*) < ?
            ORDER BY hash(value || '|' || CAST(? AS VARCHAR))
            LIMIT ?
            """,
            [*oc_params, min_frequency, seed, singleton_sample],
        ).fetchall()

        kept = frequent + singleton
        if limit and limit > 0:
            kept = kept[:limit]

        rows_covered = sum(frequency for _, frequency in kept)
        pct_rows_covered = round((rows_covered / rows_total * 100) if rows_total else 0.0, 1)

        field_counts[field] = len(kept)
        rows.extend((field, value, frequency) for value, frequency in kept)
        coverage.append(
            {
                "field": field,
                "distinct_total": distinct_total,
                "distinct_kept": len(kept),
                "rows_total": rows_total,
                "rows_covered": rows_covered,
                "pct_rows_covered": pct_rows_covered,
            }
        )

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(("field", "value", "frequency"))
        writer.writerows(rows)

    with coverage_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ("field", "distinct_total", "distinct_kept", "rows_total", "rows_covered", "pct_rows_covered")
        )
        for c in coverage:
            writer.writerow(
                (
                    c["field"],
                    c["distinct_total"],
                    c["distinct_kept"],
                    c["rows_total"],
                    c["rows_covered"],
                    c["pct_rows_covered"],
                )
            )

    return {
        "format": "frequency",
        "out_path": str(out_path),
        "coverage_path": str(coverage_path),
        "field_counts": field_counts,
        "row_count": len(rows),
        "coverage": coverage,
        "min_frequency": min_frequency,
        "singleton_sample": singleton_sample,
        "seed": seed,
        "limit": limit,
    }


def _run_row_sample(
    con: duckdb.DuckDBPyConnection,
    *,
    seed: int,
    limit: int,
    out_path: Path | str,
    outcome_types: tuple[str, ...] | None,
) -> dict:
    out_path = Path(out_path)
    oc_clause, oc_params = _outcome_type_clause(outcome_types)
    where_clause = f"WHERE {oc_clause[4:]}" if oc_clause else ""  # drop the leading "AND "

    limit_clause = "LIMIT ?" if limit and limit > 0 else ""
    params = [*oc_params, seed] + ([limit] if limit and limit > 0 else [])

    query = f"""
        SELECT nct_id, outcome_type, measure, time_frame, description
        FROM raw.design_outcomes
        {where_clause}
        ORDER BY hash(nct_id || '|' || outcome_type || '|' || coalesce(measure, '') || '|' || CAST(? AS VARCHAR))
        {limit_clause}
    """
    rows = con.execute(query, params).fetchall()

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(("nct_id", "outcome_type", "measure", "time_frame", "description"))
        writer.writerows(rows)

    return {
        "format": "rows",
        "out_path": str(out_path),
        "row_count": len(rows),
        "seed": seed,
        "limit": limit,
    }
