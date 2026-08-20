"""Shared helpers so a `pull` is an upsert, not a wholesale table replace:
repeated runs keep every previously-pulled study (and its children) intact,
touching only the rows for studies in *this* pull -- update where a row
already exists, insert where it doesn't, never drop rows for studies outside
the current pull.

That guarantee only holds if the live table still has the shape the pull code
expects, and `CREATE TABLE IF NOT EXISTS` does not check: a raw.* table created
by an earlier release keeps its original columns and constraints forever, and
the mismatch only surfaces at insert time as a binder error. Two such changes
have already shipped -- the switch from `CREATE OR REPLACE TABLE ... AS SELECT`
to a declared DDL with a PRIMARY KEY, and the design/eligibility columns
raw.studies gained for the USDM projection -- so `ensure_table` reconciles the
live table against the DDL instead, migrating the rows across.

Migration rather than a refresh is the point. Dropping raw.studies and
re-pulling would discard every study landed by an earlier pull with different
filters, which is exactly what upserting exists to prevent; a schema change is
not a reason to lose them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import duckdb

#: raw.* table and column names are literals in this codebase, never user input.
#: Checked anyway because these get interpolated into DDL, where a bound
#: parameter isn't available.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_SCRATCH = "_ensure_table_expected"


class SchemaMigrationError(RuntimeError):
    """raw.<table> exists in a shape its rows can't be carried across into.

    Raised instead of dropping the table: the rows in it came from a pull that
    may not be repeatable, so which of them to lose is the operator's call.
    """


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    type: str
    primary_key: bool


@dataclass(frozen=True)
class SchemaChange:
    """What `ensure_table` had to do to bring a table up to its DDL. Returned so
    callers can report it -- a migration rewrites the operator's warehouse, and
    doing that silently would hide both the dropped columns and the row loss."""

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
    """The shape `ddl` describes, read back from a throwaway table rather than
    parsed -- DuckDB is the authority on its own DDL, and a hand-rolled parser
    for it would be one more thing to keep in step."""
    con.execute(f"CREATE OR REPLACE TEMP TABLE {_SCRATCH} ({ddl})")
    try:
        return _columns(con, _SCRATCH)
    finally:
        con.execute(f"DROP TABLE IF EXISTS {_SCRATCH}")


def ensure_table(con: duckdb.DuckDBPyConnection, table: str, ddl: str) -> SchemaChange | None:
    """Create raw.<table>, or bring an existing one up to `ddl`.

    Returns None when nothing had to change (the overwhelmingly common case:
    one PRAGMA against a table that already matches), otherwise the
    `SchemaChange` describing the migration, for the caller to report.
    """
    _check_identifier(table)
    if not _table_exists(con, table):
        con.execute(f"CREATE TABLE raw.{table} ({ddl})")
        return None

    expected = _expected_columns(con, ddl)
    actual = _columns(con, f"raw.{table}")
    if _shape(actual) == _shape(expected):
        return None
    return _migrate(con, table, ddl, actual=actual, expected=expected)


class SchemaReconciler:
    """Runs a pull's `ensure_table` calls and remembers the ones that had to
    migrate, so `run_pull` can hand them back and the CLI can report them."""

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self._con = con
        self.changes: list[SchemaChange] = []

    def ensure(self, table: str, ddl: str) -> None:
        change = ensure_table(self._con, table, ddl)
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

    # A table that didn't carry this key may hold rows that violate it. Keep one
    # row per key rather than letting the INSERT abort: the duplicates are
    # re-pullable, the rest of the table may not be.
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
