"""Read matching.yaml's per-dimension cascade + provenance confidence floors
from vocab.* (never from the YAML directly -- see conform/__init__.py)."""

from __future__ import annotations

from dataclasses import dataclass

import duckdb


@dataclass(frozen=True)
class CascadeStep:
    field: str
    match_method: str


@dataclass(frozen=True)
class Cascade:
    steps: tuple[CascadeStep, ...]
    fallback: str  # e.g. 'not_stated', 'unspecified', 'review_queue'


def load_cascade(con: duckdb.DuckDBPyConnection, dimension: str) -> Cascade:
    rows = con.execute(
        "SELECT field, match_method, fallback_value FROM vocab.matching_cascade "
        "WHERE dimension = ? ORDER BY ordinal",
        [dimension],
    ).fetchall()
    steps = tuple(CascadeStep(field, method) for field, method, fallback in rows if fallback is None)
    fallbacks = [fallback for _f, _m, fallback in rows if fallback is not None]
    if len(fallbacks) != 1:
        raise ValueError(f"vocab.matching_cascade[{dimension}] must have exactly one fallback row, got {len(fallbacks)}")
    return Cascade(steps=steps, fallback=fallbacks[0])


def load_confidence_floor(con: duckdb.DuckDBPyConnection) -> dict[str, float]:
    return dict(con.execute("SELECT match_method, confidence FROM vocab.matching_confidence_floor").fetchall())
