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
from clinical_endpoints.ingest.upsert import ensure_table, replace_children, upsert_rows

API_BASE_URL = "https://clinicaltrials.gov/api/v2/studies"
SOURCE = "ctgov_api"

# What this backend actually lands, for raw._pull_log.source_tables (differs from
# the AACT backend: the API exposes no MeSH tree numbers at all, so there is no
# mesh_terms table here -- instead it lands the coarse browse-branch letters AACT
# has no equivalent of; see ingest/aact.py).
SOURCE_TABLES = (
    "studies",
    "design_outcomes",
    "conditions",
    "browse_conditions",
    "browse_interventions",
    "browse_condition_branches",
)

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


def _extract_condition_rows(study: dict) -> list[dict]:
    """Sponsor free-text conditions, protocolSection.conditionsModule.conditions[] --
    not MeSH-coded, so this feeds raw.conditions, not raw.browse_conditions."""
    nct_id = _get_path(study, "protocolSection", "identificationModule", "nctId")
    conditions = _get_path(study, "protocolSection", "conditionsModule", "conditions") or []
    return [{"nct_id": nct_id, "name": name} for name in conditions if name]


def _extract_mesh_rows(study: dict, *, module: str, mesh_type: str) -> list[dict]:
    """NLM-assigned MeSH terms from derivedSection.<module>.meshes[] (id, term) --
    shared by conditionBrowseModule and interventionBrowseModule."""
    nct_id = _get_path(study, "protocolSection", "identificationModule", "nctId")
    meshes = _get_path(study, "derivedSection", module, "meshes") or []
    rows = []
    for mesh in meshes:
        term = mesh.get("term")
        if not term:
            continue
        rows.append(
            {
                "nct_id": nct_id,
                "mesh_term": term,
                "mesh_term_normalised": term.strip().lower(),
                "mesh_type": mesh_type,
            }
        )
    return rows


def _extract_condition_branch_rows(study: dict) -> list[dict]:
    """Coarse top-level MeSH tree branches, derivedSection.conditionBrowseModule
    .browseBranches[] (abbrev, name) -- e.g. "BC04" = Neoplasms. This is the only
    tree-level signal the API exposes; there is no per-condition tree number."""
    nct_id = _get_path(study, "protocolSection", "identificationModule", "nctId")
    branches = _get_path(study, "derivedSection", "conditionBrowseModule", "browseBranches") or []
    rows = []
    for branch in branches:
        abbrev = branch.get("abbrev")
        if not abbrev:
            continue
        rows.append({"nct_id": nct_id, "branch_abbrev": abbrev, "branch_name": branch.get("name")})
    return rows


