"""D4: the results section lands in the same raw.outcome_* shape from both
backends. The tests that matter most here are the ones that pin the two
backends to each other -- the whole point of the shape is that nothing
downstream can tell which one a row came from."""

from __future__ import annotations

import json

import duckdb
import pytest

from clinical_endpoints.db import SCHEMAS
from clinical_endpoints.ingest import aact as aact_backend
from clinical_endpoints.ingest import ctgov_api
from clinical_endpoints.ingest.filters import PullFilters
from clinical_endpoints.ingest.results import (
    RESULTS_TABLES,
    baseline_id,
    extract_ctgov_results,
    is_non_inferiority,
    outcome_id,
    split_p_value,
    to_number,
)


def _study_with_results(nct_id: str = "NCT9001") -> dict:
    """A study record shaped like CT.gov API v2's, with a results section.

    Deliberately mixed: one continuous outcome reporting mean +/- SD, one
    time-to-event outcome reporting a median with a 95% CI, an analysis of
    each, and one baseline characteristic.
    """
    return {
        "hasResults": True,
        "protocolSection": {
            "identificationModule": {"nctId": nct_id, "briefTitle": "brief"},
            "statusModule": {"overallStatus": "COMPLETED", "startDateStruct": {"date": "2023-01-01"}},
            "designModule": {"phases": ["PHASE3"], "studyType": "INTERVENTIONAL"},
            "outcomesModule": {
                "primaryOutcomes": [
                    {"measure": "Progression-Free Survival", "timeFrame": "Event-driven"}
                ]
            },
        },
        "resultsSection": {
            "outcomeMeasuresModule": {
                "outcomeMeasures": [
                    {
                        "type": "PRIMARY",
                        "title": "Progression-Free Survival",
                        "description": "PFS by RECIST 1.1",
                        "timeFrame": "Event-driven",
                        "populationDescription": "ITT",
                        "paramType": "MEDIAN",
                        "dispersionType": "95% Confidence Interval",
                        "unitOfMeasure": "months",
                        "typeUnitsAnalyzed": "Participants",
                        "reportingStatus": "POSTED",
                        "groups": [
                            {"id": "OG000", "title": "Drug", "description": "Drug arm"},
                            {"id": "OG001", "title": "Placebo", "description": "Placebo arm"},
                        ],
                        "denoms": [
                            {
                                "units": "Participants",
                                "counts": [
                                    {"groupId": "OG000", "value": "240"},
                                    {"groupId": "OG001", "value": "238"},
                                ],
                            }
                        ],
                        "classes": [
                            {
                                "categories": [
                                    {
                                        "measurements": [
                                            {
                                                "groupId": "OG000", "value": "10.3",
                                                "lowerLimit": "8.1", "upperLimit": "12.5",
                                            },
                                            {
                                                "groupId": "OG001", "value": "6.0",
                                                "lowerLimit": "4.8", "upperLimit": "7.4",
                                            },
                                        ]
                                    }
                                ]
                            }
                        ],
                        "analyses": [
                            {
                                "paramType": "Hazard Ratio (HR)",
                                "paramValue": "0.62",
                                "pValue": "<0.001",
                                "ciPctValue": "95",
                                "ciNumSides": "2-Sided",
                                "ciLowerLimit": "0.49",
                                "ciUpperLimit": "0.78",
                                "statisticalMethod": "Cox Proportional Hazard",
                                "groupIds": ["OG000", "OG001"],
                                "nonInferiorityType": "Superiority",
                            }
                        ],
                    },
                    {
                        "type": "SECONDARY",
                        "title": "Change from Baseline in FEV1",
                        "timeFrame": "Week 12",
                        "paramType": "MEAN",
                        "dispersionType": "Standard Deviation",
                        "unitOfMeasure": "L",
                        "groups": [{"id": "OG000", "title": "Drug"}],
                        "denoms": [
                            {"units": "Participants", "counts": [{"groupId": "OG000", "value": "230"}]}
                        ],
                        "classes": [
                            {
                                "categories": [
                                    {
                                        "measurements": [
                                            {"groupId": "OG000", "value": "0.34", "spread": "0.31"}
                                        ]
                                    }
                                ]
                            }
                        ],
                        "analyses": [
                            {
                                "paramType": "Mean Difference (Net)",
                                "paramValue": "0.23",
                                "pValue": "0.021",
                                "nonInferiority": True,
                                "nonInferiorityType": "Non-Inferiority",
                                "nonInferiorityComment": "NI margin of -0.10 L",
                                "groupIds": ["OG000"],
                            }
                        ],
                    },
                ]
            },
            "baselineCharacteristicsModule": {
                "groups": [{"id": "BG000", "title": "Total"}],
                "denoms": [
                    {"units": "Participants", "counts": [{"groupId": "BG000", "value": "478"}]}
                ],
                "measures": [
                    {
                        "title": "FEV1",
                        "unitOfMeasure": "L",
                        "paramType": "MEAN",
                        "dispersionType": "Standard Deviation",
                        "classes": [
                            {
                                "categories": [
                                    {
                                        "measurements": [
                                            {"groupId": "BG000", "value": "1.82", "spread": "0.44"}
                                        ]
                                    }
                                ]
                            }
                        ],
                    }
                ],
            },
        },
    }


