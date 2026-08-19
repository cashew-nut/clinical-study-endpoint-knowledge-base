"""raw._pull_log: one row per `pull` invocation, shared by every ingestion backend.

Every pull is filtered and logged (source, filters, row counts), so re-running
`pull` with the same filters is a refresh, not a one-off script, and the pull
history stays auditable across backends.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import duckdb

# Kept for reference/backwards compatibility -- callers now pass the tables
# they actually landed via `source_tables`, since that differs by backend
# (e.g. AACT lands mesh_terms, the CT.gov API backend lands
# browse_condition_branches instead; see ingest/aact.py and ingest/ctgov_api.py).
SOURCE_TABLES = ("studies", "design_outcomes")


def ensure_pull_log(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS raw._pull_log (
            pull_id VARCHAR PRIMARY KEY,
            pulled_at TIMESTAMPTZ,
            source VARCHAR,
            filters_json JSON,
            source_tables VARCHAR[],
            row_counts JSON
        )
        """
    )


def write_pull_log(
    con: duckdb.DuckDBPyConnection,
    *,
    source: str,
    filters: dict,
    row_counts: dict,
    source_tables: tuple[str, ...] = SOURCE_TABLES,
) -> dict:
    ensure_pull_log(con)
    pull_id = str(uuid.uuid4())
    pulled_at = datetime.now(timezone.utc)
    con.execute(
        """
        INSERT INTO raw._pull_log (pull_id, pulled_at, source, filters_json, source_tables, row_counts)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [pull_id, pulled_at, source, json.dumps(filters), list(source_tables), json.dumps(row_counts)],
    )
    return {"pull_id": pull_id, "pulled_at": pulled_at}
