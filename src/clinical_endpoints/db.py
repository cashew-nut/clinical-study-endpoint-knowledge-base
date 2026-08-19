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


class AactConnectionError(RuntimeError):
    """Raised when the ATTACH to AACT's Postgres instance fails (network/auth)."""


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

    try:
        con.execute(f"ATTACH '' AS {alias} (TYPE postgres, READ_ONLY)")
    except duckdb.Error as exc:
        host = os.environ.get("PGHOST")
        port = os.environ.get("PGPORT")
        raise AactConnectionError(
            f"Could not connect to AACT Postgres at {host}:{port}: {exc}\n\n"
            "Credentials loaded correctly (host/port resolved above), so this is "
            "almost always a network reachability issue rather than a bug in this "
            "tool -- e.g. a corporate firewall/VPN blocking outbound TCP 5432 "
            "(many networks only allow 80/443 out). To narrow it down:\n"
            f"  1. `nc -vz {host} {port}` (or `telnet {host} {port}`) -- if that "
            "also hangs/fails, it's network-level, not this tool.\n"
            f"  2. Try `psql -h {host} -p {port} -U $PGUSER -d $PGDATABASE` directly, "
            "if you have psql installed.\n"
            "  3. Try a different network (e.g. a phone hotspot) to rule out a "
            "local firewall/VPN blocking port 5432 outbound.\n"
            "  4. Confirm your AACT registration is fully approved -- check for a "
            "confirmation email, not just the signup form."
        ) from exc
