"""Shared fixtures. `fake_aact_con` stands in for a real AACT ATTACH: it wires up
a `aact.ctgov.*` schema with the same shape as the tables `run_pull` queries,
so the ingestion SQL can be exercised without live AACT credentials.
"""

from __future__ import annotations

from pathlib import Path

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
            brief_title VARCHAR, official_title VARCHAR,
            enrollment INTEGER, enrollment_type VARCHAR
        )
        """
    )
    con.execute(
        """
        INSERT INTO aact.ctgov.studies VALUES
            ('NCT001', 'PHASE3', 'COMPLETED', 'INTERVENTIONAL', '2024-03-01', '2024-06-01', 'Trial A', 'Trial A official', 480, 'Actual'),
            ('NCT002', 'PHASE3', 'RECRUITING', 'INTERVENTIONAL', '2023-01-01', '2023-06-01', 'Trial B', 'Trial B official', 300, 'Estimated'),
            ('NCT003', 'PHASE1', 'COMPLETED', 'INTERVENTIONAL', '2024-01-01', '2024-06-01', 'Trial C', 'Trial C official', NULL, NULL)
        """
    )

    # ctgov.designs / ctgov.eligibilities / ctgov.design_groups -- the study-level
    # design and eligibility facts the USDM projection needs (see
    # ingest/design.py). NCT003 leaves them absent, to exercise the LEFT JOIN.
    con.execute(
        """
        CREATE TABLE aact.ctgov.designs (
            nct_id VARCHAR, allocation VARCHAR, intervention_model VARCHAR,
            primary_purpose VARCHAR, masking VARCHAR
        )
        """
    )
    con.execute(
        """
        INSERT INTO aact.ctgov.designs VALUES
            ('NCT001', 'Randomized', 'Parallel Assignment', 'Treatment', 'Double'),
            ('NCT002', 'Randomized', 'Crossover Assignment', 'Treatment', 'None (Open Label)')
        """
    )
    con.execute(
        """
        CREATE TABLE aact.ctgov.eligibilities (
            nct_id VARCHAR, gender VARCHAR, minimum_age VARCHAR, maximum_age VARCHAR,
            healthy_volunteers VARCHAR, population VARCHAR
        )
        """
    )
    con.execute(
        """
        INSERT INTO aact.ctgov.eligibilities VALUES
            ('NCT001', 'All', '18 Years', '75 Years', 'No', 'Adults with advanced NSCLC'),
            ('NCT002', 'Female', '18 Years', NULL, 'Accepts Healthy Volunteers', 'Adults with breast cancer')
        """
    )
    con.execute(
        """
        CREATE TABLE aact.ctgov.design_groups (
            nct_id VARCHAR, group_type VARCHAR, title VARCHAR, description VARCHAR
        )
        """
    )
    con.execute(
        """
        INSERT INTO aact.ctgov.design_groups VALUES
            ('NCT001', 'Experimental', 'Pembrolizumab', 'Pembrolizumab 200 mg Q3W'),
            ('NCT001', 'Active Comparator', 'Chemotherapy', 'Investigator choice'),
            ('NCT002', 'Experimental', 'Trastuzumab', 'Trastuzumab arm')
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


# --------------------------------------------------------------- USDM fixtures

USDM_STUDIES = [
    (
        "NCT00000001", "PHASE3", "COMPLETED", "INTERVENTIONAL", "2023-01-01", "2024-01-01",
        "A psoriasis trial", "A psoriasis trial, officially",
        "Parallel Assignment", "Treatment", "Randomized", "Double", 480, "Actual",
        False, "All", "18 Years", "75 Years", "Adults with moderate to severe plaque psoriasis",
    ),
    # Every design/eligibility column absent, to exercise the announced-placeholder
    # path in the wrapper envelope.
    (
        "NCT00000002", "PHASE3", "RECRUITING", "INTERVENTIONAL", "2023-06-01", "2025-01-01",
        "An oncology trial", "An oncology trial, officially",
        None, None, None, None, None, None, None, None, None, None, None,
    ),
    ("NCT00000003", "PHASE2", "COMPLETED", "INTERVENTIONAL", "2022-01-01", "2023-01-01",
     "A trial with no outcomes", "No outcomes", None, None, None, None, None, None,
     None, None, None, None, None),
]

