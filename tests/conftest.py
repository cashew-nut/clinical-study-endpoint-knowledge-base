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
            enrollment INTEGER, enrollment_type VARCHAR,
            results_first_submitted_date DATE
        )
        """
    )
    con.execute(
        """
        INSERT INTO aact.ctgov.studies VALUES
            ('NCT001', 'PHASE3', 'COMPLETED', 'INTERVENTIONAL', '2024-03-01', '2024-06-01', 'Trial A', 'Trial A official', 480, 'Actual', '2025-01-15'),
            ('NCT002', 'PHASE3', 'RECRUITING', 'INTERVENTIONAL', '2023-01-01', '2023-06-01', 'Trial B', 'Trial B official', 300, 'Estimated', NULL),
            ('NCT003', 'PHASE1', 'COMPLETED', 'INTERVENTIONAL', '2024-01-01', '2024-06-01', 'Trial C', 'Trial C official', NULL, NULL, NULL)
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
    # One lead sponsor per study, plus one collaborator on NCT001 -- so tests
    # can confirm --org/`organization` only ever look at the lead row.
    con.execute(
        """
        CREATE TABLE aact.ctgov.sponsors (
            nct_id VARCHAR, agency_class VARCHAR, lead_or_collaborator VARCHAR, name VARCHAR
        )
        """
    )
    con.execute(
        """
        INSERT INTO aact.ctgov.sponsors VALUES
            ('NCT001', 'INDUSTRY', 'lead', 'Merck Sharp & Dohme LLC'),
            ('NCT001', 'NIH', 'collaborator', 'National Cancer Institute'),
            ('NCT002', 'INDUSTRY', 'lead', 'Genentech, Inc.'),
            ('NCT003', 'INDUSTRY', 'lead', 'AstraZeneca')
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

    _add_fake_aact_results(con)
    return con


def _add_fake_aact_results(con: duckdb.DuckDBPyConnection) -> None:
    """The results-section half of the fake AACT database (D4). Only NCT001
    posted results, so the fixture exercises both branches: a study whose
    results land, and two whose absence is not an error.

    The column lists here are AACT's documented ones. `ingest/aact_results.py`
    introspects rather than assuming them, and `test_aact_results.py` drops
    columns from these tables to exercise that.
    """
    con.execute(
        """
        CREATE TABLE aact.ctgov.outcomes (
            id INTEGER, nct_id VARCHAR, outcome_type VARCHAR, title VARCHAR,
            description VARCHAR, time_frame VARCHAR, population VARCHAR,
            units VARCHAR, units_analyzed VARCHAR, param_type VARCHAR, dispersion_type VARCHAR
        )
        """
    )
    con.execute(
        """
        INSERT INTO aact.ctgov.outcomes VALUES
            (1, 'NCT001', 'Primary', 'Progression-Free Survival', 'PFS by RECIST',
             'Event-driven', 'ITT', 'months', 'Participants', 'Median',
             '95% Confidence Interval'),
            (2, 'NCT001', 'Secondary', 'Change from Baseline in FEV1', 'Spirometry',
             'Week 12', 'ITT', 'L', 'Participants', 'Mean', 'Standard Deviation')
        """
    )
    con.execute(
        """
        CREATE TABLE aact.ctgov.result_groups (
            id INTEGER, nct_id VARCHAR, ctgov_group_code VARCHAR, result_type VARCHAR,
            title VARCHAR, description VARCHAR
        )
        """
    )
    con.execute(
        """
        INSERT INTO aact.ctgov.result_groups VALUES
            (10, 'NCT001', 'OG000', 'Outcome', 'Pembrolizumab', 'Pembrolizumab 200 mg Q3W'),
            (11, 'NCT001', 'OG001', 'Outcome', 'Chemotherapy', 'Investigator choice'),
            (12, 'NCT001', 'BG000', 'Baseline', 'Total', 'All participants')
        """
    )
    con.execute(
        """
        CREATE TABLE aact.ctgov.outcome_counts (
            id INTEGER, nct_id VARCHAR, outcome_id INTEGER, result_group_id INTEGER,
            ctgov_group_code VARCHAR, scope VARCHAR, units VARCHAR, count INTEGER
        )
        """
    )
    con.execute(
        """
        INSERT INTO aact.ctgov.outcome_counts VALUES
            (100, 'NCT001', 1, 10, 'OG000', 'Measure', 'Participants', 240),
            (101, 'NCT001', 1, 11, 'OG001', 'Measure', 'Participants', 238),
            (102, 'NCT001', 2, 10, 'OG000', 'Measure', 'Participants', 230),
            (103, 'NCT001', 2, 11, 'OG001', 'Measure', 'Participants', 225)
        """
    )
    con.execute(
        """
        CREATE TABLE aact.ctgov.outcome_measurements (
            id INTEGER, nct_id VARCHAR, outcome_id INTEGER, result_group_id INTEGER,
            ctgov_group_code VARCHAR, classification VARCHAR, category VARCHAR,
            title VARCHAR, units VARCHAR, param_type VARCHAR,
            param_value VARCHAR, param_value_num DOUBLE,
            dispersion_type VARCHAR, dispersion_value VARCHAR, dispersion_value_num DOUBLE,
            dispersion_lower_limit DOUBLE, dispersion_upper_limit DOUBLE,
            explanation_of_na VARCHAR
        )
        """
    )
    con.execute(
        """
        INSERT INTO aact.ctgov.outcome_measurements VALUES
            (200, 'NCT001', 1, 10, 'OG000', NULL, NULL, 'PFS', 'months', 'Median',
             '10.3', 10.3, '95% Confidence Interval', NULL, NULL, 8.1, 12.5, NULL),
            (201, 'NCT001', 1, 11, 'OG001', NULL, NULL, 'PFS', 'months', 'Median',
             '6.0', 6.0, '95% Confidence Interval', NULL, NULL, 4.8, 7.4, NULL),
            (202, 'NCT001', 2, 10, 'OG000', NULL, NULL, 'FEV1', 'L', 'Mean',
             '0.34', 0.34, 'Standard Deviation', '0.31', 0.31, NULL, NULL, NULL),
            (203, 'NCT001', 2, 11, 'OG001', NULL, NULL, 'FEV1', 'L', 'Mean',
             '0.11', 0.11, 'Standard Deviation', '0.29', 0.29, NULL, NULL, NULL)
        """
    )
    con.execute(
        """
        CREATE TABLE aact.ctgov.outcome_analyses (
            id INTEGER, nct_id VARCHAR, outcome_id INTEGER,
            non_inferiority_type VARCHAR, non_inferiority_description VARCHAR,
            param_type VARCHAR, param_value DOUBLE,
            dispersion_type VARCHAR, dispersion_value DOUBLE,
            p_value_modifier VARCHAR, p_value DOUBLE, p_value_description VARCHAR,
            ci_n_sides VARCHAR, ci_percent DOUBLE, ci_lower_limit DOUBLE, ci_upper_limit DOUBLE,
            method VARCHAR, method_description VARCHAR, estimate_description VARCHAR,
            groups_description VARCHAR, other_analysis_description VARCHAR
        )
        """
    )
    con.execute(
        """
        INSERT INTO aact.ctgov.outcome_analyses VALUES
            (300, 'NCT001', 1, 'Superiority', NULL, 'Hazard Ratio (HR)', 0.62,
             NULL, NULL, '<', 0.001, NULL, '2-Sided', 95, 0.49, 0.78,
             'Cox Proportional Hazard', NULL, NULL, 'Pembrolizumab vs Chemotherapy', NULL),
            (301, 'NCT001', 2, 'Non-Inferiority', 'NI margin of -0.10 L',
             'Mean Difference (Net)', 0.23, 'Standard Error', 0.04,
             '=', 0.021, NULL, '2-Sided', 95, 0.05, 0.41,
             'ANCOVA', NULL, NULL, 'Pembrolizumab vs Chemotherapy', NULL)
        """
    )
    con.execute(
        """
        CREATE TABLE aact.ctgov.outcome_analysis_groups (
            id INTEGER, nct_id VARCHAR, outcome_analysis_id INTEGER,
            result_group_id INTEGER, ctgov_group_code VARCHAR
        )
        """
    )
    con.execute(
        """
        INSERT INTO aact.ctgov.outcome_analysis_groups VALUES
            (400, 'NCT001', 300, 10, 'OG000'),
            (401, 'NCT001', 300, 11, 'OG001'),
            (402, 'NCT001', 301, 10, 'OG000'),
            (403, 'NCT001', 301, 11, 'OG001')
        """
    )
    con.execute(
        """
        CREATE TABLE aact.ctgov.baseline_measurements (
            id INTEGER, nct_id VARCHAR, result_group_id INTEGER, ctgov_group_code VARCHAR,
            classification VARCHAR, category VARCHAR, title VARCHAR, description VARCHAR,
            units VARCHAR, param_type VARCHAR, param_value VARCHAR, param_value_num DOUBLE,
            dispersion_type VARCHAR, dispersion_value VARCHAR, dispersion_value_num DOUBLE,
            dispersion_lower_limit DOUBLE, dispersion_upper_limit DOUBLE,
            explanation_of_na VARCHAR, number_analyzed INTEGER, number_analyzed_units VARCHAR
        )
        """
    )
    con.execute(
        """
        INSERT INTO aact.ctgov.baseline_measurements VALUES
            (500, 'NCT001', 12, 'BG000', NULL, NULL, 'FEV1', 'Baseline spirometry',
             'L', 'Mean', '1.82', 1.82, 'Standard Deviation', '0.44', 0.44,
             NULL, NULL, NULL, 478, 'Participants')
        """
    )


# --------------------------------------------------------------- USDM fixtures

USDM_STUDIES = [
    (
        "NCT00000001", "PHASE3", "COMPLETED", "INTERVENTIONAL", "2023-01-01", "2024-01-01",
        "A psoriasis trial", "A psoriasis trial, officially",
        "Parallel Assignment", "Treatment", "Randomized", "Double", 480, "Actual",
        False, "All", "18 Years", "75 Years", "Adults with moderate to severe plaque psoriasis",
        "Acme Pharmaceuticals", False,
    ),
    # Every design/eligibility column absent, to exercise the announced-placeholder
    # path in the wrapper envelope.
    (
        "NCT00000002", "PHASE3", "RECRUITING", "INTERVENTIONAL", "2023-06-01", "2025-01-01",
        "An oncology trial", "An oncology trial, officially",
        None, None, None, None, None, None, None, None, None, None, None, None, None,
    ),
    ("NCT00000003", "PHASE2", "COMPLETED", "INTERVENTIONAL", "2022-01-01", "2023-01-01",
     "A trial with no outcomes", "No outcomes", None, None, None, None, None, None,
     None, None, None, None, None, None, None),
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


# ------------------------------------------------------- results fixtures (D4-D9)

#: Two studies with posted results, built to exercise every branch of the
#: dispersion normaliser at once: a reported SD, a standard error, a confidence
#: interval around a mean and another around a median (which must be refused),
#: an inter-quartile range, a dispersion type the vocabulary does not
#: recognise, and a count-typed outcome that has no business in an SD library.
#: FEV1 is reported in L by one study and mL by the other, which is the case
#: `factor_to_si` exists for.
RESULTS_STUDIES = [
    ("NCT10000001", "PHASE3", "COMPLETED", "INTERVENTIONAL", "2021-01-01", "2023-01-01",
     "A respiratory trial", "A respiratory trial, officially",
     "Parallel Assignment", "Treatment", "Randomized", "Double", 480, "Actual",
     False, "All", "18 Years", "75 Years", "Adults with COPD", "Acme Pharmaceuticals", True),
    ("NCT10000002", "PHASE3", "COMPLETED", "INTERVENTIONAL", "2022-01-01", "2024-01-01",
     "Another respiratory trial", "Another respiratory trial, officially",
     "Parallel Assignment", "Treatment", "Randomized", "Double", 300, "Actual",
     False, "All", "18 Years", None, "Adults with COPD", "Beta Therapeutics", True),
    # Pulled, conformed, and posted nothing: the denominator `results coverage`
    # has to keep in view.
    ("NCT10000003", "PHASE3", "RECRUITING", "INTERVENTIONAL", "2023-01-01", None,
     "A trial with no results", "No results", None, None, None, None, None, None,
     None, None, None, None, None, None, False),
]

RESULTS_DESIGN_OUTCOMES = [
    ("NCT10000001", "primary", "Change from Baseline in FEV1", "Week 12", "Spirometry", "ITT"),
    ("NCT10000001", "secondary", "Overall Survival", "Up to 60 months", None, "ITT"),
    ("NCT10000001", "secondary", "Number of participants with adverse events", "Up to Week 52",
     None, "Safety"),
    ("NCT10000002", "primary", "Change from Baseline in FEV1", "Week 12", None, "ITT"),
    ("NCT10000003", "primary", "Change from Baseline in FEV1", "Week 12", None, "ITT"),
]

RESULTS_OUTCOME_MEASURES = [
    # exact title match to the planned row
    ("OM1", "NCT10000001", 0, "PRIMARY", "Change from Baseline in FEV1", "Spirometry",
     "Week 12", "ITT", "Mean", "Standard Deviation", "L", "Participants", "POSTED"),
    # a reworded title -> conformed_measurement link
    ("OM2", "NCT10000001", 1, "SECONDARY", "Time from randomisation to death from any cause",
     None, "Up to 60 months", "ITT", "Median", "95% Confidence Interval", "months",
     "Participants", "POSTED"),
    # a count-typed outcome: has a dispersion column and no business in an SD library
    ("OM3", "NCT10000001", 2, "SECONDARY", "Number of participants with adverse events", None,
     "Up to Week 52", "Safety", "Count of Participants", "Not Applicable", "Participants",
     "Participants", "POSTED"),
    # never registered -> unlinked
    ("OM4", "NCT10000001", 3, "OTHER_PRE_SPECIFIED", "Change from Baseline in SGRQ total score",
     None, "Week 12", "ITT", "Mean", "Standard Error", "units on a scale", "Participants",
     "POSTED"),
    # conforms nowhere -> the results review queue
    ("OM5", "NCT10000001", 4, "OTHER_PRE_SPECIFIED", "Sponsor internal reference code", None,
     "N/A", None, "Number", "Not Applicable", "count", None, "POSTED"),
    # mL rather than L, so the SI column has something to do
    ("OM6", "NCT10000002", 0, "PRIMARY", "Change from Baseline in FEV1", None, "Week 12", "ITT",
     "Mean", "Standard Deviation", "mL", "Participants", "POSTED"),
    # an IQR around a median -- Wan et al. territory
    ("OM7", "NCT10000002", 1, "SECONDARY", "Change from Baseline in FEV1", None, "Week 24", "ITT",
     "Median", "Inter-Quartile Range", "mL", "Participants", "POSTED"),
    # a dispersion type nothing in the vocabulary recognises
    ("OM8", "NCT10000002", 2, "SECONDARY", "Change from Baseline in FEV1", None, "Week 52", "ITT",
     "Mean", "Bootstrap Spread", "mL", "Participants", "POSTED"),
]

RESULTS_OUTCOME_GROUPS = [
    ("OM1", "NCT10000001", "OG000", 0, "Drug", "Drug arm", 240, "Participants"),
    ("OM1", "NCT10000001", "OG001", 1, "Placebo", "Placebo arm", 238, "Participants"),
    ("OM2", "NCT10000001", "OG000", 0, "Drug", None, 240, "Participants"),
    ("OM3", "NCT10000001", "OG000", 0, "Drug", None, 240, "Participants"),
    ("OM4", "NCT10000001", "OG000", 0, "Drug", None, 100, "Participants"),
    ("OM5", "NCT10000001", "OG000", 0, "Drug", None, 240, "Participants"),
    ("OM6", "NCT10000002", "OG000", 0, "Drug", None, 150, "Participants"),
    ("OM7", "NCT10000002", "OG000", 0, "Drug", None, 150, "Participants"),
    ("OM8", "NCT10000002", "OG000", 0, "Drug", None, 150, "Participants"),
]

RESULTS_OUTCOME_MEASUREMENTS = [
    # (outcome_id, nct_id, group_key, class, category, value, value_num,
    #  spread, spread_num, lower, upper, n, comment)
    ("OM1", "NCT10000001", "OG000", None, None, "0.34", 0.34, "0.31", 0.31, None, None, None, None),
    ("OM1", "NCT10000001", "OG001", None, None, "0.11", 0.11, "0.29", 0.29, None, None, None, None),
    ("OM2", "NCT10000001", "OG000", None, None, "18.4", 18.4, None, None, 15.1, 22.0, None, None),
    ("OM3", "NCT10000001", "OG000", None, None, "37", 37.0, None, None, None, None, None, None),
    ("OM4", "NCT10000001", "OG000", None, None, "-4.2", -4.2, "0.8", 0.8, None, None, None, None),
    ("OM5", "NCT10000001", "OG000", None, None, "1", 1.0, None, None, None, None, None, None),
    ("OM6", "NCT10000002", "OG000", None, None, "290", 290.0, "310", 310.0, None, None, None, None),
    ("OM7", "NCT10000002", "OG000", None, None, "250", 250.0, None, None, 90.0, 420.0, None, None),
    ("OM8", "NCT10000002", "OG000", None, None, "300", 300.0, "45", 45.0, None, None, None, None),
]

RESULTS_OUTCOME_ANALYSES = [
    ("AN1", "OM1", "NCT10000001", 0, '["OG000","OG001"]', "Drug vs Placebo",
     "Mean Difference (Net)", "0.23", 0.23, "Standard Error", "0.04", 0.04,
     "<0.001", 0.001, "<", None, 95.0, "2-Sided", 0.11, 0.35, "ANCOVA", None,
     False, "Superiority", None, None, None),
    ("AN2", "OM2", "NCT10000001", 0, '["OG000","OG001"]', "Drug vs Placebo",
     "Hazard Ratio (HR)", "0.62", 0.62, None, None, None,
     "0.021", 0.021, "=", None, 95.0, "2-Sided", 0.49, 0.78, "Cox Proportional Hazard", None,
     False, "Superiority", None, None, None),
    ("AN3", "OM6", "NCT10000002", 0, '["OG000"]', "Drug vs Placebo",
     "Mean Difference (Net)", "120", 120.0, None, None, None,
     "0.08", 0.08, "=", None, 95.0, "2-Sided", -15.0, 255.0, "ANCOVA", None,
     True, "Non-Inferiority", "Non-inferiority margin of -100 mL", None, None),
]

RESULTS_BASELINE_MEASUREMENTS = [
    # (baseline_id, nct_id, group_key, ordinal, title, description, population,
    #  class, category, unit, param_type, value, value_num, dispersion_type,
    #  spread, spread_num, lower, upper, n, group_title)
    ("BL1", "NCT10000001", "BG000", 0, "FEV1", "Baseline spirometry", None, None, None,
     "L", "Mean", "1.82", 1.82, "Standard Deviation", "0.44", 0.44, None, None, 478, "Total"),
    ("BL2", "NCT10000002", "BG000", 0, "FEV1", None, None, None, None,
     "mL", "Mean", "1760", 1760.0, "Standard Deviation", "460", 460.0, None, None, 150, "Total"),
]


@pytest.fixture(scope="session")
def results_warehouse_path(tmp_path_factory) -> str:
    """A warehouse with vocabularies loaded, three studies pulled with their
    results section, `conform` run and `results conform` run -- the state every
    D5-D9 test assumes."""
    from clinical_endpoints.conform.pipeline import run_conform
    from clinical_endpoints.ingest.design import STUDIES_DDL
    from clinical_endpoints.ingest.results import RESULTS_TABLES
    from clinical_endpoints.results.pipeline import run_results_conform
    from clinical_endpoints.vocab.loader import default_vocab_dir, load_vocab, write_vocab_tables

    path = tmp_path_factory.mktemp("results") / "warehouse.duckdb"
    con = duckdb.connect(str(path))
    for schema in SCHEMAS:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

    vocab_dir = default_vocab_dir(Path(__file__).parent)
    write_vocab_tables(con, load_vocab(vocab_dir), vocab_dir=vocab_dir)

    con.execute(f"CREATE TABLE raw.studies ({STUDIES_DDL})")
    con.executemany(
        "INSERT INTO raw.studies VALUES (" + ", ".join(["?"] * len(RESULTS_STUDIES[0])) + ")",
        RESULTS_STUDIES,
    )
    con.execute(
        """
        CREATE TABLE raw.design_outcomes (
            nct_id VARCHAR, outcome_type VARCHAR, measure VARCHAR,
            time_frame VARCHAR, description VARCHAR, population VARCHAR
        )
        """
    )
    con.executemany("INSERT INTO raw.design_outcomes VALUES (?, ?, ?, ?, ?, ?)", RESULTS_DESIGN_OUTCOMES)

    landed = {
        "outcome_measures": RESULTS_OUTCOME_MEASURES,
        "outcome_groups": RESULTS_OUTCOME_GROUPS,
        "outcome_measurements": RESULTS_OUTCOME_MEASUREMENTS,
        "outcome_analyses": RESULTS_OUTCOME_ANALYSES,
        "baseline_measurements": RESULTS_BASELINE_MEASUREMENTS,
    }
    for table, ddl, columns in RESULTS_TABLES:
        con.execute(f"CREATE TABLE raw.{table} ({ddl})")
        rows = landed[table]
        con.executemany(
            f"INSERT INTO raw.{table} VALUES (" + ", ".join(["?"] * len(columns)) + ")", rows
        )

    run_conform(con)
    run_results_conform(con)
    con.close()
    return str(path)


@pytest.fixture
def results_con(results_warehouse_path):
    con = duckdb.connect(results_warehouse_path, read_only=True)
    yield con
    con.close()
