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
            ('NCT005', 'secondary', '  ', 'Week 12', '', 'ITT'),
            ('NCT006', 'other', 'Time to Response', 'Event-driven', 'TTR', 'ITT')
        """
    )
    return con


@pytest.fixture
def big_con() -> duckdb.DuckDBPyConnection:
    """A bigger fixture: 3 measures with frequency >= 2, and 50 singleton measures."""
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
    rows = []
    nct = 0
    for i in range(3):
        for _ in range(5):
            nct += 1
            rows.append((f"NCT{nct:04d}", "primary", f"frequent_{i}", "Week 24", "d", "ITT"))
    for i in range(50):
        nct += 1
        rows.append((f"NCT{nct:04d}", "primary", f"singleton_{i}", "Week 24", "d", "ITT"))
    con.executemany(
        "INSERT INTO raw.design_outcomes VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    return con


# ------------------------------------------------------------- frequency format


def test_writes_long_format_csv_grouped_by_field(con, tmp_path):
    out_path = tmp_path / "vocab_review.csv"
    result = run_vocab_sample(con, out_path=out_path)

    with out_path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        field_column = [row[0] for row in reader]

    assert header == ["field", "value", "frequency"]
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

    # only the >= min_frequency block is guaranteed sorted; restrict to Overall
    # Survival (freq 3) vs everything else to avoid asserting order within the
    # randomly-sampled singleton tail.
    measure_rows = [r for r in rows if r["field"] == "measure"]
    assert measure_rows[0]["value"] == "Overall Survival"


def test_min_frequency_boundary_keeps_ge_uncapped_and_samples_tail(big_con, tmp_path):
    out_path = tmp_path / "vocab_review.csv"
    result = run_vocab_sample(
        big_con, out_path=out_path, min_frequency=2, singleton_sample=10, seed=1
    )

    with out_path.open(newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["field"] == "measure"]

    frequent_values = {r["value"] for r in rows if int(r["frequency"]) >= 2}
    assert frequent_values == {"frequent_0", "frequent_1", "frequent_2"}  # all kept, uncapped

    singleton_values = [r["value"] for r in rows if int(r["frequency"]) < 2]
    assert len(singleton_values) == 10  # capped at singleton_sample
    assert all(v.startswith("singleton_") for v in singleton_values)

    assert result["field_counts"]["measure"] == 13


def test_seeded_singleton_sample_is_reproducible(big_con, tmp_path):
    out1 = tmp_path / "run1.csv"
    out2 = tmp_path / "run2.csv"
    run_vocab_sample(big_con, out_path=out1, min_frequency=2, singleton_sample=10, seed=7)
    run_vocab_sample(big_con, out_path=out2, min_frequency=2, singleton_sample=10, seed=7)

    def singleton_values(path):
        with path.open(newline="", encoding="utf-8") as f:
            return [
                r["value"]
                for r in csv.DictReader(f)
                if r["field"] == "measure" and int(r["frequency"]) < 2
            ]

    assert singleton_values(out1) == singleton_values(out2)


def test_different_seeds_can_change_the_singleton_sample(big_con, tmp_path):
    out1 = tmp_path / "run1.csv"
    out2 = tmp_path / "run2.csv"
    run_vocab_sample(big_con, out_path=out1, min_frequency=2, singleton_sample=5, seed=1)
    run_vocab_sample(big_con, out_path=out2, min_frequency=2, singleton_sample=5, seed=999)

    def singleton_values(path):
        with path.open(newline="", encoding="utf-8") as f:
            return {
                r["value"]
                for r in csv.DictReader(f)
                if r["field"] == "measure" and int(r["frequency"]) < 2
            }

    assert singleton_values(out1) != singleton_values(out2)


def test_limit_still_applies_as_hard_cap_for_backwards_compatibility(big_con, tmp_path):
    out_path = tmp_path / "vocab_review.csv"
    result = run_vocab_sample(
        big_con, out_path=out_path, min_frequency=2, singleton_sample=50, seed=1, limit=4
    )
    assert result["field_counts"]["measure"] == 4


def test_coverage_sidecar_arithmetic(con, tmp_path):
    out_path = tmp_path / "vocab_review.csv"
    result = run_vocab_sample(con, out_path=out_path, min_frequency=2, singleton_sample=300)

    coverage_path = tmp_path / "vocab_review_coverage.csv"
    assert coverage_path.exists()
    assert result["coverage_path"] == str(coverage_path)

    with coverage_path.open(newline="", encoding="utf-8") as f:
        rows = {r["field"]: r for r in csv.DictReader(f)}

    measure = rows["measure"]
    # distinct measures (non-blank): Overall Survival, Objective Response Rate, Time to Response = 3
    assert int(measure["distinct_total"]) == 3
    assert int(measure["distinct_kept"]) == 3  # small fixture, nothing dropped
    assert int(measure["rows_total"]) == 5  # 3 + 1 + 1 non-blank measure rows
    assert int(measure["rows_covered"]) == 5
    assert float(measure["pct_rows_covered"]) == 100.0


def test_coverage_reflects_dropped_singletons(big_con, tmp_path):
    out_path = tmp_path / "vocab_review.csv"
    result = run_vocab_sample(
        big_con, out_path=out_path, min_frequency=2, singleton_sample=10, seed=1
    )
    measure_coverage = next(c for c in result["coverage"] if c["field"] == "measure")
    assert measure_coverage["distinct_total"] == 53  # 3 frequent + 50 singleton
    assert measure_coverage["distinct_kept"] == 13  # 3 frequent + 10 sampled
    assert measure_coverage["rows_total"] == 65  # 3*5 + 50*1
    assert measure_coverage["rows_covered"] == 25  # 3*5 + 10*1
    assert measure_coverage["pct_rows_covered"] == pytest.approx(25 / 65 * 100, abs=0.1)


def test_outcome_type_filter(con, tmp_path):
    out_path = tmp_path / "vocab_review.csv"
    result = run_vocab_sample(con, out_path=out_path, outcome_types=("other",))

    with out_path.open(newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["field"] == "measure"]
    assert {r["value"] for r in rows} == {"Time to Response"}
    assert result["field_counts"]["measure"] == 1


# ------------------------------------------------------------------ row format


def test_row_format_writes_joinable_csv(con, tmp_path):
    out_path = tmp_path / "vocab_review_rows.csv"
    result = run_vocab_sample(con, out_path=out_path, fmt="rows")

    with out_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == ["nct_id", "outcome_type", "measure", "time_frame", "description"]
        rows = list(reader)

    # all 6 design_outcomes rows land, including the NULL/blank one -- row
    # format is a sample of raw rows, not a filtered vocabulary
    assert len(rows) == 6
    assert result["row_count"] == 6
    assert {r["nct_id"] for r in rows} == {f"NCT00{i}" for i in range(1, 7)}


def test_row_format_respects_limit(con, tmp_path):
    out_path = tmp_path / "vocab_review_rows.csv"
    result = run_vocab_sample(con, out_path=out_path, fmt="rows", limit=2, seed=1)
    assert result["row_count"] == 2
    with out_path.open(newline="", encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == 2


def test_row_format_seeded_reproducibility(con, tmp_path):
    out1 = tmp_path / "rows1.csv"
    out2 = tmp_path / "rows2.csv"
    run_vocab_sample(con, out_path=out1, fmt="rows", limit=3, seed=5)
    run_vocab_sample(con, out_path=out2, fmt="rows", limit=3, seed=5)

    with out1.open(newline="", encoding="utf-8") as f:
        rows1 = [r["nct_id"] for r in csv.DictReader(f)]
    with out2.open(newline="", encoding="utf-8") as f:
        rows2 = [r["nct_id"] for r in csv.DictReader(f)]
    assert rows1 == rows2


def test_row_format_outcome_type_filter(con, tmp_path):
    out_path = tmp_path / "vocab_review_rows.csv"
    result = run_vocab_sample(con, out_path=out_path, fmt="rows", outcome_types=("secondary",))
    with out_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert result["row_count"] == 2
    assert {r["outcome_type"] for r in rows} == {"secondary"}


def test_invalid_format_raises():
    con = duckdb.connect(":memory:")
    for schema in SCHEMAS:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    with pytest.raises(ValueError, match="fmt must be"):
        run_vocab_sample(con, fmt="bogus")


# ------------------------------------------------------------------------ CLI


def test_cli_vocab_sample_writes_csv_and_coverage(tmp_path, monkeypatch):
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
    assert (tmp_path / "vocab_review_coverage.csv").exists()
    assert "Coverage" in result.output


def test_cli_vocab_sample_format_rows(tmp_path, monkeypatch):
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
        ["vocab", "sample", "--format", "rows", "--warehouse", str(warehouse_path), "--limit", "1000"],
    )

    assert result.exit_code == 0, result.output
    assert "Wrote 1 rows" in result.output
    assert (tmp_path / "vocab_review_rows.csv").exists()


def test_cli_vocab_sample_rejects_bad_format(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["vocab", "sample", "--format", "bogus"])
    assert result.exit_code == 1
    assert "--format must be" in result.output


def test_cli_vocab_sample_rejects_bad_outcome_type(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["vocab", "sample", "--outcome-type", "bogus"])
    assert result.exit_code == 1
    assert "--outcome-type values must be from" in result.output
