"""DuckDB warehouse connection and AACT Postgres attach.

The warehouse is a single DuckDB file with three schemas: raw, vocab, conformed.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Optional

import duckdb
import pyarrow as pa
from dotenv import load_dotenv

DEFAULT_WAREHOUSE_PATH = Path("warehouse.duckdb")
SCHEMAS = ("raw", "vocab", "conformed")

REQUIRED_AACT_ENV_VARS = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")


class MissingAactCredentialsError(RuntimeError):
    pass


class AactConnectionError(RuntimeError):
    pass


def connect(warehouse_path: Path | str = DEFAULT_WAREHOUSE_PATH) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(warehouse_path))
    for schema in SCHEMAS:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    return con


def attach_aact(con: duckdb.DuckDBPyConnection, *, alias: str = "aact") -> None:
    """ATTACH the AACT Postgres database as `alias`.

    Credentials come from the PG* environment variables (loaded from `.env` if
    present) via the postgres extension's libpq conventions; they are never
    interpolated into SQL.
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
            f"Could not connect to AACT Postgres at {host}:{port}: {exc}\n"
            f"Check that outbound TCP {port} is allowed (`nc -vz {host} {port}`) and that "
            "your AACT registration has been approved."
        ) from exc


def _infer_arrow_type(values: list) -> pa.DataType:
    """Arrow type from the first non-NULL value. `bool` before `int` because
    bool subclasses int. An all-NULL column falls back to string; DuckDB casts
    a NULL of any type to the target column."""
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            return pa.bool_()
        if isinstance(value, int):
            return pa.int64()
        if isinstance(value, float):
            return pa.float64()
        if isinstance(value, dt.datetime):
            return pa.timestamp("us")
        return pa.string()
    return pa.string()


def bulk_insert(
    con: duckdb.DuckDBPyConnection,
    table: str,
    columns: list[str],
    rows: list[tuple],
    *,
    on_conflict: Optional[str] = None,
) -> None:
    """Insert `rows` into `table` through an Arrow table. Scalar parameter
    binding is far slower in DuckDB's Python client when pandas is absent.

    `table`, `columns` and `on_conflict` are hardcoded strings, never user
    input.
    """
    if not rows:
        return
    columns_by_name = {name: [row[i] for row in rows] for i, name in enumerate(columns)}
    arrow_table = pa.table(
        {name: pa.array(values, type=_infer_arrow_type(values)) for name, values in columns_by_name.items()}
    )
    cols_sql = ", ".join(columns)
    sql = f"INSERT INTO {table} ({cols_sql}) SELECT {cols_sql} FROM arrow_table"
    if on_conflict:
        sql += f" {on_conflict}"
    con.execute(sql)
