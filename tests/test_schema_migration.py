"""raw.* tables outlive the code that created them.

Two schema generations have already shipped -- the pre-upsert
`CREATE OR REPLACE TABLE ... AS SELECT` (no PRIMARY KEY, whatever columns the
upstream query happened to return) and the declared DDL with a PRIMARY KEY --
and raw.studies then gained eleven design/eligibility columns. A warehouse
built by any of them has to keep working, so every generation is exercised here
against the DDL the current code declares.
"""

from __future__ import annotations

import duckdb
import pytest

from datetime import date

from clinical_endpoints.ingest.design import STUDIES_DDL, STUDY_COLUMNS
from clinical_endpoints.ingest.upsert import (
    SchemaMigrationError,
    SchemaReconciler,
    ensure_table,
    upsert_rows,
)

#: raw.studies exactly as the pre-upsert backend left it: eight columns, no
#: constraints, types inferred by the CTAS.
GENERATION_1_DDL = """
    CREATE TABLE raw.studies AS
    SELECT * FROM (VALUES
        ('NCT001', 'Phase 2', 'Completed', 'Interventional',
         DATE '2023-01-01', DATE '2024-01-01', 'brief one', 'official one'),
        ('NCT002', 'Phase 3', 'Recruiting', 'Interventional',
         DATE '2023-06-01', NULL, 'brief two', 'official two')
    ) t(nct_id, phase, overall_status, study_type,
        start_date, primary_completion_date, brief_title, official_title)
"""

#: raw.studies as the upsert release left it: the PRIMARY KEY, but none of the
#: design/eligibility columns the USDM projection needs.
GENERATION_2_DDL = """
    CREATE TABLE raw.studies (
        nct_id VARCHAR PRIMARY KEY, phase VARCHAR, overall_status VARCHAR, study_type VARCHAR,
        start_date DATE, primary_completion_date DATE,
        brief_title VARCHAR, official_title VARCHAR
    )
"""


@pytest.fixture
def raw_con():
    con = duckdb.connect()
    con.execute("CREATE SCHEMA raw")
    yield con
    con.close()


def columns_of(con, table="raw.studies"):
    return [row[1] for row in con.execute(f"PRAGMA table_info('{table}')").fetchall()]


def primary_key_of(con, table="raw.studies"):
    return [row[1] for row in con.execute(f"PRAGMA table_info('{table}')").fetchall() if row[5]]


def a_study_row(nct_id="NCT001", brief_title="brief one"):
    values = {
        "nct_id": nct_id, "phase": "Phase 2", "overall_status": "Completed",
        "study_type": "Interventional", "brief_title": brief_title,
        "intervention_model": "Parallel Assignment", "enrollment_count": 120,
        "healthy_volunteers": False,
    }
    return tuple(values.get(column) for column in STUDY_COLUMNS)


def test_fresh_warehouse_needs_no_migration(raw_con):
    assert ensure_table(raw_con, "studies", STUDIES_DDL) is None
    assert primary_key_of(raw_con) == ["nct_id"]


def test_matching_table_is_left_alone(raw_con):
    ensure_table(raw_con, "studies", STUDIES_DDL)
    assert ensure_table(raw_con, "studies", STUDIES_DDL) is None


def test_pre_upsert_table_gains_key_and_columns_keeping_its_rows(raw_con):
    raw_con.execute(GENERATION_1_DDL)

    change = ensure_table(raw_con, "studies", STUDIES_DDL)

    assert change is not None
    assert change.added_key == ("nct_id",)
    assert change.added == tuple(STUDY_COLUMNS[8:])
    assert change.rows_kept == 2
    assert change.rows_dropped == 0
    assert columns_of(raw_con) == list(STUDY_COLUMNS)
    assert raw_con.execute(
        "SELECT nct_id, brief_title, start_date FROM raw.studies ORDER BY nct_id"
    ).fetchall() == [
        ("NCT001", "brief one", date(2023, 1, 1)),
        ("NCT002", "brief two", date(2023, 6, 1)),
    ]


