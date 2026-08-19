from __future__ import annotations

import csv

import duckdb
import pytest
from typer.testing import CliRunner

from clinical_endpoints.cli.main import app
from clinical_endpoints.db import SCHEMAS
from clinical_endpoints.vocab.sample import run_vocab_sample

runner = CliRunner()


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    for schema in SCHEMAS:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    con.execute(
        """
        CREATE TABLE raw.design_outcomes (
            nct_id VARCHAR, outcome_type VARCHAR, measure VARCHAR,
            time_frame VARCHAR, description VARCHAR, population VARCHAR
        )
        """
    )
    con.execute(
        """
        INSERT INTO raw.design_outcomes VALUES
            ('NCT001', 'primary', 'Overall Survival', 'Week 24', 'OS from randomization', 'ITT'),
            ('NCT002', 'primary', 'Overall Survival', 'Week 52', 'OS from randomization', 'ITT'),
            ('NCT003', 'primary', 'Overall Survival', '  Week 24  ', 'OS', 'ITT'),
            ('NCT004', 'secondary', 'Objective Response Rate', 'Week 12', NULL, 'ITT'),
            ('NCT005', 'secondary', '  ', 'Week 12', '', 'ITT')
        """
    )
    return con


def test_writes_long_format_csv_grouped_by_field(con, tmp_path):
    out_path = tmp_path / "vocab_review.csv"
    result = run_vocab_sample(con, out_path=out_path)

    with out_path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        field_column = [row[0] for row in reader]

    assert header == ["field", "value", "frequency"]
    # each field's rows form one contiguous block, in a fixed field order
    seen_order = list(dict.fromkeys(field_column))
    assert seen_order == ["measure", "description", "time_frame"]
    assert result["out_path"] == str(out_path)
    assert result["row_count"] == len(field_column)


def test_dedupes_trims_and_counts_frequency(con, tmp_path):
    out_path = tmp_path / "vocab_review.csv"
    run_vocab_sample(con, out_path=out_path)

    with out_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    measure_rows = {r["value"]: int(r["frequency"]) for r in rows if r["field"] == "measure"}
    assert measure_rows["Overall Survival"] == 3
    assert measure_rows["Objective Response Rate"] == 1


def test_trims_whitespace_and_drops_null_or_blank_values(con, tmp_path):
    out_path = tmp_path / "vocab_review.csv"
    run_vocab_sample(con, out_path=out_path)

    with out_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    time_frame_values = {r["value"] for r in rows if r["field"] == "time_frame"}
    assert "Week 24" in time_frame_values
    assert "  Week 24  " not in time_frame_values  # trimmed, merged with "Week 24"

    description_values = {r["value"] for r in rows if r["field"] == "description"}
    assert "" not in description_values  # NULL and blank descriptions are dropped

    measure_values = {r["value"] for r in rows if r["field"] == "measure"}
    assert "" not in measure_values and "  " not in measure_values


def test_sorted_most_frequent_first_within_each_field(con, tmp_path):
    out_path = tmp_path / "vocab_review.csv"
    run_vocab_sample(con, out_path=out_path)

    with out_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    measure_freqs = [int(r["frequency"]) for r in rows if r["field"] == "measure"]
    assert measure_freqs == sorted(measure_freqs, reverse=True)


def test_limit_caps_distinct_values_per_field(con, tmp_path):
    out_path = tmp_path / "vocab_review.csv"
    result = run_vocab_sample(con, limit=1, out_path=out_path)

    assert result["field_counts"] == {"measure": 1, "description": 1, "time_frame": 1}
    assert result["row_count"] == 3


def test_cli_vocab_sample_writes_csv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    warehouse_path = tmp_path / "warehouse.duckdb"

    from clinical_endpoints.db import connect

    con = connect(warehouse_path)
    con.execute(
        """
        CREATE TABLE raw.design_outcomes (
            nct_id VARCHAR, outcome_type VARCHAR, measure VARCHAR,
            time_frame VARCHAR, description VARCHAR, population VARCHAR
        )
        """
    )
    con.execute(
        """
        INSERT INTO raw.design_outcomes VALUES
            ('NCT001', 'primary', 'Overall Survival', 'Week 24', 'OS', 'ITT')
        """
    )
    con.close()

    result = runner.invoke(
        app,
        [
            "vocab",
            "sample",
            "--warehouse",
            str(warehouse_path),
            "--out",
            str(tmp_path / "vocab_review.csv"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Wrote 3 rows" in result.output
    assert (tmp_path / "vocab_review.csv").exists()
