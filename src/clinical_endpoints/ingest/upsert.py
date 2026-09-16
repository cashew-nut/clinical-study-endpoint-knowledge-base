"""Upsert helpers: a `pull` updates rows for the studies it landed and never
drops rows from earlier pulls.

`CREATE TABLE IF NOT EXISTS` does not check that an existing table has the
expected shape, so `ensure_table` reconciles the live table against the DDL
and migrates rows across. `--replace` is the one opt-in drop-and-recreate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import duckdb

from clinical_endpoints.db import bulk_insert

# Table and column names are literals in this codebase; checked anyway since
# they are interpolated into DDL.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_SCRATCH = "_ensure_table_expected"


class SchemaMigrationError(RuntimeError):
    """raw.<table> exists in a shape its rows cannot be carried across into.
    Raised instead of dropping the table."""


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    type: str
    primary_key: bool


@dataclass(frozen=True)
class SchemaChange:
    """What `ensure_table` did to bring a table up to its DDL, for the caller to report."""

    table: str
    added: tuple[str, ...] = ()
    dropped: tuple[str, ...] = ()
    retyped: tuple[str, ...] = ()
    added_key: tuple[str, ...] = ()
    rows_kept: int = 0
    rows_dropped: int = 0

    def describe(self) -> str:
        parts: list[str] = []
        if self.added:
            parts.append(f"added {_columns_phrase(self.added)}")
        if self.dropped:
            parts.append(f"dropped {_columns_phrase(self.dropped)}")
        if self.retyped:
            parts.append(f"retyped {', '.join(self.retyped)}")
        if self.added_key:
            parts.append(f"added PRIMARY KEY ({', '.join(self.added_key)})")
        summary = f"raw.{self.table}: {'; '.join(parts)}"
        summary += f" -- {self.rows_kept:,} row{'s' if self.rows_kept != 1 else ''} preserved"
        if self.rows_dropped:
            summary += f", {self.rows_dropped:,} dropped (duplicate or NULL key)"
        return summary


def _columns_phrase(names: tuple[str, ...]) -> str:
    shown = ", ".join(names[:4])
    if len(names) > 4:
        shown += f", +{len(names) - 4} more"
    return f"{len(names)} column{'s' if len(names) != 1 else ''} ({shown})"


def _check_identifier(name: str) -> str:
    if not _IDENTIFIER.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


def _table_exists(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM duckdb_tables() WHERE schema_name = 'raw' AND table_name = ?",
            [table],
        ).fetchone()
    )


def _columns(con: duckdb.DuckDBPyConnection, qualified: str) -> tuple[ColumnSpec, ...]:
    return tuple(
        ColumnSpec(name=row[1], type=row[2], primary_key=bool(row[5]))
        for row in con.execute(f"PRAGMA table_info('{qualified}')").fetchall()
    )


def _expected_columns(con: duckdb.DuckDBPyConnection, ddl: str) -> tuple[ColumnSpec, ...]:
    """The shape `ddl` describes, read back from a throwaway table rather than parsed."""
    con.execute(f"CREATE OR REPLACE TEMP TABLE {_SCRATCH} ({ddl})")
    try:
        return _columns(con, _SCRATCH)
    finally:
        con.execute(f"DROP TABLE IF EXISTS {_SCRATCH}")


def ensure_table(
    con: duckdb.DuckDBPyConnection, table: str, ddl: str, *, replace: bool = False
) -> SchemaChange | None:
    """Create raw.<table>, or bring an existing one up to `ddl`.

    `replace=True` drops the table first; that is deliberate data loss, not a
    migration, so it never produces a `SchemaChange`. Returns None when
    nothing had to change.
    """
    _check_identifier(table)
    if replace:
        con.execute(f"DROP TABLE IF EXISTS raw.{table}")
    if not _table_exists(con, table):
        con.execute(f"CREATE TABLE raw.{table} ({ddl})")
        return None

    expected = _expected_columns(con, ddl)
    actual = _columns(con, f"raw.{table}")
    if _shape(actual) == _shape(expected):
        return None
    return _migrate(con, table, ddl, actual=actual, expected=expected)


class SchemaReconciler:
    """Runs a pull's `ensure_table` calls and remembers the ones that migrated."""

    def __init__(self, con: duckdb.DuckDBPyConnection, *, replace: bool = False) -> None:
        self._con = con
        self._replace = replace
        self.changes: list[SchemaChange] = []

    def ensure(self, table: str, ddl: str) -> None:
        change = ensure_table(self._con, table, ddl, replace=self._replace)
        if change is not None:
            self.changes.append(change)


