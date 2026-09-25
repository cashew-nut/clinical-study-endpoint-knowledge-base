"""Export a human-reviewable sample of raw.design_outcomes vocabulary.

* `frequency` (default): a long-format CSV (field, value, frequency). Values
  occurring `--min-frequency` or more times are kept uncapped; the tail is a
  seeded random `--singleton-sample`. A `<out>_coverage.csv` sidecar records
  what fraction of rows the kept values account for.
* `rows`: one row per `raw.design_outcomes` record, seeded and optionally
  capped, for reviewing measure/time_frame/description co-occurrence.
"""

from __future__ import annotations

import csv
from pathlib import Path

import duckdb

FIELDS = ("measure", "description", "time_frame")
OUTCOME_TYPES = ("primary", "secondary", "other")


def _row_filter_clause(
    outcome_types: tuple[str, ...] | None, nct_ids: list[str] | None = None
) -> tuple[str, list]:
    clause, params = "", []
    if outcome_types:
        clause += " AND outcome_type = ANY(?)"
        params.append(list(outcome_types))
    if nct_ids is not None:
        clause += " AND nct_id = ANY(?)"
        params.append(list(nct_ids))
    return clause, params


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
    nct_ids: list[str] | None = None,
) -> dict:
    """`nct_ids` restricts the sample to those studies; None means every study."""
    if fmt == "rows":
        return _run_row_sample(
            con, seed=seed, limit=limit, out_path=out_path, outcome_types=outcome_types,
            nct_ids=nct_ids,
        )
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
        nct_ids=nct_ids,
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
    nct_ids: list[str] | None = None,
) -> dict:
    out_path = Path(out_path)
    coverage_path = out_path.with_name(f"{out_path.stem}_coverage.csv")
    oc_clause, oc_params = _row_filter_clause(outcome_types, nct_ids)

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
    nct_ids: list[str] | None = None,
) -> dict:
    out_path = Path(out_path)
    oc_clause, oc_params = _row_filter_clause(outcome_types, nct_ids)
    where_clause = f"WHERE {oc_clause[len(' AND '):]}" if oc_clause else ""

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
