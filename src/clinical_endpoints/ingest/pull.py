"""Filtered pull of AACT `studies` + `design_outcomes` into raw.* (plan §2/§10 step 1)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

import duckdb

# User-facing phase shorthand -> AACT's `studies.phase` enum values.
PHASE_ALIASES = {
    "1": "PHASE1",
    "2": "PHASE2",
    "3": "PHASE3",
    "4": "PHASE4",
    "1/2": "PHASE1/PHASE2",
    "2/3": "PHASE2/PHASE3",
    "na": "NA",
}

SOURCE_TABLES = ("studies", "design_outcomes")


def normalize_phases(phases: list[str]) -> list[str]:
    """Map CLI phase shorthand (e.g. "3", "1/2") onto AACT's `studies.phase` values."""
    normalized = []
    for raw in phases:
        key = raw.strip().lower()
        if key not in PHASE_ALIASES:
            valid = ", ".join(sorted(PHASE_ALIASES))
            raise ValueError(f"Unrecognized phase {raw!r}. Valid values: {valid}")
        normalized.append(PHASE_ALIASES[key])
    return normalized


@dataclass(frozen=True)
class PullFilters:
    phases: tuple[str, ...]
    limit: int
    since: date | None = None

    def as_dict(self) -> dict:
        return {
            "phases": list(self.phases),
            "limit": self.limit,
            "since": self.since.isoformat() if self.since else None,
        }


def run_pull(con: duckdb.DuckDBPyConnection, filters: PullFilters) -> dict:
    """Pull filtered studies + their design_outcomes from AACT into raw.*, log the pull.

    Idempotent: raw.studies / raw.design_outcomes are replaced wholesale on each
    run (a refresh, not an append), while raw._pull_log accumulates one row per
    invocation -- so re-running `pull` with the same filters is a refresh, and
    the pull history stays auditable.
    """
    aact_phases = normalize_phases(list(filters.phases))

    since_clause = "AND s.start_date >= ?" if filters.since else ""
    params: list = [aact_phases]
    if filters.since:
        params.append(filters.since)
    params.append(filters.limit)

    con.execute(
        f"""
        CREATE OR REPLACE TABLE raw.studies AS
        SELECT
            s.nct_id, s.phase, s.overall_status, s.study_type,
            s.start_date, s.primary_completion_date,
            s.brief_title, s.official_title
        FROM aact.ctgov.studies s
        WHERE s.phase = ANY(?)
        {since_clause}
        ORDER BY s.start_date DESC
        LIMIT ?
        """,
        params,
    )

    con.execute(
        """
        CREATE OR REPLACE TABLE raw.design_outcomes AS
        SELECT outcomes.*
        FROM aact.ctgov.design_outcomes AS outcomes
        JOIN raw.studies s USING (nct_id)
        """
    )

    row_counts = {
        "studies": con.execute("SELECT count(*) FROM raw.studies").fetchone()[0],
        "design_outcomes": con.execute("SELECT count(*) FROM raw.design_outcomes").fetchone()[0],
    }

    _ensure_pull_log(con)
    pull_id = str(uuid.uuid4())
    pulled_at = datetime.now(timezone.utc)
    con.execute(
        """
        INSERT INTO raw._pull_log (pull_id, pulled_at, filters_json, source_tables, row_counts)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            pull_id,
            pulled_at,
            json.dumps(filters.as_dict()),
            list(SOURCE_TABLES),
            json.dumps(row_counts),
        ],
    )

    return {"pull_id": pull_id, "pulled_at": pulled_at, "row_counts": row_counts}


def _ensure_pull_log(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS raw._pull_log (
            pull_id VARCHAR PRIMARY KEY,
            pulled_at TIMESTAMPTZ,
            filters_json JSON,
            source_tables VARCHAR[],
            row_counts JSON
        )
        """
    )