def _shape(columns: tuple[ColumnSpec, ...]) -> tuple[tuple[str, str, bool], ...]:
    return tuple((c.name, c.type, c.primary_key) for c in columns)


def _migrate(
    con: duckdb.DuckDBPyConnection,
    table: str,
    ddl: str,
    *,
    actual: tuple[ColumnSpec, ...],
    expected: tuple[ColumnSpec, ...],
) -> SchemaChange:
    actual_by_name = {c.name: c for c in actual}
    expected_by_name = {c.name: c for c in expected}
    key = tuple(c.name for c in expected if c.primary_key)
    actual_key = tuple(c.name for c in actual if c.primary_key)

    rows_before = con.execute(f"SELECT count(*) FROM raw.{table}").fetchone()[0]

    missing_key = [k for k in key if k not in actual_by_name]
    if missing_key and rows_before:
        raise SchemaMigrationError(
            f"raw.{table} has {rows_before:,} rows but no {', '.join(missing_key)} column, "
            f"so they cannot be keyed by the PRIMARY KEY ({', '.join(key)}) the current "
            f"schema declares. Inspect the table and drop it once you're satisfied "
            f"nothing in it is worth keeping, then re-run the pull."
        )

    select_exprs = []
    for column in expected:
        _check_identifier(column.name)
        source = column.name if column.name in actual_by_name else "NULL"
        select_exprs.append(f"CAST({source} AS {column.type}) AS {column.name}")

    # A table that did not carry this key may hold rows that violate it; keep
    # one row per key rather than letting the INSERT abort.
    predicate = ""
    if key and actual_key != key:
        predicate = " WHERE " + " AND ".join(f"{k} IS NOT NULL" for k in key)
        predicate += f" QUALIFY row_number() OVER (PARTITION BY {', '.join(key)}) = 1"

    staging = f"{table}__migrating"
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute(f"CREATE OR REPLACE TABLE raw.{staging} ({ddl})")
        con.execute(
            f"INSERT INTO raw.{staging} ({', '.join(c.name for c in expected)}) "
            f"SELECT {', '.join(select_exprs)} FROM raw.{table}{predicate}"
        )
        rows_kept = con.execute(f"SELECT count(*) FROM raw.{staging}").fetchone()[0]
        con.execute(f"DROP TABLE raw.{table}")
        con.execute(f"ALTER TABLE raw.{staging} RENAME TO {table}")
        con.execute("COMMIT")
    except duckdb.Error as exc:
        con.execute("ROLLBACK")
        raise SchemaMigrationError(
            f"raw.{table} could not be migrated to the current schema: {exc}. "
            f"The table is unchanged. Inspect it and drop it once you're satisfied "
            f"nothing in it is worth keeping, then re-run the pull."
        ) from exc

    return SchemaChange(
        table=table,
        added=tuple(c.name for c in expected if c.name not in actual_by_name),
        dropped=tuple(c.name for c in actual if c.name not in expected_by_name),
        retyped=tuple(
            f"{c.name} {actual_by_name[c.name].type}->{c.type}"
            for c in expected
            if c.name in actual_by_name and actual_by_name[c.name].type != c.type
        ),
        added_key=key if actual_key != key else (),
        rows_kept=rows_kept,
        rows_dropped=rows_before - rows_kept,
    )


def upsert_rows(
    con: duckdb.DuckDBPyConnection,
    table: str,
    columns: list[str],
    key_columns: list[str],
    rows: list[tuple],
) -> None:
    """Insert `rows` into raw.<table>, updating rows whose key already exists.
    Requires a PRIMARY KEY/UNIQUE constraint on `key_columns`."""
    if not rows:
        return
    # ON CONFLICT cannot update the same key twice in one statement;
    # deduplicate first, keeping the last occurrence.
    key_indexes = [columns.index(k) for k in key_columns]
    deduped: dict[tuple, tuple] = {}
    for row in rows:
        deduped[tuple(row[i] for i in key_indexes)] = row
    rows = list(deduped.values())

    update_cols = [c for c in columns if c not in key_columns]
    set_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
    on_conflict = f"ON CONFLICT ({', '.join(key_columns)}) DO UPDATE SET {set_clause}"
    bulk_insert(con, f"raw.{table}", columns, rows, on_conflict=on_conflict)


def replace_children(
    con: duckdb.DuckDBPyConnection,
    table: str,
    columns: list[str],
    key_column: str,
    key_values: list,
    rows: list[tuple],
) -> None:
    """Replace every row belonging to `key_values` in raw.<table> with `rows`."""
    if key_values:
        con.execute(f"DELETE FROM raw.{table} WHERE {key_column} = ANY(?)", [list(key_values)])
    bulk_insert(con, f"raw.{table}", columns, rows)
