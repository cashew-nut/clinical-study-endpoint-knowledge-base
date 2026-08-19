from __future__ import annotations

import json
from datetime import date

import pytest

from clinical_endpoints.db import connect
from clinical_endpoints.ingest import ctgov_api
from clinical_endpoints.ingest.ctgov_api import CtgovApiError, run_pull
from clinical_endpoints.ingest.filters import PullFilters


def _make_study(
    nct_id: str,
    phases: list[str],
    start_date: str,
    *,
    status: str = "RECRUITING",
    study_type: str = "INTERVENTIONAL",
    primary_completion_date: str | None = None,
    outcomes: dict | None = None,
) -> dict:
    status_module = {"overallStatus": status, "startDateStruct": {"date": start_date}}
    if primary_completion_date:
        status_module["primaryCompletionDateStruct"] = {"date": primary_completion_date}
    return {
        "protocolSection": {
            "identificationModule": {
                "nctId": nct_id,
                "briefTitle": f"{nct_id} brief",
                "officialTitle": f"{nct_id} official",
            },
            "statusModule": status_module,
            "designModule": {"phases": phases, "studyType": study_type},
            "outcomesModule": outcomes or {},
        }
    }


class FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.url = "https://clinicaltrials.gov/api/v2/studies?fake=1"

    def json(self) -> dict:
        return self._json


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(ctgov_api.time, "sleep", lambda _s: None)


def test_build_query_term_single_and_multi_phase_with_since():
    assert ctgov_api._build_query_term(["PHASE3"], None) == "AREA[Phase]PHASE3"
    assert ctgov_api._build_query_term(["PHASE2", "PHASE3"], None) == (
        "AREA[Phase](PHASE2 OR PHASE3)"
    )
    assert ctgov_api._build_query_term(["PHASE3"], date(2023, 1, 1)) == (
        "AREA[Phase]PHASE3 AND AREA[StartDate]RANGE[2023-01-01,MAX]"
    )


def test_extract_study_row_pads_month_precision_dates():
    study = _make_study("NCT001", ["PHASE3"], "2024-03", primary_completion_date="2024-06-15")
    row = ctgov_api._extract_study_row(study)
    assert row == {
        "nct_id": "NCT001",
        "phase": "PHASE3",
        "overall_status": "RECRUITING",
        "study_type": "INTERVENTIONAL",
        "start_date": "2024-03-01",
        "primary_completion_date": "2024-06-15",
        "brief_title": "NCT001 brief",
        "official_title": "NCT001 official",
    }


def test_extract_study_row_joins_combined_phases_aact_style():
    study = _make_study("NCT002", ["PHASE1", "PHASE2"], "2024-01-01")
    assert ctgov_api._extract_study_row(study)["phase"] == "PHASE1/PHASE2"


def test_extract_outcome_rows_covers_all_outcome_types():
    study = _make_study(
        "NCT001",
        ["PHASE3"],
        "2024-01-01",
        outcomes={
            "primaryOutcomes": [{"measure": "PFS", "timeFrame": "Event-driven", "description": "d1"}],
            "secondaryOutcomes": [{"measure": "OS", "timeFrame": "Event-driven", "description": "d2"}],
        },
    )
    rows = ctgov_api._extract_outcome_rows(study)
    assert [(r["outcome_type"], r["measure"]) for r in rows] == [
        ("primary", "PFS"),
        ("secondary", "OS"),
    ]
    assert all(r["population"] is None for r in rows)