# ----------------------------------------------------------- value coercion


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("12.4", 12.4),
        ("-0.03", -0.03),
        ("1,204", 1204.0),
        (7, 7.0),
        ("NA", None),
        ("<0.001", None),
        ("99.9%", None),
        ("", None),
        (None, None),
        (True, None),
    ],
)
def test_to_number_keeps_only_plain_numbers(raw, expected):
    assert to_number(raw) == expected


def test_split_p_value_keeps_the_comparator_and_the_bound():
    assert split_p_value("<0.001") == ("<0.001", "<", 0.001)
    assert split_p_value("0.021") == ("0.021", "=", 0.021)
    # An equality is written without a redundant "=", so the API's bare number
    # and AACT's ("=", number) pair land the same display string.
    assert split_p_value("=0.021") == ("0.021", "=", 0.021)
    assert split_p_value("Not applicable") == ("Not applicable", None, None)


def test_is_non_inferiority_prefers_the_flag_then_reads_the_type():
    assert is_non_inferiority(True, "Superiority") is True  # the flag wins
    assert is_non_inferiority(None, "Non-Inferiority") is True
    assert is_non_inferiority(None, "Non-Inferiority or Equivalence") is True
    assert is_non_inferiority(None, "Superiority") is False
    assert is_non_inferiority(None, None) is None


# ------------------------------------------------------- the CT.gov parser


def test_extract_ctgov_results_lands_every_grain():
    rows = extract_ctgov_results(_study_with_results())
    assert len(rows["outcome_measures"]) == 2
    assert len(rows["outcome_groups"]) == 3  # 2 arms on the first outcome, 1 on the second
    assert len(rows["outcome_measurements"]) == 3
    assert len(rows["outcome_analyses"]) == 2
    assert len(rows["baseline_measurements"]) == 1


def test_extract_ctgov_results_keeps_enumerations_verbatim():
    """The whole reason `param_type`/`dispersion_type` are VARCHAR and not an
    enum: this environment cannot confirm the registry's value sets, so the
    ingest layer must not be the thing that decides what is allowed."""
    rows = extract_ctgov_results(_study_with_results())
    measures = {row[4]: row for row in rows["outcome_measures"]}
    pfs = measures["Progression-Free Survival"]
    assert pfs[8] == "MEDIAN"
    assert pfs[9] == "95% Confidence Interval"
    assert pfs[10] == "months"


def test_extract_ctgov_results_carries_the_arm_denominator():
    rows = extract_ctgov_results(_study_with_results())
    by_group = {(row[0], row[2]): row for row in rows["outcome_groups"]}
    counts = {group_key: row[6] for (_outcome, group_key), row in by_group.items()}
    assert counts["OG000"] in (240, 230)
    assert 238 in {row[6] for row in rows["outcome_groups"]}


def test_extract_ctgov_results_splits_the_p_value_and_lists_the_compared_arms():
    rows = extract_ctgov_results(_study_with_results())
    hr = next(row for row in rows["outcome_analyses"] if row[6] == "Hazard Ratio (HR)")
    assert hr[12:16] == ("<0.001", 0.001, "<", None)
    assert json.loads(hr[4]) == ["OG000", "OG001"]
    assert hr[22] is False  # "Superiority" is not a non-inferiority analysis

    ni = next(row for row in rows["outcome_analyses"] if row[6] == "Mean Difference (Net)")
    assert ni[22] is True
    assert ni[24] == "NI margin of -0.10 L"


def test_extract_ctgov_results_is_empty_for_a_study_with_no_results_section():
    study = _study_with_results()
    del study["resultsSection"]
    rows = extract_ctgov_results(study)
    assert all(not landed for landed in rows.values())


def test_baseline_measurement_carries_the_characteristic_grain_and_a_denominator():
    rows = extract_ctgov_results(_study_with_results())
    (row,) = rows["baseline_measurements"]
    assert row[0] == baseline_id("NCT9001", "FEV1", "L")
    assert row[4] == "FEV1"
    assert row[18] == 478  # n, taken from the module-level denominator
    assert row[19] == "Total"  # the arm's title


