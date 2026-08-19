"""Filtered pull of study data via the public ClinicalTrials.gov API v2, as a
fallback ingestion backend when AACT access is unavailable.

Produces the *same* raw.studies / raw.design_outcomes shape as ingest/aact.py,
so everything downstream (vocab sampling, conforming, graph) is source-agnostic
and doesn't care which backend a given pull came from. Switch back to AACT with
`--source aact` once AACT access is resolved -- see plan §2, which flags the
live API/site as "the thing that gets you blocked" in the first place; this
backend exists only as a stopgap, not a replacement.

CAVEAT: this was built from ClinicalTrials.gov's documented API v2 shape, not
verified against a live response -- the sandbox this was built in also can't
reach clinicaltrials.gov (network-policy blocked), so this needs its first
real test run against the live API. If a field/param name below is wrong,
`CtgovApiError` surfaces the full HTTP response body, which is normally enough
to fix the one wrong param name without re-guessing from scratch.

Known gap vs. AACT: `raw.design_outcomes.population` is always NULL here --
the API v2 outcome objects don't expose the per-outcome population
description AACT's `design_outcomes.population` column carries.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any, Optional

import duckdb
import requests

from clinical_endpoints.ingest.filters import PullFilters, normalize_phases
from clinical_endpoints.ingest.pull_log import write_pull_log

API_BASE_URL = "https://clinicaltrials.gov/api/v2/studies"
SOURCE = "ctgov_api"

PAGE_SIZE = 200
REQUEST_TIMEOUT_S = 30
MAX_RETRIES = 3
MAX_PAGES = 25  # safety cap: at PAGE_SIZE=200 this scans up to 5000 studies


class CtgovApiError(RuntimeError):
    """Raised when the ClinicalTrials.gov API is unreachable or returns an error."""


def _build_query_term(aact_phases: list[str], since: Optional[date]) -> str:
    if len(aact_phases) == 1:
        phase_clause = f"AREA[Phase]{aact_phases[0]}"
    else:
        phase_clause = "AREA[Phase](" + " OR ".join(aact_phases) + ")"
    clauses = [phase_clause]
    if since:
        clauses.append(f"AREA[StartDate]RANGE[{since.isoformat()},MAX]")
    return " AND ".join(clauses)


def _fetch_page(query_term: str, page_token: Optional[str]) -> dict:
    params: dict[str, Any] = {
        "query.term": query_term,
        "pageSize": PAGE_SIZE,
        "sort": "StartDate:desc",
    }
    if page_token:
        params["pageToken"] = page_token

    last_exc: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(API_BASE_URL, params=params, timeout=REQUEST_TIMEOUT_S)
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(2**attempt)
            continue

        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
            time.sleep(2**attempt)
            continue
        raise CtgovApiError(
            f"ClinicalTrials.gov API returned HTTP {resp.status_code} for {resp.url}\n\n"
            f"Response body:\n{resp.text[:2000]}"
        )

    raise CtgovApiError(
        f"ClinicalTrials.gov API unreachable after {MAX_RETRIES} attempts: {last_exc}"
    ) from last_exc


def _get_path(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _parse_date(raw: Optional[str]) -> Optional[str]:
    """CT.gov dates are sometimes month-precision ("YYYY-MM"); pad to day 1."""
    if not raw:
        return None
    return f"{raw}-01" if len(raw) == 7 else raw


def _join_phases(phases: list[str]) -> Optional[str]:
    if not phases:
        return None
    return "/".join(phases)  # matches AACT's "PHASE1/PHASE2" convention for combined phases


def _extract_study_row(study: dict) -> dict:
    proto = study.get("protocolSection", {})
    ident = proto.get("identificationModule", {})
    status = proto.get("statusModule", {})
    design = proto.get("designModule", {})
    return {
        "nct_id": ident.get("nctId"),
        "phase": _join_phases(design.get("phases") or []),
        "overall_status": status.get("overallStatus"),
        "study_type": design.get("studyType"),
        "start_date": _parse_date(_get_path(status, "startDateStruct", "date")),
        "primary_completion_date": _parse_date(
            _get_path(status, "primaryCompletionDateStruct", "date")
        ),
        "brief_title": ident.get("briefTitle"),
        "official_title": ident.get("officialTitle"),
    }


def _extract_outcome_rows(study: dict) -> list[dict]:
    nct_id = _get_path(study, "protocolSection", "identificationModule", "nctId")
    outcomes_module = _get_path(study, "protocolSection", "outcomesModule") or {}
    rows = []
    for outcome_type, key in (
        ("primary", "primaryOutcomes"),
        ("secondary", "secondaryOutcomes"),
        ("other", "otherOutcomes"),
    ):
        for outcome in outcomes_module.get(key) or []:
            rows.append(
                {
                    "nct_id": nct_id,
                    "outcome_type": outcome_type,
                    "measure": outcome.get("measure"),
                    "time_frame": outcome.get("timeFrame"),
                    "description": outcome.get("description"),
                    "population": None,
                }
            )
    return rows


def run_pull(con: duckdb.DuckDBPyConnection, filters: PullFilters) -> dict:
    """Pull filtered studies + their outcome measures from the CT.gov API into raw.*.

    Paginates the API, filtering server-side (query.term) and re-checking
    client-side as a safety net, then does a final client-side sort by
    start_date desc before truncating to `limit` -- this way "most recent N"
    stays correct even if the server-side `sort` param turns out to be wrong
    or unsupported.
    """
    aact_phases = normalize_phases(list(filters.phases))
    query_term = _build_query_term(aact_phases, filters.since)
    since_iso = filters.since.isoformat() if filters.since else None

    studies: list[dict] = []
    outcomes_by_nct: dict[str, list[dict]] = {}
    seen_nct_ids: set[str] = set()
    target = max(filters.limit * 3, filters.limit + 50)

    page_token: Optional[str] = None
    for _ in range(MAX_PAGES):
        payload = _fetch_page(query_term, page_token)
        page_studies = payload.get("studies") or []
        if not page_studies:
            break

        for study in page_studies:
            row = _extract_study_row(study)
            nct_id = row["nct_id"]
            if not nct_id or nct_id in seen_nct_ids:
                continue
            if row["phase"] not in aact_phases:
                continue
            if since_iso and (not row["start_date"] or row["start_date"] < since_iso):
                continue
            seen_nct_ids.add(nct_id)
            studies.append(row)
            outcomes_by_nct[nct_id] = _extract_outcome_rows(study)

        page_token = payload.get("nextPageToken")
        if not page_token or len(studies) >= target:
            break

    studies.sort(key=lambda r: r["start_date"] or "", reverse=True)
    studies = studies[: filters.limit]
    outcomes = [o for s in studies for o in outcomes_by_nct.get(s["nct_id"], [])]

    con.execute(
        """
        CREATE OR REPLACE TABLE raw.studies (
            nct_id VARCHAR, phase VARCHAR, overall_status VARCHAR, study_type VARCHAR,
            start_date DATE, primary_completion_date DATE,
            brief_title VARCHAR, official_title VARCHAR
        )
        """
    )
    if studies:
        con.executemany(
            "INSERT INTO raw.studies VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    s["nct_id"],
                    s["phase"],
                    s["overall_status"],
                    s["study_type"],
                    s["start_date"],
                    s["primary_completion_date"],
                    s["brief_title"],
                    s["official_title"],
                )
                for s in studies
            ],
        )

    con.execute(
        """
        CREATE OR REPLACE TABLE raw.design_outcomes (
            nct_id VARCHAR, outcome_type VARCHAR, measure VARCHAR,
            time_frame VARCHAR, description VARCHAR, population VARCHAR
        )
        """
    )
    if outcomes:
        con.executemany(
            "INSERT INTO raw.design_outcomes VALUES (?, ?, ?, ?, ?, ?)",
            [
                (o["nct_id"], o["outcome_type"], o["measure"], o["time_frame"], o["description"], o["population"])
                for o in outcomes
            ],
        )

    row_counts = {"studies": len(studies), "design_outcomes": len(outcomes)}
    log_entry = write_pull_log(con, source=SOURCE, filters=filters.as_dict(), row_counts=row_counts)
    return {**log_entry, "row_counts": row_counts}
