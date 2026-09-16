"""raw._pull_log: one row per `pull` invocation (source, filters, row counts)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import duckdb

from clinical_endpoints.ingest.upsert import ensure_table

SOURCE_TABLES = ("studies", "design_outcomes")


PULL_LOG_DDL = """
    pull_id VARCHAR PRIMARY KEY,
    pulled_at TIMESTAMPTZ,
    source VARCHAR,
    filters_json JSON,
    source_tables VARCHAR[],
    row_counts JSON
"""


def ensure_pull_log(con: duckdb.DuckDBPyConnection) -> None:
    ensure_table(con, "_pull_log", PULL_LOG_DDL)


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
