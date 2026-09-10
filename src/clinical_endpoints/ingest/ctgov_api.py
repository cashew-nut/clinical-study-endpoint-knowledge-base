"""Filtered pull of study data via the public ClinicalTrials.gov API v2, as a
fallback ingestion backend when AACT access is unavailable.

Produces the *same* raw.studies / raw.design_outcomes shape as ingest/aact.py,
so everything downstream (vocab sampling, conforming, projection) is
source-agnostic and doesn't care which backend a given pull came from. Switch
back to AACT with `--source aact` once AACT access is resolved: the live API is
also the thing that gets you rate-limited or blocked, so this backend is a
stopgap, not a replacement.

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
from collections import defaultdict
from datetime import date
from typing import Any, Callable, Optional

import duckdb
import requests

from clinical_endpoints.ingest.design import (
    DESIGN_GROUPS_COLUMNS,
    DESIGN_GROUPS_DDL,
    STUDIES_DDL,
    STUDY_COLUMNS,
    normalise_enrollment_count,
    normalise_healthy_volunteers,
)
from clinical_endpoints.ingest.filters import PullFilters, normalize_phases
from clinical_endpoints.ingest.interventions import (
    INTERVENTION_TABLE_NAMES,
    INTERVENTION_TABLES,
    extract_ctgov_interventions,
)
from clinical_endpoints.ingest.pull_log import write_pull_log
from clinical_endpoints.ingest.results import (
    RESULTS_TABLE_NAMES,
    RESULTS_TABLES,
    extract_ctgov_results,
    has_results,
)
from clinical_endpoints.ingest.upsert import SchemaReconciler, replace_children, upsert_rows
from clinical_endpoints.drug_class.resolver import (
    Intervention,
    load_drug_class_mapping,
    resolve_study_drug_class_matches,
)
from clinical_endpoints.ta.resolver import (
    branch_abbrev_to_tree_prefix,
    load_ta_mapping,
    resolve_study_ta_matches,
)

API_BASE_URL = "https://clinicaltrials.gov/api/v2/studies"
SOURCE = "ctgov_api"

# What this backend actually lands, for raw._pull_log.source_tables (differs from
# the AACT backend: the API exposes no MeSH tree numbers at all, so there is no
# mesh_terms table here -- instead it lands the coarse browse-branch letters AACT
# has no equivalent of; see ingest/aact.py).
SOURCE_TABLES = (
    "studies",
    "design_outcomes",
    "design_groups",
    "conditions",
    "browse_conditions",
    "browse_interventions",
    "browse_condition_branches",
) + INTERVENTION_TABLE_NAMES

# ...plus the results section, when `--no-results` was not given. Recorded
# separately so raw._pull_log.source_tables says which of the two shapes a
# given pull actually landed -- a warehouse can hold both.
RESULTS_SOURCE_TABLES = RESULTS_TABLE_NAMES

PAGE_SIZE = 200
REQUEST_TIMEOUT_S = 30
MAX_RETRIES = 3
MAX_PAGES = 25  # safety cap: at PAGE_SIZE=200 this scans up to 5000 studies

# `--ta` isn't a CT.gov API search parameter -- there's no server-side way to
# ask for "respiratory" the way this project defines it, so filtering has to
# happen client-side against the pages the phase/since query returns, and
# recent registrations skew heavily toward whichever conditions dominate
# trial activity generally (oncology). Without a much deeper scan, a niche
# `--ta` would starve on the first `MAX_PAGES` pages before ever accumulating
# `--limit` matches -- see the bug this constant fixes: `--ta respiratory`
# landing only a handful of studies because 500 non-respiratory studies filled
# the pull first. This cap only applies when `--ta` is given.
# `--drug-class` has exactly the same problem and widens the scan the same way,
# under this same cap -- CT.gov cannot express a drug class server-side either.
# See docs/DRUG_CLASS_SPEC.md's CLI section.
MAX_PAGES_TA_FILTERED = 150  # up to 30,000 studies scanned


class CtgovApiError(RuntimeError):
    """Raised when the ClinicalTrials.gov API is unreachable or returns an error."""


def _quote_essie_phrase(value: str) -> str:
    """Essie's fielded AREA[...] search treats an unquoted multi-word value as
    separate tokens rather than one phrase -- AREA[LeadSponsorName]Memorial
    Sloan Kettering would scope only "Memorial" to that field and let "Sloan"
    and "Kettering" fall through to an unscoped term search. Quoting keeps a
    multi-word organization name scoped to the one field it belongs in.
    Unverified against a live response, like the rest of this module's query
    construction (see the module docstring's CAVEAT) -- a wrong assumption
    here would surface as an unexpectedly wide/narrow result set, not an HTTP
    error, since a malformed AREA value doesn't necessarily 400.
    """
    return '"' + value.replace('"', '\\"') + '"'


def _build_query_term(
    aact_phases: list[str], since: Optional[date], org: Optional[tuple[str, ...]] = None
) -> str:
    if len(aact_phases) == 1:
        phase_clause = f"AREA[Phase]{aact_phases[0]}"
    else:
        phase_clause = "AREA[Phase](" + " OR ".join(aact_phases) + ")"
    clauses = [phase_clause]
    if since:
        clauses.append(f"AREA[StartDate]RANGE[{since.isoformat()},MAX]")
    if org:
        quoted = [_quote_essie_phrase(o) for o in org]
        if len(quoted) == 1:
            clauses.append(f"AREA[LeadSponsorName]{quoted[0]}")
        else:
            clauses.append("AREA[LeadSponsorName](" + " OR ".join(quoted) + ")")
    return " AND ".join(clauses)


def _organization_matches(organization: Optional[str], wanted_fragments: tuple[str, ...]) -> bool:
    """Case-insensitive substring match, as a client-side safety net behind the
    server-side AREA[LeadSponsorName] filter -- see `run_pull`."""
    if not organization:
        return False
    haystack = organization.lower()
    return any(fragment in haystack for fragment in wanted_fragments)


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
    eligibility = proto.get("eligibilityModule", {})
    design_info = design.get("designInfo") or {}
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
        # Design and eligibility, per CDISC's ct-gov_mapping.xlsx -- see
        # ingest/design.py for the field-by-field USDM targets.
        "intervention_model": design_info.get("interventionModel"),
        "primary_purpose": design_info.get("primaryPurpose"),
        "allocation": design_info.get("allocation"),
        "masking": _get_path(design_info, "maskingInfo", "masking"),
        "enrollment_count": normalise_enrollment_count(
            _get_path(design, "enrollmentInfo", "count")
        ),
        "enrollment_type": _get_path(design, "enrollmentInfo", "type"),
        "healthy_volunteers": normalise_healthy_volunteers(eligibility.get("healthyVolunteers")),
        "gender": eligibility.get("sex"),
        "minimum_age": eligibility.get("minimumAge"),
        "maximum_age": eligibility.get("maximumAge"),
        "population_description": eligibility.get("studyPopulation"),
        # The *lead* sponsor only, never a collaborator -- matches the
        # AREA[LeadSponsorName] filter `_build_query_term` applies server-side.
        "organization": _get_path(proto, "sponsorCollaboratorsModule", "leadSponsor", "name"),
        # The registry's own flag, landed regardless of `--no-results`.
        "has_results": has_results(study),
    }


def _extract_arm_rows(study: dict) -> list[dict]:
    """protocolSection.armsInterventionsModule.armGroups[] -> raw.design_groups."""
    nct_id = _get_path(study, "protocolSection", "identificationModule", "nctId")
    arms = _get_path(study, "protocolSection", "armsInterventionsModule", "armGroups") or []
    return [
        {
            "nct_id": nct_id,
            "group_type": arm.get("type"),
            "title": arm.get("label"),
            "description": arm.get("description"),
        }
        for arm in arms
    ]


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


def _interventions_for_matching(intervention_rows: dict[str, list[tuple]]) -> list[Intervention]:
    """The rows `extract_ctgov_interventions` produced -> the `Intervention`
    objects the drug-class resolver takes.

    The conversion lives here rather than in the resolver because this is the
    one module that owns both shapes: the resolver reads raw.* tables and should
    not also know the positional layout of an extractor's tuples.
    """
    aliases: dict[int, list[str]] = defaultdict(list)
    for _nct_id, ordinal, _other_name, other_name_normalised in intervention_rows.get(
        "intervention_other_names", []
    ):
        if other_name_normalised:
            aliases[ordinal].append(other_name_normalised)
    return [
        Intervention(
            ordinal=ordinal,
            intervention_type=intervention_type,
            name=name,
            name_normalised=name_normalised,
            other_names_normalised=tuple(aliases.get(ordinal, ())),
        )
        for _nct_id, ordinal, intervention_type, name, name_normalised, _description in (
            intervention_rows.get("interventions", [])
        )
    ]


def run_pull(
    con: duckdb.DuckDBPyConnection,
    filters: PullFilters,
    on_page: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Pull filtered studies + their outcome measures from the CT.gov API into raw.*.

    Paginates the API, filtering server-side (query.term) and re-checking
    client-side as a safety net, then does a final client-side sort by
    start_date desc before truncating to `limit` -- this way "most recent N"
    stays correct even if the server-side `sort` param turns out to be wrong
    or unsupported.

    `filters.ta`, if given, is also applied client-side, *before* a study
    counts toward `limit` -- CT.gov's API has no server-side way to ask for
    this project's therapeutic areas, and recent registrations skew heavily
    toward whichever conditions dominate trial activity generally (oncology),
    so filtering only *after* collecting the most recent `limit` studies of
    any area would starve a smaller area of matches it actually has. Pulling
    "the 500 most recent respiratory studies" therefore has to keep scanning
    past non-matching studies -- see `MAX_PAGES_TA_FILTERED`. A study is
    judged by the exact same layered rules `ta/resolver.py` uses to write
    conformed.study_therapeutic_area, via `resolve_study_ta_matches`, so a
    pull-time match always agrees with the truth `pull` resolves afterward.

    `filters.drug_class`, if given, is applied the same way and for the same
    reason as `ta` -- the API has no server-side notion of a drug class either,
    so it is judged client-side, before `limit`, by `resolve_study_drug_class_matches`.
    It shares `ta`'s widened scan cap rather than adding one of its own.

    `filters.org`, if given, is unlike `ta`: the API *can* express it
    server-side (AREA[LeadSponsorName] in query.term), so it needs none of
    `ta`'s scan-cap widening -- it narrows `query.term` the same way
    phase/since already do, and `max_pages`/`target` stay as they are. The
    client-side `_organization_matches` check is only a safety net behind
    that, the same role phase/since's re-checks already play.

    Upserts rather than replaces: raw.studies is updated/inserted per nct_id,
    and every child table (design_outcomes, conditions, browse_*) has its rows
    for *this pull's* nct_ids replaced -- studies landed by earlier pulls with
    different filters are never touched. `filters.replace` overrides this: see
    `ingest/upsert.py`'s `ensure_table`.

    `on_page`, if given, is called as `on_page(page_index, studies_collected)`
    after each page is fetched and filtered -- the eventual `limit` isn't known
    until pagination stops (a page can contain studies later dropped by
    `since`/phase/`ta`/`org` re-checks), so this reports pre-truncation
    progress rather than a percentage of an unknowable total.
    """
    aact_phases = normalize_phases(list(filters.phases))
    query_term = _build_query_term(aact_phases, filters.since, filters.org)
    since_iso = filters.since.isoformat() if filters.since else None

    wanted_ta_ids: Optional[set[str]] = set(filters.ta) if filters.ta else None
    ta_mapping = load_ta_mapping(con) if wanted_ta_ids else None
    wanted_org_fragments: Optional[tuple[str, ...]] = (
        tuple(o.lower() for o in filters.org) if filters.org else None
    )
    wanted_class_ids: Optional[set[str]] = set(filters.drug_class) if filters.drug_class else None
    class_mapping = load_drug_class_mapping(con) if wanted_class_ids else None
    max_pages = MAX_PAGES_TA_FILTERED if (wanted_ta_ids or wanted_class_ids) else MAX_PAGES

    studies: list[dict] = []
    outcomes_by_nct: dict[str, list[dict]] = {}
    results_by_nct: dict[str, dict[str, list[tuple]]] = {}
    arms_by_nct: dict[str, list[dict]] = {}
    conditions_by_nct: dict[str, list[dict]] = {}
    browse_conditions_by_nct: dict[str, list[dict]] = {}
    browse_interventions_by_nct: dict[str, list[dict]] = {}
    condition_branches_by_nct: dict[str, list[dict]] = {}
    interventions_by_nct: dict[str, dict[str, list[tuple]]] = {}
    seen_nct_ids: set[str] = set()
    target = max(filters.limit * 3, filters.limit + 50)
    studies_scanned = 0

    page_token: Optional[str] = None
    hit_scan_cap = False
    for page_index in range(1, max_pages + 1):
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
            if wanted_org_fragments and not _organization_matches(
                row["organization"], wanted_org_fragments
            ):
                continue
            studies_scanned += 1

            browse_conditions = _extract_mesh_rows(
                study, module="conditionBrowseModule", mesh_type="condition"
            )
            browse_interventions = _extract_mesh_rows(
                study, module="interventionBrowseModule", mesh_type="intervention"
            )
            condition_branches = _extract_condition_branch_rows(study)

            if wanted_ta_ids is not None:
                branch_tree_prefixes = [
                    (prefix, b["branch_name"])
                    for b in condition_branches
                    if (prefix := branch_abbrev_to_tree_prefix(b["branch_abbrev"]))
                ]
                matches = resolve_study_ta_matches(
                    conditions=[(m["mesh_term"], m["mesh_term_normalised"]) for m in browse_conditions],
                    interventions=[
                        (m["mesh_term"], m["mesh_term_normalised"]) for m in browse_interventions
                    ],
                    tree_numbers={},
                    branch_tree_prefixes=branch_tree_prefixes,
                    mapping=ta_mapping,
                )
                if not (set(matches) & wanted_ta_ids):
                    continue

            study_interventions = extract_ctgov_interventions(study)

            if wanted_class_ids is not None:
                # Judged by the exact same per-study match `drug_class/resolver.py`
                # uses to write conformed.study_drug_class, so a pull-time match
                # always agrees with the truth `pull` resolves afterward -- the
                # property `--ta` already has via `resolve_study_ta_matches`.
                class_matches = resolve_study_drug_class_matches(
                    interventions=_interventions_for_matching(study_interventions),
                    mesh_terms=[m["mesh_term"] for m in browse_interventions],
                    ancestors=[
                        row_[1] for row_ in study_interventions["browse_intervention_ancestors"]
                    ],
                    branches=[
                        row_[2]
                        for row_ in study_interventions["browse_intervention_branches"]
                        if row_[2]
                    ],
                    mapping=class_mapping,
                )
                if not (set(class_matches) & wanted_class_ids):
                    continue

            seen_nct_ids.add(nct_id)
            studies.append(row)
            outcomes_by_nct[nct_id] = _extract_outcome_rows(study)
            if filters.with_results:
                # Parsed here rather than after truncation because the payload
                # is only in hand while the page is: `studies` is truncated to
                # `--limit` below, and rows for the dropped studies are simply
                # never read back out of this dict.
                results_by_nct[nct_id] = extract_ctgov_results(study)
            arms_by_nct[nct_id] = _extract_arm_rows(study)
            conditions_by_nct[nct_id] = _extract_condition_rows(study)
            browse_conditions_by_nct[nct_id] = browse_conditions
            browse_interventions_by_nct[nct_id] = browse_interventions
            condition_branches_by_nct[nct_id] = condition_branches
            interventions_by_nct[nct_id] = study_interventions

        if on_page:
            on_page(page_index, len(studies))

        page_token = payload.get("nextPageToken")
        if not page_token or len(studies) >= target:
            break
        if page_index == max_pages:
            hit_scan_cap = len(studies) < filters.limit

    studies.sort(key=lambda r: r["start_date"] or "", reverse=True)
    studies = studies[: filters.limit]
    kept_nct_ids = [s["nct_id"] for s in studies]
    outcomes = [o for s in studies for o in outcomes_by_nct.get(s["nct_id"], [])]
    arms = [a for nct_id in kept_nct_ids for a in arms_by_nct.get(nct_id, [])]
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
    intervention_rows: dict[str, list[tuple]] = {name: [] for name in INTERVENTION_TABLE_NAMES}
    for nct_id in kept_nct_ids:
        for name, table_rows in (interventions_by_nct.get(nct_id) or {}).items():
            intervention_rows[name].extend(table_rows)

    schema = SchemaReconciler(con, replace=filters.replace)
    schema.ensure("studies", STUDIES_DDL)
    upsert_rows(
        con,
        "studies",
        list(STUDY_COLUMNS),
        ["nct_id"],
        [tuple(s.get(column) for column in STUDY_COLUMNS) for s in studies],
    )

    schema.ensure(
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

    schema.ensure("design_groups", DESIGN_GROUPS_DDL)
    replace_children(
        con,
        "design_groups",
        list(DESIGN_GROUPS_COLUMNS),
        "nct_id",
        kept_nct_ids,
        [tuple(a.get(c) for c in DESIGN_GROUPS_COLUMNS) for a in arms],
    )

    schema.ensure("conditions", "nct_id VARCHAR, name VARCHAR")
    replace_children(
        con,
        "conditions",
        ["nct_id", "name"],
        "nct_id",
        kept_nct_ids,
        [(c["nct_id"], c["name"]) for c in conditions],
    )

    schema.ensure(
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

    schema.ensure(
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

    schema.ensure(
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

    for table, ddl, columns in INTERVENTION_TABLES:
        schema.ensure(table, ddl)
        replace_children(
            con, table, list(columns), "nct_id", kept_nct_ids, intervention_rows[table]
        )

    results_rows: dict[str, list[tuple]] = {name: [] for name in RESULTS_TABLE_NAMES}
    if filters.with_results:
        for nct_id in kept_nct_ids:
            for name, rows in (results_by_nct.get(nct_id) or {}).items():
                results_rows[name].extend(rows)
        for table, ddl, columns in RESULTS_TABLES:
            schema.ensure(table, ddl)
            replace_children(
                con, table, list(columns), "nct_id", kept_nct_ids, results_rows[table]
            )

    row_counts = {
        "studies": len(studies),
        "design_outcomes": len(outcomes),
        "design_groups": len(arms),
        "conditions": len(conditions),
        "browse_conditions": len(browse_conditions),
        "browse_interventions": len(browse_interventions),
        "browse_condition_branches": len(condition_branches),
        **{name: len(rows) for name, rows in intervention_rows.items()},
    }
    if filters.with_results:
        row_counts.update({name: len(rows) for name, rows in results_rows.items()})
    log_entry = write_pull_log(
        con,
        source=SOURCE,
        filters=filters.as_dict(),
        row_counts=row_counts,
        source_tables=SOURCE_TABLES + (RESULTS_SOURCE_TABLES if filters.with_results else ()),
    )
    return {
        **log_entry,
        "row_counts": row_counts,
        "nct_ids": kept_nct_ids,
        "migrations": schema.changes,
        "studies_scanned": studies_scanned,
        "hit_scan_cap": hit_scan_cap,
    }