def test_migrated_table_accepts_the_upsert_that_used_to_fail(raw_con):
    """The reported failure: ON CONFLICT against a table with no PRIMARY KEY."""
    raw_con.execute(GENERATION_1_DDL)
    with pytest.raises(duckdb.BinderException):
        upsert_rows(raw_con, "studies", list(STUDY_COLUMNS), ["nct_id"], [a_study_row()])

    ensure_table(raw_con, "studies", STUDIES_DDL)

    upsert_rows(
        raw_con, "studies", list(STUDY_COLUMNS), ["nct_id"],
        [a_study_row(brief_title="updated"), a_study_row("NCT999", "new study")],
    )
    assert raw_con.execute(
        "SELECT nct_id, brief_title, enrollment_count FROM raw.studies ORDER BY nct_id"
    ).fetchall() == [
        ("NCT001", "updated", 120),
        ("NCT002", "brief two", None),
        ("NCT999", "new study", 120),
    ]


def test_post_upsert_table_gains_only_the_design_columns(raw_con):
    raw_con.execute(GENERATION_2_DDL)
    raw_con.execute(
        "INSERT INTO raw.studies VALUES ('NCT001','Phase 2','Completed','Interventional',NULL,NULL,'b','o')"
    )

    change = ensure_table(raw_con, "studies", STUDIES_DDL)

    assert change.added_key == ()  # it already had the key
    assert change.added == tuple(STUDY_COLUMNS[8:])
    assert change.rows_kept == 1
    assert change.describe() == (
        "raw.studies: added 13 columns (intervention_model, primary_purpose, allocation, "
        "masking, +9 more) -- 1 row preserved"
    )


def test_duplicate_and_null_keys_are_dropped_not_fatal(raw_con):
    """A table that never carried the key may hold rows that violate it. Losing
    those beats losing the table."""
    raw_con.execute(GENERATION_1_DDL)
    raw_con.execute(
        "INSERT INTO raw.studies VALUES "
        "('NCT001','Phase 2','Completed','Interventional',NULL,NULL,'duplicate','o'), "
        "(NULL,'Phase 1','Unknown','Interventional',NULL,NULL,'no key','o')"
    )

    change = ensure_table(raw_con, "studies", STUDIES_DDL)

    assert change.rows_kept == 2
    assert change.rows_dropped == 2
    assert "2 dropped (duplicate or NULL key)" in change.describe()
    assert [r[0] for r in raw_con.execute("SELECT nct_id FROM raw.studies ORDER BY nct_id").fetchall()] == [
        "NCT001",
        "NCT002",
    ]


def test_columns_the_ddl_no_longer_declares_are_dropped(raw_con):
    """AACT's design_outcomes leads with its own `id`, which the pre-upsert
    `SELECT outcomes.*` carried into raw.*."""
    raw_con.execute(
        "CREATE TABLE raw.design_outcomes AS SELECT 7 AS id, 'NCT001' AS nct_id, "
        "'primary' AS outcome_type, 'm' AS measure, 't' AS time_frame, "
        "'d' AS description, 'p' AS population"
    )

    change = ensure_table(
        raw_con,
        "design_outcomes",
        "nct_id VARCHAR, outcome_type VARCHAR, measure VARCHAR, "
        "time_frame VARCHAR, description VARCHAR, population VARCHAR",
    )

    assert change.dropped == ("id",)
    assert change.rows_kept == 1
    assert columns_of(raw_con, "raw.design_outcomes") == [
        "nct_id", "outcome_type", "measure", "time_frame", "description", "population",
    ]


def test_widened_column_type_is_recorded(raw_con):
    raw_con.execute("CREATE TABLE raw.studies AS SELECT 'NCT001' AS nct_id, 30 AS enrollment_count")

    change = ensure_table(raw_con, "studies", "nct_id VARCHAR, enrollment_count BIGINT")

    assert change.retyped == ("enrollment_count INTEGER->BIGINT",)
    assert raw_con.execute("SELECT enrollment_count FROM raw.studies").fetchone() == (30,)


def test_impossible_cast_leaves_the_table_untouched(raw_con):
    raw_con.execute("CREATE TABLE raw.studies AS SELECT 'NCT001' AS nct_id, 'not-a-date' AS start_date")

    with pytest.raises(SchemaMigrationError, match="could not be migrated"):
        ensure_table(raw_con, "studies", "nct_id VARCHAR PRIMARY KEY, start_date DATE")

    assert raw_con.execute("SELECT * FROM raw.studies").fetchall() == [("NCT001", "not-a-date")]
    assert [r[0] for r in raw_con.execute(
        "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'raw'"
    ).fetchall()] == ["studies"]