# ---------------------------------------------------------- both backends


def _ctgov_warehouse(tmp_path, monkeypatch, studies):
    from clinical_endpoints.db import connect

    con = connect(tmp_path / "warehouse.duckdb")
    pages = [{"studies": studies}]

    def fake_fetch(query_term, page_token):
        return pages[0]

    monkeypatch.setattr(ctgov_api, "_fetch_page", fake_fetch)
    ctgov_api.run_pull(con, PullFilters(phases=("3",), limit=10))
    return con


def test_ctgov_pull_lands_results_and_the_has_results_flag(tmp_path, monkeypatch):
    con = _ctgov_warehouse(tmp_path, monkeypatch, [_study_with_results()])
    assert con.execute("SELECT count(*) FROM raw.outcome_measures").fetchone()[0] == 2
    assert con.execute("SELECT count(*) FROM raw.outcome_measurements").fetchone()[0] == 3
    assert con.execute("SELECT has_results FROM raw.studies").fetchone()[0] is True


def test_ctgov_pull_honours_no_results(tmp_path, monkeypatch):
    from clinical_endpoints.db import connect

    con = connect(tmp_path / "warehouse.duckdb")
    monkeypatch.setattr(ctgov_api, "_fetch_page", lambda *_a: {"studies": [_study_with_results()]})
    result = ctgov_api.run_pull(
        con, PullFilters(phases=("3",), limit=10, with_results=False)
    )
    assert "outcome_measures" not in result["row_counts"]
    # `has_results` is landed either way: it is the denominator for "how much
    # of this warehouse could have results", and --no-results must not hide it.
    assert con.execute("SELECT has_results FROM raw.studies").fetchone()[0] is True
    assert not con.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'raw' AND table_name = 'outcome_measures'"
    ).fetchone()


def test_repulling_a_study_replaces_its_results_rather_than_duplicating_them(
    tmp_path, monkeypatch
):
    from clinical_endpoints.db import connect

    con = connect(tmp_path / "warehouse.duckdb")
    monkeypatch.setattr(ctgov_api, "_fetch_page", lambda *_a: {"studies": [_study_with_results()]})
    ctgov_api.run_pull(con, PullFilters(phases=("3",), limit=10))
    ctgov_api.run_pull(con, PullFilters(phases=("3",), limit=10))
    assert con.execute("SELECT count(*) FROM raw.outcome_measures").fetchone()[0] == 2
    assert con.execute("SELECT count(*) FROM raw.outcome_analyses").fetchone()[0] == 2


def test_a_study_that_loses_its_results_loses_its_rows(tmp_path, monkeypatch):
    """A scoped delete-then-insert, like every other child table: a re-pull of
    a study whose results were withdrawn must not leave the old ones behind."""
    from clinical_endpoints.db import connect

    con = connect(tmp_path / "warehouse.duckdb")
    monkeypatch.setattr(ctgov_api, "_fetch_page", lambda *_a: {"studies": [_study_with_results()]})
    ctgov_api.run_pull(con, PullFilters(phases=("3",), limit=10))

    without = _study_with_results()
    del without["resultsSection"]
    monkeypatch.setattr(ctgov_api, "_fetch_page", lambda *_a: {"studies": [without]})
    ctgov_api.run_pull(con, PullFilters(phases=("3",), limit=10))
    assert con.execute("SELECT count(*) FROM raw.outcome_measures").fetchone()[0] == 0


def test_aact_pull_lands_the_same_shape(fake_aact_con):
    result = aact_backend.run_pull(fake_aact_con, PullFilters(phases=("3",), limit=10))
    assert result["results_warning"] is None
    assert result["row_counts"]["outcome_measures"] == 2
    assert result["row_counts"]["outcome_measurements"] == 4
    assert result["row_counts"]["baseline_measurements"] == 1
    assert fake_aact_con.execute(
        "SELECT has_results FROM raw.studies WHERE nct_id = 'NCT001'"
    ).fetchone()[0] is True


def test_both_backends_agree_on_the_columns_they_land(fake_aact_con, tmp_path, monkeypatch):
    """The source-agnostic promise, checked rather than asserted in a comment:
    for each of the five tables, both backends produce the identical column
    list in the identical order."""
    aact_backend.run_pull(fake_aact_con, PullFilters(phases=("3",), limit=10))
    api_con = _ctgov_warehouse(tmp_path, monkeypatch, [_study_with_results()])
    for table, _ddl, columns in RESULTS_TABLES:
        aact_columns = [
            d[0] for d in fake_aact_con.execute(f"SELECT * FROM raw.{table} LIMIT 0").description
        ]
        api_columns = [
            d[0] for d in api_con.execute(f"SELECT * FROM raw.{table} LIMIT 0").description
        ]
        assert aact_columns == api_columns == list(columns), table


