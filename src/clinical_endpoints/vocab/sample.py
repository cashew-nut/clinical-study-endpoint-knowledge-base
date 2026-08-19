"""Export a human-reviewable sample of raw.design_outcomes vocabulary.

Produces a long-format CSV (field, value, frequency) -- one block per field,
each block sorted most-frequent-first -- so a reviewer can scan the terms
that matter most before wading into the long tail. This feeds the vocab
review conversation that builds vocab/*.yaml (build order step 2).
"""

from __future__ import annotations

import csv
from pathlib import Path

import duckdb

FIELDS = ("measure", "description", "time_frame")


def run_vocab_sample(
    con: duckdb.DuckDBPyConnection,
    *,
    limit: int = 500,
    out_path: Path | str = "vocab_review.csv",
) -> dict:
    """Write the distinct-value/frequency CSV to `out_path`; return summary counts.

    `limit` caps how many distinct values are kept *per field*, taking the
    most frequent ones first -- not a cap on total rows written.
    """
    field_counts: dict[str, int] = {}
    rows: list[tuple[str, str, int]] = []

    for field in FIELDS:
        values = con.execute(
            f"""
            SELECT trim({field}) AS value, count(*) AS frequency
            FROM raw.design_outcomes
            WHERE {field} IS NOT NULL AND trim({field}) != ''
            GROUP BY value
            ORDER BY frequency DESC, value ASC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        field_counts[field] = len(values)
        rows.extend((field, value, frequency) for value, frequency in values)

    out_path = Path(out_path)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(("field", "value", "frequency"))
        writer.writerows(rows)

    return {
        "out_path": str(out_path),
        "field_counts": field_counts,
        "row_count": len(rows),
    }