def test_run_pull_lands_studies_sorted_desc_and_logs(tmp_path, monkeypatch):
    studies = [
        _make_study(
            "NCT001",
            ["PHASE3"],
            "2023-01-01",
            outcomes={"primaryOutcomes": [{"measure": "PFS", "timeFrame": "Event-driven", "description": "d"}]},
        ),
        _make_study("NCT002", ["PHASE3"], "2024-06-01"),
        _make_study("NCT003", ["PHASE1"], "2024-01-01"),  # wrong phase, filtered client-side
    ]
    responses = [FakeResponse(200, {"studies": studies})]

    def fake_get(url, params=None, timeout=None):
        return responses.pop(0)

    monkeypatch.setattr(ctgov_api.requests, "get", fake_get)

    con = connect(tmp_path / "warehouse.duckdb")
    result = run_pull(con, PullFilters(phases=("3",), limit=500))

    rows = con.execute("SELECT nct_id FROM raw.studies ORDER BY nct_id").fetchall()
    assert {r[0] for r in rows} == {"NCT001", "NCT002"}  # NCT003 excluded (PHASE1)

    # most-recent-first: NCT002 (2024) before NCT001 (2023)
    ordered = con.execute("SELECT nct_id FROM raw.studies ORDER BY start_date DESC").fetchall()
    assert [r[0] for r in ordered] == ["NCT002", "NCT001"]

    outcomes = con.execute("SELECT nct_id, measure FROM raw.design_outcomes").fetchall()
    assert outcomes == [("NCT001", "PFS")]

    log_row = con.execute(
        "SELECT source, row_counts FROM raw._pull_log"
    ).fetchone()
    assert log_row[0] == "ctgov_api"
    assert json.loads(log_row[1]) == {"studies": 2, "design_outcomes": 1}
    assert result["row_counts"] == {"studies": 2, "design_outcomes": 1}
    con.close()


def test_run_pull_respects_limit_after_sorting(tmp_path, monkeypatch):
    studies = [
        _make_study("NCT001", ["PHASE3"], "2023-01-01"),
        _make_study("NCT002", ["PHASE3"], "2024-01-01"),
    ]
    responses = [FakeResponse(200, {"studies": studies})]
    monkeypatch.setattr(ctgov_api.requests, "get", lambda *a, **k: responses.pop(0))

    con = connect(tmp_path / "warehouse.duckdb")
    run_pull(con, PullFilters(phases=("3",), limit=1))

    rows = con.execute("SELECT nct_id FROM raw.studies").fetchall()
    assert [r[0] for r in rows] == ["NCT002"]  # most recent kept, not first-seen
    con.close()


def test_run_pull_paginates_until_no_next_token(tmp_path, monkeypatch):
    page1 = FakeResponse(
        200,
        {"studies": [_make_study("NCT001", ["PHASE3"], "2024-01-01")], "nextPageToken": "tok2"},
    )
    page2 = FakeResponse(200, {"studies": [_make_study("NCT002", ["PHASE3"], "2024-02-01")]})
    responses = [page1, page2]
    seen_tokens = []

    def fake_get(url, params=None, timeout=None):
        seen_tokens.append(params.get("pageToken"))
        return responses.pop(0)

    monkeypatch.setattr(ctgov_api.requests, "get", fake_get)

    con = connect(tmp_path / "warehouse.duckdb")
    run_pull(con, PullFilters(phases=("3",), limit=500))

    assert seen_tokens == [None, "tok2"]
    rows = con.execute("SELECT count(*) FROM raw.studies").fetchone()
    assert rows[0] == 2
    con.close()


def test_run_pull_raises_clear_error_on_http_failure(tmp_path, monkeypatch):
    bad_response = FakeResponse(400, text='{"error": "unknown search area Phase"}')
    monkeypatch.setattr(ctgov_api.requests, "get", lambda *a, **k: bad_response)

    con = connect(tmp_path / "warehouse.duckdb")
    with pytest.raises(CtgovApiError, match="HTTP 400"):
        run_pull(con, PullFilters(phases=("3",), limit=500))
    con.close()


def test_run_pull_retries_transient_5xx_then_succeeds(tmp_path, monkeypatch):
    responses = [
        FakeResponse(503, text="temporarily unavailable"),
        FakeResponse(200, {"studies": [_make_study("NCT001", ["PHASE3"], "2024-01-01")]}),
    ]
    monkeypatch.setattr(ctgov_api.requests, "get", lambda *a, **k: responses.pop(0))

    con = connect(tmp_path / "warehouse.duckdb")
    result = run_pull(con, PullFilters(phases=("3",), limit=500))
    assert result["row_counts"]["studies"] == 1
    con.close()
