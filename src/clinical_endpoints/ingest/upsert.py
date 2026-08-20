"""Shared helpers so a `pull` is an upsert, not a wholesale table replace:
repeated runs keep every previously-pulled study (and its children) intact,
touching only the rows for studies in *this* pull -- update where a row
already exists, insert where it doesn't, never drop rows for studies outside
the current pull."""

from __future__ import annotations

import duckdb


def ensure_table(con: duckdb.DuckDBPyConnection, table: str, ddl: str) -> None:
    """Create raw.<table> if it doesn't already exist -- never replaces it."""
    con.execute(f"CREATE TABLE IF NOT EXISTS raw.{table} ({ddl})")


def upsert_rows(
    con: duckdb.DuckDBPyConnection,
    table: str,
    columns: list[str],
    key_columns: list[str],
    rows: list[tuple],
) -> None:
    """Insert `rows` into raw.<table>, updating rows whose key already exists
    and inserting the rest. Rows already in the table whose key is not in
    `rows` are left untouched. Requires raw.<table> to have a PRIMARY KEY/
    UNIQUE constraint on `key_columns` (see `ensure_table`'s DDL)."""
    if not rows:
        return
    update_cols = [c for c in columns if c not in key_columns]
    set_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
    placeholders = ", ".join(["?"] * len(columns))
    sql = (
        f"INSERT INTO raw.{table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(key_columns)}) DO UPDATE SET {set_clause}"
    )
    con.executemany(sql, rows)


def replace_children(
    con: duckdb.DuckDBPyConnection,
    table: str,
    columns: list[str],
    key_column: str,
    key_values: list,
    rows: list[tuple],
) -> None:
    """Replace every row belonging to `key_values` (e.g. the nct_ids just
    pulled) in raw.<table> with `rows` -- a scoped delete-then-insert, so
    rows for keys outside `key_values` (studies from earlier pulls) are never
    touched."""
    if key_values:
        con.execute(f"DELETE FROM raw.{table} WHERE {key_column} = ANY(?)", [list(key_values)])
    if not rows:
        return
    placeholders = ", ".join(["?"] * len(columns))
    con.executemany(
        f"INSERT INTO raw.{table} ({', '.join(columns)}) VALUES ({placeholders})", rows
    )