def test_the_sql_and_python_outcome_keys_agree(fake_aact_con):
    """`outcome_id_sql` and `outcome_id` are two spellings of one key. If they
    drift, the same study pulled through the two backends gets two different
    ids and every link table downstream silently doubles."""
    aact_backend.run_pull(fake_aact_con, PullFilters(phases=("3",), limit=10))
    rows = fake_aact_con.execute(
        "SELECT outcome_id, nct_id, outcome_type, title, time_frame FROM raw.outcome_measures"
    ).fetchall()
    assert rows
    for landed, nct_id, outcome_type, title, time_frame in rows:
        assert landed == outcome_id(nct_id, outcome_type, title, time_frame)


def test_the_sql_and_python_keys_agree_on_whitespace_and_case(fake_aact_con):
    """The two spellings have to agree on the *edges*: a title with a trailing
    newline, and the two backends' different casing of `outcome_type`."""
    fake_aact_con.execute(
        """
        INSERT INTO aact.ctgov.outcomes VALUES
            (3, 'NCT001', 'PRIMARY', e'Overall Survival\n', 'OS', e'  Up to 60 months  ',
             NULL, 'months', NULL, 'Median', NULL)
        """
    )
    aact_backend.run_pull(fake_aact_con, PullFilters(phases=("3",), limit=10))
    landed = fake_aact_con.execute(
        "SELECT outcome_id FROM raw.outcome_measures WHERE title LIKE 'Overall Survival%'"
    ).fetchone()[0]
    assert landed == outcome_id("NCT001", "primary", "Overall Survival", "Up to 60 months")


def test_aact_degrades_when_the_results_tables_are_missing(fake_aact_con):
    """AACT is an upstream this project cannot reach from its build
    environment. A missing results table has to cost the results section, not
    the pull."""
    fake_aact_con.execute("DROP TABLE aact.ctgov.outcomes")
    result = aact_backend.run_pull(fake_aact_con, PullFilters(phases=("3",), limit=10))
    assert "ctgov.outcomes" in result["results_warning"]
    assert result["row_counts"]["studies"] == 2  # the protocol half still landed


def test_aact_substitutes_null_for_a_column_the_upstream_does_not_carry(fake_aact_con):
    fake_aact_con.execute("ALTER TABLE aact.ctgov.outcomes DROP COLUMN population")
    result = aact_backend.run_pull(fake_aact_con, PullFilters(phases=("3",), limit=10))
    assert result["results_warning"] is None
    assert fake_aact_con.execute(
        "SELECT count(*) FROM raw.outcome_measures WHERE population IS NOT NULL"
    ).fetchone()[0] == 0
    assert result["row_counts"]["outcome_measures"] == 2


def test_aact_analyses_land_the_same_p_value_shape_as_the_api(fake_aact_con):
    aact_backend.run_pull(fake_aact_con, PullFilters(phases=("3",), limit=10))
    rows = dict(
        fake_aact_con.execute(
            "SELECT param_type, p_value FROM raw.outcome_analyses"
        ).fetchall()
    )
    assert rows["Hazard Ratio (HR)"] == "<0.001"
    assert rows["Mean Difference (Net)"] == "0.021"


def test_aact_reads_non_inferiority_from_the_type_string(fake_aact_con):
    aact_backend.run_pull(fake_aact_con, PullFilters(phases=("3",), limit=10))
    rows = dict(
        fake_aact_con.execute(
            "SELECT non_inferiority_type, non_inferiority FROM raw.outcome_analyses"
        ).fetchall()
    )
    assert rows["Superiority"] is False
    assert rows["Non-Inferiority"] is True


def test_results_tables_survive_a_schema_migration(tmp_path):
    """`ensure_table` reconciles rather than drops -- the same guarantee the
    protocol tables already have, now that five more tables depend on it."""
    from clinical_endpoints.ingest.upsert import ensure_table

    con = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    for schema in SCHEMAS:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    con.execute("CREATE TABLE raw.outcome_measures (outcome_id VARCHAR, nct_id VARCHAR)")
    con.execute("INSERT INTO raw.outcome_measures VALUES ('abc', 'NCT001')")

    change = ensure_table(con, "outcome_measures", RESULTS_TABLES[0][1])
    assert change is not None
    assert change.rows_kept == 1
    assert "title" in change.added
    assert con.execute("SELECT nct_id FROM raw.outcome_measures").fetchone()[0] == "NCT001"