def test_rows_with_no_key_column_at_all_are_refused(raw_con):
    """Nothing sensible can key these rows, and which of them to lose is the
    operator's call, not this function's."""
    raw_con.execute("CREATE TABLE raw.studies AS SELECT 'brief' AS brief_title")

    with pytest.raises(SchemaMigrationError, match="no nct_id column"):
        ensure_table(raw_con, "studies", STUDIES_DDL)

    assert raw_con.execute("SELECT * FROM raw.studies").fetchall() == [("brief",)]


def test_empty_table_with_no_key_column_migrates_cleanly(raw_con):
    raw_con.execute("CREATE TABLE raw.studies (brief_title VARCHAR)")

    change = ensure_table(raw_con, "studies", STUDIES_DDL)

    assert change.rows_kept == 0
    assert columns_of(raw_con) == list(STUDY_COLUMNS)


def test_reconciler_collects_only_the_tables_it_changed(raw_con):
    raw_con.execute(GENERATION_1_DDL)
    schema = SchemaReconciler(raw_con)

    schema.ensure("studies", STUDIES_DDL)
    schema.ensure("conditions", "nct_id VARCHAR, name VARCHAR")

    assert [c.table for c in schema.changes] == ["studies"]


# --------------------------------------------------------------- --replace


def test_replace_empties_a_table_already_on_the_current_schema(raw_con):
    """`replace=True` isn't just "skip if already correct" -- even a table that
    already matches `ddl` and holds rows gets emptied, because the whole point
    is discarding whatever an earlier pull landed."""
    ensure_table(raw_con, "studies", STUDIES_DDL)
    upsert_rows(raw_con, "studies", list(STUDY_COLUMNS), ["nct_id"], [a_study_row()])
    assert raw_con.execute("SELECT count(*) FROM raw.studies").fetchone()[0] == 1

    change = ensure_table(raw_con, "studies", STUDIES_DDL, replace=True)

    assert change is None  # a deliberate wipe is not a migration
    assert raw_con.execute("SELECT count(*) FROM raw.studies").fetchone()[0] == 0
    assert columns_of(raw_con) == list(STUDY_COLUMNS)


def test_replace_discards_a_shape_mismatch_instead_of_migrating_it(raw_con):
    """A table replace would otherwise have had to migrate (generation 1: no
    key, eight columns) is instead just dropped -- no SchemaChange, no
    rows_kept/rows_dropped bookkeeping, because nothing was carried across."""
    raw_con.execute(GENERATION_1_DDL)
    assert raw_con.execute("SELECT count(*) FROM raw.studies").fetchone()[0] == 2

    change = ensure_table(raw_con, "studies", STUDIES_DDL, replace=True)

    assert change is None
    assert raw_con.execute("SELECT count(*) FROM raw.studies").fetchone()[0] == 0
    assert columns_of(raw_con) == list(STUDY_COLUMNS)
    assert primary_key_of(raw_con) == ["nct_id"]


def test_replace_on_a_table_that_does_not_exist_yet_just_creates_it(raw_con):
    assert ensure_table(raw_con, "studies", STUDIES_DDL, replace=True) is None
    assert columns_of(raw_con) == list(STUDY_COLUMNS)


def test_reconciler_replace_empties_every_table_it_ensures(raw_con):
    raw_con.execute(GENERATION_1_DDL)
    raw_con.execute("CREATE TABLE raw.conditions (nct_id VARCHAR, name VARCHAR)")
    raw_con.execute("INSERT INTO raw.conditions VALUES ('NCT001', 'Asthma')")

    schema = SchemaReconciler(raw_con, replace=True)
    schema.ensure("studies", STUDIES_DDL)
    schema.ensure("conditions", "nct_id VARCHAR, name VARCHAR")

    assert schema.changes == []  # replace never reports as a migration
    assert raw_con.execute("SELECT count(*) FROM raw.studies").fetchone()[0] == 0
    assert raw_con.execute("SELECT count(*) FROM raw.conditions").fetchone()[0] == 0


def test_unsafe_table_name_is_refused(raw_con):
    with pytest.raises(ValueError, match="unsafe SQL identifier"):
        ensure_table(raw_con, "studies; DROP SCHEMA raw", "nct_id VARCHAR")