def run_pull(con: duckdb.DuckDBPyConnection, filters: PullFilters) -> dict:
    """Pull filtered studies + their outcome measures from the CT.gov API into raw.*.

    Paginates the API, filtering server-side (query.term) and re-checking
    client-side as a safety net, then does a final client-side sort by
    start_date desc before truncating to `limit` -- this way "most recent N"
    stays correct even if the server-side `sort` param turns out to be wrong
    or unsupported.

    Upserts rather than replaces: raw.studies is updated/inserted per nct_id,
    and every child table (design_outcomes, conditions, browse_*) has its rows
    for *this pull's* nct_ids replaced -- studies landed by earlier pulls with
    different filters are never touched.
    """
    aact_phases = normalize_phases(list(filters.phases))
    query_term = _build_query_term(aact_phases, filters.since)
    since_iso = filters.since.isoformat() if filters.since else None

    studies: list[dict] = []
    outcomes_by_nct: dict[str, list[dict]] = {}
    conditions_by_nct: dict[str, list[dict]] = {}
    browse_conditions_by_nct: dict[str, list[dict]] = {}
    browse_interventions_by_nct: dict[str, list[dict]] = {}
    condition_branches_by_nct: dict[str, list[dict]] = {}
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
            conditions_by_nct[nct_id] = _extract_condition_rows(study)
            browse_conditions_by_nct[nct_id] = _extract_mesh_rows(
                study, module="conditionBrowseModule", mesh_type="condition"
            )
            browse_interventions_by_nct[nct_id] = _extract_mesh_rows(
                study, module="interventionBrowseModule", mesh_type="intervention"
            )
            condition_branches_by_nct[nct_id] = _extract_condition_branch_rows(study)

        page_token = payload.get("nextPageToken")
        if not page_token or len(studies) >= target:
            break

    studies.sort(key=lambda r: r["start_date"] or "", reverse=True)
    studies = studies[: filters.limit]
    kept_nct_ids = [s["nct_id"] for s in studies]
    outcomes = [o for s in studies for o in outcomes_by_nct.get(s["nct_id"], [])]
    conditions = [c for nct_id in kept_nct_ids for c in conditions_by_nct.get(nct_id, [])]
    browse_conditions = [
        m for nct_id in kept_nct_ids for m in browse_conditions_by_nct.get(nct_id, [])
    ]
    browse_interventions = [
        m for nct_id in kept_nct_ids for m in browse_interventions_by_nct.get(nct_id, [])
    ]
    condition_branches = [
        b for nct_id in kept_nct_ids for b in condition_branches_by_nct.get(nct_id, [])
    ]

    ensure_table(
        con,
        "studies",
        """
        nct_id VARCHAR PRIMARY KEY, phase VARCHAR, overall_status VARCHAR, study_type VARCHAR,
        start_date DATE, primary_completion_date DATE,
        brief_title VARCHAR, official_title VARCHAR
        """,
    )
    upsert_rows(
        con,
        "studies",
        [
            "nct_id", "phase", "overall_status", "study_type",
            "start_date", "primary_completion_date", "brief_title", "official_title",
        ],
        ["nct_id"],
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

    ensure_table(
        con,
        "design_outcomes",
        """
        nct_id VARCHAR, outcome_type VARCHAR, measure VARCHAR,
        time_frame VARCHAR, description VARCHAR, population VARCHAR
        """,
    )
    replace_children(
        con,
        "design_outcomes",
        ["nct_id", "outcome_type", "measure", "time_frame", "description", "population"],
        "nct_id",
        kept_nct_ids,
        [
            (o["nct_id"], o["outcome_type"], o["measure"], o["time_frame"], o["description"], o["population"])
            for o in outcomes
        ],
    )

    ensure_table(con, "conditions", "nct_id VARCHAR, name VARCHAR")
    replace_children(
        con,
        "conditions",
        ["nct_id", "name"],
        "nct_id",
        kept_nct_ids,
        [(c["nct_id"], c["name"]) for c in conditions],
    )

    ensure_table(
        con,
        "browse_conditions",
        "nct_id VARCHAR, mesh_term VARCHAR, mesh_term_normalised VARCHAR, mesh_type VARCHAR",
    )
    replace_children(
        con,
        "browse_conditions",
        ["nct_id", "mesh_term", "mesh_term_normalised", "mesh_type"],
        "nct_id",
        kept_nct_ids,
        [
            (m["nct_id"], m["mesh_term"], m["mesh_term_normalised"], m["mesh_type"])
            for m in browse_conditions
        ],
    )

    ensure_table(
        con,
        "browse_interventions",
        "nct_id VARCHAR, mesh_term VARCHAR, mesh_term_normalised VARCHAR, mesh_type VARCHAR",
    )
    replace_children(
        con,
        "browse_interventions",
        ["nct_id", "mesh_term", "mesh_term_normalised", "mesh_type"],
        "nct_id",
        kept_nct_ids,
        [
            (m["nct_id"], m["mesh_term"], m["mesh_term_normalised"], m["mesh_type"])
            for m in browse_interventions
        ],
    )

    ensure_table(
        con,
        "browse_condition_branches",
        "nct_id VARCHAR, branch_abbrev VARCHAR, branch_name VARCHAR",
    )
    replace_children(
        con,
        "browse_condition_branches",
        ["nct_id", "branch_abbrev", "branch_name"],
        "nct_id",
        kept_nct_ids,
        [(b["nct_id"], b["branch_abbrev"], b["branch_name"]) for b in condition_branches],
    )

    row_counts = {
        "studies": len(studies),
        "design_outcomes": len(outcomes),
        "conditions": len(conditions),
        "browse_conditions": len(browse_conditions),
        "browse_interventions": len(browse_interventions),
        "browse_condition_branches": len(condition_branches),
    }
    log_entry = write_pull_log(
        con, source=SOURCE, filters=filters.as_dict(), row_counts=row_counts, source_tables=SOURCE_TABLES
    )
    return {**log_entry, "row_counts": row_counts, "nct_ids": kept_nct_ids}
