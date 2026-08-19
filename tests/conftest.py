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

    con.execute("CREATE TABLE aact.ctgov.conditions (nct_id VARCHAR, name VARCHAR)")
    con.execute(
        """
        INSERT INTO aact.ctgov.conditions VALUES
            ('NCT001', 'Non-Small Cell Lung Cancer'),
            ('NCT002', 'Breast Cancer'),
            ('NCT003', 'COPD')
        """
    )

    con.execute("CREATE TABLE aact.ctgov.browse_conditions (nct_id VARCHAR, mesh_term VARCHAR)")
    con.execute(
        """
        INSERT INTO aact.ctgov.browse_conditions VALUES
            ('NCT001', 'Carcinoma, Non-Small-Cell Lung'),
            ('NCT001', 'Lung Neoplasms'),
            ('NCT002', 'Breast Neoplasms'),
            ('NCT003', 'Pulmonary Disease, Chronic Obstructive')
        """
    )

    con.execute("CREATE TABLE aact.ctgov.browse_interventions (nct_id VARCHAR, mesh_term VARCHAR)")
    con.execute(
        """
        INSERT INTO aact.ctgov.browse_interventions VALUES
            ('NCT001', 'Pembrolizumab'),
            ('NCT002', 'Trastuzumab'),
            ('NCT003', 'Tiotropium')
        """
    )

    # AACT's mesh_terms table (present in the schema, but -- per the AACT data
    # dictionary checked for this project -- empty in the live database). The
    # fake here has zero rows too, to match `_pull_mesh_terms`'s degrade path.
    con.execute("CREATE TABLE aact.ctgov.mesh_terms (mesh_term VARCHAR, tree_number VARCHAR)")

    return con
