"""Shared fixtures. `fake_aact_con` stands in for a real AACT ATTACH: it wires up
a `aact.ctgov.*` schema with the same shape as the tables `run_pull` queries,
so the ingestion SQL can be exercised without live AACT credentials.
"""

from __future__ import annotations

import duckdb
import pytest

from clinical_endpoints.db import SCHEMAS


@pytest.fixture
def fake_aact_con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    for schema in SCHEMAS:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    con.execute("ATTACH ':memory:' AS aact")
    con.execute("CREATE SCHEMA aact.ctgov")

    con.execute(
        """
        CREATE TABLE aact.ctgov.studies (
            nct_id VARCHAR, phase VARCHAR, overall_status VARCHAR, study_type VARCHAR,
            start_date DATE, primary_completion_date DATE,
            brief_title VARCHAR, official_title VARCHAR
        )
        """
    )
    con.execute(
        """
        INSERT INTO aact.ctgov.studies VALUES
            ('NCT001', 'PHASE3', 'COMPLETED', 'INTERVENTIONAL', '2024-03-01', '2024-06-01', 'Trial A', 'Trial A official'),
            ('NCT002', 'PHASE3', 'RECRUITING', 'INTERVENTIONAL', '2023-01-01', '2023-06-01', 'Trial B', 'Trial B official'),
            ('NCT003', 'PHASE1', 'COMPLETED', 'INTERVENTIONAL', '2024-01-01', '2024-06-01', 'Trial C', 'Trial C official')
        """
    )

    con.execute(
        """
        CREATE TABLE aact.ctgov.design_outcomes (
            nct_id VARCHAR, outcome_type VARCHAR, measure VARCHAR,
            time_frame VARCHAR, description VARCHAR, population VARCHAR
        )
        """
    )
    con.execute(
        """
        INSERT INTO aact.ctgov.design_outcomes VALUES
            ('NCT001', 'primary', 'Progression-Free Survival', 'Event-driven', 'PFS by RECIST', 'ITT'),
            ('NCT002', 'primary', 'Objective Response Rate', 'Week 24', 'ORR', 'ITT'),
            ('NCT003', 'primary', 'Change from Baseline in FEV1', 'Week 12', 'Spirometry', 'ITT')
        """
    )
    return con
