"""DuckDB warehouse connection and AACT Postgres attach.

The warehouse is a single gitignored DuckDB file with four schemas:
raw / vocab / conformed / graph (see implementation plan §3).
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
from dotenv import load_dotenv

DEFAULT_WAREHOUSE_PATH = Path("warehouse.duckdb")
SCHEMAS = ("raw", "vocab", "conformed", "graph")

REQUIRED_AACT_ENV_VARS = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")


class MissingAactCredentialsError(RuntimeError):
    """Raised when AACT Postgres credentials are not available in the environment."""


def connect(warehouse_path: Path | str = DEFAULT_WAREHOUSE_PATH) -> duckdb.DuckDBPyConnection:
    """Open (creating if needed) the warehouse and ensure its schemas exist."""
    con = duckdb.connect(str(warehouse_path))
    for schema in SCHEMAS:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    return con


def attach_aact(con: duckdb.DuckDBPyConnection, *, alias: str = "aact") -> None:
    """Load `.env` (if present) and ATTACH the AACT Postgres database as `alias`.

    Credentials are read from the process environment (PGHOST/PGPORT/PGDATABASE/
    PGUSER/PGPASSWORD) by the postgres extension itself, following libpq
    conventions via an empty connection string -- they are never interpolated
    into SQL text.
    """
    load_dotenv()
    missing = [v for v in REQUIRED_AACT_ENV_VARS if not os.environ.get(v)]
    if missing:
        raise MissingAactCredentialsError(
            "Missing AACT credentials in environment: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill in your AACT read-only "
            "Postgres credentials (register at https://aact.ctti-clinicaltrials.org)."
        )

    con.execute("INSTALL postgres")
    con.execute("LOAD postgres")

    already_attached = {
        row[0] for row in con.execute("SELECT database_name FROM duckdb_databases()").fetchall()
    }
    if alias in already_attached:
        return
    con.execute(f"ATTACH '' AS {alias} (TYPE postgres, READ_ONLY)")