USDM_OUTCOMES = [
    # templated: every dimension resolves
    ("NCT00000001", "primary", "Proportion of participants achieving PASI 75", "Week 16",
     "PASI 75 response", "ITT"),
    ("NCT00000001", "secondary", "Change from Baseline in HbA1c", "Week 24",
     "Glycaemic control", "ITT"),
    ("NCT00000001", "secondary", "Overall Survival", "Event-driven, up to 36 months", None, None),
    ("NCT00000001", "other", "Number of participants with treatment-emergent adverse events",
     "Up to Week 52", None, "Safety"),
    # verbatim: no measurement resolves, so `conform` routes it to the review queue
    ("NCT00000001", "other", "Sponsor internal reference code", "N/A", None, None),
    # AACT's title-case outcome_type vocabulary, which differs from ctgov_api's
    ("NCT00000002", "Primary", "Progression-Free Survival", "Up to 36 months", None, "ITT"),
    ("NCT00000002", "Other Pre-specified", "Change from baseline in FEV1",
     "Baseline, Week 12 and Week 24", None, "Safety"),
    ("NCT00000002", "Post-Hoc", "Overall Survival", "Up to 60 months", None, None),
]


@pytest.fixture(scope="session")
def usdm_warehouse_path(tmp_path_factory) -> str:
    """A warehouse with the vocabularies loaded, two trials pulled, and `conform`
    run -- the state every USDM projection assumes."""
    from clinical_endpoints.conform.pipeline import run_conform
    from clinical_endpoints.ingest.design import DESIGN_GROUPS_DDL, STUDIES_DDL
    from clinical_endpoints.ingest.pull_log import write_pull_log
    from clinical_endpoints.vocab.loader import default_vocab_dir, load_vocab, write_vocab_tables

    path = tmp_path_factory.mktemp("usdm") / "warehouse.duckdb"
    con = duckdb.connect(str(path))
    for schema in SCHEMAS:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

    vocab_dir = default_vocab_dir(Path(__file__).parent)
    write_vocab_tables(con, load_vocab(vocab_dir), vocab_dir=vocab_dir)

    con.execute(f"CREATE TABLE raw.studies ({STUDIES_DDL})")
    con.executemany(
        "INSERT INTO raw.studies VALUES (" + ", ".join(["?"] * len(USDM_STUDIES[0])) + ")",
        USDM_STUDIES,
    )
    con.execute(f"CREATE TABLE raw.design_groups ({DESIGN_GROUPS_DDL})")
    con.executemany(
        "INSERT INTO raw.design_groups VALUES (?, ?, ?, ?)",
        [
            ("NCT00000001", "Experimental", "Drug A", "Drug A 100 mg"),
            ("NCT00000001", "Placebo Comparator", "Placebo", "Matching placebo"),
        ],
    )
    con.execute(
        """
        CREATE TABLE raw.design_outcomes (
            nct_id VARCHAR, outcome_type VARCHAR, measure VARCHAR,
            time_frame VARCHAR, description VARCHAR, population VARCHAR
        )
        """
    )
    con.executemany("INSERT INTO raw.design_outcomes VALUES (?, ?, ?, ?, ?, ?)", USDM_OUTCOMES)
    # A warehouse only ever gets raw.* through a pull, so it always has the pull
    # log too. Without it the provenance path that reads raw._pull_log.pulled_at
    # -- a TIMESTAMPTZ, which duckdb can only hand back as a datetime if pytz is
    # importable -- never ran under test.
    write_pull_log(
        con,
        source="ctgov_api",
        filters={"phases": ["3"], "limit": 10},
        row_counts={"studies": len(USDM_STUDIES), "design_outcomes": len(USDM_OUTCOMES)},
        source_tables=("studies", "design_outcomes", "design_groups"),
    )
    run_conform(con)
    con.close()
    return str(path)


@pytest.fixture
def usdm_con(usdm_warehouse_path):
    con = duckdb.connect(usdm_warehouse_path, read_only=True)
    yield con
    con.close()
